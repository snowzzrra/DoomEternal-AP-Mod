import copy
import json
from pathlib import Path

import pytest

from tools.maps.ap_map_generator import find_entity_block_bounds
from tools.maps.encounter_catalog import ENTITY_HEADER, entity_records, list_items
from tools.maps.encounter_materializer import materialize_frozen_encounters, text_sha256


EVENT = '''{ eventDef = "spawnSingleAI"; args = {
num = 3; item[0] = { eEncounterSpawnType_t = "ENCOUNTER_SPAWN_IMP"; }
item[1] = { entity = "point"; } item[2] = { string = "cohort_keep"; }
} }'''
WAIT = '{ eventDef = "waitKillCount"; args = { num = 1; item[0] = { int = 1; } } }'


def entity(name, kind, inherit, edit):
    return f'''entity {{
entityDef {name} {{ inherit = "{inherit}"; class = "{kind}";
edit = {{ {edit} }}
}}
}}
'''


def manager(name):
    return entity(name, "idEncounterManager", "encounter/manager", f'''
encounterComponent = {{ entityEvents = {{ num = 1; item[0] = {{
entity = "{name}"; events = {{ num = 2;
item[0] = {{ eventCall = {EVENT} }}
item[1] = {{ eventCall = {WAIT} }}
}} }} }} }}''')


def block(text, name):
    start, end = find_entity_block_bounds(text, name)
    return text[start:end]


def record(text, name):
    return next(entity_records(block(text, name), include_names={name}))


def ref(text, key, name):
    return {"map_key": key, "entity": name, "sha256": text_sha256(block(text, name).strip())}


@pytest.fixture
def frozen():
    source = entity("old_ai", "idAI2", "ai/fodder/imp", 'spawnPosition = { x = 1; y = 2; z = 3; }')
    source += entity("point", "idTarget_Spawn", "target/spawn", '''
entityDefs = { num = 1; item[0] = { name = "old_ai"; } }
targets = { num = 2; item[0] = "old_ai"; item[1] = "door"; }
spawnEditableShared = { deathTrigger = "door"; }
spawnPosition = { x = 20; y = 30; z = 40; }''')
    source += entity("door", "idLogicRelay", "logic/relay", "targets = { num = 0; }")
    source += manager("arena") + manager("alternate")
    template = entity("stone", "idAI2", "ai/fodder/imp_stone", '''
aiHealth = { components = { components[0] = { max = 400; starting = 300; } components[1] = { max = 10; } } }
aiEditable = { damageOutModifiers = { num = 0; } }
renderModelInfo = { scale = { x = 1; y = 1; z = 1; } }
clipModelInfo = { size = { x = 0.5; y = 0.5; z = 1.6; } }
spawnPosition = { x = 1000; y = 1000; z = 1000; }
selfReference = "stone";
''')
    profiles = {
        "imp": {"spawn_type": "ENCOUNTER_SPAWN_IMP", "inherits": ["ai/fodder/imp"]},
        "stone_imp": {"spawn_type": "ENCOUNTER_SPAWN_STONE_IMP", "inherits": ["ai/fodder/imp_stone"]},
    }
    manager_ref = ref(source, "map", "arena")
    manager_ref.pop("map_key")
    operation = {
        "manager": manager_ref, "script_index": 0, "event_index": 0,
        "event_def": "spawnSingleAI", "event_sha256": text_sha256(EVENT),
        "from": {"species": "imp", "spawn_type": "ENCOUNTER_SPAWN_IMP", "spawner": ref(source, "map", "point")},
        "waves": [{"instances": [{
            "species": "stone_imp", "spawn_type": "ENCOUNTER_SPAWN_STONE_IMP",
            "ai": ref(template, "dlc", "stone"),
            "ai_name": "ap_v070_ai_stone__ap_v070_s00100", "spawner_name": "private_point__ap_v070_s00100",
            "attributes": {"health_percent": 150, "damage_percent": 125, "speed_percent": 100, "size_percent": 50},
        }]}],
    }
    plan = {"schema": 1, "map_key": "map", "source_sha256": text_sha256(source), "operations": [operation]}
    return source, plan, {"map": source, "dlc": template}, profiles


def apply(frozen):
    source, plan, maps, profiles = frozen
    return materialize_frozen_encounters(source, plan, maps, species_profiles=profiles)


def test_direct_materializer_rejects_speed_without_positive_species_qualification(frozen):
    instance = frozen[1]["operations"][0]["waves"][0]["instances"][0]
    instance["attributes"]["speed_percent"] = 135
    instance["ai_name"] = "ap_v070_ai_stone__ap_v070_s00135"
    instance["spawner_name"] = "private_point__ap_v070_s00135"
    with pytest.raises(ValueError, match="Speed changes are not qualified"):
        apply(frozen)


def test_native_multi_definition_source_clones_only_the_selected_actor(frozen):
    source, plan, maps, profiles = frozen
    source = source.replace('"ai/fodder/imp"', '"ai/fodder/zombie_t1_hell"').replace(
        'ENCOUNTER_SPAWN_IMP', 'ENCOUNTER_SPAWN_ZOMBIE_TIER_1')
    source = source.replace('entityDefs = { num = 1; item[0] = { name = "old_ai"; } }',
        'entityDefs = { num = 2; item[0] = { name = "old_ai"; } item[1] = { name = "soldier"; } }')
    source += entity("soldier", "idAI2", "ai/fodder/soldier_blaster", "")
    for name in ("arena", "alternate"):
        original = block(source, name)
        source = source.replace(original, original.replace('edit = {',
            'edit = { aiTypeDefAssignments = "actorpopulation/default/dlc1";', 1))
    root = Path(__file__).resolve().parents[1]
    registry = json.loads((root / "content/catalog/enemy_species.json").read_bytes())
    profiles["zombie_tier_1"] = {"spawn_type": "ENCOUNTER_SPAWN_ZOMBIE_TIER_1",
        "inherits": ["ai/fodder/zombie_t1_hell"]}
    plan["source_sha256"] = text_sha256(source)
    operation = plan["operations"][0]
    operation["manager"]["sha256"] = record(source, "arena")["sha256"]
    operation["event_sha256"] = text_sha256(EVENT.replace('ENCOUNTER_SPAWN_IMP', 'ENCOUNTER_SPAWN_ZOMBIE_TIER_1'))
    operation["from"] = {"species": "zombie_tier_1", "spawn_type": "ENCOUNTER_SPAWN_ZOMBIE_TIER_1",
        "spawner": ref(source, "map", "point")}
    result = materialize_frozen_encounters(source, plan, {**maps, "map": source}, species_profiles=profiles,
        source_registry=registry, source_root=root)
    instance = operation["waves"][0]["instances"][0]
    private = record(result, instance["spawner_name"])["edit"]
    assert private["entityDefs"] == private["initialEntityDefs"] == {"num": 1, "item[0]": {"name": instance["ai_name"]}}


def test_empty_point_materializes_from_native_startup_palette(frozen):
    source, plan, maps, profiles = frozen
    source = source.replace('entityDefs = { num = 1; item[0] = { name = "old_ai"; } }', 'entityDefs = { num = 0; }')
    source += entity('parent', 'idTarget_Spawn_Parent', 'encounter/spawn_group/parent',
        'entityDefs = { num = 2; item[0] = { name = "old_ai"; } item[1] = { name = "soldier"; } }')
    source += entity('group', 'idTargetSpawnGroup', 'encounter/spawn_group/zone',
        'targetSpawnParent = "parent"; spawners = { num = 1; item[0] = "point"; }')
    source += entity('soldier', 'idAI2', 'ai/fodder/soldier_blaster', '')
    original = block(source, 'arena')
    source = source.replace(original, original.replace('edit = {',
        'edit = { aiTypeDefAssignments = "actorpopulation/default/campaign";', 1))
    plan['source_sha256'] = text_sha256(source)
    operation = plan['operations'][0]
    operation['manager'] = {key: value for key, value in ref(source, 'map', 'arena').items() if key != 'map_key'}
    operation['from']['spawner'] = ref(source, 'map', 'point')
    maps['map'] = source
    result = apply((source, plan, maps, profiles))
    instance = operation['waves'][0]['instances'][0]
    private = record(result, instance['spawner_name'])['edit']
    assert private['entityDefs'] == private['initialEntityDefs'] == {'num': 1, 'item[0]': {'name': instance['ai_name']}}
    for name in ('point', 'group', 'parent'):
        assert block(result, name) == block(source, name)
    for name in ("point", "old_ai", "soldier"):
        assert block(result, name) == block(source, name)


def native_group_state(text):
    names = {match.group(1) for match in ENTITY_HEADER.finditer(text)}
    records = {entry["name"]: entry for entry in entity_records(text, include_names=names)}
    points = {name for name, entry in records.items() if entry["class"] == "idTarget_Spawn"}
    groups = {name: entry["edit"] for name, entry in records.items() if entry["class"] == "idTargetSpawnGroup"}
    # the fixture supplies an overlap containing every point
    members = {name: set(list_items(group.get("spawners"))) | (points if group.get("includeTouchingSpawners", True) else set())
               for name, group in groups.items()}
    for name, group in groups.items():
        if group.get("exclusive", False):
            for other, candidate in groups.items():
                if other != name and not candidate.get("exclusive", False):
                    members[other] -= members[name]
    for name, group in groups.items():
        for subgroup in list_items(group.get("targetSpawnSubGroups")):
            members[name] |= members[subgroup]
    definitions = {name: [value["name"] for value in list_items(records[name]["edit"].get("entityDefs"))] for name in points}
    for name, group in groups.items():
        parent = group.get("targetSpawnParent")
        if not parent:
            continue
        for point in members[name] & points:
            if not list_items(records[point]["edit"].get("initialEntityDefs")):
                for value in list_items(records[parent]["edit"].get("entityDefs")):
                    if value["name"] not in definitions[point]:
                        definitions[point].append(value["name"])
    return groups, members, definitions


def test_explicit_profile_mask_is_preserved_without_normalization(frozen):
    _, plan, _, profiles = frozen
    mask = "ENCOUNTER_SPAWN_STONE_IMP ENCOUNTER_DO_NOT_USE_MAX_COMMON"
    instance = plan["operations"][0]["waves"][0]["instances"][0]
    instance["spawn_type"] = mask
    with pytest.raises(ValueError, match="profile/template"):
        apply(frozen)
    profiles["stone_imp"]["spawn_types"] = (profiles["stone_imp"]["spawn_type"], mask)
    manager = record(apply(frozen), "arena")["edit"]
    call = manager["encounterComponent"]["entityEvents"]["item[0]"]["events"]["item[0]"]["eventCall"]
    assert call["args"]["item[0]"]["eEncounterSpawnType_t"] == mask


def test_frozen_instance_replaces_only_selected_event_and_preserves_links(frozen):
    saved = copy.deepcopy(frozen)
    source, plan, _, _ = frozen
    result = apply(frozen)
    instance = plan["operations"][0]["waves"][0]["instances"][0]
    changed = EVENT.replace("ENCOUNTER_SPAWN_IMP", "ENCOUNTER_SPAWN_STONE_IMP").replace('entity = "point"', f'entity = "{instance["spawner_name"]}"')
    assert result.startswith(source.replace(EVENT, changed, 1))
    assert block(result, "alternate") == block(source, "alternate")
    assert block(result, "point") == block(source, "point")
    assert block(result, "old_ai") == block(source, "old_ai")
    actor = record(result, instance["ai_name"])["edit"]
    point = record(result, instance["spawner_name"])["edit"]
    assert actor["spawnPosition"] == {"x": 20, "y": 30, "z": 40}
    assert actor["selfReference"] == instance["ai_name"]
    assert actor["aiHealth"]["components"] == {"components[0]": {"max": 600, "starting": 450}, "components[1]": {"max": 10}}
    assert actor["renderModelInfo"]["scale"] == {"x": 0.5, "y": 0.5, "z": 0.5}
    assert actor["clipModelInfo"]["size"] == {"x": 0.5, "y": 0.5, "z": 1.6}
    assert actor["aiEditable"]["damageOutModifiers"]["item[0]"] == {"damageSource": "DAMAGESRC_ALL", "damageModifier": 1.25}
    assert point["entityDefs"]["item[0]"]["name"] == instance["ai_name"]
    assert point["initialEntityDefs"] == point["entityDefs"]
    assert point["targets"] == {"num": 2, "item[0]": instance["ai_name"], "item[1]": "door"}
    assert point["spawnEditableShared"]["deathTrigger"] == "door"
    assert apply(frozen) == result
    assert frozen == saved


@pytest.mark.parametrize("target", ["map", "manager", "event", "spawner", "template"])
def test_hash_mismatch_rejects_plan_before_destination_write(frozen, tmp_path, target):
    source, plan, maps, profiles = frozen
    operation = plan["operations"][0]
    if target == "map":
        plan["source_sha256"] = "0" * 64
    elif target == "manager":
        operation["manager"]["sha256"] = "0" * 64
    elif target == "event":
        operation["event_sha256"] = "0" * 64
    elif target == "spawner":
        operation["from"]["spawner"]["sha256"] = "0" * 64
    else:
        operation["waves"][0]["instances"][0]["ai"]["sha256"] = "0" * 64
    destination = tmp_path / "output.map"
    destination.write_text("existing output", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        destination.write_text(materialize_frozen_encounters(source, plan, maps, species_profiles=profiles), encoding="utf-8")
    assert destination.read_text(encoding="utf-8") == "existing output"


@pytest.mark.parametrize("fault", ["duplicate_event", "duplicate_name", "wave", "instance", "speed", "species", "origin", "baseline"])
def test_wrong_frozen_plan_is_refused(frozen, fault):
    operation = frozen[1]["operations"][0]
    instance = operation["waves"][0]["instances"][0]
    if fault == "duplicate_event":
        frozen[1]["operations"].append(copy.deepcopy(operation))
    elif fault == "duplicate_name":
        instance["spawner_name"] = instance["ai_name"]
    elif fault == "wave":
        operation["waves"].append(copy.deepcopy(operation["waves"][0]))
    elif fault == "instance":
        operation["waves"][0]["instances"].append(copy.deepcopy(instance))
    elif fault == "speed":
        instance["ai_name"] = "ap_v070_ai_stone__ap_v070_s00135"
    elif fault == "species":
        instance["spawn_type"] = "ENCOUNTER_SPAWN_IMP"
    elif fault == "origin":
        operation["from"]["spawn_type"] = "ENCOUNTER_SPAWN_STONE_IMP"
    else:
        instance["base_properties"] = {"aiHealth": {"max": 999999}}
    with pytest.raises(ValueError):
        apply(frozen)


def test_foreign_template_link_needs_qualified_import(frozen):
    source, plan, maps, profiles = frozen
    maps["dlc"] = maps["dlc"].replace('selfReference = "stone";', 'childReference = "child";')
    maps["dlc"] += entity("child", "idStatic", "test/child", "")
    reference = plan["operations"][0]["waves"][0]["instances"][0]["ai"]
    reference["sha256"] = text_sha256(block(maps["dlc"], "stone").strip())
    with pytest.raises(ValueError, match="unresolved entity reference"):
        materialize_frozen_encounters(source, plan, maps, species_profiles=profiles)


GROUP_EVENT = '''{ eventDef = "spawnAI"; args = {
num = 4; item[0] = { eEncounterSpawnType_t = "ENCOUNTER_SPAWN_IMP"; }
item[1] = { int = 4; } item[2] = { entity = "group"; }
item[3] = { string = "cohort_keep"; }
} }'''
BARRIER = '''{ eventDef = "waitAIRemaining"; args = {
num = 4; item[0] = { eEncounterSpawnType_t = "ENCOUNTER_SPAWN_ANY"; }
item[1] = { int = 0; } item[2] = { string = ""; } item[3] = { bool = false; }
} }'''


@pytest.fixture
def grouped(frozen):
    source, plan, maps, profiles = frozen
    source = source.replace(EVENT, GROUP_EVENT, 1).replace(WAIT, BARRIER, 1)
    source += block(source, "point").replace("entityDef point ", "entityDef point2 ").replace("x = 20", "x = 55")
    source += entity("group", "idTargetSpawnGroup", "encounter/spawn_group/zone", '''
spawners = { num = 2; item[0] = "point"; item[1] = "point2"; }
spawnPosition = { x = 11; y = 12; z = 13; }
flags = { noFlood = true; }''')
    plan["schema"] = 2
    plan["source_sha256"] = text_sha256(source)
    operation = plan["operations"][0]
    operation["manager"] = {key: value for key, value in ref(source, "map", "arena").items() if key != "map_key"}
    operation["event_def"] = "spawnAI"
    operation["event_sha256"] = text_sha256(GROUP_EVENT)
    operation["from"]["spawner"] = ref(source, "map", "group")
    original = operation["waves"][0]["instances"][0]
    instances = []
    for index in range(4):
        instance = copy.deepcopy(original)
        instance["ai_name"] = f"ap_v070_ai_stone{index}__ap_v070_s00100"
        instance["spawner_name"] = f"private_point{index}__ap_v070_s00100"
        instance["origin_spawner"] = ref(source, "map", "point" if index % 2 == 0 else "point2")
        instances.append(instance)
    operation["waves"][0]["instances"] = instances
    maps["map"] = source
    return source, plan, maps, profiles


def calls(text):
    return [item["eventCall"] for item in list_items(record(text, "arena")["edit"]["encounterComponent"]["entityEvents"]["item[0]"]["events"])]


def stage(grouped):
    operation = grouped[1]["operations"][0]
    operation["barrier"] = {"event_index": 1, "event_sha256": text_sha256(BARRIER)}
    instances = operation["waves"][0]["instances"]
    for index, instance in enumerate(instances):
        instance["group_name"] = f"private_group{index}__ap_v070_s00100"
    operation["waves"] = [{"instances": instances[:2], "label": "private_wave0", "min_spawn_stagger": 0.25, "max_spawn_stagger": 0.25},
                          {"instances": instances[2:], "label": "private_wave1", "min_spawn_stagger": 1, "max_spawn_stagger": 1}]
    return grouped


def group_source(grouped, source):
    _, plan, maps, profiles = grouped
    plan["source_sha256"] = text_sha256(source)
    plan["operations"][0]["from"]["spawner"] = ref(source, "map", "group")
    for wave in plan["operations"][0]["waves"]:
        for instance in wave["instances"]:
            instance["origin_spawner"] = ref(source, "map", instance["origin_spawner"]["entity"])
    maps["map"] = source
    return source, plan, maps, profiles


def test_finite_group_materializes_each_frozen_instance_and_preserves_source(grouped):
    saved = copy.deepcopy(grouped)
    source, plan, maps, profiles = grouped
    result = apply(grouped)
    operation = plan["operations"][0]
    assert [call["eventDef"] for call in calls(result)] == ["spawnSingleAI"] * 4 + ["waitAIRemaining"]
    for index, instance in enumerate(operation["waves"][0]["instances"]):
        assert calls(result)[index]["args"]["item[1]"]["entity"] == instance["spawner_name"]
        assert calls(result)[index]["args"]["item[2]"]["string"] == "cohort_keep"
        assert record(result, instance["ai_name"])["edit"]["spawnPosition"]["x"] == (20 if index % 2 == 0 else 55)
    for name in ("alternate", "point", "point2", "group", "old_ai", "door"):
        assert block(result, name) == block(source, name)
    assert BARRIER in result
    assert apply(grouped) == result
    assert grouped == saved
    with pytest.raises(ValueError, match="source hash mismatch"):
        materialize_frozen_encounters(result, plan, maps, species_profiles=profiles)


def test_staged_waves_use_private_single_member_groups_and_both_native_barriers(grouped):
    stage(grouped)
    saved = copy.deepcopy(grouped)
    result = apply(grouped)
    events = calls(result)
    assert [call["eventDef"] for call in events] == ["staggeredAISpawn", "staggeredAISpawn", "waitStaggeredSpawnComplete", "waitAIRemaining"] * 2
    for wave_index, wave in enumerate(grouped[1]["operations"][0]["waves"]):
        for instance_index, instance in enumerate(wave["instances"]):
            args = events[wave_index * 4 + instance_index]["args"]
            assert list_items(args) == [{"eEncounterSpawnType_t": instance["spawn_type"]}, {"int": 1},
                {"entity": instance["group_name"]}, {"string": wave["label"]},
                {"float": wave["min_spawn_stagger"]}, {"float": wave["max_spawn_stagger"]}]
            group = record(result, instance["group_name"])["edit"]
            assert group["spawners"] == {"num": 1, "item[0]": instance["spawner_name"]}
            assert group["exclusive"] is True
            assert group["includeTouchingSpawners"] is False
            assert list_items(group["targetSpawnSubGroups"]) == []
            assert group["flags"] == {"noFlood": True}
        assert list_items(events[wave_index * 4 + 2]["args"]) == [{"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"},
            {"int": 0}, {"string": wave["label"]}, {"bool": False}]
        assert events[wave_index * 4 + 3] == calls(grouped[0])[1]
    assert apply(grouped) == result
    assert grouped == saved


def test_private_group_excludes_authored_touching_and_subgroup_sources(grouped):
    stage(grouped)
    source = grouped[0].replace('flags = { noFlood = true; }', '''
includeTouchingSpawners = true;
targetSpawnSubGroups = { num = 1; item[0] = "authored_subgroup"; }
flags = { noFlood = true; }''')
    source += entity("authored_subgroup", "idTargetSpawnGroup", "encounter/spawn_group/zone", '''
spawners = { num = 1; item[0] = "point2"; }''')
    grouped = group_source(grouped, source)
    result = apply(grouped)
    assert block(result, "group") == block(source, "group")
    assert block(result, "authored_subgroup") == block(source, "authored_subgroup")
    for wave in grouped[1]["operations"][0]["waves"]:
        for instance in wave["instances"]:
            group = record(result, instance["group_name"])["edit"]
            assert group["includeTouchingSpawners"] is False
            assert list_items(group["targetSpawnSubGroups"]) == []
            assert list_items(group["spawners"]) == [instance["spawner_name"]]
            point = record(result, instance["spawner_name"])["edit"]
            assert list_items(point["entityDefs"]) == [{"name": instance["ai_name"]}]
            assert point["initialEntityDefs"] == point["entityDefs"]


def inherited_group(grouped):
    source = grouped[0].replace('entityDefs = { num = 1; item[0] = { name = "old_ai"; } }', 'entityDefs = { num = 0; }')
    source = source.replace('flags = { noFlood = true; }', 'targetSpawnParent = "parent"; flags = { noFlood = true; }')
    source += entity("parent", "idTarget_Spawn_Parent", "encounter/spawn_group/parent", '''
entityDefs = { num = 1; item[0] = { name = "old_ai"; } }''')
    return group_source(grouped, source)


def test_empty_group_points_use_pinned_parent_and_clone_explicit_ai(grouped):
    stage(grouped)
    grouped = inherited_group(grouped)
    result = apply(grouped)
    for wave in grouped[1]["operations"][0]["waves"]:
        for instance in wave["instances"]:
            point = record(result, instance["spawner_name"])["edit"]
            assert list_items(point["entityDefs"]) == [{"name": instance["ai_name"]}]
            assert point["initialEntityDefs"] == point["entityDefs"]
    for name in ("point", "point2", "parent"):
        assert block(result, name) == block(grouped[0], name)


@pytest.mark.parametrize("mode", ["single", "finite_group", "staged_group"])
def test_global_collection_cannot_share_private_or_original_points(request, mode):
    frozen = request.getfixturevalue("frozen" if mode == "single" else "grouped")
    if mode == "staged_group":
        stage(frozen)
    source, plan, maps, profiles = frozen
    source += entity("overlap", "idTargetSpawnGroup", "encounter/spawn_group/zone", '''
spawners = { num = 1; item[0] = "point"; } includeTouchingSpawners = true;''')
    maps["map"] = source
    plan["source_sha256"] = text_sha256(source)
    result = apply((source, plan, maps, profiles))
    groups, members, definitions = native_group_state(result)
    instances = [instance for wave in plan["operations"][0]["waves"] for instance in wave["instances"]]
    private_points = {instance["spawner_name"] for instance in instances}
    private_groups = set()
    for instance in instances:
        owner = instance["group_name"] if mode == "staged_group" else "owner_" + instance["spawner_name"]
        private_groups.add(owner)
        assert groups[owner]["exclusive"] is True
        assert members[owner] == {instance["spawner_name"]}
        assert definitions[instance["spawner_name"]] == [instance["ai_name"]]
    for name in groups.keys() - private_groups:
        assert members[name].isdisjoint(private_points)
        assert block(result, name) == block(source, name)
    for point in ("point", *([] if mode == "single" else ["point2"])):
        assert definitions[point] == ["old_ai"]
        assert block(result, point) == block(source, point)
    if mode == "single":
        event = record(result, "arena")["edit"]["encounterComponent"]["entityEvents"]["item[0]"]["events"]["item[0]"]["eventCall"]
        assert event["eventDef"] == "spawnSingleAI"
        assert groups[owner].get("targetSpawnParent") is None


def test_parent_initialization_preserves_private_same_species_attributes(grouped):
    stage(grouped)
    grouped = inherited_group(grouped)
    source, plan, maps, profiles = grouped
    maps["dlc"] = maps["dlc"].replace('inherit = "ai/fodder/imp_stone"', 'inherit = "ai/fodder/imp"')
    for wave in plan["operations"][0]["waves"]:
        for instance in wave["instances"]:
            instance["species"] = "imp"
            instance["spawn_type"] = "ENCOUNTER_SPAWN_IMP"
            instance["ai"] = ref(maps["dlc"], "dlc", "stone")
    result = apply(grouped)
    _, _, definitions = native_group_state(result)
    for wave in plan["operations"][0]["waves"]:
        for instance in wave["instances"]:
            assert definitions[instance["spawner_name"]] == [instance["ai_name"]]
            assert record(result, instance["ai_name"])["edit"]["aiHealth"]["components"]["components[0]"]["max"] == 600


@pytest.mark.parametrize("field", ['exclusive = true;', 'inherit_override'])
def test_unqualified_source_group_cannot_share_generated_points(frozen, field):
    source, plan, maps, profiles = frozen
    inherit = "unqualified/spawn_group" if field == "inherit_override" else "encounter/spawn_group/zone"
    source += entity("other", "idTargetSpawnGroup", inherit, "" if field == "inherit_override" else field)
    maps["map"] = source
    plan["source_sha256"] = text_sha256(source)
    with pytest.raises(ValueError, match="Source exclusive spawn group|Unqualified source spawn group defaults"):
        apply((source, plan, maps, profiles))


@pytest.mark.parametrize("fault", ["absent", "class", "species"])
def test_unqualified_group_parent_cannot_supply_empty_points(grouped, fault):
    grouped = inherited_group(grouped)
    source = grouped[0]
    if fault == "absent":
        source = source.replace('targetSpawnParent = "parent";', 'targetSpawnParent = "";')
    elif fault == "class":
        source = source.replace('class = "idTarget_Spawn_Parent"', 'class = "idLogicRelay"')
    else:
        source = source.replace('inherit = "ai/fodder/imp"', 'inherit = "ai/fodder/imp_stone"')
    with pytest.raises(ValueError, match="authored group parent|idTarget_Spawn_Parent|exactly one authored AI|profile/template|needs one definition"):
        apply(group_source(grouped, source))


@pytest.mark.parametrize("fault", ["group_hash", "member_hash", "member_outside_group", "barrier_hash", "barrier_index", "missing_timing", "nan_timing", "overflow_timing", "reversed_timing", "ignored_last_spawn_time", "label_collision", "group_name_collision"])
def test_unqualified_group_or_wave_is_refused_before_writing(grouped, tmp_path, fault):
    stage(grouped)
    source, plan, maps, profiles = grouped
    operation = plan["operations"][0]
    instance = operation["waves"][0]["instances"][0]
    if fault == "group_hash":
        operation["from"]["spawner"]["sha256"] = "0" * 64
    elif fault == "member_hash":
        instance["origin_spawner"]["sha256"] = "0" * 64
    elif fault == "member_outside_group":
        source += entity("outside", "idTarget_Spawn", "target/spawn", 'entityDefs = { num = 1; item[0] = { name = "old_ai"; } }')
        maps["map"] = source
        plan["source_sha256"] = text_sha256(source)
        instance["origin_spawner"] = ref(source, "map", "outside")
    elif fault == "barrier_hash":
        operation["barrier"]["event_sha256"] = "0" * 64
    elif fault == "barrier_index":
        operation["barrier"]["event_index"] = 0
    elif fault == "missing_timing":
        del operation["waves"][0]["min_spawn_stagger"]
    elif fault == "nan_timing":
        operation["waves"][0]["min_spawn_stagger"] = float("nan")
    elif fault == "overflow_timing":
        operation["waves"][0]["min_spawn_stagger"] = operation["waves"][0]["max_spawn_stagger"] = 1e300
    elif fault == "reversed_timing":
        operation["waves"][0]["min_spawn_stagger"] = 5
    elif fault == "ignored_last_spawn_time":
        operation["waves"][0]["max_spawn_stagger"] = 2
    elif fault == "label_collision":
        operation["waves"][1]["label"] = operation["waves"][0]["label"]
    else:
        instance["group_name"] = instance["ai_name"]
    destination = tmp_path / "output.map"
    destination.write_text("existing output", encoding="utf-8")
    with pytest.raises(ValueError):
        destination.write_text(materialize_frozen_encounters(source, plan, maps, species_profiles=profiles), encoding="utf-8")
    assert destination.read_text(encoding="utf-8") == "existing output"


@pytest.fixture
def reserved(grouped):
    stage(grouped)
    source, plan, maps, profiles = grouped
    old_manager = block(source, "arena")
    current = old_manager.replace('entity {', 'entity { layers { "game/map/base" }', 1)
    current = current.replace('edit = {', '''edit = {
spawnPosition = { x = 8; y = 9; z = 10; }
actorPopulation = "native_population";
aiTypeDefAssignments = { num = 1; item[0] = { entityDefs = { num = 1; item[0] = { name = "old_ai"; } } } }
commitTriggers = { num = 1; item[0] = "source_commit"; }
exitTriggers = { num = 1; item[0] = "door"; }''', 1)
    current = current.replace('ENCOUNTER_SPAWN_IMP', 'ENCOUNTER_SPAWN_ZOMBIE_TIER_1')
    current = current.replace('events = { num = 2;', 'events = { num = 3;', 1)
    current = current.replace('item[1] = { eventCall = ' + BARRIER + ' }', 'item[1] = { eventCall = ' + BARRIER + ''' }
item[2] = { eventCall = { eventDef = "activateTarget"; args = { num = 2;
item[0] = { entity = "final_relay"; } item[1] = { string = "authored"; } } } }''')
    source = source.replace(old_manager, current)
    source = source.replace('entityDef old_ai { inherit = "ai/fodder/imp"', 'entityDef old_ai { inherit = "ai/fodder/zombie_tier_1"')
    source += entity("final_relay", "idTarget_Count", "target/relay", 'targets = { num = 1; item[0] = "door"; }')
    maps["dlc"] += entity("source_commit", "idEncounterTrigger_Commit", "encounter/trigger/commit", '''
triggerFirst = true; triggerOnce = false; clipModelInfo = { clipModelName = "foreign_volume"; }''').replace(
        'entity {', 'entity { layers { "game/foreign/master_level" }', 1)
    maps["dlc"] += entity("source_flag", "idEncounterTrigger_RaiseUserFlag", "encounter/trigger/user_flag", '''
triggerFirst = true; userFlag = "foreign_flag"; clipModelInfo = { clipModelName = "foreign_volume"; }''').replace(
        'entity {', 'entity { layers { "game/foreign/master_level" }', 1)
    plan["schema"] = 3
    operation = plan["operations"][0]
    operation["manager"] = {key: value for key, value in ref(source, "map", "arena").items() if key != "map_key"}
    operation["event_sha256"] = text_sha256(GROUP_EVENT.replace("ENCOUNTER_SPAWN_IMP", "ENCOUNTER_SPAWN_ZOMBIE_TIER_1"))
    operation["from"].update(species="zombie_tier_1", spawn_type="ENCOUNTER_SPAWN_ZOMBIE_TIER_1")
    operation["reserve"] = {"commit_template": ref(maps["dlc"], "dlc", "source_commit"),
        "finish_template": ref(maps["dlc"], "dlc", "source_flag"), "manager_name": "private_reserve",
        "commit_name": "private_commit", "finish_name": "private_finish", "label": "reserve_supply",
        "completion_flag": "reserve_done", "desired_count": 1, "min_spawn_delay": 1, "max_spawn_delay": 2}
    profiles["zombie_tier_1"] = {"spawn_type": "ENCOUNTER_SPAWN_ZOMBIE_TIER_1", "inherits": ["ai/fodder/zombie_tier_1"]}
    return group_source(grouped, source)


def test_reserve_is_private_neutral_and_cleans_up_after_main_completion(reserved):
    saved = copy.deepcopy(reserved)
    result = apply(reserved)
    source, plan, _, _ = reserved
    reserve = plan["operations"][0]["reserve"]
    main_calls = calls(result)
    assert main_calls[0]["eventDef"] == "activateTarget"
    assert list_items(main_calls[0]["args"])[0] == {"entity": reserve["commit_name"]}
    assert main_calls[-3] == calls(source)[1]
    assert list_items(main_calls[-2]["args"])[0] == {"entity": reserve["finish_name"]}
    assert main_calls[-1] == calls(source)[-1]
    private = record(result, reserve["manager_name"])
    assert private["edit"]["commitTriggers"] == {"num": 1, "item[0]": reserve["commit_name"]}
    assert private["edit"]["userFlagTriggers"] == {"num": 1, "item[0]": reserve["finish_name"]}
    assert private["edit"]["exitTriggers"] == private["edit"]["preCombatTriggers"] == {"num": 0}
    scripts = list_items(private["edit"]["encounterComponent"]["entityEvents"])
    assert len(scripts) == 1 and scripts[0]["entity"] == reserve["manager_name"]
    private_calls = [value["eventCall"] for value in list_items(scripts[0]["events"])]
    assert [value["eventDef"] for value in private_calls] == ["maintainAICount", "waitForEventFlag", "stopMaintainingAICount", "removeAI"]
    assert list_items(private_calls[0]["args"]) == [{"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ZOMBIE_TIER_1"},
        {"int": 1}, {"int": 0}, {"float": 1}, {"int": 0}, {"entity": "group"}, {"string": "reserve_supply"}, {"float": 2}]
    assert list_items(private_calls[1]["args"])[2:] == [{"bool": True}, {"bool": False}]
    assert list_items(private_calls[2]["args"]) == list_items(private_calls[3]["args"]) == [
        {"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"}, {"string": "reserve_supply"}]
    main_before, main_after = record(source, "arena")["edit"], record(result, "arena")["edit"]
    for field in ("actorPopulation", "aiTypeDefAssignments", "commitTriggers", "exitTriggers"):
        assert main_after[field] == main_before[field]
    assert private["edit"]["actorPopulation"] == main_before["actorPopulation"]
    assert private["edit"]["aiTypeDefAssignments"] == main_before["aiTypeDefAssignments"]
    for name in (reserve["manager_name"], reserve["commit_name"], reserve["finish_name"]):
        assert record(result, name)["layers"] == ["game/map/base"]
    for name in (reserve["commit_name"], reserve["finish_name"]):
        trigger = record(result, name)["edit"]
        assert trigger["clipModelInfo"]["clipModelName"] is None
        assert trigger["triggerFirst"] is False
    assert record(result, reserve["finish_name"])["edit"]["userFlag"] == reserve["completion_flag"]
    for name in ("point", "point2", "group", "old_ai", "alternate", "final_relay"):
        assert block(result, name) == block(source, name)
    assert reserved == saved


def test_schema3_can_stage_without_reserve_and_does_not_cap_the_native_count(reserved):
    reserve = reserved[1]["operations"][0].pop("reserve")
    result = apply(reserved)
    assert calls(result)[0]["eventDef"] == "staggeredAISpawn"
    assert "private_reserve" not in {match.group(1) for match in ENTITY_HEADER.finditer(result)}
    reserved[1]["operations"][0]["reserve"] = reserve
    reserve["desired_count"] = 128
    private = record(apply(reserved), reserve["manager_name"])
    maintain = private["edit"]["encounterComponent"]["entityEvents"]["item[0]"]["events"]["item[0]"]["eventCall"]
    assert list_items(maintain["args"])[1] == {"int": 128}


def test_schema3_mixes_reserved_and_authored_supply_without_sharing_private_triggers(reserved):
    source, plan, maps, profiles = reserved
    alternate = block(source, "arena").replace("entityDef arena ", "entityDef alternate ").replace('"arena"', '"alternate"')
    source = source.replace(block(source, "alternate"), alternate)
    second = copy.deepcopy(plan["operations"][0])
    del second["reserve"]
    second["manager"] = {key: value for key, value in ref(source, "map", "alternate").items() if key != "map_key"}
    for wave in second["waves"]:
        wave["label"] = "other_" + wave["label"]
        for instance in wave["instances"]:
            for key in ("ai_name", "spawner_name", "group_name"):
                instance[key] = instance[key].replace("ap_v070_ai_", "ap_v070_ai_other_", 1) if key == "ai_name" else "other_" + instance[key]
    plan["operations"].append(second)
    source, plan, maps, profiles = group_source(reserved, source)
    mixed = source, plan, maps, profiles
    result = apply(mixed)
    assert calls(result)[0]["eventDef"] == "activateTarget"
    alternate_calls = list_items(record(result, "alternate")["edit"]["encounterComponent"]["entityEvents"]["item[0]"]["events"])
    assert alternate_calls[0]["eventCall"]["eventDef"] == "staggeredAISpawn"
    assert record(result, "private_reserve")["edit"]["commitTriggers"] == {"num": 1, "item[0]": "private_commit"}
    second["reserve"] = copy.deepcopy(plan["operations"][0]["reserve"])
    with pytest.raises(ValueError, match="identifier collision"):
        apply(mixed)


@pytest.mark.parametrize("fault", ["old_schema", "nonstaged", "null", "attributes", "non_zombie", "count", "count_bool", "count_overflow",
    "timing", "timing_nan", "timing_overflow", "source_name", "source_label", "wave_label", "generated_name", "same_name",
    "commit_hash", "finish_class", "commit_inherit", "multiple_scripts", "script_target", "after_relay", "wrong_relay"])
def test_unqualified_reserve_cannot_replace_destination(reserved, tmp_path, fault):
    source, plan, maps, _ = reserved
    operation = plan["operations"][0]
    reserve = operation["reserve"]
    if fault == "old_schema":
        plan["schema"] = 2
    elif fault == "nonstaged":
        del operation["barrier"]
        operation["waves"] = [{"instances": [
            {key: value for key, value in instance.items() if key != "group_name"}
            for instance in operation["waves"][0]["instances"]]}]
    elif fault == "null":
        operation["reserve"] = None
    elif fault == "attributes":
        reserve["attributes"] = {"health_percent": 200}
    elif fault == "non_zombie":
        source = source.replace('inherit = "ai/fodder/zombie_tier_1"', 'inherit = "ai/fodder/imp"')
        current = block(source, "arena").replace("ENCOUNTER_SPAWN_ZOMBIE_TIER_1", "ENCOUNTER_SPAWN_IMP")
        source = source.replace(block(source, "arena"), current)
        operation["from"].update(species="imp", spawn_type="ENCOUNTER_SPAWN_IMP")
        operation["event_sha256"] = text_sha256(GROUP_EVENT)
        operation["manager"] = {key: value for key, value in ref(source, "map", "arena").items() if key != "map_key"}
        source, plan, maps, _ = group_source(reserved, source)
        reserved = source, plan, maps, reserved[3]
    elif fault.startswith("count"):
        reserve["desired_count"] = {"count": 0, "count_bool": True, "count_overflow": 1 << 31}[fault]
    elif fault.startswith("timing"):
        reserve["min_spawn_delay"] = {"timing": 3, "timing_nan": float("nan"), "timing_overflow": 10 ** 500}[fault]
    elif fault in ("source_name", "source_label", "wave_label", "generated_name", "same_name"):
        reserve["label"] = {"source_name": "door", "source_label": "authored", "wave_label": "private_wave0",
            "generated_name": operation["waves"][1]["instances"][0]["ai_name"], "same_name": reserve["manager_name"]}[fault]
    elif fault == "commit_hash":
        reserve["commit_template"]["sha256"] = "0" * 64
    elif fault == "finish_class":
        reserve["finish_template"] = reserve["commit_template"].copy()
    elif fault == "commit_inherit":
        maps["dlc"] = maps["dlc"].replace('inherit = "encounter/trigger/commit"', 'inherit = "unqualified"')
        reserve["commit_template"] = ref(maps["dlc"], "dlc", "source_commit")
    else:
        current = block(source, "arena")
        if fault == "multiple_scripts":
            current = current.replace('entityEvents = { num = 1;', 'entityEvents = { num = 2; item[1] = { entity = "arena"; events = { num = 0; } }', 1)
        elif fault == "script_target":
            current = current.replace('entity = "arena"; events', 'entity = "alternate"; events')
        elif fault == "after_relay":
            current = current.replace('events = { num = 3;', '''events = { num = 4; item[3] = { eventCall = {
eventDef = "activateTarget"; args = { num = 2; item[0] = { entity = "final_relay"; }
item[1] = { string = "after"; } } } }''', 1)
        else:
            current = current.replace('entity = "final_relay"', 'entity = "door"')
        source = source.replace(block(source, "arena"), current)
        operation["manager"] = {key: value for key, value in ref(source, "map", "arena").items() if key != "map_key"}
        source, plan, maps, _ = group_source(reserved, source)
        reserved = source, plan, maps, reserved[3]
    destination = tmp_path / "output.map"
    destination.write_text("existing output")
    with pytest.raises(ValueError):
        destination.write_text(apply(reserved))
    assert destination.read_text() == "existing output"
