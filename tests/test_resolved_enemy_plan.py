import json
import random
from copy import deepcopy
from importlib.resources import files

import pytest

from doom_eap.content.enemy_options import SPECIES_IDS
from doom_eap.content.enemy_plan import build_resolved_enemy_blocks, enemy_combat_cost
from doom_eap.content.native_enemy_plan import build_native_recipes, encode_native_recipes, native_recipe_fingerprint
from doom_eap.content.recipe_codec import decode_recipes, encode_recipes
from doom_eap.content.secret_encounter_timers import load_secret_encounter_timers
from doom_eap.content.slot_data_contract import (
    StructureError,
    recipe_digest,
    source_index,
    source_reference,
    validate_slot_data,
)
from tests.room_fixture import sample_catalog, sample_slot, sign_slot
from tests.test_native_enemy_plan import settings

KEY = "e4m3_mcity"
ROLES = ("primary", "compatibility", "counter")


def resolved_case():
    native = json.loads(files('doom_eap.content').joinpath('native_enemy_catalog.json').read_bytes())
    species = ['cacodemon', 'imp', 'spirit']
    timer = load_secret_encounter_timers()['maps'][KEY]['timers'][0]
    event = dict(id=KEY + '/arena/event', family='group', logical_count=2, maximum_emissions=4,
                 combat_cost='14', spawn_type='ENCOUNTER_SPAWN_CACODEMON',
                 logical_types=['ENCOUNTER_SPAWN_CACODEMON'],
                 binding=dict(mode='event', entity='arena', owner='arena', reference='spawn',
                              path='encounterComponent.entityEvents.item[0].events.item[0].eventCall'))
    summon = {**event, 'id': KEY + '/summon', 'family': 'summon',
              'binding': dict(mode='summon', entity='summoner', owner=timer['manager'])}
    protected = dict(id=KEY + '/protected', protected_reason='tutorial')
    inactive = dict(id=KEY + '/inactive', inactive_reason='unused')
    native = {**native, 'species': {name: native['species'][name] for name in species},
              'maps': {KEY: dict(source_sha256='a' * 64, origins=[event, summon, protected, inactive])}}
    catalog = sample_catalog()
    catalog.update(stages={KEY: dict(access_id=7771000)}, maps={KEY: dict(stage_id=KEY)},
                   templates={name: dict(species=name) for name in species}, species=species,
                   roles=list(ROLES), spawn_types=['ENCOUNTER_SPAWN_CACODEMON'],
                   sources=source_index([dict(stage_id=KEY, source_id=row['id'], role=role)
                            for row in (event, summon) for role in ROLES] +
                           [dict(stage_id=KEY, source_id=row['id']) for row in (protected, inactive)]),
                   timers=source_index([dict(timer, stage_id=KEY, source_id=timer['entity'], role='timers')]),
                   finite_sources=source_index([dict(stage_id=KEY, source_id='arena/0/0', role='sources')]))
    kill_catalog = dict(schema_version=2, maps={KEY: dict(source_sha256='a' * 64, estimated_kills=4,
        sources=[dict(source_id='arena/0/0', map_key=KEY, count=4, repeatable=True)])})
    options = settings(enemy_count_mode='multiplier', enemy_count_multiplier=150,
                       enemy_weights={name: int(name == 'spirit') for name in SPECIES_IDS})
    campaign = dict(sequence=[KEY], stage_ids=[KEY])
    return native, catalog, kill_catalog, options, campaign


def readiness_profiles(plan):
    profile = deepcopy(sample_slot()['stage_profiles']['a'])
    profile['counter_ids'] = []
    return {KEY: profile}


def blocks_for_case(case, resolver=readiness_profiles):
    native, catalog, kills, options, campaign = case
    return build_resolved_enemy_blocks(options, campaign, seed=712, catalog=catalog,
        native_catalog=native, kill_catalog=kills, resolve_stage_profiles=resolver,
        killsanity_options=dict(enabled=True, interval=2, count=10))


def admitted_blocks(blocks, catalog):
    slot = sample_slot(catalog, blocks['recipes'])
    slot.update(blocks)
    slot['campaign'].update(sequence=[KEY], starting_stages=[KEY])
    slot['campaign']['goal']['stage_id'] = KEY
    return validate_slot_data(sign_slot(slot, catalog), catalog)


def test_final_safety_decisions_precede_accounting_and_readiness():
    case = resolved_case()
    observed = []

    def readiness(plan):
        stage = plan['maps'][KEY]
        assert 'compatibility_recipes' in stage and 'counter_recipes' in stage
        assert {body['species'] for body in stage['recipes'][1]['bodies']} == {'cacodemon'}
        cost = sum(enemy_combat_cost(body['species'], body['attributes'], buff_totems=True, screechers=True)
                   for recipe in stage['recipes'] for body in recipe['bodies'])
        assert plan['combat_costs'][KEY]['actual'] == float(cost)
        assert plan['fingerprint'] == native_recipe_fingerprint(plan)
        observed.append(plan)
        return readiness_profiles(plan)

    blocks = blocks_for_case(case, readiness)
    assert len(observed) == 1
    assert blocks['killsanity']['finite_kills'] == 6
    assert blocks['killsanity']['finite_supported_count'] == 3
    timer_ref, timer = next(iter(case[1]['timers'].items()))
    raw = observed[0]['secret_timers'][KEY][timer['entity']]
    expected = max(raw * 5 // 4, raw + 5000) * 3 // 2
    assert blocks['recipes']['timers'] == [[timer_ref, expected]]
    admitted_blocks(blocks, case[1])


def species_for_role(rows, catalog, role):
    return [catalog['templates'][body.template_ref]['species'] for row in rows
            if row.role_ref == role for body in row.bodies]


def test_alternatives_survive_encoding_and_resolved_recipes_round_trip_and_digest(monkeypatch):
    case = resolved_case()
    blocks = blocks_for_case(case)
    admitted = admitted_blocks(blocks, case[1])
    digest = recipe_digest(blocks['recipes'])
    final = build_native_recipes(case[3], case[4], seed=712, catalog=case[0])

    def forbidden(*args, **kwargs):
        raise AssertionError('Resolved encoding and decoding must not draw randomness')

    monkeypatch.setattr(random.Random, 'random', forbidden)
    monkeypatch.setattr(random.Random, 'getrandbits', forbidden)
    decoded = decode_recipes(admitted.recipes)
    packed = encode_recipes(decoded)
    assert packed == blocks['recipes']
    assert recipe_digest(packed) == digest
    rows = decoded.maps[KEY].rows
    assert {row.role_ref for row in rows} == set(ROLES)
    assert species_for_role(rows, case[1], 'compatibility') == ['spirit'] * 3
    assert species_for_role(rows, case[1], 'counter') == ['cacodemon'] * 3
    assert encode_native_recipes(final, case[1], active_maps=[KEY]) == packed


def test_finite_kill_budget_reflects_final_recipes(monkeypatch):
    from doom_eap.content import native_enemy_safety

    original = native_enemy_safety.resolve_native_stage

    def resolve(stage, *args):
        original(stage, *args)
        recipe = stage['recipes'][0]
        recipe.update(bodies=recipe['bodies'][:1], physical_count=1, maximum_emissions=2,
                      final_credit_units=2, quantity_ratio=[1, 2])
        stage['counter_recipes'][0] = deepcopy(recipe)
        return stage

    monkeypatch.setattr(native_enemy_safety, 'resolve_native_stage', resolve)
    blocks = blocks_for_case(resolved_case())
    assert blocks['killsanity'] == dict(interval=2, count=10, finite_supported_count=1,
                                      finite_kills=2, estimated_kills=8, sources=[[source_reference(KEY, 'arena/0/0', 'sources'), 2]])


def test_disabled_recipes_resolve_native_timers_and_source_decisions():
    native, catalog, kills, options, campaign = resolved_case()
    options = {**options, 'enabled': False}
    blocks = build_resolved_enemy_blocks(options, campaign, seed=712, catalog=catalog,
        native_catalog=native, kill_catalog=kills, resolve_stage_profiles=readiness_profiles,
        killsanity_options=dict(enabled=False), easier_secret_encounters=False)
    assert blocks['recipes']['enabled'] is False
    refs = catalog['sources']
    assert blocks['recipes']['maps'][KEY] == dict(rows=[],
        passthrough_sources=sorted(ref for ref, row in refs.items() if row.get('role', 'primary') == 'primary'),
        disabled_sources=sorted(ref for ref, row in refs.items() if row.get('role', 'primary') != 'primary'))
    timer_ref, timer = next(iter(catalog['timers'].items()))
    duration = timer['duration_ms']
    assert blocks['recipes']['timers'] == [[timer_ref, max(duration * 5 // 4, duration + 5000)]]
    assert blocks['killsanity'] == dict(interval=0, count=0, finite_supported_count=0,
                                      finite_kills=0, estimated_kills=0, sources=[])
    admitted_blocks(blocks, catalog)


@pytest.mark.parametrize('count', [2001, 65535, True, 2.0])
def test_more_than_two_thousand_kill_rewards_are_rejected(count):
    from doom_eap.content.killsanity_plan import build_resolved_killsanity

    with pytest.raises(StructureError, match='Auto or 1..2000'):
        build_resolved_killsanity(dict(enabled=True, interval=1, count=count), {}, {})


def test_missing_recipe_reference_raises_a_typed_error():
    from doom_eap.content.slot_data_contract import ReferenceError

    case = resolved_case()
    case[1]['templates'] = {}
    with pytest.raises(ReferenceError, match='no catalog reference'):
        blocks_for_case(case)


def test_large_resolved_blocks_raise_a_typed_size_error(monkeypatch):
    from doom_eap.content import slot_data_contract
    from doom_eap.content.slot_data_contract import PayloadSizeError

    monkeypatch.setattr(slot_data_contract, 'MAX_PAYLOAD_BYTES', 1)
    with pytest.raises(PayloadSizeError) as error:
        blocks_for_case(resolved_case())
    assert error.value.total_bytes > error.value.recipe_bytes > 1


def test_population_limits_and_runtime_ratio_survive_producer_encoding():
    case = resolved_case()
    blocks = blocks_for_case(case)
    plan = build_native_recipes(case[3], case[4], seed=712, catalog=case[0])
    recipe = plan['maps'][KEY]['recipes'][0]
    body = recipe['bodies'][0]
    body['population_limits'] = dict(scope=123, maximum=8, heavy=0, superheavy=0, pressure=8,
                                    mixed=False, strict=True, counter='spirit', species={'spirit': 2})
    body['special_limits'] = dict(marauder=1, archvile=2, spirit=3, combined_special=4)
    recipe.update(bodies=[body], logical_count=2, maximum_emissions=2, final_credit_units=2,
                  quantity_ratio=[12, 2])
    blocks['recipes'] = encode_native_recipes(plan, case[1], active_maps=[KEY])
    rows = decode_recipes(admitted_blocks(blocks, case[1]).recipes).maps[KEY].rows
    row = next(row for row in rows if case[1]['sources'][row.source_ref]['source_id'] == recipe['id']
               and row.role_ref == 'primary')
    assert (row.ratio_numerator, len(row.bodies)) == (12, 1)
    assert row.bodies[0].limits.special == (1, 2, 3, 4)
    population = row.bodies[0].limits.population
    assert population.scope == '000000000000007b'
    assert (population.strict, population.heavy) == (True, 0)
    assert (population.counter, population.species_caps) == ('spirit', (('spirit', 2),))


@pytest.fixture(params=[('e4m1_rig', 'spirit'), ('e4m1_rig', 'tentacle'),
                        ('e4m1_rig', 'super_tentacle'), ('e4m3_mcity', 'tentacle')])
def spawn_safety_case(request):
    from doom_eap.content.slot_data_contract import load_slot_catalog
    from tests.test_resolved_room_materialization import installed_slot

    key, species = request.param
    catalog = load_slot_catalog()
    observed = []
    def readiness(plan):
        observed.append(plan)
        return {key: deepcopy(sample_slot()['stage_profiles']['a'])}
    options = settings(enemy_weights={name: 100 * int(name == species) + int(name == 'imp') for name in SPECIES_IDS})
    blocks = build_resolved_enemy_blocks(options, dict(sequence=[key], stage_ids=[key]), seed=712,
        catalog=catalog, resolve_stage_profiles=readiness,
        killsanity_options=dict(enabled=True, interval=2, count=10))
    data = installed_slot(catalog, blocks['recipes'], [key])
    return key, species, catalog, observed[0], blocks, data


def _assert_safe_recipe_round_trip(blocks, denied, slot):
    assert all(body['species'] != 'spirit' for recipe in denied for body in recipe['bodies'])
    assert encode_recipes(decode_recipes(slot.recipes)) == blocks['recipes']


def _assert_resolved_recipe_cost(stage):
    assert all(body['species'] not in {'tentacle', 'super_tentacle'}
               for recipe in stage['recipes'] for body in recipe['bodies'])


def test_spawn_safety_rows_pass_consumer_without_resolution(spawn_safety_case, monkeypatch):
    from doom_eap.content.native_enemy_safety import SPIRIT_DENIED_SPAWN_POINTS
    from tests.test_resolved_room_materialization import forbid_selection
    from tools.maps.native_recipe_materializer import expand_resolved_native_recipes

    key, species, catalog, plan, blocks, data = spawn_safety_case
    cost = sum(enemy_combat_cost(body['species'], body['attributes'], buff_totems=True, screechers=True)
               for recipe in plan['maps'][key]['recipes'] if not recipe['runtime_only'] for body in recipe['bodies'])
    assert plan['combat_costs'][key]['actual'] == float(cost)
    assert plan['fingerprint'] == native_recipe_fingerprint(plan)
    slot = validate_slot_data(data, catalog)
    forbid_selection(monkeypatch)
    stage = expand_resolved_native_recipes(slot, catalog)['maps'][key]
    _assert_resolved_recipe_cost(stage)
    denied = [recipe for recipe in stage['recipes'] if recipe['binding'].get('reference') in
              SPIRIT_DENIED_SPAWN_POINTS.get(key, ())]
    _assert_safe_recipe_round_trip(blocks, denied, slot)


def test_unsafe_resolved_body_is_refused_instead_of_repaired(spawn_safety_case):
    from dataclasses import replace

    from doom_eap.content.slot_data_contract import catalog_tables
    from tools.maps.native_recipe_materializer import RecipeMaterializationError, expand_resolved_native_recipes

    key, species, catalog, plan, blocks, data = spawn_safety_case
    resolved = decode_recipes(validate_slot_data(data, catalog).recipes)
    tables = catalog_tables(catalog)
    stage = resolved.maps[key]
    row = next(row for row in stage.rows if row.role_ref == 'compatibility')
    primary = next(candidate for candidate in stage.rows if candidate.role_ref == 'primary' and
        tables['sources'][candidate.source_ref]['source_id'] == tables['sources'][row.source_ref]['source_id'])
    unsafe = replace(primary, bodies=row.bodies)
    rows = tuple(unsafe if candidate is primary else candidate for candidate in stage.rows)
    packed = encode_recipes(replace(resolved, maps={**resolved.maps, key: replace(stage, rows=rows)}))
    slot = validate_slot_data(sign_slot({**deepcopy(data), 'recipes': packed}, catalog), catalog)
    with pytest.raises(RecipeMaterializationError, match='spawn safety|flying bodies'):
        expand_resolved_native_recipes(slot, catalog)
