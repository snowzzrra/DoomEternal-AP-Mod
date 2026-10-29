import json
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zipfile import ZipFile

import pytest

from doom_eap.content import enemy_plan, native_enemy_plan, native_enemy_safety
from doom_eap.content.enemy_options import normalize_enemy_options
from doom_eap.content.recipe_codec import decode_recipes, encode_recipes
from doom_eap.content.slot_data_contract import (
    LimitSet, Population, catalog_tables, load_slot_catalog, validate_slot_data,
)
from doom_eap.launcher.launcher_core import RoomCompiler, RoomSnapshot, SeedManifest
from doom_eap.runtime import context_registry as registry
from tests.test_cross_repo_acceptance import (
    COMPACT, ENEMIES, ap_source, compiler, generated, scratch, without_authoring_maps,
)
from tests.test_slot_data_contract import sample_slot, sign_slot
from tools.maps.native_recipe_materializer import (
    RecipeMaterializationError, expand_resolved_native_recipes, materialize_resolved_timers,
)

ROOT = Path(__file__).resolve().parents[1]
KEY = 'e1m3_cult'


def installed_slot(catalog, recipes, stages):
    tables = catalog_tables(catalog)
    data = sample_slot(catalog, recipes)
    profile = data['stage_profiles'].pop('a')
    profile.update(counter_ids=[], opening_counter_ids=[])
    data['stage_profiles'] = {key: deepcopy(profile) for key in stages}
    data['campaign'].update(sequence=stages, starting_stages=stages[:1],
        access_items=sorted(tables['stages'][key]['access_id'] for key in stages))
    data['campaign']['goal']['stage_id'] = stages[-1]
    data['campaign']['fortress'].update(active_spend_group_ids=[], phase_thresholds=[0, 1])
    data['campaign']['fortress']['battery_economy'].update(cost=0, surplus=0, total=0)
    data['runtime']['mastery_requirement_counts'] = {}
    return sign_slot(data, catalog)


@pytest.fixture(scope='module')
def resolved_input():
    catalog = load_slot_catalog()
    settings = normalize_enemy_options(dict(enemies={'revision': 5}, enemy_count_mode='multiplier', enemy_count_multiplier=150))
    plan = native_enemy_plan.build_native_recipes(settings, {'sequence': [KEY]}, seed=73822)
    recipes = native_enemy_plan.encode_native_recipes(plan, catalog, active_maps=[KEY, 'hub'])
    data = installed_slot(catalog, recipes, [KEY])
    return data, catalog


def forbid_selection(monkeypatch):
    forbidden = Mock(side_effect=AssertionError('Compilation must use resolved decisions.'))
    monkeypatch.setattr(native_enemy_plan, 'build_native_recipes', forbidden)
    monkeypatch.setattr(enemy_plan, '_random_for', forbidden)
    monkeypatch.setattr(enemy_plan, 'sample_species', forbidden)
    monkeypatch.setattr(native_enemy_safety, 'resolve_native_stage', forbidden)
    return forbidden


def rewrite_bodies(data, catalog, transform):
    slot = validate_slot_data(data, catalog,
        supported_capabilities=frozenset(data['compatibility']['required_capabilities']))
    resolved = decode_recipes(slot.recipes)
    maps = {key: replace(stage, rows=tuple(replace(row, bodies=tuple(map(transform, row.bodies)))
            for row in stage.rows)) for key, stage in resolved.maps.items()}
    return sign_slot({**deepcopy(data), 'recipes': encode_recipes(replace(resolved, maps=maps))}, catalog)


def test_quotas_and_tail_credit_match_the_resolved_rows(resolved_input, monkeypatch):
    data, catalog = resolved_input
    slot = validate_slot_data(data, catalog)
    forbid_selection(monkeypatch)
    plan = expand_resolved_native_recipes(slot, catalog)
    rows = decode_recipes(slot.recipes).maps[KEY].rows
    expanded = {row['id']: row for row in plan['maps'][KEY]['recipes']}
    partials = []
    for row in rows:
        if row.role_ref != 'primary':
            continue
        recipe = expanded[catalog_tables(catalog)['sources'][row.source_ref]['source_id']]
        assert_recipe_quantities(recipe, row)
        assert recipe['quantity_ratio'] == [row.ratio_numerator, row.ratio_denominator]
        assert len(recipe['bodies']) == len(row.bodies)
        if 0 < row.final_credit_units < row.logical_count:
            body = recipe['bodies'][(row.maximum_emissions - 1) % len(row.bodies)]
            partials.append(body['tail_definition'])
            assert f'_w{row.final_credit_units:08x}' in body['tail_definition']
    assert partials


def assert_recipe_quantities(recipe, row):
    assert [recipe[key] for key in ('logical_count', 'maximum_emissions', 'final_credit_units')] == [
        row.logical_count, row.maximum_emissions, row.final_credit_units]


def with_body_limits(resolved_input):
    data, catalog = resolved_input
    slot = validate_slot_data(data, catalog)
    resolved = decode_recipes(slot.recipes)
    stage = resolved.maps[KEY]
    row = next(row for row in stage.rows if row.role_ref == 'primary' and len(row.bodies) > 1)
    from doom_eap.content.enemy_plan import LIMITED_SPECIES

    limits = LimitSet((1, 2, 3, 4), Population('000000000000007b', 8, 3, 2, 8, 0, True, None,
        tuple((name, 2) for name in sorted(LIMITED_SPECIES))))
    bodies = (replace(row.bodies[0], limits=limits), *row.bodies[1:])
    changed = replace(row, bodies=bodies)
    rows = tuple(changed if candidate is row else candidate for candidate in stage.rows)
    recipes = encode_recipes(replace(resolved, maps={**resolved.maps, KEY: replace(stage, rows=rows)}))
    data = sign_slot({**deepcopy(data), 'recipes': recipes}, catalog)
    return data, catalog, row, limits


def test_each_body_keeps_its_own_limit_set(resolved_input):
    data, catalog, row, limits = with_body_limits(resolved_input)
    plan = expand_resolved_native_recipes(validate_slot_data(data, catalog), catalog)
    recipe = next(recipe for recipe in plan['maps'][KEY]['recipes']
                  if recipe['id'] == catalog_tables(catalog)['sources'][row.source_ref]['source_id'])
    first, second = recipe['bodies'][:2]
    assert first['special_limits'] == dict(marauder=1, archvile=2, spirit=3, combined_special=4)
    assert first['population_limits'] == dict(scope=123, maximum=8, heavy=3, superheavy=2, pressure=8,
        mixed=0, strict=True, counter=None, species=dict(limits.population.species_caps))
    assert first['definition'] != second['definition']
    assert second['special_limits'] == dict(zip(('marauder', 'archvile', 'spirit', 'combined_special'), row.bodies[1].limits.special))


@pytest.mark.parametrize('enabled', [True, False])
def test_timers_are_the_frozen_values(resolved_input, enabled, monkeypatch):
    original, catalog = resolved_input
    data = deepcopy(original)
    if not enabled:
        data['recipes'] = native_enemy_plan.encode_native_recipes(None, catalog, active_maps=[KEY, 'hub'])
    data['recipes']['timers'] = [[ref, 12345 + index] for index, (ref, _) in enumerate(data['recipes']['timers'])]
    slot = validate_slot_data(sign_slot(data, catalog), catalog)
    forbid_selection(monkeypatch)
    tables = catalog_tables(catalog)
    source = '\n'.join('entity { entityDef ' + tables['timers'][ref]['entity'] +
        ' { inherit = "info/challenge"; class = "idInfo"; edit = {} } }' for ref, _ in slot.recipes.timers)
    member = 'map_patch/maps/' + tables['maps'][KEY]['runtime_map'] + '.entities'
    output = materialize_resolved_timers(source, member, slot, catalog)
    for _, duration in slot.recipes.timers:
        assert f'challengeDuration = {duration};' in output
    assert materialize_resolved_timers(output, member, slot, catalog) == output


@pytest.mark.parametrize('invalid', ['attribute', 'quota', 'reference', 'population'])
def test_unusable_recipe_refuses_without_substitution(resolved_input, invalid):
    original, catalog = resolved_input
    data = deepcopy(original)
    slot = validate_slot_data(data, catalog)
    if invalid == 'reference':
        tables = deepcopy(catalog_tables(catalog))
        tables['templates'] = {}
        code = 'recipe_reference'
    else:
        tables = catalog
        row = next(row for row in data['recipes']['maps'][KEY]['rows'] if row[1] == 'primary' and row[5])
        if invalid == 'attribute':
            data = rewrite_bodies(data, catalog, lambda body: replace(body, health_percent=10001))
            code = 'recipe_unsafe'
        elif invalid == 'population':
            population = Population('000000000000007b', 0x1000000, 0x1000000, 0, 0, 0, False, None, ())
            data = rewrite_bodies(data, catalog,
                lambda body: replace(body, limits=replace(body.limits, population=population)))
            code = 'recipe_unsafe'
        else:
            row[5] += row[4] * sum(count for _, count in data['recipes']['patterns'][row[3]])
            code = 'recipe_quota'
        slot = validate_slot_data(sign_slot(data, catalog), catalog)
    with pytest.raises(RecipeMaterializationError) as caught:
        expand_resolved_native_recipes(slot, tables)
    assert caught.value.code == code


def activate_schema(monkeypatch, data):
    identity = registry._SLOT_IDENTITY
    monkeypatch.setitem(identity, 'slot_data_schema_version', 7)
    data['compatibility'].update(apworld_version=identity['apworld_revision'],
        minimum_client_version=identity['release_version'], bridge_protocol=identity['bridge_protocol_version'],
        manifest_schema_version=identity['manifest_schema_version'], mod_contract_revision=identity['session_mod_contract_revision'])


def test_resolved_mastery_count_is_written_without_scaling(resolved_input, monkeypatch):
    _, catalog = resolved_input
    identity, entry = next(iter(catalog_tables(catalog)['masteries'].items()))
    member = 'gameresources/generated/decls/' + entry['decls']['unlockable']['path']
    original = entry['requirement']['original_count']
    source = ('{ edit = { rules = { item[0] = { stat = "' + entry['signal']['rule_0_statname'] +
              '"; statDuration = "DUR_CUSTOM"; count = ' + str(original) + '; } } } }').encode()
    compiler = RoomCompiler.__new__(RoomCompiler)
    compiler.base_resource, compiler.payload_resource, compiler.payload_manifest = None, None, None
    monkeypatch.setitem(registry._SLOT_IDENTITY, 'slot_data_schema_version', 7)
    monkeypatch.setattr('tools.release.room_payloads.assemble_room_files', lambda *args: ({member: source}, {}))
    manifest = SimpleNamespace(options=dict(runtime=dict(mastery_requirement_counts={identity: 7})))
    assembled, _ = compiler._assemble_room_files(manifest, {})
    assert b'count = 7;' in assembled[member]


def generated_resolved_slot(document):
    return deepcopy(document['slots'][0]['connected']['slot_data']), load_slot_catalog()


@pytest.mark.apworld
@pytest.mark.integration
@pytest.mark.parametrize('case_id', [COMPACT, ENEMIES])
def test_schema_seven_room_compiles_from_installed_catalogs_without_authoring_maps(
        generated, compiler, scratch, monkeypatch, case_id, record_property):
    document = generated(case_id)
    data, catalog = generated_resolved_slot(document)
    activate_schema(monkeypatch, data)
    sign_slot(data, catalog)
    connected = {**document['slots'][0]['connected'], 'slot_data': data}
    snapshot = RoomSnapshot.from_packets(document['room_info'], connected)
    known = set(document['slots'][0]['world']['known_ids'])
    manifest = SeedManifest.from_room(snapshot, known, static_content_digest=compiler.static_content_digest)
    forbid_selection(monkeypatch)
    started = time.perf_counter()
    with without_authoring_maps():
        package = compiler.build(manifest, scratch / ('resolved-' + case_id))
    record_property('compile_seconds', time.perf_counter() - started)
    with ZipFile(package) as archive:
        assert json.loads(archive.read('seed_manifest.json'))['options'] == data
        assert 'enemy_safety_adjustments.json' not in archive.namelist()
        assert_frozen_package_timers(archive, compiler, data, catalog)
    before = package.read_bytes()
    broken = deepcopy(data)
    if broken['recipes']['profiles']:
        broken = rewrite_bodies(broken, catalog, lambda body: replace(body, health_percent=10001))
        with pytest.raises(RecipeMaterializationError) as caught:
            compiler.build(replace(manifest, options=broken), package.parent, force=True)
        assert caught.value.code == 'recipe_unsafe'
        assert package.read_bytes() == before
        with pytest.raises(RecipeMaterializationError):
            compiler.build(replace(manifest, options=broken), package.parent)


def assert_frozen_package_timers(archive, compiler, data, catalog):
    from tools.maps.ap_map_generator import find_entity_block_bounds

    tables = catalog_tables(catalog)
    texts = {}
    for ref, duration in data['recipes']['timers']:
        timer = tables['timers'][ref]
        member = compiler.payload_manifest['maps'][timer['stage_id']]['target_member']
        if member not in texts:
            texts[member] = compiler._decompress_entities_text(archive.read(member), member)
        text = texts[member]
        bounds = find_entity_block_bounds(text, timer['entity'])
        assert bounds is not None
        assert f'challengeDuration = {duration};' in text[slice(*bounds)]
