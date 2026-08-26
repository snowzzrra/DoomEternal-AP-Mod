"""Application state and orchestration for standalone launcher."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

from doom_eap.content.options_foundation import load_options_schema, save_player_yaml

from .launcher_core import LaunchWorkflow, RoomSnapshot
from .launcher_doctor import DoctorReport, LauncherDoctor, write_support_bundle
from .launcher_integration import (
    IntegratedLaunchWorkflow,
    IntegratedSetupRecord,
    RoomSetupCoordinator,
    installed_package_issue_payload,
    setup_failure_payload,
)
from .launcher_native_health import NativeHealthReader, doom_base_dir_from_config
from .launcher_platform import (
    AMMO_HOTKEY_STATE_FILENAME,
    AMMO_HOTKEY_STATE_HEADER,
    AMMO_REFILL_BIND_COMMAND,
    GameLinkResult,
    SteamInstallationLocator,
    cleanup_legacy_doomeap_cfg,
    cleanup_stale_doom_config_bind,
    detect_doom_processes,
    launch_doom_via_steam,
    launcher_user_paths,
    migrate_legacy_launcher_data,
    probe_meathook,
    probe_runtime_prerequisites,
    read_handshake_probe,
    redact_secrets,
    resolve_doom_config_path,
    validate_game_root,
    validate_save_directory,
    write_ammo_refill_hotkey_state,
)
from .launcher_supervisor import BridgeSupervisor


AMMO_REFILL_KEYBIND_CONFIG = "ammo_refill_keybind"
DEFAULT_AMMO_REFILL_KEYBIND = "F9"
AMMO_REFILL_SUPPORTED_KEY_TOKENS = frozenset(
    {
        *(f"F{number}" for number in range(1, 13)),
        *(chr(code) for code in range(ord("A"), ord("Z") + 1)),
        *(str(number) for number in range(10)),
        "Space",
        "Tab",
        "Backspace",
        "Insert",
        "Delete",
        "Home",
        "End",
        "PageUp",
        "PageDown",
        "Up",
        "Down",
        "Left",
        "Right",
    }
)


def normalize_ammo_refill_keybind(keybind: str) -> str:
    """Accept one proven DOOM key token or an unbound value."""
    value = str(keybind).strip()
    if not value or value.casefold() == "unbound":
        return ""
    if "\n" in value or "\r" in value or "+" in value or len(value) > 32:
        raise ValueError("Ammo Refill keybind must be one simple key")
    folded = {token.casefold(): token for token in AMMO_REFILL_SUPPORTED_KEY_TOKENS}
    canonical = folded.get(value.casefold())
    if canonical is None:
        raise ValueError("Ammo Refill keybind uses an unsupported physical key")
    return canonical


def application_directory() -> Path:
    """Use distribution directory for external package resources."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    module_dir = Path(__file__).resolve().parent
    return module_dir if (module_dir / "data").is_dir() else Path(__file__).resolve().parents[2]


def bundle_directory() -> Path:
    """Return root directory for bundled resources (sys._MEIPASS in frozen runtime)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return application_directory()


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
        self.diagnostic_history: deque[str] = deque(maxlen=500)
        self.config = self._load_config()
        configured_keybind = self.config.get(AMMO_REFILL_KEYBIND_CONFIG)
        if configured_keybind is None or not isinstance(configured_keybind, str):
            normalized_keybind = DEFAULT_AMMO_REFILL_KEYBIND
        else:
            try:
                normalized_keybind = normalize_ammo_refill_keybind(configured_keybind)
            except ValueError:
                normalized_keybind = DEFAULT_AMMO_REFILL_KEYBIND
        if configured_keybind != normalized_keybind:
            self.config[AMMO_REFILL_KEYBIND_CONFIG] = normalized_keybind
            self._persist_config()
        self.options_schema = load_options_schema(
            self.client_dir / "data" / "options_schema.json"
        )
        self.state = LauncherState.IDLE
        self.connected_room = False
        self.supervisor: BridgeSupervisor | None = None
        self._lifecycle_lock = threading.Lock()
        self._pending_connect: dict[str, str] | None = None
        self.last_setup: IntegratedSetupRecord | None = None
        self.last_setup_failure: dict[str, object] | None = None
        self.last_room_package_issue: dict[str, object] | None = None
        self.session_start_time = time.time()
        self._consent_lock = threading.Lock()
        self._consent_requests: dict[str, tuple[threading.Event, list[bool]]] = {}
        self._confirmation_lock = threading.Lock()
        self._confirmation_requests: dict[str, tuple[threading.Event, list[bool]]] = {}
        self._uninstall_confirmation_lock = threading.Lock()
        self._uninstall_confirmation_requests: dict[str, tuple[threading.Event, list[bool]]] = {}
        self.workflow = IntegratedLaunchWorkflow(
            self.client_dir,
            self.state_dir,
            self.config_path,
            event_sink=self._setup_event,
            consent=self._request_consent,
            confirmation=self._request_installation_confirmation,
            uninstall_confirmation=self._request_uninstall_confirmation,
        )
        self.setup = RoomSetupCoordinator(
            self.workflow,
            self._setup_event,
            self._setup_result,
        )
        self._native_health_reader: NativeHealthReader | None = None
        self._last_native_health: dict[str, object] | None = None
        self._native_client_process: subprocess.Popen | None = None
        self._last_game_running: bool = False

    def _native_client_running(self) -> bool:
        with self._lifecycle_lock:
            if self._native_client_process is None:
                return False
            poll = self._native_client_process.poll()
            if poll is not None:
                exit_code = poll
                self._native_client_process = None
                self.emit("native_client_exited", returncode=exit_code)
                return False
            return True

    def _ensure_native_client(self, *, platform: str | None = None) -> bool:
        target_platform = platform if platform is not None else os.name
        if target_platform != "nt":
            return False
        with self._lifecycle_lock:
            if not self.connected_room:
                return False
            if self._native_client_process is not None:
                if self._native_client_process.poll() is None:
                    return True
                exit_code = self._native_client_process.poll()
                self._native_client_process = None
                self.emit("native_client_exited", returncode=exit_code)

            game_root = self.config.get("game_root") or self.config.get("doom_base_dir")
            if not game_root:
                return False
            try:
                root = validate_game_root(Path(str(game_root)))
            except Exception:
                return False

            client_exe = self.client_dir / "ap_client.exe"
            if not client_exe.is_file():
                self._record_diagnostic(f"native client executable missing at {client_exe}")
                return False

            meathook = probe_meathook(root)
            if not meathook.ok:
                return False

            LaunchWorkflow.write_client_config(self.client_dir, runtime_config=self.config)
            command = [str(client_exe), str(root)]
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            try:
                self._native_client_process = subprocess.Popen(
                    command,
                    cwd=str(root),
                    creationflags=creationflags,
                )
                self.emit("native_client_started", path=str(client_exe), game_root=str(root))
                return True
            except Exception as error:
                self._native_client_process = None
                self.emit("native_client_start_failed", message=str(error))
                return False

    def _stop_native_client(self) -> None:
        with self._lifecycle_lock:
            process = self._native_client_process
            self._native_client_process = None
        if process is None:
            return
        if process.poll() is not None:
            return
        try:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        except Exception:
            pass
        self.emit("native_client_stopped")

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

    def ensure_ammo_refill_keybind(self, *, force_check: bool = False) -> Path | None:
        """Write AP-owned Ammo Refill hotkey state and clean stale config binds."""
        configured_keybind = self.config.get(AMMO_REFILL_KEYBIND_CONFIG, DEFAULT_AMMO_REFILL_KEYBIND)
        if not isinstance(configured_keybind, str):
            normalized_keybind = DEFAULT_AMMO_REFILL_KEYBIND
        else:
            try:
                normalized_keybind = normalize_ammo_refill_keybind(configured_keybind)
            except ValueError:
                normalized_keybind = DEFAULT_AMMO_REFILL_KEYBIND

        base_dir = doom_base_dir_from_config(self.config)
        if base_dir is not None:
            cleanup_legacy_doomeap_cfg(base_dir)

        is_running = self.is_game_running()
        cleanup_stale_doom_config_bind(self.config, is_game_running=is_running)

        state_file = write_ammo_refill_hotkey_state(base_dir, normalized_keybind)
        logger.info(
            "AMMO_HOTKEY_CONFIG path=%s token=%s state=%s",
            state_file,
            normalized_keybind or "UNBOUND",
            "loaded" if normalized_keybind else "disabled",
        )
        self._record_diagnostic(
            f"AMMO_HOTKEY_CONFIG path={state_file} token={normalized_keybind or 'UNBOUND'} state={'loaded' if normalized_keybind else 'disabled'}"
        )

        self.emit(
            "ammo_refill_keybind_status",
            state="configured" if normalized_keybind else "unbound",
            configured_key=normalized_keybind,
            path=str(state_file) if state_file else None,
        )
        return state_file

    def ensure_ammo_refill_config(self) -> Path | None:
        """Ensure launcher-managed Ammo Refill configuration."""
        return self.ensure_ammo_refill_keybind()

    def save_config(self, updates: dict[str, object]) -> None:
        self.config.update(updates)
        self._persist_config()
        LaunchWorkflow.write_client_config(
            self.client_dir,
            runtime_config=self.config,
        )

    def discover(self) -> dict[str, object]:
        found: dict[str, object] = {"platform": "windows" if os.name == "nt" else "linux"}
        installations, sentinel = SteamInstallationLocator().inspect_discovery()
        found["game_discovery"] = asdict(sentinel)
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

    def launch_game(self, *, platform: str | None = None) -> str:
        """Launch through Steam URL handler after validating live runtime prerequisites."""
        target_platform = platform if platform is not None else os.name
        game_root = self.config.get("game_root") or self.config.get("doom_base_dir")
        if not game_root:
            raise RuntimeError("DOOM Eternal installation is not configured.")
        root = validate_game_root(Path(str(game_root)))
        prereqs = probe_runtime_prerequisites(root, self.client_dir, self.config)
        if not prereqs.ok:
            failed = [c.message for c in prereqs.checks if not c.ok]
            raise RuntimeError(f"Cannot launch DOOM Eternal: {'; '.join(failed)}")
        if target_platform == "nt":
            if not self._ensure_native_client(platform=target_platform):
                raise RuntimeError("Could not start Doom Eternal Archipelago client runtime (ap_client.exe).")
        url = launch_doom_via_steam()
        self.emit("steam_launch_requested", url=url)
        return url

    def install_game_link(self, force_repair: bool = False) -> GameLinkResult:
        """Acquire, verify, and install supported Game Link runtime library."""
        game_root = self.config.get("game_root") or self.config.get("doom_base_dir")
        if not game_root:
            raise RuntimeError("DOOM Eternal installation is not configured.")
        root = validate_game_root(Path(str(game_root)))
        self.ensure_ammo_refill_config()
        local_key = "meathook_dll"
        local_value = self.config.get(local_key)
        local_artifact = Path(str(local_value)).expanduser() if local_value else None
        result = self.workflow.ensure_game_link(
            root,
            local_artifact=local_artifact,
            force_repair=force_repair,
        )
        self.emit(
            "game_link_status",
            state=result.state,
            message=result.message,
            path=result.path,
            sha256=result.sha256,
            ownership=result.ownership,
            backup_path=result.backup_path,
        )
        return result

    def probe_handshake(self) -> dict[str, object]:
        base = self.config.get("doom_base_dir")
        if not base:
            result = {"status": "unavailable", "reason": "DOOM Eternal base directory is not configured"}
        else:
            result = read_handshake_probe(Path(str(base)).expanduser() / "ap_gameplay_save.state")
        self.emit("handshake_probe", **result)
        return dict(result)

    def read_native_health(self, *, force: bool = False) -> dict[str, object]:
        """Return normalized native AP health without emitting launcher activity."""
        base = doom_base_dir_from_config(self.config)
        if base is None:
            result = {
                "state": "not_ready",
                "ready": False,
                "degraded": False,
                "reason": "base_directory_unconfigured",
            }
            return result
        path = base / "ap_rpc_health.state"
        if self._native_health_reader is None or self._native_health_reader.path != path:
            self._native_health_reader = NativeHealthReader(path)
        result = self._native_health_reader.read(force=force).document()
        if result.get("native_state") is not None:
            self._last_native_health = dict(result)
        return result

    def native_health(self, *, force: bool = False) -> dict[str, object]:
        return self.read_native_health(force=force)

    def _live_support_diagnostics(self) -> dict[str, object]:
        """Expose live ownership/process facts without bridge credentials."""
        supervisor = self.supervisor
        if supervisor is None:
            supervisor_details: dict[str, object] = {
                "status": "unavailable",
                "running": False,
                "reason": "supervisor_not_created",
            }
        else:
            supervisor_details = {
                "status": supervisor.state.value.lower(),
                "running": supervisor.running,
                "last_error": dict(supervisor.last_error) if supervisor.last_error else None,
            }
        direct = self.read_native_health(force=True)
        expected_native_path = None
        base = doom_base_dir_from_config(self.config)
        if base is not None:
            expected_native_path = str(base / "ap_rpc_health.state")
        last_known = self._last_native_health
        if (
            last_known is not None
            and (
                expected_native_path is None
                or str(last_known.get("path", "")) != expected_native_path
            )
        ):
            last_known = None
        if direct.get("native_state") is not None:
            native = {
                "source": "direct",
                "evidence": "direct",
                "health": direct,
                "direct": direct,
            }
        elif last_known is not None:
            native = {
                "source": "last_known",
                "evidence": "last_known",
                "health": dict(last_known),
                "direct": direct,
            }
        else:
            native = {
                "source": "unknown",
                "evidence": "unavailable",
                "health": direct,
                "direct": direct,
            }
        return {
            "supervisor": supervisor_details,
            "native_rpc": native,
            "config_paths": {
                "application_dir": str(self.application_dir),
                "client_dir": str(self.client_dir),
                "config_file": str(self.config_path),
                "state_dir": str(self.state_dir),
            },
        }

    def run_doctor(self) -> DoctorReport:
        report = LauncherDoctor(
            config=self.config,
            paths=self.user_paths,
            config_path=self.config_path,
            last_setup_failure=self.last_setup_failure,
            last_room_package_issue=self.last_room_package_issue,
            live_support=self._live_support_diagnostics(),
        ).run()
        self.emit("doctor_report", report=report.document())
        return report

    def repair_preview(self):
        return LauncherDoctor(
            config=self.config,
            paths=self.user_paths,
            config_path=self.config_path,
            last_setup_failure=self.last_setup_failure,
            last_room_package_issue=self.last_room_package_issue,
        ).repair_preview()

    def apply_repair(self, action_key: str) -> str:
        """Apply selected Doctor action. Room changes require connected-room setup."""
        doctor = LauncherDoctor(
            config=self.config,
            paths=self.user_paths,
            config_path=self.config_path,
            last_setup_failure=self.last_setup_failure,
            last_room_package_issue=self.last_room_package_issue,
        )
        actions = {action.key: action for action in doctor.repair_preview()}
        action = actions.get(action_key)
        if action is None:
            raise ValueError("repair action is unavailable")
        if action_key == "archive_stale_install_record":
            backup = doctor.archive_stale_install_record()
            self.emit("repair_complete", action=action_key, backup=str(backup))
            return str(backup)
        if action_key in {"rebuild_room_package", "update_room_package", "reinstall_room_mod"}:
            self.ensure_ammo_refill_config()
            if not self.setup.start(force=True):
                raise RuntimeError("connect to room before rebuilding its room package")
            self.emit("repair_started", action=action_key)
            return "Room package rebuild started; installed hash will be checked after setup."
        if action_key in {"install_game_link", "repair_game_link"}:
            result = self.install_game_link(force_repair=action_key == "repair_game_link")
            game_root = self.config.get("game_root") or self.config.get("doom_base_dir")
            root = validate_game_root(Path(str(game_root))) if game_root else None
            post_probe = probe_meathook(root)
            if not post_probe.ok:
                self.emit(
                    "repair_failed",
                    action=action_key,
                    state=result.state,
                    message=f"post-repair probe failed: {post_probe.message}",
                )
                raise RuntimeError(f"Game Link repair was not verified: {post_probe.message}")
            self.emit(
                "repair_complete",
                action=action_key,
                state="repaired",
                path=result.path,
                sha256=result.sha256,
                probe=post_probe.message,
            )
            return f"Game Link runtime repaired and verified: {post_probe.message}"
        raise ValueError("unsupported repair action")

    def create_support_bundle(self, destination: Path, *, logs: list[str] | None = None) -> Path:
        support_condump = self._request_support_condump()
        diagnostic_logs = [*self.diagnostic_history, *(logs or [])]
        bundle = write_support_bundle(
            destination,
            self.run_doctor(),
            logs=diagnostic_logs,
            config=self.config,
            paths=self.user_paths,
            application_dir=self.client_dir,
            session_start=self.session_start_time,
            last_setup_failure=self.last_setup_failure,
            support_condump=support_condump,
        )
        self.emit("support_bundle_ready", path=str(bundle))
        return bundle

    def _request_support_condump(self) -> dict[str, object]:
        """Attempt one diagnostic condump, recording closed-game availability."""
        requested_at = time.time()
        supervisor = self.supervisor
        supervisor_available = supervisor is not None and supervisor.running
        native_health = self.read_native_health(force=True)
        native_stopped = native_health.get("native_state") == "stopped"
        if native_stopped:
            return {
                "status": "unavailable",
                "reason": "game_closed",
                "message": "diagnostic condump unavailable: game not running",
                "requested_at": requested_at,
            }
        # Fresh native readiness is authoritative under Proton; avoid making
        # bounded diagnostics wait on a host-side executable-name probe.
        game_running = bool(native_health.get("ready")) if supervisor_available else self.is_game_running()
        if not game_running:
            return {
                "status": "unavailable",
                "reason": "game_closed",
                "message": "diagnostic condump unavailable: game not running",
                "requested_at": requested_at,
            }
        if not supervisor_available:
            return {
                "status": "unavailable",
                "reason": "bridge_worker_unavailable",
                "requested_at": requested_at,
            }
        assert supervisor is not None
        save_value = self.config.get("save_games_dir")
        if not save_value:
            return {
                "status": "unavailable",
                "reason": "saved_games_path_unconfigured",
                "requested_at": requested_at,
            }
        try:
            save_dir = Path(str(save_value)).expanduser()
        except (OSError, TypeError, ValueError, RuntimeError) as error:
            return {
                "status": "unavailable",
                "reason": "saved_games_path_invalid",
                "message": f"Saved Games path unavailable: {type(error).__name__}: {error}",
                "requested_at": requested_at,
            }
        previous_mtime: dict[Path, float] = {}
        try:
            for candidate in [save_dir / "AP_SUPPORT_FILE.txt", *sorted(save_dir.glob("AP_SUPPORT_FILE*.txt"))]:
                try:
                    previous_mtime[candidate] = candidate.stat().st_mtime
                except (OSError, ValueError, RuntimeError):
                    pass
        except (OSError, ValueError, RuntimeError):
            pass
        try:
            supervisor.request_support_condump()
        except Exception as error:
            return {
                "status": "unavailable",
                "reason": "request_failed",
                "message": str(error),
                "requested_at": requested_at,
            }

        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            candidates = [save_dir / "AP_SUPPORT_FILE.txt"]
            try:
                candidates.extend(sorted(save_dir.glob("AP_SUPPORT_FILE*.txt")))
            except (OSError, ValueError, RuntimeError):
                pass
            for candidate in candidates:
                try:
                    stat = candidate.stat()
                except (OSError, ValueError, RuntimeError):
                    continue
                if stat.st_mtime >= requested_at - 1.0 and stat.st_mtime > previous_mtime.get(candidate, 0.0):
                    return {
                        "status": "available",
                        "path": str(candidate),
                        "requested_at": requested_at,
                        "mtime": stat.st_mtime,
                    }
            time.sleep(0.1)
        return {
            "status": "pending",
            "reason": "game_diagnostic_condump_not_observed_within_timeout",
            "requested_at": requested_at,
        }

    def emit(self, kind: str, **payload: object) -> None:
        event = {"type": kind, **payload}
        self._record_event(event)
        self.events.put(event)

    def _record_diagnostic(self, text: object) -> None:
        sanitized = redact_secrets(str(text)).replace("\r", " ").replace("\n", " ").strip()
        if sanitized:
            self.diagnostic_history.append(sanitized[:1000])

    def _record_event(self, event: dict[str, object]) -> None:
        kind = str(event.get("type", "event"))
        if "heartbeat" in kind.casefold():
            return
        fields = []
        for key in (
            "endpoint", "slot", "seed_name", "state", "code", "reason", "message",
            "raw_message", "technical_message", "failure_domain", "recovery_action",
        ):
            if key in event and event[key] not in (None, ""):
                fields.append(f"{key}={event[key]}")
        self._record_diagnostic(f"{kind}: {' | '.join(fields) or 'received'}")

    def _worker_event(
        self, supervisor: BridgeSupervisor, event: dict[str, object]
    ) -> None:
        kind = str(event.get("type", ""))
        if kind == "setup_failed" and not event.get("failure_domain"):
            event = {
                **event,
                **setup_failure_payload(
                    RuntimeError(str(event.get("message", "setup failed"))),
                    phase="game_setup",
                ),
            }
        stop_failed_worker = False
        pending: dict[str, str] | None = None
        emit_event = True
        intentional_disconnect = False
        stop_native = False
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
                    self.last_setup_failure = None
                    self.last_room_package_issue = None
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
                stop_native = True
            elif kind in {"client_stopping", "disconnected"}:
                if kind == "disconnected":
                    self.last_setup_failure = None
                    self.last_room_package_issue = None
                if (
                    self.state in {LauncherState.FAILED, LauncherState.DISCONNECTING}
                    or self._pending_connect is not None
                ):
                    emit_event = False
            elif kind == "worker_stopped":
                self.supervisor = None
                emit_event = False
                stop_native = True
                if self._pending_connect is not None:
                    pending = self._pending_connect
                    self._pending_connect = None
                    self.state = LauncherState.CONNECTING
                elif self.state is LauncherState.DISCONNECTING:
                    self.state = LauncherState.IDLE
                    intentional_disconnect = True
                elif self.state is not LauncherState.FAILED:
                    self.state = LauncherState.FAILED
        if stop_native:
            self._stop_native_client()
        if emit_event:
            self._record_event(event)
            self.events.put(event)
        if stop_failed_worker:
            supervisor.stop(emit_disconnected=False)
        if intentional_disconnect:
            self.emit("disconnected", intentional=True)
        if pending is not None:
            self._start_supervisor(pending)

    def _worker_log(self, text: str) -> None:
        if text:
            self._record_diagnostic(f"worker: {text}")
            self.events.put({"type": "log", "message": text})

    def _setup_event(self, kind: str, payload: dict[str, object]) -> None:
        if kind == "setup_failed":
            self.last_setup_failure = dict(payload)
            if payload.get("failure_domain") in {"room_package", "installed_room_package"}:
                self.last_room_package_issue = dict(payload)
        elif kind == "setup_ready":
            self.last_setup_failure = None
            self.last_room_package_issue = None
        self.emit(kind, **payload)
        if kind == "setup_ready" and payload.get("adapter_state") == "applied":
            self._ensure_native_client()

    def _setup_result(self, record: IntegratedSetupRecord) -> None:
        self.last_setup = record

    def _request_consent(self, spec) -> bool:
        request_id = uuid.uuid4().hex
        wait = threading.Event()
        answer: list[bool] = []
        with self._consent_lock:
            self._consent_requests[request_id] = (wait, answer)
        if spec.name == "Meathook":
            purpose = "Game Link runtime library"
            source = "GitHub / brongo"
        elif spec.name == "EternalModInjector":
            purpose = "Windows mod installation tools"
            source = "GameBanana / DOOM 2016+ Modding Community"
        else:
            purpose = "Mod installation tool"
            source = "GitHub"
        self.emit(
            "dependency_consent_required",
            request_id=request_id,
            name=spec.name,
            version=spec.version,
            url=spec.url,
            sha256=spec.sha256,
            purpose=purpose,
            source=source,
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

    def _request_installation_confirmation(self) -> bool:
        request_id = uuid.uuid4().hex
        wait = threading.Event()
        answer: list[bool] = []
        with self._confirmation_lock:
            self._confirmation_requests[request_id] = (wait, answer)
        self.emit(
            "installation_confirmation_required",
            request_id=request_id,
            message="Did the mod installation complete successfully in EternalModInjector?",
        )
        wait.wait(timeout=300.0)
        with self._confirmation_lock:
            self._confirmation_requests.pop(request_id, None)
        return bool(answer and answer[0])

    def resolve_installation_confirmation(self, request_id: str, confirmed: bool) -> None:
        with self._confirmation_lock:
            pending = self._confirmation_requests.get(request_id)
        if pending is None:
            return
        wait, answer = pending
        answer.append(bool(confirmed))
        wait.set()

    def _request_uninstall_confirmation(self) -> bool:
        request_id = uuid.uuid4().hex
        wait = threading.Event()
        answer: list[bool] = []
        with self._uninstall_confirmation_lock:
            self._uninstall_confirmation_requests[request_id] = (wait, answer)
        self.emit(
            "uninstall_confirmation_required",
            request_id=request_id,
            operation="uninstall",
            message="Did EternalModInjector finish uninstalling this room package successfully?",
        )
        wait.wait(timeout=300.0)
        with self._uninstall_confirmation_lock:
            self._uninstall_confirmation_requests.pop(request_id, None)
        return bool(answer and answer[0])

    def resolve_uninstall_confirmation(self, request_id: str, confirmed: bool) -> None:
        with self._uninstall_confirmation_lock:
            pending = self._uninstall_confirmation_requests.get(request_id)
        if pending is None:
            return
        wait, answer = pending
        answer.append(bool(confirmed))
        wait.set()

    def confirm_manual_installation(self) -> bool:
        """Confirm manual mod installation from the manual fallback state."""
        with self._lifecycle_lock:
            last_event = dict(self.setup._last_event) if self.setup._last_event else None
        if not last_event:
            raise RuntimeError("No connected room session is available.")
        self.ensure_ammo_refill_config()
        snapshot = RoomSnapshot.from_event(last_event)
        record = self.workflow.confirm_manual_installation(snapshot, str(last_event.get("endpoint") or ""))
        self.last_setup = record
        self.last_setup_failure = None
        self.last_room_package_issue = None
        self.emit(
            "setup_ready",
            manifest_hash=record.manifest_hash,
            randomize_dash=record.randomize_dash,
            adapter_state=record.adapter_state,
            message=record.adapter_message,
            steam_launch_option=record.steam_launch_option,
            new_install=record.new_install,
        )
        self._ensure_native_client()
        return True

    def _entrypoint(self) -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve()
        return self.client_dir / "doom_eap" / "launcher" / "launcher_app.py"

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
            self.emit(
                "setup_failed",
                code="bridge_start_failed",
                **setup_failure_payload(error, phase="game_setup"),
            )
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
        self.ensure_ammo_refill_config()
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
        event_type = event.get("type")
        if event_type == "connected":
            self.connected_room = True
            self.last_setup_failure = None
            self.last_room_package_issue = None
            self.setup.observe(event)
            try:
                from .launcher_core import RoomSnapshot

                snapshot = RoomSnapshot.from_event(event)
                state = self.workflow.install_state(snapshot)
                self.emit(
                    "room_install_state",
                    state=state.state,
                    manifest_hash=state.manifest_hash,
                    staged_mod=state.staged_mod,
                    steam_launch_option=state.steam_launch_option,
                    reason=state.reason,
                    readiness=state.readiness,
                    readiness_reason=state.readiness_reason,
                    **(
                        installed_package_issue_payload(state.reason)
                        if state.state == "update_required"
                        else {}
                    ),
                )
                if state.state == "update_required":
                    self.last_room_package_issue = {
                        "type": "room_install_state",
                        "state": state.state,
                        "reason": state.reason,
                        **installed_package_issue_payload(state.reason),
                    }
                if state.state == "already_installed" and state.readiness != "blocked":
                    self._ensure_native_client()
            except Exception as error:
                issue = setup_failure_payload(error, phase="room_snapshot")
                self.last_room_package_issue = {
                    "type": "room_install_state",
                    "state": "install_needed",
                    "reason": str(error),
                    **issue,
                }
                self.emit(
                    "room_install_state",
                    state="install_needed",
                    reason=f"could not verify installed room mod: {error}",
                    readiness="blocked",
                    readiness_reason=str(error),
                    **issue,
                )
        elif event_type == "setup_ready":
            if event.get("adapter_state") == "applied":
                self._ensure_native_client()

    def send_chat(self, text: str) -> None:
        if not text.strip():
            return
        with self._lifecycle_lock:
            connected = self.connected_room
            supervisor = self.supervisor
        if not connected or supervisor is None:
            raise RuntimeError("not connected")
        supervisor.send_chat(text)

    def poll_game_lifecycle(self) -> None:
        """Track game process lifecycle and clean stale config bindings on game exit."""
        current_running = self.is_game_running()
        if self._last_game_running and not current_running:
            cleanup_stale_doom_config_bind(self.config, is_game_running=False)
        self._last_game_running = current_running

    def set_ammo_refill_keybind(self, keybind: str) -> None:
        try:
            normalized = normalize_ammo_refill_keybind(keybind)
        except ValueError:
            normalized = DEFAULT_AMMO_REFILL_KEYBIND
        self.save_config({AMMO_REFILL_KEYBIND_CONFIG: normalized})
        self.ensure_ammo_refill_keybind()

    def request_ammo_refill(self) -> None:
        with self._lifecycle_lock:
            connected = self.connected_room
            supervisor = self.supervisor
        if not connected or supervisor is None or not supervisor.running:
            self.emit(
                "ammo_refill",
                status="blocked",
                message="Ammo Refill unavailable while disconnected",
            )
            return
        control = json.dumps({"type": "ammo_refill"}, separators=(",", ":"))
        try:
            supervisor.send_command(f"AP_CONTROL {control}")
        except Exception as error:
            self.emit("ammo_refill", status="error", message=str(error))

    def request_inventory_resync(self) -> None:
        with self._lifecycle_lock:
            connected = self.connected_room
            supervisor = self.supervisor
        if not connected:
            raise RuntimeError("not connected")
        if supervisor is None or not supervisor.running:
            raise RuntimeError("bridge worker is not running")
        try:
            supervisor.request_inventory_resync()
        except Exception as error:
            message = str(error).replace("\r", " ").replace("\n", " ")[:512]
            self.emit("inventory_resync", status="error", message=message)
            raise

    def disconnect(self) -> None:
        self._stop_native_client()
        supervisor: BridgeSupervisor | None
        with self._lifecycle_lock:
            if self.state is LauncherState.DISCONNECTING:
                return
            supervisor = self.supervisor
            self._pending_connect = None
            self.connected_room = False
            self.last_setup_failure = None
            self.last_room_package_issue = None
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
        self.ensure_ammo_refill_config()
        return self.setup.start(force=True)

    def prepare_setup(self) -> bool:
        self.ensure_ammo_refill_config()
        return self.setup.start()

    def reinstall_setup(self) -> bool:
        self.ensure_ammo_refill_config()
        return self.setup.start(force=True)

    def uninstall_setup(self) -> dict[str, object]:
        """Queue current room package uninstall on serialized setup worker."""
        with self._lifecycle_lock:
            last_event = dict(self.setup._last_event) if self.setup._last_event else None
            connected = self.connected_room
        if not connected or not last_event:
            raise RuntimeError("connect to room before uninstalling its mod")
        if not self.setup.submit_uninstall(last_event):
            payload = {
                "state": "attention",
                "message": "Another room setup or uninstall operation is already active.",
                "attention": True,
            }
            self.emit("uninstall_attention", **payload)
            return payload
        return {"state": "queued", "attention": False}

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

    def close(self) -> None:
        self._stop_native_client()
        self.disconnect()
