import json
import shutil
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock
from zipfile import ZipFile

import pytest

from doom_eap.content import slot_data_contract
from doom_eap.launcher import launcher_core as core
from doom_eap.runtime import observers as registry
from doom_eap.runtime.campaign import UnifiedCampaign, validate_plan
from tests.room_fixture import sign_slot


@pytest.mark.parametrize("changed", ["slot_catalog.json", "projection_manifest.json", "native_enemy_assets.json"])
def test_cached_catalog_is_isolated_and_rejects_changed_installed_files(tmp_path, monkeypatch, changed):
    directory = tmp_path / "doom_eap/content"
    directory.mkdir(parents=True)
    installed = registry.REPO_ROOT / "doom_eap/content"
    for name in ("slot_catalog.json", "projection_manifest.json", *slot_data_contract.CATALOG_DEPENDENCIES):
        shutil.copyfile(slot_data_contract.catalog_dependency_path(installed, name), directory / name)
    monkeypatch.setattr(registry, "REPO_ROOT", tmp_path)
    first = registry.load_slot_catalog()
    first["roles"].clear()
    first.tables["maps"].clear()
    second = registry.load_slot_catalog()
    assert second["roles"] and second.tables["maps"]
    (directory / changed).write_text("{}", encoding="utf-8")
    with pytest.raises(slot_data_contract.DigestError):
        registry.load_slot_catalog()


def room_packets(data):
    return {"team": 0, "slot": 1, "slot_data": data, "missing_locations": [2_200_000_001],
            "checked_locations": [], "placements": [{
                "location_id": 2_200_000_001, "location_name": "Fixture check", "item_id": 7770001,
                "item_name": "Fixture item", "recipient_slot": 1, "recipient_name": "Fixture player",
                "classification": 0, "trap": False, "local": True,
            }]}


def snapshot_for(data):
    return core.RoomSnapshot.from_packets({"seed_name": "fixture"}, room_packets(data))


def tree_bytes(root):
    return {path.relative_to(root): path.read_bytes() if path.is_file() else None for path in root.rglob("*")}


def forbid_reconstruction(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("Room admission must use resolved decisions"))
    from doom_eap.content import enemy_options, killsanity_plan, native_enemy_plan, native_enemy_safety

    monkeypatch.setattr(enemy_options, "normalize_enemy_options", forbidden)
    monkeypatch.setattr(killsanity_plan, "build_killsanity_plan", forbidden)
    monkeypatch.setattr(native_enemy_plan, "build_native_recipes", forbidden)
    monkeypatch.setattr(native_enemy_safety, "resolve_native_stage", forbidden)
    return forbidden


@pytest.mark.parametrize("surplus", [0, 1])
def test_boundaries_use_shared_validation_without_reconstruction(resolved_room, monkeypatch, surplus):
    data, catalog = resolved_room
    economy = data["campaign"]["fortress"]["battery_economy"]
    economy.update(surplus=surplus, total=economy["cost"] + surplus)
    sign_slot(data, catalog)
    forbidden = forbid_reconstruction(monkeypatch)
    snapshot = snapshot_for(data)
    manifest = core.SeedManifest.from_room(snapshot, {2_200_000_001})
    assert manifest.options == registry.validate_slot_contract(data) == data
    assert core.RESOLVED_ROOM_CAPABILITIES <= set(manifest.required_capabilities)
    assert validate_plan(data) is data["campaign"]
    assert manifest.options["campaign"]["unprepared_start_stages"] == ["e1m1_intro"]
    assert manifest.options["campaign"]["opening_stage"] == "e1m1_intro"
    assert manifest.require_complete_placements() == snapshot.placements
    forbidden.assert_not_called()


def _alter_signed_room(data, catalog, fault):
    if fault == "reference":
        data["campaign"]["starting_stages"] = ["unknown"]
        sign_slot(data, catalog)
    elif fault == "unprepared":
        data["campaign"]["unprepared_start_stages"] = ["unknown"]
        sign_slot(data, catalog)
    elif fault == "battery":
        data["campaign"]["fortress"]["battery_economy"].update(surplus=2, total=4)
        sign_slot(data, catalog)
    elif fault == "compatibility":
        data["compatibility"]["bridge_protocol"] += 1
        sign_slot(data, catalog)
    elif fault == "capability":
        data["compatibility"]["required_capabilities"] = ["unsupported"]
        sign_slot(data, catalog)


def invalid_room(data, catalog, fault):
    if fault == "object":
        return []
    if fault == "schema":
        return {"slot_data_schema_version": 6}
    if fault == "structure":
        data["runtime"]["difficulty"]["engine"] = True
    elif fault == "digest":
        data["tracker_options"]["difficulty"] += 1
    elif fault == "size":
        data["tracker_options"]["padding"] = ["x" * 512] * 16_000
    else:
        _alter_signed_room(data, catalog, fault)
    return data


@pytest.mark.parametrize(("fault", "code"), [
    ("structure", "slot_structure"), ("digest", "slot_digest"), ("size", "slot_size"),
    ("reference", "slot_reference"), ("battery", "slot_structure"),
    ("unprepared", "slot_reference"),
    ("schema", "slot_schema"), ("compatibility", "slot_compatibility"), ("capability", "slot_structure"),
    ("object", "slot_structure"),
])
def test_rejection_precedes_all_writes_and_cached_packages(resolved_room, tmp_path, monkeypatch, fault, code):
    data, catalog = resolved_room
    snapshot = snapshot_for(data)
    manifest = core.SeedManifest.from_room(snapshot, {2_200_000_001})
    (tmp_path / "keep").write_bytes(b"unchanged")
    cached = tmp_path / "cached.zip"
    with ZipFile(cached, "w") as package:
        package.writestr("seed_manifest.json", json.dumps(manifest.document()))
        package.writestr("seed_receipt.json", json.dumps({"manifest_hash": manifest.manifest_hash}))
    assert core.RoomCompiler.validate_cached_package(cached, manifest)
    before = tree_bytes(tmp_path)
    bad = invalid_room(deepcopy(data), catalog, fault)
    snapshot = replace(snapshot, slot_data=bad)
    manifest = replace(manifest, options=bad)
    compiler = core.RoomCompiler.__new__(core.RoomCompiler)
    state, persist = {"keep": "unchanged"}, Mock()
    operations = [
        lambda: snapshot_for(bad), lambda: registry.validate_slot_contract(bad), lambda: validate_plan(bad),
        lambda: UnifiedCampaign(bad, state, persist),
        lambda: core.SeedManifest.from_room(snapshot, {2_200_000_001}),
        lambda: core.SeedManifest.create(seed_name="fixture", team=0, slot=1, options=bad,
                                         active_location_ids=list(snapshot.active_location_ids), placements=snapshot.placements),
        lambda: core.RoomCompiler.validate_cached_package(cached, manifest),
        lambda: compiler.build(manifest, tmp_path / "output"),
    ]
    for operation in operations:
        with pytest.raises(registry.SlotAdmissionError) as caught:
            operation()
        assert caught.value.code == code
        assert tree_bytes(tmp_path) == before
    assert state == {"keep": "unchanged"}
    persist.assert_not_called()


def test_placement_digest_is_checked_after_scouting(resolved_room, tmp_path):
    data, _ = resolved_room
    packets = room_packets(data)
    packets["placements"][0]["item_id"] += 1
    with pytest.raises(registry.SlotAdmissionError) as caught:
        core.RoomSnapshot.from_packets({"seed_name": "fixture"}, packets)
    assert caught.value.code == "slot_digest"
    assert tree_bytes(tmp_path) == {}


def test_setup_reports_admission_by_type_and_code():
    from doom_eap.launcher.launcher_integration import classify_setup_failure, setup_failure_payload

    error = registry.SlotAdmissionError("slot_schema", "A different room is required.")
    assert classify_setup_failure(error, phase="campaign_session") == "room_package"
    payload = setup_failure_payload(error, phase="campaign_session")
    assert payload["code"] == "slot_schema"
    assert payload["user_message"] == str(error)
    assert payload["recovery_action"] == "generate_room"


def test_admission_precedes_package_preparation(resolved_room, tmp_path):
    from doom_eap.launcher.launcher_integration import IntegratedLaunchWorkflow

    data, catalog = resolved_room
    snapshot = snapshot_for(data)
    snapshot = replace(snapshot, slot_data=invalid_room(deepcopy(data), catalog, "schema"))
    workflow = IntegratedLaunchWorkflow.__new__(IntegratedLaunchWorkflow)
    workflow._room_compiler = Mock(side_effect=AssertionError("Admission must precede package preparation"))
    workflow._config = Mock(side_effect=AssertionError("Admission must precede setup"))
    for boundary in (workflow._manifest_for, workflow.execute):
        with pytest.raises(registry.SlotAdmissionError) as caught:
            boundary(snapshot)
        assert caught.value.code == "slot_schema"
    workflow._room_compiler.assert_not_called()
    workflow._config.assert_not_called()
    assert tree_bytes(tmp_path) == {}
