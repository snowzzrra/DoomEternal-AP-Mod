import asyncio
import importlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "doom_base_dir": str(tmp_path / "base"), "save_games_dir": str(tmp_path),
        "client_state_file": str(tmp_path / "state.json"), "bridge_log_path": str(tmp_path / "bridge.log"),
    }))
    monkeypatch.setenv("DOOM_AP_CONFIG_FILE", str(config))
    monkeypatch.setenv("DOOM_AP_APPLICATION_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(root.parent / "Archipelago"))
    monkeypatch.syspath_prepend(str(root / "packaging/standalone_runtime"))
    module = importlib.import_module("doom_eap.runtime.bridge_client")
    monkeypatch.setattr(module, "INV_DUMP_DIR", str(tmp_path))
    monkeypatch.setattr(module, "doom_process_identity", lambda: "fixture-process")
    return module




@pytest.mark.parametrize("old_key", [None, "room-A", "room-B"])
def test_map_publishers_are_bound_to_room_and_pending_same_room_survives(bridge, tmp_path, old_key):
    context = SimpleNamespace(get_ap_state_key=lambda: "room-B")
    context.quarantine_unbound_physical_events = bridge.DoomEternalContext.quarantine_unbound_physical_events.__get__(context)
    files = [tmp_path / name for name in bridge.PUBLISHER_MAP_EVENT_FILENAMES]
    for file in files:
        file.write_bytes(b"pending publisher")
    if old_key:
        (tmp_path / "ap_event_session.json").write_text(json.dumps({"ap_state_key": old_key}))
    assert bridge.DoomEternalContext.check_and_update_event_session(context)
    if old_key == "room-B":
        assert all(file.read_bytes() == b"pending publisher" for file in files)
    else:
        assert not any(file.exists() for file in files)
        assert len(list((tmp_path / "ap_event_quarantine").rglob("*.txt"))) == len(files)


def test_failed_quarantine_cannot_bind_old_events_to_new_room(bridge, tmp_path, monkeypatch):
    file = tmp_path / next(iter(bridge.PUBLISHER_MAP_EVENT_FILENAMES))
    file.write_bytes(b"old room")
    context = SimpleNamespace(get_ap_state_key=lambda: "room-B")
    context.quarantine_unbound_physical_events = bridge.DoomEternalContext.quarantine_unbound_physical_events.__get__(context)
    monkeypatch.setattr(bridge, "quarantine_event_file", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="could not be quarantined"):
        bridge.DoomEternalContext.check_and_update_event_session(context)
    assert not (tmp_path / "ap_event_session.json").exists()
    assert file.read_bytes() == b"old room"


def test_death_monitor_restarts_after_transport_failure_and_honors_shutdown(bridge, monkeypatch):
    async def run():
        events = []
        context = SimpleNamespace(exit_event=asyncio.Event())
        async def monitor():
            events.append("poll")
            if len(events) == 1:
                raise OSError("connection interrupted")
            events.extend(("receive", "send"))
            context.exit_event.set()
        async def pause(_):
            return
        context.death_monitor_loop = monitor
        monkeypatch.setattr(bridge.asyncio, "sleep", pause)
        await bridge.DoomEternalContext.death_monitor_supervisor(context)
        assert events == ["poll", "poll", "receive", "send"]
    asyncio.run(run())


def test_lost_wup_ledger_drains_received_queue_and_replay_does_not_duplicate(bridge, monkeypatch):
    from doom_eap.contracts.runtime_context import RuntimeContext
    from doom_eap.runtime.weapon_points import WeaponPointReceipts

    async def run():
        context = bridge.DoomEternalContext(None, None)
        context.state_key, context.server, context.auth, context.slot = "room", object(), "Fixture", 1
        context.item_state_ready = True
        context.session_state = bridge.default_session_state()
        context.items_received = [SimpleNamespace(item=7770903, location=index, player=2, flags=0)
                                  for index in range(22)]
        native = SimpleNamespace(gained=63, grants=[])
        def grant(amount, expected):
            assert expected == native.gained
            native.grants.append(amount)
            native.gained += amount
            return {"gained_before": expected, "gained_after": native.gained}
        native.observe = lambda: {"gained_after": native.gained}
        native.grant_weapon_upgrade_points = grant
        history = {index: bridge.receipt_identity(item) for index, item in enumerate(context.items_received)}
        monkeypatch.setattr(context, "persist_session_state", lambda: None)
        monkeypatch.setattr(context, "weapon_points_receipt_owner", lambda: (
            WeaponPointReceipts(context.session_state, context.persist_session_state, native, "room"), history))
        monkeypatch.setattr(context, "_reconcile_native_receipt_owners", lambda _: None)
        runtime = RuntimeContext("base/e1m1_intro", "Base", ("game/sp/e1m1_intro/e1m1_intro",), ("e1m1_intro",), frozenset())
        monkeypatch.setattr(context, "_refresh_runtime_context", lambda _: runtime)
        monkeypatch.setattr(context, "_active_materialization_lease", lambda _: "42:1")
        monkeypatch.setattr(context, "spool_deferred_receipt_notification", lambda *a, **k: (True, ""))
        monkeypatch.setattr(context, "_reconcile_blood_punch", lambda _: None)
        monkeypatch.setattr(context, "_trigger_live_context_materialization", lambda: None)
        monkeypatch.setattr(context, "onboard_bootstrap", lambda _: None)
        await context.process_pending_item_receipts("received_items")
        assert context.items_processed == 22 and native.grants == [3]
        await context.process_pending_item_receipts("reconnect")
        assert context.items_processed == 22 and native.grants == [3]
    asyncio.run(run())
