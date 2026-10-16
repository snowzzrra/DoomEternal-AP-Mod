import copy
import hashlib

import pytest

from tools.maps.encounter_catalog import entity_records
from tools.maps.enemy_attribute_baselines import resolve_enemy_attribute_baselines
from tools.maps.enemy_attributes import materialize_enemy_attributes


SOURCE = '''entity { entityDef source_imp {
    inherit = "ai/fodder/imp"; class = "idAI2";
    edit = {
        aiHealth = { components = {
            components[0] = { max = 200; starting = 150; }
            components[1] = { max = 80; }
        } }
        clipModelInfo = { size = { x = 0.5; y = 0.5; z = 1.6; } }
    }
} }'''


def qualification(source=SOURCE, scale=None):
    record = next(entity_records(source))
    return {
        "schema_version": 1, "qualification": "offline_native_defaults",
        "native": {"damage_out_modifiers": {"value": {"num": 0}}},
        "templates": {hashlib.sha256(source.strip().encode()).hexdigest(): {
            "entity": record["name"], "inherit": record["inherit"],
            "scale": scale or {"x": 1, "y": 1, "z": 1},
        }},
    }


def test_native_defaults_apply_to_only_the_qualified_clone():
    catalog = qualification()
    saved = copy.deepcopy(catalog)
    bases = resolve_enemy_attribute_baselines(SOURCE, catalog=catalog)
    result = materialize_enemy_attributes(SOURCE, "ap_v070_ai_imp__ap_v070_s00100",
        health_percent=200, damage_percent=150, size_percent=50, base_properties=bases)
    edit = next(entity_records(result))["edit"]
    assert edit["aiHealth"]["components"] == {
        "components[0]": {"max": 400, "starting": 300}, "components[1]": {"max": 80},
    }
    assert edit["aiEditable"]["damageOutModifiers"] == {
        "num": 1, "item[0]": {"damageSource": "DAMAGESRC_ALL", "damageModifier": 1.5},
    }
    assert edit["renderModelInfo"]["scale"] == {"x": 0.5, "y": 0.5, "z": 0.5}
    assert edit["clipModelInfo"] == next(entity_records(SOURCE))["edit"]["clipModelInfo"]
    bases["renderModelInfo"]["scale"]["x"] = 9
    bases["aiEditable"]["damageOutModifiers"]["num"] = 9
    assert resolve_enemy_attribute_baselines(SOURCE, catalog=catalog)["renderModelInfo"]["scale"]["x"] == 1
    assert catalog == saved


def test_branched_health_and_authored_scale_keep_their_structure():
    source = SOURCE.replace("max = 200; starting = 150;",
        "max = { defaultValue = 6000; } starting = { defaultValue = 6000; }")
    bases = resolve_enemy_attribute_baselines(source, catalog=qualification(source, {"x": 8, "y": 8, "z": 8}))
    assert bases["aiHealth"]["components"]["components[0]"] == {
        "max": {"defaultValue": 6000}, "starting": {"defaultValue": 6000},
    }
    assert bases["renderModelInfo"]["scale"] == {"x": 8, "y": 8, "z": 8}


def test_missing_hitpoint_base_is_not_invented():
    source = SOURCE.replace("components[0] = { max = 200; starting = 150; }", "")
    bases = resolve_enemy_attribute_baselines(source, catalog=qualification(source))
    with pytest.raises(ValueError, match="Unqualified inherited base"):
        materialize_enemy_attributes(source, "ap_v070_ai_imp__ap_v070_s00100",
            health_percent=200, base_properties=bases)


def test_qualified_hitpoint_input_preserves_shield_and_death_rule():
    source = SOURCE.replace("components[0] = { max = 200; starting = 150; }", "")
    source = source.replace('ai/fodder/imp', 'ai/ambient/buffpod').replace(
        "clipModelInfo =", "aiEditable = { death = { onlyDieByGK = true; } } clipModelInfo =")
    catalog = qualification(source)
    next(iter(catalog["templates"].values()))["hitpoint_input"] = {"max": 100, "starting": 100}
    saved = copy.deepcopy(catalog)
    original = next(entity_records(source))["edit"]
    bases = resolve_enemy_attribute_baselines(source, catalog=catalog)
    edit = next(entity_records(materialize_enemy_attributes(source,
        "ap_v070_ai_buffpod__ap_v070_s00100", health_percent=200, base_properties=bases)))["edit"]
    assert edit["aiHealth"]["components"] == {
        "components[0]": {"max": 200, "starting": 200},
        "components[1]": original["aiHealth"]["components"]["components[1]"],
    }
    assert edit["aiEditable"]["death"] == {"onlyDieByGK": True}
    bases["aiHealth"]["components"]["components[0]"]["max"] = 999
    assert resolve_enemy_attribute_baselines(source, catalog=catalog)["aiHealth"]["components"]["components[0]"]["max"] == 100
    assert catalog == saved


@pytest.mark.parametrize("authored, expected", [
    ("max = 200; starting = 150;", {"max": 400, "starting": 300}),
    ("max = 200;", {"max": 400, "starting": 200}),
    ("max = { defaultValue = 6000; } starting = { defaultValue = 6000; }",
        {"max": {"defaultValue": 12000}, "starting": {"defaultValue": 12000}}),
])
def test_authored_hitpoints_override_qualified_input(authored, expected):
    source = SOURCE.replace("max = 200; starting = 150;", authored)
    catalog = qualification(source)
    next(iter(catalog["templates"].values()))["hitpoint_input"] = {"max": 100, "starting": 100}
    bases = resolve_enemy_attribute_baselines(source, catalog=catalog)
    edit = next(entity_records(materialize_enemy_attributes(source,
        "ap_v070_ai_imp__ap_v070_s00100", health_percent=200, base_properties=bases)))["edit"]
    assert edit["aiHealth"]["components"]["components[0]"] == expected
    assert edit["aiHealth"]["components"]["components[1]"] == {"max": 80}


def test_changed_source_cannot_reuse_qualified_defaults():
    with pytest.raises(ValueError, match="Unqualified enemy attribute source SHA-256"):
        resolve_enemy_attribute_baselines(SOURCE.replace("ai/fodder/imp", "ai/other"), catalog=qualification())
