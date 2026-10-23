import hashlib
import json
import zipfile

import pytest

from doom_eap.launcher import campaign_resources, launcher_platform
from doom_eap.launcher.native_enemy_resources import prepare_native_resource_banks


TARGET = "game/sp/e1m1_intro/e1m1_intro"
@pytest.fixture
def bank_room(tmp_path, monkeypatch):
    from test_resource_bank import write_bank
    from doom_eap.launcher import resource_bank
    from importlib.resources import files
    catalog = json.loads(files("doom_eap.content").joinpath("native_enemy_catalog.json").read_bytes())["maps"]
    donor = catalog["e5m1_spear"]["runtime_map"]
    base = tmp_path / "base"
    base.mkdir()
    banks = ["shared.resources", "base.resources", "dlc.resources", "dlc.streamdb"]
    spec = {"files": [{"name": name} for name in banks], "maps": [{"name": TARGET}, {"name": donor}],
        "mapFileRefs": [{"file": index, "map": 0} for index in (0, 1)] +
                       [{"file": index, "map": 1} for index in (0, 2, 3)]}
    write_bank(base / banks[0], [('model', 'native', b'native', ())])
    write_bank(base / banks[1], [('model', 'destination', b'destination', ())])
    write_bank(base / banks[2], [('rs_streamfile', 'generated/decls/entitydef/ai/armored.decl', b'{ model = "armor"; }', ()),
                               ('model', 'armor', b'armor', ()), ('staticShadowGeom', 'map shadows', b'unused', ())])
    (base / banks[3]).write_bytes(b'native stream database')
    for name in ('meta.resources', 'idRehash.map'):
        (base / name).write_bytes(b'fixture metadata')
    monkeypatch.setattr(resource_bank, 'read_masks', lambda _: {name: b'\xff' for name in banks[:3]})
    (base / "packagemapspec.json").write_text(json.dumps(spec), encoding="utf-8")
    plan = {"kind": "native_recipes", "maps": {"e1m1_intro": {"runtime_map": TARGET,
        "recipes": [{"bodies": [{"species": "armored_baron", "ai": {"map_key": "e5m1_spear"}}]}]}}}
    return base, spec, plan


ROOTS = {TARGET: ['ai/armored']}


def bank_order(spec, map_index):
    return [spec["files"][ref["file"]]["name"] for ref in sorted(
        (row for row in spec["mapFileRefs"] if row["map"] == map_index), key=lambda row: row["file"], reverse=True)]


@pytest.mark.parametrize('family', ['recipes', 'compatibility_recipes', 'counter_recipes'])
def test_native_donor_closure_keeps_destination_priority_and_restores_between_rooms(bank_room, family):
    base, original, plan = bank_room
    if family != 'recipes':
        stage = plan['maps']['e1m1_intro']
        stage[family] = stage['recipes']
        stage['recipes'] = [{'bodies': [{'species': 'zombie', 'ai': {'map_key': 'e1m1_intro'}}]}]
    updates, report = prepare_native_resource_banks(base, plan, ROOTS)
    projected = json.loads(updates["packagemapspec.json"])
    bank, = report['consolidated']
    assert bank_order(projected, 0) == ["base.resources", "shared.resources", bank]
    assert bank_order(projected, 1) == bank_order(original, 1)
    assert report["maps"][TARGET] == [bank]
    (base / "packagemapspec.json").write_bytes(updates["packagemapspec.json"])
    assert prepare_native_resource_banks(base, plan, ROOTS)[0] == {}
    reordered = {**projected, "mapFileRefs": list(reversed(projected["mapFileRefs"]))}
    (base / "packagemapspec.json").write_text(json.dumps(reordered), encoding="utf-8")
    restored, _ = prepare_native_resource_banks(base, None)
    normalized = json.loads(restored["packagemapspec.json"])
    assert normalized["files"] == original["files"] and normalized["maps"] == original["maps"]
    assert sorted(normalized["mapFileRefs"], key=lambda ref: (ref["map"], ref["file"])) == original["mapFileRefs"]


@pytest.mark.parametrize('existing_import', [False, True])
def test_hub_text_uses_a_compact_bank_with_enemizer_disabled(bank_room, monkeypatch, existing_import):
    from test_resource_bank import write_bank
    from doom_eap.launcher.resource_bank import ResourceIndex
    base, spec, _ = bank_room
    spec['maps'] += [{'name': 'game/hub/hub'}, {'name': 'game/horde/e6m3_mcity_horde/e6m3_mcity_horde'}]
    spec['mapFileRefs'] += [{'file': 1, 'map': 2}, {'file': 2, 'map': 3}]
    write_bank(base / 'dlc.resources', [
        ('rs_streamfile', 'generated/decls/entitydef/gui/text.decl', b'{ model = "editors/models/gui_text.lwo"; }', ()),
        ('model', 'editors/models/gui_text.lwo', b'model', ()),
        ('cswf', 'swf/guientity/generic_text.swf', b'swf', ()),
        ('staticGeom', 'unrelated map', b'geometry', ())])
    if existing_import:
        donor = 'game/dlc2/e5m1_spear/e5m1_spear.resources'
        (base / donor).parent.mkdir(parents=True)
        (base / 'dlc.resources').rename(base / donor)
        spec['files'][2]['name'] = donor
        (base / 'packagemapspec.json.backup').write_text(json.dumps(spec))
        spec['mapFileRefs'].append({'file': 2, 'map': 2})
        monkeypatch.setattr('doom_eap.launcher.resource_bank.read_masks', lambda _: {
            row['name']: b'\xff' for row in spec['files'] if row['name'].endswith('.resources')})
    (base / 'packagemapspec.json').write_text(json.dumps(spec))
    updates, report = prepare_native_resource_banks(base, None, hub_world_text=True)
    projected = json.loads(updates['packagemapspec.json']);bank, = report['consolidated']
    assert bank_order(projected, 2) == ['base.resources', bank]
    index = ResourceIndex(base / bank)
    assert len(index.records) == 3 and all(key[0] != 'staticGeom' for key, _ in index.records)
    assert (base / 'packagemapspec.json').read_text() == json.dumps(spec)


@pytest.mark.parametrize("damage", ["missing", "traversal", "ownership"])
def test_native_resource_preparation_refuses_missing_banks_or_external_changes(bank_room, damage):
    base, spec, plan = bank_room
    if damage == "missing":
        (base / "dlc.streamdb").unlink()
    elif damage == "traversal":
        spec["files"][2]["name"] = "../outside.resources"
        (base / "packagemapspec.json").write_text(json.dumps(spec), encoding="utf-8")
    else:
        updates, _ = prepare_native_resource_banks(base, plan, ROOTS)
        spec = json.loads(updates["packagemapspec.json"])
        spec["mapFileRefs"].pop(0)
        (base / "packagemapspec.json").write_text(json.dumps(spec), encoding="utf-8")
    before = (base / "packagemapspec.json").read_bytes()
    with pytest.raises(ValueError):
        prepare_native_resource_banks(base, plan, ROOTS)
    assert (base / "packagemapspec.json").read_bytes() == before


def test_room_finalizer_installs_and_clears_native_banks_without_species_imports(bank_room, monkeypatch):
    base, original, plan = bank_room
    monkeypatch.setattr(launcher_platform, "detect_doom_processes", lambda: [])
    monkeypatch.setattr(launcher_platform, "publish_file", lambda source, destination, **kwargs: source.replace(destination))
    monkeypatch.setattr('doom_eap.launcher.resource_bank.register_banks', lambda *args: None)
    monkeypatch.setattr('doom_eap.launcher.build_manifest.register_resource_manifest', lambda *args: None)
    monkeypatch.setattr('doom_eap.launcher.resource_bank.obsolete_banks', lambda *args: {})
    package = base.parent / "room.zip"
    for active in (plan, None):
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("seed_manifest.json", json.dumps({"options": {"enemy_plan": active, "enemies": {}}}))
            archive.writestr("seed_receipt.json", json.dumps({'native_resource_roots': ROOTS if active else {}}))
        result = campaign_resources.enable_campaign_resources(base.parent, package)
        assert result["state"] == "enabled"
        assert bool(result["native_enemy_banks"]) == bool(active)
    assert json.loads((base / "packagemapspec.json").read_bytes()) == original


def test_bank_publication_failure_rolls_back_other_resource_metadata(tmp_path, monkeypatch):
    names = ("meta.resources", "idRehash.map", "packagemapspec.json")
    originals = {name: name.encode() for name in names}
    for name, payload in originals.items():
        (tmp_path / name).write_bytes(payload)
    def publish(source, destination, *, operation):
        if destination.name == "packagemapspec.json" and operation == "resource_activation":
            raise OSError("injected bank publication failure")
        source.replace(destination)
    monkeypatch.setattr(launcher_platform, "publish_file", publish)
    report = {"source_hashes": {name: hashlib.sha256(payload).hexdigest() for name, payload in originals.items()}}
    with pytest.raises(OSError, match="bank publication"):
        campaign_resources.publish_resource_activation(tmp_path, {name: b"changed" for name in names}, report)
    assert {name: (tmp_path / name).read_bytes() for name in names} == originals


def test_finalizer_activates_the_packaged_fortress_hud_and_preserves_other_bits(tmp_path, monkeypatch):
    import struct
    from test_resource_bank import write_bank
    base = tmp_path / 'base'; base.mkdir()
    bank = 'game/hub/hub_patch1.resources'
    (base / bank).parent.mkdir(parents=True)
    name = 'swf/hud/hud_score.swf'; hud = b'packaged AP HUD'
    write_bank(base / bank, [('cswf', name, hud, ())])
    mask = struct.pack('<IIQIQ', 0, 1, 17, 1, 2)
    write_bank(base / 'meta.resources', [('rs_streamfile', 'generated/buildgame/container.mask', mask, ())])
    (base / 'idRehash.map').write_text(bank + ';8\n')
    package = tmp_path / 'room.zip'
    with zipfile.ZipFile(package, 'w') as archive:
        archive.writestr('hub_patch1/' + name, hud)
    monkeypatch.setattr('doom_eap.launcher.native_enemy_resources.prepare_native_resource_banks',
                        lambda *args, **kwargs: ({}, {'maps': {}}))
    monkeypatch.setattr('doom_eap.launcher.resource_bank.obsolete_banks', lambda *args: {})
    monkeypatch.setattr('doom_eap.launcher.build_manifest.register_resource_manifest', lambda *args: None)
    monkeypatch.setattr(launcher_platform, 'detect_doom_processes', lambda: [])
    monkeypatch.setattr(launcher_platform, 'publish_file', lambda source, destination, **kwargs: source.replace(destination))
    result = campaign_resources.enable_campaign_resources(tmp_path, package)
    updated = campaign_resources._records(base / 'meta.resources', {'generated/buildgame/container.mask'})['generated/buildgame/container.mask'][3]
    assert result['state'] == 'enabled' and updated == mask[:-8] + struct.pack('<Q', 3)
    assert (base / 'idRehash.map').read_text() == bank + ';8\n'
    assert campaign_resources.enable_campaign_resources(tmp_path, package)['state'] == 'enabled'
    before = (base / 'meta.resources').read_bytes()
    with zipfile.ZipFile(package, 'w') as archive:
        archive.writestr('hub_patch1/' + name, b'different HUD')
    with pytest.raises(ValueError, match='Fortress HUD differs'):
        campaign_resources.enable_campaign_resources(tmp_path, package)
    assert (base / 'meta.resources').read_bytes() == before


def test_finalizer_digest_matches_metadata_after_bank_registration(tmp_path, monkeypatch):
    from doom_eap.launcher import native_enemy_resources, resource_bank
    base = tmp_path / 'base'
    base.mkdir()
    originals = {name: name.encode() for name in ('meta.resources', 'idRehash.map', 'packagemapspec.json')}
    originals['packagemapspec.json'] = b'{"files":[]}'
    for name, payload in originals.items():
        (base / name).write_bytes(payload)
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in originals.items()}
    monkeypatch.setattr(native_enemy_resources, 'prepare_native_resource_banks', lambda *args, **kwargs:
        ({'packagemapspec.json': b'{"files":[]}'},
         {'maps': {}, 'consolidated': {'bank': {}}, 'source_hashes': hashes}))
    monkeypatch.setattr(resource_bank, 'register_banks', lambda base, banks, updates, obsolete:
        updates.update({'meta.resources': b'updated resource metadata'}))
    monkeypatch.setattr('doom_eap.launcher.build_manifest.register_resource_manifest', lambda *args: None)
    monkeypatch.setattr(launcher_platform, 'detect_doom_processes', lambda: [])
    monkeypatch.setattr(launcher_platform, 'publish_file', lambda source, destination, **kwargs: source.replace(destination))
    package = tmp_path / 'room.zip'
    with zipfile.ZipFile(package, 'w') as archive:
        archive.writestr('seed_manifest.json', '{}')
    result = campaign_resources.enable_campaign_resources(tmp_path, package)
    assert result['meta_sha256'] == hashlib.sha256((base / 'meta.resources').read_bytes()).hexdigest()
    assert (base / 'meta.resources').read_bytes() != originals['meta.resources']
