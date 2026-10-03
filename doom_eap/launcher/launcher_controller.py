"""Application state and orchestration for standalone launcher."""

from __future__ import annotations

import hashlib
import json
import uuid
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

from doom_eap.content.options_foundation import load_options_schema, save_player_yaml

from copy import deepcopy
from .launcher_workers import LauncherWorkers, LauncherWorkCancelled
from .launcher_repairs import apply_repair, install_game_link
from .launcher_interactions import LauncherInteractions
from .launcher_reporting import ScopedSupportReport, report_body, report_problem, save_report_draft, submission_endpoint, submit_report

from .connection_errors import enrich_connection_failure, validate_server_address
from .launcher_core import ROOM_SLOT_DEFAULTS, LaunchWorkflow, RoomSnapshot, release_identity
from .launcher_doctor import Diagnostic, DoctorReport, LauncherDoctor, write_support_bundle
from .launcher_integration import (
    IntegratedLaunchWorkflow,
    IntegratedSetupRecord,
    RoomSetupCoordinator,
    installed_package_issue_payload,
    setup_failure_payload,
)
from .launcher_native_health import NativeHealthReader, doom_base_dir_from_config
from .launcher_platform import (
    SavedGamesSelection,
    SteamInstallationLocator,
    cleanup_legacy_doomeap_cfg,
    cleanup_stale_doom_config_bind,
    detect_doom_processes,
    doom_saved_games_base,
    launch_doom_via_steam,
    launcher_user_paths,
    migrate_legacy_launcher_data,
    probe_meathook,
    probe_runtime_prerequisites,
    publish_file,
    read_handshake_probe,
    redact_secrets,
    select_saved_games_dir,
    validate_game_root,
    validate_save_directory,
    read_ap_hotkey_state,
    write_ap_hotkey_states,
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
    value = {"pgup": "PageUp", "pgdn": "PageDown"}.get(value.casefold(), value)
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
        self.bundle_dir = bundle_directory()
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
        if not self.config.get("client_state_file"):
            receipt_root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) if os.name == "nt" else Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
            self.config["client_state_file"] = str(receipt_root / "doom-eternal-ap/client_state.json")
        for key, default, special in ((AMMO_REFILL_KEYBIND_CONFIG, "F9", False), ("special_toggle_keybind", "F10", True)):
            configured = self.config.get(key)
            if configured is None:
                configured = read_ap_hotkey_state(doom_base_dir_from_config(self.config), special=special)
            self.config[key] = normalize_ammo_refill_keybind(default if configured is None else configured)
        self._persist_config()
        self.options_schema = load_options_schema(
            self.client_dir / "data" / "options_schema.json"
        )
        self.state = LauncherState.IDLE
        self.connected_room = False
        self.item_history_status: dict[str, object] | None = None
        self.supervisor: BridgeSupervisor | None = None
        self._lifecycle_lock = threading.Lock()
        self._condump_lock = threading.Lock()
        self._condump_pending = None
        self._pending_connect: dict[str, str] | None = None
        self.last_setup: IntegratedSetupRecord | None = None
        self.last_setup_failure: dict[str, object] | None = None
        self.last_connection_error: dict[str, object] | None = None
        self.connection_attempt_id = 0
        self._supervisor_attempt = 0
        self.last_room_package_issue: dict[str, object] | None = None
        self.session_start_time = time.time()
        self.workers = LauncherWorkers(self._job_failed)
        self.interactions = LauncherInteractions(lambda kind, payload: self.emit(kind, **payload))
        self.workflow = IntegratedLaunchWorkflow(
            self.client_dir,
            self.state_dir,
            self.config_path,
            data_dir=self.user_paths.data_dir,
            event_sink=self._setup_event,
            consent=self._request_consent,
            confirmation=self._request_installation_confirmation,
            uninstall_confirmation=self._request_uninstall_confirmation,
        )
        self.setup = RoomSetupCoordinator(
            self.workflow,
            self._setup_event,
            self._setup_result,
            self.workers,
            self.interactions,
        )
        self._native_health_reader: NativeHealthReader | None = None
        self._last_native_health: dict[str, object] | None = None
        self._native_client_process: subprocess.Popen | None = None
        self._native_helper_startup: dict[str, object] = {
            "process_start_status": "not_attempted",
            "winerror": None,
            "normalized_category": None,
            "disappeared_after_validation": False,
            "latest_startup_error": None,
        }
        self._last_game_running: bool = False
        self._game_lifecycle_sample: bool | None = None
        self._game_lifecycle_sample_lock = threading.Lock()
        self._game_lifecycle_stop = threading.Event()
        self._game_lifecycle_thread: threading.Thread | None = None

    def _native_start_failure(
        self,
        *,
        reason: str,
        technical_message: str,
        executable_path: Path,
        exit_code: int | None = None,
    ) -> None:
        if reason == "native_helper_missing":
            message = (
                "The Game integration helper is missing and may have been quarantined. "
                "Check Windows Security Protection history, repair or reinstall DoomEAP, "
                "then retry. Generate a Support Report if the problem continues."
            )
        elif reason == "native_helper_application_control_blocked":
            message = (
                "Windows application control blocked the Game integration helper. "
                "Generate a Support Report and ask the device administrator or DoomEAP "
                "maintainer to review the signed release artifact."
            )
        elif reason == "native_helper_removed_after_validation":
            message = (
                "The Game integration helper disappeared while Windows was starting it and "
                "may have been quarantined. Check Windows Security Protection history, "
                "repair or reinstall DoomEAP, then retry."
            )
        elif reason == "native_helper_access_denied":
            message = (
                "Windows denied access to the Game integration helper. Windows Security "
                "or antivirus software may have blocked it. Check Protection history, "
                "repair or reinstall DoomEAP, then retry. Generate a Support Report if "
                "the problem continues."
            )
        elif reason == "immediate_exit":
            message = (
                "The Game integration helper stopped immediately before Game Link became "
                "ready. Windows Security or antivirus software may have blocked it. "
                "Retry once, then generate a Support Report if the problem continues."
            )
        else:
            message = (
                "Windows could not create the Game integration helper process. "
                "Retry once, then generate a Support Report if the problem continues."
            )
        detail = (
            f"Game integration helper startup failed: reason={reason} "
            f"path={executable_path} detail={technical_message}"
        )
        if exit_code is not None:
            detail += f" exit_code={exit_code}"
        self._record_diagnostic(detail)
        self.emit(
            "native_client_start_failed",
            title="Game integration helper could not start",
            message=message,
            reason=reason,
            executable_path=str(executable_path),
            technical_message=technical_message,
            exit_code=exit_code,
        )

    @staticmethod
    def _classify_native_start_error(error: Exception, *, helper_exists: bool) -> tuple[str, bool]:
        winerror = getattr(error, "winerror", None)
        disappeared = (isinstance(error, FileNotFoundError) or winerror in {2, 3}) and not helper_exists
        if winerror == 4556:
            return "native_helper_application_control_blocked", False
        if disappeared:
            return "native_helper_removed_after_validation", True
        if isinstance(error, PermissionError) or winerror == 5:
            return "native_helper_access_denied", False
        return "native_helper_process_creation_failed", False

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

    def _ensure_native_client(self, *, platform: str | None = None, generation: int | None = None) -> bool:
        target_platform = platform if platform is not None else os.name
        if target_platform != "nt":
            return False
        with self._lifecycle_lock:
            if generation is not None and not self.workers.accepts(generation):
                return False
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
                self._native_helper_startup.update({
                    "expected_path": str(client_exe), "present": False, "sha256": None,
                    "process_start_status": "failed", "winerror": None,
                    "normalized_category": "native_helper_missing",
                    "disappeared_after_validation": False,
                    "latest_startup_error": "expected helper is missing; it may have been quarantined",
                })
                self._native_start_failure(
                    reason="native_helper_missing",
                    technical_message=f"file not found: {client_exe}; it may have been quarantined",
                    executable_path=client_exe,
                )
                return False

            try:
                helper_sha256 = hashlib.sha256(client_exe.read_bytes()).hexdigest()
            except OSError:
                helper_sha256 = None
            self._native_helper_startup.update({
                "expected_path": str(client_exe), "present": True, "sha256": helper_sha256,
                "process_start_status": "validated", "winerror": None,
                "normalized_category": None, "disappeared_after_validation": False,
                "latest_startup_error": None,
            })

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
                time.sleep(0.15)
                exit_code = self._native_client_process.poll()
                if exit_code is not None:
                    health = self.read_native_health(force=True)
                    self._native_client_process = None
                    if health.get("native_state") in {"starting", "ready", "unavailable"}:
                        self.emit(
                            "native_client_already_running",
                            pid=health.get("pid"),
                            native_state=health.get("native_state"),
                        )
                        return True
                    self._native_helper_startup.update({
                        "process_start_status": "failed", "normalized_category": "immediate_exit",
                        "latest_startup_error": "process exited before publishing current native health",
                    })
                    self._native_start_failure(
                        reason="immediate_exit",
                        technical_message="process exited before publishing current native health",
                        executable_path=client_exe,
                        exit_code=exit_code,
                    )
                    return False
                self.emit("native_client_started", path=str(client_exe), game_root=str(root))
                self._native_helper_startup["process_start_status"] = "started"
                return True
            except Exception as error:
                self._native_client_process = None
                winerror = getattr(error, "winerror", None)
                category, disappeared = self._classify_native_start_error(
                    error, helper_exists=client_exe.is_file()
                )
                technical_message = (
                    f"{type(error).__name__}: {error}"
                    + (f" (winerror={winerror})" if winerror is not None else "")
                ).replace("\r", " ").replace("\n", " ")[:512]
                self._native_helper_startup.update({
                    "present": client_exe.is_file(), "process_start_status": "failed",
                    "winerror": winerror, "normalized_category": category,
                    "disappeared_after_validation": disappeared,
                    "latest_startup_error": technical_message,
                })
                self._native_start_failure(
                    reason=category,
                    technical_message=technical_message,
                    executable_path=client_exe,
                )
                return False

    def _stop_native_client(self) -> None:
        if hasattr(self, "workflow") and not self.workflow.session_owner.can_close():
            return
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
        publish_file(temporary, self.config_path, operation="launcher_config_publish")

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

        special = normalize_ammo_refill_keybind(str(self.config.get("special_toggle_keybind", "F10")))
        if normalized_keybind and normalized_keybind == special:
            raise ValueError("Ammo Refill and Special Weapon toggle must use different keys")
        state_file = write_ap_hotkey_states(base_dir, normalized_keybind, special)
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

    def resolve_saved_games_dir(
        self,
        *,
        configured: object = None,
        game_root: Path | str | None = None,
        known_base: Path | str | None = None,
    ) -> SavedGamesSelection:
        """Resolve the single effective Saved Games base with self-heal evidence.

        A configured path with valid .../id Software/DOOMEternal/base shape is
        preserved. A stale configured path (launcher/client directory, game
        installation, wrong shape, or missing) never becomes authority: the
        canonical Known-Folder/Proton base wins, then a single live-writer
        candidate, otherwise selection fails closed.
        """
        value = configured if configured is not None else self.config.get("save_games_dir")
        root_value = game_root
        if root_value is None:
            for key in ("game_root", "doom_base_dir"):
                candidate = self.config.get(key)
                if candidate and str(candidate):
                    root_value = candidate
                    break
        root_path: Path | None = None
        if root_value is not None:
            try:
                root_path = Path(str(root_value)).expanduser()
                if root_path.name.casefold() == "base":
                    root_path = root_path.parent
            except (OSError, TypeError, ValueError, RuntimeError):
                root_path = None
        known = Path(str(known_base)).expanduser() if known_base is not None else None
        if known is None:
            try:
                known = doom_saved_games_base(game_root=root_path)
            except (OSError, TypeError, ValueError, RuntimeError):
                known = None
        client_dir = self.client_dir if self.client_dir != self.application_dir else None
        return select_saved_games_dir(
            str(value) if value is not None and str(value) else None,
            known_base=known,
            app_dir=self.application_dir,
            client_dir=client_dir,
            game_root=root_path,
            game_running=self.is_game_running(),
        )

    def discover(self) -> dict[str, object]:
        found: dict[str, object] = {"platform": "windows" if os.name == "nt" else "linux"}
        installations, sentinel = SteamInstallationLocator().inspect_discovery()
        found["game_discovery"] = asdict(sentinel)
        game_root_value: Path | None = None
        if len(installations) == 1:
            installation = installations[0]
            game_root_value = installation.game_root
            found["game_root"] = str(installation.game_root)
            found["doom_base_dir"] = str(installation.game_root / "base")
        elif len(installations) > 1:
            found["ambiguous_game_roots"] = [str(item.game_root) for item in installations]

        selection = self.resolve_saved_games_dir(game_root=game_root_value)
        if selection.path is not None:
            found["save_games_dir"] = str(selection.path)
        if selection.repaired and selection.path is not None:
            found["save_games_repair"] = {
                "previous_path": selection.previous_path,
                "selected_path": str(selection.path),
                "source": selection.source,
                "reason": selection.reason,
            }
            self.emit(
                "SAVED_GAMES_PATH_REPAIRED",
                previous_path=selection.previous_path,
                selected_path=str(selection.path),
                source=selection.source,
                reason=selection.reason,
            )

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
        """Prepare process-session admission on the shared worker before opening Steam."""
        event = self.setup.current_event
        if not self.connected_room or not event:
            raise RuntimeError("Connect to an AP room before playing")
        snapshot = RoomSnapshot.from_event(event)
        configuration = dict(self.config)
        generation = self.workers.generation

        def operation(job):
            job.check()
            root = validate_game_root(Path(str(configuration.get("game_root") or configuration.get("doom_base_dir"))))
            prerequisites = probe_runtime_prerequisites(root, self.client_dir, configuration)
            if not prerequisites.ok:
                raise RuntimeError("Cannot play: " + "; ".join(check.message for check in prerequisites.checks if not check.ok))
            workflow = self.workflow.for_job(job, self._setup_event, self.interactions.for_job(job))
            installed = workflow.install_state(snapshot)
            if installed.state != "already_installed":
                raise RuntimeError("Prepare the room package before playing")
            status = workflow.session_owner.prepare(snapshot, configuration)
            job.check()
            if status["state"] != "prelaunch_ready":
                raise RuntimeError("An AP game process is already active or requires attention")
            if (platform or os.name) == "nt" and not self._ensure_native_client(generation=generation):
                raise RuntimeError("The game integration helper could not start")
            self.emit("ap_session_status", **status)
            url = launch_doom_via_steam()
            self.emit("steam_launch_requested", url=url)

        if not self.workers.submit("launch_game", operation, generation=generation):
            raise RuntimeError("A game launch is already being prepared")
        return "queued"




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
        session = dict(self.workflow.session_owner.status)
        result["session"] = session
        if result.get("ready") and not session.get("ready"):
            result.update(state="not_ready", ready=False, reason=session["state"])
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
            "ap_session": dict(self.workflow.session_owner.status),
            "native_diagnostics": self._native_diagnostics(),
            "native_helper_startup": dict(self._native_helper_startup),
            "config_paths": {
                "application_dir": str(self.application_dir),
                "client_dir": str(self.client_dir),
                "config_file": str(self.config_path),
                "state_dir": str(self.state_dir),
            },
        }

    def _native_diagnostics(self) -> dict:
        from .sentinel_diagnostics import collect
        session = dict(self.workflow.session_owner.status)
        if not session.get("pid") and os.name == "nt":
            from doom_eap.runtime.observer_lifecycle import windows_game_processes
            processes = windows_game_processes()
            root = self.config.get("game_root") or self.config.get("doom_base_dir")
            if root and processes and len(processes) == 1:
                game = Path(str(root)).resolve()
                if game.name.casefold() == "base":
                    game = game.parent
                if os.path.normcase(processes[0]["path"]) == os.path.normcase(str(game / "DOOMEternalx64vk.exe")):
                    session.update(pid=processes[0]["pid"], process_created=processes[0]["created"])
        return collect(self.config, session)

    def run_doctor(self, *, job=None) -> DoctorReport:
        if job is not None:
            job.check()
        report = LauncherDoctor(
            config={
                **self.config,
                "application_dir": str(self.application_dir),
                "client_dir": str(self.client_dir),
            },
            paths=self.user_paths,
            config_path=self.config_path,
            last_setup_failure=self.last_setup_failure,
            last_room_package_issue=self.last_room_package_issue,
            live_support=self._live_support_diagnostics(),
        ).run()
        if job is None:
            self.emit("doctor_report", report=report.document())
        else:
            self._setup_event("doctor_report", job.event({"report": report.document()}))
        return report

    def request_doctor(self, *, preview: bool = False) -> bool:
        with self._lifecycle_lock:
            generation = self.workers.generation
            config = deepcopy(self.config)
            failure = deepcopy(self.last_setup_failure)
            issue = deepcopy(self.last_room_package_issue)

        def operation(job):
            try:
                doctor = LauncherDoctor(
                    config={**config, "application_dir": str(self.application_dir), "client_dir": str(self.client_dir)},
                    paths=self.user_paths, config_path=self.config_path,
                    last_setup_failure=failure, last_room_package_issue=issue,
                    live_support=None if preview else self._live_support_diagnostics(),
                )
                if preview:
                    payload = {"actions": tuple(asdict(action) for action in doctor.repair_preview())}
                else:
                    payload = {"report": doctor.run().document()}
                self._setup_event("repair_preview_result" if preview else "doctor_report", job.event(payload))
            except LauncherWorkCancelled:
                raise
            except Exception as error:
                self._setup_event("doctor_failed", job.event({"message": str(error), "preview": preview}))

        return self.workers.submit(("doctor", preview), operation, generation=generation)

    def _queue_repair_setup(self, job) -> bool:
        return self.setup.start(force=True, generation=job.generation)

    @property
    def operation_generation(self) -> int:
        return self.workers.generation

    def request_repair(self, action_key: str, *, integration_only: bool = False, generation: int | None = None) -> bool:
        with self._lifecycle_lock:
            generation = self.workers.generation if generation is None else generation
            if not self.workers.accepts(generation):
                return False
            last_failure = deepcopy(self.last_setup_failure)
            last_issue = deepcopy(self.last_room_package_issue)

        def operation(job):
            try:
                if integration_only or action_key in {
                    "install_game_link", "repair_game_link", "rebuild_room_package",
                    "update_room_package", "reinstall_room_mod",
                }:
                    with self._lifecycle_lock:
                        job.check()
                        self.ensure_ammo_refill_config()
                workflow = self.workflow.for_job(job, self._setup_event, self.interactions.for_job(job))
                config = workflow._config()

                def emit(kind, **payload):
                    self._setup_event(kind, job.event(payload))

                if integration_only:
                    result = install_game_link(config, workflow, emit, force_repair=True)
                    message = result.message
                else:
                    doctor = LauncherDoctor(
                        config=config, paths=self.user_paths, config_path=self.config_path,
                        last_setup_failure=last_failure, last_room_package_issue=last_issue,
                    )
                    message = apply_repair(
                        action_key, doctor=doctor, config=config, workflow=workflow, emit=emit,
                        start_room_setup=lambda: self._queue_repair_setup(job),
                    )
                emit("integration_repair_result" if integration_only else "ui_repair_result",
                     message=str(message), success=True)
            except LauncherWorkCancelled:
                raise
            except Exception as error:
                self._setup_event(
                    "integration_repair_result" if integration_only else "ui_repair_result",
                    job.event({"message": f"Repair error: {error}", "success": False}),
                )

        return self.workers.submit(("repair", action_key), operation, generation=generation)

    def request_support_bundle(self, destination: Path, *, logs: list[str]) -> bool:
        generation = self.workers.generation
        logs = list(logs)

        def operation(job):
            try:
                self.create_support_bundle(destination, logs=logs, job=job)
            except LauncherWorkCancelled:
                raise
            except Exception as error:
                self._setup_event("support_bundle_failed", job.event({"message": str(error)}))

        return self.workers.submit(("support_bundle", str(destination)), operation, generation=generation)

    def request_problem_report(self, *, logs: list[str]) -> bool:
        generation = self.workers.generation
        logs = list(logs)

        def operation(job):
            result = report_problem(ScopedSupportReport(self.create_support_bundle, job), logs=logs, job=job,
                                    draft_path=self.user_paths.data_dir / "reports" / "draft.json")
            self._setup_event("problem_report_ready", job.event({
                "message": result.message, "path": str(result.path) if result.path else None,
                "payload": result.payload, "submitted": result.submitted, "url": result.url,
            }))

        return self.workers.submit("problem_report", operation, generation=generation)

    def save_problem_report(self, payload):
        save_report_draft(self.user_paths.data_dir / "reports" / "draft.json", payload)

    def start_new_problem_report(self, *, logs):
        draft = self.user_paths.data_dir / "reports" / "draft.json"
        saved = json.loads(draft.read_text(encoding="utf-8"))
        if not saved.get("url"):
            raise ValueError("Resolve or retry the saved report before starting another.")
        archived = draft.with_name(str(uuid.UUID(saved["payload"]["idempotency_key"])) + ".json")
        publish_file(draft, archived, operation="report_archive_publish")
        return self.request_problem_report(logs=logs)

    def request_report_submission(self, payload: dict) -> bool:
        endpoint = submission_endpoint(self.client_dir)
        if endpoint is None:
            raise RuntimeError("Online submission is awaiting service deployment. Export the report locally.")
        payload = deepcopy(payload)
        report_body(payload)
        def operation(job):
            job.check()
            draft = self.user_paths.data_dir / "reports" / "draft.json"
            save_report_draft(draft, payload, submitted=True)
            try:
                url = submit_report(endpoint, payload)
            except Exception as error:
                self._setup_event("problem_report_retry", job.event({"message": str(error)}))
                return
            save_report_draft(draft, payload, submitted=True, url=url)
            self._setup_event("problem_report_submitted", job.event({"url": url}))
        return self.workers.submit(("report_submission", payload["idempotency_key"]), operation)

    def create_support_bundle(self, destination: Path, *, logs: list[str] | None = None, job=None) -> Path:
        """Collect a support bundle without requiring a healthy game install.

        Condump capture and doctor inspection are both best-effort: either may
        degrade to an unavailable marker while the bundle still records logs,
        configuration evidence, and the last setup failure.
        """
        try:
            support_condump: dict[str, object] | None = (
                self._request_support_condump() if job is None else self._request_support_condump(job=job)
            )
        except LauncherWorkCancelled:
            raise
        except Exception as error:
            support_condump = {
                "status": "unavailable",
                "reason": f"{type(error).__name__}: {error}",
            }
        try:
            report = self.run_doctor() if job is None else self.run_doctor(job=job)
        except LauncherWorkCancelled:
            raise
        except Exception as error:
            report = DoctorReport(
                LauncherDoctor.VERSION,
                (Diagnostic(
                    "support",
                    "attention",
                    f"doctor inspection is unavailable: {type(error).__name__}: {error}",
                ),),
                "needs_attention",
                "",
                "",
                f"Support bundle collected without a doctor report: {error}",
                {},
            )
        diagnostic_logs = [*self.diagnostic_history, *(logs or [])]
        with self._lifecycle_lock:
            room = self.setup.last_event or {}
            connected = self.connected_room
        slot_data = room.get("slot_data")
        slot_data = slot_data if isinstance(slot_data, dict) else {}
        option_keys = set(ROOM_SLOT_DEFAULTS) | {
            option["key"] for option in self.options_schema.get("options", [])
            if isinstance(option, dict) and isinstance(option.get("key"), str)
        }
        support_diagnostics = {
            **report.support_diagnostics,
            "session": {
                "connected": connected,
                "identity_source": "connected" if connected else "last_known" if room else "unavailable",
                "seed_name": room.get("seed_name"),
                "slot": room.get("slot"),
                "team": room.get("team"),
                # Keep options and contract identity; omit placements/protocol history.
                "slot_data": {
                    key: value for key, value in slot_data.items()
                    if key in option_keys or key in {
                        "death_link", "death_link_mode", "slot_data_version",
                        "slot_data_schema_version", "slot_data_revision", "apworld_revision", "release_version",
                        "content_revision", "capabilities",
                    }
                },
            },
        }
        try:
            support_diagnostics["release"] = release_identity()
        except Exception:
            support_diagnostics["release"] = {"status": "unavailable"}
        if job is not None:
            job.check()
        bundle = write_support_bundle(
                destination,
                report,
                logs=diagnostic_logs,
                config=self.config,
                paths=self.user_paths,
                application_dir=self.client_dir,
                session_start=self.session_start_time,
                last_setup_failure=self.last_setup_failure,
                last_connection_error=self.last_connection_error,
                support_condump=support_condump,
                support_diagnostics=support_diagnostics,
                archive_directory=self.user_paths.data_dir / "support-bundles",
        )
        if support_condump and support_condump.get("owned_capture"):
            capture = Path(str(support_condump["path"]))
            if capture.parent.resolve() == (self.user_paths.data_dir / "support-captures").resolve():
                capture.unlink(missing_ok=True)
        if job is None:
            self.emit("support_bundle_ready", path=str(bundle))
        else:
            self._setup_event("support_bundle_ready", job.event({"path": str(bundle)}))
        return bundle

    def _request_support_condump(self, *, job=None) -> dict[str, object]:
        """Attempt one diagnostic condump, recording closed-game availability."""
        if job is not None:
            job.check()
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
        # use fresh native readiness on proton; diagnostics don't need to wait for the host process-name check
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
        with self._condump_lock:
            scope = self.workflow.session_owner.observe()
            if not scope.get("ready") or not scope.get("pid") or not scope.get("process_created"):
                return {"status":"unavailable", "reason":"qualified_process_unavailable"}
            def process_scope(value):
                admission = value.get("admission", {})
                return (value.get("pid"), value.get("process_created"),
                        admission.get("namespace_id"), admission.get("build_id"), admission.get("instance_id"))

            identity = process_scope(scope)
            def sources():
                return [path for path in save_dir.glob("AP_SUPPORT_FILE*.txt")
                        if re.fullmatch(r"AP_SUPPORT_FILE(?:_[0-9]+)*\.txt", path.name)]

            pending = self._condump_pending
            if pending is None or pending["identity"] != identity:
                previous = {}
                for path in sources():
                    stat = path.stat()
                    previous[path.name] = (stat.st_size, stat.st_mtime_ns)
                pending = {"identity": identity, "requested_at": requested_at, "previous": previous}
                self._condump_pending = pending
                supervisor.request_support_condump()
            requested_at = pending["requested_at"]
            previous = pending["previous"]
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                if job is not None:
                    job.check()
                current = self.workflow.session_owner.observe()
                if not current.get("ready") or process_scope(current) != identity:
                    return {"status":"unavailable", "reason":"process_scope_changed"}
                for source in sources():
                    try:
                        stat = source.stat()
                        changed = previous.get(source.name) != (stat.st_size, stat.st_mtime_ns)
                        if not changed or stat.st_mtime < requested_at - 1:
                            continue
                        capture_dir = self.user_paths.data_dir / "support-captures"
                        capture_dir.mkdir(parents=True,exist_ok=True)
                        destination = capture_dir / (uuid.uuid4().hex + ".txt")
                        with source.open("rb") as incoming:
                            payload = incoming.read(16*1024*1024 + 1)
                        after = source.stat()
                        if (after.st_size,after.st_mtime_ns) != (stat.st_size,stat.st_mtime_ns):
                            continue
                        if len(payload) > 16*1024*1024:
                            return {"status":"unavailable", "reason":"condump_too_large"}
                        current = self.workflow.session_owner.observe()
                        if not current.get("ready") or process_scope(current) != identity:
                            return {"status":"unavailable", "reason":"process_scope_changed"}
                        with destination.open("xb") as outgoing:
                            outgoing.write(payload)
                            outgoing.flush()
                            os.fsync(outgoing.fileno())
                        self._condump_pending = None
                        return {"status":"available", "path":str(destination), "owned_capture":True,
                            "source_filename":source.name, "source_size":len(payload),
                            "pid":scope["pid"], "process_created":scope["process_created"],
                            "namespace":scope.get("admission", {}).get("namespace_id"),
                            "build_id":scope.get("admission", {}).get("build_id"),
                            "instance_id":scope.get("admission", {}).get("instance_id"),
                            "requested_at":requested_at, "freshness":"request_copy"}
                    except FileNotFoundError:
                        continue
                time.sleep(.1)
            return {"status":"pending", "reason":"game_diagnostic_condump_not_observed_within_timeout"}

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
            "category", "attempt_id", "reason_codes", "meathook_ok",
            "meathook_status", "meathook_message", "native_state",
        ):
            if key in event and event[key] not in (None, ""):
                fields.append(f"{key}={event[key]}")
        message = f"{kind}: {' | '.join(fields) or 'received'}"
        if kind == "integration_status":
            if getattr(self, "_last_integration_diagnostic", None) == message:
                return
            self._last_integration_diagnostic = message
        self._record_diagnostic(message)

    def _worker_event(
        self, supervisor: BridgeSupervisor, event: dict[str, object]
    ) -> None:
        kind = str(event.get("type", ""))
        stop_failed_worker = False
        pending: dict[str, str] | None = None
        emit_event = True
        intentional_disconnect = False
        stop_native = False
        with self._lifecycle_lock:
            if supervisor is not self.supervisor:
                return
            if kind == "item_history_status":
                self.item_history_status = dict(event)
            if kind == "error" and not event.get("failure_domain"):
                event = enrich_connection_failure(event, attempt_id=self.connection_attempt_id)
                self.last_connection_error = dict(event)
            if kind == "setup_failed" and not event.get("failure_domain"):
                event = {
                    **event,
                    **setup_failure_payload(
                        RuntimeError(str(event.get("message", "setup failed"))),
                        phase="game_setup",
                    ),
                }
            if kind == "setup_failed" and not event.get("attempt_id"):
                event = {**event, "attempt_id": self.connection_attempt_id}
            if kind in {"client_started", "connecting"}:
                if self.state is not LauncherState.DISCONNECTING:
                    self.state = LauncherState.CONNECTING
            elif kind == "connected":
                if self.state is LauncherState.DISCONNECTING:
                    emit_event = False
                else:
                    self.state = LauncherState.CONNECTED
                    self.last_setup_failure = None
                    self.last_connection_error = None
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

    def _worker_log(self, supervisor: BridgeSupervisor, text: str) -> None:
        with self._lifecycle_lock:
            if supervisor is not self.supervisor or not text:
                return
            self._record_diagnostic(f"worker: {text}")
            self.events.put({"type": "log", "message": text, "attempt_id": self._supervisor_attempt})

    def _job_failed(self, job, error) -> None:
        self._setup_event("setup_failed", {
            **setup_failure_payload(error, phase="game_setup"),
            "launcher_job_generation": job.generation,
        })

    def _setup_event(self, kind: str, payload: dict[str, object]) -> None:
        with self._lifecycle_lock:
            generation = payload.get("launcher_job_generation")
            if generation is not None and not self.workers.accepts(generation):
                return
            if (kind == "setup_ready" and payload.get("adapter_state") == "applied") or (
                kind == "room_install_state" and payload.get("state") == "already_installed"
                and payload.get("readiness") == "blocked"
            ):
                game_root = self.config.get("game_root") or self.config.get("doom_base_dir")
                meathook = probe_meathook(Path(str(game_root)).expanduser().resolve() if game_root else None)
                payload = {**payload, "meathook_ok": meathook.ok, "meathook_status": meathook.status.value}
            if kind == "setup_failed":
                self.last_setup_failure = dict(payload)
                if payload.get("failure_domain") in {"room_package", "installed_room_package"}:
                    self.last_room_package_issue = dict(payload)
            elif kind == "room_install_state" and (
                payload.get("state") == "update_required" or payload.get("failure_domain")
            ):
                self.last_room_package_issue = {"type": kind, **payload}
            elif kind == "setup_ready":
                self.last_setup_failure = None
                self.last_room_package_issue = None
            self.emit(kind, **payload)
        if kind == "setup_ready" and payload.get("adapter_state") == "applied":
            self._ensure_native_client(generation=generation)

    def _setup_result(self, record: IntegratedSetupRecord, generation: int) -> None:
        with self._lifecycle_lock:
            if self.workers.accepts(generation):
                self.last_setup = record

    def _request_consent(self, spec) -> bool:
        return self.interactions.consent(spec)

    def resolve_consent(self, request_id: str, accepted: bool) -> None:
        self.interactions.resolve('dependency_consent_required', request_id, accepted)

    def _request_installation_confirmation(self) -> bool:
        return self.interactions.confirmation()

    def resolve_installation_confirmation(self, request_id: str, confirmed: bool) -> None:
        self.interactions.resolve('installation_confirmation_required', request_id, confirmed)

    def _request_uninstall_confirmation(self) -> bool:
        return self.interactions.uninstall_confirmation()

    def resolve_uninstall_confirmation(self, request_id: str, confirmed: bool) -> None:
        self.interactions.resolve('uninstall_confirmation_required', request_id, confirmed)

    def confirm_manual_installation(self, *, generation: int | None = None) -> bool:
        """Queue verified manual-install acknowledgement for the captured room."""
        with self._lifecycle_lock:
            generation = self.workers.generation if generation is None else generation
            if not self.workers.accepts(generation):
                return False
            last_event = self.setup.current_event
        if not last_event:
            raise RuntimeError("No connected room session is available.")

        def operation(job):
            with self._lifecycle_lock:
                job.check()
                self.ensure_ammo_refill_config()
            workflow = self.workflow.for_job(job, self._setup_event, self.interactions.for_job(job))
            snapshot = RoomSnapshot.from_event(last_event)
            record = workflow.confirm_manual_installation(snapshot, str(last_event.get("endpoint") or ""))
            self._setup_result(record, job.generation)
            self._setup_event("setup_ready", job.event({
                "manifest_hash": record.manifest_hash, "randomize_dash": record.randomize_dash,
                "adapter_state": record.adapter_state, "message": record.adapter_message,
                "steam_launch_option": record.steam_launch_option, "new_install": record.new_install,
            }))

        return self.workers.submit(
            ("manual_confirmation", self.setup.room_key(last_event)), operation, generation=generation,
        )

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
            log_sink=lambda text: self._worker_log(supervisor, text),
            archipelago_source=self._archipelago_source(),
        )
        with self._lifecycle_lock:
            self.supervisor = supervisor
            self._supervisor_attempt = self.connection_attempt_id
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
        if not endpoint.strip():
            raise ValueError("server address is required")
        if not slot.strip():
            raise ValueError("player name is required")
        endpoint = validate_server_address(endpoint)
        try:
            game = validate_game_root(Path(game_root))
            saves = validate_save_directory(Path(saves_root))
        except ValueError as error:
            raise ValueError(str(error)) from error
        with self._lifecycle_lock:
            if "new_ap_save" in self.workers.active:
                raise RuntimeError("Wait for the AP save backup and restart to finish before reconnecting")
            if self.state in {LauncherState.CONNECTING, LauncherState.CONNECTED}:
                raise RuntimeError("disconnect the current bridge worker before connecting again")
            if self.state is LauncherState.DISCONNECTING:
                raise RuntimeError("bridge worker is still disconnecting")
            self.connection_attempt_id += 1
            self.item_history_status = None
            self.setup.invalidate()
        self.interactions.cancel_all()
        self.save_config(
            {
                "server_address": endpoint,
                "slot": slot.strip(),
                "game_root": str(game),
                "doom_base_dir": str(game / "base"),
                "save_games_dir": str(saves),
            }
        )
        self.ensure_ammo_refill_config()
        connection = {
            "endpoint": endpoint,
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

    def request_integration_status(self) -> bool:
        with self._lifecycle_lock:
            generation = self.workers.generation
            game_root = self.config.get("game_root") or self.config.get("doom_base_dir")

        def operation(job):
            root = Path(str(game_root)).expanduser().resolve() if game_root else None
            meathook = probe_meathook(root)
            try:
                health = self.native_health()
                state = str(health.get("state", "not_ready")) if isinstance(health, dict) else "not_ready"
            except Exception:
                state = "not_ready"
                health = {"reason": "health_unavailable", "session": dict(self.workflow.session_owner.status)}
            self._setup_event("integration_status", job.event({
                "meathook_ok": meathook.ok, "meathook_status": meathook.status.value,
                "meathook_message": meathook.message, "native_state": state,
                "reason": health.get("reason"), "session": health.get("session", {}),
            }))

        return self.workers.submit("integration_status", operation, generation=generation)

    def _queue_room_readiness(self, event) -> bool:
        generation = self.workers.generation
        event = deepcopy(event)
        configuration = dict(self.config)

        def operation(job):
            def emit(kind, **payload):
                self._setup_event(kind, job.event(payload))

            try:
                snapshot = RoomSnapshot.from_event(event)
                workflow = self.workflow.for_job(job, self._setup_event, self.interactions.for_job(job))
                state = workflow.install_state(snapshot)
            except LauncherWorkCancelled:
                raise
            except Exception as error:
                issue = setup_failure_payload(error, phase="room_snapshot")
                emit(
                    "room_install_state",
                    state="install_needed",
                    reason=f"could not verify installed room mod: {error}",
                    readiness="blocked",
                    readiness_reason=str(error),
                    **issue,
                )
                return
            if state.state == "already_installed" and state.readiness != "blocked":
                job.check()
                status = workflow.session_owner.prepare(snapshot, configuration)
                job.check()
                emit("ap_session_status", **status)
            emit(
                "room_install_state",
                state=state.state,
                manifest_hash=state.manifest_hash,
                staged_mod=state.staged_mod,
                steam_launch_option=state.steam_launch_option,
                reason=state.reason,
                readiness=state.readiness,
                readiness_reason=state.readiness_reason,
                **(installed_package_issue_payload(state.reason) if state.state == "update_required" else {}),
            )
            if state.state == "already_installed" and state.readiness != "blocked":
                self._ensure_native_client(generation=job.generation)

        return self.workers.submit(("room_readiness", self.setup.room_key(event)), operation, generation=generation)

    def process_event(self, event: dict[str, object]) -> bool | None:
        attempt = event.get("attempt_id")
        if attempt is not None and attempt != self.connection_attempt_id:
            return False
        generation = event.get("launcher_job_generation")
        if generation is not None and not self.workers.accepts(generation):
            return False
        event_type = event.get("type")
        if event_type == "connected":
            self.connected_room = True
            self.last_setup_failure = None
            self.last_room_package_issue = None
            self.setup.observe(event)
            self._queue_room_readiness(event)

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
        """Consume cached process state and clean stale bindings on game exit."""
        if self._game_lifecycle_thread is None:
            self._game_lifecycle_thread = threading.Thread(
                target=self._sample_game_lifecycle,
                name="DoomLifecycleSampler",
                daemon=True,
            )
            self._game_lifecycle_thread.start()
        with self._game_lifecycle_sample_lock:
            current_running = self._game_lifecycle_sample
        if current_running is None:
            return
        if self._last_game_running and not current_running:
            cleanup_stale_doom_config_bind(self.config, is_game_running=False)
        self._last_game_running = current_running

    def _sample_game_lifecycle(self) -> None:
        """Run the slow Windows process enumeration away from the Qt thread."""
        while not self._game_lifecycle_stop.is_set():
            if os.name == "nt":
                from doom_eap.runtime.observer_lifecycle import windows_game_processes
                processes = windows_game_processes()
                current_running = None if processes is None else bool(processes)
                status = self.workflow.session_owner.observe(processes)
            else:
                current_running = self.is_game_running()
                status = self.workflow.session_owner.observe()
            semantic = (status.get("state"), status.get("pid"), status.get("namespace_id"),
                        status.get("admission", {}).get("instance_id"))
            if semantic != getattr(self, "_last_ap_session_status", None):
                self._last_ap_session_status = semantic
                self.emit("ap_session_status", **status)
            with self._game_lifecycle_sample_lock:
                self._game_lifecycle_sample = current_running
            self._game_lifecycle_stop.wait(1.0)

    def set_ammo_refill_keybind(self, keybind: str) -> None:
        self.set_ap_keybinds(keybind, str(self.config.get("special_toggle_keybind", "F10")))

    def set_ap_keybinds(self, refill: str, special: str) -> None:
        refill, special = normalize_ammo_refill_keybind(refill), normalize_ammo_refill_keybind(special)
        if refill and refill == special:
            raise ValueError("Ammo Refill and Special Weapon toggle must use different keys")
        previous = {key: self.config.get(key) for key in (AMMO_REFILL_KEYBIND_CONFIG, "special_toggle_keybind")}
        write_ap_hotkey_states(doom_base_dir_from_config(self.config), refill, special)
        try:
            self.save_config({AMMO_REFILL_KEYBIND_CONFIG: refill, "special_toggle_keybind": special})
        except OSError:
            self.config.update(previous)
            write_ap_hotkey_states(doom_base_dir_from_config(self.config), str(previous[AMMO_REFILL_KEYBIND_CONFIG]), str(previous["special_toggle_keybind"]))
            raise
        self.emit("ammo_refill_keybind_status", state="configured", keybind=refill)

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

    def ap_backups(self) -> list[str]:
        if not self.connected_room or not self.setup.current_event:
            raise RuntimeError("Connect to a room before selecting its AP backups")
        return self.workflow.session_owner.list_backups(RoomSnapshot.from_event(self.setup.current_event))

    def request_new_ap_save(self) -> bool:
        if not self.connected_room or not self.setup.current_event:
            raise RuntimeError("Connect to the existing room before creating its new game save")
        if self.workflow.session_owner.game_processes(self.config) != ():
            raise RuntimeError("Exit DOOM Eternal before creating a new AP game save")
        snapshot = RoomSnapshot.from_event(self.setup.current_event)
        configuration = dict(self.config)
        supervisor = self.supervisor
        self.disconnect()

        def operation(job):
            job.check()
            if supervisor is not None and not supervisor.wait_stopped(15):
                raise RuntimeError("The bridge has not stopped; existing saves were kept")
            archive = job.publish(lambda: self.workflow.session_owner.restart_campaign(snapshot, configuration))
            self.emit("ap_backup_result", message=(
                f"Your previous game save and AP receipt state were backed up to {archive}. "
                "Reconnect to the same room and wait for AP save preparation, then start DOOM Eternal through Steam. "
                "The multiworld is unchanged; items in the server's current history will be delivered to the new save."
            ))

        return self.workers.submit("new_ap_save", operation, generation=self.workers.generation)

    def request_ap_backup(self, *, restore=None) -> bool:
        if not self.connected_room or not self.setup.current_event:
            raise RuntimeError("Connect to a room before managing its AP save")
        snapshot=RoomSnapshot.from_event(self.setup.current_event)
        configuration=dict(self.config)

        def operation(job):
            job.check()
            if restore is not None:
                self.workflow.session_owner.prepare(snapshot,configuration,recovery_basename=restore)
                message="Compatible recovery is staged. Play this room to let the game restore it. Receipt history, purchases and consumables were preserved."
            else:
                from .launcher_session import namespace_id
                expected=namespace_id(snapshot.seed_name,snapshot.team,snapshot.slot,
                                      snapshot.slot_data["native_generation_fingerprint"])
                if self.workflow.session_owner.namespace != expected:
                    raise RuntimeError("The live AP session belongs to a different room")
                result=self.workflow.session_owner.create_backup()
                message=f"AP backup captured and verified: {result['basename']}. Opening it in-game remains unverified."
            job.check()
            self.emit("ap_backup_result",**job.event({"message":message}))

        return self.workers.submit("ap_backup",operation,generation=self.workers.generation)

    def request_inventory_resync(self, *, domain="all", item_id=None) -> None:
        with self._lifecycle_lock:
            connected = self.connected_room
            supervisor = self.supervisor
        if not connected:
            raise RuntimeError("not connected")
        if supervisor is None or not supervisor.running:
            raise RuntimeError("bridge worker is not running")
        try:
            supervisor.request_inventory_resync(domain=domain, item_id=item_id)
        except Exception as error:
            message = str(error).replace("\r", " ").replace("\n", " ")[:512]
            self.emit("inventory_resync", status="error", message=message)
            raise

    def disconnect(self) -> None:
        with self._lifecycle_lock:
            self.setup.invalidate()
        self.interactions.cancel_all()
        self._stop_native_client()
        supervisor: BridgeSupervisor | None
        with self._lifecycle_lock:
            if self.state is LauncherState.DISCONNECTING:
                return
            supervisor = self.supervisor
            self._pending_connect = None
            self.connected_room = False
            self.item_history_status = None
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
        *, imported: dict[str, object] | None = None,
    ) -> Path:
        """Save future-room generation input without touching connected room state."""
        saved = save_player_yaml(
            destination,
            self.options_schema,
            player_name,
            values,
            imported=imported,
        )
        self.emit("player_yaml_saved", path=str(saved))
        return saved

    def _start_room_setup(self, *, force: bool, generation: int | None) -> bool:
        with self._lifecycle_lock:
            generation = self.workers.generation if generation is None else generation
            if not self.workers.accepts(generation):
                return False
            self.ensure_ammo_refill_config()
        return self.setup.start(force=force, generation=generation)

    def retry_setup(self) -> bool:
        return self._start_room_setup(force=True, generation=None)

    def prepare_setup(self, *, generation: int | None = None) -> bool:
        return self._start_room_setup(force=False, generation=generation)

    def reinstall_setup(self, *, generation: int | None = None) -> bool:
        return self._start_room_setup(force=True, generation=generation)

    def uninstall_setup(self, *, generation: int | None = None) -> dict[str, object]:
        """Queue current room package uninstall on serialized setup worker."""
        with self._lifecycle_lock:
            generation = self.workers.generation if generation is None else generation
            if not self.workers.accepts(generation):
                raise RuntimeError("Room changed before uninstall confirmation.")
            last_event = self.setup.last_event
            connected = self.connected_room
        if not connected or not last_event:
            raise RuntimeError("connect to room before uninstalling its mod")
        if not self.setup.submit_uninstall(last_event, generation=generation):
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
        if not self.workflow.session_owner.can_close():
            raise RuntimeError("Keep the launcher open until DOOM Eternal has exited")
        self.workflow.session_owner.retire()
        self._game_lifecycle_stop.set()
        lifecycle_thread = self._game_lifecycle_thread
        if lifecycle_thread is not None and lifecycle_thread.is_alive():
            lifecycle_thread.join(timeout=1.0)
        self._stop_native_client()
        self.disconnect()
        self.workers.close()
