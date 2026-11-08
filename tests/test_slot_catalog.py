import json
import shutil
from copy import deepcopy

import pytest

from doom_eap.content.recipe_codec import recipe_references
from doom_eap.content.slot_data_contract import (
    CATALOG_DEPENDENCIES,
    DigestError,
    SlotCatalog,
    canonical_bytes,
    catalog_dependency_path,
    catalog_digest,
    catalog_tables,
    installed_catalog_tables,
    load_slot_catalog,
)
from tools.content.compile_content_catalog import MOD_ROOT
from tools.validation.audit_packaged_transition_bridge import (
    assert_generation_only_apworld,
)


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


@pytest.mark.parametrize('name', CATALOG_DEPENDENCIES)
def test_loader_detects_dependency_changed_after_generation(tmp_path, name):
    path = copy_catalog(tmp_path / 'content')
    dependency = path.with_name(name)
    dependency.write_bytes(dependency.read_bytes() + b' ')
    with pytest.raises(DigestError, match=f'dependency digest mismatch: {name}'):
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
