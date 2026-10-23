from copy import deepcopy
from fractions import Fraction

import pytest

from doom_eap.content.enemy_options import SPECIES_IDS, normalize_enemy_options
from doom_eap.content.native_enemy_plan import build_native_recipes, validate_native_recipes


def catalog():
    return {"schema_version": 1, "species": {
        "imp": {"spawn_type": "ENCOUNTER_SPAWN_IMP", "hitpoints": {"max": 200, "starting": 200},
                "ai": {"map_key": "base", "entity": "imp", "sha256": "b" * 64}},
        "stone_imp": {"spawn_type": "ENCOUNTER_SPAWN_STONE_IMP", "hitpoints": {"max": 200, "starting": 200},
                      "ai": {"map_key": "dlc", "entity": "stone_imp", "sha256": "c" * 64}}}, "maps": {"base": {
        "source_sha256": "a" * 64, "origins": [{"id": "base/source/0", "family": "maintenance",
            "spawn_type": "ENCOUNTER_SPAWN_IMP", "logical_types": ["ENCOUNTER_SPAWN_IMP"],
            "logical_count": 2, "maximum_emissions": 8, "combat_cost": "2"},
            {"id": "base/protected", "protected_reason": "tutorial"}]}}}


def settings(**options):
    return normalize_enemy_options({"enemy_size_variation_chance": 0,
        **{f"enemy_{name}": {"minimum": 100, "maximum": 100} for name in ("health", "damage", "speed", "size")},
        **options})


def test_opening_keeps_requests_and_counters_out_of_early_fights():
    source = catalog()
    stage = source['maps'].pop('base')
    origin = stage['origins'][0]
    origin['binding'] = {'owner': 'barge_encounter_manager_barge_arena'}
    source['maps']['e1m1_intro'] = stage
    options = settings(enemy_weights={'imp': 1, 'stone_imp': 1000})
    campaign = {'sequence': ['e1m1_intro'], 'opening_stage': 'e1m1_intro'}
    plan = build_native_recipes(options, campaign, seed=7, catalog=source)
    bodies = plan['maps']['e1m1_intro']['recipes'][0]['bodies']
    assert len(bodies) >= origin['logical_count']
    assert {body['species'] for body in bodies} == {'imp'}
    assert plan['combat_costs']['e1m1_intro']['campaign']['opening_waves']
    assert validate_native_recipes(plan, options, catalog=source) == plan


def test_late_balanced_waves_can_include_expensive_specials_and_keep_the_body_floor():
    from importlib.resources import files
    import json
    from doom_eap.content.native_enemy_plan import _phase_bodies
    source=json.loads(files('doom_eap.content').joinpath('native_enemy_catalog.json').read_bytes())
    options=settings(enemy_variety=100)
    candidates=sorted(name for name in source['species'] if options['weights'].get(name,0)>0)
    bodies=_phase_bodies([{'id':'wave','logical_count':400}],candidates,options,Fraction(145,100),
        seed=7,key='late_arena',catalog=source,stage_index=5,stage_count=6,balanced=True,revision=7)['wave']
    assert len(bodies)==400
    assert {'archvile','marauder','doom_hunter'} <= {body['species'] for body in bodies}


def test_auto_preserves_variety_after_the_original_budget_would_be_exhausted():
    source = catalog()
    origin = source['maps']['base']['origins'][0]
    origin.update(logical_count=100, maximum_emissions=100, combat_cost='100')
    options = settings(enemy_variety=100, progressive_difficulty=False)
    plan = build_native_recipes(options, {'sequence': ['base']}, seed=7, catalog=source)
    bodies = plan['maps']['base']['recipes'][0]['bodies']
    assert len(bodies) == 100
    assert 15 < sum(body['species'] == 'stone_imp' for body in bodies) < 85
    assert validate_native_recipes(plan, options, catalog=source) == plan


def test_frozen_composition_survives_rmo_ranking_and_revision_two_validation():
    source = catalog()
    source['maps']['other'] = deepcopy(source['maps']['base'])
    options = settings()
    campaign = {'sequence': ['base', 'other'], 'composition_bands': {'base': 1, 'other': 0}}
    plan = build_native_recipes(options, campaign, seed=7, catalog=source)
    reordered = build_native_recipes(options, {**campaign, 'sequence': ['other', 'base']}, seed=7, catalog=source)
    assert plan['maps'] == reordered['maps'] and plan['combat_costs'] == reordered['combat_costs']
    legacy = build_native_recipes(options, {'sequence': ['base']}, seed=7, catalog=source, revision=2)
    assert validate_native_recipes(legacy, options, catalog=source) == legacy


def test_campaign_rating_excludes_master_level_and_runtime_only_origins():
    source = catalog()
    origin = source['maps']['base']['origins'][0]
    origin['layers'] = ['Campaign']
    source['maps']['base']['origins'] += [
        {**origin, 'id': 'master', 'logical_count': 100, 'layers': ['Master_Level']},
        {**origin, 'id': 'runtime', 'logical_count': 100, 'runtime_only': True}]
    options = settings(enemy_weights={name: int(name == 'imp') for name in SPECIES_IDS})
    plan = build_native_recipes(options, {'sequence': ['base']}, seed=7, catalog=source)
    profile = plan['combat_costs']['base']['campaign']
    assert profile['bodies'] == 2 and profile['species'] == ['imp']
    assert profile['actual'] < plan['combat_costs']['base']['actual']


def test_cached_validation_rechecks_plan_options_and_catalog_mutations():
    source, options = catalog(), settings()
    campaign = {'sequence': ['base']}
    plan = build_native_recipes(options, campaign, seed=351, catalog=source)
    validate_native_recipes(plan, options, campaign, catalog=source)
    assert validate_native_recipes(deepcopy(plan), options, campaign, catalog=source) == plan
    changed = deepcopy(plan)
    changed['maps']['base']['recipes'][0]['physical_count'] += 1
    with pytest.raises(ValueError):
        validate_native_recipes(changed, options, campaign, catalog=source)
    changed_source = deepcopy(source)
    changed_source['maps']['base']['source_sha256'] = 'f' * 64
    with pytest.raises(ValueError):
        validate_native_recipes(plan, options, campaign, catalog=changed_source)
    changed_options = settings(enemy_count_mode='multiplier', enemy_count_multiplier=900)
    with pytest.raises(ValueError):
        validate_native_recipes(plan, changed_options, campaign, catalog=source)


def test_frozen_native_maintenance_recipe_keeps_logical_identity_and_scales_emission_quota():
    options = settings(enemy_count_mode="multiplier", enemy_count_multiplier=300,
                       enemy_weights={name: int(name == "stone_imp") for name in SPECIES_IDS})
    source = catalog()
    plan = build_native_recipes(options, {"sequence": ["base"]}, seed=12, catalog=source)
    assert plan == build_native_recipes(options, {"sequence": ["base"]}, seed=12, catalog=source)
    recipe, = plan["maps"]["base"]["recipes"]
    assert recipe["logical_count"] == 2 and recipe["physical_count"] == 6
    assert recipe["maximum_emissions"] == 24
    assert recipe["selected_spawn_type"] == "ENCOUNTER_SPAWN_IMP"
    assert all(body["species"] == "stone_imp" for body in recipe["bodies"])
    assert len({body["definition"] for body in recipe["bodies"]}) == 6
    assert plan["maps"]["base"]["protected"] == [{"id": "base/protected", "reason": "tutorial"}]
    assert source == catalog()
    assert validate_native_recipes(plan, options, {"sequence": ["base"]}, catalog=source) == plan
    changed = deepcopy(plan)
    changed["maps"]["base"]["recipes"][0]["bodies"][0]["species"] = "imp"
    with pytest.raises(ValueError, match="differ"):
        validate_native_recipes(changed, options, {"sequence": ["base"]}, catalog=source)


def test_auto_preserves_source_quantity_when_attributes_make_enemies_more_expensive():
    source = catalog()
    source["maps"]["base"]["origins"][0]["combat_cost"] = "20"
    options = settings(enemy_weights={name: int(name == "imp") for name in SPECIES_IDS})
    plan = build_native_recipes(options, {"sequence": ["base"]}, seed=12, catalog=source)
    double = {**options, "attributes": deepcopy(options["attributes"])}
    double["attributes"]["health"]["global"] = {"early": [200, 200], "late": [200, 200]}
    heavier = build_native_recipes(double, {"sequence": ["base"]}, seed=12, catalog=source)
    assert plan["maps"]["base"]["recipes"][0]["physical_count"] == 2
    assert heavier["maps"]["base"]["recipes"][0]["physical_count"] == 2
    assert heavier['combat_costs']['base']['actual'] > plan['combat_costs']['base']['actual']


def test_native_plan_round_trips_seed_types_and_rejects_invalid_campaign_progression():
    source = catalog()
    options = settings(enemy_count_mode="multiplier", enemy_count_multiplier=300)
    plan = build_native_recipes(options, {"sequence": ["base"]}, seed=12, catalog=source)
    assert plan == build_native_recipes(options, plan["campaign"], seed="12", catalog=source)
    assert validate_native_recipes(plan, options, catalog=source) == plan
    for campaign in ({"sequence": ["base", "base"]}, {"sequence": [["base"]]},
                     {"sequence": ["base"], "mode": "mission_access_as_items", "enemy_progression_bands": {"base": 5}}):
        with pytest.raises(ValueError, match="campaign"):
            build_native_recipes(options, campaign, seed=12, catalog=source)


def test_unbounded_maintenance_and_unresolved_sources_are_distinct():
    source = catalog()
    source["maps"]["base"]["origins"][0]["maximum_emissions"] = 0
    plan = build_native_recipes(settings(), {"sequence": ["base"]}, seed=8, catalog=source)
    assert plan["maps"]["base"]["recipes"][0]["maximum_emissions"] == 0
    source["maps"]["base"]["unresolved"] = [{"id": "unresolved"}]
    with pytest.raises(ValueError, match="Unresolved native origin"):
        build_native_recipes(settings(), {"sequence": ["base"]}, seed=8, catalog=source)


def test_ammunition_supply_and_its_initial_wave_keep_chainsaw_targets():
    source = catalog()
    source['species']['prowler'] = {**source['species']['stone_imp'], 'spawn_type': 'ENCOUNTER_SPAWN_PROWLER'}
    refill = source['maps']['base']['origins'][0]
    refill.update(maximum_emissions=0, logical_types=['ENCOUNTER_SPAWN_IMP', 'ENCOUNTER_SPAWN_PROWLER'],
        binding={'owner': 'arena', 'arguments': [{'string': 'refill'}]})
    initial = {**deepcopy(refill), 'id': 'initial', 'family': 'staggered', 'maximum_emissions': 2}
    source['maps']['base']['origins'].append(initial)
    for mode in ('auto', 'multiplier'):
        plan = build_native_recipes(settings(enemy_count_mode=mode, enemy_count_multiplier=1),
            {'sequence': ['base']}, seed=8, catalog=source)
        recipes = plan['maps']['base']['recipes']
        assert all(len(recipe['bodies']) >= 2 for recipe in recipes)
        assert {body['species'] for recipe in recipes for body in recipe['bodies']} == {'imp'}
        assert recipes[0]['maximum_emissions'] == 0 and recipes[1]['maximum_emissions'] == 2
    excluded = settings(enemy_weights={name: int(name == 'stone_imp') for name in SPECIES_IDS})
    with pytest.raises(ValueError, match='Ammunition source'):
        build_native_recipes(excluded, {'sequence': ['base']}, seed=8, catalog=source)


def test_progressive_composition_bounds_each_body_without_erasing_requests():
    source = catalog()
    source['maps']['late'] = deepcopy(source['maps']['base'])
    source['maps']['base']['origins'][0].update(logical_count=100, maximum_emissions=100)
    source['maps']['late']['origins'][0].update(logical_count=100, maximum_emissions=100)
    options = settings(enemy_variety=100)
    plan = build_native_recipes(options, {'sequence': ['base', 'late']}, seed=7, catalog=source)
    early, late = (plan['maps'][key]['recipes'][0]['bodies'] for key in ('base', 'late'))
    assert len(early) == len(late) == 100
    assert {'imp', 'stone_imp'} <= {body['species'] for body in early}
    assert sum(body['species'] == 'stone_imp' for body in late) > sum(body['species'] == 'stone_imp' for body in early)
    legacy = build_native_recipes(options, {'sequence': ['base', 'late']}, seed=7, catalog=source, revision=3)
    assert any(body['species'] == 'stone_imp' for body in legacy['maps']['base']['recipes'][0]['bodies'])
    assert validate_native_recipes(legacy, options, catalog=source) == legacy


def test_blocking_waves_progress_independently_in_parallel_encounters():
    source = catalog()
    origin = source['maps']['base']['origins'][0]
    source['maps']['base']['origins'] = [
        {**origin, 'id': f'{owner}/{wave}', 'wave': f'{owner}/0/{wave}'}
        for owner in ('arena', 'parallel') for wave in (0, 4)]
    health = dict(early_minimum=100, early_maximum=100, late_minimum=200, late_maximum=200)
    options = settings(enemy_health=health)
    plan = build_native_recipes(options, {'sequence': ['base']}, seed=7, catalog=source)
    for recipe in plan['maps']['base']['recipes']:
        expected = 100 if recipe['id'].endswith('/0') else 200
        assert {body['attributes']['health_percent'] for body in recipe['bodies']} == {expected}
    flat = build_native_recipes(settings(progressive_difficulty=False, enemy_health=health),
        {'sequence': ['base']}, seed=7, catalog=source)
    assert len({body['attributes']['health_percent'] for recipe in flat['maps']['base']['recipes']
                for body in recipe['bodies']}) == 1


def test_workload_does_not_sum_alternative_scripts_or_unbounded_refills():
    source = catalog()
    origin = source['maps']['base']['origins'][0]
    source['maps']['base']['origins'] = [
        {**origin, 'id': f'arena/{script}/{event}', 'wave': f'arena/{script}/0',
         'maximum_emissions': maximum, 'binding': {'mode': 'event', 'entity': 'arena',
             'path': f'encounterComponent.entityEvents.item[{script}].events.item[{event}].eventCall'}}
        for script in (0, 1) for event, maximum in ((0, 9999), (1, 0))]
    plan = build_native_recipes(settings(), {'sequence': ['base']}, seed=7, catalog=source)
    profile = plan['combat_costs']['base']['campaign']
    assert profile['finite_spawns'] == 9999
    assert profile['estimated_kills'] == 8


@pytest.mark.parametrize('original', ['shotgun_soldier', 'chaingun_soldier', 'stone_imp', 'zombie_maykr'])
def test_special_fodder_ammunition_sources_keep_ordinary_chainsaw_targets(original):
    source = catalog()
    spawn_type = 'ENCOUNTER_SPAWN_' + original.upper()
    source['species'][original] = {**source['species']['stone_imp'], 'spawn_type': spawn_type}
    refill = source['maps']['base']['origins'][0]
    refill.update(maximum_emissions=0, logical_types=[spawn_type], spawn_type=spawn_type)
    options = settings(enemy_weights={name: int(name in {'imp', original}) for name in SPECIES_IDS})
    plan = build_native_recipes(options, {'sequence': ['base']}, seed=7, catalog=source)
    assert {body['species'] for recipe in plan['maps']['base']['recipes'] for body in recipe['bodies']} == {'imp'}


def test_reduction_rounds_native_phase_once_without_forcing_zero_weight_suppliers():
    source = catalog()
    first = source["maps"]["base"]["origins"][0]
    first.update(logical_count=1, maximum_emissions=1, combat_cost="1", phase="arena")
    source["maps"]["base"]["origins"].append({**first, "id": "base/source/1"})
    options = settings(enemy_count_mode="multiplier", enemy_count_multiplier=50,
        enemy_weights={name: int(name == "stone_imp") for name in SPECIES_IDS})
    plan = build_native_recipes(options, {"sequence": ["base"]}, seed=7, catalog=source)
    recipes = plan["maps"]["base"]["recipes"]
    assert sorted(row["physical_count"] for row in recipes) == [0, 1]
    assert all(body["species"] == "stone_imp" for row in recipes for body in row["bodies"])


def test_auto_keeps_every_request_and_zero_variety_even_when_only_expensive_species_are_allowed():
    source = catalog()
    first = source["maps"]["base"]["origins"][0]
    first.update(logical_count=1, maximum_emissions=1, combat_cost="1", phase="arena")
    source["maps"]["base"]["origins"].append({**first, "id": "base/source/1"})
    options = settings(enemy_variety=0, enemy_weights={name: int(name == "stone_imp") for name in SPECIES_IDS})
    plan = build_native_recipes(options, {"sequence": ["base"]}, seed=7, catalog=source)
    recipes = plan["maps"]["base"]["recipes"]
    assert [row["physical_count"] for row in recipes] == [1, 1]
    assert {body["species"] for row in recipes for body in row["bodies"]} == {"stone_imp"}


def test_auto_density_preserves_requests_and_frozen_revisions():
    source = catalog()
    first = source['maps']['base']['origins'][0]
    first.update(logical_count=3, maximum_emissions=6, phase='arena')
    source['maps']['base']['origins'].append({**first, 'id': 'base/later', 'logical_count': 1, 'maximum_emissions': 1})
    options = settings(enemy_density=150, enemy_weights={name: int(name == 'stone_imp') for name in SPECIES_IDS})
    plan = build_native_recipes(options, {'sequence': ['base']}, seed=7, catalog=source)
    recipes = plan['maps']['base']['recipes']
    assert all(r['physical_count'] >= r['logical_count'] for r in recipes)
    assert sum(r['physical_count'] for r in recipes) == 6
    assert recipes[0]['maximum_emissions'] == recipes[0]['physical_count'] * 2
    assert recipes[1]['maximum_emissions'] == recipes[1]['physical_count']
    frozen = build_native_recipes(options, {'sequence': ['base']}, seed=7, catalog=source, revision=7)
    assert [r['physical_count'] for r in frozen['maps']['base']['recipes']] == [5, 2]
    assert validate_native_recipes(frozen, options, catalog=source) == frozen
    legacy = build_native_recipes(options, {'sequence': ['base']}, seed=7, catalog=source, revision=1)
    assert validate_native_recipes(legacy, options, catalog=source) == legacy
    assert legacy['fingerprint'] != plan['fingerprint']
    invalid = deepcopy(plan)
    invalid['revision'] = 999
    with pytest.raises(ValueError, match='revision'):
        validate_native_recipes(invalid, options, catalog=source)


def test_finite_maintenance_last_body_has_only_the_remaining_logical_credit():
    source = catalog()
    source["maps"]["base"]["origins"][0]["maximum_emissions"] = 3
    options = settings(enemy_count_mode="multiplier", enemy_count_multiplier=150,
        enemy_weights={name: int(name == "imp") for name in SPECIES_IDS})
    plan = build_native_recipes(options, {"sequence": ["base"]}, seed=7, catalog=source)
    recipe, = plan["maps"]["base"]["recipes"]
    assert recipe["physical_count"] == 3 and recipe["maximum_emissions"] == 5
    assert recipe["final_credit_units"] == 1
    assert (4 * recipe["logical_count"] + recipe["final_credit_units"]) / recipe["physical_count"] == 3


def test_spirit_call_metadata_preserves_native_host_filters_and_flags():
    from doom_eap.content.native_enemy_plan import native_recipe_metadata

    source = catalog()
    origin = source["maps"]["base"]["origins"][0]
    origin.update(family="spirit", binding={"mode": "event", "entity": "arena", "owner": "arena", "reference": "point",
        "event_def": "spawnSpirit", "arguments": [{"entity": "point"},
            {"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"}, {"string": "host | filter"}, {"bool": True}]})
    plan = build_native_recipes(settings(), {"sequence": ["base"]}, seed=8, catalog=source)
    assert native_recipe_metadata(plan, "base").splitlines()[1].split("|")[9:14] == [
        "", "686f7374207c2066696c746572", "", "00000fffffffffff", "01"]


def test_extracted_campaign_origins_allow_cross_campaign_species_with_no_supply_weight_override():
    from importlib.resources import files
    import json

    sources = json.loads(files("doom_eap.content").joinpath("native_enemy_catalog.json").read_bytes())
    options = settings(enemy_count_mode="multiplier", enemy_count_multiplier=100,
        enemy_weights={name: int(name == "stone_imp") for name in SPECIES_IDS})
    campaign = {"sequence": sorted(sources["maps"])}
    plan = build_native_recipes(options, campaign, seed=73819, catalog=sources, revision=3)
    for map_key, source in sources["maps"].items():
        expected = {row["id"] for row in source["origins"]
                    if not row.get("protected_reason") and not row.get("inactive_reason") and not row.get('legacy_protected_reason')}
        recipes = plan["maps"][map_key]["recipes"]
        assert {row["id"] for row in recipes if not row["runtime_only"]} == expected
        assert {row["id"] for row in recipes if row["runtime_only"]} == {row["id"] for row in source["runtime_origins"]}
        assert all(row["physical_count"] == row["logical_count"] for row in recipes if not row["runtime_only"])
        assert all(row["quantity_ratio"] == [100, 100] and 0 < row["physical_count"] <= len(sources["species"])
                   for row in recipes if row["runtime_only"])
        assert all(body["species"] == "stone_imp" and body["ai"]["map_key"] == "e5m1_spear"
                   for row in recipes for body in row["bodies"])
