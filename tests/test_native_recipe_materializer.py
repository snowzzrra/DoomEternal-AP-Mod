from copy import deepcopy
import hashlib
from textwrap import dedent

import pytest

from doom_eap.content.enemy_options import SPECIES_IDS, normalize_enemy_options
from doom_eap.content.native_enemy_plan import build_native_recipes, native_recipe_metadata, native_recipe_fingerprint
from tools.maps.encounter_catalog import entity_records, list_items
from tools.maps.native_recipe_materializer import materialize_native_recipe_definitions, materialize_native_recipe_calls, _refill_limits, _functional_token


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


def test_checkpoint_body_library_reuses_identical_definitions():
    plan = frozen_plan()
    recipe = plan['maps']['map']['recipes'][0]
    plan['maps']['map']['compatibility_recipes'] = [deepcopy(recipe)]
    plan['maps']['map']['counter_recipes'] = [deepcopy(recipe)]
    plan['fingerprint'] = native_recipe_fingerprint(plan)
    compiled = materialize_native_recipe_definitions(SOURCE, 'map', plan, {'map': SOURCE})
    records = list(entity_records(compiled))
    assert sum(row['name'] == recipe['bodies'][0]['definition'] for row in records) == 1
    compatible, = [row for row in records if row['name'] == 'ap_v070_compat_r' + recipe['origin']]
    assert len(list_items(compatible['edit']['entityDefs'])) == recipe['physical_count']
    alternate, = [row for row in records if row['name'] == 'ap_v070_counter_r' + recipe['origin']]
    assert len(list_items(alternate['edit']['entityDefs'])) == recipe['physical_count']


@pytest.mark.parametrize('authored_initial', [False, True])
@pytest.mark.parametrize('spawner_field', ['spawners', 'spawnersTouchingPrimitives'])
def test_shared_empty_point_prepares_direct_recipe_for_inherited_group_definitions(authored_initial, spawner_field):
    source = SOURCE.replace('edit = { aiHealth', 'edit = { factionName = "source_faction"; aiHealth')
    source = source.replace('edit = { entityDefs = { num = 1; item[0] = { name = "original"; } } }',
        'edit = { entityDefs = { num = 0; } initialEntityDefs = { ' +
        ('num = 1; item[0] = { name = "original"; }' if authored_initial else 'num = 0;') + ' } }')
    source += '''entity {
    entityDef alternative { inherit = "ai/fodder/imp"; class = "idAI2";
        edit = { factionName = "parent_faction"; aiEditable = { death = { triggerOnDeath = "door_relay"; } } } }
}
entity {
    entityDef parent { inherit = "target/spawn_parent"; class = "idTarget_Spawn_Parent";
        edit = { entityDefs = { num = 2; item[0] = { name = "original"; } item[1] = { name = "alternative"; } } } }
}
entity {
    entityDef group { inherit = "encounter/spawn_group/zone"; class = "idTargetSpawnGroup";
        edit = { targetSpawnParent = "parent"; spawners = { num = 3; item[0] = "point"; item[2] = "point"; } } }
}
'''
    source = source.replace('spawners = { num = 3;', spawner_field + ' = { num = 3;')
    plan = frozen_plan(source, family='immediate', logical_count=1, maximum=1,
        bindings=[{'mode':'event', 'reference':point, 'owner':'arena'} for point in ('group', 'point')])
    frozen = deepcopy(plan)
    compiled = materialize_native_recipe_definitions(source, 'map', plan, {'map':source})
    records = {row['name']:row for row in entity_records(compiled)}
    direct, = [r for r in plan['maps']['map']['recipes'] if r['binding']['reference'] == 'point']
    variants = [r for r in records.values() if r['name'].startswith('ap_v070_functional_') and
                r['class'] == 'idAI2' and r['inherit'] == direct['bodies'][0]['definition']]
    assert {r['edit']['factionName'] for r in variants} == (
        {'source_faction'} if authored_initial else {'source_faction', 'parent_faction'})
    if not authored_initial:
        inherited, = [r for r in variants if r['edit']['factionName'] == 'parent_faction']
        assert inherited['edit']['aiEditable']['death']['triggerOnDeath'] == 'door_relay'
    assert records['point']['edit'] == next(entity_records(source, include_names={'point'}))['edit']
    assert plan == frozen


@pytest.mark.parametrize('state', ['AIOVERRIDE_PLAY_ENTRANCE_ANIMATION', 'AIOVERRIDE_SPAWN_ANIM_IDLE',
                                 'AIOVERRIDE_FORCE_TELEPORT_TO_TRAVERSAL'])
def test_entrance_uses_ground_from_its_encounter_without_moving_native_points(state):
    source = SOURCE.replace('edit = { entityDefs', '''edit = {
        spawnPosition = { x = 0; y = 0; z = -5; }
        spawnEditable = { aiStateOverride = "AIOVERRIDE_PLAY_ENTRANCE_ANIMATION"; spawnAnim = "jump_up"; }
        entityDefs''')
    source = source.replace('AIOVERRIDE_PLAY_ENTRANCE_ANIMATION', state)
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
    assert len(helpers) == 1 and helpers[0]['edit']['spawnEditableShared']['groupName'] == helpers[0]['name']
    assert helpers[0]['edit']['spawnPosition'] == {'x': 5, 'y': 0, 'z': 0}
    assert next(entity_records(source, include_names={'point'})) == next(entity_records(compiled, include_names={'point'}))


@pytest.mark.parametrize('animation,start,expected', [
    ('zombie_tier_1/spawn/monster_closet_rand', (10, 0, 0), (10, 0, 0)),
    ('zombie_tier_1/spawn/sneaky_spawn_03', (10, 0, 0), (10, 0, 0)),
    ('soldier_blaster/traversal/jump_forward_1000_down_1000', (10, 0, 10), (10, 10, 0)),
])
def test_entrances_keep_their_individual_authored_destinations(animation, start, expected):
    x,y,z=start
    source=SOURCE.replace('edit = { entityDefs', f'''edit = {{
        spawnPosition = {{ x = {x}; y = {y}; z = {z}; }}
        spawnOrientation = {{ mat = {{ mat[0] = {{ y = 1; }} mat[1] = {{ x = -1; }} }} }}
        spawnEditable = {{ aiStateOverride = "AIOVERRIDE_PLAY_ENTRANCE_ANIMATION";
            spawnAnim = "animweb/characters/monsters/{animation}"; }}
        entityDefs''')
    source += source[source.index('entity {\n    entityDef point'):].replace('entityDef point', 'entityDef second').replace(
        f'x = {x}; y = {y}; z = {z};', f'x = {x + 10}; y = {y}; z = {z};')
    source += '''entity { entityDef floor { inherit = "info/teleport_destination"; class = "idInfo_TeleportDestination";
        edit = { spawnPosition = { x = 3; } } } }'''
    bindings=[{'mode':'event','reference':name,'owner':'arena'} for name in ('point','second')]
    plan=frozen_plan(source,family='immediate',logical_count=1,maximum=1,bindings=bindings)
    before=deepcopy(plan)
    compiled=materialize_native_recipe_definitions(source,'map',plan,{'map':source})
    helpers=[r['edit']['spawnPosition'] for r in entity_records(compiled) if r['name'].startswith('ap_v070_landing_')]
    assert sorted(tuple(p.get(axis,0) for axis in ('x','y','z')) for p in helpers) == [expected,(expected[0]+10,*expected[1:])]
    assert plan == before


@pytest.mark.parametrize('marker_kind', ['navigation', 'spawn_parent', 'player_destination', 'pickup', 'distant_wave'])
def test_animation_only_encounter_uses_ground_marker_without_changing_frozen_recipe(marker_kind):
    source = SOURCE.replace('edit = { entityDefs', '''edit = {
        spawnPosition = { z = -10; }
        spawnEditable = { aiStateOverride = "AIOVERRIDE_PLAY_ENTRANCE_ANIMATION"; spawnAnim = "jump_up"; }
        entityDefs''')
    source += '''entity {
    entityDef flyer { inherit = "ai/heavy/painelemental"; class = "idAI2"; edit = {} }
}
entity {
    entityDef air_pool { inherit = "target/spawn"; class = "idTarget_Spawn";
        edit = { spawnPosition = { z = -2; }
            entityDefs = { num = 2; item[0] = { name = "original"; } item[1] = { name = "flyer"; } } }
    }
}
'''
    for name, x, snap in [('unsafe', 0, False), ('floor', 3, True), ('foreign', 60, True)]:
        source += f'''entity {{
    entityDef {name} {{ inherit = "info/traversal/imp"; class = "idInfoTraversal";
        edit = {{ snapToGround = {str(snap).lower()}; spawnPosition = {{ x = {x}; y = 0; z = 0; }} }}
    }}
}}
'''
    if marker_kind == 'spawn_parent':
        source = source.replace('entityDef floor { inherit = "info/traversal/imp"; class = "idInfoTraversal";',
            'entityDef floor { inherit = "target/spawn"; class = "idTarget_Spawn";')
        source = source.replace('snapToGround = true; spawnPosition = { x = 3;',
            'targetSpawnParent = "pool"; entityDefs = { num = 1; item[0] = { name = "original"; } } spawnPosition = { x = 3;')
    elif marker_kind == 'player_destination':
        source = source.replace('entityDef floor { inherit = "info/traversal/imp"; class = "idInfoTraversal";',
            'entityDef floor { inherit = "info/teleport_destination"; class = "idInfo_TeleportDestination";')
    elif marker_kind == 'pickup':
        source = source.replace('entityDef floor { inherit = "info/traversal/imp"; class = "idInfoTraversal";',
            'entityDef floor { inherit = "pickup/armor/medium"; class = "idProp2";')
        source = source.replace('snapToGround = true; spawnPosition = { x = 60;',
            'snapToGround = false; spawnPosition = { x = 60;')
    bindings=None
    if marker_kind == 'distant_wave':
        source += '''entity {
    entityDef distant { inherit = "target/spawn"; class = "idTarget_Spawn";
        edit = { spawnPosition = { x = 200; }
            entityDefs = { num = 1; item[0] = { name = "original"; } } }
    }
}
'''
        bindings=[{'mode':'event','reference':point,'owner':'arena'} for point in ('point','distant')]
    plan = frozen_plan(source, family='immediate', logical_count=1, maximum=1,bindings=bindings)
    before = deepcopy(plan)
    compiled = materialize_native_recipe_definitions(source, 'map', plan, {'map': source})
    helper, = [r for r in entity_records(compiled) if r['name'].startswith('ap_v070_landing_')]
    assert helper['edit']['spawnPosition'] == {'x': 3, 'y': 0, 'z': 0}
    assert helper['edit']['spawnEditableShared']['groupName'] == helper['name']
    assert next(entity_records(source, include_names={'point'})) == next(entity_records(compiled, include_names={'point'}))
    assert plan == before


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
    source = SOURCE.replace('edit = { aiHealth', 'edit = { flags = { hide = true; } aiHealth')
    plan = frozen_plan(source)
    generated = materialize_native_recipe_definitions(source, "map", plan, {"map": source})
    assert generated.startswith(source)
    records = {record["name"]: record for record in entity_records(generated)}
    recipe, = plan["maps"]["map"]["recipes"]
    bodies = recipe["bodies"]
    for body in bodies:
        record = records[body["definition"]]
        assert record['edit']['flags']['hide'] is False
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
    source = source.replace('edit = { entityDefs', 'edit = { flags = { noFlood = true; } entityDefs')
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


def test_single_request_supplies_a_complete_point_when_the_passive_source_is_absent():
    source = SOURCE.replace('edit = { entityDefs', 'edit = { spawnPosition = { x = 12; y = 5; z = 2; } entityDefs')
    source += '''entity { entityDef arena { inherit = "encounter/manager"; class = "idEncounterManager";
        edit = { request = { eventDef = "spawnSingleAI"; args = { num = 1; item[0] = { entity = "point"; } } } } } }'''
    plan = frozen_plan(source, {'mode': 'event', 'entity': 'arena', 'owner': 'arena', 'path': 'request',
                               'event_def': 'spawnSingleAI', 'argument': 0, 'reference': 'point'},
                       family='immediate', logical_count=1, maximum=1)
    compiled = materialize_native_recipe_calls(source, 'map', plan, {'map': source})
    records = {r['name']: r for r in entity_records(compiled)}
    assert records['ap_v070_source_' + _functional_token('point')]['edit'] == {
        **records['point']['edit'], 'flags': {'noFlood': False}}


@pytest.mark.parametrize('kill_type,expected', [('ENCOUNTER_SPAWN_IMP', {}), ('ENCOUNTER_SPAWN_ANY', {}),
                                               ('ENCOUNTER_SPAWN_REVENANT', {'refill': 8})])
def test_auxiliary_refills_are_bounded_without_cutting_off_kill_objectives(kill_type, expected):
    source = '''entity { entityDef arena { inherit = "encounter/manager"; class = "idEncounterManager";
        edit = { events = { num = 4;
            item[0] = { eventCall = { eventDef = "maintainAICount"; args = { num = 7;
                item[0] = {} item[1] = {} item[2] = {} item[3] = {} item[4] = {} item[5] = {}
                item[6] = { string = "ammo"; } } } }
            item[1] = { eventCall = { eventDef = "waitKillCount"; args = { num = 1;
                item[0] = { eEncounterSpawnType_t = "''' + kill_type + '''"; } } } }
            item[2] = { eventCall = { eventDef = "waitAIRemaining"; args = { num = 1;
                item[0] = { eEncounterSpawnType_t = "ENCOUNTER_SPAWN_REVENANT"; } } } }
            item[3] = { eventCall = { eventDef = "stopMaintainingAICount"; args = { num = 2;
                item[0] = { eEncounterSpawnType_t = "ENCOUNTER_SPAWN_IMP"; } item[1] = { string = "ammo"; } } } }
        } } } }'''
    recipes = [{'origin': 'refill', 'family': 'maintenance', 'maximum_emissions': 0, 'physical_count': 2,
                'selected_spawn_type': 'ENCOUNTER_SPAWN_IMP', 'binding': {'owner': 'arena', 'path': 'events.item[0].eventCall'}},
               {'family': 'immediate', 'maximum_emissions': 6, 'binding': {'owner': 'arena'}}]
    assert _refill_limits(source, recipes) == expected
    assert _refill_limits(source.replace('stopMaintainingAICount', 'bossPhaseFinished'), recipes) == {}
