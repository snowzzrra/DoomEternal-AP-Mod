"""Characterize durable publication separately from native execution."""

import json
from pathlib import Path

import pytest

from doom_eap.runtime import bridge_client as bridge


@pytest.fixture
def io_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "QUEUE_DIR", str(tmp_path / "queue"))
    monkeypatch.setattr(bridge, "CLIENT_STATE_FILE", tmp_path / "client.json")
    gate = []
    monkeypatch.setattr(bridge, "set_rpc_execution", lambda enabled: gate.append(enabled) or True)
    return tmp_path, gate


def test_namespace_payload_and_duplicate_policy(io_boundary):
    root, gate = io_boundary
    assert bridge.send_command(" give ammo ", coalesce_key="ammo", state_key="room")
    command_id = bridge.room_scoped_command_id("ammo", "room")
    command = root / "queue" / f"{command_id}.cmd"
    assert command.read_bytes() == b"AP_EXECUTION_CLASS_V1 PLAYER_RUNTIME\ngive ammo\n"
    assert not bridge.send_command("replacement", coalesce_key="ammo", state_key="room")
    assert bridge.send_command("replacement", coalesce_key="ammo", state_key="room", already_queued_ok=True)
    assert gate == [True, True]
    assert b"replacement" not in command.read_bytes()
    command.rename(command.with_suffix(".processing"))
    assert bridge.command_spool_exists("ammo", "room")
    assert not bridge.send_command("replacement", coalesce_key="ammo", state_key="room")
    assert bridge.room_scoped_command_id(command_id, "room") == command_id


@pytest.mark.parametrize("fields,headers", [
    ({"execution_class": "MAP_ENTITY_SAFE", "operation": "FAST_TRAVEL_UNLOCK",
      "materialization_lease": "1:2"},
     "AP_EXECUTION_CLASS_V1 MAP_ENTITY_SAFE\nAP_MAP_ENTITY_OPERATION_V1 FAST_TRAVEL_UNLOCK\nAP_MATERIALIZATION_LEASE_V1 1:2\n"),
    ({"execution_class": "TRANSIENT_EFFECT", "transient_scope": "epoch-a"},
     "AP_EXECUTION_CLASS_V1 TRANSIENT_EFFECT\nAP_TRANSIENT_SCOPE_V1 epoch-a\n"),
])
def test_execution_class_wire_format(io_boundary, fields, headers):
    root, gate = io_boundary
    assert bridge.send_command("activate", coalesce_key="job", arm_rpc=False, room_scoped=False, **fields)
    assert (root / "queue/job.cmd").read_bytes() == (headers + "activate\n").encode()
    assert gate == []


def test_processing_race_does_not_leave_replay(io_boundary, monkeypatch):
    root, gate = io_boundary
    link = bridge.os.link

    def claim_during_publish(source, destination):
        link(source, destination)
        Path(destination).with_suffix(".processing").write_bytes(b"already claimed")

    monkeypatch.setattr(bridge.os, "link", claim_during_publish)
    assert bridge.send_command("give ammo", coalesce_key="job", room_scoped=False, already_queued_ok=True)
    assert not (root / "queue/job.cmd").exists()
    assert (root / "queue/job.processing").read_bytes() == b"already claimed"
    assert gate == [True]


def test_fsync_failure_does_not_publish(io_boundary, monkeypatch):
    root, gate = io_boundary

    def fail(_fd):
        raise OSError("fsync failed")

    monkeypatch.setattr(bridge.os, "fsync", fail)
    assert not bridge.send_command("give ammo", coalesce_key="job", room_scoped=False)
    assert not (root / "queue/job.cmd").exists()
    assert gate == []


def test_gate_failure_is_not_publication_rollback(io_boundary, monkeypatch):
    root, _gate = io_boundary

    def fail(_enabled):
        raise OSError("gate unavailable")

    monkeypatch.setattr(bridge, "set_rpc_execution", fail)
    assert not bridge.send_command("give ammo", coalesce_key="job", room_scoped=False)
    assert (root / "queue/job.cmd").is_file()


def test_state_commit_retains_unknown_fields_and_old_file_on_fault(io_boundary, monkeypatch):
    root, _gate = io_boundary
    state = {"version": 2, "sessions": {"room": {"processed_items": 2, "custom": [1, 2]}}}
    bridge.save_client_state(state)
    before = (root / "client.json").read_bytes()
    assert b"\r" not in before
    assert json.loads(before) == state

    def fail(_source, _destination):
        raise OSError("replace unavailable")

    monkeypatch.setattr(bridge.os, "replace", fail)
    with pytest.raises(OSError, match="replace unavailable"):
        bridge.save_client_state({"sessions": {}})
    assert (root / "client.json").read_bytes() == before


def test_invalid_envelopes_and_diagnostic_allowlist(io_boundary):
    root, _gate = io_boundary
    for fields in ({"execution_class": "unknown"}, {"operation": "FAST_TRAVEL_UNLOCK"},
                   {"execution_class": "MAP_ENTITY_SAFE"}, {"execution_class": "TRANSIENT_EFFECT"},
                   {"materialization_lease": "invalid"}, {"diagnostic": True}):
        assert not bridge.send_command("give ammo", coalesce_key="job", room_scoped=False, **fields)
    assert not (root / "queue/job.cmd").exists()
    assert bridge.send_command("condump AP_SUPPORT_FILE.txt", coalesce_key="support", room_scoped=False,
                               arm_rpc=False, diagnostic=True)
    assert (root / "queue/support.cmd").read_bytes() == (
        b"AP_DIAGNOSTIC_CONDUMP_V1 AP_SUPPORT_FILE.txt\ncondump AP_SUPPORT_FILE.txt\n"
    )
