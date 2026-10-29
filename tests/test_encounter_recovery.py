import asyncio
import importlib
import io
import json
import struct
import threading
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from doom_eap.launcher.launcher_controller import LauncherController
from doom_eap.runtime.encounter_recovery import MESSAGES
from doom_eap.runtime.weapon_points import SentinelWeaponPoints, WeaponPointsBlocked


class Transport(SentinelWeaponPoints):
    def __init__(self, tmp_path, outcome=4, failure=None):
        super().__init__(tmp_path / "probe.exe", 71, "a" * 64)
        self.calls, self.outcome, self.failure = [], outcome, failure

    def _run(self, args, payload=None):
        if payload is None:
            return {"availability": "enabled", "lifecycle": "active", "process_created": "123",
                    "instance_id": "b" * 32, "build_id": "fixture", "lifecycle_generation": "7"}
        op = struct.unpack_from("<H", payload, 6)[0]
        self.calls.append((op, payload))
        if (self.failure in ("unsupported", "old_probe") and op == 52) or (self.failure == "uncertain" and op == 53):
            error = WeaponPointsBlocked("fixture transport refusal")
            error.probe_result = "capability_unavailable" if self.failure == "unsupported" else "timeout"
            if self.failure == "old_probe":
                error.returncode = 2
            raise error
        state = 1 if op == 53 else 4 if op == 54 and self.outcome == 3 else 3
        return {"result": "ok", "namespace": self.namespace, "build_id": "fixture",
                "request_id": str(struct.unpack_from("<Q", payload, 60)[0]), "nonce": payload[68:84].hex(),
                "state": state, "outcome": 0 if state == 1 else 4 if op == 52 else self.outcome,
                "token": "91", "stamp": "92", "save": "ap-fixture/GAME-AUTOSAVE0"}


@pytest.mark.parametrize("outcome,message", MESSAGES.items())
def test_typed_polling_surfaces_exact_result_and_releases(tmp_path, outcome, message):
    link = Transport(tmp_path, outcome)
    response = link.finish_encounter()
    assert response["message"] == message
    assert [op for op, _ in link.calls] == [52, 53, 54, 55]
    request = link.calls[1][1]
    assert struct.unpack_from("<Q", request, 16)[0] == 16777216
    assert struct.unpack_from("<QQ", request, 249) == (91, 92)
    assert request[153:249].rstrip(b"\0") == b"ap-fixture/GAME-AUTOSAVE0"
    assert all(payload[24:] == request[24:] for _, payload in link.calls[1:])
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure,message,operations", [
    ("unsupported", "Unsupported encounter", [52]),
    ("old_probe", "Unsupported encounter", [52]),
    ("uncertain", "Recovery outcome unknown", [52, 53, 55]),
])
def test_unsupported_core_and_uncertain_submit_never_retry(tmp_path, failure, message, operations):
    link = Transport(tmp_path, failure=failure)
    response = link.finish_encounter()
    assert response["message"] == message
    assert [op for op, _ in link.calls] == operations
    assert bool(response.get("note")) == (failure in ("unsupported", "old_probe"))


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    game, saves = tmp_path / "game/base", tmp_path / "local"
    game.mkdir(parents=True); saves.mkdir()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"doom_base_dir": str(game), "save_games_dir": str(saves),
        "client_state_file": str(tmp_path / "state.json"), "bridge_log_path": str(tmp_path / "bridge.log")}))
    monkeypatch.setenv("DOOM_AP_CONFIG_FILE", str(config))
    monkeypatch.setenv("DOOM_AP_APPLICATION_DIR", str(tmp_path))
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "Archipelago"))
    monkeypatch.syspath_prepend(str(root / "DoomEternal-AP-Mod/packaging/standalone_runtime"))
    return importlib.import_module("doom_eap.runtime.bridge_client")


def test_launcher_control_reaches_bridge_and_records_result(tmp_path, monkeypatch, bridge):
    commands, events, logs = [], [], []
    controller = LauncherController.__new__(LauncherController)
    controller._lifecycle_lock = threading.Lock()
    controller.connected_room = True
    controller.supervisor = SimpleNamespace(running=True, send_command=commands.append)
    controller.emit = lambda *args, **kwargs: events.append((args, kwargs))
    controller.request_finish_encounter()
    assert commands == ['AP_CONTROL {"type":"finish_encounter"}']
    link = Transport(tmp_path)
    ctx = SimpleNamespace(exit_event=asyncio.Event(), native_game_link=lambda: link)
    ctx.request_finish_encounter = MethodType(bridge.DoomEternalContext.request_finish_encounter, ctx)
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO(commands[0] + "\n/exit\n"))
    monkeypatch.setattr(bridge, "emit_launcher_event", lambda *args, **kwargs: events.append((args, kwargs)))
    monkeypatch.setattr(bridge, "log_item_event", lambda *args, **kwargs: logs.append((args, kwargs)))
    asyncio.run(bridge.launcher_control_loop(ctx))
    assert events[-1][0] == ("encounter_recovery",)
    assert events[-1][1]["message"] == "Unsupported encounter"
    assert ctx._encounter_recovery_attempts[-1]["request_id"] == events[-1][1]["request_id"]
    assert logs[-1][1]["trace"] and [op for op, _ in link.calls] == [52, 53, 54, 55]
    controller.connected_room = False
    controller.request_finish_encounter()
    assert len(commands) == 1 and events[-1][1]["message"] == "No active encounter"


def test_button_sends_control_and_ui_prints_result(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QFrame, QLabel, QWidget
    from doom_eap.launcher.launcher_ui import LauncherUI
    app = QApplication.instance() or QApplication([])
    commands, logs = [], []
    controller = LauncherController.__new__(LauncherController)
    controller._lifecycle_lock = threading.Lock()
    controller.connected_room = True
    controller.supervisor = SimpleNamespace(running=True, send_command=commands.append)
    controller.emit = lambda *args, **kwargs: None
    shell = SimpleNamespace(controller=controller, _card=lambda *_: QFrame(),
        _label=lambda text, *_: QLabel(text), _status_strip=lambda: QWidget(),
        _append_session_event=lambda _: None, _append_log=logs.append)
    for name in ("_request_ammo_refill", "_set_ammo_refill_indicator", "_request_inventory_resync",
                 "_uninstall_room_package", "_disconnect", "_ap_save_actions", "_confirm_manual_installation",
                 "_prepare", "_run_setup_action"):
        setattr(shell, name, lambda *args, **kwargs: None)
    card = LauncherUI._session_hero(shell)
    assert shell.finish_encounter_button.text() == "Finish current encounter"
    shell.finish_encounter_button.click()
    assert commands == ['AP_CONTROL {"type":"finish_encounter"}']
    LauncherUI._handle_event(shell, {"type": "encounter_recovery", "message": "Unsupported encounter",
                                   "note": "The running Core does not support encounter recovery."})
    assert logs == ["Unsupported encounter", "The running Core does not support encounter recovery."]
    card.close()
    app.processEvents()
