"""Application state and orchestration for standalone launcher."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import uuid
from dataclasses import asdict
from enum import Enum
from pathlib import Path

from launcher_core import LaunchWorkflow
from launcher_doctor import DoctorReport, LauncherDoctor, write_support_bundle
from launcher_integration import (
    IntegratedLaunchWorkflow,
    IntegratedSetupRecord,
    RoomSetupCoordinator,
)
from launcher_platform import (
    SteamInstallationLocator,
    detect_doom_processes,
    launch_doom_via_steam,
    launcher_user_paths,
    migrate_legacy_launcher_data,
    read_handshake_probe,
    validate_game_root,
    validate_save_directory,
)
from launcher_supervisor import BridgeSupervisor
from options_foundation import load_options_schema, save_player_yaml


def application_directory() -> Path:
    """Use distribution directory for frozen launcher resources."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


class LauncherState(str, Enum):
    IDLE = "IDLE"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    FAILED = "FAILED"
    DISCONNECTING = "DISCONNECTING"


class LauncherController:
    """Own launcher state; UI consumes queued events on its main thread."""

    def __init__(self, application_dir: Path | None = None):
        self.application_dir = (application_dir or application_directory()).resolve()
        packaged_client = self.application_dir / "client"
        self.client_dir = packaged_client if packaged_client.is_dir() else self.application_dir
        self.user_paths = launcher_user_paths()
        migrate_legacy_launcher_data(self.application_dir / "launcher-data", self.user_paths)
        self.state_dir = self.user_paths.state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.user_paths.config_dir.mkdir(parents=True, exist_ok=True)
        self.user_paths.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.user_paths.config_dir / "launcher.json"
        self.events: queue.Queue[dict[str, object]] = queue.Queue()
        self.config = self._load_config()
        self.options_schema = load_options_schema(
            self.client_dir / "data" / "options_schema.json"
        )
        self.state = LauncherState.IDLE
        self.connected_room = False
        self.supervisor: BridgeSupervisor | None = None
        self._lifecycle_lock = threading.Lock()
        self._pending_connect: dict[str, str] | None = None
        self.last_setup: IntegratedSetupRecord | None = None
        self._consent_lock = threading.Lock()
        self._consent_requests: dict[str, tuple[threading.Event, list[bool]]] = {}
        self.workflow = IntegratedLaunchWorkflow(
            self.client_dir,
            self.state_dir,
            self.config_path,
            event_sink=self._setup_event,
            consent=self._request_consent,
        )
        self.setup = RoomSetupCoordinator(
            self.workflow,
            self._setup_event,
            self._setup_result,
        )

    def _load_config(self) -> dict[str, object]:
        if self.config_path.is_file():
            try:
                value = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    value.pop("password", None)
                    return value
            except (OSError, json.JSONDecodeError):
                pass
        return {}

    def _persist_config(self) -> None:
        self.config.pop("password", None)
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.config, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.config_path)

    def save_config(self, updates: dict[str, object]) -> None:
        self.config.update(updates)
        self._persist_config()
        LaunchWorkflow.write_client_config(
            self.client_dir,
            runtime_config=self.config,
        )

    def discover(self) -> dict[str, object]:
        found: dict[str, object] = {"platform": "windows" if os.name == "nt" else "linux"}
        installations = SteamInstallationLocator().discover()
        found["game_discovery"] = asdict(SteamInstallationLocator().discover_sentinel())
        if len(installations) == 1:
            installation = installations[0]
            found["game_root"] = str(installation.game_root)
            found["doom_base_dir"] = str(installation.game_root / "base")
            if os.name != "nt":
                saves = (
                    installation.library_root
                    / "steamapps/compatdata/782330/pfx/drive_c/users/steamuser/Saved Games"
                    / "id Software/DOOMEternal/base"
                )
                if saves.is_dir():
                    found["save_games_dir"] = str(saves)
        elif len(installations) > 1:
            found["ambiguous_game_roots"] = [str(item.game_root) for item in installations]

        if os.name == "nt" and "save_games_dir" not in found:
            saves = Path.home() / "Saved Games/id Software/DOOMEternal/base"
            if saves.is_dir():
                found["save_games_dir"] = str(saves)

        remote_candidates: list[Path] = []
        for root in SteamInstallationLocator.default_roots():
            userdata = root / "userdata"
            if not userdata.is_dir():
                continue
            remote_candidates.extend(
                candidate
                for candidate in userdata.glob("*/782330/remote")
                if candidate.is_dir()
            )
        unique_remote = sorted({candidate.resolve() for candidate in remote_candidates})
        if len(unique_remote) == 1:
            found["steam_remote_dir"] = str(unique_remote[0])
            found["steam_id3"] = LaunchWorkflow._steam_id3(found["steam_remote_dir"])
        elif len(unique_remote) > 1:
            found["ambiguous_steam_remote_dirs"] = [str(path) for path in unique_remote]
        self.config = {**self.config, **found}
        self._persist_config()
        LaunchWorkflow.write_client_config(
            self.client_dir,
            runtime_config=self.config,
        )
        return found

    def game_processes(self) -> tuple[dict[str, object], ...]:
        """Return bounded facts for supported game/client processes."""
        return detect_doom_processes()

    def is_game_running(self) -> bool:
        return any(str(item.get("name", "")).casefold() in {"doometernalx64vk", "doometernalx64vk.exe"} for item in self.game_processes())

    def launch_game(self) -> str:
        """Launch through Steam URL handler."""
        url = launch_doom_via_steam()
        self.emit("steam_launch_requested", url=url)
        return url

    def probe_handshake(self) -> dict[str, object]:
        base = self.config.get("doom_base_dir")
        if not base:
            result = {"status": "unavailable", "reason": "DOOM Eternal base directory is not configured"}
        else:
            result = read_handshake_probe(Path(str(base)).expanduser() / "ap_gameplay_save.state")
        self.emit("handshake_probe", **result)
        return dict(result)

    def run_doctor(self) -> DoctorReport:
        report = LauncherDoctor(config=self.config, paths=self.user_paths).run()
        self.emit("doctor_report", report=report.document())
        return report

    def repair_preview(self):
        return LauncherDoctor(config=self.config, paths=self.user_paths).repair_preview()

    def apply_repair(self, action_key: str) -> str:
        """Apply selected Doctor action. Room changes require connected-room setup."""
        doctor = LauncherDoctor(config=self.config, paths=self.user_paths)
        actions = {action.key: action for action in doctor.repair_preview()}
        action = actions.get(action_key)
        if action is None:
            raise ValueError("repair action is unavailable")
        if action_key == "archive_stale_install_record":
            backup = doctor.archive_stale_install_record()
            self.emit("repair_complete", action=action_key, backup=str(backup))
            return str(backup)
        if action_key == "reinstall_room_mod":
            if not self.setup.start(force=True):
                raise RuntimeError("connect to room before reinstalling its mod")
            self.emit("repair_started", action=action_key)
            return "Room mod reinstall started; installed hash will be checked after setup."
        raise ValueError("unsupported repair action")

    def create_support_bundle(self, destination: Path, *, logs: list[str] | None = None) -> Path:
        bundle = write_support_bundle(destination, self.run_doctor(), logs=logs or [])
        self.emit("support_bundle_ready", path=str(bundle))
        return bundle

    def emit(self, kind: str, **payload: object) -> None:
        self.events.put({"type": kind, **payload})

    def _worker_event(
        self, supervisor: BridgeSupervisor, event: dict[str, object]
    ) -> None:
        kind = str(event.get("type", ""))
        stop_failed_worker = False
        pending: dict[str, str] | None = None
        emit_event = True
        intentional_disconnect = False
        with self._lifecycle_lock:
            if supervisor is not self.supervisor:
                return
            if kind in {"client_started", "connecting"}:
                if self.state is not LauncherState.DISCONNECTING:
                    self.state = LauncherState.CONNECTING
            elif kind == "connected":
                if self.state is LauncherState.DISCONNECTING:
                    emit_event = False
                else:
                    self.state = LauncherState.CONNECTED
            elif kind in {"error", "setup_failed"}:
                if (
                    self.state is LauncherState.DISCONNECTING
                    or self._pending_connect is not None
                ):
                    emit_event = False
                else:
                    self.state = LauncherState.FAILED
                    self.connected_room = False
                    stop_failed_worker = supervisor.running
            elif kind in {"client_stopping", "disconnected"}:
                if (
                    self.state in {LauncherState.FAILED, LauncherState.DISCONNECTING}
                    or self._pending_connect is not None
                ):
                    emit_event = False
            elif kind == "worker_stopped":
                self.supervisor = None
                emit_event = False
                if self._pending_connect is not None:
                    pending = self._pending_connect
                    self._pending_connect = None
                    self.state = LauncherState.CONNECTING
                elif self.state is LauncherState.DISCONNECTING:
                    self.state = LauncherState.IDLE
                    intentional_disconnect = True
                elif self.state is not LauncherState.FAILED:
                    self.state = LauncherState.FAILED
        if emit_event:
            self.events.put(event)
        if stop_failed_worker:
            supervisor.stop(emit_disconnected=False)
        if intentional_disconnect:
            self.emit("disconnected", intentional=True)
        if pending is not None:
            self._start_supervisor(pending)

    def _worker_log(self, text: str) -> None:
        if text:
            self.emit("log", message=text)

    def _setup_event(self, kind: str, payload: dict[str, object]) -> None:
        self.events.put({"type": kind, **payload})

    def _setup_result(self, record: IntegratedSetupRecord) -> None:
        self.last_setup = record

    def _request_consent(self, spec) -> bool:
        request_id = uuid.uuid4().hex
        wait = threading.Event()
        answer: list[bool] = []
        with self._consent_lock:
            self._consent_requests[request_id] = (wait, answer)
        self.emit(
            "dependency_consent_required",
            request_id=request_id,
            name=spec.name,
            version=spec.version,
            url=spec.url,
            sha256=spec.sha256,
        )
        wait.wait(timeout=300.0)
        with self._consent_lock:
            self._consent_requests.pop(request_id, None)
        return bool(answer and answer[0])

    def resolve_consent(self, request_id: str, accepted: bool) -> None:
        with self._consent_lock:
            pending = self._consent_requests.get(request_id)
        if pending is None:
            return
        wait, answer = pending
        answer.append(bool(accepted))
        wait.set()

    def _entrypoint(self) -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve()
        return self.client_dir / "launcher_app.py"

    def _archipelago_source(self) -> Path | None:
        if getattr(sys, "frozen", False):
            return None
        configured = os.environ.get("ARCHIPELAGO_SOURCE")
        candidate = Path(configured).expanduser() if configured else self.application_dir.parent / "Archipelago"
        return candidate.resolve() if (candidate / "CommonClient.py").is_file() else None

    def _start_supervisor(self, connection: dict[str, str]) -> None:
        profile = hashlib.sha256(
            f"{connection['endpoint']}\0{connection['slot']}\0{self.state_dir}".encode()
        ).hexdigest()
        supervisor = BridgeSupervisor(
            entrypoint=self._entrypoint(),
            application_dir=self.client_dir,
            config_path=self.config_path,
            profile_id=profile,
            event_sink=lambda event: self._worker_event(supervisor, event),
            log_sink=self._worker_log,
            archipelago_source=self._archipelago_source(),
        )
        with self._lifecycle_lock:
            self.supervisor = supervisor
            self.state = LauncherState.CONNECTING
        try:
            supervisor.start(
                endpoint=connection["endpoint"],
                player=connection["slot"],
                password=connection["password"],
            )
        except Exception as error:
            with self._lifecycle_lock:
                if self.supervisor is supervisor:
                    self.supervisor = None
                    self.state = LauncherState.FAILED
            self.emit("setup_failed", code="bridge_start_failed", message=str(error))
            return
        self.emit("connecting", endpoint=connection["endpoint"], slot=connection["slot"])

    def connect(
        self,
        *,
        endpoint: str,
        slot: str,
        password: str,
        game_root: str,
        saves_root: str,
    ) -> None:
        if not endpoint.strip() or not slot.strip():
            raise ValueError("server address and slot are required")
        try:
            game = validate_game_root(Path(game_root))
            saves = validate_save_directory(Path(saves_root))
        except ValueError as error:
            raise ValueError(str(error)) from error
        self.save_config(
            {
                "server_address": endpoint.strip(),
                "slot": slot.strip(),
                "game_root": str(game),
                "doom_base_dir": str(game / "base"),
                "save_games_dir": str(saves),
            }
        )
        connection = {
            "endpoint": endpoint.strip(),
            "slot": slot.strip(),
            "password": password,
        }
        stop_failed_worker: BridgeSupervisor | None = None
        with self._lifecycle_lock:
            if self.state in {LauncherState.CONNECTING, LauncherState.CONNECTED}:
                raise RuntimeError("disconnect the current bridge worker before connecting again")
            if self.state is LauncherState.DISCONNECTING:
                raise RuntimeError("bridge worker is still disconnecting")
            if self.supervisor is not None:
                self._pending_connect = connection
                self.state = LauncherState.CONNECTING
                stop_failed_worker = self.supervisor
        if stop_failed_worker is not None:
            stop_failed_worker.stop(emit_disconnected=False)
            return
        self._start_supervisor(connection)

    def process_event(self, event: dict[str, object]) -> None:
        if event.get("type") == "connected":
            self.connected_room = True
            self.setup.observe(event)
            try:
                from launcher_core import RoomSnapshot

                snapshot = RoomSnapshot.from_event(event)
                state = self.workflow.install_state(snapshot)
                self.emit(
                    "room_install_state",
                    state=state.state,
                    manifest_hash=state.manifest_hash,
                    staged_mod=state.staged_mod,
                    steam_launch_option=state.steam_launch_option,
                    reason=state.reason,
                )
            except Exception as error:
                self.emit(
                    "room_install_state",
                    state="install_needed",
                    reason=f"could not verify installed room mod: {error}",
                )

    def send_command(self, text: str) -> None:
        if not text.strip():
            return
        if self.supervisor is None:
            raise RuntimeError("not connected")
        self.supervisor.send_command(text)

    def disconnect(self) -> None:
        supervisor: BridgeSupervisor | None
        with self._lifecycle_lock:
            if self.state is LauncherState.DISCONNECTING:
                return
            supervisor = self.supervisor
            self._pending_connect = None
            self.connected_room = False
            if supervisor is None:
                self.state = LauncherState.IDLE
            else:
                self.state = LauncherState.DISCONNECTING
        if supervisor is None:
            self.emit("disconnected", intentional=True)
            return
        supervisor.stop(emit_disconnected=False)

    def save_player_options(
        self,
        destination: Path,
        player_name: str,
        values: dict[str, object],
    ) -> Path:
        """Save future-room generation input without touching connected room state."""
        saved = save_player_yaml(
            destination,
            self.options_schema,
            player_name,
            values,
        )
        self.emit("player_yaml_saved", path=str(saved))
        return saved

    def retry_setup(self) -> bool:
        return self.setup.start(force=True)

    def prepare_setup(self) -> bool:
        return self.setup.start()

    def reinstall_setup(self) -> bool:
        return self.setup.start(force=True)

    def open_adapter(self) -> None:
        record = self.last_setup
        if record is None or not record.adapter_command:
            raise RuntimeError("no prepared manager/injector command is available")
        command = list(record.adapter_command)
        working_directory = Path(str(self.config.get("game_root") or self.application_dir))
        if os.name == "nt":
            subprocess.Popen(command, cwd=working_directory)
            return
        terminal_commands = (
            ("x-terminal-emulator", "-e"),
            ("konsole", "-e"),
            ("gnome-terminal", "--"),
            ("xterm", "-e"),
        )
        for terminal, flag in terminal_commands:
            executable = shutil.which(terminal)
            if executable:
                subprocess.Popen([executable, flag, *command], cwd=working_directory)
                return
        raise RuntimeError("no supported terminal emulator found for interactive injector")

    def confirm_windows_installation(self, succeeded: bool) -> None:
        """Record user confirmation after EternalModManager completes its manual step."""
        if self.last_setup is None or self.last_setup.adapter_state != "manual_action_required":
            raise RuntimeError("there is no pending EternalModManager confirmation")
        self.workflow.mark_windows_installation(succeeded)
        self.emit("windows_installation_confirmed", succeeded=succeeded)

    def close(self) -> None:
        self.disconnect()
