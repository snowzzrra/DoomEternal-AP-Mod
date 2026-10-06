"""Room-bound prelaunch admission with one live, single-use Windows lease."""
from __future__ import annotations

import ctypes
from itertools import chain
from dataclasses import asdict
from ctypes import wintypes
import hashlib
import importlib.util
import json
import logging
import os
import re
from pathlib import Path
import secrets
import selectors
import subprocess
import sys
import threading
import stat
import zipfile
from types import SimpleNamespace

from doom_eap.contracts.core_distribution import verify_runtime
from doom_eap.runtime.observer_lifecycle import windows_game_processes
from doom_eap.runtime.weapon_points import namespace_id

_UNSET = object()
_INSTALL_KEYS = ("game_root", "doom_base_dir", "steam_remote_dir", "save_games_dir", "core_runtime_manifest", "selected_core_runtime_manifest", "client_state_file", "proton_compat_data_dir", "proton_executable")


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
    match = re.match(rb"sentinel-run-v1\nrun=[0-9a-f]{32}\nprotection=[0-9a-f]{64}\nowner=([1-9][0-9]*)\ncreated=([1-9][0-9]*)\nsentinel-test-session-v2\n", raw)
    recognized = match and len(raw) <= 65536 and int(match[1]) <= 0xffffffff and int(match[2]) <= 0xffffffffffffffff
    state = prelaunch_owner_state(int(match[1]), int(match[2])) if recognized else "legacy"
    if state == "alive":
        raise RuntimeError("Another live launcher owns this game preparation; close that launcher before retrying")
    if state == "unknown" or (state == "legacy" and not explicit):
        raise RuntimeError("Prelaunch ownership is unknown. Use Repair Session with DOOM closed to inspect or archive this marker")
    if state == "legacy":
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenSemaphoreW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        api.OpenSemaphoreW.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenSemaphoreW(0x100000, False, "Local\\SentinelA-" + hashlib.sha256(raw).hexdigest())
        if handle:
            api.CloseHandle(handle)
            raise RuntimeError("A live prelaunch lease exists; its marker was preserved")
        if ctypes.get_last_error() != 2:
            raise RuntimeError("Prelaunch lease ownership cannot be inspected; marker preserved")
    directory = state_dir / "prelaunch-backups"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (hashlib.sha256(raw).hexdigest() + "-" + secrets.token_hex(8) + ".txt")
    if path.read_bytes() != raw:
        raise RuntimeError("Prelaunch marker changed during recovery; retry inspection")
    os.replace(path, target)
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
        raise RuntimeError("AP campaign provider metadata is unreadable")
    return text[len(header):-1].decode("ascii")


class _ProtonSessionOwner:
    def __init__(self, client_dir, data_dir, state_dir):
        self.client_dir, self.data_dir, self.state_dir = client_dir, data_dir, state_dir
        self.process = None
        self.closable = False
        self._config_identity = None
        self.status = {"state": "not_prepared", "ready": False}

    def request(self, action, *, timeout=10, **payload):
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("The Windows AP session supervisor is unavailable")
        self.process.stdin.write(json.dumps({"action": action, **payload}) + "\n")
        self.process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout):
                self.closable = False
                raise RuntimeError("The Windows AP session supervisor did not respond")
        raw = self.process.stdout.readline(1024 * 1024 + 1)
        response = json.loads(raw)
        if not isinstance(response, dict) or type(response.get("can_close")) is not bool:
            raise RuntimeError("Invalid Windows session supervisor response")
        self.closable = response["can_close"]
        if "error" in response:
            error = RuntimeError(response["error"])
            for key, value in response.get("failure", {}).items():
                if key in {"failure_domain", "stage", "operation", "source_path", "destination_path", "errno", "winerror", "filename", "filename2"}:
                    setattr(error, key, value)
            raise error
        if "status" in response:
            if not isinstance(response["status"], dict):
                raise RuntimeError("Invalid Windows session status")
            self.status = response["status"]
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
            raise RuntimeError("The packaged Windows AP session supervisor is unavailable")
        command, environment = runtime(config)
        converted = {key: config[key] for key in _INSTALL_KEYS if key in config}
        for key in ("game_root", "doom_base_dir", "steam_remote_dir", "save_games_dir", "core_runtime_manifest", "selected_core_runtime_manifest", "client_state_file"):
            if converted.get(key):
                converted[key] = windows_path(converted[key])
        if not converted.get("game_root"):
            converted["game_root"] = windows_path(Path(config["doom_base_dir"]).parent)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "session_supervisor.log").open("ab") as log:
            self.process = subprocess.Popen([*command, windows_path(executable)], env=environment,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                            text=True, encoding="utf-8", bufsize=1)
        try:
            self._config_identity = config_identity
            return self.request("prepare", timeout=300, room=asdict(snapshot), config=converted,
                                recover_prelaunch=recover_prelaunch,
                                client_dir=windows_path(self.client_dir), data_dir=windows_path(self.data_dir),
                                state_dir=windows_path(self.state_dir))
        except (OSError, ValueError, RuntimeError):
            self.process.stdin.close()
            self.closable = False
            raise

    def observe(self):
        if self.process is not None and (self.process.poll() is not None or self.process.stdin.closed):
            from doom_eap.runtime.proton import windows_processes
            configuration = dict(zip(_INSTALL_KEYS, self._config_identity))
            processes = windows_processes(configuration, self.client_dir / "APSessionOwner.exe")
            self.closable = processes == ()
            self.status.update(state="game_exited" if self.closable else "supervisor_unavailable", ready=False)
            return self.status
        try:
            return self.request("observe")
        except (OSError, ValueError, RuntimeError):
            self.closable = False
            self.status.update(state="supervisor_unavailable", ready=False)
            return self.status

    def retire(self):
        if self.process is None:
            return
        if self.process.poll() is None and not self.process.stdin.closed:
            self.request("retire")
            self.process.stdin.close()
        elif not self.closable:
            raise RuntimeError("Confirm DOOM Eternal has exited before retiring its session supervisor")
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
                                timeout=6, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            value = json.loads(result.stdout)
        except (ValueError, UnicodeError) as error:
            raise RuntimeError("Core session probe did not return a valid response") from error
        if not isinstance(value, dict):
            raise RuntimeError("Core session probe returned an invalid object")
        if result.returncode and "--save-admission" not in arguments:
            outcome = value.get("outcome", value.get("result", "unknown"))
            raise RuntimeError(f"Core session refused: {outcome} (error {value.get('win32_error', 0)})")
        return value

    def prepare(self, snapshot, config: dict, *, recovery_basename=None, recover_prelaunch=False) -> dict:
        try:
            if os.name != "nt" or not (config.get("game_root") or config.get("doom_base_dir")):
                return self._prepare(snapshot, config, recovery_basename=recovery_basename, recover_prelaunch=recover_prelaunch)
            from .launcher_core_install import game_write_lock
            root = Path(str(config.get("game_root") or config["doom_base_dir"])).resolve()
            if root.name.casefold() == "base":
                root = root.parent
            with self._lock, game_write_lock(root):
                return self._prepare(snapshot, config, recovery_basename=recovery_basename, recover_prelaunch=recover_prelaunch)
        except (RuntimeError, OSError, ValueError) as error:
            from .launcher_integration import setup_failure_payload
            error.failure_domain = "campaign_session"
            diagnostic = setup_failure_payload(error, phase="campaign_session")
            import traceback
            diagnostic["traceback"] = traceback.format_exc()
            from datetime import datetime, timezone
            diagnostic["at_utc"] = datetime.now(timezone.utc).isoformat()
            diagnostic["metadata"] = getattr(error.__cause__, "private_metadata", None)
            try:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                (self.state_dir / "session_prepare_failure.json").write_text(
                    json.dumps(diagnostic, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
            except OSError:
                logging.getLogger(__name__).exception("AP_SESSION_DIAGNOSTIC_WRITE_FAILED")
            logging.getLogger(__name__).exception("AP_SESSION_PREPARATION_REFUSED %s", diagnostic)
            raise

    def _prepare(self, snapshot, config: dict, *, recovery_basename=None, recover_prelaunch=False) -> dict:
        with self._lock:
            config_identity = tuple(config.get(key) for key in _INSTALL_KEYS)
            namespace = namespace_id(snapshot.seed_name, snapshot.team, snapshot.slot,
                                     snapshot.slot_data["native_generation_fingerprint"])
            if os.name != "nt":
                if recovery_basename is not None:
                    raise RuntimeError("Recovery requires a qualified native helper in this environment")
                if self._remote is None:
                    self._remote = _ProtonSessionOwner(self.client_dir, self.data_dir, self.state_dir)
                self.status = self._remote.prepare(snapshot, config, recover_prelaunch=recover_prelaunch)
                self.namespace = namespace
                return dict(self.status)
            processes = windows_game_processes()
            if processes is None:
                raise RuntimeError("Game process identity is unknown; AP session preparation refused")
            if processes:
                if recovery_basename is not None:
                    raise RuntimeError("Exit DOOM Eternal before restoring an AP backup")
                if self._handle and namespace == self.namespace and self._config_identity == config_identity:
                    return self.observe(processes)
                raise RuntimeError("Exit DOOM Eternal before preparing this AP session")
            if recovery_basename is None and self._handle and namespace == self.namespace and self._config_identity == config_identity and self.status["state"] == "prelaunch_ready":
                return dict(self.status)
            self.retire(processes)
            if any(char in snapshot.seed_name for char in "\r\n\0"):
                raise ValueError("Room seed contains unsupported control characters")
            root = Path(str(config.get("game_root") or config["doom_base_dir"])).resolve()
            if root.name.casefold() == "base":
                root = root.parent
            self._game_exe = root / "DOOMEternalx64vk.exe"
            runtime_manifest = config.get("core_runtime_manifest") or config.get("selected_core_runtime_manifest")
            if not runtime_manifest:
                raise RuntimeError("Select or download a compatible Core runtime before preparing this session")
            self._runtime = Path(str(runtime_manifest)).resolve()
            if not self._runtime.is_dir():
                self._runtime = self._runtime.parent
            manifest, contents = verify_runtime(self._runtime / "distribution.json", bootstrap_from_archive=True)
            helper = self._runtime / "prepare_vanilla_backup.py"
            if "prepare_vanilla_backup.py" not in contents or helper.read_bytes() != contents["prepare_vanilla_backup.py"]:
                raise RuntimeError("Runtime package lacks its verified vanilla-protection helper")
            self._build = manifest["build_id"]
            for name in ("sentinel_core.dll", "msimg32.dll"):
                if not (root / name).is_file() or (root / name).read_bytes() != contents[name]:
                    raise RuntimeError("Installed game integration differs from the qualified runtime; prepare setup again")
            archive_prelaunch(root / "sentinel-prelaunch.txt", self.state_dir, explicit=recover_prelaunch)
            remote = Path(str(config.get("steam_remote_dir", ""))).resolve()
            if remote.name.casefold() != "remote" or remote.parent.name != "782330" or remote.parent.parent.parent.name.casefold() != "userdata":
                raise RuntimeError("Select the Steam account save provider before preparing the AP session")
            account = remote.parent.parent.name
            if not account.isdecimal() or not 0 < int(account) <= 0xffffffff:
                raise RuntimeError("Steam account identity is invalid")
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
                             "provenance=synthetic-fixture\n").encode("utf-8")
                if all(not path.exists() or (path.is_dir() and all(
                        file.name == marker and file.is_file() and file.read_bytes() == ownership
                        for file in path.iterdir())) for path in native_dirs):
                    native_exists = False
            if contract.exists() != checkpoint.exists() or (recovery_basename is None and native_exists != contract.exists()):
                raise RuntimeError("AP campaign metadata and native saves disagree; inspect or recover the campaign before playing")
            intent = "resume" if native_exists else "create"
            if recovery_basename is not None:
                if not re.fullmatch(r"[a-z0-9-]{1,128}", str(recovery_basename)):
                    raise ValueError("Invalid AP backup basename")
                intent = "recover"
            difficulty = snapshot.slot_data["campaign_plan"].get("difficulty")
            if type(difficulty) is not int or not 0 <= difficulty <= 3:
                raise ValueError("Room campaign difficulty is unsupported")
            descriptor = (f"sentinel-test-session-v2\nseed_hex={snapshot.seed_name.encode('utf-8').hex()}\nteam={snapshot.team}\nslot={snapshot.slot}\n"
                          f"generation_fingerprint={snapshot.slot_data['native_generation_fingerprint']}\n"
                          f"provenance=synthetic-fixture\nroot={campaign_root}\ncampaign=unified\nstarting_stage=hub\n"
                          f"difficulty={difficulty}\nintent={intent}\n")
            if intent in {"resume", "recover"}:
                expected = (f"sentinel-campaign-v2\nnamespace={namespace}\ngeneration={snapshot.slot_data['native_generation_fingerprint']}\n"
                            f"provenance=synthetic-fixture\ncampaign=unified\nstarting_stage=hub\ndifficulty={difficulty}\nslot=AUTOSAVE0\n")
                if contract.read_bytes() != expected.encode("utf-8"):
                    raise RuntimeError("AP campaign identity or immutable options differ from this room")
                if intent == "resume" and not any((path / "GAME-AUTOSAVE0").is_dir() for path in native_dirs):
                    raise RuntimeError("The AP native campaign is missing; use compatible recovery")
            self.state_dir.mkdir(parents=True, exist_ok=True)
            descriptor_path = self.state_dir / "ap-session-descriptor.txt"
            if intent == "recover":
                descriptor += f"backup={recovery_basename}\n"
            descriptor_path.write_text(descriptor, encoding="utf-8", newline="\n")
            mode = "--save-session-prepare" if intent == "create" or not namespace_dir.exists() else "--save-session-reopen"
            plan = self._probe(mode, str(descriptor_path))
            if plan.get("namespace_id") != namespace:
                raise RuntimeError("Core storage returned a different room namespace")
            if plan.get("native_root") != _campaign_native_root(campaign_root, namespace):
                raise RuntimeError("Core storage returned a different campaign provider")
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
                    run_backup_parent=None))
            except (module.Refused, OSError, ValueError) as error:
                refused = RuntimeError(f"Vanilla protection could not be verified; AP activation refused: {error}")
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
                raise RuntimeError("A conflicting native session environment is present")
            text = (f"sentinel-run-v1\nrun={secrets.token_hex(16)}\nprotection={protection['reference_manifest_sha256']}\n"
                    f"owner={os.getpid()}\ncreated={created}\n" + descriptor).encode("utf-8")
            control_hash = hashlib.sha256(text).hexdigest()
            handle = api.CreateSemaphoreW(None, 1, 1, "Local\\SentinelA-" + control_hash)
            if not handle or ctypes.get_last_error() == 183:
                if handle:
                    api.CloseHandle(handle)
                raise RuntimeError("Could not acquire the one-use AP prelaunch lease")
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
                           "backup_directory": str(backup)}
            return dict(self.status)

    def verify_backup(self, basename: str, *, namespace=None) -> dict:
        if not re.fullmatch(r"[a-z0-9-]{1,128}", basename):
            raise ValueError("Invalid AP backup basename")
        result = self._probe("--save-session-verify-backup", str(self.state_dir / "ap-session-descriptor.txt"), basename)
        if (result.get("archive_verified") is not True or result.get("owner_bound") is not True
                or result.get("quarantine") is not False or result.get("namespace") != (namespace or self.namespace)):
            raise RuntimeError("Backup lacks compatible AP identity and native owner proof")
        return result

    def game_processes(self, config: dict):
        if os.name == "nt":
            return windows_game_processes()
        from doom_eap.runtime.proton import windows_processes
        return windows_processes(config, self.client_dir / "APSessionOwner.exe")

    def restart_campaign(self, snapshot, config: dict) -> Path:
        from doom_eap.contracts.command_publication import queue_session_namespace
        from doom_eap.runtime.client_state_store import ClientStateStore
        from doom_eap.runtime.item_reconciliation import CLIENT_STATE_VERSION, default_session_state, migrate_client_state

        with self._lock:
            processes = self.game_processes(config)
            if processes != ():
                raise RuntimeError("Exit DOOM Eternal before creating a new AP game save; its process must be confirmed closed")
            self.retire(processes)
            root = Path(config["doom_base_dir"]).resolve()
            if (root.parent / "sentinel-prelaunch.txt").exists():
                raise RuntimeError("Another launcher owns an AP session; close it before restarting")
            remote = Path(config["steam_remote_dir"]).resolve()
            if remote.name.casefold() != "remote" or remote.parent.name != "782330" or remote.parent.parent.parent.name.casefold() != "userdata":
                raise RuntimeError("Select the Steam account save provider before restarting")
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
                raise RuntimeError("Local AP receipt state is too large to back up")
            state = json.loads(raw) if raw is not None else {"version": CLIENT_STATE_VERSION, "sessions": {}}
            if not isinstance(state, dict) or not isinstance(state.get("sessions"), dict):
                raise RuntimeError("Local AP receipt state is unreadable; restart refused")
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
            files = {}
            for index, source in enumerate(sources):
                for path in chain((source,), source.rglob("*") if source.is_dir() else ()):
                    info = path.lstat()
                    if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                        raise RuntimeError("AP restart refuses linked or special save files")
                    if path.is_file():
                        name = f"{index}/{path.relative_to(source).as_posix() if source.is_dir() else source.name}"
                        files[name] = (path, hashlib.sha256(path.read_bytes()).hexdigest())
            token = secrets.token_hex(16)
            backup = self.data_dir / "campaigns" / "restarts" / token
            backup.mkdir(parents=True, exist_ok=False)
            archive = backup / "campaign.zip"
            with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as output:
                for name, (path, digest) in files.items():
                    output.write(path, name)
                if raw is not None:
                    output.writestr("client_state.json", raw)
                output.writestr("room.json", json.dumps({"seed": snapshot.seed_name, "team": snapshot.team,
                    "slot": snapshot.slot, "namespace": namespace, "sources": [str(path) for path in sources],
                    "sha256": {name: digest for name, (_, digest) in files.items()}}))
            with archive.open("r+b") as stream:
                os.fsync(stream.fileno())
            with zipfile.ZipFile(archive) as saved:
                if saved.testzip() is not None or any(hashlib.sha256(saved.read(name)).hexdigest() != digest for name, (_, digest) in files.items()):
                    raise RuntimeError("AP backup verification failed; existing saves were kept")
                if raw is not None and saved.read("client_state.json") != raw:
                    raise RuntimeError("AP receipt backup verification failed; existing saves were kept")
            if self.game_processes(config) != ():
                raise RuntimeError("DOOM Eternal started during backup; existing saves were kept")
            moved = []
            try:
                for source in sources:
                    target = source.with_name("." + source.name + "-restart-" + token)
                    os.replace(source, target)
                    moved.append((source, target))
                for key in keys:
                    state["sessions"].pop(key)
                state["sessions"][state_key] = default_session_state()
                store.commit(state, reason="campaign_restart")
            except BaseException:
                for source, target in reversed(moved):
                    os.replace(target, source)
                if raw is not None:
                    store.commit(json.loads(raw), reason="campaign_restart_rollback")
                elif state_file.exists():
                    state_file.unlink()
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
            raise RuntimeError("Native AP backup requires a fresh qualified game session")
        namespace=self.namespace
        run=subprocess.run([str(self._runtime / "sentinel_probe.exe"),"--native-backup",
            "--pid",str(scope["pid"]),"--namespace",namespace,"--campaign","game","--slot","0","--json"],
            capture_output=True,timeout=18,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        reports=[json.loads(line) for line in run.stdout.splitlines() if line.strip()]
        request=next((value for value in reports if value.get("operation")=="native_backup_request"),{})
        result=reports[-1] if reports else {}
        current=self.observe()
        if (not current.get("ready") or request.get("pid")!=scope["pid"]
                or request.get("process_created")!=str(scope["process_created"])
                or request.get("instance_id")!=scope["admission"].get("instance_id")
                or request.get("namespace")!=namespace or self.namespace!=namespace or run.returncode
                or result.get("state")!="complete"):
            raise RuntimeError("Native backup not confirmed; retained archives remain available. No retry was made.")
        self.verify_backup(result["basename"],namespace=namespace)
        return result

    def observe(self, processes=_UNSET) -> dict:
        with self._lock:
            if self._remote is not None:
                self.status = self._remote.observe()
                return dict(self.status)
            if not self._handle:
                return dict(self.status)
            processes = windows_game_processes() if processes is _UNSET else processes
            if processes is None:
                self.status.update(state="process_unknown", ready=False)
                return dict(self.status)
            if not processes:
                if self._process is not None:
                    self.retire(processes)
                    self.status.update(state="game_exited", ready=False)
                return dict(self.status)
            if len(processes) != 1 or os.path.normcase(str(processes[0]["path"])) != os.path.normcase(str(self._game_exe)):
                self.status.update(state="process_ambiguous", ready=False)
                return dict(self.status)
            process = processes[0]
            identity = process["pid"], process["created"]
            if self._process is not None and identity != self._process:
                self.status.update(state="process_changed", ready=False)
                return dict(self.status)
            self._process = identity
            self.status.update(pid=process["pid"], process_created=process["created"])
            try:
                admission = self._probe("--pid", str(process["pid"]), "--save-admission", "--json")
            except (RuntimeError, OSError, subprocess.SubprocessError):
                self.status.update(state="awaiting_admission", ready=False)
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
            return dict(self.status)

    def can_close(self) -> bool:
        if self._remote is not None:
            return self._remote.process is None or self._remote.closable
        if not self._handle:
            return True
        processes = windows_game_processes()
        return processes == ()

    def retire(self, processes=_UNSET) -> None:
        from .launcher_core_install import game_write_lock
        with self._lock:
            if self._control is not None:
                with game_write_lock(self._control.parent):
                    return self._retire(processes)
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
                raise RuntimeError("The AP session owner must remain open until the game has exited")
            self._api.CloseHandle(self._handle)
            self._handle = None
            if self._control.exists() and self._control.read_bytes() == self._control_bytes:
                self._control.unlink()
            self._control = None
            self._process = None
            self._instance = None
            self.status = {"state": "not_prepared", "ready": False}
