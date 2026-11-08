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
from doom_eap.runtime.inventory import namespace_id
from doom_eap.runtime.observers import same_executable_path, windows_game_processes
from doom_eap.strings import PlayerError

from .checkpoint import CheckpointPlan, CheckpointState, RecoveryState, recovery_observation
from .ui.strings import checkpoint_blocked


def _death_policy_line(slot_data):
    policy = slot_data.get("runtime", {}).get("difficulty", {}).get("death_policy", "normal")
    return "death_policy=restart_stage\n" if policy == "restart_stage" else ""


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


def _require_stale_prelaunch_lease(state, raw):
    if state == "legacy":
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenSemaphoreW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        api.OpenSemaphoreW.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenSemaphoreW(0x100000, False, "Local\\SentinelA-" + hashlib.sha256(raw).hexdigest())
        if handle:
            api.CloseHandle(handle)
            raise RuntimeError('A live prelaunch lease exists; its marker was preserved')
        if ctypes.get_last_error() != 2:
            raise RuntimeError('Prelaunch lease ownership cannot be inspected; marker preserved')

def _prelaunch_marker_owner(raw):
    match = re.match(rb"sentinel-run-v1\nrun=[0-9a-f]{32}\nprotection=[0-9a-f]{64}\nowner=([1-9][0-9]*)\ncreated=([1-9][0-9]*)\nsentinel-test-session-v3\n", raw)
    recognized = match and len(raw) <= 65536 and int(match[1]) <= 0xffffffff and int(match[2]) <= 0xffffffffffffffff
    state = prelaunch_owner_state(int(match[1]), int(match[2])) if recognized else "legacy"
    return state

def archive_prelaunch(path, state_dir, *, explicit=False):
    if not path.exists():
        return None
    with path.open("rb") as stream:
        raw = stream.read(65537)
    state = _prelaunch_marker_owner(raw)
    if state == "alive":
        raise RuntimeError('Another live launcher owns this game preparation; close that launcher before retrying')
    if state == "unknown" or (state == "legacy" and not explicit):
        raise RuntimeError('Prelaunch ownership is unknown. Use Repair Session with DOOM closed to inspect or archive this marker')
    _require_stale_prelaunch_lease(state, raw)
    directory = state_dir / "prelaunch-backups"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (hashlib.sha256(raw).hexdigest() + "-" + secrets.token_hex(8) + ".txt")
    if path.read_bytes() != raw:
        raise RuntimeError('Prelaunch marker changed during recovery; retry inspection')
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
        raise RuntimeError('AP campaign provider metadata is unreadable')
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
            raise RuntimeError('AP backup destination must be outside its sources')
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
            raise RuntimeError('AP backup refuses linked or special save files')
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


def _verify_campaign_archive(archive, files, raw):
    with zipfile.ZipFile(archive) as saved:
        if saved.testzip() is not None or any(hashlib.sha256(saved.read(name)).hexdigest() != digest
                                            for name, (_, digest) in files.items()):
            raise RuntimeError('AP backup verification failed; existing saves were kept')
        if raw is not None and saved.read("client_state.json") != raw:
            raise RuntimeError('AP receipt backup verification failed; existing saves were kept')

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
    _verify_campaign_archive(archive, files, raw)
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

    def _supervisor_status(self, response):
        if "status" in response:
            if not isinstance(response["status"], dict):
                raise SessionSupervisorError('Invalid Windows session status')
            self.status = response["status"]
            if self._runtime_details is not None:
                self.status["proton_runtime"] = self._runtime_details
            self._game_observed |= bool(self.status.get("pid"))

    def _supervisor_error(self, response):
        if "error" in response:
            error = RuntimeError(response["error"])
            for key, value in response.get("failure", {}).items():
                if key in {"code", "failure_domain", "stage", "operation", "source_path", "destination_path", "errno", "winerror", "filename", "filename2", "proton_reason", "proton_candidates", "proton_selected"}:
                    setattr(error, key, value)
            raise error

    def request(self, action, *, timeout=10, **payload):
        if self.process is None or self.process.poll() is not None:
            raise SessionSupervisorError('The Windows AP session supervisor is unavailable')
        self.process.stdin.write(json.dumps({"action": action, **payload}) + "\n")
        self.process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout):
                self.closable = False
                raise SessionSupervisorError('The Windows AP session supervisor did not respond')
        raw = self.process.stdout.readline(SESSION_PIPE_LIMIT + 1)
        response = json.loads(raw)
        if not isinstance(response, dict) or type(response.get("can_close")) is not bool:
            raise SessionSupervisorError('Invalid Windows session supervisor response')
        self.closable = response["can_close"]
        self._supervisor_status(response)
        self._supervisor_error(response)
        return self.status

    def _windows_session_config(self, config, windows_path):
        converted = {key: config[key] for key in _INSTALL_KEYS if key in config}
        for key in ("game_root", "doom_base_dir", "steam_remote_dir", "save_games_dir", "core_runtime_manifest", "selected_core_runtime_manifest", "client_state_file", "delivery_ledger_file"):
            if converted.get(key):
                converted[key] = windows_path(converted[key])
        if not converted.get("game_root"):
            converted["game_root"] = windows_path(Path(config["doom_base_dir"]).parent)
        return converted

    def _reuse_supervisor(self, namespace, config_identity):
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

    def prepare(self, snapshot, config, *, recover_prelaunch=False):
        from doom_eap.runtime.bridge_config import runtime, windows_path
        namespace = namespace_id(snapshot.seed_name, snapshot.team, snapshot.slot,
                                 snapshot.slot_data["native_generation_fingerprint"])
        config_identity = tuple(config.get(key) for key in _INSTALL_KEYS)
        reused = self._reuse_supervisor(namespace, config_identity)
        if reused is not None:
            return reused
        executable = self.client_dir / "APSessionOwner.exe"
        if not executable.is_file():
            raise RuntimeError('The packaged Windows AP session supervisor is unavailable')
        command, environment = runtime(config)
        self._runtime_details = {"source": "manual" if config.get("proton_executable") else "config_info",
            "executable": command[0], "tool": Path(command[0]).parent.name,
            "compat_data_path": environment["STEAM_COMPAT_DATA_PATH"],
            "wineprefix": environment["WINEPREFIX"], "steam_install": environment["STEAM_COMPAT_CLIENT_INSTALL_PATH"]}
        converted = self._windows_session_config(config, windows_path)
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
        from doom_eap.runtime.bridge_config import windows_processes
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
            return False
        if self.process.poll() is None and not self.process.stdin.closed:
            self.request("retire")
            self.process.stdin.close()
        elif not self.closable:
            raise RuntimeError('Confirm DOOM Eternal has exited before retiring its session supervisor')
        self.process.wait(timeout=10)
        self.process = None
        self.closable = True
        return True


def stop_native_client(process):
    if process is None or process.poll() is not None:
        return False
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1.0)
    return True


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
            raise RuntimeError('Core session probe did not return a valid response') from error
        if not isinstance(value, dict):
            raise RuntimeError('Core session probe returned an invalid object')
        if result.returncode and "--save-admission" not in arguments:
            outcome = value.get("outcome", value.get("result", "unknown"))
            refused = RuntimeError('Core session refused: {value_0} (error {value_1})'.format(value_0=f'{outcome}', value_1=f"{value.get('win32_error', 0)}"))
            refused.storage_outcome = outcome
            raise refused
        return value

    def _decode_admission_probe(self, raw, query, process):
        try:
            response = json.loads(raw)
            if not isinstance(response, dict):
                raise ValueError('Core diagnostic returned an invalid object')
            query["response"] = response
            query["identity_matches"] = (response.get("target_pid") == process["pid"]
                and response.get("server_pid") == process["pid"]
                and str(response.get("process_created")) == str(process["created"]))
        except ValueError as invalid:
            query.update(error=str(invalid), stdout=raw)

    def _failed_probe_output(self, failed, query):
        for stream in ("stdout", "stderr"):
            value = getattr(failed, stream, None)
            if value:
                query[stream] = value[:4096].decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)[:4096]

    def _admission_failure_queries(self, processes, document, process):
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
                self._decode_admission_probe(raw, query, process)
            except (OSError, subprocess.SubprocessError) as failed:
                query.update(error_type=type(failed).__name__, error=str(failed))
                self._failed_probe_output(failed, query)

    def _admission_capture_due(self, processes, process, admission):
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
        return (candidates,)

    def _completed_admission_capture(self, document):
        native = document["queries"].get("--native", {})
        if (native.get("identity_matches") and native.get("response", {}).get("availability") in {"enabled", "disabled", "retained"}
                and all(query.get("identity_matches") and query.get("returncode") == 0 for query in document["queries"].values())):
            self._admission_capture_attempts = 4

    def _capture_admission_failure(self, process=None, admission=None, error=None, *, processes=_UNSET):
        runtime = getattr(self, "_runtime", None)
        if runtime is None:
            return
        admission = admission or {}
        process = process or {}
        due = self._admission_capture_due(processes, process, admission)
        if due is None:
            return
        candidates, = due
        document = {"at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "pid": process.get("pid"), "process_created": str(process.get("created")),
                    "namespace_id": self.namespace, "build_id": self._build,
                    "probe": str(runtime / "sentinel_probe.exe"), "attempt": self._admission_capture_attempts,
                    "admission": admission, "admission_error": str(error) if error else None, "queries": {}}
        document.update(state=self.status.get("state"), expected_game_path=str(self._game_exe),
                        expected_process=self._process, processes=candidates)
        self._admission_failure_queries(processes, document, process)
        self._completed_admission_capture(document)
        try:
            from doom_eap.launcher.launcher_platform import _atomic_write_bytes
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

    def _prepare_remote_session(self, recovery_basename, snapshot, config, recover_prelaunch, namespace):
        if os.name != "nt":
            if recovery_basename is not None:
                raise RuntimeError('Recovery requires a qualified native helper in this environment')
            if self._remote is None:
                self._remote = _ProtonSessionOwner(self.client_dir, self.data_dir, self.state_dir)
            self.status = self._remote.prepare(snapshot, config, recover_prelaunch=recover_prelaunch)
            self.namespace = namespace
            return dict(self.status)

    def _reusable_prelaunch_status(self, recovery_basename, namespace, config_identity):
        if recovery_basename is None and self._handle and namespace == self.namespace and self._config_identity == config_identity and self.status["state"] in {"prelaunch_ready", "awaiting_recovery"}:
            return dict(self.status)

    def _prepared_session_status(self, recovery_basename, namespace, config_identity):
        processes = windows_game_processes()
        if processes is None:
            raise RuntimeError('Game process identity is unknown; AP session preparation refused')
        if processes:
            if recovery_basename is not None:
                raise RuntimeError('Close DOOM Eternal before restoring a backup.')
            if self._handle and namespace == self.namespace and self._config_identity == config_identity:
                return self.observe(processes)
            raise RuntimeError('Close DOOM Eternal before preparing this save.')
        reused = self._reusable_prelaunch_status(recovery_basename, namespace, config_identity)
        if reused is not None:
            return reused
        return processes

    def _session_runtime(self, snapshot, config):
        if any(char in snapshot.seed_name for char in "\r\n\0"):
            raise ValueError('Room seed contains unsupported control characters')
        root = Path(str(config.get("game_root") or config["doom_base_dir"])).resolve()
        if root.name.casefold() == "base":
            root = root.parent
        self._game_exe = root / "DOOMEternalx64vk.exe"
        runtime_manifest = config.get("core_runtime_manifest") or config.get("selected_core_runtime_manifest")
        if not runtime_manifest:
            raise RuntimeError('Select or download a compatible Core runtime before preparing this session')
        self._runtime = Path(str(runtime_manifest)).resolve()
        if not self._runtime.is_dir():
            self._runtime = self._runtime.parent
        manifest, contents = verify_runtime(self._runtime / "distribution.json", bootstrap_from_archive=True)
        helper = self._runtime / "prepare_vanilla_backup.py"
        if "prepare_vanilla_backup.py" not in contents or helper.read_bytes() != contents["prepare_vanilla_backup.py"]:
            raise RuntimeError('Runtime package lacks its verified vanilla-protection helper')
        return root, helper, manifest, contents

    def _native_marker_files(self, native_dirs, marker, ownership):
        if all(not path.exists() or (path.is_dir() and all(
                file.name == marker and file.is_file() and file.read_bytes() == ownership
                for file in path.iterdir())) for path in native_dirs):
            return False
        return True

    def _native_marker_only(self, native_exists, contract, checkpoint, namespace, snapshot, native_dirs):
        if native_exists and not contract.exists() and not checkpoint.exists():
            marker = f"sentinel-owner-{namespace}.txt"
            ownership = (f"sentinel-native-session-v1\nnamespace_id={namespace}\nseed_hex={snapshot.seed_name.encode('utf-8').hex()}\n"
                         f"team={snapshot.team}\nslot={snapshot.slot}\ngeneration_fingerprint={snapshot.slot_data['native_generation_fingerprint']}\n"
                         "provenance=synthetic-fixture\n").encode()
            native_exists = self._native_marker_files(native_dirs, marker, ownership)
        return native_exists

    def _campaign_native_state(self, campaign_root, namespace, remote, local, snapshot):
        native_name = _campaign_native_root(campaign_root, namespace)
        native_dirs = [remote / native_name, local / native_name]
        native_exists = any(path.exists() for path in native_dirs)
        namespace_dir = campaign_root / namespace
        contract, checkpoint = namespace_dir / "campaign.contract", namespace_dir / "campaign.checkpoint"
        native_exists = self._native_marker_only(native_exists, contract, checkpoint, namespace, snapshot, native_dirs)
        if not contract.exists() and (checkpoint.exists() or native_exists):
            raise RuntimeError('AP campaign metadata and native saves disagree; inspect or recover the campaign before playing')
        return namespace_dir, contract, native_exists

    def _campaign_session_provider(self, config, namespace, snapshot, recovery_basename):
        remote = Path(str(config.get("steam_remote_dir", ""))).resolve()
        if remote.name.casefold() != "remote" or remote.parent.name != "782330" or remote.parent.parent.parent.name.casefold() != "userdata":
            raise RuntimeError('Select the Steam account save provider before preparing the AP session')
        account = remote.parent.parent.name
        if not account.isdecimal() or not 0 < int(account) <= 0xffffffff:
            raise RuntimeError('Steam account identity is invalid')
        local = Path(str(config["save_games_dir"])).resolve()
        campaign_root = self.data_dir / "campaigns"
        campaign_root.mkdir(parents=True, exist_ok=True)
        namespace_dir, contract, native_exists = self._campaign_native_state(campaign_root, namespace, remote, local, snapshot)
        intent = "resume" if native_exists or contract.exists() else "create"
        if recovery_basename is not None:
            if not re.fullmatch(r"[a-z0-9-]{1,128}", str(recovery_basename)):
                raise ValueError('Invalid AP backup basename')
            intent = "recover"
        return remote, account, local, campaign_root, namespace_dir, contract, intent

    def _campaign_descriptor(self, snapshot, namespace, campaign_root, intent, contract):
        difficulty = snapshot.slot_data["runtime"]["difficulty"]["engine"]
        death_policy = _death_policy_line(snapshot.slot_data)
        if type(difficulty) is not int or not 0 <= difficulty <= 3:
            raise ValueError('Room campaign difficulty is unsupported')
        descriptor = (f"sentinel-test-session-v3\nseed_hex={snapshot.seed_name.encode('utf-8').hex()}\nteam={snapshot.team}\nslot={snapshot.slot}\n"
                      f"generation_fingerprint={snapshot.slot_data['native_generation_fingerprint']}\n"
                      "slot_data_revision=0.7.0\nsession_mod_contract_revision=2\n"
                      f"provenance=synthetic-fixture\nroot={campaign_root}\ncampaign=unified\nstarting_stage=hub\n"
                      f"difficulty={difficulty}\n{death_policy}intent={intent}\n")
        if intent in {"resume", "recover"}:
            expected = (f"sentinel-campaign-v2\nnamespace={namespace}\ngeneration={snapshot.slot_data['native_generation_fingerprint']}\n"
                        f"provenance=synthetic-fixture\ncampaign=unified\nstarting_stage=hub\n"
                        f"difficulty={difficulty}\n{death_policy}slot=AUTOSAVE0\n")
            if contract.read_bytes() != expected.encode("utf-8"):
                raise RuntimeError('AP campaign identity or immutable options differ from this room')
        return descriptor

    def _vanilla_protection_error(self, error, backup):
        refused = RuntimeError('Vanilla protection could not be verified; AP activation refused: {value_0}'.format(value_0=f'{error}'))
        refused.stage = getattr(error, "stage", "vanilla_protection")
        metadata = getattr(error, "private_metadata", None) or {}
        refused.operation = metadata.get("operation", refused.stage)
        refused.source_path = metadata.get("source_path") or metadata.get("path") or getattr(error, "filename", None)
        refused.destination_path = metadata.get("destination_path") or getattr(error, "filename2", None) or str(backup)
        refused.cause_errno = getattr(error, "errno", None) or metadata.get("errno")
        refused.cause_winerror = getattr(error, "win32_error", None) or getattr(error, "winerror", None)
        refused.cause_filename = getattr(error, "filename", None) or metadata.get("filename")
        refused.cause_filename2 = getattr(error, "filename2", None) or metadata.get("filename2")
        return refused

    def _protect_vanilla_saves(self, helper, namespace, account, remote, local, campaign_root):
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
            refused = self._vanilla_protection_error(error, backup)
            raise refused from error
        return backup, protection

    def _launcher_process_created(self, api):
        times = [wintypes.FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(api.GetCurrentProcess(), *(ctypes.byref(value) for value in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return created

    def _prelaunch_control(self, protection, descriptor, root):
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.GetCurrentProcess.restype = wintypes.HANDLE
        api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        api.GetProcessTimes.restype = wintypes.BOOL
        api.CreateSemaphoreW.argtypes = [ctypes.c_void_p, wintypes.LONG, wintypes.LONG, wintypes.LPCWSTR]
        api.CreateSemaphoreW.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        created = self._launcher_process_created(api)
        if os.environ.get("SENTINEL_AP_TEST_SESSION"):
            raise RuntimeError('A conflicting native session environment is present')
        text = (f"sentinel-run-v1\nrun={secrets.token_hex(16)}\nprotection={protection['reference_manifest_sha256']}\n"
                f"owner={os.getpid()}\ncreated={created}\n" + descriptor).encode("utf-8")
        control_hash = hashlib.sha256(text).hexdigest()
        handle = api.CreateSemaphoreW(None, 1, 1, "Local\\SentinelA-" + control_hash)
        if not handle or ctypes.get_last_error() == 183:
            if handle:
                api.CloseHandle(handle)
            raise RuntimeError('Could not acquire the one-use AP prelaunch lease')
        control = root / "sentinel-prelaunch.txt"
        published = False
        try:
            with control.open("xb") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            published = True
        finally:
            if not published:
                api.CloseHandle(handle)
        return api, handle, control, text, control_hash

    def _verify_session_plan(self, plan, namespace, campaign_root):
        if plan.get("namespace_id") != namespace:
            raise RuntimeError('Core storage returned a different room namespace')
        if plan.get("native_root") != _campaign_native_root(campaign_root, namespace):
            raise RuntimeError('Core storage returned a different campaign provider')

    def _verify_session_dlls(self, root, contents):
        for name in ("sentinel_core.dll", "msimg32.dll"):
            if not (root / name).is_file() or (root / name).read_bytes() != contents[name]:
                raise RuntimeError('Installed game integration differs from the qualified runtime; prepare setup again')

    def _prepare(self, snapshot, config: dict, *, recovery_basename=None, recover_prelaunch=False) -> dict:
        with self._lock:
            config_identity = tuple(config.get(key) for key in _INSTALL_KEYS)
            namespace = namespace_id(snapshot.seed_name, snapshot.team, snapshot.slot,
                                 snapshot.slot_data["native_generation_fingerprint"])
            if os.name != "nt":
                return self._prepare_remote_session(recovery_basename, snapshot, config, recover_prelaunch, namespace)
            prepared = self._prepared_session_status(recovery_basename, namespace, config_identity)
            if isinstance(prepared, dict):
                return prepared
            processes = prepared
            self.retire(processes)
            root, helper, manifest, contents = self._session_runtime(snapshot, config)
            self._build = manifest["build_id"]
            self._verify_session_dlls(root, contents)
            archive_prelaunch(root / "sentinel-prelaunch.txt", self.state_dir, explicit=recover_prelaunch)
            remote, account, local, campaign_root, namespace_dir, contract, intent = self._campaign_session_provider(config, namespace, snapshot, recovery_basename)
            descriptor = self._campaign_descriptor(snapshot, namespace, campaign_root, intent, contract)
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
            self._verify_session_plan(plan, namespace, campaign_root)
            if intent == "recover":
                self.verify_backup(str(recovery_basename), namespace=namespace)
            backup, protection = self._protect_vanilla_saves(helper, namespace, account, remote, local, campaign_root)
            api, handle, control, text, control_hash = self._prelaunch_control(protection, descriptor, root)
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
            raise ValueError('Invalid AP backup basename')
        result = self._probe("--save-session-verify-backup", str(self.state_dir / "ap-session-descriptor.txt"), basename)
        if (result.get("archive_verified") is not True or result.get("owner_bound") is not True
                or result.get("quarantine") is not False or result.get("namespace") != (namespace or self.namespace)):
            raise RuntimeError('Backup lacks compatible AP identity and native owner proof')
        return result

    def game_processes(self, config: dict):
        if os.name == "nt":
            return windows_game_processes()
        from doom_eap.runtime.bridge_config import windows_processes
        return windows_processes(config, self.client_dir / "APSessionOwner.exe")

    def _campaign_metadata_status(self, paths, result):
        metadata = [paths[name]["status"] == "present" for name in ("campaign.contract", "campaign.checkpoint")]
        autosave = any(paths.get(name + "_autosave", {}).get("status") == "present" for name in ("steam_native", "local_native"))
        if any(metadata) and not all(metadata):
            result["status"] = "partial_metadata"
        elif all(metadata):
            result["status"] = "files_present" if autosave else "metadata_only"
        else:
            result["status"] = "native_only" if autosave else "absent"

    def _campaign_file_status(self, paths, result):
        statuses = {item["status"] for item in paths.values()}
        for status in ("inaccessible", "wrong_type", "metadata_invalid", "not_configured"):
            if status in statuses:
                result["status"] = status
                return
        self._campaign_metadata_status(paths, result)

    def _campaign_storage_plan(self, snapshot, campaign_root, result):
        descriptor = self.state_dir / "ap-session-descriptor.txt"
        if os.name == "nt" and descriptor.is_file() and hasattr(self, "_runtime"):
            text = descriptor.read_text(encoding="utf-8")
            identity = (''.join((f"seed_hex={snapshot.seed_name.encode('utf-8').hex()}", '', '\n', 'team=', '{value_0}'.format(value_0=f'{snapshot.team}'), '', '\n', 'slot=', '{value_0}'.format(value_0=f'{snapshot.slot}'), '', '\n', 'generation_fingerprint=', '{value_0}'.format(value_0=f"{snapshot.slot_data['native_generation_fingerprint']}"), '', '\n', '')))
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

    def _campaign_provider_paths(self, config, native, paths):
        for name, key in (("steam_native", "steam_remote_dir"), ("local_native", "save_games_dir")):
            if config.get(key):
                root = Path(str(config[key])).expanduser().resolve() / native
                paths[name] = _inspect_save_path(root, "directory")
                paths[name + "_autosave"] = _inspect_save_path(root / 'GAME-AUTOSAVE0', "directory")
            else:
                paths[name] = {"status": "not_configured", "path": None}

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
        self._campaign_provider_paths(config, native, paths)
        expected = (f"sentinel-campaign-v2\nnamespace={namespace}\ngeneration={snapshot.slot_data['native_generation_fingerprint']}\n"
                    f"provenance=synthetic-fixture\ncampaign=unified\nstarting_stage=hub\n"
                    f"difficulty={snapshot.slot_data['runtime']['difficulty']['engine']}\n"
                    f"{_death_policy_line(snapshot.slot_data)}slot=AUTOSAVE0\n").encode()
        _inspect_campaign_metadata(directory, paths, expected)
        self._campaign_file_status(paths, result)
        self._campaign_storage_plan(snapshot, campaign_root, result)
        if self.status.get("namespace_id") == namespace and self.status.get("state") in {"admitted", "awaiting_admission", "recovering"}:
            result["status"] = "session_busy"
        return result

    def _room_receipt_backup(self, config, snapshot):
        raw = None
        if config.get("client_state_file"):
            try:
                with Path(config["client_state_file"]).open("rb") as stream:
                    contents = stream.read(16 * 1024 * 1024 + 1)
            except FileNotFoundError:
                contents = None
            if contents is not None:
                if len(contents) > 16 * 1024 * 1024:
                    raise RuntimeError('Local AP receipt state is too large to back up')
                state = json.loads(contents)
                if not isinstance(state, dict) or not isinstance(state.get("sessions"), dict):
                    raise RuntimeError('Local AP receipt state is unreadable')
                prefix = f"{snapshot.seed_name}:{snapshot.team}:{snapshot.slot}"
                raw = json.dumps({"version": state.get("version"), "sessions": {
                    key: value for key, value in state["sessions"].items()
                    if key in {prefix, prefix + ":" + snapshot.slot_data["native_generation_fingerprint"]}
                }}).encode("utf-8")
        return raw

    def backup_local_files(self, snapshot, config):
        from .ledger_preservation import delivery_backup, room_key

        with self._lock:
            if self.game_processes(config) != ():
                raise RuntimeError('Close DOOM Eternal before copying saves.')
            inspection = self.inspect_campaign(snapshot, config)
            if inspection["status"] in {"inaccessible", "wrong_type", "metadata_invalid", "not_configured", "session_busy"}:
                raise RuntimeError('Inspect AP save details before copying local files: ' + inspection["status"])
            namespace = inspection["namespace_id"]
            directory = self.data_dir / "campaigns" / namespace
            sources = self._preservation_sources(snapshot, inspection, directory)
            if not sources:
                raise RuntimeError('No local files are available for this room')
            raw = self._room_receipt_backup(config, snapshot)
            archive = self.data_dir / "campaigns/local-copies" / secrets.token_hex(16) / "campaign.zip"
            with delivery_backup(config, room_key(snapshot), archive.parent.parent) as ledger_snapshot:
                _archive_campaign_files(archive, snapshot, namespace, list(dict.fromkeys(sources)), raw, ledger_snapshot)
            if self.game_processes(config) != ():
                raise RuntimeError('DOOM Eternal started during the copy; its completeness is unconfirmed')
            return archive

    def _preservation_sources(self, snapshot, inspection, directory):
        sources = [directory] if directory.is_dir() else []
        sources.extend(self.data_dir / "campaigns" / name for name in self.list_backups(snapshot))
        for name in ("steam_native", "local_native"):
            entry = inspection["paths"][name]
            if entry["status"] == "present":
                if inspection["native_root_source"] != "native.root":
                    raise RuntimeError('The native save provider is derived; its room association is unconfirmed')
                sources.append(Path(entry["path"]))
        return sources

    def _restart_campaign_state(self, config, snapshot, CLIENT_STATE_VERSION):
        root = Path(config["doom_base_dir"]).resolve()
        if (root.parent / "sentinel-prelaunch.txt").exists():
            raise RuntimeError('Another launcher owns an AP session; close it before restarting')
        remote = Path(config["steam_remote_dir"]).resolve()
        if remote.name.casefold() != "remote" or remote.parent.name != "782330" or remote.parent.parent.parent.name.casefold() != "userdata":
            raise RuntimeError('Select the Steam account save provider before restarting')
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
            raise RuntimeError('Local AP receipt state is too large to back up')
        state = json.loads(raw) if raw is not None else {"version": CLIENT_STATE_VERSION, "sessions": {}}
        return root, remote, local, namespace, prefix, state_key, state_file, raw, state

    def _restart_event_sources(self, local, keys, state_key, sources, root):
        event_key = None
        event_session = local / "ap_event_session.json"
        if event_session.is_file():
            event_key = json.loads(event_session.read_text(encoding="utf-8")).get("ap_state_key")
        if event_key is None or event_key in keys | {state_key}:
            for pattern in ("ap_event_*.txt", "ap_active_map*.txt", "ap_telemetry*.txt", "ap_condump*.txt", "ap_event_session.json"):
                sources.extend(local.glob(pattern))
            sources.extend(root.glob("ap_transition_*.evt"))
            from doom_eap.content.content_catalog import load_publisher_contracts
            sources.extend(local / trigger["filename"] for publisher in load_publisher_contracts()
                           for trigger in publisher.triggers_for("map_event_file"))

    def _restart_campaign_sources(self, namespace, remote, local, snapshot, root, keys, state_key, queue_session_namespace):
        native_name = _campaign_native_root(self.data_dir / "campaigns", namespace)
        sources = [remote / native_name, local / native_name,
                   self.data_dir / "campaigns" / namespace]
        sources.extend(self.data_dir / "campaigns" / name for name in self.list_backups(snapshot))
        queue = root / "ap_queue"
        for key in keys | {state_key}:
            sources.extend(queue.glob(f"recv-{queue_session_namespace(key)}-*"))
        self._restart_event_sources(local, keys, state_key, sources, root)
        sources = list(dict.fromkeys(path for path in sources if path.exists() or path.is_symlink()))
        return sources

    def _restart_campaign_files(self, ledger, room_key, snapshot, token, archive, sources, state, keys, state_key, default_session_state, store, finish_new_save, audit_new_save, raw, state_file):
        moved = []
        epoch_binding = None
        applied = False
        try:
            epoch_binding = asyncio.run(audit_new_save(ledger, room_key(snapshot), token, archive))
            for source in sources:
                target = source.with_name("." + source.name + "-restart-" + token)
                os.replace(source, target)
                moved.append((source, target))
            _restart_receipt_state(state, keys, state_key, ledger is not None, default_session_state)
            store.commit(state, reason="campaign_restart")
            finish_new_save(ledger, epoch_binding, token, applied=True)
            applied = True
        finally:
            if not applied:
                for source, target in reversed(moved):
                    os.replace(target, source)
                if raw is not None:
                    store.commit(json.loads(raw), reason="campaign_restart_rollback")
                elif state_file.exists():
                    state_file.unlink()
                finish_new_save(ledger, epoch_binding, token, applied=False)

    def restart_campaign(self, snapshot, config: dict) -> Path:
        from doom_eap.contracts.receipt_delivery import queue_session_namespace
        from doom_eap.runtime.inventory import (
            CLIENT_STATE_VERSION,
            ClientStateStore,
            default_session_state,
            migrate_client_state,
        )

        from .ledger_preservation import audit_new_save, delivery_backup, delivery_restart, finish_new_save, room_key

        with self._lock:
            processes = self.game_processes(config)
            if processes != ():
                raise RuntimeError('Exit DOOM Eternal before creating a new AP game save; its process must be confirmed closed')
            with delivery_restart(config, room_key(snapshot)) as ledger:
                self.retire(processes)
                root, remote, local, namespace, prefix, state_key, state_file, raw, state = self._restart_campaign_state(config, snapshot, CLIENT_STATE_VERSION)
                if not isinstance(state, dict) or not isinstance(state.get("sessions"), dict):
                    raise RuntimeError('Local AP receipt state is unreadable; restart refused')
                logger = logging.getLogger(__name__)
                store = ClientStateStore(state_file, version=CLIENT_STATE_VERSION, migrate=migrate_client_state,
                    logger=logger, log_event=lambda kind, **details: logger.info("%s %s", kind, details))
                keys = {key for key in state["sessions"] if key == prefix or key.startswith(prefix + ":")}
                sources = self._restart_campaign_sources(namespace, remote, local, snapshot, root, keys, state_key, queue_session_namespace)
                token = secrets.token_hex(16)
                backup = self.data_dir / "campaigns" / "restarts" / token
                archive = backup / "campaign.zip"
                with delivery_backup(config, room_key(snapshot), backup.parent) as ledger_snapshot:
                    _archive_campaign_files(archive, snapshot, namespace, sources, raw, ledger_snapshot)
                if self.game_processes(config) != ():
                    raise RuntimeError('DOOM Eternal started during backup; existing saves were kept')
                self._restart_campaign_files(ledger, room_key, snapshot, token, archive, sources, state, keys, state_key, default_session_state, store, finish_new_save, audit_new_save, raw, state_file)
                self.namespace = ""
                return archive

    def list_backups(self, snapshot) -> list[str]:
        namespace = namespace_id(snapshot.seed_name, snapshot.team, snapshot.slot,
                                 snapshot.slot_data["native_generation_fingerprint"])
        candidates=[]
        for path in (self.data_dir / "campaigns").glob("transport-backup-*"):
            manifest=path / "transport.manifest"
            if not path.is_symlink() and manifest.is_file() and manifest.stat().st_size <= 65536:
                if f"namespace={namespace}\n" in manifest.read_text(encoding="utf-8"):
                    candidates.append(path.name)
        return sorted(candidates,reverse=True)

    def _native_backup_result(self, run):
        reports=[json.loads(line) for line in run.stdout.splitlines() if line.strip()]
        request=next((value for value in reports if value.get("operation")=="native_backup_request"),{})
        result=reports[-1] if reports else {}
        return request, result

    def _confirm_native_backup(self, current, request, scope, namespace, run, result):
        if (not current.get("ready") or request.get("pid")!=scope["pid"]
                or request.get("process_created")!=str(scope["process_created"])
                or request.get("instance_id")!=scope["admission"].get("instance_id")
                or request.get("namespace")!=namespace or self.namespace!=namespace or run.returncode
                or result.get("state")!="complete"):
            raise RuntimeError('Native backup not confirmed; retained archives remain available. No retry was made.')

    def create_backup(self) -> dict:
        scope=self.observe()
        if os.name != "nt" or not scope.get("ready"):
            raise RuntimeError('Native AP backup requires a fresh qualified game session')
        namespace=self.namespace
        run=subprocess.run([str(self._runtime / "sentinel_probe.exe"),"--native-backup",
            "--pid",str(scope["pid"]),"--namespace",namespace,"--campaign","game","--slot","0","--json"],
            capture_output=True,timeout=18,creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        request, result = self._native_backup_result(run)
        current=self.observe()
        self._confirm_native_backup(current, request, scope, namespace, run, result)
        self.verify_backup(result["basename"],namespace=namespace)
        return result

    def _observed_game_process(self, processes):
        path_matches = False
        path_error = None
        if len(processes) == 1:
            try:
                path_matches = same_executable_path(processes[0]["path"], self._game_exe)
            except OSError as error:
                path_error = error
        if not path_matches:
            self.status.update(state="process_ambiguous", ready=False)
            self._capture_admission_failure(error=path_error or 'Game process count or executable does not match',
                                            processes=processes)
            return dict(self.status)

    def _admission_matches(self, admission, process):
        expected = {"state": "admitted", "namespace_id": self.namespace, "build_id": self._build,
                    "target_pid": process["pid"], "server_pid": process["pid"], "process_created": str(process["created"])}
        matches = all(admission.get(key) == value for key, value in expected.items()) and admission.get("accepting_requests") is True
        instance = admission.get("instance_id")
        matches = matches and instance is not None and (self._instance is None or instance == self._instance)
        if matches:
            self._instance = instance
        return matches

    def _observe_remote_session(self):
        self.status = self._remote.observe()
        if self._remote.process is not None and self._remote.closable and (
                self._remote.process.poll() is not None or self._remote.process.stdin.closed):
            self._remote.retire()
        return dict(self.status)

    def _unknown_game_process(self, processes):
        if processes is None:
            self.status.update(state="process_unknown", ready=False)
            self._capture_admission_failure(error='Game process identity query failed', processes=None)
            return dict(self.status)

    def _observe_admission(self, processes):
        process = processes[0]
        identity = process["pid"], process["created"]
        if self._process is not None and identity != self._process:
            self.status.update(state="process_changed", ready=False)
            self._capture_admission_failure(error='The prepared game process identity changed', processes=processes)
            return dict(self.status)
        self._process = identity
        self.status.update(pid=process["pid"], process_created=process["created"])
        try:
            admission = self._probe("--pid", str(process["pid"]), "--save-admission", "--json")
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            self.status.update(state="awaiting_admission", ready=False)
            self._capture_admission_failure(process, error=error)
            return dict(self.status)
        matches = self._admission_matches(admission, process)
        self.status.update(state="admitted" if matches else "admission_refused", ready=matches,
                           pid=process["pid"], process_created=process["created"], admission=admission)
        self._observe_checkpoint(matches)
        if not matches:
            self._capture_admission_failure(process, admission)
        return dict(self.status)

    def observe(self, processes=_UNSET) -> dict:
        with self._lock:
            if self._remote is not None:
                return self._observe_remote_session()
            if not self._handle:
                return dict(self.status)
            processes = windows_game_processes() if processes is _UNSET else processes
            if processes is None:
                return self._unknown_game_process(processes)
            if not processes:
                if self._process is not None:
                    self.retire(processes)
                    self.status.update(state="game_exited", ready=False)
                return dict(self.status)
            observed = self._observed_game_process(processes)
            if observed is not None:
                return observed
            return self._observe_admission(processes)

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

    def retire(self, processes=_UNSET) -> bool:
        from .launcher_core_install import game_write_lock
        while True:
            with self._lock:
                control = self._control
                if control is None:
                    return self._retire(processes)
            with game_write_lock(control.parent), self._lock:
                if self._control == control:
                    return self._retire(processes)

    def _retire(self, processes=_UNSET) -> bool:
        with self._lock:
            if self._remote is not None:
                stopped = self._remote.retire()
                self.status = {"state": "not_prepared", "ready": False}
                return stopped
            if not self._handle:
                return False
            processes = windows_game_processes() if processes is _UNSET else processes
            if processes != ():
                raise PlayerError('launcher.ui', 'close_doom_eternal_before_closing_this_launcher')
            self._api.CloseHandle(self._handle)
            self._handle = None
            if self._control.exists() and self._control.read_bytes() == self._control_bytes:
                self._control.unlink()
            self._control = None
            self._process = None
            self._instance = None
            self.status = {"state": "not_prepared", "ready": False}
            return True
