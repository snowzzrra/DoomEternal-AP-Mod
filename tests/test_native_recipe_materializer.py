from copy import deepcopy
import hashlib
from textwrap import dedent

import pytest

from doom_eap.content.enemy_options import SPECIES_IDS, normalize_enemy_options
from doom_eap.content.native_enemy_plan import build_native_recipes, native_recipe_metadata
from tools.maps.encounter_catalog import entity_records, list_items
from tools.maps.native_recipe_materializer import materialize_native_recipe_definitions, materialize_native_recipe_calls


SOURCE = '''entity {
    entityDef original {
        inherit = "ai/fodder/imp";
        class = "idAI2";
        edit = { aiHealth = { components = { components[0] = { max = 200; starting = 200; } } } }
    }
}
entity {
    entityDef point {
        inherit = "target/spawn";
        class = "idTarget_Spawn";
        edit = { entityDefs = { num = 1; item[0] = { name = "original"; } } }
    }
}
'''


def frozen_plan(source=SOURCE, binding=None, family="maintenance", logical_count=2, maximum=6, multiplier=100, bindings=None):
    actor = next(entity_records(source, include_names={"original"}))
    catalog = {"schema_version": 1, "species": {"imp": {"spawn_type": "ENCOUNTER_SPAWN_IMP",
        "hitpoints": {"max": 200, "starting": 200}, "ai": {
        "map_key": "map", "entity": "original", "sha256": actor["sha256"]}}}, "maps": {"map": {
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "origins": [{"id": "map/request",
            "family": family, "spawn_type": "ENCOUNTER_SPAWN_IMP", "logical_types": ["ENCOUNTER_SPAWN_IMP"],
            "binding": binding or {"mode": "event", "reference": "point", "owner": "arena"},
            "logical_count": logical_count, "maximum_emissions": maximum, "combat_cost": "2"}]}}}
    if bindings is not None:
        original = catalog["maps"]["map"]["origins"][0]
        catalog["maps"]["map"]["origins"] = [{**original, "id": f"map/request/{index}", "binding": value}
                                              for index, value in enumerate(bindings)]
    options = normalize_enemy_options({"enemy_count_mode": "multiplier", "enemy_count_multiplier": multiplier,
        "enemy_weights": {name: int(name == "imp") for name in SPECIES_IDS},
        "enemy_size_variation_chance": 0,
        **{f"enemy_{name}": {"minimum": 100, "maximum": 100} for name in ("damage", "speed", "size")},
        "enemy_health": {"minimum": 200, "maximum": 200}})
    return build_native_recipes(options, {"sequence": ["map"]}, seed=8, catalog=catalog)


def test_entrance_uses_ground_from_its_encounter_without_moving_native_points():
    source = SOURCE.replace('edit = { entityDefs', '''edit = {
        spawnPosition = { x = 0; y = 0; z = -5; }
        spawnEditable = { aiStateOverride = "AIOVERRIDE_PLAY_ENTRANCE_ANIMATION"; spawnAnim = "jump_up"; }
        entityDefs''')
    for name,x in [('floor', 5), ('foreign', 1), ('unused', 0)]:
        source += f'''entity {{
    entityDef {name} {{ inherit = "target/spawn"; class = "idTarget_Spawn";
        edit = {{ spawnPosition = {{ x = {x}; y = 0; z = 0; }}
            entityDefs = {{ num = 1; item[0] = {{ name = "original"; }} }} }}
    }}
}}
'''
    bindings = [{'mode':'event','reference':point,'owner':owner} for point,owner in
                [('point','arena'),('floor','arena'),('foreign','another')]]
    plan = frozen_plan(source, family='immediate', logical_count=1, maximum=1, bindings=bindings)
    compiled = materialize_native_recipe_definitions(source, 'map', plan, {'map':source})
    helpers = [r for r in entity_records(compiled) if r['name'].startswith('ap_v070_landing_')]
    assert len(helpers) == 1 and helpers[0]['edit']['spawnEditableShared']['groupName'] == 'floor'
    assert next(entity_records(source, include_names={'point'})) == next(entity_records(compiled, include_names={'point'}))


def test_direct_activations_of_a_shared_point_have_distinct_native_clones():
    source = SOURCE + '''entity { entityDef trigger { inherit = "target/count"; class = "idTarget_Count";
        edit = { targets = { num = 3; item[0] = "point"; item[1] = "point"; item[2] = "wall_relay"; } } } }
        entity { entityDef removal { inherit = "target/remove"; class = "idTarget_Remove";
        edit = { targets = { num = 1; item[0] = "point"; } } } }
        entity { entityDef wall_relay { inherit = "target/count"; class = "idTarget_Count";
        edit = { targets = { num = 1; item[0] = "wall"; } } } }'''
    source = source.replace('        entity {', 'entity {').replace('entity { entityDef', 'entity {\n    entityDef')
    bindings = [{"mode": "direct_point", "entity": "trigger", "owner": "trigger", "reference": "point",
                 "path": f"targets.item[{index}]"} for index in range(2)]
    plan = frozen_plan(source, family="direct_point", logical_count=1, maximum=1, multiplier=200, bindings=bindings)
    compiled = materialize_native_recipe_calls(source, "map", plan, {"map": source})
    names = {"trigger", "point", "original", "removal", "wall_relay"}
    names.update("ap_v070_direct_r" + recipe["origin"] for recipe in plan["maps"]["map"]["recipes"])
    records = {row["name"]: row for row in entity_records(compiled, include_names=names)}
    before = {row["name"]: row for row in entity_records(source, include_names=names)}
    aliases = list_items(records["trigger"]["edit"]["targets"])
    assert aliases[0] != aliases[1] and aliases[2] == "wall_relay"
    for alias, recipe in zip(aliases, plan["maps"]["map"]["recipes"]):
        assert alias == "ap_v070_direct_r" + recipe["origin"]
        assert records[alias]["edit"] == before["point"]["edit"]
        assert len(recipe["bodies"]) == 2
    for name in ("point", "removal", "wall_relay"):
        assert records[name]["edit"] == before[name]["edit"]
    assert materialize_native_recipe_calls(source, "map", plan, {"map": source}) == compiled


def test_independent_bodies_preserve_source_identity_and_prepare_hidden_extra_bodies():
    source = SOURCE.split('entity {\n    entityDef point')[0].replace('entity {', 'entity {\n    layers { "campaign" }', 1)
    source = source.replace('edit = { aiHealth', 'edit = { spawnPosition = { x = 12; y = 13; z = 14; } '
        'flags = { hide = true; } aiHealth')
    plan = frozen_plan(source, {"mode": "independent_actor", "entity": "original"}, "independent_actor", 1, 1, 200)
    compiled = materialize_native_recipe_calls(source, "map", plan, {"map": source})
    records = {row["name"]: row for row in entity_records(compiled)}
    recipe, = plan["maps"]["map"]["recipes"]
    bodies = list_items(records["ap_v070_independent_r" + recipe["origin"]]["edit"]["entityDefs"])
    assert len(bodies) == 2
    assert records["original"]["inherit"] == bodies[0]["name"]
    assert records["original"]["layers"] == ["campaign"]
    assert records["original"]["edit"]["flags"]["hide"] is True
    for index, entry in enumerate(bodies):
        variant = records[entry["name"]]
        assert variant["layers"] == ["spawn_target_layer"]
        assert variant["inherit"] == recipe["bodies"][index]["definition"]
        assert variant["edit"]["spawnPosition"] == {"x": 12, "y": 13, "z": 14}
        assert variant["edit"]["flags"]["hide"] is True
        assert "aiHealth" not in variant["edit"]
        assert records[variant["inherit"]]["edit"]["aiHealth"]["components"]["components[0]"]["max"] == 400
    extra = records[f"ap_v070_independent_body_r{recipe['origin']}_00000001"]
    assert extra["inherit"] == bodies[1]["name"] and extra["layers"] == ["campaign"]


def test_independent_empty_recipe_keeps_a_hidden_native_identity():
    from doom_eap.content.native_enemy_plan import native_recipe_fingerprint

    source = SOURCE.split('entity {\n    entityDef point')[0]
    plan = frozen_plan(source, {"mode": "independent_actor", "entity": "original"}, "independent_actor", 1, 1)
    recipe, = plan["maps"]["map"]["recipes"]
    recipe.update(bodies=[], physical_count=0, maximum_emissions=0, final_credit_units=0, quantity_ratio=[0, 1])
    plan["fingerprint"] = native_recipe_fingerprint(plan)
    records = {row["name"]: row for row in entity_records(materialize_native_recipe_calls(source, "map", plan, {"map": source}))}
    marker = records["original"]["inherit"]
    assert marker == "ap_v070_empty_r" + recipe["origin"]
    assert records[marker]["edit"]["flags"]["hide"] is True
    assert list_items(records["ap_v070_independent_r" + recipe["origin"]]["edit"]["entityDefs"]) == []


def test_native_library_loads_frozen_bodies_as_templates_and_preserves_original_point():
    plan = frozen_plan()
    generated = materialize_native_recipe_definitions(SOURCE, "map", plan, {"map": SOURCE})
    assert generated.startswith(SOURCE)
    records = {record["name"]: record for record in entity_records(generated)}
    recipe, = plan["maps"]["map"]["recipes"]
    bodies = recipe["bodies"]
    for body in bodies:
        record = records[body["definition"]]
        assert record["layers"] == ["spawn_target_layer"]
        assert record["edit"]["aiHealth"]["components"]["components[0]"]["max"] == 400
        assert body["hitpoints"]["max"] == record["edit"]["aiHealth"]["components"]["components[0]"]["max"]
    library = records["ap_v070_recipe_plan"]
    assert list_items(library["edit"]["entityDefs"]) == [{"name": body["definition"]} for body in bodies]
    assert records["point"]["edit"]["entityDefs"]["item[0]"] == {"name": "original"}
    body_library = records["ap_v070_bodies_r" + recipe["origin"]]
    assert list_items(body_library["edit"]["entityDefs"]) == list_items(library["edit"]["entityDefs"])
    assert library["edit"]["spawnEditableShared"]["groupName"].startswith("EAP4|" + plan["fingerprint"] + "|map\n")


def test_large_native_library_survives_engine_property_limit_without_changing_plan():
    plan = frozen_plan(bindings=[{"mode": "event", "reference": "point", "owner": f"arena_{i}"}
                                 for i in range(414)])
    before = deepcopy(plan)
    generated = materialize_native_recipe_definitions(SOURCE, "map", plan, {"map": SOURCE})
    records = {row["name"]: row for row in entity_records(generated)
               if row["name"].startswith("ap_v070_recipe_plan")}
    header = records["ap_v070_recipe_plan"]["edit"]["spawnEditableShared"]["groupName"]
    version, count, size, checksum = header.split("|")
    chunks = [records[f"ap_v070_recipe_plan_{i}"]["edit"]["spawnEditableShared"]["groupName"]
              for i in range(int(count, 16))]
    text = "".join(chunk.encode("ascii")[:2046].decode("ascii") for chunk in chunks)
    assert version == "EAPC1" and len(chunks) > 1 and all(0 < len(chunk.encode("ascii")) <= 1800 for chunk in chunks)
    assert len(text) == int(size, 16) and text == native_recipe_metadata(plan, "map")
    hash_value = 14695981039346656037
    for byte in text.encode("ascii"):
        hash_value = ((hash_value ^ byte) * 1099511628211) & ((1 << 64) - 1)
    assert checksum == f"{hash_value:016x}"
    assert plan == before


def test_native_library_refuses_changed_donor_or_body_tracking():
    plan = frozen_plan()
    changed = SOURCE.replace("max = 200", "max = 201")
    with pytest.raises(ValueError, match="donor differs"):
        materialize_native_recipe_definitions(SOURCE, "map", plan, {"map": changed})
    bad = deepcopy(plan)
    bad["maps"]["map"]["recipes"][0]["bodies"][0]["spawn_type"] = "ENCOUNTER_SPAWN_MARAUDER"
    with pytest.raises(ValueError, match="tracking differs"):
        materialize_native_recipe_definitions(SOURCE, "map", bad, {"map": SOURCE})


def test_physical_bodies_inherit_source_references_without_copying_source_health_or_model():
    from doom_eap.content.native_enemy_plan import native_recipe_fingerprint

    source = SOURCE.replace("edit = { aiHealth", '''edit = {
        factionName = "encounter_team";
        targets = { num = 1; item[0] = "door"; }
        aiEditable = { tempMemoryData = { num = 1; item[0] = { key = "encounter_role"; value = 1; } }
            death = { trigger = "door"; declTwitchPain = "imp_pain"; }
            spawnSettings = { initialTarget = "destination";
                initialState = "AIOVERRIDE_FORCE_MOVE_TO_TRAVERSAL"; entranceAnimPath = "imp_entrance"; } }
        aiHealth''')
    source += '''entity { entityDef door { inherit = "logic/relay"; class = "idLogicRelay"; edit = {} } }
        entity { entityDef destination { inherit = "info/traversal"; class = "idInfoTraversal"; edit = {} } }'''
    plan = frozen_plan(source)
    donor_source = SOURCE.replace('edit = { aiHealth', 'edit = { renderModelInfo = { model = "donor_model"; } '
        'afProperties = { articulatedFigure = "donor_physics"; } aiHealth')
    donor = next(entity_records(donor_source, include_names={"original"}))
    for body in plan["maps"]["map"]["recipes"][0]["bodies"]:
        body["ai"] = {"map_key": "donor", "entity": "original", "sha256": donor["sha256"]}
    plan["fingerprint"] = native_recipe_fingerprint(plan)
    compiled = materialize_native_recipe_definitions(source, "map", plan, {"map": source, "donor": donor_source})
    records = {row["name"]: row for row in entity_records(compiled)}
    bodies = {body["definition"] for body in plan["maps"]["map"]["recipes"][0]["bodies"]}
    variants = [row for row in records.values() if row["class"] == "idAI2" and row["name"].startswith("ap_v070_functional_")]
    assert len(variants) == len(bodies)
    for row in variants:
        assert row["layers"] == ["spawn_target_layer"]
        assert row["inherit"] in bodies
        assert row["edit"]["renderModelInfo"]["model"] == "donor_model"
        assert row["edit"]["afProperties"]["articulatedFigure"] == "donor_physics"
        assert row["edit"]["aiHealth"]["components"]["components[0]"]["max"] == 400
        assert {k: row["edit"][k] for k in ("factionName", "targets", "aiEditable")} == {
            "factionName": "encounter_team", "targets": {"num": 1, "item[0]": "door"},
            "aiEditable": {"tempMemoryData": {"num": 1, "item[0]": {"key": "encounter_role", "value": 1}},
                "death": {"trigger": "door"}, "spawnSettings": {"initialTarget": "destination",
                "initialState": "AIOVERRIDE_FORCE_TELEPORT_TO_TRAVERSAL"}}}
    libraries = [row for row in records.values() if row["class"] == "idTarget_Spawn" and
                 row["name"].startswith("ap_v070_functional_") and row["name"] != "ap_v070_functional_sources"]
    assert len(libraries) == 1
    assert {item["name"] for item in list_items(libraries[0]["edit"]["entityDefs"])} == {row["name"] for row in variants}
    assert "ap_v070_functional_sources" in records


@pytest.mark.parametrize("external_target", [False, True])
def test_native_calls_bind_the_original_request_without_rebuilding_its_flow_or_labels(external_target):
    source = SOURCE + dedent('''
    entity {
        entityDef group { inherit = "encounter/spawn_group/zone"; class = "idTargetSpawnGroup";
            edit = { spawners = { num = 1; item[0] = "point"; } } }
    }
    entity {
        entityDef arena { inherit = "encounter/manager"; class = "idEncounterManager";
            edit = { encounterComponent = { entityEvents = { num = 1; item[0] = { events = { num = 2;
                item[0] = { eventCall = { eventDef = "maintainAICount"; args = { num = 7;
                    item[0] = { eEncounterSpawnType_t = "ENCOUNTER_SPAWN_IMP"; }
                    item[1] = { int = 2; } item[2] = { int = 6; } item[3] = { float = 0; } item[4] = { float = 0; }
                    item[5] = { entity = "group"; } item[6] = { string = "public_label"; } } } }
                item[1] = { eventCall = { eventDef = "stopMaintainingAICount"; args = { num = 1;
                    item[0] = { string = "public_label"; } } } }
            } } } } }
        }
    }''')
    if external_target:
        from tools.maps.ap_map_generator import find_entity_block_bounds
        start, end = find_entity_block_bounds(source, "group")
        source = source[:start] + source[end:]
    path = "encounterComponent.entityEvents.item[0].events.item[0].eventCall"
    plan = frozen_plan(source, {"mode": "event", "entity": "arena", "owner": "arena", "path": path,
        "event_def": "maintainAICount", "argument": 5, "reference": "group"})
    generated = materialize_native_recipe_calls(source, "map", plan, {"map": source})
    before = {row["name"]: row for row in entity_records(source)}
    after = {row["name"]: row for row in entity_records(generated)}
    events = after["arena"]["edit"]["encounterComponent"]["entityEvents"]["item[0]"]["events"]
    original = before["arena"]["edit"]["encounterComponent"]["entityEvents"]["item[0]"]["events"]
    expected = deepcopy(original)
    alias = "ap_v070_request_r" + plan["maps"]["map"]["recipes"][0]["origin"]
    expected["item[0]"]["eventCall"]["args"]["item[5]"]["entity"] = alias
    assert events == expected
    assert after[alias]["class"] == "idTargetSpawnGroup"
    assert list_items(after[alias]["edit"]["spawners"]) == []
    assert after[alias]['edit']['clipModelInfo']['type'] == 'CLIPMODEL_NONE'
    assert after["point"]["edit"] == before["point"]["edit"]
    if not external_target:
        assert after["group"]["edit"] == before["group"]["edit"]
    assert "|group|arena|" in after["ap_v070_recipe_plan"]["edit"]["spawnEditableShared"]["groupName"]


def test_summon_templates_freeze_logical_choices_without_editing_duplicate_requests_or_callbacks():
    source = SOURCE + '''entity { entityDef template { inherit = "encounter/archvile_template";
        class = "idArchvileTemplate"; edit = { summonList = { aiSpawnTypeList = { num = 3;
            item[0] = "ENCOUNTER_SPAWN_ANY"; item[1] = "ENCOUNTER_SPAWN_IMP"; item[2] = "ENCOUNTER_SPAWN_ANY";
        } } targets = { num = 1; item[0] = "door"; } } } }'''
    plan = frozen_plan(source, {"mode": "summon", "entity": "template", "spawn_type": "ENCOUNTER_SPAWN_ANY"}, family="summon")
    compiled = materialize_native_recipe_calls(source, "map", plan, {"map": source})
    template = next(entity_records(compiled, include_names={"template"}))
    assert list_items(template["edit"]["summonList"]["aiSpawnTypeList"]) == ["ENCOUNTER_SPAWN_IMP"] * 3
    assert template["edit"]["targets"] == {"num": 1, "item[0]": "door"}
    with pytest.raises(ValueError, match="quantity differs"):
        from doom_eap.content.native_enemy_plan import native_recipe_fingerprint
        bad = deepcopy(plan)
        bad["maps"]["map"]["recipes"][0]["binding"]["spawn_type"] = "ENCOUNTER_SPAWN_IMP"
        bad["fingerprint"] = native_recipe_fingerprint(bad)
        materialize_native_recipe_calls(source, "map", bad, {"map": source})
