import json
import re
from pathlib import Path

import pytest

from doom_eap.content.content_catalog import load_content_catalog
from doom_eap.contracts.ap_visual_contract import load_ap_visual_contract
from tools.maps.ap_map_generator import (
    entity_definition_index,
    extract_target_names,
    find_entity_block_bounds,
    generate_map,
)

ROOT = Path(__file__).resolve().parents[1]
DLC_MAPS = ('e4m1_rig', 'e4m2_swamp', 'e4m3_mcity', 'e5m1_spear', 'e5m2_earth', 'e5m3_hell')
VANILLA_ARMOR = {
    'e4m1_rig': 'pickups_pickup_armor_large_5',
    'e4m2_swamp': 'pickups_pickup_armor_large_4',
    'e4m3_mcity': 'game_pickup_armor_large_1',
}


def entity(text, index, name):
    bounds = find_entity_block_bounds(text, name, index=index)
    assert bounds is not None, name
    return text[bounds[0]:bounds[1] + 1]


def position(block):
    body = re.search(r'\bspawnPosition\s*=\s*\{([^}]+)\}', block)[1]
    values = dict(re.findall(r'\b([xyz])\s*=\s*([-+\d.eE]+);', body))
    return tuple(float(values[axis]) for axis in 'xyz')


def assert_pickup_chain(source, source_index, compiled, compiled_index, pickup, model):
    location_id = pickup.location_id
    original = pickup.ap_check.removeprefix('AP_CHECK_').lower()
    assert original not in compiled_index
    trigger = entity(compiled, compiled_index, 'ap_independent_' + original)
    assert 'triggerOnce = true;' in trigger
    assert pickup.ap_check in extract_target_names(trigger)
    expected = position(entity(source, source_index, original))
    assert position(trigger) == pytest.approx(expected)
    visual = entity(compiled, compiled_index, f'ap_location_visual_{location_id}')
    original_block = entity(source, source_index, original)
    layers = re.search(r'\blayers\s*\{[^}]*\}', original_block)
    if layers:
        assert layers[0] in trigger
        assert layers[0] in visual
    if 7772053 <= location_id <= 7772055:
        assert 'inherit = "pickup/mega_armor";' in original_block
        assert 'extralives_none' not in original_block
    assert f'model = "{model}";' in visual
    assert position(visual) == pytest.approx((expected[0], expected[1], expected[2] + 1.5))
    relay = entity(compiled, compiled_index, pickup.ap_check)
    assert extract_target_names(relay) == [f'ap_notify_location_{location_id}', f'ap_event_{location_id}']
    notification = entity(compiled, compiled_index, f'ap_notify_location_{location_id}')
    assert f'#str_ap_location_{location_id}' in notification
    event = entity(compiled, compiled_index, f'ap_event_{location_id}')
    assert f'echo AP_CHECK_EVENT_{location_id}; condump ap_event_{location_id}.txt;' in event


@pytest.mark.integration
@pytest.mark.parametrize('map_key', DLC_MAPS)
def test_dlc_pickups_compile_to_independent_check_chains(tmp_path, map_key):
    catalog = load_content_catalog()
    spec = catalog.maps[map_key]
    pickups = [row for row in catalog.physical_locations
               if row.map_key == map_key and 7772000 <= row.location_id < 7772100]
    assert pickups
    output, manifest = tmp_path / 'output.entities', tmp_path / 'manifest.json'
    source_path = ROOT / 'vanillamaps' / spec.source_file
    generate_map(source_path, output, spec.level_config_path, manifest, {}, enable_notifications=False)
    source, compiled = source_path.read_text(encoding='utf-8'), output.read_text(encoding='utf-8')
    source_index, compiled_index = entity_definition_index(source), entity_definition_index(compiled)
    emitted = json.loads(manifest.read_text())
    model = load_ap_visual_contract()['model']
    for pickup in pickups:
        assert emitted[pickup.ap_check] == pickup.location_id
        assert_pickup_chain(source, source_index, compiled, compiled_index, pickup, model)
    vanilla = [VANILLA_ARMOR[map_key]] if map_key in VANILLA_ARMOR else []
    for match in re.finditer(r'\bentityDef\s+(\w*mega_armor_\w*)\s*\{', source):
        name = match[1]
        if 'game/sp/extralives/extralives_none' in entity(source, source_index, name):
            vanilla.append(name)
    for name in vanilla:
        assert entity(compiled, compiled_index, name) == entity(source, source_index, name)
