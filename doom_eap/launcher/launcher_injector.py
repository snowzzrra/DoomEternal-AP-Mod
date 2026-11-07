from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Protocol

from doom_eap.launcher.launcher_platform import (
    REQUIRED_DLL_OVERRIDE,
    _acquire_windows_mutex,
    _atomic_write_bytes,
    _dependency_mutex_name,
    _release_windows_mutex,
    detect_doom_processes,
    publish_file,
    require_doom_closed,
    validate_game_root,
)
from doom_eap.launcher.ui.strings import text as player_text


def restore_standard_streams(*, require_pipes=False):
    if sys.platform != 'win32':
        return
    import ctypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetStdHandle.argtypes = [ctypes.c_ulong]
    kernel.GetStdHandle.restype = ctypes.c_void_p
    # windowed builds keep inherited pipes but leave Python's streams unset
    for name, number, mode, flags in (
        ('stdin', -10, 'r', os.O_RDONLY), ('stdout', -11, 'w', os.O_WRONLY), ('stderr', -12, 'w', os.O_WRONLY),
    ):
        if getattr(sys, name) is not None:
            continue
        handle = kernel.GetStdHandle(number & 0xffffffff)
        if not handle or handle == ctypes.c_void_p(-1).value:
            if require_pipes:
                raise RuntimeError('The session supervisor requires an inherited {value_0} pipe'.format(value_0=f'{name}'))
            stream = open(os.devnull, mode, encoding='utf-8')
        else:
            descriptor = msvcrt.open_osfhandle(handle, flags | os.O_BINARY)
            stream = os.fdopen(descriptor, mode, buffering=1, encoding='utf-8')
        setattr(sys, name, stream)


@lru_cache(maxsize=32)
def _sha256_file_identity(path: str, size: int, modified_ns: int, created_ns: int) -> str:
    """Hash immutable file identity once; metadata changes invalidate the entry."""
    del size, modified_ns, created_ns
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def create_secure_ssl_context(cafile: str | None = None) -> ssl.SSLContext:
    """Create a secure default SSL context using certifi CA bundle or supplied cafile.

    Certificate and hostname verification are strictly enforced (CERT_REQUIRED).
    """
    import certifi

    ca_bundle = cafile or certifi.where()
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca_bundle)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    return context


class DownloadTransport(Protocol):
    def fetch(self, url: str, destination: Path) -> DownloadResult | None: ...


@dataclass(frozen=True)
class DownloadResult:
    final_url: str
    content_length: int | None
    headers: dict[str, str] | None = None


def is_transient_download_error(exc: Exception) -> bool:
    """Classify transient transport errors safe for bounded retry."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {429, 500, 502, 503, 504}
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, ssl.SSLError):
            return False
        if isinstance(reason, socket.gaierror | socket.timeout | TimeoutError | ConnectionResetError | ConnectionRefusedError | OSError):
            return True
        return False
    if isinstance(exc, socket.timeout | TimeoutError | ConnectionResetError | http.client.RemoteDisconnected):
        return True
    return False


class UrlDownloadTransport:
    """Secure HTTPS download transport with validated SSL context, bounded timeout, and transient retry."""

    def __init__(
        self,
        ssl_context: ssl.SSLContext | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
        backoff_base: float = 0.5,
    ):
        self.ssl_context = ssl_context if ssl_context is not None else create_secure_ssl_context()
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.backoff_base = max(0.0, backoff_base)

    def _fetch_once(self, url, destination, headers):
        request = urllib.request.Request(url, headers={'User-Agent': 'DoomEternal-AP-Launcher', **(headers or {})})
        with urllib.request.urlopen(request, timeout=self.timeout, context=self.ssl_context) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output)
            headers = getattr(response, "headers", None)
            raw_length = headers.get("Content-Length") if headers is not None and hasattr(headers, "get") else None
            try:
                content_length = int(raw_length) if raw_length is not None else None
            except ValueError:
                content_length = None
            final_url = response.geturl() if hasattr(response, "geturl") else url
            return DownloadResult(final_url, content_length, dict(response.headers))

    def fetch(self, url: str, destination: Path, *, headers: dict[str, str] | None = None) -> DownloadResult:
        total_attempts = self.max_retries + 1
        for attempt in range(1, total_attempts + 1):
            try:
                return self._fetch_once(url, destination, headers)
            except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
                if destination.exists():
                    try:
                        destination.unlink()
                    except OSError:
                        pass
                if attempt < total_attempts and is_transient_download_error(error):
                    time.sleep(self.backoff_base * (2 ** (attempt - 1)))
                    continue
                raise
        raise RuntimeError('download attempts exhausted')


class PrerequisiteStatus(str, Enum):
    OK = "ok"
    MISSING = "missing"
    INVALID = "invalid"
    INCOMPATIBLE = "incompatible"
    NEEDS_USER_ACTION = "needs_user_action"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PrerequisiteCheck:
    key: str
    status: PrerequisiteStatus
    message: str
    details: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == PrerequisiteStatus.OK


@dataclass(frozen=True)
class RuntimePrerequisiteReport:
    checks: tuple[PrerequisiteCheck, ...]

    @property
    def ok(self) -> bool:
        """Require the game, verified Core pair, and packaged client runtime."""
        mandatory_keys = {"game", "meathook", "client_runtime"}
        return all(check.ok for check in self.checks if check.key in mandatory_keys)

    def get(self, key: str) -> PrerequisiteCheck | None:
        for check in self.checks:
            if check.key == key:
                return check
        return None

    @property
    def meathook(self) -> PrerequisiteCheck | None:
        return self.get("meathook")

    @property
    def game(self) -> PrerequisiteCheck | None:
        return self.get("game")


def probe_meathook(game_root: Path | None) -> PrerequisiteCheck:
    """Verify the Core runtime pair used by Game Link."""
    from ..contracts.core_distribution import read_manifest, verify_build_identity
    if game_root is None:
        return PrerequisiteCheck("meathook", PrerequisiteStatus.MISSING, 'Choose the game folder.')
    try:
        root = validate_game_root(game_root)
        value = read_manifest(root / "sentinel-distribution.json")
        records = {item["path"]: item for item in value["artifacts"]}
        for name in ("sentinel_core.dll", "msimg32.dll"):
            data = (root / name).read_bytes()
            if len(data) != records[name]["size"] or hashlib.sha256(data).hexdigest() != records[name]["sha256"]:
                raise ValueError('Game connection file {value_0} is damaged. Open Help and select REPAIR GAME CONNECTION.'.format(value_0=f'{name}'))
            verify_build_identity(name, data, value["build_id"])
        if (root / "XINPUT1_3.dll").exists():
            raise ValueError('XINPUT1_3.dll conflicts with DoomEAP. Uninstall the mod that installed it, then retry. If you do not know which mod installed it, select Help -> SAVE SUPPORT REPORT.')
        return PrerequisiteCheck("meathook", PrerequisiteStatus.OK, 'Game connection files checked.',
                                 {"path": str(root / "sentinel_core.dll"), "sha256": records["sentinel_core.dll"]["sha256"],
                                  "version": value["version"], "build_id": value["build_id"]})
    except (OSError, ValueError, KeyError, TypeError):
        return PrerequisiteCheck("meathook", PrerequisiteStatus.MISSING, 'Game connection files are unavailable. Open Help and select Fix Setup.')


def probe_runtime_prerequisites(
    game_root: Path | None,
    client_dir: Path | None = None,
    config: Mapping[str, object] | None = None,
) -> RuntimePrerequisiteReport:
    """Probe the game, Core and client prerequisites."""
    checks: list[PrerequisiteCheck] = []

    if game_root is None:
        checks.append(PrerequisiteCheck(
            key='game', status=PrerequisiteStatus.MISSING, message='DOOM Eternal installation is not configured',
            details={},
        ))
    else:
        try:
            validated_root = validate_game_root(game_root)
            checks.append(PrerequisiteCheck(
                key='game', status=PrerequisiteStatus.OK, message='DOOM Eternal installation validated',
                details={'path': str(validated_root)},
            ))
        except ValueError as error:
            checks.append(PrerequisiteCheck(
                key='game', status=PrerequisiteStatus.INVALID, message=str(error), details={'path': str(game_root)},
            ))

    checks.append(probe_meathook(game_root))

    if client_dir is not None:
        packaged_bridge = client_dir / "bridge_client.py"
        if packaged_bridge.is_file() or getattr(sys, "frozen", False):
            checks.append(PrerequisiteCheck(
                key='client_runtime', status=PrerequisiteStatus.OK, message='Bundled client runtime validated',
                details={'path': str(client_dir)},
            ))
        else:
            checks.append(PrerequisiteCheck(
                key='client_runtime', status=PrerequisiteStatus.MISSING,
                message='Client runtime bridge is missing: {value_0}'.format(value_0=f'{packaged_bridge}'),
                details={'expected_path': str(packaged_bridge)},
            ))

    if os.name != "nt" and config is not None:
        launch_opts = str(config.get("steam_launch_options") or "")
        if REQUIRED_DLL_OVERRIDE in launch_opts:
            checks.append(PrerequisiteCheck(
                key='linux_steam_override', status=PrerequisiteStatus.OK,
                message='Steam launch option configured with msimg32 override', details={'configured': True},
            ))
        else:
            checks.append(PrerequisiteCheck(
                key="linux_steam_override",
                status=PrerequisiteStatus.NEEDS_USER_ACTION,
                message='Set Steam launch option: WINEDLLOVERRIDES="{value_0}" %command%'.format(value_0=f'{REQUIRED_DLL_OVERRIDE}'),
                details={"required_override": REQUIRED_DLL_OVERRIDE, "configured": False},
            ))

    return RuntimePrerequisiteReport(tuple(checks))


@dataclass(frozen=True)
class DependencySpec:
    name: str
    version: str
    url: str
    sha256: str
    executable_glob: str
    archive_type: str
    commit: str = ""
    platforms: frozenset[str] = frozenset()
    expected_size: int | None = None


WINDOWS_INJECTOR_REQUIRED_MEMBERS: tuple[str, ...] = (
    'EternalModInjector.bat', 'EternalModManager.exe', 'base/BlangParser.dll', 'base/DEternal_loadMods.exe',
    'base/DEternal_patchManifest.exe', 'base/EternalPatcher.def', 'base/EternalPatcher.exe',
    'base/EternalPatcher.exe.config', 'base/Newtonsoft.Json.dll', 'base/idRehash.exe', 'base/opusdec.exe',
    'base/opusenc.exe', 'base/rs_data', 'base/zlib64.dll',
)

WINDOWS_MOD_INJECTOR = DependencySpec(
    name='EternalModInjector', version='2026-09-17', url='https://gamebanana.com/dl/1819101',
    sha256='567fddbae0c6298b9cfecf6ac7e2b129ec109c53f1b09691aea1fb75f08a0f41',
    executable_glob='**/EternalModInjector.bat', archive_type='zip', expected_size=5182727,
)

WINDOWS_MOD_MANAGER = WINDOWS_MOD_INJECTOR

LINUX_MOD_INJECTOR = DependencySpec(
    name='EternalModInjectorShell', version='6.66-rev3.16',
    url='https://github.com/leveste/EternalBasher/releases/download/v6.66-rev3.16/EternalModInjectorShell.tar.gz',
    sha256='f891336c7481d5be4fe51e20a4ca7f40a6b9d3b9ef00948859b4b3c28523a9bf',
    executable_glob='**/EternalModInjectorShell.sh', archive_type='tar.gz', expected_size=16754097,
)

IDFILE_DECOMPRESSOR_LINUX = DependencySpec(
    name='idFileDeCompressor', version='v1.0.1',
    url='https://github.com/brunoanc/idFileDeCompressorGo/releases/download/v1.0.1/idFileDeCompressor',
    sha256='8bd14670fdb5a47533c5a59e9b6e1e32eaa3f06ce8b7ad2b4a8df520348b9ab8', executable_glob='idFileDeCompressor',
    archive_type='file', commit='67cd82781edd0595b0513707907ab63cdaa2691a', platforms=frozenset({'linux'}),
    expected_size=946404,
)

IDFILE_DECOMPRESSOR_WINDOWS = DependencySpec(
    name='idFileDeCompressor', version='v1.0.1',
    url='https://github.com/brunoanc/idFileDeCompressorGo/releases/download/v1.0.1/idFileDeCompressor.exe',
    sha256='02488dfb2999542cee4b7c0713ec03dd534ddd876169aac43b0217b362427a06', executable_glob='idFileDeCompressor.exe',
    archive_type='file', commit='67cd82781edd0595b0513707907ab63cdaa2691a', platforms=frozenset({'windows'}),
    expected_size=877056,
)


def idfile_decompressor_spec(platform_name: str | None = None) -> DependencySpec | None:
    selected = (platform_name or ('windows' if os.name == 'nt' else 'linux' if sys.platform.startswith('linux') else sys.platform)).casefold()
    if selected == "linux":
        return IDFILE_DECOMPRESSOR_LINUX
    if selected == "windows":
        return IDFILE_DECOMPRESSOR_WINDOWS
    return None


IDFILE_DECOMPRESSOR = idfile_decompressor_spec() or IDFILE_DECOMPRESSOR_LINUX


@dataclass(frozen=True)
class GameLinkResult:
    state: str
    message: str
    path: str
    sha256: str
    ownership: str
    backup_path: str = ""


def _defender_allow_action(data, events):
    threat_id = data.get("Threat ID", "")
    setting = next((record for record in events if record["id"] == 5007 and threat_id and
                    f"\\ThreatIDDefaultAction\\{threat_id} =" in record["data"].get("New Value", "")), None)
    return setting, setting and setting["data"]["New Value"].endswith("= 0x6")


def _defender_detection_error(events, candidates, message):
    for event in events:
        data = event["data"]
        resources = {resource.strip().removeprefix("file:_").removeprefix("file:").casefold()
                     for resource in data.get("Path", "").split(";")}
        if event["id"] not in (1116, 1117) or not resources & candidates:
            continue
        setting, allowed = _defender_allow_action(data, events)
        detail = (f"An allow action is recorded at {setting['time']}. Retry setup to restore the verified file. If it is removed again, check Protection History for this exact path."
                  if allowed else 'Review this detection in Windows Security -> Protection history before retrying.')
        return RuntimeError(f"{message} Defender detected {data.get('Threat Name', 'this file')} at {event['time']}. {detail}")


def _core_file_error(path: Path, error: Exception) -> RuntimeError:
    message = 'Core runtime file could not be read or installed: {value_0}. {value_1}'.format(value_0=f'{path}', value_1=f'{error}')
    if sys.platform != "win32":
        return RuntimeError(message)
    script = ''.join(("$ErrorActionPreference = 'Stop'", '\n', '[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()', '\n', "Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Windows Defender/Operational'; Id=1116,1117,5007; StartTime=(Get-Date).AddDays(-7)} -MaxEvents 200 | ForEach-Object {", '\n', '    $xml = [xml]$_.ToXml(); $data = @{{}}', '\n', "    foreach ($item in $xml.Event.EventData.Data) {{ $data[$item.Name] = $item.'#text' }}", '\n', "    @{{time=$_.TimeCreated.ToUniversalTime().ToString('o'); id=$_.Id; data=$data}}", '\n', '}} | ConvertTo-Json -Depth 4 -Compress', '\n', ''))
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, encoding="utf-8", errors="replace", timeout=5,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise ValueError('Defender history query failed')
        events = json.loads(result.stdout)
        if isinstance(events, dict):
            events = [events]
        candidates = {str(candidate).casefold() for candidate in
                      (path, getattr(error, "filename", None), getattr(error, "cause_filename", None),
                       getattr(error, "source_path", None)) if candidate}
        detection = _defender_detection_error(events, candidates, message)
        if detection is not None:
            return detection
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        return RuntimeError(message + ' Protection history could not be read. Select Help -> SAVE SUPPORT REPORT.')
    return RuntimeError(message + ' No matching detection was found. Select Help -> SAVE SUPPORT REPORT.')


def install_meathook(
    game_root: Path, dependency_manager: DependencyManager, *, state_dir: Path | None = None,
    consent: Callable[[DependencySpec], bool], local_artifact: Path | None = None, force_repair: bool = False,
    journal=None,
) -> GameLinkResult:
    from .launcher_core_install import game_write_lock, recover_interrupted
    root = validate_game_root(game_root)
    with game_write_lock(root):
        if journal is None:
            require_installation_idle(root)
        else:
            journal.document["evidence"]["core_state_dir"] = str(Path(state_dir).resolve()) if state_dir else None
            journal.save()
        processes = detect_doom_processes()
        if processes:
            if probe_meathook(root).ok and not force_repair:
                installed = probe_meathook(root)
                return GameLinkResult("verified", installed.message, str(root / "sentinel_core.dll"), str(installed.details["sha256"]), "verified")
            detected = ", ".join('{value_0} (PID {value_1})'.format(value_0=f"{item['name']}", value_1=f"{item['pid']}") for item in processes)
            error = RuntimeError('Close DOOM Eternal before repairing its connection files. Detected: {value_0}'.format(value_0=f'{detected}'))
            error.processes = processes
            raise error
        recover_interrupted(root, state_dir)
        return _install_core_pair(root, state_dir, consent, local_artifact, force_repair, journal)


def _core_artifact_matches(path, record):
    if not path.exists():
        return True
    payload = path.read_bytes()
    return len(payload) == record['size'] and hashlib.sha256(payload).hexdigest() == record['sha256']


def _owned_core_partial(root, existing, manifest):
    from ..contracts.core_distribution import read_manifest
    from .launcher_core_install import NAMES

    if not existing.is_file() and not any((root / name).exists() for name in NAMES[:2]):
        return False
    try:
        previous = {item['path']: item for item in
                    read_manifest(existing if existing.exists() else manifest, require_compatible=False)['artifacts']}
        return all(_core_artifact_matches(root / name, previous[name]) for name in ('sentinel_core.dll', 'msimg32.dll'))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _core_without_artifact(installed, root):
    if installed.ok:
        return GameLinkResult(state="verified", message=installed.message, path=str(root / "sentinel_core.dll"),
                              sha256=str(installed.details["sha256"]), ownership="verified")


def _keep_installed_core(installed, value, force_repair, root):
    if installed.ok:
        from ..contracts.core_distribution import version_key
        current = version_key(str(installed.details["version"]))
        incoming_version = version_key(value["version"])
        if current > incoming_version:
            return GameLinkResult(state="verified", message=installed.message, path=str(root / "sentinel_core.dll"),
                                  sha256=str(installed.details["sha256"]), ownership="verified")
        if installed.details["build_id"] == value["build_id"] and not force_repair:
            return GameLinkResult("verified", installed.message, str(root / "sentinel_core.dll"), str(installed.details["sha256"]), "verified")


def _confirm_core_replacement(root, installed, owned_partial, value, force_repair, consent):
    if any((root / name).exists() for name in ("sentinel_core.dll", "msimg32.dll")) and not installed.ok and not owned_partial:
        spec = DependencySpec(player_text('launcher.platform', 'confirm_connection_replacement'), value["version"],
            str(root), value["build_id"], "", "local")
        if not force_repair or not consent(spec):
            raise RuntimeError('Core/bootstrap ownership cannot be verified; foreign files are preserved.')


def _publish_core_pair(contents, manifest, root, state_dir, journal, owned_partial, value):
    from .launcher_core_install import NAMES
    incoming = {name: contents[name] for name in NAMES[:2]}
    incoming[NAMES[2]] = manifest.read_bytes()
    try:
        backup = _replace_core_pair(root, state_dir, incoming, journal)
    except OSError as error:
        raise _core_file_error(Path(error.filename) if error.filename else root / "sentinel_core.dll", error) from error
    return GameLinkResult(state="repaired" if owned_partial else "installed", message='Game connection files {value_0} installed.'.format(value_0=f"{value['version']}"),
                          path=str(root / "sentinel_core.dll"), sha256=hashlib.sha256(contents["sentinel_core.dll"]).hexdigest(),
                          ownership="launcher_installed", backup_path=str(backup))


def _install_core_pair(root, state_dir, consent, local_artifact, force_repair, journal=None):
    """Install a verified local Core distribution without replacing foreign providers."""
    from ..contracts.core_distribution import verify_runtime
    installed = probe_meathook(root)
    if local_artifact is None:
        return _core_without_artifact(installed, root)
    manifest = local_artifact / "distribution.json" if local_artifact.is_dir() else local_artifact
    try:
        value, contents = verify_runtime(manifest, bootstrap_from_archive=True)
    except OSError as error:
        raise _core_file_error(Path(error.filename) if error.filename else manifest, error) from error
    retained = _keep_installed_core(installed, value, force_repair, root)
    if retained is not None:
        return retained
    if (root / "XINPUT1_3.dll").exists():
        raise RuntimeError('XINPUT1_3.dll conflicts with DoomEAP. Uninstall the mod that installed it, then retry. If you do not know which mod installed it, select Help -> SAVE SUPPORT REPORT.')
    existing = root / "sentinel-distribution.json"
    owned_partial = _owned_core_partial(root, existing, manifest) if not installed.ok else False
    _confirm_core_replacement(root, installed, owned_partial, value, force_repair, consent)
    return _publish_core_pair(contents, manifest, root, state_dir, journal, owned_partial, value)


def _replace_core_pair(root, state_dir, incoming, journal):
    from .launcher_core_install import replace_pair
    if journal is not None:
        for name, content in incoming.items():
            journal.check_space(root / name, len(content))
            journal.expect(root / name, hashlib.sha256(content).hexdigest())
    return replace_pair(root, state_dir, incoming, journal=journal)


def require_installation_idle(game_root):
    from .install_journal import InstallationRecoveryRequired, InstallJournal, InstallPhase
    journal = InstallJournal(game_root)
    if journal.load() and journal.phase not in {InstallPhase.COMPLETE, InstallPhase.ROLLED_BACK}:
        raise InstallationRecoveryRequired('installation recovery is pending')


@contextmanager
def installation_write(game_root, journal=None):
    from .launcher_core_install import game_write_lock
    with game_write_lock(game_root):
        if journal is None:
            require_installation_idle(game_root)
        yield


@contextmanager
def client_config_write(bases):
    from .install_journal import InstallationRecoveryRequired
    from .launcher_core_install import game_write_lock
    roots = _configuration_roots(bases)
    with ExitStack() as stack:
        for root in roots:
            require_installation_idle(root)
            try:
                stack.enter_context(game_write_lock(root, blocking=False))
            except (TimeoutError, BlockingIOError) as error:
                raise InstallationRecoveryRequired('game configuration is busy') from error
            require_installation_idle(root)
        yield


def _configuration_roots(bases):
    roots = set()
    for base in bases:
        if not base:
            continue
        path = Path(str(base)).expanduser().resolve()
        root = path.parent if path.name.casefold() == "base" else path
        if root.is_dir():
            roots.add(root)
    return sorted(roots)


@dataclass(frozen=True)
class InstalledDependency:
    name: str
    version: str
    artifact_sha256: str
    source_url: str
    root: str
    executable: str
    commit: str = ""


class DependencyManager:
    """Acquire pinned dependencies only after consent, then verify and atomically install."""

    RECEIPT = "dependency.json"

    def __init__(
        self,
        root: Path,
        transport: DownloadTransport | None = None,
        diagnostic_sink: Callable[[str, dict[str, object]], None] | None = None,
    ):
        self.root = root
        self.transport = transport or UrlDownloadTransport()
        self.diagnostic_sink = diagnostic_sink

    @staticmethod
    def current_platform_name() -> str:
        if os.name == "nt":
            return "windows"
        if sys.platform.startswith("linux"):
            return "linux"
        return sys.platform.casefold()

    _platform_name = current_platform_name

    def platform_supported(self, spec: DependencySpec, platform_name: str | None = None) -> bool:
        selected = (platform_name or self._platform_name()).casefold()
        return not spec.platforms or selected in spec.platforms

    @staticmethod
    def _cache_key(spec: DependencySpec) -> str:
        platform = "+".join(sorted(spec.platforms)) or "any"
        return f"{spec.name}-{platform}-{spec.version}-{spec.sha256}"

    def _destination(self, spec: DependencySpec) -> Path:
        return self.root / self._cache_key(spec)

    def _legacy_destination(self, spec: DependencySpec) -> Path:
        return self.root / f"{spec.name}-{spec.version}"

    def _acquisition_path(self, spec: DependencySpec) -> Path:
        return self.root / f".{self._cache_key(spec)}.last-acquisition.json"

    def _record_acquisition(self, spec: DependencySpec, state: str, **details: object) -> None:
        payload: dict[str, object] = {
            'name': spec.name, 'version': spec.version, 'platform': self._platform_name(), 'state': state,
            'source_url': spec.url, 'expected_size': spec.expected_size, 'expected_sha256': spec.sha256, **details,
        }
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            destination = self._acquisition_path(spec)
            temporary = destination.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            publish_file(temporary, destination, operation="dependency_diagnostic_receipt")
        except OSError:
            pass
        if self.diagnostic_sink is not None:
            self.diagnostic_sink("dependency_diagnostic", payload)

    def last_acquisition(self, spec: DependencySpec) -> dict[str, object] | None:
        try:
            document = json.loads(self._acquisition_path(spec).read_text(encoding="utf-8"))
            return document if isinstance(document, dict) else None
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    @contextmanager
    def _cache_lock(self, spec: DependencySpec):
        """Serialize cache inspection and publication across launcher processes.

        Windows uses a named session mutex (Local\\ + sha256 of cache root and
        entry identity) with a 30s wait. Byte-range locks on a buffered handle
        cannot serialize here because the diagnostic receipt write closes the
        handle before the guarded section exits. Linux keeps fcntl.flock.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        cache_key = self._cache_key(spec)
        if os.name == "nt":
            mutex_name = _dependency_mutex_name(self.root, cache_key)
            handle, abandoned = _acquire_windows_mutex(mutex_name)
            if abandoned and self.diagnostic_sink is not None:
                try:
                    self.diagnostic_sink("dependency_diagnostic", {
                        'name': spec.name, 'version': spec.version, 'platform': self._platform_name(),
                        'state': 'cache_lock_abandoned', 'source_url': spec.url,
                    })
                except (OSError, ValueError, TypeError):
                    pass
            try:
                yield
            finally:
                _release_windows_mutex(handle)
            return
        lock_path = self.root / f".{cache_key}.lock"
        with lock_path.open("a+b") as lock:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _safe_member(name: str) -> bool:
        path = PurePosixPath(name.replace("\\", "/"))
        return not path.is_absolute() and ".." not in path.parts

    @classmethod
    def _extract_zip(cls, archive, destination):
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                if not cls._safe_member(member.filename):
                    raise ValueError('unsafe archive path: {value_0}'.format(value_0=f'{member.filename}'))
                mode = member.external_attr >> 16
                if mode & 0o170000 == 0o120000:
                    raise ValueError('archive symlink rejected: {value_0}'.format(value_0=f'{member.filename}'))
            source.extractall(destination)

    @classmethod
    def _extract_tar(cls, archive, destination):
        with tarfile.open(archive, "r:gz") as source:
            members = source.getmembers()
            for member in members:
                if (
                    not cls._safe_member(member.name)
                    or member.issym()
                    or member.islnk()
                    or not (member.isfile() or member.isdir())
                ):
                    raise ValueError('unsafe archive member: {value_0}'.format(value_0=f'{member.name}'))
            source.extractall(destination, members=members)

    @classmethod
    def _extract(cls, spec: DependencySpec, archive: Path, destination: Path) -> None:
        if spec.archive_type == "file":
            target_name = spec.executable_glob or archive.name
            target_file = destination / target_name
            shutil.copy2(archive, target_file)
            return
        elif spec.archive_type == "zip":
            cls._extract_zip(archive, destination)
        elif spec.archive_type == "tar.gz":
            cls._extract_tar(archive, destination)
        else:
            raise ValueError('unsupported archive type: {value_0}'.format(value_0=f'{spec.archive_type}'))

    @classmethod
    def is_contained(cls, candidate: Path | str, root: Path | str) -> bool:
        """Verify candidate physically resides inside root after canonical resolution."""
        try:
            canonical_candidate = Path(candidate).resolve()
            canonical_root = Path(root).resolve()
            if canonical_candidate == canonical_root:
                return False
            canonical_candidate.relative_to(canonical_root)
            return True
        except (ValueError, OSError, RuntimeError):
            return False

    is_contained_within = is_contained
    is_contained_in = is_contained

    @classmethod
    def contains_path(cls, root: Path | str, candidate: Path | str) -> bool:
        return cls.is_contained(candidate, root)

    def _receipt_mismatch(self, receipt, spec, destination):
        if receipt.name != spec.name:
            return None, "receipt_name_mismatch"
        if receipt.version != spec.version:
            return None, "receipt_version_mismatch"
        if receipt.artifact_sha256 != spec.sha256:
            return None, "receipt_sha256_mismatch"
        if spec.commit and receipt.commit not in {"", spec.commit}:
            return None, "receipt_commit_mismatch"
        if Path(receipt.root).resolve() != destination.resolve():
            return None, "receipt_root_mismatch"

    def _installed_with_reason(self, spec: DependencySpec, destination: Path) -> tuple[InstalledDependency | None, str]:
        receipt_path = destination / self.RECEIPT
        if not receipt_path.is_file():
            return None, "receipt_missing"
        try:
            receipt = InstalledDependency(**json.loads(receipt_path.read_text(encoding="utf-8")))
            executable = Path(receipt.executable)
        except (OSError, ValueError, TypeError):
            return None, "receipt_invalid"
        mismatch = self._receipt_mismatch(receipt, spec, destination)
        if mismatch is not None:
            return mismatch
        if not executable.is_file():
            return None, "executable_missing"
        if not self.is_contained(executable, destination):
            return None, "executable_outside_cache"
        if spec.archive_type == "file" and self._sha256(executable) != spec.sha256:
            return None, "cached_sha256_mismatch"
        if os.name != "nt" and not os.access(executable, os.X_OK):
            return None, "executable_not_permitted"
        return receipt, "verified"

    def _installed(self, spec: DependencySpec, destination: Path) -> InstalledDependency | None:
        return self._installed_with_reason(spec, destination)[0]

    def cache_diagnostics(self, spec: DependencySpec) -> dict[str, object]:
        destination = self._destination(spec)
        installed, reason = self._installed_with_reason(spec, destination)
        if installed is not None:
            executable = Path(installed.executable)
        else:
            executable = destination / spec.executable_glob
            receipt_path = destination / self.RECEIPT
            if receipt_path.is_file():
                try:
                    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
                    if isinstance(payload, dict) and isinstance(payload.get("executable"), str):
                        executable = Path(payload["executable"])
                except (OSError, ValueError, TypeError):
                    pass
        canonical_destination = destination.resolve()
        canonical_executable = executable.resolve()
        details: dict[str, object] = {
            'cache_path': str(executable), 'cache_path_display': str(executable),
            'cache_path_canonical': str(canonical_executable), 'cache_root_display': str(destination),
            'cache_root_canonical': str(canonical_destination), 'executable_display': str(executable),
            'executable_canonical': str(canonical_executable), 'executable_path': str(executable),
            'executable_path_canonical': str(canonical_executable),
            'containment': self.is_contained(executable, destination), 'cache_exists': executable.is_file(),
            'cached_size': None, 'cached_sha256': None, 'hash_valid': False, 'mode': None, 'executable': False,
            'verification_stage': reason,
        }
        if executable.is_file():
            stat_result = executable.stat()
            details.update({
                'cached_size': stat_result.st_size, 'cached_sha256': self._sha256(executable),
                'hash_valid': self._sha256(executable) == spec.sha256, 'mode': oct(stat_result.st_mode & 511),
                'executable': os.access(executable, os.X_OK),
            })
        if installed is not None:
            details["status"] = "verified"
        else:
            details["status"] = "missing_or_invalid"
        last = self.last_acquisition(spec)
        if last is not None:
            details["last_acquisition"] = last
        return details

    def inspect(self, spec: DependencySpec) -> InstalledDependency | None:
        """Return dependency only when cache receipt and artifact remain verified."""
        if not self.platform_supported(spec):
            return None
        with self._cache_lock(spec):
            destination = self._destination(spec)
            installed = self._installed(spec, destination)
            if installed is not None:
                return installed
            legacy = self._installed(spec, self._legacy_destination(spec))
            if legacy is None:
                return None
            self._promote_legacy(spec, legacy)
            return self._installed(spec, destination)

    def _promote_legacy(self, spec: DependencySpec, installed: InstalledDependency) -> None:
        legacy = self._legacy_destination(spec)
        destination = self._destination(spec)
        relative_executable = Path(installed.executable).resolve().relative_to(legacy.resolve())
        receipt_path = legacy / self.RECEIPT
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["root"] = str(destination.resolve())
        receipt["executable"] = str((destination / relative_executable).resolve())
        incoming_receipt = legacy / f".{self.RECEIPT}.incoming"
        incoming_receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        publish_file(incoming_receipt, receipt_path, operation="dependency_receipt_publish")
        publish_file(legacy, destination, operation="dependency_legacy_promote")

    def _acquire_archive(self, local_artifact, spec, archive):
        if local_artifact is None:
            try:
                download = self.transport.fetch(spec.url, archive)
            except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
                self._record_acquisition(
                    spec, 'failed', verification_stage='transport', failure_reason=f'{type(error).__name__}: {error}',
                    final_url=spec.url,
                )
                raise
            final_url = download.final_url if download is not None else spec.url
            content_length = download.content_length if download is not None else None
        else:
            if not local_artifact.is_file():
                raise FileNotFoundError(local_artifact)
            shutil.copy2(local_artifact, archive)
            final_url = str(local_artifact.resolve())
            content_length = archive.stat().st_size
        return final_url, content_length

    def _verify_download(self, archive, spec, final_url, content_length):
        actual = self._sha256(archive)
        actual_size = archive.stat().st_size
        self._record_acquisition(
            spec, 'downloaded', final_url=final_url, content_length=content_length, actual_download_size=actual_size,
            actual_download_sha256=actual,
        )
        if spec.expected_size is not None and actual_size != spec.expected_size:
            self._record_acquisition(
                spec, 'failed', verification_stage='download_size', failure_reason='download_size_mismatch',
                actual_download_size=actual_size, actual_download_sha256=actual, final_url=final_url,
            )
            raise ValueError('size mismatch for {value_0}: expected {value_1}, got {value_2}'.format(value_0=f'{spec.name}', value_1=f'{spec.expected_size}', value_2=f'{actual_size}'))
        if actual != spec.sha256:
            self._record_acquisition(
                spec, 'failed', verification_stage='download_sha256', failure_reason='download_sha256_mismatch',
                actual_download_size=actual_size, actual_download_sha256=actual, final_url=final_url,
            )
            raise ValueError('SHA-256 mismatch for {value_0}: expected {value_1}, got {value_2}'.format(value_0=f'{spec.name}', value_1=f'{spec.sha256}', value_2=f'{actual}'))
        return actual, actual_size

    def _unpack_dependency(self, temporary, spec, archive):
        extracted = temporary / "extracted"
        extracted.mkdir()
        self._extract(spec, archive, extracted)
        executables = sorted(extracted.glob(spec.executable_glob))
        if len(executables) != 1:
            raise ValueError('expected one {value_0}, found {value_1}'.format(value_0=f'{spec.executable_glob}', value_1=f'{len(executables)}'))
        executable_relative = executables[0].relative_to(extracted)
        if os.name != "nt":
            executables[0].chmod(executables[0].stat().st_mode | 0o100)
        return extracted, executable_relative

    def _publish_dependency(self, spec, staged, destination):
        previous = self.root / f".{self._cache_key(spec)}.previous"
        if previous.exists():
            if previous.is_dir() and not previous.is_symlink():
                shutil.rmtree(previous)
            else:
                previous.unlink()
        if destination.exists():
            publish_file(destination, previous, operation="dependency_rotate")
        try:
            publish_file(staged, destination, operation="dependency_publish")
        except (OSError, ValueError, TypeError):
            if previous.exists() and not destination.exists():
                publish_file(previous, destination, operation="dependency_rollback")
            raise
        if previous.exists():
            shutil.rmtree(previous) if previous.is_dir() else previous.unlink()

    def _verify_published_dependency(self, receipt, spec, destination, final_url, content_length, actual_size, actual):
        if not Path(receipt.executable).is_file():
            raise RuntimeError('dependency installation failed')
        verified = self._installed(spec, destination)
        if verified is None:
            _, reason = self._installed_with_reason(spec, destination)
            self._record_acquisition(
                spec, 'failed', verification_stage='post_publish', failure_reason=reason,
                cache_path=str(receipt.executable), cache_root_display=str(destination),
                cache_root_canonical=str(destination.resolve()), executable_display=str(receipt.executable),
                executable_canonical=str(Path(receipt.executable).resolve()),
                containment=self.is_contained(Path(receipt.executable), destination),
            )
            raise RuntimeError('dependency installation failed verification: {value_0}'.format(value_0=f'{reason}'))
        executable = Path(verified.executable)
        self._record_acquisition(
            spec, 'published', verification_stage='post_publish', cache_path=str(executable),
            cached_size=executable.stat().st_size, cached_sha256=self._sha256(executable),
            mode=oct(executable.stat().st_mode & 511), executable=os.access(executable, os.X_OK), final_url=final_url,
            content_length=content_length, actual_download_size=actual_size, actual_download_sha256=actual,
        )
        return verified

    def acquire(
        self,
        spec: DependencySpec,
        *,
        consent: Callable[[DependencySpec], bool],
        local_artifact: Path | None = None,
        platform_name: str | None = None,
    ) -> InstalledDependency:
        if not self.platform_supported(spec, platform_name):
            selected = platform_name or self._platform_name()
            raise RuntimeError('dependency {value_0} is unsupported on platform {value_1}'.format(value_0=f'{spec.name}', value_1=f'{selected}'))
        destination = self._destination(spec)
        with self._cache_lock(spec):
            installed = self._installed(spec, destination)
            if installed is not None:
                self._record_acquisition(spec, "cache_hit", cache_path=str(installed.executable))
                return installed
            legacy = self._installed(spec, self._legacy_destination(spec))
            if legacy is not None:
                self._promote_legacy(spec, legacy)
                installed = self._installed(spec, destination)
                if installed is not None:
                    self._record_acquisition(spec, "cache_hit", cache_path=str(installed.executable))
                    return installed
            self._record_acquisition(spec, "cache_miss", cache_path=str(destination))
            if local_artifact is None and not consent(spec):
                self._record_acquisition(spec, "declined", failure_reason="consent_declined")
                raise PermissionError('dependency acquisition declined: {value_0}'.format(value_0=f'{spec.name}'))

            with tempfile.TemporaryDirectory(prefix=f".{spec.name}-", dir=self.root) as temporary_name:
                temporary = Path(temporary_name)
                archive = temporary / "artifact"
                final_url, content_length = self._acquire_archive(local_artifact, spec, archive)
                actual, actual_size = self._verify_download(archive, spec, final_url, content_length)

                extracted, executable_relative = self._unpack_dependency(temporary, spec, archive)

                staged = self.root / f".{self._cache_key(spec)}.incoming"
                shutil.rmtree(staged, ignore_errors=True)
                shutil.copytree(extracted, staged)
                receipt = InstalledDependency(
                    name=spec.name, version=spec.version, artifact_sha256=actual,
                    source_url=spec.url if local_artifact is None else str(local_artifact.resolve()),
                    root=str(destination.resolve()), executable=str((destination / executable_relative).resolve()),
                    commit=spec.commit,
                )
                (staged / self.RECEIPT).write_text(
                    json.dumps(asdict(receipt), indent=2, sort_keys=True) + '\n', encoding='utf-8',
                )
                self._publish_dependency(spec, staged, destination)
                return self._verify_published_dependency(receipt, spec, destination, final_url, content_length, actual_size, actual)

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()

    def remove(self, spec: DependencySpec) -> bool:
        """Remove hashed and legacy cache entries owned by dependency spec."""
        with self._cache_lock(spec):
            removed = False
            for destination in (self._destination(spec), self._legacy_destination(spec)):
                if destination.exists() or destination.is_symlink():
                    self._remove_path(destination)
                    removed = True
            return removed

    def cache_present(self, spec: DependencySpec) -> bool:
        return any(
            path.exists() or path.is_symlink()
            for path in (self._destination(spec), self._legacy_destination(spec))
        )


is_contained = DependencyManager.is_contained
is_contained_within = DependencyManager.is_contained


@dataclass(frozen=True)
class AdapterResult:
    state: str
    message: str
    command: tuple[str, ...] = ()
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    details: dict[str, object] = field(default_factory=dict)


class LinuxVerificationError(ValueError):
    def __init__(self, message: str, details: Mapping[str, object] | None = None):
        super().__init__(message)
        self.details = dict(details or {})


def _contains_bytes(path: Path, needle: bytes, *, chunk_size: int = 1024 * 1024) -> bool:
    """Search bounded chunks without loading an entire game resource archive."""
    if not needle:
        return False
    overlap = len(needle) - 1
    try:
        with path.open("rb") as source:
            previous = b""
            while chunk := source.read(chunk_size):
                if needle in previous + chunk:
                    return True
                previous = (previous + chunk)[-overlap:] if overlap else b""
    except OSError:
        return False
    return False


def _vanilla_resource_name(name):
    return (isinstance(name, str) and name.endswith('.resources')
            and not name.replace("\\", "/").removeprefix('./').startswith('ap_enemy_resources/'))


def _resource_references(refs, map_index, files, runtime_map):
    candidates: list[tuple[int, str]] = []
    for relation in refs:
        if not isinstance(relation, dict) or relation.get("map") != map_index:
            continue
        file_index = relation.get("file")
        if not isinstance(file_index, int) or not 0 <= file_index < len(files):
            raise ValueError('runtime map {value_0} has invalid resource reference'.format(value_0=f'{runtime_map!r}'))
        record = files[file_index]
        name = record.get("name") if isinstance(record, dict) else None
        if _vanilla_resource_name(name):
            candidates.append((file_index, name))
    return candidates


def _packagemapspec_owner(packagemapspec: Path, runtime_map: str) -> tuple[str, int, list[str]]:
    document = json.loads(packagemapspec.read_text(encoding="utf-8"))
    files = document.get("files")
    maps = document.get("maps")
    refs = document.get("mapFileRefs")
    if not isinstance(files, list) or not isinstance(maps, list) or not isinstance(refs, list):
        raise ValueError('packagemapspec.json has invalid resource tables')
    map_indices = [
        index for index, record in enumerate(maps)
        if isinstance(record, dict) and record.get("name") == runtime_map
    ]
    if len(map_indices) != 1:
        raise ValueError('runtime map {value_0} has no unique packagemapspec record'.format(value_0=f'{runtime_map!r}'))
    map_index = map_indices[0]
    candidates = _resource_references(refs, map_index, files, runtime_map)
    if not candidates:
        raise ValueError('runtime map {value_0} has no resource reference'.format(value_0=f'{runtime_map!r}'))
    return candidates[0][1], candidates[0][0], [name for _, name in candidates]


def installation_error_details(error: Exception, step: str) -> dict:
    return {
        'required_resources': [], **getattr(error, 'details', {}),
        'failure_step': getattr(error, 'details', {}).get('failure_step', step), 'exception_type': type(error).__name__,
        'code': getattr(error, 'code', None), 'verification_error': str(error),
        'traceback': ''.join(traceback.format_exception(type(error), error, error.__traceback__, limit=6)),
    }


@contextmanager
def installation_step(step: str):
    try:
        yield
    except (OSError, RuntimeError, ValueError, KeyError, zipfile.BadZipFile, struct.error) as error:
        error.details = installation_error_details(error, step)
        raise


def _installation_failure(result: AdapterResult, error: Exception, step: str, event_sink,
                          game_root: Path | None = None, staged_mod: Path | None = None) -> AdapterResult:
    details = {**result.details, **installation_error_details(error, step), "post_install_verification": "failed"}
    if not details["required_resources"] and game_root is not None and staged_mod is not None:
        try:
            details["required_resources"], _ = _required_linux_resource_specs(staged_mod, game_root / "base/packagemapspec.json")
        except (OSError, RuntimeError, ValueError, KeyError, zipfile.BadZipFile, struct.error) as evidence_error:
            details["resource_evidence_error"] = f"{type(evidence_error).__name__}: {evidence_error}"
    message = _injector_failure_message(error, details["failure_step"])
    if event_sink is not None:
        for line in json.dumps(details, indent=2).splitlines():
            event_sink("log", message=line)
        for line in details["traceback"].splitlines():
            event_sink("log", message=line)
    return AdapterResult(state="failed", message=message, command=result.command, stdout=result.stdout,
                         stderr=result.stderr, returncode=result.returncode, details=details)


def _injector_failure_message(error: Exception, step: str) -> str:
    if isinstance(error, InjectorRecoveryRequired):
        return str(error)
    return 'Installation failed at {value_0}: {value_1}: {value_2}'.format(value_0=f'{step}', value_1=f'{type(error).__name__}', value_2=f'{error}')


def _injector_settings(text):
    return {key: next((line.split('=', 1)[1] for line in text.splitlines() if line.startswith(f':{key}=')), None)
            for key in ('HAS_CHECKED_RESOURCES', 'RESET_BACKUPS', 'ASSET_VERSION')}


def _injector_summary_fields(text, base, lines, spec, decoded):
    return {
        'settings': _injector_settings(text),
        'settings_backup_lines': sum(line.rstrip().endswith('.backup') for line in text.splitlines()),
        'backup_files': sum(path.is_file() for path in base.rglob('*.backup')), 'rehash_lines': len(lines),
        'rehash_bank_lines': sum('ap_enemy_resources/' in line.replace('\\', '/') for line in lines),
        'bank_files': sorted(path.name for path in (base / 'ap_enemy_resources').glob('*')),
        'bank_marker': spec.is_file() and 'doomEAPEnemyResourceBanks' in spec.read_text(encoding='utf-8-sig'),
        'meta_decoded': decoded,
    }


def injector_state_summary(game_root: Path) -> dict:
    from .campaign_resources import _records
    base = game_root / "base"
    settings = game_root / 'EternalModInjector Settings.txt'
    mapping = base / "idRehash.map"
    spec = base / "packagemapspec.json"
    text = settings.read_text(encoding="utf-8-sig", errors="replace") if settings.is_file() else ""
    lines = mapping.read_text(encoding="utf-8-sig", errors="replace").splitlines() if mapping.is_file() else []
    try:
        row = _records(base / "meta.resources", {"generated/buildgame/container.mask"})["generated/buildgame/container.mask"]
        decoded = row[4] == 0
    except (OSError, ValueError, struct.error) as error:
        decoded = 'unavailable: {value_0}: {value_1}'.format(value_0=f'{type(error).__name__}', value_1=f'{error}')
    return _injector_summary_fields(text, base, lines, spec, decoded)


def _log_injector_state(game_root: Path, phase: str, event_sink) -> dict:
    try:
        summary = injector_state_summary(game_root)
    except (OSError, ValueError) as error:
        summary = {"error": f"{type(error).__name__}: {error}"}
    if event_sink is not None:
        for key, value in summary.items():
            for line in json.dumps(value, indent=2).splitlines():
                event_sink("log", message='Injector {value_0}: {value_1}={value_2}'.format(value_0=f'{phase}', value_1=f'{key}', value_2=f'{line}'))
    return summary


def _injector_mask_offsets(base: Path) -> set[int] | None:
    from .campaign_resources import _records
    from .resource_bank import mask_tables
    try:
        mask = _records(base / "meta.resources", {"generated/buildgame/container.mask"})["generated/buildgame/container.mask"][3]
        return set(mask_tables(mask))
    except (OSError, ValueError, struct.error):
        return None


def _mapping_row(line: str) -> tuple[str, int | None]:
    name, separator, offset = line.rpartition(";")
    name = PurePosixPath(name.replace("\\", "/")).as_posix()
    offset = offset.strip()
    value = int(offset) if separator and len(offset) <= 20 and re.fullmatch(r"[0-9]+", offset) else None
    return name, value


def _clean_injector_mapping(original: str, offsets: set[int] | None) -> tuple[str, bool]:
    kept, names, seen_offsets, bank_offsets = [], set(), set(), set()
    coherent = offsets is not None
    for line in original.splitlines(keepends=True):
        name, value = _mapping_row(line)
        if name.casefold().startswith("ap_enemy_resources/"):
            bank_offsets.add(value)
            continue
        if value is None or not name.endswith('.resources'):
            coherent = False
            continue
        if not _mapping_row_allowed(name, value, offsets, names, seen_offsets):
            coherent = False
            continue
        names.add(name.casefold())
        seen_offsets.add(value)
        kept.append(line)
    return "".join(kept), bool(kept) and coherent and offsets is not None and seen_offsets == offsets - bank_offsets


def _mapping_row_allowed(name, value, offsets, names, seen_offsets) -> bool:
    path = PurePosixPath(name)
    return (not path.is_absolute() and ".." not in path.parts and ":" not in name
            and (offsets is None or value in offsets) and name.casefold() not in names and value not in seen_offsets)


def _injector_mapping_changes(game_root: Path) -> dict[Path, bytes]:
    base = game_root / "base"
    mapping = base / "idRehash.map"
    original = mapping.read_text(encoding="utf-8-sig", errors="replace") if mapping.is_file() else ""
    cleaned, coherent = _clean_injector_mapping(original, _injector_mask_offsets(base))
    changes = {}
    if mapping.is_file() and cleaned != original:
        changes[mapping] = cleaned.encode("utf-8")
    if not coherent:
        settings = game_root / 'EternalModInjector Settings.txt'
        content = settings.read_text(encoding="utf-8-sig") if settings.is_file() else ""
        lines = [line for line in content.splitlines() if not line.startswith(":HAS_CHECKED_RESOURCES=")]
        lines.append(':HAS_CHECKED_RESOURCES=0')
        changes[settings] = ("\n".join(lines) + "\n").encode("utf-8")
    return changes


INJECTOR_HOLD = ".doomeap-injector-hold"
INJECTOR_INPUTS = ("base/idRehash.map", "EternalModInjector Settings.txt")


class InjectorRecoveryRequired(RuntimeError):
    code = "injector_recovery_required"

    def __init__(self):
        super().__init__(player_text('launcher.setup', 'protection_stopped'))


class InjectorHoldPhase(str, Enum):
    HOLDING = "holding"
    PREPARED = "prepared"
    INJECTING = "injecting"
    RESTORING = "restoring"
    COMPLETE = "complete"


def _check_injector_path(path: Path) -> None:
    if path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise InjectorRecoveryRequired()


def _injector_file_hash(path: Path) -> str | None:
    _check_injector_path(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise InjectorRecoveryRequired()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _injector_bank_hash(directory: Path) -> str | None:
    _check_injector_path(directory)
    if not directory.exists():
        return None
    if not directory.is_dir():
        raise InjectorRecoveryRequired()
    files = {}
    pending = [directory]
    while pending:
        for path in pending.pop().iterdir():
            _check_injector_path(path)
            name = path.relative_to(directory).as_posix()
            if path.is_dir():
                files[name] = "directory"
                pending.append(path)
            else:
                files[name] = _injector_file_hash(path)
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def _write_injector_hold(root: Path, record: dict) -> None:
    payload = json.dumps(record, sort_keys=True).encode()
    document = {"record": record, "sha256": hashlib.sha256(payload).hexdigest()}
    _atomic_write_bytes(root / INJECTOR_HOLD / "journal.json", json.dumps(document).encode())


def _read_injector_hold(root: Path) -> dict:
    try:
        _check_injector_path(root / INJECTOR_HOLD / "journal.json")
        document = json.loads((root / INJECTOR_HOLD / "journal.json").read_bytes())
        record = document["record"]
        digest = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
        if (document["sha256"] != digest or record["version"] != 1 or record["game_root"] != str(root)
                or set(record["files"]) != set(INJECTOR_INPUTS)):
            raise ValueError('invalid injector hold')
        record["phase"] = InjectorHoldPhase(record["phase"])
        return record
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise InjectorRecoveryRequired() from error


def _create_injector_hold(root: Path, changes: dict[Path, bytes]) -> dict:
    directory = root / INJECTOR_HOLD
    record = {"version": 1, "game_root": str(root), "phase": InjectorHoldPhase.HOLDING,
              "banks": _injector_bank_hash(root / "base/ap_enemy_resources"), "files": {}}
    directory.mkdir()
    for index, name in enumerate(INJECTOR_INPUTS):
        path = root / name
        original = _injector_file_hash(path)
        if original is not None:
            _atomic_write_bytes(directory / f"input-{index}.before", path.read_bytes())
        prepared = hashlib.sha256(changes[path]).hexdigest() if path in changes else original
        record["files"][name] = {"original": original, "prepared": prepared}
    _write_injector_hold(root, record)
    return record


def _restore_injector_inputs(root: Path, record: dict) -> None:
    originals = {}
    for index, name in enumerate(INJECTOR_INPUTS):
        saved = root / INJECTOR_HOLD / f"input-{index}.before"
        entry = record["files"][name]
        if (_injector_file_hash(saved) != entry["original"]
                or _injector_file_hash(root / name) not in {entry["original"], entry["prepared"]}):
            raise InjectorRecoveryRequired()
        originals[name] = saved.read_bytes() if saved.exists() else None
    for name, content in originals.items():
        if content is None:
            (root / name).unlink(missing_ok=True)
        else:
            _atomic_write_bytes(root / name, content)


def _publish_injector_inputs(root: Path, changes: dict[Path, bytes], record: dict) -> None:
    for path in changes:
        if _injector_file_hash(path) != record["files"][path.relative_to(root).as_posix()]["original"]:
            raise InjectorRecoveryRequired()
    for path, content in changes.items():
        _atomic_write_bytes(path, content)


def _restore_injector_banks(root: Path, record: dict) -> None:
    original = root / "base/ap_enemy_resources"
    held = root / INJECTOR_HOLD / "ap_enemy_resources"
    if held.exists():
        if original.exists() or _injector_bank_hash(held) != record["banks"]:
            raise InjectorRecoveryRequired()
        held.rename(original)
    if _injector_bank_hash(original) != record["banks"]:
        raise InjectorRecoveryRequired()


def _cleanup_injector_hold(root: Path) -> None:
    directory = root / INJECTOR_HOLD
    for index in range(len(INJECTOR_INPUTS)):
        (directory / f"input-{index}.before").unlink(missing_ok=True)
    (directory / "journal.json").unlink(missing_ok=True)
    directory.rmdir()


def _recover_injector_resources(root: Path) -> bool:
    directory = root / INJECTOR_HOLD
    _check_injector_path(root / "base")
    _check_injector_path(directory)
    if not directory.exists():
        return False
    if not any(directory.iterdir()):
        directory.rmdir()
        return True
    record = _read_injector_hold(root)
    if record["phase"] is InjectorHoldPhase.INJECTING:
        raise InjectorRecoveryRequired()
    if record["phase"] in {InjectorHoldPhase.HOLDING, InjectorHoldPhase.PREPARED}:
        _restore_injector_inputs(root, record)
    if record["phase"] is not InjectorHoldPhase.COMPLETE:
        _restore_injector_banks(root, record)
        record["phase"] = InjectorHoldPhase.COMPLETE
        _write_injector_hold(root, record)
    _cleanup_injector_hold(root)
    return True


def recover_injector_resources(game_root: Path) -> bool:
    from .launcher_core_install import game_write_lock
    with game_write_lock(game_root):
        require_doom_closed('recovering injector resources')
        return _recover_injector_resources(game_root.resolve())


@dataclass
class InjectorResourceHold:
    root: Path
    record: dict

    def start_injector(self) -> None:
        self.record["phase"] = InjectorHoldPhase.INJECTING
        _write_injector_hold(self.root, self.record)

    def finish_injector(self) -> None:
        self.record["phase"] = InjectorHoldPhase.RESTORING
        _write_injector_hold(self.root, self.record)


def _finish_injector_hold(root, record, failure, event_sink):
    if record["phase"] is InjectorHoldPhase.INJECTING:
        if failure is None:
            raise InjectorRecoveryRequired()
        failure.details = {**getattr(failure, "details", {}), "recovery_required": True,
                           "recovery_error": player_text('launcher.setup', 'protection_stopped')}
        return
    if record["phase"] is InjectorHoldPhase.PREPARED:
        record["phase"] = InjectorHoldPhase.RESTORING
        _write_injector_hold(root, record)
    _recover_injector_resources(root)
    _log_injector_state(root, "after", event_sink)


@contextmanager
def prepare_injector_resources(game_root: Path, event_sink=None):
    from .launcher_core_install import game_write_lock
    game_root = game_root.resolve()
    with game_write_lock(game_root):
        require_doom_closed('preparing injector resources')
        _recover_injector_resources(game_root)
        _log_injector_state(game_root, 'before preparation', event_sink)
        changes = _injector_mapping_changes(game_root)
        record = _create_injector_hold(game_root, changes)
        directory = game_root / "base/ap_enemy_resources"
        completed = False
        try:
            if directory.exists():
                directory.rename(game_root / INJECTOR_HOLD / directory.name)
            _publish_injector_inputs(game_root, changes, record)
            record["phase"] = InjectorHoldPhase.PREPARED
            _write_injector_hold(game_root, record)
            _log_injector_state(game_root, "before", event_sink)
            yield InjectorResourceHold(game_root, record)
            completed = True
        finally:
            failure = None if completed else sys.exc_info()[1]
            _finish_injector_hold(game_root, record, failure, event_sink)


def _tag_resource_candidates(members):
    candidates = []
    for name in members:
        match = re.search(r"/maps/(game/(?:dlc|dlc2)/[^/]+/[^/]+)\.entities$", f"/{name}")
        if match is not None and match[1] != 'game/dlc/hub/hub':
            candidates.append((match[1], name.split("/", 1)[0], name))
    return sorted(candidates)


def _required_tag_resource(members, packagemapspec):
    candidates = _tag_resource_candidates(members)
    for runtime_map, staged_owner, member in candidates:
        try:
            expected_owner, _, _ = _packagemapspec_owner(packagemapspec, runtime_map)
        except (OSError, ValueError):
            continue
        if PurePosixPath(expected_owner).name.removesuffix(".resources") == staged_owner:
            return ('tag', runtime_map, member)
    return ('tag', candidates[0][0], None) if candidates else None


def _required_shell_resource(owners, owner_members, required):
    shell_owners = [
        owner for owner in owners
        if re.fullmatch(r"gameresources(?:_patch\d+)?", owner, re.IGNORECASE)
    ]
    shell_owner = "gameresources_patch1" if "gameresources_patch1" in shell_owners else next(iter(shell_owners), "gameresources")
    shell_members = [
        name for name in owner_members.get(shell_owner, ())
        if name.casefold().endswith((".decl", ".entities"))
    ]
    required.append(("shell", "", next(iter(shell_members or owner_members.get(shell_owner, ())), None)))
    return shell_owner


def _select_resource_member(runtime_map, packagemapspec, shell_owner, label, member, map_member, owner_members):
    expected_owner = ""
    priority: int | None = None
    candidates: list[str] = []
    owner_error = ""
    if runtime_map:
        try:
            expected_owner, priority, candidates = _packagemapspec_owner(packagemapspec, runtime_map)
        except (OSError, ValueError) as error:
            owner_error = f"{type(error).__name__}: {error}"
    else:
        expected_owner = f"{shell_owner}.resources"
    owner_stem = PurePosixPath(expected_owner).name.removesuffix(".resources")
    if runtime_map and label != "tag":
        member = map_member(runtime_map, owner_stem)
    selected_member = member if member and member.split("/", 1)[0] == owner_stem else None
    owner_member_list = owner_members.get(owner_stem, [])
    if selected_member is None and owner_member_list and not runtime_map:
        selected_member = owner_member_list[0]
    resource_path = packagemapspec.parent / expected_owner
    return expected_owner, priority, candidates, owner_error, owner_stem, selected_member, owner_member_list, resource_path


def _resource_payload_status(selected_member, resource_path, meta_path, path_present, payload_present):
    status = "verified" if (
        selected_member is not None
        and resource_path.is_file()
        and meta_path.is_file()
        and path_present
        and payload_present
    ) else "failed"
    return status


def _resource_payload_evidence(resource_path, selected_member, payloads, label, runtime_map, expected_owner, priority, candidates, owner_stem, owner_member_list, owner_error):
    meta_path = resource_path.with_suffix(".meta")
    path_marker = ""
    payload_present = False
    path_present = False
    if selected_member is not None:
        path_marker = selected_member.split("/", 1)[1]
        path_present = _contains_bytes(resource_path, path_marker.encode("utf-8"))
        payload_present = _contains_bytes(resource_path, payloads[selected_member])
    status = _resource_payload_status(selected_member, resource_path, meta_path, path_present, payload_present)
    result = {
        "label": label,
        "runtime_map": runtime_map,
        "expected_owner": expected_owner,
        "resource_path": str(resource_path),
        "meta_path": str(meta_path),
        "resource_priority": priority,
        "resource_candidates": candidates,
        "staged_owner": owner_stem,
        "staged_member": selected_member or "",
        "staged_member_sha256": (
            hashlib.sha256(payloads[selected_member]).hexdigest()
            if selected_member is not None else ""
        ),
        "owner_member_count": len(owner_member_list),
        "resource_exists": resource_path.is_file(),
        "meta_exists": meta_path.is_file(),
        "path_present": path_present,
        "payload_present": payload_present,
        "status": status,
        "failure_reason": "; ".join(reason for reason, present in (
            (owner_error, not owner_error), ('missing staged member', selected_member is not None),
            ('missing resource file', resource_path.is_file()), ('missing meta file', meta_path.is_file()),
            ('resource path missing from container', path_present),
            ('room payload missing from container', payload_present),
        ) if not present),
    }
    return status, result


def _inspect_required_resources(required, packagemapspec, shell_owner, map_member, owner_members, payloads, results, warnings):
    for label, runtime_map, member in required:
        expected_owner, priority, candidates, owner_error, owner_stem, selected_member, owner_member_list, resource_path = _select_resource_member(runtime_map, packagemapspec, shell_owner, label, member, map_member, owner_members)
        status, result = _resource_payload_evidence(resource_path, selected_member, payloads, label, runtime_map, expected_owner, priority, candidates, owner_stem, owner_member_list, owner_error)
        results.append(result)
        if status != "verified":
            warnings.append(f"{label}:{expected_owner}:{result['failure_reason']}")


def _required_linux_resource_specs(staged_mod: Path, packagemapspec: Path) -> tuple[list[dict[str, object]], list[str]]:
    with zipfile.ZipFile(staged_mod) as archive:
        members = sorted(archive.namelist())
        payloads = {name: archive.read(name) for name in members}
    owners = sorted({name.split("/", 1)[0] for name in members if "/" in name})
    owner_members = {
        owner: [name for name in members if name.startswith(owner + "/")]
        for owner in owners
    }

    def map_member(runtime_map: str, owner: str) -> str | None:
        marker = f"/maps/{runtime_map}.entities"
        candidates = [name for name in owner_members.get(owner, ()) if marker in f"/{name}"]
        return candidates[0] if candidates else None

    required: list[tuple[str, str, str | None]] = []
    for label, runtime_map in (('e1m1', 'game/sp/e1m1_intro/e1m1_intro'), ('hub', 'game/hub/hub')):
        required.append((label, runtime_map, None))

    shell_owner = _required_shell_resource(owners, owner_members, required)

    tag_request = _required_tag_resource(members, packagemapspec)
    if tag_request is not None:
        required.append(tag_request)

    results: list[dict[str, object]] = []
    warnings: list[str] = []
    _inspect_required_resources(required, packagemapspec, shell_owner, map_member, owner_members, payloads, results, warnings)
    return results, warnings


def _required_resource_output_issues(output: str, results: Sequence[Mapping[str, object]]) -> list[str]:
    names = {
        str(value).casefold()
        for result in results
        for value in (
            result.get('label'), result.get('expected_owner'), result.get('runtime_map'), result.get('staged_owner'),
        )
        if value
    }
    issues: list[str] = []
    for line in output.splitlines():
        lowered = line.casefold()
        warning = re.search(r"\bwarning\b", lowered) is not None
        skipped = re.search(r"\bskipped?\b", lowered) is not None
        if not (warning or skipped):
            continue
        if "resource" in lowered or any(name in lowered for name in names):
            issues.append(line.strip()[:500])
    return issues


@installation_step("verify")
def verify_linux_mod_installation(
    game_root: Path,
    staged_mod: Path,
    *,
    stdout: str = "",
    stderr: str = "",
) -> dict[str, object]:
    """Verify InjectorShell changed required owner resources in current game format."""
    root = validate_game_root(game_root)
    staged_input = staged_mod.expanduser()
    if staged_input.is_symlink():
        raise ValueError('staged room ZIP is a symbolic link')
    staged = staged_input.resolve()
    mods = (root / "Mods").resolve()
    if staged.parent != mods or not staged.is_file():
        raise ValueError('staged room ZIP is outside configured game Mods folder')
    if not zipfile.is_zipfile(staged):
        raise ValueError('staged room ZIP is invalid')
    packagemapspec = root / "base" / "packagemapspec.json"
    if not packagemapspec.is_file():
        raise FileNotFoundError('packagemapspec.json is missing from current game resources')
    results, missing = _required_linux_resource_specs(staged, packagemapspec)
    output_issues = _required_resource_output_issues(f'{stdout}\n{stderr}', results)
    if output_issues:
        raise LinuxVerificationError(
            'InjectorShell reported required resource warnings/skips: ' + ' | '.join(output_issues),
            {'required_resources': results, 'output_issues': output_issues},
        )
    if missing:
        raise LinuxVerificationError(
            'required resource evidence missing: ' + ', '.join(missing), {'required_resources': results},
        )
    return {
        'state': 'verified', 'mods_path': str(mods), 'packagemapspec_path': str(packagemapspec),
        'packagemapspec_sha256': hashlib.sha256(packagemapspec.read_bytes()).hexdigest(), 'required_resources': results,
    }


def _stage_mod(mod_zip: Path, game_root: Path) -> Path:
    require_doom_closed('staging a room mod')
    if not mod_zip.is_file() or not zipfile.is_zipfile(mod_zip):
        raise ValueError('mod is not a valid ZIP: {value_0}'.format(value_0=f'{mod_zip}'))
    mods = game_root / "Mods"
    mods.mkdir(parents=True, exist_ok=True)
    destination = mods / mod_zip.name
    incoming = destination.with_name(f".{destination.name}.incoming")
    shutil.copy2(mod_zip, incoming)
    publish_file(incoming, destination, operation="room_mod_stage")
    return destination


LEGACY_DOOM_AP_MOD_NAMES = frozenset(
    {
        'ap_mod.zip', 'doometernalarchipelagoalpha.zip', 'doometernalarchipelagobeta.zip',
        'doometernalarchipelagoprealpha.zip',
    }
)

LEGACY_DOOM_AP_VERSIONED_MOD = re.compile(
    'doometernalarchipelago-(?:[0-9a-f]{16}|v[0-9][0-9a-z._-]*)\\.zip', re.IGNORECASE,
)


def is_legacy_doom_ap_mod(path: Path) -> bool:
    """Recognize only historical or room-bound package names owned by this project."""
    name = path.name
    return (
        name.casefold() in LEGACY_DOOM_AP_MOD_NAMES
        or LEGACY_DOOM_AP_VERSIONED_MOD.fullmatch(name) is not None
    )


def stage_room_mod(
    mod_zip: Path,
    game_root: Path,
    ownership_receipt: Path,
    *,
    manifest_hash: str,
    legacy_removal_sink: Callable[[Path], None] | None = None,
    journal=None,
) -> Path:
    with installation_write(game_root, journal):
        return _stage_room_mod(mod_zip, game_root, ownership_receipt, manifest_hash=manifest_hash,
                               legacy_removal_sink=legacy_removal_sink, journal=journal)


def _stage_room_mod(
    mod_zip: Path,
    game_root: Path,
    ownership_receipt: Path,
    *,
    manifest_hash: str,
    legacy_removal_sink: Callable[[Path], None] | None = None,
    journal=None,
) -> Path:
    """Atomically replace only recognized DOOM Eternal Archipelago mod ZIPs."""
    require_doom_closed('staging a room mod')
    if not mod_zip.is_file() or not zipfile.is_zipfile(mod_zip):
        raise ValueError('mod is not a valid ZIP: {value_0}'.format(value_0=f'{mod_zip}'))
    mods = game_root / "Mods"
    mods.mkdir(parents=True, exist_ok=True)
    destination = mods / mod_zip.name
    candidates = room_mod_candidates(mods, destination, ownership_receipt)
    if journal is not None:
        return _stage_journaled_mod(mod_zip, destination, candidates, journal, legacy_removal_sink)
    return _replace_room_mod(mod_zip, destination, candidates, ownership_receipt, manifest_hash, legacy_removal_sink)


def room_mod_candidates(mods: Path, destination: Path, ownership_receipt: Path) -> list[Path]:
    previous = _read_mod_receipt(ownership_receipt)
    previous_path = Path(str(previous["staged_mod"])) if previous.get("staged_mod") else None
    previous_sha = str(previous.get("staged_sha256", ""))
    candidates = []
    for candidate in sorted(mods.glob("*.zip")):
        owned = _owned_room_mod(candidate, previous_path, previous_sha)
        if not (is_legacy_doom_ap_mod(candidate) or owned):
            continue
        if candidate.is_symlink():
            raise RuntimeError('Room mod is a symbolic link: {value_0}'.format(value_0=f'{candidate}'))
        candidates.append(candidate)
    if destination.exists() and destination not in candidates:
        raise RuntimeError('Refusing to overwrite an unowned room mod: {value_0}'.format(value_0=f'{destination}'))
    return candidates


def _read_mod_receipt(ownership_receipt):
    previous: dict[str, object] = {}
    if ownership_receipt.is_file():
        try:
            loaded = json.loads(ownership_receipt.read_text(encoding="utf-8"))
            previous = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            previous = {}

    return previous


def _owned_room_mod(candidate, previous_path, previous_sha):
    if previous_path is None or candidate.resolve() != previous_path.resolve():
        return False
    if hashlib.sha256(candidate.read_bytes()).hexdigest() != previous_sha:
        raise RuntimeError('Room mod ownership hash does not match: {value_0}'.format(value_0=f'{candidate}'))
    return True


def _stage_journaled_mod(source, destination, candidates, journal, removal_sink):
    content = source.read_bytes()
    for candidate in candidates:
        if candidate != destination:
            journal.expect(candidate, None)
    journal.expect(destination, hashlib.sha256(content).hexdigest())
    require_doom_closed('publishing a room mod')
    journal.replace(destination, content, operation="room_mod_stage")
    for candidate in candidates:
        if candidate != destination:
            journal.remove(candidate)
        if removal_sink is not None:
            removal_sink(candidate)
    return destination


def _replace_room_mod(mod_zip, destination, candidates, ownership_receipt, manifest_hash, legacy_removal_sink):
    mods = destination.parent
    expected_sha = hashlib.sha256(mod_zip.read_bytes()).hexdigest()

    with tempfile.NamedTemporaryFile(dir=mods, prefix=".room-mod-", suffix=".incoming", delete=False) as handle:
        incoming = Path(handle.name)
    backups = []
    published = False
    temporary = ownership_receipt.with_suffix(f"{ownership_receipt.suffix}.tmp")
    receipt = {
        'schema': 1, 'manifest_hash': manifest_hash, 'staged_mod': str(destination.resolve()),
        'staged_sha256': expected_sha,
    }
    try:
        shutil.copy2(mod_zip, incoming)
        if hashlib.sha256(incoming.read_bytes()).hexdigest() != expected_sha or not zipfile.is_zipfile(incoming):
            raise RuntimeError('Room mod copy verification failed')
        require_doom_closed('publishing a room mod')
        for candidate in candidates:
            backup = incoming.with_name(f"{incoming.name}.{candidate.name}.bak")
            publish_file(candidate, backup, operation="room_mod_backup")
            backups.append((candidate, backup))
        publish_file(incoming, destination, operation="room_mod_stage")
        published = True
        ownership_receipt.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        publish_file(temporary, ownership_receipt, operation="room_receipt_publish")
    except (OSError, RuntimeError, ValueError, KeyError, zipfile.BadZipFile, struct.error):
        if published and destination not in candidates:
            destination.unlink()
        for candidate, backup in reversed(backups):
            publish_file(backup, candidate, operation="room_mod_restore")
        raise
    finally:
        incoming.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
    _cleanup_mod_backups(backups, legacy_removal_sink)
    return destination


def _cleanup_mod_backups(backups, legacy_removal_sink):
    for candidate, backup in backups:
        try:
            backup.unlink()
        except OSError:
            pass  # retained backups stay outside the injector's ZIP scan
        if legacy_removal_sink is not None:
            legacy_removal_sink(candidate)


def configure_windows_injector_settings(game_root: Path, *, journal=None) -> Path:
    """Configure EternalModInjector Settings.txt to prevent automatic game launch while preserving existing settings."""
    settings_file = game_root / 'EternalModInjector Settings.txt'
    if not settings_file.is_file():
        _write_installer_settings(
            settings_file, '\n'.join((':ASSET_VERSION=2026-04-03', ':AUTO_LAUNCH_GAME=0', ':AUTO_UPDATE=0', '')),
            journal=journal,
        )
        return settings_file

    lines = settings_file.read_text(encoding="utf-8", errors="replace").splitlines()
    has_auto_launch = False
    has_auto_update = False
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(":AUTO_LAUNCH_GAME="):
            new_lines.append(':AUTO_LAUNCH_GAME=0')
            has_auto_launch = True
        elif stripped.startswith(":AUTO_UPDATE="):
            new_lines.append(':AUTO_UPDATE=0')
            has_auto_update = True
        else:
            new_lines.append(line)

    if not has_auto_launch:
        new_lines.append(':AUTO_LAUNCH_GAME=0')
    if not has_auto_update:
        new_lines.append(':AUTO_UPDATE=0')

    _write_installer_settings(settings_file, "\n".join(new_lines) + "\n", journal=journal)
    return settings_file


def _write_installer_settings(path, content, *, journal=None):
    if journal is None:
        _atomic_write_bytes(path, content.encode("utf-8"))
    else:
        journal.replace(path, content.encode("utf-8"), operation="injector_settings")


def stage_windows_injector_toolchain(
    dependency: InstalledDependency,
    game_root: Path,
    *,
    state_dir: Path | None = None,
    journal=None,
) -> Path:
    """Safely install and verify the complete Windows EternalModInjector toolchain into game root."""
    dep_root = Path(dependency.root)

    missing_members = [
        member for member in WINDOWS_INJECTOR_REQUIRED_MEMBERS
        if not (dep_root / member).is_file()
    ]
    if missing_members:
        raise RuntimeError(
            'Windows mod injector package is corrupted or incomplete: missing {value_0} file(s): {value_1}'.format(value_0=f'{len(missing_members)}', value_1=f"{', '.join(missing_members)}")
        )

    check_existing_injector_tools(dep_root, game_root, WINDOWS_INJECTOR_REQUIRED_MEMBERS)

    for rel_path in WINDOWS_INJECTOR_REQUIRED_MEMBERS:
        src = dep_root / rel_path
        dest = game_root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        src_bytes = src.read_bytes()
        src_sha = hashlib.sha256(src_bytes).hexdigest()
        _stage_windows_tool(dest, src_bytes, src_sha, rel_path, journal)

    (game_root / "Mods").mkdir(parents=True, exist_ok=True)
    configure_windows_injector_settings(game_root, journal=journal)

    batch_path = game_root / "EternalModInjector.bat"
    if not batch_path.is_file():
        raise RuntimeError('EternalModInjector.bat is missing after staging into game root.')
    return batch_path


class InjectorInstallationMismatch(RuntimeError):
    def __init__(self, relative):
        from .ui.strings import text

        super().__init__(text("launcher.setup", "injector_differs", path=str(relative)))
        self.details = {"failure_category": "injector_installation_mismatch", "injector_file": str(relative)}


def check_existing_injector_tools(source_root, game_root, members):
    for relative in members:
        destination = game_root / relative
        _check_injector_path(destination)
        if destination.exists() and (
            not destination.is_file()
            or DependencyManager._sha256(destination) != DependencyManager._sha256(source_root / relative)
        ):
            raise InjectorInstallationMismatch(relative)


def _stage_windows_tool(dest, src_bytes, src_sha, rel_path, journal):
    if dest.is_file():
        return
    if journal is not None:
        journal.replace(dest, src_bytes, operation="injector_stage")
        return

    incoming = dest.with_name(f".{dest.name}.incoming")
    try:
        incoming.write_bytes(src_bytes)
        if hashlib.sha256(incoming.read_bytes()).hexdigest() != src_sha:
            raise RuntimeError('staging validation failed for {value_0}'.format(value_0=f'{rel_path}'))
        publish_file(incoming, dest, operation="injector_stage")
    finally:
        if incoming.exists():
            try:
                incoming.unlink()
            except OSError:
                pass


SANDBOX_DIR_NAME = "doomSandBox"
SANDBOX_EXE_NAME = "DOOMSandBox64vk.exe"
SANDBOX_HOLD_SUFFIX = ".doom_eap_hold"
SANDBOX_TRANSACTION_FILE = "sandbox_guard_tx.json"


def _recover_sandbox_file(original, hold, tx_file, event_sink):
    if not original.exists() and hold.is_file():
        publish_file(hold, original, operation="sandbox_recover")
        recovered_size = original.stat().st_size
        recovered_sha = hashlib.sha256(original.read_bytes()).hexdigest()
        if tx_file and tx_file.is_file():
            try:
                tx_data = json.loads(tx_file.read_text(encoding="utf-8"))
                expected_sha = tx_data.get("sha256")
                if expected_sha and recovered_sha != expected_sha:
                    raise RuntimeError(
                        'Stale sandbox hold recovery integrity failure: expected SHA-256 {value_0}, got {value_1}'.format(value_0=f'{expected_sha}', value_1=f'{recovered_sha}')
                    )
            finally:
                tx_file.unlink(missing_ok=True)
        if event_sink is not None:
            event_sink(
                'injector_sandbox_guard', state='recovered_stale_hold', verified=True, sha256=recovered_sha,
                size=recovered_size,
            )


def _restore_sandbox_file(hold, original, tx_file, original_sha, original_size, event_sink):
    if hold.is_file():
        publish_file(hold, original, operation="sandbox_restore")
        restored_size = original.stat().st_size
        restored_sha = hashlib.sha256(original.read_bytes()).hexdigest()

        if tx_file and tx_file.is_file():
            tx_file.unlink(missing_ok=True)

        if restored_sha != original_sha or restored_size != original_size:
            raise RuntimeError(
                'Sandbox restoration integrity check failed! Expected SHA-256 {value_0} ({value_1} bytes), but restored file has {value_2} ({value_3} bytes).'.format(value_0=f'{original_sha}', value_1=f'{original_size}', value_2=f'{restored_sha}', value_3=f'{restored_size}')
            )

        if event_sink is not None:
            event_sink(
                'injector_sandbox_guard', state='restored', verified=True, sha256=restored_sha, size=restored_size,
            )


@contextmanager
def hold_sandbox(
    game_root: Path,
    *,
    state_dir: Path | None = None,
    event_sink: Callable[..., None] | None = None,
) -> Iterator[None]:
    """Transient compatibility guard around upstream EternalModInjector execution.

    Temporarily moves doomSandBox/DOOMSandBox64vk.exe out of the injector's view
    to prevent injector crashes caused by updated sandbox hashes, while guaranteeing
    the normal DOOMEternalx64vk.exe executable remains completely validated and
    the sandbox executable is restored byte-for-byte in all exit scenarios.
    """
    sandbox_dir = game_root / SANDBOX_DIR_NAME
    original = sandbox_dir / SANDBOX_EXE_NAME
    hold = sandbox_dir / (SANDBOX_EXE_NAME + SANDBOX_HOLD_SUFFIX)
    tx_file = (state_dir / SANDBOX_TRANSACTION_FILE) if state_dir is not None else None

    _recover_sandbox_file(original, hold, tx_file, event_sink)

    if original.exists() and hold.exists():
        raise RuntimeError(
            "Ambiguous sandbox state: both '{value_0}' and hold file '{value_1}' exist. Manual intervention is required to avoid data loss. Neither file was modified.".format(value_0=f'{original}', value_1=f'{hold}')
        )

    # upstream skips sandbox patching when the executable is absent
    if not original.is_file():
        yield
        return

    original_size = original.stat().st_size
    original_sha = hashlib.sha256(original.read_bytes()).hexdigest()

    if tx_file is not None:
        tx_file.parent.mkdir(parents=True, exist_ok=True)
        tx_data = {
            'original': str(original), 'hold': str(hold), 'sha256': original_sha, 'size': original_size,
            'timestamp': datetime.now().isoformat(),
        }
        tx_file.write_text(json.dumps(tx_data, indent=2) + "\n", encoding="utf-8")

    publish_file(original, hold, operation="sandbox_hold")
    if event_sink is not None:
        event_sink('injector_sandbox_guard', state='held', sha256=original_sha, size=original_size)

    try:
        yield
    finally:
        _restore_sandbox_file(hold, original, tx_file, original_sha, original_size, event_sink)


def recover_stale_sandbox_hold(
    game_root: Path,
    *,
    state_dir: Path | None = None,
    event_sink: Callable[..., None] | None = None,
) -> None:
    sandbox_dir = game_root / SANDBOX_DIR_NAME
    original = sandbox_dir / SANDBOX_EXE_NAME
    hold = sandbox_dir / (SANDBOX_EXE_NAME + SANDBOX_HOLD_SUFFIX)
    tx_file = (state_dir / SANDBOX_TRANSACTION_FILE) if state_dir is not None else None
    if original.exists() and hold.exists():
        raise RuntimeError(
            "Ambiguous sandbox state: both '{value_0}' and hold file '{value_1}' exist. Manual intervention is required to avoid data loss. Neither file was modified.".format(value_0=f'{original}', value_1=f'{hold}')
        )
    if original.exists() or not hold.is_file():
        return
    _recover_sandbox_file(original, hold, tx_file, event_sink)


def finalize_mod_installation(game_root, staged, result, *, event_sink=None, journal=None):
    try:
        from .campaign_resources import enable_campaign_resources
        resources = enable_campaign_resources(game_root, staged, journal=journal)
    except (OSError, RuntimeError, ValueError, KeyError, zipfile.BadZipFile, struct.error) as error:
        return _installation_failure(result, error, 'prepare banks', event_sink, game_root, staged)
    return AdapterResult(state="applied", message=result.message, command=result.command,
        returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
        details={**result.details, "campaign_resources": resources})


def verify_mod_installation(game_root, staged, result, *, platform_name, event_sink=None):
    try:
        from .campaign_resources import installed_resource_fingerprints
        from .native_enemy_resources import verify_native_resource_banks
        verify_native_resource_banks(game_root / 'base', result.details.get('campaign_resources', {}).get('banks', {}))
        verification = verify_linux_mod_installation(game_root, staged, stdout=result.stdout, stderr=result.stderr)
        verification["installed_resources"] = installed_resource_fingerprints(game_root)
    except (OSError, RuntimeError, ValueError, KeyError, zipfile.BadZipFile, struct.error) as error:
        return _installation_failure(result, error, "verify", event_sink, game_root, staged)
    return AdapterResult(state="applied", message=result.message, command=result.command,
        returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
        details={**result.details, **verification, "post_install_verification": verification["state"]})
