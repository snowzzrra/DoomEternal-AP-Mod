"""Delivery safety across killed processes; all rooms, configuration and effects are isolated."""

import json

import pytest
from delivery_scenario_support import (
    Scenario,
    balance_packets,
    history,
    require,
)
from websockets.legacy.client import connect

from doom_eap.runtime.ledger import CommandState, Ledger, RoomKey

HEALTH_GRANT = "ai_ScriptCmdEnt ap_rpc_v3_7770025 activate"


@pytest.fixture
def v2_scenario(tmp_path):
    peer = Scenario(tmp_path, backend="v2")
    try:
        yield peer
    finally:
        peer.close()


def ledger_snapshot(peer):
    async def read():
        ledger = await Ledger.open(peer.root / "delivery.sqlite3", read_only=True)
        key = RoomKey("DeliveryRoomA", 0, 1, "fixture-v2")
        try:
            commands = await ledger.commands(key, tuple(CommandState), None, 256)
            attempts = []
            for command in commands:
                attempts.extend(await ledger.attempts(key, command.plan.command_id, 0, 256))
            return await ledger.room(key), commands, tuple(attempts)
        finally:
            await ledger.close()
    return peer.reader_loop.run_until_complete(read())


def finish_v2_script(peer):
    room, commands, attempts = ledger_snapshot(peer)
    if room.receipt_boundary == 0:
        peer.collect([history()])
    if not commands:
        peer.action("plan")
    if not attempts:
        peer.action("prepare")
    _, commands, _ = ledger_snapshot(peer)
    if commands[0].state is not CommandState.CONFIRMED:
        peer.action("send")
        peer.action("result", state="confirmed")
    peer.action("ack")


def _assert_barrier_committed_identity(after, before):
    assert [row.plan for row in after[1]] == [row.plan for row in before[1]]
    assert after[2] == before[2]


@pytest.mark.parametrize("point,stage", [
    (f"{name}:{side}", stage)
    for name, stage in (("ingestion", 0), ("plan", 1), ("preparation", 2),
                        ("send", 3), ("acceptance", 3), ("result", 4), ("result_commit", 4), ("ack", 5))
    for side in ("before", "after")
])
def test_v2_barriers_preserve_committed_identity(v2_scenario, point, stage):
    peer = v2_scenario
    peer.start(point)
    actions = [("collect", {}), ("plan", {}), ("prepare", {}), ("send", {}),
               ("result", {"state": "confirmed"}), ("ack", {})]
    for action, fields in actions[:stage]:
        if action == "collect":
            peer.collect([history()])
        else:
            peer.action(action, **fields)
    action, fields = actions[stage]
    if action == "collect":
        peer.begin_packet([history()])
    else:
        peer.bridge.send({"action": action, **fields})
    peer.bridge.expect(point)
    before = ledger_snapshot(peer)
    peer.checkpoint(point)
    peer.kill_bridge()
    peer.start()
    after = ledger_snapshot(peer)
    _assert_barrier_committed_identity(after, before)
    finish_v2_script(peer)
    _, commands, _ = ledger_snapshot(peer)
    assert len(commands) == 1 and commands[0].state is CommandState.CONFIRMED
    assert peer.effects() == [commands[0].plan.command_id]
    assert peer.action("query")["reply"]["retention"] == "compact"


@pytest.mark.parametrize("point", ["attempt_journal:after", "external_effect:before", "external_effect:after"])
@pytest.mark.parametrize("core_lost", [False, True])
def test_v2_bridge_crash_and_core_crash_have_distinct_evidence(v2_scenario, point, core_lost):
    peer = v2_scenario
    peer.kill_core()
    peer.start_core(point)
    peer.start()
    peer.collect([history()])
    peer.action("plan")
    peer.action("prepare")
    peer.bridge.send({"action": "send"})
    peer.core.expect(point)
    original = ledger_snapshot(peer)[2][0].attempt
    peer.kill_bridge()
    if core_lost:
        peer.kill_core()
        peer.start_core()
    else:
        peer.core.send("continue")
        peer.core.expect("effect_complete")
    peer.start()
    reply = peer.action("query")["reply"]
    assert ledger_snapshot(peer)[2][0].attempt == original
    if core_lost:
        assert reply["status"] == "instance_changed"
        peer.action("result", state="effect_unknown")
        assert ledger_snapshot(peer)[1][0].state is CommandState.EFFECT_UNKNOWN
        assert peer.status["items"][0] == {"status": "blocked", "actions": ["try_again", "mark_as_received"]}
        assert len(peer.effects()) == int(point == "external_effect:after")
    else:
        assert reply["effect"] == "applied"
        finish_v2_script(peer)
        assert peer.effects() == [original.command_id]


def test_v2_expired_result_keeps_identity_without_repeating_effect(v2_scenario):
    peer = v2_scenario
    peer.start()
    peer.collect([history()])
    peer.action("plan")
    peer.action("prepare")
    peer.action("send")
    original = ledger_snapshot(peer)[2][0].attempt

    async def changed_envelope():
        async with connect(f"ws://127.0.0.1:{peer.core_port}") as core:
            await core.send(json.dumps({"op": "submit", "reference": original.reference.hex(),
                                        "envelope": "changed"}))
            return json.loads(await core.recv())

    assert peer.reader_loop.run_until_complete(changed_envelope())["status"] == "identity_conflict"
    assert peer.action("expire")["reply"]["retention"] == "compact"
    assert peer.action("send")["reply"]["retention"] == "compact"
    peer.action("result", state="effect_unknown")
    assert ledger_snapshot(peer)[1][0].state is CommandState.EFFECT_UNKNOWN
    assert peer.effects() == [original.command_id]


@pytest.mark.parametrize("point", [
    pytest.param("send:after", id="publication:after"),
    pytest.param("acceptance:before", id="group_progress:before"),
    pytest.param("result_commit:after", id="group_completion:after"),
    pytest.param("ack:before", id="receipt_cursor:before"),
])
def test_owner_replay_preserves_one_grant(v2_scenario, point):
    peer = v2_scenario
    peer.start(point)
    peer.collect([history()])
    peer.action("plan")
    peer.action("prepare")
    if point in {"send:after", "acceptance:before"}:
        peer.bridge.send({"action": "send"})
    else:
        peer.action("send")
        if point == "result_commit:after":
            peer.bridge.send({"action": "result"})
        else:
            peer.action("result")
            peer.bridge.send({"action": "ack"})
    peer.bridge.expect(point)
    original = ledger_snapshot(peer)[1][0].plan
    peer.kill_bridge()
    peer.start()
    peer.collect([history()])
    finish_v2_script(peer)
    assert ledger_snapshot(peer)[1][0].plan == original
    assert peer.effects() == [original.command_id]


def test_owner_dispatch_without_readback_is_visible_after_boundary(v2_scenario):
    peer = v2_scenario
    peer.start()
    assert peer.collect([history()])["processed"] == 1
    peer.action("plan")
    peer.action("prepare")
    peer.action("send")
    peer.action("dispatch_only")
    status = peer.action("result")
    assert status["items"] == [{"status": "blocked", "actions": ["try_again", "mark_as_received"]}]
    assert ledger_snapshot(peer)[1][0].state is CommandState.EFFECT_UNKNOWN
    peer.kill_bridge()
    peer.start()
    packet = {**history(), "index": 1}
    assert peer.collect([packet])["processed"] == 2
    assert peer.status["items"][0]["status"] == "blocked"
    assert len(peer.effects()) == 1


def _assert_resolution_after_restart(original, peer, stored):
    assert stored.state is CommandState.EFFECT_UNKNOWN and stored.result == original.result
    assert stored.revision == original.revision + 1
    assert peer.effects() == [original.plan.command_id]


@pytest.mark.parametrize("point", ["resolution:before", "resolution:after"])
@pytest.mark.parametrize("action", ["try_again", "mark_as_received"])
def test_owner_resolution_survives_kill_and_duplicate_confirmation(v2_scenario, point, action):
    peer = v2_scenario
    peer.start(point)
    peer.collect([history()])
    peer.action("plan")
    peer.action("prepare")
    peer.action("send")
    peer.action("dispatch_only")
    peer.action("result")
    original, = ledger_snapshot(peer)[1]
    request = {"action": "resolve", "command_id": original.plan.command_id,
               "revision": original.revision, "resolution": action}
    peer.bridge.send(request)
    peer.bridge.expect(point)
    peer.kill_bridge()
    peer.start()
    peer.action("resolve", **{key: value for key, value in request.items() if key != "action"})
    peer.action("resolve", **{key: value for key, value in request.items() if key != "action"})
    rows = ledger_snapshot(peer)[1]
    assert len(rows) == (2 if action == "try_again" else 1)
    stored, = [row for row in rows if row.plan.command_id == original.plan.command_id]
    _assert_resolution_after_restart(original, peer, stored)


def restart_publication(peer, point, item, commands):
    peer.start(point)
    peer.begin_packet([history(item)])
    peer.bridge.expect(point)
    peer.checkpoint(point)
    if point == "publication:before":
        assert not list((peer.root / "base/ap_queue").glob("*.cmd"))
    peer.native()
    peer.checkpoint("before-restart")
    if point == "publication:before":
        assert peer.effects() == []
    peer.kill_bridge()
    peer.start()
    status = peer.collect([history(item)])
    peer.native()
    assert status["processed"] == 1, f"Receipt lost at {point}: {status}"
    require(peer.effects() == commands, f"Receipt replayed at {point}: {peer.effects()}")


def receive_ammo(peer, consumed=0):
    peer.collect([history(7770024)])
    return peer.collect(balance_packets(peer.status["state_key"], consumed))
