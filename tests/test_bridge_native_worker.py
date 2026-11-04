import asyncio
import gc
import importlib
import json
import os
import threading
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

from doom_eap.runtime.native_worker import NativeWorker, StaleNativeResult


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    configuration = tmp_path / "config.json"
    configuration.write_text(json.dumps({
        "doom_base_dir": str(tmp_path / "game/base"),
        "save_games_dir": str(tmp_path / "local"),
        "steam_remote_dir": str(tmp_path / "Steam/userdata/1/782330/remote"),
        "client_state_file": str(tmp_path / "state.json"),
        "bridge_log_path": str(tmp_path / "bridge.log"),
    }))
    monkeypatch.setenv("DOOM_AP_CONFIG_FILE", str(configuration))
    monkeypatch.setenv("DOOM_AP_APPLICATION_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(root.parent / "Archipelago"))
    monkeypatch.syspath_prepend(str(root / "packaging/standalone_runtime"))
    module = importlib.import_module("doom_eap.runtime.bridge_client")
    monkeypatch.setattr(module, "DOOM_BASE_DIR", str(tmp_path / "game/base"))
    return module


def test_manual_reconcile_uses_coordinator_summary_without_legacy_rune_call(bridge):
    from unittest.mock import AsyncMock
    output = []
    processor = bridge.DoomCommandProcessor.__new__(bridge.DoomCommandProcessor)
    processor.ctx = SimpleNamespace(manual_reconcile_inventory=AsyncMock(return_value=(
        SimpleNamespace(replayed=1, special_stages=0, skipped_never_replay=0, skipped_unproven=0), None)))
    processor.output = output.append
    asyncio.run(processor._reconcile())
    assert len(output) == 1


def test_feedback_obligation_after_ingestion_survives_restart_without_duplicate(bridge, tmp_path, monkeypatch):
    from types import MethodType

    from test_delivery_runtime import Core, RoomDelivery
    from test_delivery_runtime import bridge as fixture_bridge

    from doom_eap.runtime import delivery_runtime
    from doom_eap.runtime.ledger import NetworkReceipt

    value, definitions, policies = fixture_bridge(tmp_path)
    monkeypatch.setattr(bridge, "ENABLE_ITEM_NOTIFICATIONS", True)
    value.items_received = [NetworkReceipt(7770900, 1, 1, 0)]
    value.location_setup = SimpleNamespace(received_ids=frozenset())
    for name in ("delivery_notification_commands", "item_activation_commands", "_fresh_receipt_owned_count", "progressive_stage"):
        setattr(value, name, MethodType(getattr(bridge.DoomEternalContext, name), value))
    value.items_processed = 1
    peers = []
    def connect(*args, **kwargs):
        core = Core(*args, **kwargs)
        if peers:
            core.commands = peers[-1].commands
            core.weapons = peers[-1].weapons
        peers.append(core)
        return core
    monkeypatch.setattr(delivery_runtime, "NativeTransport", connect)
    async def check():
        config = {"client_state_file": str(tmp_path / "state.json")}
        for _ in range(2):
            owner = RoomDelivery(value, config, definitions, policies, lambda *args, **kwargs: None)
            async def balance():
                return None
            owner._balance = balance
            try:
                await owner.reconcile(SimpleNamespace(epoch=1))
                notifications = [r for r in await owner._rows() if not r.plan.required]
                assert len(notifications) == 1
                assert notifications[0].attempt_count == 1
            finally:
                await owner.close()
    asyncio.run(check())


@pytest.mark.parametrize("platform", ["nt", "posix"])
@pytest.mark.parametrize("available", [True, False, None])
def test_process_probe_keeps_loop_responsive_and_fails_closed(bridge, monkeypatch, platform, available):
    from doom_eap.runtime import observer_lifecycle, proton
    release = threading.Event()
    entered = threading.Event()
    expected = Path(bridge.DOOM_BASE_DIR).parent / "DOOMEternalx64vk.exe"
    # the Proton helper reports Windows-style paths, the native path is the host path
    reported = proton.windows_path(expected) if platform == "posix" else str(expected)
    rows = ({"pid": 123, "created": 456, "path": reported},) if available else (() if available is False else None)

    def query(*args):
        entered.set()
        assert release.wait(5)
        return rows

    monkeypatch.setattr(bridge, "os", SimpleNamespace(**{**vars(os), "name": platform}))
    monkeypatch.setattr(observer_lifecycle, "windows_game_processes", query)
    monkeypatch.setattr(proton, "windows_processes", query)

    async def run():
        context = bridge.DoomEternalContext(None, None)
        ticks = 0
        task = asyncio.create_task(context._query_process_identity())
        try:
            deadline = asyncio.get_running_loop().time() + 1.1
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
                ticks += 1
            assert entered.is_set() and not task.done() and ticks >= 10
            release.set()
            assert await task == ("windows:123:456" if available else None)
            assert not context.item_state_ready
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            context._native_worker.close()
            await context.session_tasks.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["room", "process"])
def test_pending_process_probe_discards_changed_identity(bridge, monkeypatch, change):
    entered, release = threading.Event(), threading.Event()
    process = ["windows:123:456"]

    def query():
        value = process[0]
        entered.set()
        assert release.wait(5)
        return value

    monkeypatch.setattr(bridge, "doom_process_identity", query)

    async def run():
        context = bridge.DoomEternalContext(None, None)
        task = asyncio.create_task(context._query_process_identity())
        try:
            while not entered.is_set():
                await asyncio.sleep(0.01)
            if change == "room":
                context.room_seed_name = "different-room"
            else:
                process[0] = "windows:124:789"
            release.set()
            if change == "room":
                with pytest.raises(StaleNativeResult):
                    await task
            else:
                assert await task is None
            assert not context.item_state_ready
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            context._native_worker.close()
            await context.session_tasks.close()
    asyncio.run(run())


def test_cancelled_native_call_stays_serialized():
    entered, release = threading.Event(), threading.Event()
    calls = []

    def first():
        calls.append("first")
        entered.set()
        assert release.wait(5)
        calls.append("finished")

    async def run():
        worker = NativeWorker(lambda: "room", lambda: "process")
        task = asyncio.create_task(worker.call(first, process="process"))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            second = asyncio.create_task(worker.call(lambda: calls.append("second"), process="process"))
            await asyncio.sleep(0.05)
            assert calls == ["first"] and not second.done()
            release.set()
            await second
            assert calls == ["first", "finished", "second"]
        finally:
            release.set()
            worker.close()
    asyncio.run(run())








@pytest.mark.parametrize("monitor", ["death_monitor", "tracker"])
def test_stale_native_work_restarts_monitor_without_stopping_it(bridge, monkeypatch, monitor):
    async def run():
        context = bridge.DoomEternalContext(None, None)
        calls = []

        async def poll():
            calls.append(True)
            if len(calls) == 1:
                raise StaleNativeResult("room changed")
            context.exit_event.set()

        monkeypatch.setattr(context, f"{monitor}_loop", poll)
        await getattr(context, f"{monitor}_supervisor")()
        assert len(calls) == 2 and context.exit_event.is_set()
        await context.session_tasks.close()
    asyncio.run(run())


@pytest.mark.parametrize("stopping", [False, True])
def test_server_completion_retrieves_exception_before_silent_return(bridge, stopping):
    async def run():
        errors = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, event: errors.append(event))
        context = SimpleNamespace(exit_event=asyncio.Event(), _room_session_established=True,
                                  disconnected_intentionally=False, reset_room_session_authority=lambda reason: None)
        if stopping:
            context.exit_event.set()

        async def failed():
            raise AssertionError("transport failed")

        task = asyncio.create_task(failed())
        task.add_done_callback(lambda done: bridge.report_server_stop(context, done))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert task.done()
        reference = weakref.ref(task)
        del task
        gc.collect()
        await asyncio.sleep(0)
        assert reference() is None and not errors
    asyncio.run(run())
