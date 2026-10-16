import copy

import pytest

from tools.maps.encounter_catalog import entity_records
from tools.maps.enemy_attributes import materialize_enemy_attributes


SOURCE = '''entity {
    entityDef authored_imp {
        inherit = "ai/fodder/imp";
        class = "idAI2";
        edit = {
            aiHealth = {
                components = {
                    components[0] = { max = 200; starting = 150; flags = "KEEP"; }
                    components[1] = { max = 80; regenInterval = { value = 3; } }
                }
            }
            aiEditable = {
                damageOutModifiers = {
                    num = 2;
                    item[0] = { damageSource = "DAMAGESRC_MELEE"; damageModifier = 0.5; }
                    item[1] = { damageSource = "DAMAGESRC_FIRE"; damageModifier = 0; }
                }
            }
            renderModelInfo = { model = "imp.md6"; scale = { x = 1.25; y = 0.75; z = 1; } }
            clipModelInfo = { size = { x = 2; y = 3; z = 4; } }
            spawnPosition = { x = 123.456789; y = -0.001; z = 8; }
        }
    }
}
'''


def properties(block):
    return next(entity_records(block))["edit"]


def test_neutral_clone_preserves_authored_bytes():
    result = materialize_enemy_attributes(SOURCE, "ap_v070_ai_imp__ap_v070_s00100")
    assert result == SOURCE.replace("entityDef authored_imp", "entityDef ap_v070_ai_imp__ap_v070_s00100", 1)


def test_instance_attributes_preserve_shield_collision_and_source():
    original = properties(SOURCE)
    result = materialize_enemy_attributes(
        SOURCE, "ap_v070_ai_imp__ap_v070_s00125",
        health_percent=50, damage_percent=150, size_percent=200,
    )
    edit = properties(result)
    hitpoints = edit["aiHealth"]["components"]["components[0]"]
    assert hitpoints == {"max": 100, "starting": 75, "flags": "KEEP"}
    assert edit["aiHealth"]["components"]["components[1]"] == original["aiHealth"]["components"]["components[1]"]
    assert edit["clipModelInfo"] == original["clipModelInfo"]
    assert edit["spawnPosition"] == original["spawnPosition"]
    assert edit["renderModelInfo"] == {"model": "imp.md6", "scale": {"x": 2.5, "y": 1.5, "z": 2}}
    modifiers = edit["aiEditable"]["damageOutModifiers"]
    assert modifiers["num"] == 3
    assert [modifiers[f"item[{i}]"] for i in range(3)] == [
        {"damageSource": "DAMAGESRC_MELEE", "damageModifier": 0.75},
        {"damageSource": "DAMAGESRC_FIRE", "damageModifier": 0},
        {"damageSource": "DAMAGESRC_ALL", "damageModifier": 1.5},
    ]
    assert properties(SOURCE) == original
    assert "spawnPosition = { x = 123.456789; y = -0.001; z = 8; }" in result


def test_damage_preserves_first_match_and_existing_all_fallback():
    source = SOURCE.replace('damageSource = "DAMAGESRC_FIRE"', 'damageSource = "DAMAGESRC_FIRE | DAMAGESRC_ALL"')
    edit = properties(materialize_enemy_attributes(source, "ap_v070_ai_imp__ap_v070_s00100", damage_percent=200))
    modifiers = edit["aiEditable"]["damageOutModifiers"]
    assert modifiers["num"] == 2
    assert modifiers["item[0]"] == {"damageSource": "DAMAGESRC_MELEE", "damageModifier": 1}
    assert modifiers["item[1]"] == {"damageSource": "DAMAGESRC_FIRE | DAMAGESRC_ALL", "damageModifier": 0}


def test_health_scaling_preserves_native_default_value_structure():
    source = SOURCE.replace("max = 200; starting = 150;", "max = { defaultValue = 6000; } starting = { defaultValue = 4500; }")
    edit = properties(materialize_enemy_attributes(source, "ap_v070_ai_archvile__ap_v070_s00100", health_percent=50))
    assert edit["aiHealth"]["components"]["components[0]"] == {
        "max": {"defaultValue": 3000}, "starting": {"defaultValue": 2250}, "flags": "KEEP",
    }
    assert edit["aiHealth"]["components"]["components[1]"] == properties(source)["aiHealth"]["components"]["components[1]"]
    with pytest.raises(ValueError, match="Unqualified branched health base"):
        materialize_enemy_attributes(source.replace("defaultValue = 6000;", "defaultValue = 6000; branch = 5000;"),
            "ap_v070_ai_archvile__ap_v070_s00100", health_percent=50)


def test_qualified_inherited_bases_are_isolated_between_clones():
    source = '''entity { entityDef authored_imp {
        inherit = "ai/fodder/imp"; class = "idAI2"; edit = { renderModelInfo = { model = "imp.md6"; } }
    } }'''
    base = {
        "aiHealth": {"components": {"components[0]": {"max": 400, "starting": 300}}},
        "aiEditable": {"damageOutModifiers": {"num": 0}},
        "renderModelInfo": {"scale": {"x": 1, "y": 1, "z": 1}},
    }
    saved = copy.deepcopy(base)
    small = properties(materialize_enemy_attributes(source, "ap_v070_ai_small__ap_v070_s00001", health_percent=1, damage_percent=1, size_percent=1, base_properties=base))
    large = properties(materialize_enemy_attributes(source, "ap_v070_ai_large__ap_v070_s10000", health_percent=10000, damage_percent=10000, size_percent=10000, base_properties=base))
    assert small["aiHealth"]["components"]["components[0]"] == {"max": 4, "starting": 3}
    assert large["aiHealth"]["components"]["components[0]"] == {"max": 40000, "starting": 30000}
    assert small["renderModelInfo"]["scale"] == {"x": 0.01, "y": 0.01, "z": 0.01}
    assert large["renderModelInfo"]["scale"] == {"x": 100, "y": 100, "z": 100}
    assert base == saved
    assert properties(source) == {"renderModelInfo": {"model": "imp.md6"}}


@pytest.mark.parametrize("attribute", ["health_percent", "damage_percent", "size_percent"])
def test_missing_inherited_base_is_refused(attribute):
    source = 'entity { entityDef ai { inherit = "ai/imp"; class = "idAI2"; edit = { } } }'
    with pytest.raises(ValueError, match="Unqualified inherited base"):
        materialize_enemy_attributes(source, "ap_v070_ai_private__ap_v070_s00100", **{attribute: 150})


@pytest.mark.parametrize("name,value", [("health_percent", True), ("damage_percent", 0), ("size_percent", 10001), ("health_percent", 1.5)])
def test_invalid_percent_is_refused(name, value):
    with pytest.raises(ValueError, match="must be an integer"):
        materialize_enemy_attributes(SOURCE, "ap_v070_ai_private__ap_v070_s00100", **{name: value})


def test_player_and_ambiguous_source_are_refused():
    with pytest.raises(ValueError, match="exactly one idAI2"):
        materialize_enemy_attributes(SOURCE.replace('class = "idAI2"', 'class = "idPlayer"'), "ap_v070_ai_private__ap_v070_s00100")
    with pytest.raises(ValueError, match="exactly one idAI2"):
        materialize_enemy_attributes(SOURCE + SOURCE, "ap_v070_ai_private__ap_v070_s00100")


@pytest.mark.parametrize("name", ["private__ap_v070_s00150", "ap_v070_ai___ap_v070_s00150", "ap_v070_ai_a__ap_v070_s00000", "ap_v070_ai_" + "a" * 500 + "__ap_v070_s00150"])
def test_names_outside_native_speed_route_are_refused(name):
    with pytest.raises(ValueError, match="ap_v070_ai_"):
        materialize_enemy_attributes(SOURCE, name)
