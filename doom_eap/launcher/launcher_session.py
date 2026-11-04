"""Room-bound prelaunch admission with one live, single-use Windows lease."""
from __future__ import annotations

import asyncio
import ctypes
import datetime
import hashlib
import importlib.util
import json
import logging
import os
import re
import secrets
import selectors
import shutil
import stat
import subprocess
import sys
import threading
import time
import zipfile
from ctypes import wintypes
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from doom_eap.contracts.core_distribution import verify_runtime
from doom_eap.launcher.ui.strings import text as player_text
from doom_eap.runtime.observer_lifecycle import same_executable_path, windows_game_processes
from doom_eap.runtime.weapon_points import namespace_id

from .checkpoint import CheckpointPlan, CheckpointState, RecoveryState, recovery_observation
from .ui.strings import checkpoint_blocked


class SessionSupervisorError(RuntimeError):
    code = "session_supervisor_failed"
    failure_domain = "campaign_session"


class SaveInterruptedError(RuntimeError):
    code = "save_interrupted"
    failure_domain = "campaign_session"

    def __init__(self):
        super().__init__(player_text('launcher.save', 'interrupted'))


class CheckpointRecoveryBlockedError(RuntimeError):
    code = "checkpoint_recovery_blocked"
    failure_domain = "campaign_session"

    def __init__(self, reason):
        super().__init__(checkpoint_blocked(reason))

_UNSET = object()
# Full campaign room snapshots require the same large frame limit at both ends.
SESSION_PIPE_LIMIT = 256 * 1024 * 1024
_INSTALL_KEYS = ("game_root", "doom_base_dir", "steam_remote_dir", "save_games_dir", "core_runtime_manifest", "selected_core_runtime_manifest", "client_state_file", "delivery_ledger_file", "delivery_ledger_expected", "proton_compat_data_dir", "proton_executable")


def prelaunch_owner_state(pid, created):
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    api.GetProcessTimes.restype = wintypes.BOOL
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.OpenProcess(0x100000 | 0x1000, False, pid)
    if not handle:
        return "ended" if ctypes.get_last_error() == 87 else "unknown"
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return "unknown"
        actual = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        waited = api.WaitForSingleObject(handle, 0)
        return "ended" if actual != created or waited == 0 else "alive" if waited == 258 else "unknown"
    finally:
        api.CloseHandle(handle)


def archive_prelaunch(path, state_dir, *, explicit=False):
    if not path.exists():
        return None
    with path.open("rb") as stream:
        raw = stream.read(65537)
    match = re.match(rb"sentinel-run-v1\nrun=[0-9a-f]{32}\nprotection=[0-9a-f]{64}\nowner=([1-9][0-9]*)\ncreated=([1-9][0-9]*)\nsentinel-test-session-v3\n", raw)
    recognized = match and len(raw) <= 65536 and int(match[1]) <= 0xffffffff and int(match[2]) <= 0xffffffffffffffff
    state = prelaunch_owner_state(int(match[1]), int(match[2])) if recognized else "legacy"
    if state == "alive":
        raise RuntimeError(player_text('launcher.session', 'another_live_launcher_owns_this_game_preparation_close_that_laun'))
    if state == "unknown" or (state == "legacy" and not explicit):
        raise RuntimeError(player_text('launcher.session', 'prelaunch_ownership_is_unknown_use_repair_session_with_doom_clos'))
    if state == "legacy":
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenSemaphoreW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        api.OpenSemaphoreW.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenSemaphoreW(0x100000, False, "Local\\SentinelA-" + hashlib.sha256(raw).hexdigest())
        if handle:
            api.CloseHandle(handle)
            raise RuntimeError(player_text('launcher.session', 'a_live_prelaunch_lease_exists_its_marker_was_preserved'))
        if ctypes.get_last_error() != 2:
            raise RuntimeError(player_text('launcher.session', 'prelaunch_lease_ownership_cannot_be_inspected_marker_preserved'))
    directory = state_dir / "prelaunch-backups"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (hashlib.sha256(raw).hexdigest() + "-" + secrets.token_hex(8) + ".txt")
    if path.read_bytes() != raw:
        raise RuntimeError(player_text('launcher.session', 'prelaunch_marker_changed_during_recovery_retry_inspection'))
    shutil.move(path, target)
    logging.getLogger(__name__).info("AP_PRELAUNCH_ARCHIVED owner_state=%s source=%s destination=%s", state, path, target)
    return target


def _campaign_native_root(campaign_root, namespace):
    record = campaign_root / namespace / "native.root"
    if not record.exists():
        return "ap-" + namespace[:40]
    with record.open("rb") as stream:
        text = stream.read(2049)
    header = f"sentinel-native-root-v1\nnamespace={namespace}\nroot=".encode("ascii")
    if not text.startswith(header) or not re.fullmatch(rb"ap-[0-9a-f]{40}\n", text[len(header):]):
        raise RuntimeError(player_text('launcher.session', 'ap_campaign_provider_metadata_is_unreadable'))
    return text[len(header):-1].decode("ascii")


def _inspect_save_path(path, kind):
    result = {"path": str(path), "status": "present"}
    try:
        info = path.lstat()
        if (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or not (stat.S_ISDIR(info.st_mode) if kind == "directory" else stat.S_ISREG(info.st_mode))):
            result["status"] = "wrong_type"
        elif kind == "directory":
            with os.scandir(path) as entries:
                result["entries"] = [entry.name for _, entry in zip(range(32), entries, strict=False)]
    except FileNotFoundError:
        result["status"] = "missing"
    except OSError as error:
        result.update(status="inaccessible", error=str(error), errno=error.errno,
                      winerror=getattr(error, "winerror", None))
    return result


def _inspect_campaign_metadata(directory, paths, expected):
    entry = paths["campaign.contract"]
    if entry["status"] != "present":
        return
    try:
        with (directory / "campaign.contract").open("rb") as stream:
            raw = stream.read(65537)
        entry["status"] = "present" if raw == expected else "metadata_invalid"
    except OSError as error:
        entry.update(status="inaccessible", error=str(error))


def _campaign_copy_files(archive, sources):
    files = {}
    for index, source in enumerate(sources):
        if archive.resolve().is_relative_to(source.resolve()):
            raise RuntimeError(player_text('launcher.session', 'ap_backup_destination_must_be_outside_its_sources'))
        for path in _campaign_source_files(source):
            name = f"{index}/{path.relative_to(source).as_posix() if source.is_dir() else source.name}"
            files[name] = (path, hashlib.sha256(path.read_bytes()).hexdigest())
    return files


def _campaign_source_files(source):
    pending = [source]
    while pending:
        path = pending.pop()
        info = path.lstat()
        if (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
            raise RuntimeError(player_text('launcher.session', 'ap_backup_refuses_linked_or_special_save_files'))
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                pending.extend(Path(entry.path) for entry in entries)
        else:
            yield path


def _restart_receipt_state(state, keys, state_key, owner, default_session_state):
    backend = state["sessions"].get(state_key, {}).get("delivery_backend")
    for key in keys:
        state["sessions"].pop(key)
    state["sessions"][state_key] = default_session_state()
    if owner or backend == "ledger":
        state["sessions"][state_key]["delivery_backend"] = "ledger"
        state["sessions"][state_key]["delivery_ledger_expected"] = owner


def _archive_campaign_files(archive, snapshot, namespace, sources, raw=None, ledger_snapshot=None):
    files = _campaign_copy_files(archive, sources)
    if ledger_snapshot is not None:
        files["delivery.sqlite3"] = (ledger_snapshot, hashlib.sha256(ledger_snapshot.read_bytes()).hexdigest())
    archive.parent.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as output:
        for name, (path, _digest) in files.items():
            output.write(path, name)
        if raw is not None:
            output.writestr("client_state.json", raw)
        output.writestr("room.json", json.dumps({"seed": snapshot.seed_name, "team": snapshot.team,
            "slot": snapshot.slot, "namespace": namespace, "sources": [str(path) for path in sources],
            "sha256": {name: digest for name, (_, digest) in files.items()}}))
    with archive.open("r+b") as stream:
        os.fsync(stream.fileno())
    with zipfile.ZipFile(archive) as saved:
        if saved.testzip() is not None or any(hashlib.sha256(saved.read(name)).hexdigest() != digest
                                            for name, (_, digest) in files.items()):
            raise RuntimeError(player_text('launcher.session', 'ap_backup_verification_failed_existing_saves_were_kept'))
        if raw is not None and saved.read("client_state.json") != raw:
            raise RuntimeError(player_text('launcher.session', 'ap_receipt_backup_verification_failed_existing_saves_were_kept'))
    return archive


class _ProtonSessionOwner:
    def __init__(self, client_dir, data_dir, state_dir):
        self.client_dir, self.data_dir, self.state_dir = client_dir, data_dir, state_dir
        self.process = None
        self.closable = False
        self._config_identity = None
        self._next_process_query = 0
        self._game_observed = False
        self._channel_failure = None
        self._runtime_details = None
        self.status = {"state": "not_prepared", "ready": False}

    def request(self, action, *, timeout=10, **payload):
        if self.process is None or self.process.poll() is not None:
            raise SessionSupervisorError(player_text('launcher.session', 'the_windows_ap_session_supervisor_is_unavailable'))
        self.process.stdin.write(json.dumps({"action": action, **payload}) + "\n")
        self.process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout):
                self.closable = False
                raise SessionSupervisorError(player_text('launcher.session', 'the_windows_ap_session_supervisor_did_not_respond'))
        raw = self.process.stdout.readline(SESSION_PIPE_LIMIT + 1)
        response = json.loads(raw)
        if not isinstance(response, dict) or type(response.get("can_close")) is not bool:
            raise SessionSupervisorError(player_text('launcher.session', 'invalid_windows_session_supervisor_response'))
        self.closable = response["can_close"]
        if "status" in response:
            if not isinstance(response["status"], dict):
                raise SessionSupervisorError(player_text('launcher.session', 'invalid_windows_session_status'))
            self.status = response["status"]
            if self._runtime_details is not None:
                self.status["proton_runtime"] = self._runtime_details
            self._game_observed |= bool(self.status.get("pid"))
        if "error" in response:
            error = RuntimeError(response["error"])
            for key, value in response.get("failure", {}).items():
                if key in {"code", "failure_domain", "stage", "operation", "source_path", "destination_path", "errno", "winerror", "filename", "filename2", "proton_reason", "proton_candidates", "proton_selected"}:
                    setattr(error, key, value)
            raise error
        return self.status

    def prepare(self, snapshot, config, *, recover_prelaunch=False):
        from doom_eap.runtime.proton import runtime, windows_path
        namespace = namespace_id(snapshot.seed_name, snapshot.team, snapshot.slot,
                                 snapshot.slot_data["native_generation_fingerprint"])
        config_identity = tuple(config.get(key) for key in _INSTALL_KEYS)
        if self.process is not None and (self.process.poll() is not None or self.process.stdin.closed):
            self.observe()
            self.retire()
        if self.process is not None:
            if namespace != self.status.get("namespace_id") or config_identity != self._config_identity:
                self.retire()
            else:
                status = self.request("observe")
                if status.get("state") != "game_exited":
                    return status
                self.retire()
        executable = self.client_dir / "APSessionOwner.exe"
        if not executable.is_file():
            raise RuntimeError(player_text('launcher.session', 'the_packaged_windows_ap_session_supervisor_is_unavailable'))
        command, environment = runtime(config)
        self._runtime_details = {"source": "manual" if config.get("proton_executable") else "config_info",
            "executable": command[0], "tool": Path(command[0]).parent.name,
            "compat_data_path": environment["STEAM_COMPAT_DATA_PATH"],
            "wineprefix": environment["WINEPREFIX"], "steam_install": environment["STEAM_COMPAT_CLIENT_INSTALL_PATH"]}
        converted = {key: config[key] for key in _INSTALL_KEYS if key in config}
        for key in ("game_root", "doom_base_dir", "steam_remote_dir", "save_games_dir", "core_runtime_manifest", "selected_core_runtime_manifest", "client_state_file", "delivery_ledger_file"):
            if converted.get(key):
                converted[key] = windows_path(converted[key])
        if not converted.get("game_root"):
            converted["game_root"] = windows_path(Path(config["doom_base_dir"]).parent)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "session_supervisor.log").open("ab") as log:
            self._game_observed = False
            self._next_process_query = 0
            self._channel_failure = None
            self.process = subprocess.Popen([*command, windows_path(executable)], env=environment,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                            text=True, encoding="utf-8", bufsize=1)
        try:
            self._config_identity = config_identity
            return self.request("prepare", timeout=300, room=asdict(snapshot), config=converted,
                                recover_prelaunch=recover_prelaunch,
                                client_dir=windows_path(self.client_dir), data_dir=windows_path(self.data_dir),
                                state_dir=windows_path(self.state_dir))
        except (OSError, ValueError, RuntimeError) as error:
            self._preparation_channel_failure(error)
            self.process.stdin.close()
            raise

    def _preparation_channel_failure(self, error):
        if self.status.get("state") != "recovery_blocked":
            self.status.update(state="supervisor_unavailable", ready=False, first_failure=str(error))
            self.closable = False

    def observe(self):
        if self.process is None:
            return self.status
        if self.process.poll() is not None or self.process.stdin.closed:
            return self._observe_disconnected()
        try:
            return self.request("observe")
        except (OSError, ValueError, RuntimeError) as error:
            if self._channel_failure is None:
                self._channel_failure = str(error)
                self.status["first_failure"] = self._channel_failure
                logging.getLogger(__name__).warning("AP_SUPERVISOR_UNAVAILABLE exit=%s channel_closed=%s error=%s",
                    self.process.poll(), self.process.stdin.closed, error)
            self.closable = False
            self.status.update(state="supervisor_unavailable", ready=False)
            return self.status

    def _observe_disconnected(self):
        if self.status.get("state") == "recovery_blocked" and self.closable:
            return self.status
        from doom_eap.runtime.proton import windows_processes
        self.closable = False
        self.status.update(state="supervisor_unavailable", ready=False)
        if time.monotonic() < self._next_process_query:
            return self.status
        self._next_process_query = time.monotonic() + 10
        configuration = dict(zip(_INSTALL_KEYS, self._config_identity, strict=True))
        processes = windows_processes(configuration, self.client_dir / "APSessionOwner.exe")
        self.closable = processes == ()
        self._game_observed |= bool(processes)
        if self.closable and self._game_observed:
            self.status["state"] = "game_exited"
        return self.status

    def retire(self):
        if self.process is None:
            return
        if self.process.poll() is None and not self.process.stdin.closed:
            self.request("retire")
            self.process.stdin.close()
        elif not self.closable:
            raise RuntimeError(player_text('launcher.session', 'confirm_doom_eternal_has_exited_before_retiring_its_session_supe'))
        self.process.wait(timeout=10)
        self.process = None
        self.closable = True


class APSessionOwner:
    def __init__(self, client_dir: Path, data_dir: Path, state_dir: Path):
        self.client_dir, self.data_dir, self.state_dir = client_dir, data_dir, state_dir
        self._lock = threading.RLock()
        self._handle = None
        self._process = None
        self.status = {"state": "not_prepared", "ready": False}
        self.namespace = ""
        self._control = None
        self._remote = None
        self._instance = None
        self._config_identity = None

    def _probe(self, *arguments: str) -> dict:
        result = subprocess.run([str(self._runtime / "sentinel_probe.exe"), *arguments], capture_output=True,
                                timeout=6, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            value = json.loads(result.stdout)
        except (ValueError, UnicodeError) as error:
            raise RuntimeError(player_text('launcher.session', 'core_session_probe_did_not_return_a_valid_response')) from error
        if not isinstance(value, dict):
            raise RuntimeError(player_text('launcher.session', 'core_session_probe_returned_an_invalid_object'))
        if result.returncode and "--save-admission" not in arguments:
            outcome = value.get("outcome", value.get("result", "unknown"))
            refused = RuntimeError(player_text('launcher.session', 'core_session_refused_value_0_error_value_1', value_0=f'{outcome}', value_1=f"{value.get('win32_error', 0)}"))
            refused.storage_outcome = outcome
            raise refused
        return value

    def _capture_admission_failure(self, process=None, admission=None, error=None, *, processes=_UNSET):
        runtime = getattr(self, "_runtime", None)
        if runtime is None:
            return
        admission = admission or {}
        process = process or {}
        candidates = None if processes is None else list(processes[:8]) if processes is not _UNSET else []
        key = (process.get("pid"), str(process.get("created")), self.namespace,
               self.status.get("state"), json.dumps(candidates, sort_keys=True),
               *(admission.get(field) for field in ("state", "fault", "prepared_routes", "instance_id")))
        now = time.monotonic()
        if key != getattr(self, "_admission_capture_key", None):
            self._admission_capture_key, self._admission_capture_attempts = key, 0
            self._next_admission_capture = 0
        if self._admission_capture_attempts >= 4 or now < self._next_admission_capture:
            return
        self._admission_capture_attempts += 1
        self._next_admission_capture = now + 5
        document = {"at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "pid": process.get("pid"), "process_created": str(process.get("created")),
                    "namespace_id": self.namespace, "build_id": self._build,
                    "probe": str(runtime / "sentinel_probe.exe"), "attempt": self._admission_capture_attempts,
                    "admission": admission, "admission_error": str(error) if error else None, "queries": {}}
        document.update(state=self.status.get("state"), expected_game_path=str(self._game_exe),
                        expected_process=self._process, processes=candidates)
        for flag in (("--native", "--engine", "--save-installation") if processes is _UNSET else ()):
            query = {}
            document["queries"][flag] = query
            try:
                result = subprocess.run([document["probe"], "--pid", str(process["pid"]), flag,
                                         "--timeout-ms", "250", "--json"], capture_output=True,
                                        timeout=1, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                query.update(returncode=result.returncode, stderr=result.stderr[:4096].decode("utf-8", errors="replace"))
                raw = result.stdout[:65536].decode("utf-8", errors="replace")
                query["truncated"] = len(result.stdout) > 65536
                try:
                    response = json.loads(raw)
                    if not isinstance(response, dict):
                        raise ValueError(player_text('launcher.session', 'core_diagnostic_returned_an_invalid_object'))
                    query["response"] = response
                    query["identity_matches"] = (response.get("target_pid") == process["pid"]
                        and response.get("server_pid") == process["pid"]
                        and str(response.get("process_created")) == str(process["created"]))
                except ValueError as invalid:
                    query.update(error=str(invalid), stdout=raw)
            except (OSError, subprocess.SubprocessError) as failed:
                query.update(error_type=type(failed).__name__, error=str(failed))
                for stream in ("stdout", "stderr"):
                    value = getattr(failed, stream, None)
                    if value:
                        query[stream] = value[:4096].decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)[:4096]
        native = document["queries"].get("--native", {})
        if (native.get("identity_matches") and native.get("response", {}).get("availability") in {"enabled", "disabled", "retained"}
                and all(query.get("identity_matches") and query.get("returncode") == 0 for query in document["queries"].values())):
            self._admission_capture_attempts = 4
        try:
            from .launcher_platform import _atomic_write_bytes
            self.state_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(self.state_dir / "session_admission_failure.json",
                                (json.dumps(document, indent=2, ensure_ascii=True) + "\n").encode("utf-8"))
        except OSError:
            logging.getLogger(__name__).exception("AP_ADMISSION_DIAGNOSTIC_WRITE_FAILED diagnostic=%s", document)

    def prepare(self, snapshot, config: dict, *, recovery_basename=None, recover_prelaunch=False) -> dict:
        from .launcher_core import _normalize_slot_data
        _normalize_slot_data(snapshot.slot_data)
        try:
            if not (config.get("game_root") or config.get("doom_base_dir")):
                return self._prepare(snapshot, config, recovery_basename=recovery_basename, recover_prelaunch=recover_prelaunch)
            from .launcher_core_install import game_write_lock
            root = Path(str(config.get("game_root") or config["doom_base_dir"])).resolve()
            if root.name.casefold() == "base":
                root = root.parent
            with game_write_lock(root), self._lock:
                return self._prepare(snapshot, config, recovery_basename=recovery_basename, recover_prelaunch=recover_prelaunch)
        except (RuntimeError, OSError, ValueError) as error:
            self._record_preparation_failure(error, snapshot, config)
            raise

    def _record_preparation_failure(self, error, snapshot, config):
        import traceback
        from datetime import datetime, timezone

        from .launcher_integration import setup_failure_payload
        if not getattr(error, "failure_domain", None):
            error.failure_domain = "campaign_session"
        diagnostic = setup_failure_payload(error, phase="campaign_session")
        diagnostic["traceback"] = traceback.format_exc()
        diagnostic["at_utc"] = datetime.now(timezone.utc).isoformat()
        diagnostic["metadata"] = getattr(error.__cause__, "private_metadata", None)
        if snapshot is not None:
            try:
                diagnostic["campaign_inspection"] = self.inspect_campaign(snapshot, config)
            except (OSError, RuntimeError, ValueError, KeyError) as inspection_error:
                diagnostic["campaign_inspection"] = {"status": "unavailable", "reason": str(inspection_error)}
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            (self.state_dir / "session_prepare_failure.json").write_text(
                json.dumps(diagnostic, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        except OSError:
            logging.getLogger(__name__).exception("AP_SESSION_DIAGNOSTIC_WRITE_FAILED")
        logging.getLogger(__name__).exception("AP_SESSION_PREPARATION_REFUSED %s", diagnostic)

    def _prepare(self, snapshot, config: dict, *, recovery_basename=None, recover_prelaunch=False) -> dict:
        with self._lock:
            config_identity = tuple(config.get(key) for key in _INSTALL_KEYS)
            namespace = namespace_id(snapshot.seed_name, snapshot.team, snapshot.slot,
                                     snapshot.slot_data["native_generation_fingerprint"])
            if os.name != "nt":
                if recovery_basename is not None:
                    raise RuntimeError(player_text('launcher.session', 'recovery_requires_a_qualified_native_helper_in_this_environment'))
                if self._remote is None:
                    self._remote = _ProtonSessionOwner(self.client_dir, self.data_dir, self.state_dir)
                self.status = self._remote.prepare(snapshot, config, recover_prelaunch=recover_prelaunch)
                self.namespace = namespace
                return dict(self.status)
            processes = windows_game_processes()
            if processes is None:
                raise RuntimeError(player_text('launcher.session', 'game_process_identity_is_unknown_ap_session_preparation_refused'))
            if processes:
                if recovery_basename is not None:
                    raise RuntimeError(player_text('launcher.session', 'close_doom_eternal_before_restoring_a_backup'))
                if self._handle and namespace == self.namespace and self._config_identity == config_identity:
                    return self.observe(processes)
                raise RuntimeError(player_text('launcher.session', 'close_doom_eternal_before_preparing_this_save'))
            if recovery_basename is None and self._handle and namespace == self.namespace and self._config_identity == config_identity and self.status["state"] in {"prelaunch_ready", "awaiting_recovery"}:
                return dict(self.status)
            self.retire(processes)
            if any(char in snapshot.seed_name for char in "\r\n\0"):
                raise ValueError(player_text('launcher.session', 'room_seed_contains_unsupported_control_characters'))
            root = Path(str(config.get("game_root") or config["doom_base_dir"])).resolve()
            if root.name.casefold() == "base":
                root = root.parent
            self._game_exe = root / "DOOMEternalx64vk.exe"
            runtime_manifest = config.get("core_runtime_manifest") or config.get("selected_core_runtime_manifest")
            if not runtime_manifest:
                raise RuntimeError(player_text('launcher.session', 'select_or_download_a_compatible_core_runtime_before_preparing_th'))
            self._runtime = Path(str(runtime_manifest)).resolve()
            if not self._runtime.is_dir():
                self._runtime = self._runtime.parent
            manifest, contents = verify_runtime(self._runtime / "distribution.json", bootstrap_from_archive=True)
            helper = self._runtime / "prepare_vanilla_backup.py"
            if "prepare_vanilla_backup.py" not in contents or helper.read_bytes() != contents["prepare_vanilla_backup.py"]:
                raise RuntimeError(player_text('launcher.session', 'runtime_package_lacks_its_verified_vanilla_protection_helper'))
            self._build = manifest["build_id"]
            for name in ("sentinel_core.dll", "msimg32.dll"):
                if not (root / name).is_file() or (root / name).read_bytes() != contents[name]:
                    raise RuntimeError(player_text('launcher.session', 'installed_game_integration_differs_from_the_qualified_runtime_pr'))
            archive_prelaunch(root / "sentinel-prelaunch.txt", self.state_dir, explicit=recover_prelaunch)
            remote = Path(str(config.get("steam_remote_dir", ""))).resolve()
            if remote.name.casefold() != "remote" or remote.parent.name != "782330" or remote.parent.parent.parent.name.casefold() != "userdata":
                raise RuntimeError(player_text('launcher.session', 'select_the_steam_account_save_provider_before_preparing_the_ap_s'))
            account = remote.parent.parent.name
            if not account.isdecimal() or not 0 < int(account) <= 0xffffffff:
                raise RuntimeError(player_text('launcher.session', 'steam_account_identity_is_invalid'))
            local = Path(str(config["save_games_dir"])).resolve()
            campaign_root = self.data_dir / "campaigns"
            campaign_root.mkdir(parents=True, exist_ok=True)
            native_name = _campaign_native_root(campaign_root, namespace)
            native_dirs = [remote / native_name, local / native_name]
            native_exists = any(path.exists() for path in native_dirs)
            namespace_dir = campaign_root / namespace
            contract, checkpoint = namespace_dir / "campaign.contract", namespace_dir / "campaign.checkpoint"
            if native_exists and not contract.exists() and not checkpoint.exists():
                marker = f"sentinel-owner-{namespace}.txt"
                ownership = (f"sentinel-native-session-v1\nnamespace_id={namespace}\nseed_hex={snapshot.seed_name.encode('utf-8').hex()}\n"
                             f"team={snapshot.team}\nslot={snapshot.slot}\ngeneration_fingerprint={snapshot.slot_data['native_generation_fingerprint']}\n"
                             "provenance=synthetic-fixture\n").encode()
                if all(not path.exists() or (path.is_dir() and all(
                        file.name == marker and file.is_file() and file.read_bytes() == ownership
                        for file in path.iterdir())) for path in native_dirs):
                    native_exists = False
            if not contract.exists() and (checkpoint.exists() or native_exists):
                raise RuntimeError(player_text('launcher.session', 'ap_campaign_metadata_and_native_saves_disagree_inspect_or_recove'))
            intent = "resume" if native_exists or contract.exists() else "create"
            if recovery_basename is not None:
                if not re.fullmatch(r"[a-z0-9-]{1,128}", str(recovery_basename)):
                    raise ValueError(player_text('launcher.session', 'invalid_ap_backup_basename'))
                intent = "recover"
            difficulty = snapshot.slot_data["runtime"]["difficulty"]["engine"]
            if type(difficulty) is not int or not 0 <= difficulty <= 3:
                raise ValueError(player_text('launcher.session', 'room_campaign_difficulty_is_unsupported'))
            descriptor = (f"sentinel-test-session-v3\nseed_hex={snapshot.seed_name.encode('utf-8').hex()}\nteam={snapshot.team}\nslot={snapshot.slot}\n"
                          f"generation_fingerprint={snapshot.slot_data['native_generation_fingerprint']}\n"
                          "slot_data_revision=0.7.0\nsession_mod_contract_revision=2\n"
                          f"provenance=synthetic-fixture\nroot={campaign_root}\ncampaign=unified\nstarting_stage=hub\n"
                          f"difficulty={difficulty}\nintent={intent}\n")
            if intent in {"resume", "recover"}:
                expected = (f"sentinel-campaign-v2\nnamespace={namespace}\ngeneration={snapshot.slot_data['native_generation_fingerprint']}\n"
                            f"provenance=synthetic-fixture\ncampaign=unified\nstarting_stage=hub\ndifficulty={difficulty}\nslot=AUTOSAVE0\n")
                if contract.read_bytes() != expected.encode("utf-8"):
                    raise RuntimeError(player_text('launcher.session', 'ap_campaign_identity_or_immutable_options_differ_from_this_room'))
            self.state_dir.mkdir(parents=True, exist_ok=True)
            descriptor_path = self.state_dir / "ap-session-descriptor.txt"
            if intent == "recover":
                descriptor += f"backup={recovery_basename}\n"
            descriptor_path.write_text(descriptor, encoding="utf-8", newline="\n")
            checkpoint_status = self._prepare_checkpoint(snapshot, config, descriptor_path) if intent == "resume" else {}
            if intent == "recover":
                checkpoint_status = {"state": "awaiting_recovery", "recovery_state": RecoveryState.RECOVERING.value,
                                     "preservation_path": str(self.backup_local_files(snapshot, config))}
            mode = "--save-session-prepare" if intent == "create" or not namespace_dir.exists() else "--save-session-reopen"
            plan = self._probe(mode, str(descriptor_path))
            if plan.get("namespace_id") != namespace:
                raise RuntimeError(player_text('launcher.session', 'core_storage_returned_a_different_room_namespace'))
            if plan.get("native_root") != _campaign_native_root(campaign_root, namespace):
                raise RuntimeError(player_text('launcher.session', 'core_storage_returned_a_different_campaign_provider'))
            if intent == "recover":
                self.verify_backup(str(recovery_basename), namespace=namespace)
            spec = importlib.util.spec_from_file_location("_sentinel_vanilla_protection", helper)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            backup = self.data_dir.parent / "backups" / "vanilla" / namespace / secrets.token_hex(16)
            backup.parent.mkdir(parents=True, exist_ok=True)
            try:
                protection = module.protect(SimpleNamespace(action="prepare", steam_account=account,
                    steam_app_root=str(remote.parent), local_provider_root=str(local), backup_directory=str(backup),
                    ap_root=str(campaign_root), uninstall_root=[str(self.state_dir)], reference_directory=None,
                    run_backup_parent=None, slot_data_revision="0.7.0", mod_contract_revision=2))
            except (module.Refused, OSError, ValueError) as error:
                refused = RuntimeError(player_text('launcher.session', 'vanilla_protection_could_not_be_verified_ap_activation_refused_v', value_0=f'{error}'))
                refused.stage = getattr(error, "stage", "vanilla_protection")
                metadata = getattr(error, "private_metadata", None) or {}
                refused.operation = metadata.get("operation", refused.stage)
                refused.source_path = metadata.get("source_path") or metadata.get("path") or getattr(error, "filename", None)
                refused.destination_path = metadata.get("destination_path") or getattr(error, "filename2", None) or str(backup)
                refused.cause_errno = getattr(error, "errno", None) or metadata.get("errno")
                refused.cause_winerror = getattr(error, "win32_error", None) or getattr(error, "winerror", None)
                refused.cause_filename = getattr(error, "filename", None) or metadata.get("filename")
                refused.cause_filename2 = getattr(error, "filename2", None) or metadata.get("filename2")
                raise refused from error
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.GetCurrentProcess.restype = wintypes.HANDLE
            api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            api.GetProcessTimes.restype = wintypes.BOOL
            api.CreateSemaphoreW.argtypes = [ctypes.c_void_p, wintypes.LONG, wintypes.LONG, wintypes.LPCWSTR]
            api.CreateSemaphoreW.restype = wintypes.HANDLE
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            api.CloseHandle.restype = wintypes.BOOL
            times = [wintypes.FILETIME() for _ in range(4)]
            if not api.GetProcessTimes(api.GetCurrentProcess(), *(ctypes.byref(value) for value in times)):
                raise ctypes.WinError(ctypes.get_last_error())
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            if os.environ.get("SENTINEL_AP_TEST_SESSION"):
                raise RuntimeError(player_text('launcher.session', 'a_conflicting_native_session_environment_is_present'))
            text = (f"sentinel-run-v1\nrun={secrets.token_hex(16)}\nprotection={protection['reference_manifest_sha256']}\n"
                    f"owner={os.getpid()}\ncreated={created}\n" + descriptor).encode("utf-8")
            control_hash = hashlib.sha256(text).hexdigest()
            handle = api.CreateSemaphoreW(None, 1, 1, "Local\\SentinelA-" + control_hash)
            if not handle or ctypes.get_last_error() == 183:
                if handle:
                    api.CloseHandle(handle)
                raise RuntimeError(player_text('launcher.session', 'could_not_acquire_the_one_use_ap_prelaunch_lease'))
            control = root / "sentinel-prelaunch.txt"
            try:
                with control.open("xb") as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                api.CloseHandle(handle)
                raise
            self._api, self._handle = api, handle
            self._control, self._control_bytes = control, text
            self.namespace = namespace
            self._config_identity = config_identity
            self.status = {"state": "prelaunch_ready", "ready": False, "namespace_id": namespace,
                           "control_sha256": control_hash, "intent": intent, "build_id": self._build,
                           "backup_directory": str(backup), **checkpoint_status}
            return dict(self.status)

    def _prepare_checkpoint(self, snapshot, config, descriptor):
        archive = self.backup_local_files(snapshot, config)
        self.status = {"state": "recovery_blocked", "ready": False, "preservation_path": str(archive),
                       "namespace_id": namespace_id(snapshot.seed_name, snapshot.team, snapshot.slot,
                           snapshot.slot_data["native_generation_fingerprint"])}
        try:
            plan = CheckpointPlan.decode(self._probe("--save-session-checkpoint-plan", str(descriptor)))
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as cause:
            self.status.update(recovery_state=RecoveryState.BLOCKED.value, recovery_reason=str(cause))
            raise CheckpointRecoveryBlockedError(str(cause)) from cause
        status = {**plan.projection(), "preservation_path": str(archive),
                  "new_save_available": plan.state is CheckpointState.INTERRUPTED and plan.checkpoint == 0}
        if not plan.resume_allowed and not plan.recoverable:
            self.status.update(status, recovery_reason=plan.state.value, recovery_state=RecoveryState.BLOCKED.value)
            raise CheckpointRecoveryBlockedError(plan.state.value)
        if plan.recoverable:
            status.update(state="awaiting_recovery", recovery_state=RecoveryState.RECOVERING.value)
        return status

    def verify_backup(self, basename: str, *, namespace=None) -> dict:
        if not re.fullmatch(r"[a-z0-9-]{1,128}", basename):
            raise ValueError(player_text('launcher.session', 'invalid_ap_backup_basename'))
        result = self._probe("--save-session-verify-backup", str(self.state_dir / "ap-session-descriptor.txt"), basename)
        if (result.get("archive_verified") is not True or result.get("owner_bound") is not True
                or result.get("quarantine") is not False or result.get("namespace") != (namespace or self.namespace)):
            raise RuntimeError(player_text('launcher.session', 'backup_lacks_compatible_ap_identity_and_native_owner_proof'))
        return result

    def game_processes(self, config: dict):
        if os.name == "nt":
            return windows_game_processes()
        from doom_eap.runtime.proton import windows_processes
        return windows_processes(config, self.client_dir / "APSessionOwner.exe")

    def inspect_campaign(self, snapshot, config):
        namespace = namespace_id(snapshot.seed_name, snapshot.team, snapshot.slot,
                                 snapshot.slot_data["native_generation_fingerprint"])
        campaign_root = self.data_dir / "campaigns"
        directory = campaign_root / namespace

        paths = {name: _inspect_save_path(directory / name, "file")
                 for name in ("native.root", "campaign.contract", "campaign.checkpoint")}
        result = {"namespace_id": namespace, "seed": snapshot.seed_name, "team": snapshot.team,
                  "slot": snapshot.slot, "paths": paths, "native_root_source": "derived",
                  "session": dict(self.status), "native_load_verified": False,
                  "native_backup_available": os.name == "nt" and self.status.get("ready") is True
                      and self.status.get("namespace_id") == namespace}
        if paths["native.root"]["status"] in {"inaccessible", "wrong_type"}:
            result["status"] = paths["native.root"]["status"]
            return result
        try:
            native = _campaign_native_root(campaign_root, namespace)
            if paths["native.root"]["status"] == "present":
                result["native_root_source"] = "native.root"
        except OSError as error:
            result.update(status="inaccessible", reason=str(error))
            return result
        except (RuntimeError, UnicodeError) as error:
            result.update(status="metadata_invalid", reason=str(error))
            return result
        for name, key in (("steam_native", "steam_remote_dir"), ("local_native", "save_games_dir")):
            if config.get(key):
                root = Path(str(config[key])).expanduser().resolve() / native
                paths[name] = _inspect_save_path(root, "directory")
                paths[name + "_autosave"] = _inspect_save_path(root / 'GAME-AUTOSAVE0', "directory")
            else:
                paths[name] = {"status": "not_configured", "path": None}
        expected = (f"sentinel-campaign-v2\nnamespace={namespace}\ngeneration={snapshot.slot_data['native_generation_fingerprint']}\n"
                    f"provenance=synthetic-fixture\ncampaign=unified\nstarting_stage=hub\n"
                    f"difficulty={snapshot.slot_data['runtime']['difficulty']['engine']}\nslot=AUTOSAVE0\n").encode()
        _inspect_campaign_metadata(directory, paths, expected)
        statuses = {item["status"] for item in paths.values()}
        metadata = [paths[name]["status"] == "present" for name in ("campaign.contract", "campaign.checkpoint")]
        autosave = any(paths.get(name + "_autosave", {}).get("status") == "present"
                       for name in ("steam_native", "local_native"))
        if "inaccessible" in statuses:
            result["status"] = "inaccessible"
        elif "wrong_type" in statuses:
            result["status"] = "wrong_type"
        elif "metadata_invalid" in statuses:
            result["status"] = "metadata_invalid"
        elif "not_configured" in statuses:
            result["status"] = "not_configured"
        elif any(metadata) and not all(metadata):
            result["status"] = "partial_metadata"
        elif all(metadata):
            result["status"] = "files_present" if autosave else "metadata_only"
        else:
            result["status"] = "native_only" if autosave else "absent"
        descriptor = self.state_dir / "ap-session-descriptor.txt"
        if os.name == "nt" and descriptor.is_file() and hasattr(self, "_runtime"):
            text = descriptor.read_text(encoding="utf-8")
            identity = (''.join((f"seed_hex={snapshot.seed_name.encode('utf-8').hex()}", '', '\n', 'team=', player_text('launcher.ui', 'value_0', value_0=f'{snapshot.team}'), '', '\n', 'slot=', player_text('launcher.ui', 'value_0', value_0=f'{snapshot.slot}'), '', '\n', 'generation_fingerprint=', player_text('launcher.ui', 'value_0', value_0=f"{snapshot.slot_data['native_generation_fingerprint']}"), '', '\n', '')))
            if identity in text and f"root={campaign_root}\n" in text:
                try:
                    result["storage"] = self._probe("--save-session-plan", str(descriptor))
                    checkpoint = self._probe("--save-session-checkpoint-plan", str(descriptor))
                    plan = CheckpointPlan.decode(checkpoint)
                    result.update(plan.projection())
                    result["status"] = plan.state.value
                    result["new_save_available"] = plan.state is CheckpointState.INTERRUPTED and plan.checkpoint == 0
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    result["storage"] = {"status": "refused", "reason": str(error)}
                    if getattr(error, "storage_outcome", None) == "busy":
                        result["status"] = "session_busy"
        if self.status.get("namespace_id") == namespace and self.status.get("state") in {"admitted", "awaiting_admission", "recovering"}:
            result["status"] = "session_busy"
        return result

    def backup_local_files(self, snapshot, config):
        from .ledger_preservation import delivery_backup, room_key

        with self._lock:
            if self.game_processes(config) != ():
                raise RuntimeError(player_text('launcher.session', 'close_doom_eternal_before_copying_saves'))
            inspection = self.inspect_campaign(snapshot, config)
            if inspection["status"] in {"inaccessible", "wrong_type", "metadata_invalid", "not_configured", "session_busy"}:
                raise RuntimeError(player_text('launcher.session', 'inspect_ap_save_details_before_copying_local_files') + inspection["status"])
            namespace = inspection["namespace_id"]
            directory = self.data_dir / "campaigns" / namespace
            sources = self._preservation_sources(snapshot, inspection, directory)
            if not sources:
                raise RuntimeError(player_text('launcher.session', 'no_local_files_are_available_for_this_room'))
            raw = None
            if config.get("client_state_file"):
                try:
                    with Path(config["client_state_file"]).open("rb") as stream:
                        contents = stream.read(16 * 1024 * 1024 + 1)
                except FileNotFoundError:
                    contents = None
                if contents is not None:
                    if len(contents) > 16 * 1024 * 1024:
                        raise RuntimeError(player_text('launcher.session', 'local_ap_receipt_state_is_too_large_to_back_up'))
                    state = json.loads(contents)
                    if not isinstance(state, dict) or not isinstance(state.get("sessions"), dict):
                        raise RuntimeError(player_text('launcher.session', 'local_ap_receipt_state_is_unreadable'))
                    prefix = f"{snapshot.seed_name}:{snapshot.team}:{snapshot.slot}"
                    raw = json.dumps({"version": state.get("version"), "sessions": {
                        key: value for key, value in state["sessions"].items()
                        if key in {prefix, prefix + ":" + snapshot.slot_data["native_generation_fingerprint"]}
                    }}).encode("utf-8")
            archive = self.data_dir / "campaigns/local-copies" / secrets.token_hex(16) / "campaign.zip"
            with delivery_backup(config, room_key(snapshot), archive.parent.parent) as ledger_snapshot:
                _archive_campaign_files(archive, snapshot, namespace, list(dict.fromkeys(sources)), raw, ledger_snapshot)
            if self.game_processes(config) != ():
                raise RuntimeError(player_text('launcher.session', 'doom_eternal_started_during_the_copy_its_completeness_is_unconfi'))
            return archive

    def _preservation_sources(self, snapshot, inspection, directory):
        sources = [directory] if directory.is_dir() else []
        sources.extend(self.data_dir / "campaigns" / name for name in self.list_backups(snapshot))
        for name in ("steam_native", "local_native"):
            entry = inspection["paths"][name]
            if entry["status"] == "present":
                if inspection["native_root_source"] != "native.root":
                    raise RuntimeError(player_text('launcher.session', 'the_native_save_provider_is_derived_its_room_association_is_unco'))
                sources.append(Path(entry["path"]))
        return sources

    def restart_campaign(self, snapshot, config: dict) -> Path:
        from doom_eap.contracts.command_publication import queue_session_namespace
        from doom_eap.runtime.client_state_store import ClientStateStore
        from doom_eap.runtime.item_reconciliation import (
            CLIENT_STATE_VERSION,
            default_session_state,
            migrate_client_state,
        )

        from .ledger_preservation import audit_new_save, delivery_backup, delivery_restart, finish_new_save, room_key

        with self._lock:
            processes = self.game_processes(config)
            if processes != ():
                raise RuntimeError(player_text('launcher.session', 'exit_doom_eternal_before_creating_a_new_ap_game_save_its_process'))
            with delivery_restart(config, room_key(snapshot)) as ledger:
                self.retire(processes)
                root = Path(config["doom_base_dir"]).resolve()
                if (root.parent / "sentinel-prelaunch.txt").exists():
                    raise RuntimeError(player_text('launcher.session', 'another_launcher_owns_an_ap_session_close_it_before_restarting'))
                remote = Path(config["steam_remote_dir"]).resolve()
                if remote.name.casefold() != "remote" or remote.parent.name != "782330" or remote.parent.parent.parent.name.casefold() != "userdata":
                    raise RuntimeError(player_text('launcher.session', 'select_the_steam_account_save_provider_before_restarting'))
                local = Path(config["save_games_dir"]).resolve()
                namespace = namespace_id(snapshot.seed_name, snapshot.team, snapshot.slot,
                                         snapshot.slot_data["native_generation_fingerprint"])
                prefix = f"{snapshot.seed_name}:{snapshot.team}:{snapshot.slot}"
                state_key = prefix + ":" + snapshot.slot_data["native_generation_fingerprint"]
                state_file = Path(config["client_state_file"]).resolve()
                raw = None
                if state_file.exists():
                    with state_file.open("rb") as stream:
                        raw = stream.read(16 * 1024 * 1024 + 1)
                if raw is not None and len(raw) > 16 * 1024 * 1024:
                    raise RuntimeError(player_text('launcher.session', 'local_ap_receipt_state_is_too_large_to_back_up'))
                state = json.loads(raw) if raw is not None else {"version": CLIENT_STATE_VERSION, "sessions": {}}
                if not isinstance(state, dict) or not isinstance(state.get("sessions"), dict):
                    raise RuntimeError(player_text('launcher.session', 'local_ap_receipt_state_is_unreadable_restart_refused'))
                logger = logging.getLogger(__name__)
                store = ClientStateStore(state_file, version=CLIENT_STATE_VERSION, migrate=migrate_client_state,
                    logger=logger, log_event=lambda kind, **details: logger.info("%s %s", kind, details))
                keys = {key for key in state["sessions"] if key == prefix or key.startswith(prefix + ":")}
                native_name = _campaign_native_root(self.data_dir / "campaigns", namespace)
                sources = [remote / native_name, local / native_name,
                           self.data_dir / "campaigns" / namespace]
                sources.extend(self.data_dir / "campaigns" / name for name in self.list_backups(snapshot))
                queue = root / "ap_queue"
                for key in keys | {state_key}:
                    sources.extend(queue.glob(f"recv-{queue_session_namespace(key)}-*"))
                event_key = None
                event_session = local / "ap_event_session.json"
                if event_session.is_file():
                    event_key = json.loads(event_session.read_text(encoding="utf-8")).get("ap_state_key")
                if event_key is None or event_key in keys | {state_key}:
                    for pattern in ("ap_event_*.txt", "ap_active_map*.txt", "ap_telemetry*.txt", "ap_condump*.txt", "ap_event_session.json"):
                        sources.extend(local.glob(pattern))
                    sources.extend(root.glob("ap_transition_*.evt"))
                    from doom_eap.content.publisher_loader import load_publisher_contracts
                    sources.extend(local / trigger["filename"] for publisher in load_publisher_contracts()
                                   for trigger in publisher.triggers_for("map_event_file"))
                sources = list(dict.fromkeys(path for path in sources if path.exists() or path.is_symlink()))
                token = secrets.token_hex(16)
                backup = self.data_dir / "campaigns" / "restarts" / token
                archive = backup / "campaign.zip"
                with delivery_backup(config, room_key(snapshot), backup.parent) as ledger_snapshot:
                    _archive_campaign_files(archive, snapshot, namespace, sources, raw, ledger_snapshot)
                if self.game_processes(config) != ():
                    raise RuntimeError(player_text('launcher.session', 'doom_eternal_started_during_backup_existing_saves_were_kept'))
                moved = []
                epoch_binding = None
                try:
                    epoch_binding = asyncio.run(audit_new_save(ledger, room_key(snapshot), token, archive))
                    for source in sources:
                        target = source.with_name("." + source.name + "-restart-" + token)
                        os.replace(source, target)
                        moved.append((source, target))
                    _restart_receipt_state(state, keys, state_key, ledger is not None, default_session_state)
                    store.commit(state, reason="campaign_restart")
                    finish_new_save(ledger, epoch_binding, token, applied=True)
                except BaseException:
                    for source, target in reversed(moved):
                        os.replace(target, source)
                    if raw is not None:
                        store.commit(json.loads(raw), reason="campaign_restart_rollback")
                    elif state_file.exists():
                        state_file.unlink()
                    finish_new_save(ledger, epoch_binding, token, applied=False)
                    raise
                self.namespace = ""
                return archive

    def list_backups(self, snapshot) -> list[str]:
        namespace = namespace_id(snapshot.seed_name,snapshot.team,snapshot.slot,
                                 snapshot.slot_data["native_generation_fingerprint"])
        candidates=[]
        for path in (self.data_dir / "campaigns").glob("transport-backup-*"):
            manifest=path / "transport.manifest"
            if not path.is_symlink() and manifest.is_file() and manifest.stat().st_size <= 65536:
                if f"namespace={namespace}\n" in manifest.read_text(encoding="utf-8"):
                    candidates.append(path.name)
        return sorted(candidates,reverse=True)

    def create_backup(self) -> dict:
        scope=self.observe()
        if os.name != "nt" or not scope.get("ready"):
            raise RuntimeError(player_text('launcher.session', 'native_ap_backup_requires_a_fresh_qualified_game_session'))
        namespace=self.namespace
        run=subprocess.run([str(self._runtime / "sentinel_probe.exe"),"--native-backup",
            "--pid",str(scope["pid"]),"--namespace",namespace,"--campaign","game","--slot","0","--json"],
            capture_output=True,timeout=18,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        reports=[json.loads(line) for line in run.stdout.splitlines() if line.strip()]
        request=next((value for value in reports if value.get("operation")=="native_backup_request"),{})
        result=reports[-1] if reports else {}
        current=self.observe()
        if (not current.get("ready") or request.get("pid")!=scope["pid"]
                or request.get("process_created")!=str(scope["process_created"])
                or request.get("instance_id")!=scope["admission"].get("instance_id")
                or request.get("namespace")!=namespace or self.namespace!=namespace or run.returncode
                or result.get("state")!="complete"):
            raise RuntimeError(player_text('launcher.session', 'native_backup_not_confirmed_retained_archives_remain_available_n'))
        self.verify_backup(result["basename"],namespace=namespace)
        return result

    def observe(self, processes=_UNSET) -> dict:
        with self._lock:
            if self._remote is not None:
                self.status = self._remote.observe()
                if self._remote.process is not None and self._remote.closable and (
                        self._remote.process.poll() is not None or self._remote.process.stdin.closed):
                    self._remote.retire()
                return dict(self.status)
            if not self._handle:
                return dict(self.status)
            processes = windows_game_processes() if processes is _UNSET else processes
            if processes is None:
                self.status.update(state="process_unknown", ready=False)
                self._capture_admission_failure(error=player_text('launcher.session', 'game_process_identity_query_failed'), processes=None)
                return dict(self.status)
            if not processes:
                if self._process is not None:
                    self.retire(processes)
                    self.status.update(state="game_exited", ready=False)
                return dict(self.status)
            path_matches = False
            path_error = None
            if len(processes) == 1:
                try:
                    path_matches = same_executable_path(processes[0]["path"], self._game_exe)
                except OSError as error:
                    path_error = error
            if not path_matches:
                self.status.update(state="process_ambiguous", ready=False)
                self._capture_admission_failure(error=path_error or player_text('launcher.session', 'game_process_count_or_executable_does_not_match'),
                                                processes=processes)
                return dict(self.status)
            process = processes[0]
            identity = process["pid"], process["created"]
            if self._process is not None and identity != self._process:
                self.status.update(state="process_changed", ready=False)
                self._capture_admission_failure(error=player_text('launcher.session', 'the_prepared_game_process_identity_changed'), processes=processes)
                return dict(self.status)
            self._process = identity
            self.status.update(pid=process["pid"], process_created=process["created"])
            try:
                admission = self._probe("--pid", str(process["pid"]), "--save-admission", "--json")
            except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                self.status.update(state="awaiting_admission", ready=False)
                self._capture_admission_failure(process, error=error)
                return dict(self.status)
            matches = (admission.get("state") == "admitted" and admission.get("accepting_requests") is True
                       and admission.get("namespace_id") == self.namespace and admission.get("build_id") == self._build
                       and admission.get("target_pid") == process["pid"] and admission.get("server_pid") == process["pid"]
                       and admission.get("process_created") == str(process["created"]))
            instance = admission.get("instance_id")
            matches = matches and instance is not None and (self._instance is None or instance == self._instance)
            if matches:
                self._instance = instance
            self.status.update(state="admitted" if matches else "admission_refused", ready=matches,
                               pid=process["pid"], process_created=process["created"], admission=admission)
            self._observe_checkpoint(matches)
            if not matches:
                self._capture_admission_failure(process, admission)
            return dict(self.status)

    def _observe_checkpoint(self, admitted):
        if self.status.get("intent") not in {"resume", "recover"}:
            return
        config = dict(zip(_INSTALL_KEYS, self._config_identity, strict=True))
        recovery, verified, reason = recovery_observation(config, self.status)
        if self.status.get("resume_allowed") is False and recovery is RecoveryState.NONE:
            recovery = RecoveryState.RECOVERING
        self.status.update(recovery_state=recovery.value, recovery_reason=reason, resume_verified=verified)
        if recovery is RecoveryState.BLOCKED:
            self.status.update(state="recovery_blocked", ready=False)
        elif admitted and recovery is RecoveryState.RECOVERING:
            self.status.update(state="recovering", ready=False)
        elif admitted and not verified:
            self.status.update(state="awaiting_resume", ready=False)

    def can_close(self) -> bool:
        if self._remote is not None:
            return self._remote.process is None or self._remote.closable
        if not self._handle:
            return True
        processes = windows_game_processes()
        return processes == ()

    def retire(self, processes=_UNSET) -> None:
        from .launcher_core_install import game_write_lock
        while True:
            with self._lock:
                control = self._control
                if control is None:
                    return self._retire(processes)
            with game_write_lock(control.parent), self._lock:
                if self._control == control:
                    return self._retire(processes)

    def _retire(self, processes=_UNSET) -> None:
        with self._lock:
            if self._remote is not None:
                self._remote.retire()
                self.status = {"state": "not_prepared", "ready": False}
                return
            if not self._handle:
                return
            processes = windows_game_processes() if processes is _UNSET else processes
            if processes != ():
                raise RuntimeError(player_text('launcher.ui', 'close_doom_eternal_before_closing_this_launcher'))
            self._api.CloseHandle(self._handle)
            self._handle = None
            if self._control.exists() and self._control.read_bytes() == self._control_bytes:
                self._control.unlink()
            self._control = None
            self._process = None
            self._instance = None
            self.status = {"state": "not_prepared", "ready": False}
