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
from .launcher_reporting import ScopedSupportReport, report_problem

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
    AMMO_HOTKEY_STATE_FILENAME,
    AMMO_HOTKEY_STATE_HEADER,
    AMMO_REFILL_BIND_COMMAND,
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
    resolve_doom_config_path,
    select_saved_games_dir,
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
        if reason == "executable_missing":
            message = (
                "The Game integration helper is missing from this installation. "
                "Repair or reinstall DoomEAP, then retry. If the problem continues, "
                "generate a Support Report."
            )
        elif reason == "access_denied":
            message = (
                "Windows denied access to the Game integration helper. Windows Security "
                "or antivirus software may have blocked it. Check Protection history "
                "and restore or allow the DoomEAP file if it was blocked, then retry. "
                "If the problem continues, generate a Support Report."
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
                self._native_start_failure(
                    reason="executable_missing",
                    technical_message=f"file not found: {client_exe}",
                    executable_path=client_exe,
                )
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
                    self._native_start_failure(
                        reason="immediate_exit",
                        technical_message="process exited before publishing current native health",
                        executable_path=client_exe,
                        exit_code=exit_code,
                    )
                    return False
                self.emit("native_client_started", path=str(client_exe), game_root=str(root))
                return True
            except Exception as error:
                self._native_client_process = None
                winerror = getattr(error, "winerror", None)
                access_denied = isinstance(error, PermissionError) or winerror == 5
                self._native_start_failure(
                    reason="access_denied" if access_denied else "process_creation_failed",
                    technical_message=(
                        f"{type(error).__name__}: {error}"
                        + (f" (winerror={winerror})" if winerror is not None else "")
                    ),
                    executable_path=client_exe,
                )
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
        if target_platform == "nt" and self.connected_room:
            if not self._ensure_native_client(platform=target_platform):
                raise RuntimeError(
                    "Game integration helper could not start. Review the launcher warning "
                    "or generate a Support Report."
                )
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
            result = report_problem(ScopedSupportReport(self.create_support_bundle, job), logs=logs, job=job)
            self._setup_event("problem_report_ready", job.event({
                "message": result.message, "path": str(result.path) if result.path else None,
                "browser_opened": result.browser_opened,
            }))

        return self.workers.submit("problem_report", operation, generation=generation)

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
        )
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
        previous_files: dict[Path, tuple[int, int]] = {}
        try:
            for candidate in set(save_dir.glob("AP_SUPPORT_FILE*.txt")):
                try:
                    stat = candidate.stat()
                    previous_files[candidate] = (stat.st_size, stat.st_mtime_ns)
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
            if job is not None:
                job.check()
            candidates: list[tuple[int, str, Path, os.stat_result, str]] = []
            try:
                paths = sorted(save_dir.glob("AP_SUPPORT_FILE*.txt"))
            except (OSError, ValueError, RuntimeError):
                paths = []
            for candidate in paths:
                try:
                    stat = candidate.stat()
                except (OSError, ValueError, RuntimeError):
                    continue
                previous = previous_files.get(candidate)
                freshness = "new" if previous is None else "modified"
                changed = previous is None or previous != (stat.st_size, stat.st_mtime_ns)
                if changed and stat.st_mtime >= requested_at - 1.0:
                    candidates.append((stat.st_mtime_ns, candidate.name, candidate, stat, freshness))
            if candidates:
                _, _, candidate, stat, freshness = max(candidates)
                return {
                    "status": "available",
                    "path": str(candidate),
                    "source_filename": candidate.name,
                    "requested_at": requested_at,
                    "source_mtime": stat.st_mtime,
                    "source_size": stat.st_size,
                    "freshness": freshness,
                    "selection_reason": "freshest_changed_candidate_by_mtime_ns_then_filename",
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
            "category", "attempt_id", "reason_codes",
        ):
            if key in event and event[key] not in (None, ""):
                fields.append(f"{key}={event[key]}")
        self._record_diagnostic(f"{kind}: {' | '.join(fields) or 'received'}")

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
            if self.state in {LauncherState.CONNECTING, LauncherState.CONNECTED}:
                raise RuntimeError("disconnect the current bridge worker before connecting again")
            if self.state is LauncherState.DISCONNECTING:
                raise RuntimeError("bridge worker is still disconnecting")
            self.connection_attempt_id += 1
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
            self._setup_event("integration_status", job.event({
                "meathook_ok": meathook.ok, "meathook_status": meathook.status.value,
                "meathook_message": meathook.message, "native_state": state,
            }))

        return self.workers.submit("integration_status", operation, generation=generation)

    def _queue_room_readiness(self, event) -> bool:
        generation = self.workers.generation
        event = deepcopy(event)

        def operation(job):
            def emit(kind, **payload):
                self._setup_event(kind, job.event(payload))

            try:
                snapshot = RoomSnapshot.from_event(event)
                workflow = self.workflow.for_job(job, self._setup_event, self.interactions.for_job(job))
                state = workflow.install_state(snapshot)
                emit(
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
                if state.state == "already_installed" and state.readiness != "blocked":
                    self._ensure_native_client(generation=job.generation)
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
            current_running = self.is_game_running()
            with self._game_lifecycle_sample_lock:
                self._game_lifecycle_sample = current_running
            self._game_lifecycle_stop.wait(1.0)

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
        self._game_lifecycle_stop.set()
        lifecycle_thread = self._game_lifecycle_thread
        if lifecycle_thread is not None and lifecycle_thread.is_alive():
            lifecycle_thread.join(timeout=1.0)
        self._stop_native_client()
        self.disconnect()
        self.workers.close()
