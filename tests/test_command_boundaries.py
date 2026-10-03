"""Project-owned publication and persistence contracts without importing the root."""

import json
import hashlib
import logging

import pytest

from doom_eap.contracts.command_publication import (
    CommandEvidence, queue_session_namespace, stable_spool_id, validate_spool_id,
)
from doom_eap.runtime.command_spool import CommandSpool
from doom_eap.runtime.client_state_store import ClientStateStore
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


def test_state_load_quarantine_and_session_projection(tmp_path):
    path = tmp_path / "state.json"
    store = ClientStateStore(path, version=CLIENT_STATE_VERSION, migrate=migrate_client_state,
                             log_event=lambda *a, **k: None, logger=logging.getLogger(__name__))
    assert store.load() == {"version": CLIENT_STATE_VERSION, "sessions": {}}
    path.write_text("{bad json", encoding="utf-8")
    assert store.load() == {"version": CLIENT_STATE_VERSION, "sessions": {}}
    assert len(list(tmp_path.glob("state.json.corrupt-*"))) == 1
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
