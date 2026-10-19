import hashlib
import json

import pytest

from tools.maps.native_enemy_inventory import EMITTERS, extract_native_inventory, inventory_summary


def entity(name, kind, edit, layers=""):
    return f'entity {{ {layers} entityDef {name} {{ inherit = "test/{name}"; class = "{kind}"; edit = {{ {edit} }} }} }}\n'


def test_inventory_covers_native_families_without_requiring_completion_or_species_permissions():
    events = " ".join(f'item[{index}] = {{ eventCall = {{ eventDef = "{event}"; args = {{ num = 0; }} }} }}'
                      for index, event in enumerate(EMITTERS))
    source = entity("arena", "idEncounterManager", f'encounterComponent = {{ entityEvents = {{ num = 1; item[0] = {{ entity = "arena"; events = {{ num = {len(EMITTERS)}; {events} }} }} }} }}')
    source += entity("point", "idTarget_Spawn", 'entityDefs = { num = 1; item[0] = { name = "template"; } } targets = { num = 1; item[0] = "door"; }')
    source += entity("door", "idLogicRelay", "")
    source += entity("template", "idAI2", "") + entity("independent", "idAI2", "")
    result = extract_native_inventory(source, "map", profiles={"arena": {"protection": {
        "spawn_events": [{"script": 0, "event": 0}], "reason": "protected boss"}}})
    assert len(result["emitters"]) == len(EMITTERS)
    assert {row["family"] for row in result["emitters"]} == set(EMITTERS.values())
    assert all(row["owner"] == "arena" for row in result["emitters"])
    assert [(row["event_index"], row["protected_reason"]) for row in result["emitters"] if row["protected_reason"]] == [(0, "protected boss")]
    assert {row["entity"]: row["point_template"] for row in result["actors"]} == {"independent": False, "template": True}
    assert result["points"][0]["references"] == ["door", "template"]
    assert inventory_summary({"maps": {"map": result}})["runtime_coverage_verified"] is False


def test_external_timeline_and_unknown_emitter_are_retained():
    source = entity("timeline", "idTarget_Timeline", '''componentTimeLine = { entityEvents = { num = 1; item[0] = {
        entity = "arena"; events = { num = 1; item[0] = { eventCall = { eventDef = "spawnUnknownAI"; args = { num = 0; } } } }
    } } }''')
    row, = extract_native_inventory(source, "map")["emitters"]
    assert row["family"] == "unknown"
    assert row["protected_reason"] is None
    assert row["script_index"] is None
    assert row["owner"] == "arena"


def test_direct_inventory_separates_activation_edges_from_groups_and_removal():
    source = entity("point", "idTarget_Spawn", 'entityDefs = { num = 0; }')
    source += entity("group", "idTargetSpawnGroup", 'spawners = { num = 1; item[0] = "point"; } targets = { num = 1; item[0] = "point"; }')
    source += entity("actor", "idAI2", 'targets = { num = 1; item[0] = "point"; }')
    source += entity("parent", "idTarget_Spawn_Parent", 'targets = { num = 1; item[0] = "point"; }')
    source += entity("point_source", "idTarget_Spawn", 'targets = { num = 1; item[0] = "point"; }')
    source += entity("facing", "idTrigger_Facing", 'targets = { num = 1; item[0] = "point"; }')
    source += entity("trigger", "idTarget_Count", 'targets = { num = 3; item[0] = "point"; item[1] = "point"; item[2] = "group"; }')
    source += entity("removal", "idTarget_Remove", 'targets = { num = 1; item[0] = "point"; }')
    source += entity("arena", "idEncounterManager", 'systemSpawnTargets = { num = 1; item[0] = "point"; }')
    inventory = extract_native_inventory(source, "map")
    assert [(row["entity"], row["path"], row["reference"]) for row in inventory["direct_activations"]] == [
        ("facing", "targets.item[0]", "point"), ("trigger", "targets.item[0]", "point"), ("trigger", "targets.item[1]", "point")]
    assert {row["entity"] for row in inventory["point_dependencies"]} == {"arena", "actor", "group", "parent", "point_source"}


def test_names_and_layers_are_hints_while_protection_uses_explicit_source_rules():
    event = 'encounterComponent = { entityEvents = { num = 1; item[0] = { events = { num = 1; item[0] = { eventCall = { eventDef = "spawnAI"; args = { num = 0; } } } } } } }'
    source = entity("boss_adds", "idEncounterManager", event)
    source += entity("training", "idEncounterManager", event, 'layers { "cp_tutorial" }')
    result = extract_native_inventory(source, "map")
    assert all(row["protected_reason"] is None for row in result["emitters"])
    assert {row["entity"]: row["tutorial_hint"] for row in result["emitters"]} == {"boss_adds": False, "training": True}
    protected = extract_native_inventory(source, "map", profiles={"training": {"protection": {
        "entire_encounter": True, "reason": "dedicated tutorial"}}})
    assert next(row for row in protected["emitters"] if row["entity"] == "training")["protected_reason"] == "dedicated tutorial"




def test_duplicate_origins_are_rejected():
    source = entity("actor", "idAI2", "") * 2
    with pytest.raises(ValueError, match="Duplicate native entity"):
        extract_native_inventory(source, "map")


def test_source_catalog_includes_native_origins_without_occurrence_qualification(tmp_path):
    from tools.maps.native_enemy_inventory import build_native_inventory

    source = entity("arena", "idEncounterManager", '''encounterComponent = { entityEvents = { num = 1; item[0] = {
        entity = "arena"; events = { num = 1; item[0] = { eventCall = { eventDef = "spawnSpirit"; args = { num = 0; } } } }
    } } }''')
    inputs = {
        "content/catalog/encounter_profiles.json": {"maps": {}, "qualification_gaps": []},
        "content/maps/map/descriptor.json": {"source_file": "map.map", "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                                             "runtime_map": "game/map", "resource_path": "map.resources"},
        "content/maps/map/locations.json": {},
        "content/maps/map/onboarding.json": {},
    }
    for relative, value in inputs.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
    (tmp_path / "vanillamaps").mkdir()
    (tmp_path / "vanillamaps/map.map").write_bytes(source.encode("utf-8"))
    catalog = build_native_inventory(tmp_path)
    origin, = catalog["maps"]["map"]["emitters"]
    assert origin["family"] == "spirit" and origin["protected_reason"] is None


def test_native_recipe_catalog_preserves_logical_alias_and_extracts_summon_requests():
    from pathlib import Path
    from tools.maps.native_enemy_inventory import native_recipe_catalog

    registry = json.loads((Path(__file__).parents[1] / "content/catalog/enemy_species.json").read_bytes())
    source = entity("arena", "idEncounterManager", '''encounterComponent = { entityEvents = { num = 1; item[0] = {
        events = { num = 1; item[0] = { eventCall = { eventDef = "spawnSingleAI"; args = { num = 3;
            item[0] = { eEncounterSpawnType_t = "ENCOUNTER_SPAWN_MAYKR_ANGEL"; }
            item[1] = { entity = "point"; } item[2] = { string = "label"; } } } } }
    } } }''')
    source += entity("point", "idTarget_Spawn", 'entityDefs = { num = 1; item[0] = { name = "blood"; } }')
    source += entity("blood", "idAI2", "").replace('inherit = "test/blood"', 'inherit = "ai/heavy/bloodangel"')
    source += entity("summoner", "idArchvileTemplate", '''summonList = { aiSpawnTypeList = { num = 3;
        item[0] = "ENCOUNTER_SPAWN_IMP"; item[1] = "ENCOUNTER_SPAWN_CACODEMON";
        item[2] = "ENCOUNTER_SPAWN_IMP"; } }''')
    inventory = {"maps": {"map": extract_native_inventory(source, "map")}}
    catalog = native_recipe_catalog(inventory, registry)["maps"]["map"]
    assert not catalog["unresolved"]
    request = next(row for row in catalog["origins"] if row["family"] == "immediate")
    assert request["logical_types"] == ["ENCOUNTER_SPAWN_MAYKR_ANGEL"]
    assert request["combat_cost"] == "24"
    summons = [row for row in catalog["origins"] if row["family"] == "summon"]
    assert len(summons) == 2
    assert {row["spawn_type"]: row["logical_count"] for row in summons} == {
        "ENCOUNTER_SPAWN_IMP": 2, "ENCOUNTER_SPAWN_CACODEMON": 1}
    assert all(row["maximum_emissions"] == 0 for row in summons)
    assert len({row["phase"] for row in summons}) == 1


def test_null_single_point_and_archvile_template_have_no_native_birth_but_spirit_can_use_default_group():
    from pathlib import Path
    from tools.maps.native_enemy_inventory import native_recipe_catalog

    registry = json.loads((Path(__file__).parents[1] / "content/catalog/enemy_species.json").read_bytes())
    source = entity("arena", "idEncounterManager", '''encounterComponent = { entityEvents = { num = 1; item[0] = {
        events = { num = 3;
            item[0] = { eventCall = { eventDef = "spawnSingleAI"; args = { num = 3;
                item[0] = { eEncounterSpawnType_t = "ENCOUNTER_SPAWN_IMP"; }
                item[1] = { entity = ""; } item[2] = { string = ""; } } } }
            item[1] = { eventCall = { eventDef = "spawnArchvile"; args = { num = 4;
                item[0] = { entity = "point"; } item[1] = { entity = ""; }
                item[2] = { string = ""; } item[3] = { string = ""; } } } }
            item[2] = { eventCall = { eventDef = "spawnSpirit"; args = { num = 4;
                item[0] = { entity = ""; } item[1] = { eEncounterSpawnType_t = "ENCOUNTER_SPAWN_ANY"; }
                item[2] = { string = ""; } item[3] = { bool = false; } } } }
        } } } }''')
    stage = native_recipe_catalog({"maps": {"map": extract_native_inventory(source, "map")}}, registry)["maps"]["map"]
    assert len([row for row in stage["origins"] if row.get("inactive_reason")]) == 2
    assert [row["family"] for row in stage["origins"] if not row.get("inactive_reason")] == ["spirit"]
