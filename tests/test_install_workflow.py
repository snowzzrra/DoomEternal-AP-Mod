import json

import pytest

from launcher_core import InstallPlan, InstallRecord, LaunchWorkflow, ModCompiler, RoomSnapshot, release_identity


def _snapshot() -> RoomSnapshot:
    ids = ModCompiler().active_location_ids(False)
    identity = release_identity()
    return RoomSnapshot.from_packets(
        {"seed_name": "install-seed"},
        {
            "team": 1,
            "slot": 2,
            "slot_data": {
                "randomize_dash": False,
                "bridge_protocol": 4,
                "content_revision": identity["content_revision"],
            },
            "missing_locations": ids[::2],
            "checked_locations": ids[1::2],
        },
    )


def test_room_snapshot_drives_successful_install(tmp_path):
    record = LaunchWorkflow().execute(_snapshot(), tmp_path / "active")
    persisted = json.loads((tmp_path / "active/.doom_ap_install_record.json").read_text())
    assert record.state == "active"
    assert persisted["manifest_hash"] == record.manifest_hash
    assert persisted["content_revision"] == release_identity()["content_revision"]
    assert set(record.installed_files) == {"e1m2_war.locations.json", "seed_manifest.json"}


def test_install_rolls_back_reverse_order_after_intermediate_failure(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "active"
    source.mkdir()
    target.mkdir()
    (source / "one.txt").write_text("new-one")
    (source / "two.txt").write_text("new-two")
    (target / "one.txt").write_text("old-one")
    prior = InstallRecord("active", str(target), "test", "", "old", {"one.txt": "old"}, None)
    (target / InstallPlan.RECORD_NAME).write_text(json.dumps(prior.__dict__))
    record = InstallRecord("installing", str(target), "test", "", "new", {}, None)
    with pytest.raises(RuntimeError, match="injected"):
        InstallPlan(target, source).install(record, fail_after=2)
    assert (target / "one.txt").read_text() == "old-one"
    assert not (target / "two.txt").exists()
    assert json.loads((target / InstallPlan.RECORD_NAME).read_text())["state"] == "failed"


def test_same_manifest_install_is_idempotent(tmp_path):
    workflow = LaunchWorkflow()
    target = tmp_path / "active"
    first = workflow.execute(_snapshot(), target)
    before = (target / "seed_manifest.json").stat().st_mtime_ns
    second = workflow.execute(_snapshot(), target)
    assert second.manifest_hash == first.manifest_hash
    assert (target / "seed_manifest.json").stat().st_mtime_ns == before
