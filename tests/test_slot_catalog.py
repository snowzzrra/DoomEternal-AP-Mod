from copy import deepcopy
import json
import shutil

import pytest

from doom_eap.content.content_catalog import load_content_catalog
from doom_eap.content.recipe_codec import recipe_references
from doom_eap.content.slot_data_contract import (
    CATALOG_DEPENDENCIES, DigestError, ReferenceError, StructureError, SlotCatalog,
    canonical_bytes, catalog_dependency_path, catalog_digest, catalog_tables,
    installed_catalog_tables, load_slot_catalog,
)
from tools.content.compile_content_catalog import MOD_ROOT, compile_slot_catalog
from tools.validation.audit_packaged_transition_bridge import assert_generation_only_apworld, assert_packaged_slot_catalog


def test_catalog_generation_is_byte_stable_and_reordering_preserves_references():
    content = load_content_catalog()
    catalog = load_slot_catalog()
    native = json.loads((MOD_ROOT / 'doom_eap/content/native_enemy_catalog.json').read_bytes())
    kills = json.loads((MOD_ROOT / 'doom_eap/content/campaign_kill_sources.json').read_bytes())
    items = dict(enumerate(catalog['items']))
    first = compile_slot_catalog(content, native, kills, items)
    second = compile_slot_catalog(content, native, kills, items)
    expected = (MOD_ROOT / 'doom_eap/content/slot_catalog.json').read_bytes()
    assert canonical_bytes(first) + b'\n' == canonical_bytes(second) + b'\n' == expected
    native['species'] = dict(reversed(list(native['species'].items())))
    native['maps'] = dict(reversed(list(native['maps'].items())))
    for stage in native['maps'].values():
        stage['origins'].reverse()
        stage['runtime_origins'].reverse()
    for stage in kills['maps'].values():
        stage['sources'].reverse()
    reordered = compile_slot_catalog(content, native, kills, items)
    dependencies = {name: json.loads(catalog_dependency_path(MOD_ROOT / 'doom_eap/content', name).read_bytes())
                    for name in CATALOG_DEPENDENCIES}
    dependencies['native_enemy_catalog.json'] = native
    resolved = SlotCatalog(reordered, installed_catalog_tables(reordered, dependencies))
    assert recipe_references(catalog) == recipe_references(resolved)
    assert canonical_bytes(reordered) + b'\n' == expected


def test_catalog_resolves_native_bindings_assets_and_authored_definitions():
    document = load_slot_catalog()
    catalog = catalog_tables(document)
    assert catalog['maps']['hub']['stage_id'] is None
    assert all(row['ai']['sha256'] in catalog['assets']['bodies'] for row in catalog['templates'].values())
    assert catalog['maps']['e1m1_intro']['origins'][0]['binding']
    assert any('location' in row or 'region' in row for row in catalog['finite_sources'].values())
    assert catalog['masteries'] and catalog['counter_rules'] and catalog['runtime_restrictions']
    assert len(canonical_bytes(document)) < 200_000
    assert not {'maps', 'sources', 'assets', 'templates', 'finite_sources', 'stages'} & document.keys()


def copy_catalog(target):
    target.mkdir(parents=True)
    for name in ('slot_catalog.json', 'projection_manifest.json'):
        shutil.copyfile(MOD_ROOT / 'doom_eap/content' / name, target / name)
    for name in CATALOG_DEPENDENCIES:
        shutil.copyfile(catalog_dependency_path(MOD_ROOT / 'doom_eap/content', name), target / name)
    return target / 'slot_catalog.json'


def test_loader_recomputes_both_manifest_and_room_digests(tmp_path):
    path = copy_catalog(tmp_path / 'content')
    catalog = load_slot_catalog(path=path)
    digest = catalog_digest(catalog)
    assert load_slot_catalog(digest, path=path) == catalog
    with pytest.raises(DigestError, match='Room catalog digest mismatch'):
        load_slot_catalog('0' * 64, path=path)
    catalog['battery_surplus_percent'] += 1
    path.write_bytes(canonical_bytes(catalog))
    with pytest.raises(DigestError, match='Installed catalog digest mismatch'):
        load_slot_catalog(digest, path=path)


def test_packaged_catalog_is_required_and_checked(tmp_path):
    path = copy_catalog(tmp_path / 'doom_eap/content')
    assert_packaged_slot_catalog(tmp_path)
    path.write_text('{}', encoding='ascii')
    with pytest.raises(DigestError):
        assert_packaged_slot_catalog(tmp_path)
    path.unlink()
    with pytest.raises(StructureError, match='Cannot read slot catalog'):
        assert_packaged_slot_catalog(tmp_path)


def test_source_references_include_map_ownership():
    from doom_eap.content.slot_data_contract import source_index

    catalog = deepcopy(catalog_tables(load_slot_catalog()))
    catalog['sources'] = source_index([dict(stage_id=stage, source_id='shared', role='primary') for stage in ('a', 'b')])
    refs = recipe_references(catalog)['sources']
    assert refs['a', 'shared', 'primary'] != refs['b', 'shared', 'primary']


def test_adding_source_and_species_preserves_existing_references():
    document = load_slot_catalog()
    directory = MOD_ROOT / 'doom_eap/content'
    dependencies = {name: json.loads(catalog_dependency_path(directory, name).read_bytes()) for name in CATALOG_DEPENDENCIES}
    native = dependencies['native_enemy_catalog.json']
    native['species']['aaa_new'] = deepcopy(native['species']['imp'])
    dependencies['enemy_species.json']['species']['aaa_new'] = {}
    native['maps']['e1m1_intro']['origins'].insert(0, dict(id='aaa_new', logical_types=['AAA_NEW']))
    kills = dependencies['campaign_kill_sources.json']['maps']['e1m1_intro']['sources']
    kills.insert(0, dict(source_id='aaa_new', count=1))
    timers = next(stage['timers'] for stage in dependencies['secret_encounter_timers.json']['maps'].values()
                  if stage['timers'])
    timers.insert(0, dict(timers[0], entity='aaa_new'))
    grown = SlotCatalog(document, installed_catalog_tables(document, dependencies))
    before, after = recipe_references(document), recipe_references(grown)
    for table, references in before.items():
        assert references.items() <= after[table].items()


def test_source_hash_collision_fails_generation(monkeypatch):
    from doom_eap.content import slot_data_contract

    catalog = load_slot_catalog()
    native = json.loads((MOD_ROOT / 'doom_eap/content/native_enemy_catalog.json').read_bytes())
    kills = json.loads((MOD_ROOT / 'doom_eap/content/campaign_kill_sources.json').read_bytes())
    monkeypatch.setattr(slot_data_contract, 'source_reference', lambda *args: '0' * 16)
    with pytest.raises(ReferenceError, match='Source hash collision'):
        compile_slot_catalog(load_content_catalog(), native, kills, dict(enumerate(catalog['items'])))


@pytest.mark.parametrize('name', CATALOG_DEPENDENCIES)
def test_loader_detects_dependency_changed_after_generation(tmp_path, name):
    path = copy_catalog(tmp_path / 'content')
    dependency = path.with_name(name)
    dependency.write_bytes(dependency.read_bytes() + b' ')
    with pytest.raises(DigestError, match=f'dependency digest mismatch: {name}'):
        load_slot_catalog(path=path)


@pytest.mark.parametrize('name', ['native_enemy_catalog.json', 'native_enemy_assets.json', 'campaign_stage_ids.json'])
def test_loader_requires_installed_dependencies(tmp_path, name):
    path = copy_catalog(tmp_path / 'content')
    path.with_name(name).unlink()
    with pytest.raises(StructureError, match='Cannot read catalog dependency'):
        load_slot_catalog(path=path)


@pytest.mark.parametrize('changed', [False, True])
def test_apworld_archive_dependencies_are_checked(tmp_path, changed):
    from zipfile import ZipFile

    path = copy_catalog(tmp_path / 'content')
    package = tmp_path / 'doometernal.apworld'
    with ZipFile(package, 'w') as archive:
        archive.writestr('doometernal/__init__.py', '')
        for source in path.parent.iterdir():
            if source.name == 'native_enemy_assets.json':
                continue
            payload = source.read_bytes()
            if changed and source.name == 'native_enemy_catalog.json':
                payload += b' '
            archive.writestr('doometernal/' + source.name, payload)
    if changed:
        with pytest.raises(DigestError, match='dependency digest mismatch'):
            assert_generation_only_apworld(package)
    else:
        assert_generation_only_apworld(package)
