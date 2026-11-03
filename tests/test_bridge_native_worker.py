import asyncio
import gc
import importlib
import json
import os
from pathlib import Path
import threading
import weakref
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


@pytest.mark.parametrize("platform", ["nt", "posix"])
@pytest.mark.parametrize("available", [True, False, None])
def test_process_probe_keeps_loop_responsive_and_fails_closed(bridge, monkeypatch, platform, available):
    from doom_eap.runtime import observer_lifecycle, proton
    release = threading.Event()
    entered = threading.Event()
    expected = Path(bridge.DOOM_BASE_DIR).parent / "DOOMEternalx64vk.exe"
    rows = ({"pid": 123, "created": 456, "path": str(expected)},) if available else (() if available is False else None)

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


@pytest.mark.parametrize("change", ["room", "process"])
@pytest.mark.parametrize("failure", [False, True])
def test_stale_native_receipt_cannot_advance_cursor(bridge, monkeypatch, change, failure):
    entered, release = threading.Event(), threading.Event()
    process = ["windows:123:456"]

    def grant():
        entered.set()
        assert release.wait(5)
        if failure:
            raise RuntimeError("native operation refused")
        return {"outcome": 0}

    monkeypatch.setattr(bridge, "doom_process_identity", lambda: process[0])

    async def run():
        context = bridge.DoomEternalContext(None, None)
        link = SimpleNamespace(_process_identity="windows:123:456", ensure_meat_hook=grant)

        async def native_link():
            return link

        monkeypatch.setattr(context, "_native_game_link_async", native_link)
        commits = []
        monkeypatch.setattr(context, "_acknowledge_receipt", lambda item: commits.append(item))
        task = asyncio.create_task(context._apply_native_owner_receipt(0, SimpleNamespace(item=7770083)))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.01)
            if change == "room":
                context.room_seed_name = "different-room"
            else:
                process[0] = "windows:124:789"
            release.set()
            with pytest.raises(StaleNativeResult):
                await task
            assert not commits and context.items_processed == 0
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            context._native_worker.close()
            await context.session_tasks.close()
    asyncio.run(run())


def test_proton_timeout_blocks_receipt_without_blocking_loop(bridge, monkeypatch):
    from doom_eap.runtime import proton
    import subprocess
    release, entered = threading.Event(), threading.Event()

    def timeout(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        raise subprocess.TimeoutExpired("fixture-proton", 30)

    monkeypatch.setattr(bridge, "os", SimpleNamespace(**{**vars(os), "name": "posix"}))
    monkeypatch.setattr(proton, "runtime", lambda configuration: (["fixture-proton"], {}))
    monkeypatch.setattr(proton.subprocess, "run", timeout)

    async def run():
        context = bridge.DoomEternalContext(None, None)
        context.item_state_ready = True
        commits = []
        monkeypatch.setattr(context, "_acknowledge_receipt", commits.append)
        task = asyncio.create_task(context._apply_native_owner_receipt(0, SimpleNamespace(item=7770083)))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            assert not task.done()
            release.set()
            with pytest.raises(RuntimeError, match="qualified Windows game process"):
                await task
            assert not commits and context.items_processed == 0
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            context._native_worker.close()
            await context.session_tasks.close()
    asyncio.run(run())


def test_weapon_point_checkpoints_stay_on_loop_and_precede_native_grant(bridge):
    from doom_eap.runtime.weapon_points import WeaponPointReceipts

    async def run():
        context = bridge.DoomEternalContext(None, None)
        context.session_state = {}
        main_thread = threading.get_ident()
        checkpoints, grants = [], []
        native = SimpleNamespace(gained=0)

        def persist():
            assert threading.get_ident() == main_thread
            assert asyncio.get_running_loop() is loop
            checkpoints.append(len(context.session_state["weapon_points"]["receipts"]))

        def observe():
            assert threading.get_ident() != main_thread
            return {"gained_after": native.gained}

        def grant(amount, expected):
            assert checkpoints == [0, 1] and threading.get_ident() != main_thread
            assert expected == native.gained
            grants.append(amount)
            native.gained += amount
            return {"gained_before": expected, "gained_after": native.gained}

        loop = asyncio.get_running_loop()
        native.observe, native.grant_weapon_upgrade_points = observe, grant
        owner = WeaponPointReceipts(context.session_state, persist, native, "room")
        try:
            await owner.deliver_async(0, "receipt", {0: "receipt"}, context._native_call)
            await owner.deliver_async(0, "receipt", {0: "receipt"}, context._native_call)
            assert grants == [3] and checkpoints == [0, 1]
        finally:
            context._native_worker.close()
            await context.session_tasks.close()
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
                                  disconnected_intentionally=False, reset_queue_session_authority=lambda reason: None)
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
