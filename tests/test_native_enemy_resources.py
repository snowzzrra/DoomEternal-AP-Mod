import hashlib
import json
from pathlib import Path
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
