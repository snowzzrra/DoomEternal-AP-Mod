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


def frozen_plan(source=SOURCE, binding=None, family="maintenance", logical_count=2, maximum=6, multiplier=100, bindings=None,
                revision=13):
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
    return build_native_recipes(options, {"sequence": ["map"]}, seed=8, catalog=catalog, revision=revision)


































def test_immora_group_keeps_its_canonical_spawn_available_after_checkpoint():
    point = 'breach_wasteland_arena_target_spawn_60'
    group = 'breach_wasteland_arena_encounter_spawn_group_zone_2'
    owner = '_example_encounter_manager_0'
    source = SOURCE.replace('entityDef point {', 'entityDef ' + point + ' {').replace(
        'edit = { entityDefs', '''edit = {
            flags = { noFlood = true; hide = false; }
            spawnPosition = { x = -1733.45044; y = -317.500153; z = -163.999985; }
            spawnOrientation = { mat = { mat[0] = { x = -1; y = 2.68220845e-07; }
                mat[1] = { x = -2.68220845e-07; y = -1; } } }
            initialEntityDefs = { num = 0; }
            spawnEditable = { spawnAt = ""; aiStateOverride = "AIOVERRIDE_DEFAULT"; }
            portal = ""; targetSpawnParent = "";
            entityDefs''')
    source += '''entity { entityDef ''' + group + ''' {
        inherit = "encounter/spawn_group/zone"; class = "idTargetSpawnGroup";
        edit = { spawners = { num = 1; item[0] = "''' + point + '''"; } } } }
    entity { entityDef ''' + owner + ''' { inherit = "encounter/manager"; class = "idEncounterManager";
        edit = { encounterComponent = { entityEvents = { num = 1; item[0] = { events = { num = 24;
            item[23] = { eventCall = { eventDef = "spawnAI"; args = { num = 4;
                item[0] = { eEncounterSpawnType_t = "ENCOUNTER_SPAWN_CURSED_PROWLER"; }
                item[1] = { int = 1; } item[2] = { entity = "''' + group + '''"; }
                item[3] = { string = ""; } } } }
        } } } } } } }
    '''
    binding = {'mode': 'event', 'entity': owner, 'owner': owner,
               'path': 'encounterComponent.entityEvents.item[0].events.item[23].eventCall',
               'event_def': 'spawnAI', 'argument': 2, 'reference': group}
    plan = frozen_plan(source, binding, family='group', logical_count=1, maximum=1)
    frozen = deepcopy(plan)
    compiled = materialize_native_recipe_calls(source, 'map', plan, {'map': source})
    before = {row['name']: row for row in entity_records(source)}
    after = {row['name']: row for row in entity_records(compiled)}
    helper = after['ap_v070_source_' + _functional_token(point)]
    assert helper['class'] == before[point]['class']
    assert helper['inherit'] == before[point]['inherit']
    assert helper['edit'] == {**before[point]['edit'], 'flags': {'noFlood': False, 'hide': False}}
    assert after[point]['edit'] == before[point]['edit']
    assert after[group]['edit'] == before[group]['edit']
    assert plan == frozen


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
