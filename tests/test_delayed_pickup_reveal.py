import re

import pytest

from tools.maps.ap_map_generator import (
    _spawn_position_from_block,
    append_target_to_named_entity,
    build_universal_physical_policy,
    extract_target_names,
    find_entity_block_bounds,
    generate_independent_pickup_trigger,
    generate_inert_location_visual,
    replace_pickup_reveal_target,
)
from tools.maps.encounter_catalog import parse_object


def entity(name, classname, edit):
    return f'''entity {{
entityDef {name} {{
inherit = "test";
class = "{classname}";
edit = {{ {edit} }}
}}
}}
'''


def source():
    return (
        entity("pickup", "idProp2", 'flags = { hide = true; } spawnPosition = { z = 1.95; y = 61; } whenToSave = "SGT_CHECKPOINT";')
        + entity("reveal", "idTarget_Show", 'targets = { num = 1; item[0] = "pickup"; }')
        + entity("gate", "idTarget_Count", 'delay = 1; targets = { num = 3; item[0] = "reveal"; item[1] = "sound"; item[2] = "secret"; }')
    )


def block(content, name):
    return content[slice(*find_entity_block_bounds(content, name))]


def properties(content, name):
    text = block(content, name)
    header = re.search(r'\bentityDef\s+\S+\s*\{', text)
    return parse_object(text[header.end():].rsplit("}", 2)[0])["edit"]


@pytest.mark.parametrize("delayed_layer", [False, True])
def test_delayed_reward_requires_relay_activation_before_touch(delayed_layer):
    policy = {
        "independent_ap_trigger": True,
        "gate_relay": "gate",
        "reveal_entity": "reveal",
        "independent_position": [0.0, 61.0, 1.95],
    }
    original = source()
    if delayed_layer:
        original = original.replace('entityDef pickup', 'layers { "reward_layer" } entityDef pickup')
        policy.update(preserve_layers=False, preserve_visual_layers=False)
    rewritten = replace_pickup_reveal_target(original, "pickup", 1, policy)
    rewritten = append_target_to_named_entity(rewritten, "gate", "ap_independent_pickup")
    assert properties(rewritten, "gate")["delay"] == 1
    assert extract_target_names(block(rewritten, "gate")) == ["reveal", "sound", "secret", "ap_independent_pickup"]
    assert extract_target_names(block(rewritten, "reveal")) == ["ap_location_visual_1"]
    generated = build_universal_physical_policy("AP_CHECK_PICKUP", 1, block(original, "pickup"), "ap/model", {"preserve_rotation": False})
    generated.update(policy)
    trigger = generate_independent_pickup_trigger("pickup", "AP_CHECK_PICKUP", block(original, "pickup"), generated)
    visual = generate_inert_location_visual(block(original, "pickup"), generated)
    assert properties(trigger, "ap_independent_pickup")["triggerFirst"] is True
    assert _spawn_position_from_block(trigger) == (0.0, 61.0, 1.95)
    assert properties(visual, "ap_location_visual_1")["flags"]["hide"] is True
    assert properties(visual, "ap_location_visual_1")["whenToSave"] == "SGT_CHECKPOINT"
    if delayed_layer:
        assert 'reward_layer' not in trigger
        assert 'reward_layer' not in visual


def test_crystal_contact_is_armed_without_requiring_the_vanilla_use_relay():
    source = entity('crystal', 'idInteractable_WorldCache',
                    'automapPropertiesDecl = "argent_cell"; spawnPosition = { x = 16; y = 22; z = 4; }')
    source = source.replace('inherit = "test";', 'inherit = "progress/argent_cell";')
    trigger = generate_independent_pickup_trigger('crystal', 'AP_CHECK_CRYSTAL', source,
                {'gate_relay': 'target_relay_argent_cell_useable'})
    assert properties(trigger, 'ap_independent_crystal').get('triggerFirst', False) is False
    assert properties(trigger, 'ap_independent_crystal')['triggerOnce'] is True


@pytest.mark.parametrize("changed", [
    source().replace('hide = true;', 'hide = false;'),
    source().replace('item[0] = "pickup";', 'item[0] = "another_pickup";'),
])
def test_reveal_rejects_source_drift(changed):
    with pytest.raises(ValueError):
        replace_pickup_reveal_target(changed, "pickup", 1, {"reveal_entity": "reveal", "gate_relay": "gate"})
