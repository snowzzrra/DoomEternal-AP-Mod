"""Project-owned publication and persistence contracts without importing the root."""

import json
import hashlib
import logging
import copy
from pathlib import Path

import pytest

from doom_eap.contracts.command_publication import (
    CommandEvidence, queue_session_namespace, stable_spool_id, validate_spool_id,
)
from doom_eap.runtime.command_spool import CommandSpool
from doom_eap.runtime.client_state_store import ClientStateStore, ClientStateLoadError, atomic_write_json
from doom_eap.runtime.item_reconciliation import CLIENT_STATE_VERSION, migrate_client_state


def test_evidence_does_not_promote_acceptance_to_execution(tmp_path):
    def broken_gate(_enabled):
        raise OSError("gate failed after publication")

    spool = CommandSpool(tmp_path, arm_rpc=broken_gate, log_delivery=lambda *a, **k: None,
                         logger=logging.getLogger(__name__))
    result = spool.publish("give ammo", coalesce_key="job", room_scoped=False)
    assert not result.accepted
    assert result.evidence is CommandEvidence.DURABLY_PUBLISHED
    assert result.command_id == "job"
    duplicate = spool.publish("replacement", coalesce_key="job", room_scoped=False, arm_rpc=False,
                              already_queued_ok=True)
    assert duplicate.accepted
    assert duplicate.evidence is CommandEvidence.SPOOL_PRESENT
    (tmp_path / "job.cmd").unlink()
    assert not spool.exists("job", room_scoped=False)
    # Absence alone is not consumed or verified gameplay evidence.
    assert result.evidence is CommandEvidence.DURABLY_PUBLISHED


@pytest.mark.parametrize("value", ["", "..", "a/b", "a\\b", "nul", "COM1.cmd", "a:", "a.", "a ", "a\n", "x" * 129])
def test_invalid_ids_rejected(value):
    with pytest.raises(ValueError):
        validate_spool_id(value)


def test_namespace_and_logical_identity_are_deterministic(tmp_path):
    expected = hashlib.sha256(b"room").hexdigest()[:16]
    assert queue_session_namespace("room") == expected
    assert queue_session_namespace(None) is None
    assert stable_spool_id("item", {"b": 2, "a": 1}) == stable_spool_id("item", {"a": 1, "b": 2})
    spool = CommandSpool(tmp_path, arm_rpc=lambda _v: True, log_delivery=lambda *a, **k: None,
                         logger=logging.getLogger(__name__))
    assert spool.scoped_id("job") == "job"
    (tmp_path / "active_session_namespace").write_text(expected + "\n", encoding="ascii")
    assert spool.scoped_id("job") == spool.scoped_id("job", "room")
    assert spool.scoped_id(spool.scoped_id("job")) == spool.scoped_id("job")


def test_state_load_missing_and_corrupt_preserves_original_and_blocks(tmp_path):
    path = tmp_path / "state.json"
    store = ClientStateStore(path, version=CLIENT_STATE_VERSION, migrate=migrate_client_state,
                             log_event=lambda *a, **k: None, logger=logging.getLogger(__name__))
    assert store.load() == {"version": CLIENT_STATE_VERSION, "sessions": {}}
    path.write_text("{bad json", encoding="utf-8")
    with pytest.raises(ClientStateLoadError, match="Item delivery blocked"):
        store.load()
    assert path.read_text(encoding="utf-8") == "{bad json"
    assert [copy.read_text(encoding="utf-8") for copy in tmp_path.glob("state.json.corrupt-*")] == ["{bad json"]
    with pytest.raises(ClientStateLoadError):
        store.commit({"version": CLIENT_STATE_VERSION, "sessions": {}})
    path.unlink()
    with pytest.raises(ClientStateLoadError):
        store.load()


def test_state_session_projection(tmp_path):
    path = tmp_path / "state.json"
    store = ClientStateStore(path, version=CLIENT_STATE_VERSION, migrate=migrate_client_state,
                             log_event=lambda *a, **k: None, logger=logging.getLogger(__name__))
    session = {"custom": [1, 2], "receipt_history": "invalid", "deathlinked": True,
               "automap_cleanup": {}, "fast_travel_delivered": {}, "weapon_masteries_observed": {},
               "sticky_mastery_observed": True}
    document = {"version": CLIENT_STATE_VERSION, "sessions": {"room": session}}
    store.commit_session(document, session, processed_items=3, cultist_autosave_path=None,
                         received_deathlink_event_ids={f"{i:03d}" for i in range(70)},
                         save_slot_observations={"GAME-AUTOSAVE0": {"baseline": 1}})
    assert json.loads(path.read_text(encoding="utf-8")) == document
    assert session == {
        "custom": [1, 2], "receipt_history": {"processed_boundary": 3}, "processed_items": 3,
        "cultist_autosave_path": None, "received_deathlink_event_ids": [f"{i:03d}" for i in range(6, 70)],
        "save_slot_observations": {"GAME-AUTOSAVE0": {"baseline": 1}},
    }


@pytest.mark.parametrize("copy_fails", [False, True])
def test_unreadable_state_blocks_without_replacing_original(tmp_path, monkeypatch, copy_fails):
    path = tmp_path / "state.json"
    original = b'{"version": 2, "sessions": {"room": {"processed_items": 12}}}'
    path.write_bytes(original)
    store = ClientStateStore(path, version=CLIENT_STATE_VERSION, migrate=migrate_client_state,
                             log_event=lambda *a, **k: None, logger=logging.getLogger(__name__))
    def denied(*args, **kwargs):
        raise PermissionError("read denied")
    monkeypatch.setattr(Path, "read_text", denied)
    if copy_fails:
        monkeypatch.setattr("doom_eap.runtime.client_state_store.shutil.copy2", denied)
    with pytest.raises(ClientStateLoadError, match="read denied") as error:
        store.load()
    assert path.read_bytes() == original
    if copy_fails:
        assert "quarantine copy failed" in str(error.value)
    else:
        assert [copy.read_bytes() for copy in tmp_path.glob("state.json.corrupt-*")] == [original]


@pytest.mark.parametrize("version", [1, 2])
def test_existing_state_preserves_receipt_boundary(tmp_path, version):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": version, "sessions": {"room": {"processed_items": 12}}}))
    store = ClientStateStore(path, version=CLIENT_STATE_VERSION, migrate=migrate_client_state,
                             log_event=lambda *a, **k: None, logger=logging.getLogger(__name__))
    assert store.load()["sessions"]["room"]["processed_items"] == 12


def test_failed_session_commit_preserves_in_memory_history(tmp_path, monkeypatch):
    store = ClientStateStore(tmp_path / "state.json", version=CLIENT_STATE_VERSION, migrate=migrate_client_state,
                             log_event=lambda *a, **k: None, logger=logging.getLogger(__name__))
    session = {"processed_items": 7, "receipt_history": {"processed_boundary": 7}}
    document = {"version": CLIENT_STATE_VERSION, "sessions": {"room": session}}
    original = copy.deepcopy(document)
    def denied(*args, **kwargs):
        raise PermissionError("replace denied")
    monkeypatch.setattr("doom_eap.runtime.client_state_store.os.replace", denied)
    with pytest.raises(PermissionError):
        store.commit_session(document, session, processed_items=8, cultist_autosave_path=None,
                             received_deathlink_event_ids=set(), save_slot_observations={})
    assert document == original


@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_atomic_json_failure_preserves_previous_file(tmp_path, monkeypatch, failure):
    path = tmp_path / "config.json"
    original = b'{"player_option": "original"}\n'
    path.write_bytes(original)
    def denied(*args, **kwargs):
        raise OSError("write denied")
    monkeypatch.setattr(f"doom_eap.runtime.client_state_store.os.{failure}", denied)
    with pytest.raises(OSError, match="write denied"):
        atomic_write_json(path, {"player_option": "new"})
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))
