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
from pathlib import Path

from launcher_integration import (
    IntegratedLaunchWorkflow,
    IntegratedSetupRecord,
    RoomSetupCoordinator,
)
from launcher_platform import SteamInstallationLocator
from launcher_supervisor import BridgeSupervisor


def application_directory() -> Path:
    """Use distribution directory when frozen, never temporary extraction paths."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


class LauncherController:
    """Own launcher state; UI consumes queued events on its main thread."""

    def __init__(self, application_dir: Path | None = None):
        self.application_dir = (application_dir or application_directory()).resolve()
        self.state_dir = self.application_dir / "launcher-data"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.state_dir / "launcher.json"
        self.events: queue.Queue[dict[str, object]] = queue.Queue()
        self.config = self._load_config()
        self.supervisor: BridgeSupervisor | None = None
        self.last_setup: IntegratedSetupRecord | None = None
        self._consent_lock = threading.Lock()
        self._consent_requests: dict[str, tuple[threading.Event, list[bool]]] = {}
        self.workflow = IntegratedLaunchWorkflow(
            self.application_dir,
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
                    return value
            except (OSError, json.JSONDecodeError):
                pass
        return {}

    def save_config(self, updates: dict[str, object]) -> None:
        self.config.update(updates)
        self.config.pop("password", None)
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.config, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.config_path)

    def discover(self) -> dict[str, object]:
        found: dict[str, object] = {"platform": "windows" if os.name == "nt" else "linux"}
        installations = SteamInstallationLocator().discover()
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
        elif len(unique_remote) > 1:
            found["ambiguous_steam_remote_dirs"] = [str(path) for path in unique_remote]
        self.config = {**found, **self.config}
        return found

    def emit(self, kind: str, **payload: object) -> None:
        self.events.put({"type": kind, **payload})

    def _worker_event(self, event: dict[str, object]) -> None:
        self.events.put(event)

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
        return self.application_dir / "launcher_app.py"

    def _archipelago_source(self) -> Path | None:
        if getattr(sys, "frozen", False):
            return None
        configured = os.environ.get("ARCHIPELAGO_SOURCE")
        candidate = Path(configured).expanduser() if configured else self.application_dir.parent / "Archipelago"
        return candidate.resolve() if (candidate / "CommonClient.py").is_file() else None

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
        if self.supervisor is not None and self.supervisor.running:
            raise RuntimeError("disconnect the current bridge worker before connecting again")
        game = Path(game_root).expanduser().resolve()
        saves = Path(saves_root).expanduser().resolve()
        if not (game / "DOOMEternalx64vk.exe").is_file() or not (game / "base").is_dir():
            raise ValueError("select the DOOM Eternal directory containing DOOMEternalx64vk.exe")
        if not saves.is_dir():
            raise ValueError("select the DOOM Eternal save base directory")
        self.save_config(
            {
                "server_address": endpoint.strip(),
                "slot": slot.strip(),
                "game_root": str(game),
                "doom_base_dir": str(game / "base"),
                "save_games_dir": str(saves),
            }
        )
        profile = hashlib.sha256(
            f"{endpoint.strip()}\0{slot.strip()}\0{self.state_dir}".encode()
        ).hexdigest()
        self.supervisor = BridgeSupervisor(
            entrypoint=self._entrypoint(),
            application_dir=self.application_dir,
            config_path=self.config_path,
            profile_id=profile,
            event_sink=self._worker_event,
            log_sink=self._worker_log,
            archipelago_source=self._archipelago_source(),
        )
        self.supervisor.start(
            endpoint=endpoint.strip(),
            player=slot.strip(),
            password=password,
        )
        self.emit("connecting", endpoint=endpoint.strip(), slot=slot.strip())

    def process_event(self, event: dict[str, object]) -> None:
        if event.get("type") == "connected":
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
        if self.supervisor is not None:
            self.supervisor.stop()
            self.supervisor = None
        self.emit("disconnected", intentional=True)

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
