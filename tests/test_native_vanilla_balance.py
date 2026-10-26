from collections import Counter
from copy import deepcopy
from importlib.resources import files
import json

import pytest

from doom_eap.content.enemy_options import normalize_enemy_options
from doom_eap.content.native_enemy_plan import build_native_recipes, native_body_name, validate_native_recipes
from doom_eap.content.enemy_plan import BASE_COMBAT_COST
from doom_eap.content.vanilla_enemy_profiles import SPECIAL_SPECIES, vanilla_profile


@pytest.fixture(scope='module')
def native_catalog():
    return json.loads(files('doom_eap.content').joinpath('native_enemy_catalog.json').read_bytes())


def source(catalog, map_key, species, ordinal, *, count=1, maximum=1, wave=0, owner='arena'):
    return dict(id=f'{map_key}/source/{ordinal}', family='group', logical_count=count,
        maximum_emissions=maximum, logical_types=[catalog['species'][species]['spawn_type']],
        spawn_type=catalog['species'][species]['spawn_type'], combat_cost='1',
        wave=f'{owner}/encounterComponent.entityEvents.item[0]/{wave}', binding={'owner': owner})


def fixture_catalog(native_catalog, maps):
    return dict(schema_version=1, species=deepcopy(native_catalog['species']), maps={
        key: dict(source_sha256='a' * 64, origins=origins) for key, origins in maps.items()})


def effective(**overrides):
    return normalize_enemy_options(dict(enemy_preset='slayers_path', use_dlc_content=True, **overrides))


def recipes(stage):
    return {recipe['id']: recipe for recipe in stage['recipes']}


def test_slayers_path_preserves_finite_categories_and_all_special_species(native_catalog):
    key = 'e5m3_hell'
    species = ['imp', 'hell_knight', 'baron', *sorted(SPECIAL_SPECIES)]
    origins = [source(native_catalog, key, name, index) for index, name in enumerate(species)]
    catalog = fixture_catalog(native_catalog, {key: origins})
    options = effective()
    campaign = dict(sequence=[key], mode='mission_access_as_items', enemy_progression_bands={key: 0})
    plan = build_native_recipes(options, campaign, seed=127, catalog=catalog)
    stage = plan['maps'][key]
    report, = stage['vanilla_reference']['encounters']
    assert Counter(report['reference_finite']) == Counter(report['actual_finite'])
    assert set(report['deviations']) <= {'randomized_species_cost_variation'}
    bodies = [body for recipe in stage['recipes'] for body in recipe['bodies']]
    assert Counter(body['species'] for body in bodies if body['species'] in SPECIAL_SPECIES) == {
        name: 1 for name in SPECIAL_SPECIES}
    peak = vanilla_profile(catalog, key)['encounters'][0]['peak']
    for body in bodies:
        assert body['population_limits']['strict']
        assert body['population_limits']['heavy'] == peak['heavy']
        assert body['population_limits']['superheavy'] == peak['superheavy']
        assert body['population_limits']['species'] == peak['specials']
        assert body['special_limits']['combined_special'] == peak['combined_special']
        assert all(body['attributes'][name] == 100 for name in
                   ('health_percent', 'damage_percent', 'speed_percent'))
        assert body['definition'].isascii() and len(body['definition']) < 512
    assert validate_native_recipes(plan, options, campaign, catalog=catalog) == plan


@pytest.mark.parametrize('count,maximum,reference_counts', [(4, 8, (2, 4)), (1, 1, (1, 1))])
def test_corresponding_encounter_uses_actual_emissions_and_allows_empty_sources(
        native_catalog, count, maximum, reference_counts):
    reference, target = 'e1m1_intro', 'e2m1_nest'
    baseline = [source(native_catalog, reference, name, index, count=quantity, maximum=quantity)
                for index, (name, quantity) in enumerate(zip(('imp', 'hell_knight'), reference_counts))]
    targets = [source(native_catalog, target, 'imp', index, count=count, maximum=maximum)
               for index in range(1 if maximum > count else 3)]
    catalog = fixture_catalog(native_catalog, {reference: baseline, target: targets})
    options = effective()
    campaign = dict(sequence=[target, reference], mode='random_mission_order')
    plan = build_native_recipes(options, campaign, seed=31, catalog=catalog)
    stage = plan['maps'][target]
    report, = stage['vanilla_reference']['encounters']
    assert Counter(report['actual_finite']) == Counter(fodder=reference_counts[0], heavy=reference_counts[1])
    assert Counter(report['reference_finite']) == Counter(report['target_finite'])
    assert not report['deviations']
    assert sum(recipe['maximum_emissions'] for recipe in stage['recipes']) == sum(reference_counts)
    if maximum > count:
        assert len(stage['recipes'][0]['bodies']) == 3
    else:
        assert any(not recipe['bodies'] for recipe in stage['recipes'])


def test_reference_without_renewable_pool_does_not_add_infinite_fodder(native_catalog):
    reference, target = 'e1m1_intro', 'e2m1_nest'
    baseline = [source(native_catalog, reference, 'hell_knight', 0)]
    targets = [source(native_catalog, target, 'imp', 0),
               source(native_catalog, target, 'imp', 1, maximum=0)]
    catalog = fixture_catalog(native_catalog, {reference: baseline, target: targets})
    plan = build_native_recipes(effective(), dict(sequence=[target], mode='random_mission_order'),
        seed=31, catalog=catalog)
    stage = plan['maps'][target]
    report, = stage['vanilla_reference']['encounters']
    assert Counter(report['actual_finite']) == Counter(heavy=1)
    assert not Counter(report['actual_renewable'])
    assert not recipes(stage)[targets[1]['id']]['bodies']
    assert not report['deviations']


def test_opaque_native_population_and_stationary_geometry_are_preserved(native_catalog):
    key = 'e2m2_base'
    known = source(native_catalog, key, 'imp', 0)
    opaque = source(native_catalog, key, 'imp', 1)
    opaque['logical_types'].append(native_catalog['species']['baron']['spawn_type'])
    stationary = source(native_catalog, key, 'tentacle', 2, owner='native_relay')
    catalog = fixture_catalog(native_catalog, {key: [known, opaque, stationary]})
    plan = build_native_recipes(effective(), dict(sequence=[key], mode='mission_access_as_items',
        enemy_progression_bands={key: 0}),
        seed=31, catalog=catalog, revision=13)
    stage = plan['maps'][key]
    assert set(recipes(stage)) == {known['id']}
    preserved = {row['id']: row['reason'] for row in stage['protected']}
    assert preserved[opaque['id']] == 'unknown native population choice'
    assert preserved[stationary['id']] == 'native stationary spawn'
    metadata = stage['vanilla_reference']
    assert metadata['unknown_native_sources'][0]['source_id'] == opaque['id']
    relay = next(row for row in metadata['encounters'] if row['owner'] == 'native_relay')
    assert relay['translation'] == 'native_geometry_passthrough'
    assert relay['reference_map'] == key
    assert not relay['deviations']


def test_longer_map_repeats_the_reference_curve_and_randomizes_tentacles(native_catalog):
    reference, target = 'e1m1_intro', 'e2m1_nest'
    baseline = [source(native_catalog, reference, 'imp', index, count=index + 1, maximum=index + 1,
                       owner=f'fight{index}') for index in range(5)]
    targets = [source(native_catalog, target, 'imp', index, owner=f'room{index}') for index in range(10)]
    catalog = fixture_catalog(native_catalog, {reference: baseline, target: targets})
    plan = build_native_recipes(effective(), dict(sequence=[target, reference], mode='random_mission_order'),
                                seed=31, catalog=catalog)
    reports = plan['maps'][target]['vanilla_reference']['encounters']
    assert [row['actual_finite_total'] for row in reports] == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]
    tentacle = source(native_catalog, reference, 'tentacle', 0, count=2, maximum=2)
    catalog = fixture_catalog(native_catalog, {reference: [tentacle]})
    plan = build_native_recipes(effective(), dict(sequence=[reference], mode='random_mission_order'),
                                seed=31, catalog=catalog)
    recipe, = plan['maps'][reference]['recipes']
    assert len(recipe['bodies']) == 2
    assert all(body['species'] != 'tentacle' for body in recipe['bodies'])
    catalog = fixture_catalog(native_catalog, {
        reference: [source(native_catalog, reference, 'cueball', 0)],
        target: [source(native_catalog, target, 'tentacle', 0, count=2, maximum=2)],
    })
    plan = build_native_recipes(effective(), dict(sequence=[target, reference], mode='random_mission_order'),
                                seed=31, catalog=catalog)
    recipe, = plan['maps'][target]['recipes']
    assert len(recipe['bodies']) == 2


def test_short_rmo_spans_full_reference_curve_and_mai_keeps_map_identity(native_catalog):
    sequence = ['e5m3_hell', 'e1m2_war', 'e4m3_mcity', 'e2m1_nest',
                'e3m1_slayer', 'e1m1_intro', 'e5m4_boss']
    options = effective()
    campaign = dict(sequence=sequence, mode='random_mission_order')
    plan = build_native_recipes(options, campaign, seed=20261025, catalog=native_catalog)
    assert plan['maps'][sequence[0]]['vanilla_reference']['map_key'] == 'e1m1_intro'
    assert plan['maps'][sequence[-2]]['vanilla_reference']['map_key'] == 'e5m3_hell'
    assert plan['maps'][sequence[-1]]['vanilla_reference']['map_key'] == 'e5m4_boss'
    assert all(stage['vanilla_reference']['target_encounters'] >=
               stage['vanilla_reference']['known_target_encounters'] for stage in plan['maps'].values())
    campaign['mode'] = 'mission_access_as_items'
    campaign['enemy_progression_bands'] = {key: index for index, key in enumerate(sequence)}
    mai = build_native_recipes(options, campaign, seed=20261025, catalog=native_catalog)
    assert all(stage['vanilla_reference']['map_key'] == key for key, stage in mai['maps'].items())


def test_vanilla_heavy_substitutions_respect_native_pressure(native_catalog):
    key = 'e1m1_intro'
    origins = [source(native_catalog, key, 'cacodemon', 0, count=3, maximum=3)]
    catalog = fixture_catalog(native_catalog, {key: origins})
    plan = build_native_recipes(effective(), dict(sequence=[key]), seed=0, catalog=catalog, revision=13)
    report, = plan['maps'][key]['vanilla_reference']['encounters']
    bodies = plan['maps'][key]['recipes'][0]['bodies']
    assert Counter(report['actual_finite']) == Counter(heavy=3)
    assert sum(BASE_COMBAT_COST[body['species']] for body in bodies) <= 3 * BASE_COMBAT_COST['cacodemon']


def test_superheavy_roster_can_vary_above_the_cheapest_native_species(native_catalog):
    key = 'e5m3_hell'
    catalog = fixture_catalog(native_catalog, {key: [source(native_catalog, key, 'baron', 0, count=8, maximum=8)]})
    plan = build_native_recipes(effective(), dict(sequence=[key]), seed=12, catalog=catalog)
    report, = plan['maps'][key]['vanilla_reference']['encounters']
    bodies = plan['maps'][key]['recipes'][0]['bodies']
    assert Counter(report['actual_finite']) == Counter(superheavy=8)
    assert any(body['species'] != 'baron' for body in bodies)
    assert all(body['population_limits']['superheavy'] == 8 for body in bodies)


def test_each_encounter_keeps_its_own_combined_special_limit(native_catalog):
    key = 'e5m3_hell'
    origins = [source(native_catalog, key, 'spirit', 0, count=2, maximum=2, owner='spirits'),
               source(native_catalog, key, 'marauder', 1, owner='marauder')]
    catalog = fixture_catalog(native_catalog, {key: origins})
    plan = build_native_recipes(effective(), dict(sequence=[key], mode='mission_access_as_items',
        enemy_progression_bands={key: 0}), seed=31, catalog=catalog)
    for recipe in plan['maps'][key]['recipes']:
        expected = 2 if recipe['binding']['owner'] == 'spirits' else 1
        assert all(body['special_limits']['combined_special'] == expected for body in recipe['bodies'])
        assert all(body['population_limits']['combined_special'] == expected for body in recipe['bodies'])


def test_script_terminated_maintenance_preserves_native_ceiling_without_finite_kill_quota(native_catalog):
    key = 'e5m1_spear'
    finite = source(native_catalog, key, 'imp', 0)
    maintained = source(native_catalog, key, 'zombie_tier_1', 1, count=3, maximum=9999)
    maintained.update(family='maintenance', effective_replenishment=True)
    catalog = fixture_catalog(native_catalog, {key: [finite, maintained]})
    plan = build_native_recipes(effective(), dict(sequence=[key], mode='mission_access_as_items',
        enemy_progression_bands={key: 0}), seed=31, catalog=catalog)
    stage = plan['maps'][key]
    report, = stage['vanilla_reference']['encounters']
    recipe = recipes(stage)[maintained['id']]
    assert recipe['renewable']
    assert recipe['maximum_emissions'] == 9999
    assert recipe['physical_count'] == 3
    assert report['actual_finite_total'] == 1
    assert Counter(report['actual_renewable']) == Counter(fodder=3)
    assert plan['combat_costs'][key]['campaign']['finite_spawns'] == 1
    assert not report['deviations']


def test_finite_encounter_does_not_receive_another_scripts_replenishment_ceiling(native_catalog):
    reference, target = 'e1m1_intro', 'e2m1_nest'
    stopped = source(native_catalog, reference, 'zombie_tier_1', 1, count=3, maximum=9999)
    stopped.update(family='maintenance', effective_replenishment=True)
    baseline = [source(native_catalog, reference, 'imp', 0), stopped,
                source(native_catalog, reference, 'imp', 2, count=2, maximum=2, owner='finite_arena')]
    catalog = fixture_catalog(native_catalog, {reference: baseline,
        target: [source(native_catalog, target, 'imp', 0)]})
    plan = build_native_recipes(effective(), dict(sequence=[target, reference], mode='random_mission_order'),
        seed=31, catalog=catalog)
    stage = plan['maps'][target]
    report, = stage['vanilla_reference']['encounters']
    assert report['reference_owner'] == 'finite_arena'
    assert report['actual_finite_total'] == 2
    assert sum(recipe['maximum_emissions'] for recipe in stage['recipes']) == 2
    assert not Counter(report['actual_renewable'])
    assert not report['deviations']
