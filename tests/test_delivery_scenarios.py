"""Delivery safety across killed processes; all rooms, configuration and effects are isolated."""

import json
import os
import shutil

import pytest

from delivery_scenario_support import (
    MSVC_REASON, SafetyFailure, Scenario, balance_packets, history, require,
)
from test_native_queue import native_queue_executable

requires_msvc = pytest.mark.skipif(
    os.name != "nt" or shutil.which("cl.exe") is None, reason=MSVC_REASON,
)
replayed_receipt = pytest.mark.xfail(
    strict=True, reason="Consumed command is republished when its receipt progress is not saved", raises=SafetyFailure,
)
missing_grant = pytest.mark.xfail(
    strict=True, reason="Interrupted native attempt is quarantined before its grant reaches the console", raises=SafetyFailure,
)
hidden_failure = pytest.mark.xfail(
    strict=True, reason="Committed receipt hides a failed native delivery from launcher status", raises=SafetyFailure,
)
HEALTH_GRANT = "ai_ScriptCmdEnt ap_rpc_v3_7770025 activate"


@pytest.fixture
def scenario(tmp_path):
    peer = Scenario(tmp_path)
    try:
        yield peer
    finally:
        peer.close()


@pytest.fixture
def native_scenario(tmp_path, native_queue_executable):
    peer = Scenario(tmp_path, native_queue_executable)
    try:
        yield peer
    finally:
        peer.close()


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


@pytest.mark.parametrize("point", [
    "publication:before",
    pytest.param("publication:after", marks=replayed_receipt),
    pytest.param("group_progress:before", marks=replayed_receipt),
    "group_progress:after",
])
@pytest.mark.integration
@requires_msvc
def test_restart_during_publication_preserves_one_grant(native_scenario, point):
    restart_publication(native_scenario, point, 7770029, [
        "ai_ScriptCmdEnt ap_rpc_v3_7770029_0 activate", "ai_ScriptCmdEnt ap_rpc_v3_7770029_1 activate",
    ])


@pytest.mark.parametrize("point", [
    "group_completion:before",
    pytest.param("group_completion:after", marks=replayed_receipt),
    pytest.param("receipt_cursor:before", marks=replayed_receipt),
    "receipt_cursor:after",
])
@pytest.mark.integration
@requires_msvc
def test_restart_after_group_completion_preserves_receipt(native_scenario, point):
    restart_publication(native_scenario, point, 7770025, [HEALTH_GRANT])


@pytest.mark.parametrize("point", [
    "native_claim:before", "native_claim:after", "attempt_journal:before",
    pytest.param("attempt_journal:after", marks=missing_grant),
    pytest.param("external_effect:before", marks=missing_grant),
    "external_effect:after", "native_completion:before", "native_completion:after",
])
@pytest.mark.integration
@requires_msvc
def test_restart_after_effect_does_not_grant_twice(native_scenario, point):
    peer = native_scenario
    peer.start()
    status = peer.collect([history()])
    assert status["processed"] == 1
    peer.native(point)
    peer.kill_bridge()
    peer.start()
    status = peer.collect([history()])
    peer.native()
    assert status["processed"] == 1, f"Receipt lost at {point}: {status}"
    require(peer.effects() == [HEALTH_GRANT], f"Grant missing or duplicated at {point}: {peer.effects()}")


@hidden_failure
@pytest.mark.integration
@requires_msvc
def test_failed_native_attempt_is_visible_after_cursor_commit(native_scenario):
    peer = native_scenario
    peer.start()
    assert peer.collect([history()])["processed"] == 1
    peer.native("attempt_journal:after")
    peer.kill_bridge()
    peer.start()
    peer.native()
    assert list((peer.root / "base/ap_queue").glob("*.failed"))
    status = peer.collect([history()])
    assert peer.effects() == []
    peer.action("reconcile")
    visible = any(event.get("status") in {"blocked", "uncertain", "error"}
                  for event in peer.bridge.launcher_events if event["type"] == "item_history_status")
    require(visible, f"Failed native delivery is invisible behind cursor: {status}")


@pytest.mark.parametrize("point", ["native_claim:before", "native_claim:after"])
@pytest.mark.integration
@requires_msvc
def test_map_change_blocks_pending_old_context_command(native_scenario, point):
    peer = native_scenario
    peer.start()
    peer.collect([history()])
    peer.native(point)
    marker = peer.root / "base/ap_queue/active_materialization_lease"
    marker.write_text("AP_MATERIALIZATION_LEASE_V1 2:2\n", encoding="ascii")
    peer.native()
    require(peer.effects() == [], "Old map command executed after the lease changed")
    marker.write_text("AP_MATERIALIZATION_LEASE_V1 1:1\n", encoding="ascii")
    peer.native()
    require(peer.effects() == [HEALTH_GRANT], "Held command was lost when its map returned")


def receive_ammo(peer, consumed=0):
    peer.collect([history(7770024)])
    return peer.collect(balance_packets(peer.status["state_key"], consumed))


@pytest.mark.integration
@requires_msvc
def test_auto_arm_cannot_execute_unconfirmed_refill(native_scenario):
    peer = native_scenario
    peer.start()
    assert receive_ammo(peer)["ammo_available"] == 1
    assert peer.action("ammo")["ammo_pending"]
    peer.ap.expect("debit")
    peer.action("background")
    peer.native()
    require(peer.effects() == ["give health"], "Native auto-arm executed an unconfirmed refill")
    assert peer.release()["ammo_available"] == 0
    peer.native()
    require(peer.effects() == ["give health", "give ammo"], "Confirmed refill did not execute exactly once")


def test_corrupt_state_blocks_replayed_history(scenario):
    peer = scenario
    peer.start()
    assert peer.collect([history(7770024)])["processed"] == 1
    peer.kill_bridge()
    original = b'{"version":2,"sessions":'
    state = peer.root / "state.json"
    state.write_bytes(original)
    assert not peer.start()["ready"]
    status = peer.collect([history(7770024)])
    require(status["blocked"] and not status["ready"], "Corrupt state authorized replayed history")
    assert state.read_bytes() == original
    assert not list((peer.root / "base/ap_queue").glob("*.cmd"))


def test_reconnect_does_not_repeat_debit(scenario):
    peer = scenario
    peer.start()
    receive_ammo(peer)
    peer.action("ammo")
    debit = peer.ap.expect("debit")
    peer.kill_bridge()
    peer.start()
    values = json.loads((peer.root / "ap-storage.json").read_text())
    assert values[debit["key"]] == 1
    status = receive_ammo(peer, values[debit["key"]])
    assert status["ammo_available"] == 0 and not status["ammo_pending"]
    peer.release()
    require(json.loads((peer.root / "ap-storage.json").read_text()) == values, "Reconnect repeated a debit")
    assert not list((peer.root / "base/ap_queue").glob("*.cmd"))


def test_room_switch_rejects_late_reply(scenario):
    peer = scenario
    peer.start()
    receive_ammo(peer)
    peer.action("ammo")
    old_key = peer.ap.expect("debit")["key"]
    peer.action("switch")
    assert old_key != f"doom_eap:{peer.status['state_key']}:ammo_refill_consumed"
    receive_ammo(peer)
    status = peer.release()
    require(not status["ammo_pending"] and status["ammo_available"] == 1, "Late reply charged the new room")
    assert not list((peer.root / "base/ap_queue").glob("*.cmd"))
