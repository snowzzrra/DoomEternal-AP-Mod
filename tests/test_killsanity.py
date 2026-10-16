import asyncio
from copy import deepcopy
import json
import logging
import struct
from types import SimpleNamespace

import pytest

from doom_eap.contracts.check_observation import CheckObservation
from doom_eap.runtime.check_publication import CheckPublication
from doom_eap.runtime.client_state_store import ClientStateStore
from doom_eap.runtime.killsanity import Killsanity
from doom_eap.runtime.weapon_points import SentinelWeaponPoints, WeaponPointsBlocked


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


def test_progress_survives_checkpoint_restart_and_atomic_write_failure(tmp_path):
    session = {}
    kills = tracker(session)
    document = {"version": 2, "sessions": {"room": session}}
    store = ClientStateStore(tmp_path / "client-state.json", version=2, migrate=lambda value: (value, False),
                             log_event=lambda *args, **kwargs: None, logger=logging.getLogger("killsanity-test"))
    persist = lambda: store.commit(document, reason="killsanity-test")
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
    assert reopened.total == 101 and reopened.cursor == 1
    assert set(restored["killsanity"]) == {"producer", "counter_abi", "cursor", "total", "source_gaps"}


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
    assert kills.total == 1088 and kills.state["source_gaps"] == 2
    assert "observed kills only" in caplog.text
    for update in (snapshot(1087, gaps=2), snapshot(1088, gaps=1)):
        with pytest.raises(ValueError, match="backwards"):
            kills.consume(update, lambda: None)
    assert kills.total == 1088
    with pytest.raises(ValueError, match="new room"):
        tracker({"killsanity": {"total": 87, "cursor": 87, "producer": PRODUCER}})


def test_publication_retries_all_due_milestones_and_respects_room_switch():
    async def run():
        session = {"killsanity": {"total": 250}}
        kills = tracker(session)
        sent, submitted = [], set()
        facts = lambda connected=True: CheckObservation(frozenset(), frozenset(submitted),
                                                       frozenset({1, 2}), True, connected)
        failures = [True]

        async def send(messages):
            if failures and failures.pop():
                raise OSError("disconnected")
            sent.extend(messages)

        publication = CheckPublication(send, submitted.add, 30)
        await kills.publish(facts(False), publication)
        assert not sent
        with pytest.raises(OSError):
            await kills.publish(facts(), publication)
        assert not submitted
        await kills.publish(facts(), publication)
        await kills.publish(facts(), publication)
        assert sent == [{"cmd": "LocationChecks", "locations": [1, 2]}]
        assert submitted == {1, 2} and kills.counter() == {"total": 250, "target": None}

        async def switch_room(messages):
            kills.bind({}, "e" * 64, MILESTONES)

        submitted.clear()
        await kills.publish(facts(), CheckPublication(switch_room, submitted.add, 30))
        assert not submitted and kills.total == 0

    asyncio.run(run())


def test_native_wire_selects_room_and_mission_totals_without_active_gameplay(tmp_path):
    link = SentinelWeaponPoints(tmp_path / "probe.exe", 71, NAMESPACE)
    response = dict(result="ok", combat_abi=2, pid=71, process_created="101",
                    instance_id=PRODUCER[2], build_id=PRODUCER[3], availability=0,
                    namespace_id=NAMESPACE, qualified_kills="1000", observation_gaps="0")
    calls = []

    def query(arguments, payload):
        calls.append((arguments, payload))
        return response

    link._run = query
    observed = link.observe_combat({MISSION})
    assert observed == snapshot(1000)
    header, body = calls[0][1][:16], calls[0][1][16:]
    assert struct.unpack("<4sHHII", header) == (b"SCIP", 1, 50, len(body), 0)
    expected = struct.pack("<QIIH", 4194304, 2, 71, 64) + NAMESPACE.encode()
    expected += struct.pack("<IH", 1, len(MISSION)) + MISSION.encode()
    assert body == expected
    for key, value in (("combat_abi", 1), ("namespace_id", "d" * 64),
                       ("qualified_kills", True), ("qualified_kills", "-1"),
                       ("qualified_kills", str(2**64)), ("observation_gaps", "bad")):
        before = response[key]
        response[key] = value
        with pytest.raises(WeaponPointsBlocked):
            link.observe_combat({MISSION})
        response[key] = before
    count = len(calls)
    for maps in ([], ["bad\0map"], ["não-ascii"], MISSION):
        with pytest.raises(WeaponPointsBlocked):
            link.observe_combat(maps)
    assert len(calls) == count


def test_presentation_retries_range_preserves_room_and_hides_finished_counter():
    async def run():
        kills = tracker({"killsanity": {"total": 87}})
        kills.consume(snapshot(13), lambda: None)
        calls = []

        def refuse(*args):
            calls.append(args)
            raise OSError("Core unavailable")

        with pytest.raises(OSError):
            await kills.present(SimpleNamespace(publish_combat_progress=refuse), False)
        kills.consume(snapshot(113), lambda: None)
        await kills.present(SimpleNamespace(publish_combat_progress=lambda *args: calls.append(args)), True)
        await kills.present(SimpleNamespace(publish_combat_progress=lambda *args: calls.append(args)), True)
        assert calls == [(100, 200, 100, 100, 100, False),
                         (200, 0, 100, 200, 100, False), (200, 0, 0, 0, 0, False)]

        kills.bind({}, "e" * 64, MILESTONES)
        await kills.present(SimpleNamespace(publish_combat_progress=lambda *args: calls.append(args)), True)
        assert calls[-1] == (0, 100, 0, 0, 0, True)

        kills.consume(snapshot(1, namespace="e" * 64,
                               producer=["72", "102", "c" * 32, "test-build"]), lambda: None)

        def change_room(*args):
            kills.bind({"killsanity": {"total": 99}}, NAMESPACE, MILESTONES)
            kills.consume(snapshot(1), lambda: None)

        await kills.present(SimpleNamespace(publish_combat_progress=change_room), True)
        await kills.present(SimpleNamespace(publish_combat_progress=lambda *args: calls.append(args)), True)
        assert calls[-1] == (100, 200, 100, 100, 100, True)

    asyncio.run(run())


def test_native_presentation_wire_binds_acknowledgement_to_request_and_scope(tmp_path):
    link = SentinelWeaponPoints(tmp_path / "probe.exe", 71, NAMESPACE)
    scope = dict(availability="enabled", lifecycle="active", process_created="101",
                 instance_id=PRODUCER[2], lifecycle_generation="2", build_id=PRODUCER[3])
    tamper = {}
    requests = []

    def query(arguments, message=None):
        if message is None:
            return scope
        assert arguments == ["--combat-presentation"]
        assert len(message) == 201
        assert struct.unpack_from("<4sHHII", message) == (b"SCIP", 1, 51, 185, 0)
        capability, pid, created, instance, generation, request_id, nonce, deadline = struct.unpack_from("<QIQ16sQQ16sI", message, 16)
        assert (capability, pid, created, instance.hex(), generation, deadline) == (8388608, 71, 101, PRODUCER[2], 2, 1000)
        assert request_id and len(nonce) == 16
        abi, namespace, total, target, first, last, step, visible = struct.unpack_from("<I65sQQQQQI", message, 88)
        assert (abi, namespace, total, target, first, last, step, visible) == (1, NAMESPACE.encode() + b"\0", 350, 400, 100, 300, 100, 1)
        requests.append(message)
        result = dict(result="ok", combat_presentation_abi=1, status=0, pid=71,
                    process_created="101", instance_id=PRODUCER[2], lifecycle_generation="2",
                    build_id=PRODUCER[3], request_id=str(request_id), nonce=nonce.hex(),
                    total="350", target="400", milestone="300", visible=1)
        result.update(tamper)
        return result

    link._run = query
    link.publish_combat_progress(350, 400, 100, 300, 100)
    for key, value in (("nonce", "00" * 16), ("pid", 72), ("process_created", "102"),
                       ("instance_id", "c" * 32), ("lifecycle_generation", "3"),
                       ("build_id", "other"), ("request_id", "0"), ("status", 1),
                       ("combat_presentation_abi", 2), ("total", "351"), ("target", "500"),
                       ("milestone", "200"), ("visible", 0)):
        tamper[key] = value
        with pytest.raises(WeaponPointsBlocked):
            link.publish_combat_progress(350, 400, 100, 300, 100)
        tamper.clear()
    before = len(requests)
    for args in ((True, 400), (350, 350), (350, 400, 100, 301, 100), (350, 400, 0, 100, 100)):
        with pytest.raises(WeaponPointsBlocked):
            link.publish_combat_progress(*args)
    assert len(requests) == before
