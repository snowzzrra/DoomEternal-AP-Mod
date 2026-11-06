import json

import pytest

from doom_eap.runtime.save_files import read_stage_restart
from doom_eap.runtime.stage_restart import activity_events


def test_runtime_restart_admission_requires_nightmare():
    from doom_eap.content.slot_data_contract import ULTRA_NIGHTMARE_CAPABILITY, StructureError, validate_slot_data
    from doom_eap.runtime.context_registry import SUPPORTED_SLOT_CAPABILITIES
    from tests.test_slot_data_contract import sample_catalog, sample_slot, sign_slot

    catalog = sample_catalog()
    data = sample_slot(catalog)
    data["runtime"]["difficulty"] = {"engine": 3, "death_policy": "restart_stage"}
    data["compatibility"]["required_capabilities"] = [ULTRA_NIGHTMARE_CAPABILITY]
    result = validate_slot_data(sign_slot(data, catalog), catalog, supported_capabilities=SUPPORTED_SLOT_CAPABILITIES)
    assert result.runtime.difficulty.death_policy == "restart_stage"
    with pytest.raises(StructureError, match="unsupported capabilities"):
        validate_slot_data(sign_slot(data, catalog), catalog,
                           supported_capabilities=SUPPORTED_SLOT_CAPABILITIES - {ULTRA_NIGHTMARE_CAPABILITY})
    data["runtime"]["difficulty"]["engine"] = 2
    with pytest.raises(StructureError, match="engine Nightmare"):
        validate_slot_data(sign_slot(data, catalog), catalog, supported_capabilities=SUPPORTED_SLOT_CAPABILITIES)


def test_restart_activity_is_process_and_room_bound(tmp_path, monkeypatch):
    monkeypatch.setattr("doom_eap.launcher.sentinel_diagnostics.diagnostics_directory", lambda _: tmp_path)
    record = {"schema": "sentinel-startup-v1", "pid": 7, "process_created": "81",
              "admission": {"namespace_id": "a" * 64}, "campaign": {
                  "restart_sequence": 2, "restart_status": 1, "restart_stage_id": 7770001,
                  "restart_map": "game/sp/e1m1_intro/e1m1_intro", "restart_reason": ""}}
    path = tmp_path / "7-81.latest.json"
    path.write_text(json.dumps(record))
    pending = read_stage_restart({}, "windows:7:81", "a" * 64)
    assert activity_events(None, pending, "Hell on Earth") == [{"status": "pending", "stage": "Hell on Earth"}]
    assert activity_events(pending, pending, "Hell on Earth") == []
    assert read_stage_restart({}, "windows:7:81", "b" * 64) is None
    assert read_stage_restart({}, "windows:7:82", "a" * 64) is None
    path.write_bytes(b"partial snapshot")
    assert read_stage_restart({}, "windows:7:81", "a" * 64) is None


@pytest.mark.parametrize(("status", "name"), [(2, "done"), (3, "failed")])
def test_restart_completion_keeps_the_pending_event_when_polling_skips_it(status, name):
    current = {"sequence": 5, "status": status}
    assert [event["status"] for event in activity_events(None, current, "mission")] == ["pending", name]
    pending = dict(current, status=1)
    assert [event["status"] for event in activity_events(pending, current, "mission")] == [name]
    assert activity_events(current, current, "mission") == []
    assert activity_events(dict(current, reason="first"), dict(current, reason="next"), "mission") == []
