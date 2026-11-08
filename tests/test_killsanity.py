import json
import logging
from copy import deepcopy

import pytest

from doom_eap.runtime.campaign import Killsanity
from doom_eap.runtime.inventory import ClientStateStore

NAMESPACE = "a" * 64
MISSION = "game/sp/intro/intro"
PRODUCER = ["71", "101", "b" * 32, "test-build"]
MILESTONES = [{"threshold": 100, "location_id": 1}, {"threshold": 200, "location_id": 2}]


def snapshot(kills, *, producer=PRODUCER, gaps=0, namespace=NAMESPACE):
    return dict(producer=producer, namespace_id=namespace, qualified_kills=kills, observation_gaps=gaps)


def tracker(session, namespace=NAMESPACE):
    result = Killsanity(logging.getLogger("killsanity-test"))
    result.bind(session, namespace, MILESTONES)
    return result


def _assert_persisted_kill_cursor(reopened, restored):
    assert reopened.total == 101 and reopened.cursor == 1
    assert set(restored["killsanity"]) == {"producer", "counter_abi", "cursor", "total", "source_gaps"}


def test_progress_survives_checkpoint_restart_and_atomic_write_failure(tmp_path):
    session = {}
    kills = tracker(session)
    document = {"version": 2, "sessions": {"room": session}}
    store = ClientStateStore(tmp_path / "client-state.json", version=2, migrate=lambda value: (value, False),
                             log_event=lambda *args, **kwargs: None, logger=logging.getLogger("killsanity-test"))
    def persist():
        return store.commit(document, reason="killsanity-test")
    first = snapshot(87)
    assert kills.consume(first, persist) == ()
    saved = deepcopy(session)
    assert kills.total == 87
    assert kills.consume(first, persist) == ()
    continuation = snapshot(100)

    def failed_commit():
        raise OSError("disk unavailable")

    with pytest.raises(OSError):
        kills.consume(continuation, failed_commit)
    assert session == saved
    assert kills.consume(continuation, persist) == (1,)
    restored = json.loads(store.path.read_text(encoding="utf-8"))["sessions"]["room"]
    reopened = tracker(restored)
    assert reopened.total == 100
    assert reopened.consume(continuation, lambda: None) == ()
    restarted = snapshot(1, producer=["72", "102", "c" * 32, "test-build"])
    reopened.consume(restarted, lambda: None)
    _assert_persisted_kill_cursor(reopened, restored)


def _assert_kill_source_gap(caplog, kills):
    assert kills.total == 1088 and kills.state["source_gaps"] == 2
    assert "observed kills only" in caplog.text


def test_cumulative_recovery_after_long_bridge_absence_and_room_isolation(caplog):
    session = {}
    kills = tracker(session)
    assert kills.consume(snapshot(87), lambda: None) == ()
    assert kills.consume(snapshot(1087), lambda: None) == (1, 2)
    kills.consume(snapshot(1087), lambda: None)
    assert kills.total == 1087 and kills.cursor == 1087
    other = tracker({}, "d" * 64)
    with pytest.raises(ValueError, match="another room"):
        other.consume(snapshot(1087), lambda: None)
    other.consume(snapshot(1, namespace="d" * 64), lambda: None)
    assert other.total == 1
    kills = tracker(session)
    kills.consume(snapshot(1088, gaps=2), lambda: None)
    _assert_kill_source_gap(caplog, kills)
    for update in (snapshot(1087, gaps=2), snapshot(1088, gaps=1)):
        with pytest.raises(ValueError, match="backwards"):
            kills.consume(update, lambda: None)
    assert kills.total == 1088
    with pytest.raises(ValueError, match="new room"):
        tracker({"killsanity": {"total": 87, "cursor": 87, "producer": PRODUCER}})
