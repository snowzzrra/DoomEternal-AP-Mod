from copy import deepcopy
import hashlib
from importlib.resources import files
import json
from textwrap import dedent

import pytest


def test_sparse_identity_rotation_keeps_the_portal_landing_in_front():
    from tools.maps.native_recipe_materializer import _entrance_endpoint

    landing = _entrance_endpoint({
        'spawnPosition': {'x': -149.680283, 'y': 489.999908, 'z': 4.20000219},
        'spawnOrientation': {'mat': {'mat[0]': {'y': 1.77324068e-06}, 'mat[1]': {'x': -1.77324068e-06}}},
        'spawnEditable': {'spawnAnim': 'animweb/characters/monsters/marauder/traversals/jump_forward_100'},
    })
    assert landing['x'] == pytest.approx(-148.680283)
    assert landing['y'] == pytest.approx(489.9999097732407)
    assert landing['z'] == pytest.approx(4.20000219)


def test_ground_replacement_of_airborne_secret_uses_its_ground_spawn():
    from tools.maps.native_recipe_materializer import _entrance_landings

    records = {name: {'class': 'idTarget_Spawn', 'edit': {
        'spawnPosition': dict(zip(('x', 'y', 'z'), position)),
        'spawnEditable': {'aiStateOverride': 'AIOVERRIDE_TELEPORT'},
    }} for name, position in {
        'air': (-134.999969, 176.750046, -28.5500145),
        'ground': (-156.999939, 166, -42.8248329),
    }.items()}
    recipes = [{
        'binding': {'owner': 'secret', 'reference': name, 'path': 'encounter.events.item[0]'},
        'selected_spawn_type': species,
        'bodies': bodies,
    } for name, species, bodies in (
        ('ground', 'ENCOUNTER_SPAWN_ZOMBIE_TIER_1', []),
        ('air', 'ENCOUNTER_SPAWN_PAIN_ELEMENTAL', [{'species': 'imp'}]),
    )]
    assert _entrance_landings(recipes, records)['air'] == 'ground'


def test_ground_replacement_prefers_its_encounters_jump_endpoint_to_another_floor():
    from tools.maps.native_recipe_materializer import _entrance_landings

    records = {name: {'class': 'idTarget_Spawn', 'edit': {
        'spawnPosition': {'x': 0, 'y': 0, 'z': height}, 'spawnEditable': {}}}
        for name, height in [('air', 5), ('ground', 0)]}
    records['ground']['edit']['spawnEditable'] = {
        'aiStateOverride': 'AIOVERRIDE_PLAY_ENTRANCE_ANIMATION', 'spawnAnim': 'jump_forward_100'}
    records['checkpoint'] = {'class': 'idPlayerStart', 'edit': {'spawnPosition': {'x': 0, 'y': 0, 'z': -10}}}
    recipes = [{'binding': {'owner': 'fight', 'path': 'encounter.events.item[0]', 'reference': name},
                'selected_spawn_type': species, 'bodies': [{'species': 'imp'}]}
               for name, species in [('ground', 'ENCOUNTER_SPAWN_REVENANT'), ('air', 'ENCOUNTER_SPAWN_LOST_SOUL')]]
    assert _entrance_landings(recipes, records)['air'] == 'ground'


@pytest.mark.parametrize('marker_count', [1, 2])
def test_traversal_without_target_uses_only_an_unambiguous_colocated_marker(marker_count):
    from tools.maps.native_recipe_materializer import _entrance_landings

    records = {'point': {'class': 'idTarget_Spawn', 'edit': {
        'spawnPosition': {'x': 1, 'y': 2, 'z': 20},
        'spawnEditable': {'aiStateOverride': 'AIOVERRIDE_FORCE_TELEPORT_TO_TRAVERSAL'}}}}
    records.update({f'ledge{index}': {'class': 'idInfoTraversal', 'edit': {
        'spawnPosition': {'x': 1, 'y': 2, 'z': 20}, 'traversalEndpoint': {'x': 5, 'z': -20}}}
        for index in range(marker_count)})
    recipe = {'binding': {'owner': 'fight', 'path': 'encounter.events.item[0]', 'reference': 'point'},
              'selected_spawn_type': 'ENCOUNTER_SPAWN_SHOTGUN_SOLDIER', 'bodies': [{'species': 'imp'}]}
    assert _entrance_landings([recipe], records).get('point') == ('ledge0' if marker_count == 1 else None)

from doom_eap.content.enemy_options import SPECIES_IDS, normalize_enemy_options
from doom_eap.content.native_enemy_plan import build_native_recipes, native_recipe_metadata, native_recipe_fingerprint, validate_native_recipes
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


def test_shared_spawn_parent_keeps_map_bindings_without_foreign_species_state():
    source = SOURCE.replace('name = "original";', 'name = "lostsoul";') + dedent('''
    entity { entityDef lostsoul { inherit = "ai/fodder/lostsoul"; class = "idAI2"; edit = {
        factionName = "lost_soul";
        targets = { num = 1; item[0] = { name = "relay"; } }
        aiEditable = {
            tempMemoryData = { lostsoul_wait_charge = 1; }
            death = { triggerOnDeath = "relay"; }
            spawnSettings = { initialTarget = "player1"; }
        }
    } } }
    entity { entityDef shared { inherit = "encounter/spawn_group/zone"; class = "idTargetSpawnGroup";
        edit = { targetSpawnParent = "point"; spawners = { num = 1; item[0] = "child"; } } } }
    entity { entityDef child { inherit = "target/spawn"; class = "idTarget_Spawn"; edit = {} } }
    ''')
    plan = frozen_plan(source, binding={'mode': 'event', 'owner': 'arena', 'reference': 'child'})
    compiled = materialize_native_recipe_definitions(source, 'map', plan, {'map': source})
    variants = [row for row in entity_records(compiled)
                if row['name'].startswith('ap_v070_functional_' + _functional_token('lostsoul') + '_')]
    assert variants
    for row in variants:
        assert row['inherit'] in {body['definition'] for recipe in plan['maps']['map']['recipes'] for body in recipe['bodies']}
        assert 'factionName' not in row['edit']
        assert 'tempMemoryData' not in row['edit']['aiEditable']
        assert row['edit']['targets']['item[0]']['name'] == 'relay'
        assert row['edit']['aiEditable']['death']['triggerOnDeath'] == 'relay'
        assert row['edit']['aiEditable']['spawnSettings']['initialTarget'] == 'player1'


@pytest.mark.parametrize('randomized,damage_type,label,expected', [
    (True, 'ENCOUNTER_SPAWN_ANY', '', 'wait'),
    (False, 'ENCOUNTER_SPAWN_ANY', '', 'damageAI'),
    (True, 'ENCOUNTER_SPAWN_IMP', '', 'damageAI'),
    (True, 'ENCOUNTER_SPAWN_ANY', 'cleanup', 'damageAI'),
])
def test_kill_objective_cleanup_keeps_randomized_survivors(randomized, damage_type, label, expected):
    from tools.maps.native_recipe_materializer import _neutralize_cleanup_damage

    source = '''entity { entityDef arena { inherit = "encounter/manager"; class = "idEncounterManager";
        edit = { events = { num = 4;
            item[0] = { eventCall = { eventDef = "waitKillCount"; args = { num = 1;
                item[0] = { eEncounterSpawnType_t = "ENCOUNTER_SPAWN_DOOM_HUNTER"; } } } }
            item[1] = { eventCall = { eventDef = "stopMaintainingAICount"; args = { num = 0; } } }
            item[2] = { eventCall = { eventDef = "damageAI"; args = { num = 3;
                item[0] = { decl = { damage = "damage/ai/kill_full_gib"; } }
                item[1] = { eEncounterSpawnType_t = "''' + damage_type + '''"; }
                item[2] = { string = "''' + label + '''"; } } } }
            item[3] = { eventCall = { eventDef = "waitAIRemaining"; args = { num = 1;
                item[0] = { eEncounterSpawnType_t = "ENCOUNTER_SPAWN_ANY"; } } } }
        } } } }'''
    before, = entity_records(source)
    after, = entity_records(_neutralize_cleanup_damage(source, {'events'} if randomized else set()))
    events = after['edit']['events']
    assert events['item[2]']['eventCall']['eventDef'] == expected
    for index in (0, 1, 3):
        assert events[f'item[{index}]'] == before['edit']['events'][f'item[{index}]']
    if expected == 'wait':
        assert events['item[2]']['eventCall']['args']['item[0]'] == {'float': 0}


@pytest.mark.parametrize('kill_type,stopped,expected', [
    ('ENCOUNTER_SPAWN_ARCHVILE', False, {'refill': 10}),
    ('ENCOUNTER_SPAWN_ZOMBIE_TIER_1', False, {}),
    ('ENCOUNTER_SPAWN_ANY', False, {}),
    ('ENCOUNTER_SPAWN_ARCHVILE', True, {}),
])
def test_initial_maintenance_is_finite_before_a_survivor_barrier(kill_type, stopped, expected):
    source = '''entity { entityDef arena { inherit = "encounter/manager"; class = "idEncounterManager";
        edit = { events = { num = 4;
            item[0] = { eventCall = { eventDef = "maintainAICount"; args = { num = 7;
                item[6] = { string = "zombies"; } } } }
            item[1] = { eventCall = { eventDef = "waitKillCount"; args = { num = 1;
                item[0] = { eEncounterSpawnType_t = "''' + kill_type + '''"; } } } }
            item[2] = { eventCall = { eventDef = "''' + ('stopMaintainingAICount' if stopped else 'wait') + '''";
                args = { num = 2; item[0] = { eEncounterSpawnType_t = "ENCOUNTER_SPAWN_ZOMBIE_TIER_1"; }
                    item[1] = { string = "zombies"; } } } }
            item[3] = { eventCall = { eventDef = "waitAIRemaining"; args = { num = 2;
                item[0] = { eEncounterSpawnType_t = "ENCOUNTER_SPAWN_ANY"; } item[1] = { int = 4; } } } }
        } } } }'''
    recipes = [{'origin': 'refill', 'family': 'maintenance', 'maximum_emissions': 0, 'physical_count': 4,
                'selected_spawn_type': 'ENCOUNTER_SPAWN_ZOMBIE_TIER_1',
                'binding': {'owner': 'arena', 'path': 'events.item[0].eventCall'}},
               {'family': 'immediate', 'maximum_emissions': 6, 'binding': {'owner': 'arena'}}]
    assert _refill_limits(source, recipes) == expected


































def test_darklord_plans_preserve_frozen_phantoms_and_compile_new_rooms_without_them():
    key = 'e5m4_boss'
    catalog = json.loads(files('doom_eap.content').joinpath('native_enemy_catalog.json').read_bytes())
    types = {catalog['species'][name]['spawn_type'] for name in ('hell_knight', 'mancubus', 'baron', 'tyrant')}
    origins = [row for row in catalog['maps'][key]['runtime_origins'] if row['spawn_type'] in types]
    catalog['maps'] = {key: dict(source_sha256=hashlib.sha256(SOURCE.encode()).hexdigest(),
                               origins=[], runtime_origins=origins)}
    catalog['species']['imp']['ai'] = dict(map_key=key, entity='original', sha256=next(entity_records(SOURCE))['sha256'])
    options = normalize_enemy_options(dict(enemies={'revision': 5}, enemy_count_mode='multiplier',
        enemy_count_multiplier=100, enemy_weights={name: int(name == 'imp') for name in SPECIES_IDS},
        enemy_size_variation_chance=0,
        **{f'enemy_{name}': dict(minimum=100, maximum=100) for name in ('health', 'damage', 'speed', 'size')}))
    campaign = dict(sequence=[key])
    frozen = build_native_recipes(options, campaign, seed=8, catalog=catalog, revision=18)
    assert frozen['fingerprint'] == '9d7c86938f5e7a21c1f42c9246361cb78abc03a237586b4c27fc70791fbecb2a'
    assert validate_native_recipes(frozen, options, campaign, catalog=catalog) == frozen
    old_recipes = frozen['maps'][key]['recipes']
    assert sum('/darklord_phantoms/' in row['id'] for row in old_recipes) == 4
    compiled = materialize_native_recipe_calls(SOURCE, key, frozen, {key: SOURCE})
    assert hashlib.sha256(compiled.encode()).hexdigest() == '448ccaf8e15a89fccab2722aebc5547940ecf6de4adbe24eb518f423c33c7ff6'
    current = build_native_recipes(options, campaign, seed=8, catalog=catalog)
    assert current['maps'][key]['recipes'] == [row for row in old_recipes if '/darklord_phantoms/' not in row['id']]
    compiled = materialize_native_recipe_calls(SOURCE, key, current, {key: SOURCE})
    rows = {row['name']: row for row in entity_records(compiled)}
    assert all('ap_v070_bodies_r' + row['origin'] in rows for row in current['maps'][key]['recipes'])


@pytest.mark.parametrize('entity', ['', 'ap_darklord_phantoms'])
def test_runtime_summon_library_compiles_without_an_authorial_template(entity):
    plan = frozen_plan(binding={'mode': 'summon', 'entity': entity}, family='summon',
                       logical_count=1, maximum=1, revision=12)
    compiled = materialize_native_recipe_calls(SOURCE, 'map', plan, {'map': SOURCE})
    rows = {row['name']: row for row in entity_records(compiled)}
    recipe, = plan['maps']['map']['recipes']
    library = rows['ap_v070_bodies_r' + recipe['origin']]['edit']['entityDefs']
    assert library['num'] == len(recipe['bodies']) > 0
    assert all(rows[item['name']]['class'] == 'idAI2' for item in list_items(library))
    states = {rows[item['name']]['edit'].get('aiEditable', {}).get('spawnSettings', {}).get('initialState')
              for item in list_items(library)}
    assert states == {'AIOVERRIDE_FORCE_AWARENESS_OF_PLAYER' if entity else None}


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
