from copy import deepcopy
from importlib.resources import files
import json
from pathlib import Path
import pytest

from doom_eap.content.enemy_options import SPECIES_IDS, normalize_enemy_options
from doom_eap.content.native_enemy_plan import build_native_recipes, native_recipe_metadata, native_recipe_fingerprint
from doom_eap.content.native_enemy_safety import FLYING_REPLACEMENTS, project_native_safety, _replace_species
from doom_eap.content.enemy_species import enemy_speed_capabilities


@pytest.mark.parametrize('preset', ['slayers_path', 'oops_all_imps'])
def test_mission_challenges_keep_finite_native_species_on_main_routes(preset):
    from doom_eap.content.vanilla_enemy_profiles import load_vanilla_profiles

    catalog = json.loads(files('doom_eap.content').joinpath('native_enemy_catalog.json').read_bytes())
    before = deepcopy(catalog)
    targets = json.loads(files('doom_eap.content').joinpath('challenge_enemy_targets.json').read_bytes())
    targets = [target for target in targets if target['map_key'] is not None]
    sequence = sorted({target['map_key'] for target in targets})
    options = normalize_enemy_options({'enemy_preset': preset})
    plan = build_native_recipes(options, {'sequence': sequence}, seed=712, catalog=catalog)
    for target in targets:
        stage = plan['maps'][target['map_key']]
        preserved = {row['id'] for row in stage['protected'] if row['reason'] == 'challenge enemy supply'}
        profile = load_vanilla_profiles()['maps'][target['map_key']]
        supplies = [slot for encounter in profile['encounters']
                    if encounter['id'] in profile['primary_encounters']
                    and encounter.get('combat_kind') not in {'secret_encounter', 'slayer_gate_encounter'}
                    for slot in encounter['slots'] if slot['source_id'] in preserved
                    and set(slot['species']) <= set(target['species'])]
        assert sum(slot['count'] for slot in supplies if not slot['renewable']) >= target['minimum']
        assert not preserved.intersection(recipe['id'] for recipe in stage['recipes'])
    assert catalog == before


def test_required_archvile_preservation_keeps_frozen_room_recipes_reconstructible():
    from doom_eap.content.native_enemy_plan import native_recipe_seed, validate_native_recipes

    key = 'e3m2_hell_b'
    campaign = {'sequence': [key]}
    options = normalize_enemy_options({'enemy_preset': 'oops_all_imps'})
    old = build_native_recipes(options, campaign, seed=712, revision=19)
    current = build_native_recipes(options, campaign, seed=712)
    origin = key + '/ai_encounter_manager_demonpit_01a/encounterComponent.entityEvents.item[0].events.item[13].eventCall'
    old_recipe = next(recipe for recipe in old['maps'][key]['recipes'] if recipe['id'] == origin)
    assert old_recipe['bodies'] and all(body['species'] != 'archvile' for body in old_recipe['bodies'])
    assert {'id': origin, 'reason': 'challenge enemy supply'} in current['maps'][key]['protected']
    for plan in (old, current):
        assert validate_native_recipes(native_recipe_seed(plan), options) == plan


def frozen_plan(map_key, species, source_types):
    catalog = json.loads(files('doom_eap.content').joinpath('native_enemy_catalog.json').read_bytes())
    stage = catalog['maps'][map_key]
    stage['origins'] = []
    stage['runtime_origins'] = [row for row in stage['runtime_origins'] if row['spawn_type'] in source_types]
    catalog['maps'] = {map_key: stage}
    settings = normalize_enemy_options({'enemy_weights': {name: int(name == 'imp') for name in SPECIES_IDS},
                                       'enemy_count_mode': 'multiplier', 'enemy_count_multiplier': 100})
    plan = build_native_recipes(settings, {'sequence': [map_key]}, seed=712, catalog=catalog, revision=3)
    if species != 'imp':
        for recipe in plan['maps'][map_key]['recipes']:
            for ordinal, body in enumerate(recipe['bodies']):
                _replace_species(body, species, recipe, ordinal,
                    {**settings, 'special_limits': plan['maps'][map_key]['special_limits']}, catalog, enemy_speed_capabilities())
        plan['fingerprint'] = native_recipe_fingerprint(plan)
    return plan, settings


def test_holt_airborne_summons_keep_flying_bodies_and_checkpoint_compatibility():
    plan, settings = frozen_plan('e4m3_mcity', 'imp', {
        'ENCOUNTER_SPAWN_CACODEMON', 'ENCOUNTER_SPAWN_PAIN_ELEMENTAL', 'ENCOUNTER_SPAWN_IMP'})
    before = deepcopy(plan)
    projected = project_native_safety(plan, settings)
    stage = projected['maps']['e4m3_mcity']
    for recipe in stage['recipes']:
        expected = {'imp'} if recipe['selected_spawn_type'] == 'ENCOUNTER_SPAWN_IMP' else FLYING_REPLACEMENTS
        assert {body['species'] for body in recipe['bodies']} <= expected
    assert len(stage['compatibility_recipes']) == 2
    assert all({body['species'] for body in recipe['bodies']} == {'imp'}
               for recipe in stage['compatibility_recipes'])
    original_metadata = native_recipe_metadata(plan, 'e4m3_mcity').splitlines()
    projected_metadata = native_recipe_metadata(projected, 'e4m3_mcity').splitlines()
    assert original_metadata[0] == projected_metadata[0]
    assert [row.split('|')[:14] for row in original_metadata[1:]] == [row.split('|')[:14] for row in projected_metadata[1:]]
    assert plan == before


@pytest.fixture
def atlantica_mixed_plan():
    key = 'e4m1_rig'
    references = {'traversal_section_target_spawn_127', 'destroyed_rig_target_spawn_215',
                  'traversal_section_target_spawn_151'}
    catalog = json.loads(files('doom_eap.content').joinpath('native_enemy_catalog.json').read_bytes())
    stage = catalog['maps'][key]
    stage['origins'] = [{**row, 'logical_count': 2, 'maximum_emissions': 3} for row in stage['origins']
                        if row.get('binding', {}).get('reference') in references]
    stage['runtime_origins'] = []
    catalog['maps'] = {key: stage}
    settings = normalize_enemy_options({'enemy_weights': {name: int(name == 'imp') for name in SPECIES_IDS},
                                       'enemy_count_mode': 'multiplier', 'enemy_count_multiplier': 150})
    plan = build_native_recipes(settings, {'sequence': [key]}, seed=712, catalog=catalog, revision=3)
    limits = {**settings, 'special_limits': plan['maps'][key]['special_limits']}
    for recipe in plan['maps'][key]['recipes']:
        for ordinal, (body, species) in enumerate(zip(recipe['bodies'], ('imp', 'spirit', 'cacodemon'))):
            _replace_species(body, species, recipe, ordinal, limits, catalog, enemy_speed_capabilities())
    plan['fingerprint'] = native_recipe_fingerprint(plan)
    return plan, settings


def test_atlantica_spirit_denylist_preserves_other_bodies_and_frozen_identity(atlantica_mixed_plan):
    plan, settings = atlantica_mixed_plan
    before = deepcopy(plan)
    projected = project_native_safety(plan, settings)
    stage = projected['maps']['e4m1_rig']
    denied = [recipe for recipe in before['maps']['e4m1_rig']['recipes']
              if recipe['binding']['reference'] != 'traversal_section_target_spawn_151']
    assert len(stage['recipes']) == 3
    assert stage['compatibility_recipes'] == denied
    for original, recipe in zip(before['maps']['e4m1_rig']['recipes'], stage['recipes']):
        assert {key: value for key, value in recipe.items() if key != 'bodies'} == {
            key: value for key, value in original.items() if key != 'bodies'}
        assert (recipe['logical_count'], recipe['physical_count'], recipe['maximum_emissions'],
                recipe['final_credit_units']) == (2, 3, 5, 1)
        assert recipe['bodies'][0] == original['bodies'][0]
        assert recipe['bodies'][2] == original['bodies'][2]
        body = recipe['bodies'][1]
        if original in denied:
            assert body['species'] == 'cacodemon'
            assert body['attributes'] == original['bodies'][1]['attributes']
            assert body['definition'] != original['bodies'][1]['definition']
            assert body['tail_definition'] != original['bodies'][1]['tail_definition']
        else:
            assert recipe == original
    assert [row.split('|')[:14] for row in native_recipe_metadata(plan, 'e4m1_rig').splitlines()] == [
        row.split('|')[:14] for row in native_recipe_metadata(projected, 'e4m1_rig').splitlines()]
    assert projected['source_fingerprint'] == before['fingerprint']
    assert projected['fingerprint'] == native_recipe_fingerprint(projected) != before['fingerprint']
    assert [change for change in projected['compiler_adjustments'] if change['action'] == 'spirit_spawn_fallback'] == [
        {'origin': recipe['id'], 'action': 'spirit_spawn_fallback', 'before': 'spirit', 'after': 'cacodemon'}
        for recipe in denied]
    assert plan == before
    other_map = deepcopy(plan)
    other_map['maps'] = {'e4m2_swamp': other_map['maps']['e4m1_rig']}
    other_map['fingerprint'] = native_recipe_fingerprint(other_map)
    other_stage = project_native_safety(other_map, settings)['maps']['e4m2_swamp']
    assert other_stage['recipes'] == before['maps']['e4m1_rig']['recipes']
    assert 'compatibility_recipes' not in other_stage


def test_atlantica_projected_and_checkpoint_bodies_reach_room_package(atlantica_mixed_plan, monkeypatch):
    from doom_eap.launcher import launcher_core
    from tools.maps.ap_map_generator import find_entity_block_bounds
    from tools.maps.encounter_catalog import entity_records, list_items
    from tools.release.prebuilt_room_resources import get_frozen_bundle_dir

    root = Path(__file__).resolve().parents[1]
    plan, settings = atlantica_mixed_plan
    before = deepcopy(plan)
    projected = project_native_safety(plan, settings)
    stage = projected['maps']['e4m1_rig']
    compiler = launcher_core.RoomCompiler.__new__(launcher_core.RoomCompiler)
    resources = get_frozen_bundle_dir(root)
    compiler.payload_resource = resources / 'room_payloads.zip'
    compiler.payload_manifest = json.loads((resources / 'room_payload_manifest.json').read_bytes())
    member = compiler.payload_manifest['maps']['e4m1_rig']['target_member']
    compiler._compress_entities_text = lambda text: text.encode()
    # handcrafted bodies bypass planner reconstruction for this compiler check.
    monkeypatch.setattr(launcher_core, 'validate_frozen_enemy_plan', lambda *_: None)
    manifest = launcher_core.SeedManifest.create(seed_name='Safety', team=0, slot=1,
        active_location_ids=launcher_core.ModCompiler(root).known_location_ids(),
        options={'enemy_plan': plan, 'enemies': settings, 'use_dlc_content': True}, static_precompile=True)
    assembled = {member: b'previous compiled content'}
    roots = compiler._apply_enemy_plan(assembled, manifest)
    text = assembled[member].decode()
    for recipe in stage['recipes']:
        start, end = find_entity_block_bounds(text, 'ap_v070_bodies_r' + recipe['origin'])
        library, = entity_records(text[start:end])
        assert [row['name'] for row in list_items(library['edit']['entityDefs'])] == [
            body[field] for body in recipe['bodies'] for field in ('definition', 'tail_definition') if field in body]
    for recipe in [*stage['recipes'], *stage['compatibility_recipes'], *stage['counter_recipes']]:
        for body in recipe['bodies']:
            for field in ('definition', 'tail_definition'):
                if field in body:
                    start, end = find_entity_block_bounds(text, body[field])
                    record, = entity_records(text[start:end])
                    assert record['class'] == 'idAI2'
    profiles = json.loads((root / 'content/catalog/native_enemy_assets.json').read_bytes())['bodies']
    required = {(asset['mapResourceType'], asset['name'])
                for recipe in [*stage['recipes'], *stage['compatibility_recipes']]
                for body in recipe['bodies'] for asset in profiles[body['ai']['sha256']]['assets']}
    assets = json.loads(assembled[member.split('/')[0] + '/EternalMod/assetsinfo/e4m1_rig.json'])['assets']
    assert required <= {(asset['mapResourceType'], asset['name']) for asset in assets}
    assert {name for _, name in required} <= set(roots[stage['runtime_map']])
    assert json.loads(assembled['enemy_safety_adjustments.json']) == projected['compiler_adjustments']
    assert plan == before


def test_spirit_counter_uses_its_reserved_population_without_relaxing_primary_bodies():
    from doom_eap.content.enemy_plan import LIMITED_SPECIES

    plan, settings = frozen_plan('e3m1_slayer', 'spirit', {'ENCOUNTER_SPAWN_IMP'})
    recipe, = plan['maps']['e3m1_slayer']['recipes']
    body, = recipe['bodies']
    caps = dict.fromkeys(LIMITED_SPECIES, 0)
    caps['spirit'] = 2
    body['population_limits'] = dict(maximum=3, pressure=3, heavy=0, superheavy=0,
                                    mixed=True, strict=True, scope=123, species=caps)
    before = deepcopy(plan)
    projected = project_native_safety(plan, settings)['maps']['e3m1_slayer']
    original, = projected['recipes']
    alternate, = projected['counter_recipes']
    assert original['bodies'][0]['population_limits']['heavy'] == 0
    assert alternate['bodies'][0]['population_limits']['heavy'] == 2
    assert alternate['bodies'][0]['population_limits']['counter'] == 'spirit'
    for field in ('maximum', 'pressure', 'superheavy', 'scope', 'species'):
        assert alternate['bodies'][0]['population_limits'][field] == body['population_limits'][field]
    assert plan == before


@pytest.mark.parametrize('source,replacement', [('stone_imp', 'imp'), ('armored_baron', 'baron')])
def test_inventory_counter_keeps_quantity_and_credit(source, replacement):
    plan, settings = frozen_plan('e5m1_spear', source, {'ENCOUNTER_SPAWN_IMP'})
    before = deepcopy(plan)
    stage = project_native_safety(plan, settings)['maps']['e5m1_spear']
    original, = stage['recipes']
    alternate, = stage['counter_recipes']
    assert {body['species'] for body in alternate['bodies']} == {replacement}
    for field in ('logical_count', 'physical_count', 'maximum_emissions', 'final_credit_units', 'binding'):
        assert alternate[field] == original[field]
    assert plan == before
