import importlib
import sys
import time
from types import ModuleType

import launcher_controller
from launcher_controller import LauncherController, LauncherState


class FakeWorkflow:
    def __init__(self, application_dir, *_args, **_kwargs):
        self.application_dir = application_dir


class FakeSetup:
    def __init__(self, *_args, **_kwargs):
        pass


class FakeSupervisor:
    instances = []

    def __init__(self, **kwargs):
        self.application_dir = kwargs["application_dir"]
        self.event_sink = kwargs["event_sink"]
        self.running = False
        self.stop_calls = 0
        self.__class__.instances.append(self)

    def start(self, **_kwargs):
        self.running = True

    def stop(self, **_kwargs):
        self.stop_calls += 1

    def fail(self):
        self.event_sink({"type": "error", "code": "connect_failed", "message": "no room"})

    def stopping_events(self):
        self.event_sink({"type": "client_stopping"})
        self.event_sink({"type": "disconnected"})

    def finish(self):
        self.running = False
        self.event_sink({"type": "worker_stopped", "intentional": True, "returncode": 0})


def _controller(tmp_path, monkeypatch, *, packaged=False):
    root = tmp_path / "package"
    root.mkdir()
    if packaged:
        (root / "client").mkdir()
    monkeypatch.setattr(launcher_controller, "load_options_schema", lambda _path: object())
    monkeypatch.setattr(launcher_controller, "IntegratedLaunchWorkflow", FakeWorkflow)
    monkeypatch.setattr(launcher_controller, "RoomSetupCoordinator", FakeSetup)
    monkeypatch.setattr(launcher_controller, "BridgeSupervisor", FakeSupervisor)
    FakeSupervisor.instances.clear()
    controller = LauncherController(root)
    game = tmp_path / "game"
    (game / "base").mkdir(parents=True)
    (game / "DOOMEternalx64vk.exe").write_text("")
    saves = tmp_path / "saves"
    saves.mkdir()
    connection = dict(
        endpoint="localhost:38281",
        slot="Doomguy",
        password="",
        game_root=str(game),
        saves_root=str(saves),
    )
    return controller, connection


def test_connection_failure_can_retry_before_failed_worker_finishes(tmp_path, monkeypatch):
    controller, connection = _controller(tmp_path, monkeypatch)
    controller.connect(**connection)
    failed = FakeSupervisor.instances[0]
    failed.fail()

    assert controller.state is LauncherState.FAILED
    assert failed.stop_calls == 1
    failed.stopping_events()
    assert controller.state is LauncherState.FAILED
    controller.connect(**connection)
    assert controller.state is LauncherState.CONNECTING
    assert len(FakeSupervisor.instances) == 1
    failed.stopping_events()
    assert controller.state is LauncherState.CONNECTING

    failed.finish()

    assert len(FakeSupervisor.instances) == 2
    assert controller.supervisor is FakeSupervisor.instances[1]
    assert controller.state is LauncherState.CONNECTING


def test_disconnect_is_state_driven_and_emits_once_after_cleanup(tmp_path, monkeypatch):
    controller, connection = _controller(tmp_path, monkeypatch)
    controller.connect(**connection)
    supervisor = FakeSupervisor.instances[0]

    started = time.monotonic()
    controller.disconnect()
    elapsed = time.monotonic() - started
    controller.disconnect()

    assert elapsed < 0.05
    assert controller.state is LauncherState.DISCONNECTING
    assert supervisor.stop_calls == 1
    supervisor.finish()
    events = list(controller.events.queue)
    assert controller.state is LauncherState.IDLE
    assert [event["type"] for event in events].count("disconnected") == 1


def test_connection_failure_after_connected_restores_retry_ready_ui(monkeypatch):
    pyside = ModuleType("PySide6")
    qtcore = ModuleType("PySide6.QtCore")
    setattr(qtcore, "QTimer", object)
    setattr(qtcore, "Qt", object)
    qtgui = ModuleType("PySide6.QtGui")
    setattr(qtgui, "QFont", object)
    setattr(qtgui, "QIcon", object)
    qtwidgets = ModuleType("PySide6.QtWidgets")
    for name in (
        "QApplication", "QCheckBox", "QComboBox", "QFileDialog", "QFrame", "QGridLayout",
        "QHBoxLayout", "QLabel", "QLineEdit", "QMainWindow", "QMessageBox", "QPlainTextEdit",
        "QProgressBar", "QPushButton", "QScrollArea", "QSpinBox", "QTabWidget", "QVBoxLayout", "QWidget",
    ):
        setattr(qtwidgets, name, object)
    monkeypatch.setitem(sys.modules, "PySide6", pyside)
    monkeypatch.setitem(sys.modules, "PySide6.QtCore", qtcore)
    monkeypatch.setitem(sys.modules, "PySide6.QtGui", qtgui)
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", qtwidgets)
    LauncherUI = importlib.import_module("launcher_ui").LauncherUI

    class FakeWidget:
        def __init__(self):
            self.enabled = True
            self.visible = False

        def setEnabled(self, enabled):
            self.enabled = enabled

        def setVisible(self, visible):
            self.visible = visible

    class FakeLauncherUI:
        def __init__(self):
            self._room_connected = False
            self._connection_pending = True
            self.server = FakeWidget()
            self.slot = FakeWidget()
            self.password = FakeWidget()
            self.game_root = FakeWidget()
            self.saves_root = FakeWidget()
            self.stop_button = FakeWidget()
            self.connected_options_notice = FakeWidget()
            self.reinstall_button = FakeWidget()
            self.guidance = FakeWidget()
            self.state = None
            self.logs = []

        def _set_connection_controls(self, *args, **kwargs):
            LauncherUI._set_connection_controls(self, *args, **kwargs)

        def _set_state(self, headline, detail, **state):
            self.next_action = state["action"]
            self.state = (headline, detail, state)

        def _append_log(self, text):
            self.logs.append(text)

    ui = FakeLauncherUI()
    LauncherUI._handle_event(ui, {"type": "connected"})
    ui.reinstall_button.setVisible(True)
    ui.guidance.setVisible(True)

    LauncherUI._handle_event(
        ui,
        {"type": "error", "code": "connection_failed", "message": "room connection lost"},
    )

    assert not ui._room_connected
    assert not ui._connection_pending
    assert all(field.enabled for field in (ui.server, ui.slot, ui.password, ui.game_root, ui.saves_root))
    assert not ui.stop_button.visible
    assert not ui.connected_options_notice.visible
    assert not ui.reinstall_button.visible
    assert not ui.guidance.visible
    assert ui.next_action == "Retry connection"
    assert ui.state == (
        "Connection failed.",
        "room connection lost",
        {"action": "Retry connection", "step": 2, "complete": 1, "state": "CONNECTION FAILED"},
    )


def test_root_launcher_resolves_sibling_client_resources(tmp_path, monkeypatch):
    controller, connection = _controller(tmp_path, monkeypatch, packaged=True)

    assert controller.client_dir == controller.application_dir / "client"
    assert controller.state_dir == controller.application_dir / "launcher-data"
    assert controller.workflow.application_dir == controller.client_dir
    controller.connect(**connection)
    assert FakeSupervisor.instances[0].application_dir == controller.client_dir
