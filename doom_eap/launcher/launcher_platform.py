"""Verified dependencies and platform launch adapters for beta launcher."""

from __future__ import annotations

import csv
import difflib
import hashlib
import http.client
import json
import os
import re
import shlex
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
import webbrowser
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import ClassVar, Protocol

from doom_eap.launcher.ui.strings import text as player_text

DOOM_ETERNAL_APP_ID = "782330"
REQUIRED_DLL_OVERRIDE = "msimg32=n,b"
STEAM_GAME_URL = f"steam://rungameid/{DOOM_ETERNAL_APP_ID}"
WINDOWS_MANUAL_INSTALL_GUIDE_URL = (
    "https://github.com/snowzzrra/DoomEternal-AP-Mod/blob/main/docs/INSTALL.md#windows-manual-installation"
)


def restore_standard_streams(*, require_pipes=False):
    if sys.platform != 'win32':
        return
    import ctypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetStdHandle.argtypes = [ctypes.c_ulong]
    kernel.GetStdHandle.restype = ctypes.c_void_p
    # windowed builds keep inherited pipes but leave Python's streams unset
    for name, number, mode, flags in (('stdin', -10, 'r', os.O_RDONLY),
                                     ('stdout', -11, 'w', os.O_WRONLY),
                                     ('stderr', -12, 'w', os.O_WRONLY)):
        if getattr(sys, name) is not None:
            continue
        handle = kernel.GetStdHandle(number & 0xffffffff)
        if not handle or handle == ctypes.c_void_p(-1).value:
            if require_pipes:
                raise RuntimeError(player_text('launcher.platform', 'the_session_supervisor_requires_an_inherited_value_0_pipe', value_0=f'{name}'))
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


@dataclass(frozen=True)
class LauncherUserPaths:
    """Per-user launcher roots for configuration, state, cache, and logs."""

    config_dir: Path
    state_dir: Path
    data_dir: Path


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

    def fetch(self, url: str, destination: Path, *, headers: dict[str, str] | None = None) -> DownloadResult:
        total_attempts = self.max_retries + 1
        for attempt in range(1, total_attempts + 1):
            try:
                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": "DoomEternal-AP-Launcher", **(headers or {})},
                )
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout,
                    context=self.ssl_context,
                ) as response, destination.open("wb") as output:
                    shutil.copyfileobj(response, output)
                    headers = getattr(response, "headers", None)
                    raw_length = headers.get("Content-Length") if headers is not None and hasattr(headers, "get") else None
                    try:
                        content_length = int(raw_length) if raw_length is not None else None
                    except ValueError:
                        content_length = None
                    final_url = response.geturl() if hasattr(response, "geturl") else url
                    return DownloadResult(final_url, content_length, dict(response.headers))
            except Exception as error:
                if destination.exists():
                    try:
                        destination.unlink()
                    except OSError:
                        pass
                if attempt < total_attempts and is_transient_download_error(error):
                    time.sleep(self.backoff_base * (2 ** (attempt - 1)))
                    continue
                raise
        raise RuntimeError(player_text('launcher.platform', 'download_attempts_exhausted'))


def launcher_user_paths(
    *,
    environment: dict[str, str] | None = None,
    platform_name: str | None = None,
) -> LauncherUserPaths:
    env = os.environ if environment is None else environment
    platform_name = platform_name or ("windows" if os.name == "nt" else "linux")
    if platform_name == "windows":
        root = Path(env.get("LOCALAPPDATA") or (Path.home() / "AppData/Local"))
        root = root / player_text('launcher.platform', 'doom_eternal_archipelago')
        return LauncherUserPaths(root / "config", root / "state", root / "data")
    root_name = "doom-eternal-archipelago"
    return LauncherUserPaths(
        Path(env.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / root_name,
        Path(env.get("XDG_STATE_HOME") or (Path.home() / ".local/state")) / root_name,
        Path(env.get("XDG_DATA_HOME") or (Path.home() / ".local/share")) / root_name,
    )


def migrate_legacy_launcher_data(legacy_dir: Path, paths: LauncherUserPaths) -> None:
    """Copy legacy launcher data into user roots without deleting source files."""
    if not legacy_dir.is_dir():
        return
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted(legacy_dir.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(legacy_dir)
        destination_root = paths.config_dir if relative.name == "launcher.json" and len(relative.parts) == 1 else paths.state_dir
        destination = destination_root / relative
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative.name == "launcher.json" and len(relative.parts) == 1:
            try:
                document = json.loads(source.read_text(encoding="utf-8"))
                if isinstance(document, dict):
                    document.pop("password", None)
                    destination.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                    continue
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        shutil.copy2(source, destination)


class DiscoveryStatus(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    INVALID = "invalid"


@dataclass(frozen=True)
class DiscoverySentinel:
    status: str
    path: str = ""
    candidates: tuple[str, ...] = ()
    reason: str = ""
    trace: tuple[dict[str, object], ...] = ()


def validate_game_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    if not (root / "DOOMEternalx64vk.exe").is_file() or not (root / "base").is_dir():
        raise ValueError(player_text('launcher.doctor', 'choose_the_folder_containing_doometernalx64vk_exe'))
    return root


def validate_save_directory(path: Path) -> Path:
    root = path.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(player_text('launcher.platform', 'choose_an_existing_save_folder_ending_in_id_software_doometernal'))
    if not is_saved_games_base_shape(root):
        raise ValueError(
            player_text('launcher.platform', 'choose_an_existing_save_folder_ending_in_id_software_doometernal')
        )
    return root


SAVED_GAMES_PROVIDER = "id Software"
SAVED_GAMES_TITLE = "DOOMEternal"
SAVED_GAMES_LEAF = "base"
SAVED_GAMES_LEAF_CHAIN = (SAVED_GAMES_PROVIDER, SAVED_GAMES_TITLE, SAVED_GAMES_LEAF)
FOLDERID_SAVED_GAMES = '{4C5C32FF-BB9D-43B9-B382-325F2A56FE60}'
LIVE_WRITER_FRESHNESS_SECONDS = 24.0 * 3600.0
SAVED_GAMES_MARKER_PREFIXES = ("ap_active_map", "ap_event_", "ap_telemetry")


def _saved_games_norm(path: Path | str, *, platform: str) -> str:
    text = os.path.abspath(os.path.expanduser(str(path)))
    if platform == "nt":
        text = os.path.normcase(text)
    return text.rstrip("\\/")


def _is_within_directory(child: Path | str, parent: Path | str, *, platform: str) -> bool:
    child_text = _saved_games_norm(child, platform=platform)
    parent_text = _saved_games_norm(parent, platform=platform)
    if child_text == parent_text:
        return True
    return child_text.startswith(parent_text + ("\\" if platform == "nt" else "/"))


def is_saved_games_base_shape(path: Path | str, *, platform: str | None = None) -> bool:
    """Check the .../id Software/DOOMEternal/base leaf chain without touching disk."""
    target = platform if platform is not None else os.name
    parts = [part for part in Path(str(path)).expanduser().parts if part not in ("", "/", "\\")]
    chain = parts[-3:] if len(parts) >= 3 else []
    if target == "nt":
        chain = [part.casefold() for part in chain]
        expected = [part.casefold() for part in SAVED_GAMES_LEAF_CHAIN]
    else:
        expected = list(SAVED_GAMES_LEAF_CHAIN)
    return chain == expected


def saved_games_shape_verdict(path: Path | str, *, platform: str | None = None) -> str:
    """Classify a configured Saved Games path without side effects."""
    target = platform if platform is not None else os.name
    try:
        expanded = Path(str(path)).expanduser()
    except (OSError, TypeError, ValueError, RuntimeError):
        return "unparseable"
    if not _saved_games_norm(expanded, platform=target):
        return "unparseable"
    try:
        if not expanded.is_dir():
            return "not_a_directory" if expanded.exists() else "missing"
    except (OSError, ValueError, RuntimeError):
        return "inaccessible"
    if not is_saved_games_base_shape(expanded, platform=target):
        return "invalid_saved_games_shape"
    return "valid_saved_games_base"


def known_folder_saved_games(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Return FOLDERID_SavedGames on Windows; None elsewhere or on failure."""
    target = platform if platform is not None else os.name
    env = environ if environ is not None else os.environ
    override = env.get("DOOMEAP_SAVED_GAMES")
    if override:
        candidate = Path(override).expanduser()
        return candidate
    if target != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]

        class _GUID(ctypes.Structure):
            _fields_: ClassVar = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8),
            ]

        guid = _GUID()
        ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
        if ole32.CLSIDFromString(FOLDERID_SAVED_GAMES, ctypes.byref(guid)) != 0:
            return None
        buffer = wintypes.LPWSTR()
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(buffer)) != 0:
            return None
        try:
            value = buffer.value
        finally:
            ole32.CoTaskMemFree(buffer)
        if not value:
            return None
        return Path(value)
    except (OSError, AttributeError, ValueError):
        return None


def doom_saved_games_base(
    *,
    platform: str | None = None,
    home: Path | str | None = None,
    known_folder: Path | str | None = None,
    game_root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Return the canonical Saved Games base without probing arbitrary directories."""
    target = platform if platform is not None else os.name
    if target == "nt":
        folder = Path(known_folder).expanduser() if known_folder is not None else known_folder_saved_games(platform=target, environ=environ)
        if folder is None:
            base_home = Path(home).expanduser() if home is not None else Path.home()
            folder = base_home / 'Saved Games'
        return folder / SAVED_GAMES_PROVIDER / SAVED_GAMES_TITLE / SAVED_GAMES_LEAF
    if game_root is None:
        return None
    try:
        root = Path(str(game_root)).expanduser()
    except (OSError, TypeError, ValueError, RuntimeError):
        return None
    for parent in (root, *root.parents):
        if parent.name.casefold() == "steamapps":
            return (
                parent.parent
                / player_text('launcher.platform', 'steamapps_compatdata_782330_pfx_drive_c_users_steamuser_saved_ga')
                / SAVED_GAMES_PROVIDER
                / SAVED_GAMES_TITLE
                / SAVED_GAMES_LEAF
            )
    return None


def saved_games_live_markers(
    directory: Path | str,
    *,
    now: float | None = None,
    freshness_seconds: float = LIVE_WRITER_FRESHNESS_SECONDS,
) -> tuple[str, ...]:
    """Return fresh AP writer marker names; never reads file contents or sizes."""
    try:
        root = Path(str(directory)).expanduser()
        if not root.is_dir():
            return ()
    except (OSError, TypeError, ValueError, RuntimeError):
        return ()
    cutoff = (now if now is not None else time.time()) - max(0.0, freshness_seconds)
    live: list[str] = []
    try:
        names = sorted(path.name for path in root.iterdir() if path.is_file())
    except (OSError, ValueError, RuntimeError):
        return ()
    for name in names:
        if not name.startswith(SAVED_GAMES_MARKER_PREFIXES):
            continue
        try:
            if (root / name).stat().st_mtime >= cutoff:
                live.append(name)
        except (OSError, ValueError, RuntimeError):
            continue
        if len(live) >= 24:
            break
    return tuple(live)


@dataclass(frozen=True)
class SavedGamesSelection:
    path: Path | None
    source: str
    reason: str
    repaired: bool = False
    previous_path: str | None = None
    rejected: tuple[dict[str, object], ...] = ()
    live_evidence: tuple[str, ...] = ()


def select_saved_games_dir(
    configured: Path | str | None,
    *,
    candidates: Sequence[Path | str] = (),
    known_base: Path | str | None = None,
    app_dir: Path | str | None = None,
    client_dir: Path | str | None = None,
    game_root: Path | str | None = None,
    platform: str | None = None,
    now: float | None = None,
    game_running: bool = False,
) -> SavedGamesSelection:
    """Deterministically resolve the single effective Saved Games base.

    A configured path with valid .../id Software/DOOMEternal/base shape is
    always preserved. Anything else (missing, wrong shape, or inside the
    launcher application/client directory or the game installation) is never
    used as authority; the canonical Known-Folder/Proton base wins, then a
    single live-writer candidate, otherwise selection fails closed.
    """
    target = platform if platform is not None else os.name
    forbidden = [
        str(scope)
        for scope in (app_dir, client_dir, game_root)
        if scope is not None and str(scope)
    ]
    rejected: list[dict[str, object]] = []

    def _reject(candidate: object, reason: str) -> None:
        rejected.append({"path": str(candidate), "reason": reason})

    configured_text = str(configured) if configured is not None and str(configured) else None
    configured_path = Path(configured_text).expanduser() if configured_text else None
    configured_verdict = saved_games_shape_verdict(configured_path, platform=target) if configured_path else "unconfigured"
    configured_forbidden = bool(
        configured_path is not None
        and any(_is_within_directory(configured_path, scope, platform=target) for scope in forbidden)
    )
    if configured_forbidden and configured_path is not None:
        if str(app_dir) and _is_within_directory(configured_path, str(app_dir), platform=target):
            configured_verdict = "application_directory_not_saved_games"
        elif str(client_dir) and _is_within_directory(configured_path, str(client_dir), platform=target):
            configured_verdict = "application_directory_not_saved_games"
        else:
            configured_verdict = "game_installation_not_saved_games"
    if configured_text is not None:
        _reject(configured_text, configured_verdict if configured_verdict != "valid_saved_games_base" or configured_forbidden else "configured")
    configured_usable = (
        configured_path is not None
        and configured_verdict == "valid_saved_games_base"
        and not configured_forbidden
    )
    try:
        configured_exists = bool(configured_path is not None and configured_path.is_dir())
    except (OSError, ValueError, RuntimeError):
        configured_exists = False
    if configured_usable and configured_exists and configured_path is not None:
        return SavedGamesSelection(
            path=configured_path,
            source="configured_saved_games_dir",
            reason=player_text('launcher.platform', 'configured_saved_games_base_has_valid_shape_and_exists'),
            rejected=tuple(rejected),
        )

    ordered: list[Path] = []
    seen: set[str] = set()
    for raw in ((known_base,) if known_base is not None else ()) + tuple(candidates):
        if raw is None or not str(raw):
            continue
        try:
            candidate = Path(str(raw)).expanduser()
        except (OSError, TypeError, ValueError, RuntimeError):
            _reject(raw, "unparseable")
            continue
        key = _saved_games_norm(candidate, platform=target)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(candidate)

    viable: list[Path] = []
    live: dict[str, tuple[str, ...]] = {}
    for candidate in ordered:
        verdict = saved_games_shape_verdict(candidate, platform=target)
        if verdict != "valid_saved_games_base":
            _reject(str(candidate), verdict)
            continue
        if any(_is_within_directory(candidate, scope, platform=target) for scope in forbidden):
            _reject(str(candidate), "application_directory_not_saved_games")
            continue
        viable.append(candidate)
        markers = saved_games_live_markers(candidate, now=now)
        if markers:
            live[_saved_games_norm(candidate, platform=target)] = markers

    known_key = _saved_games_norm(Path(str(known_base)).expanduser(), platform=target) if known_base else None
    known_viable = next((item for item in viable if _saved_games_norm(item, platform=target) == known_key), None) if known_key else None
    if known_viable is not None:
        return SavedGamesSelection(
            path=known_viable,
            source="canonical_saved_games_base",
            reason=player_text('launcher.platform', 'canonical_saved_games_base_selected_over_invalid_configured_path'),
            repaired=configured_text is not None,
            previous_path=configured_text,
            rejected=tuple(rejected),
            live_evidence=live.get(_saved_games_norm(known_viable, platform=target), ()),
        )
    live_viable = [item for item in viable if _saved_games_norm(item, platform=target) in live]
    if len(live_viable) == 1 and (len(viable) == 1 or game_running):
        only = live_viable[0]
        return SavedGamesSelection(
            path=only,
            source="live_writer_evidence",
            reason=player_text('launcher.platform', 'single_live_ap_writer_candidate_selected_over_invalid_configured'),
            repaired=configured_text is not None,
            previous_path=configured_text,
            rejected=tuple(rejected),
            live_evidence=live.get(_saved_games_norm(only, platform=target), ()),
        )
    if len(viable) == 1 and configured_text is None:
        only = viable[0]
        return SavedGamesSelection(
            path=only,
            source="single_viable_candidate",
            reason=player_text('launcher.platform', 'no_configured_path_single_viable_candidate_selected'),
            rejected=tuple(rejected),
            live_evidence=live.get(_saved_games_norm(only, platform=target), ()),
        )
    if not viable:
        reason = "no_marker_evidence" if configured_text is None else "stale_config_no_viable_candidate"
        if len(rejected) > 1 and configured_text is not None:
            reason = "ambiguous" if any(str(item.get("path")) != configured_text for item in rejected) else reason
        return SavedGamesSelection(path=None, source="unresolved", reason=reason, rejected=tuple(rejected))
    return SavedGamesSelection(path=None, source="unresolved", reason="ambiguous", rejected=tuple(rejected))


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
        return all(
            check.ok
            for check in self.checks
            if check.key in mandatory_keys
        )

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
        return PrerequisiteCheck("meathook", PrerequisiteStatus.MISSING, player_text('launcher.platform', 'choose_the_game_folder'))
    try:
        root = validate_game_root(game_root)
        value = read_manifest(root / "sentinel-distribution.json")
        records = {item["path"]: item for item in value["artifacts"]}
        for name in ("sentinel_core.dll", "msimg32.dll"):
            data = (root / name).read_bytes()
            if len(data) != records[name]["size"] or hashlib.sha256(data).hexdigest() != records[name]["sha256"]:
                raise ValueError(player_text('launcher.platform', 'game_connection_file_value_0_is_damaged_open_help_and_select_rep', value_0=f'{name}'))
            verify_build_identity(name, data, value["build_id"])
        if (root / "XINPUT1_3.dll").exists():
            raise ValueError(player_text('launcher.platform', 'xinput1_3_dll_conflicts_with_doomeap_uninstall_the_mod_that_inst'))
        return PrerequisiteCheck("meathook", PrerequisiteStatus.OK, player_text('launcher.platform', 'game_connection_files_checked'),
                                 {"path": str(root / "sentinel_core.dll"), "sha256": records["sentinel_core.dll"]["sha256"],
                                  "version": value["version"], "build_id": value["build_id"]})
    except (OSError, ValueError, KeyError, TypeError):
        return PrerequisiteCheck("meathook", PrerequisiteStatus.MISSING, player_text('launcher.platform', 'game_connection_files_are_unavailable_open_help_and_select_fix_s'))



def probe_runtime_prerequisites(
    game_root: Path | None,
    client_dir: Path | None = None,
    config: Mapping[str, object] | None = None,
) -> RuntimePrerequisiteReport:
    """Probe the game, Core and client prerequisites."""
    checks: list[PrerequisiteCheck] = []

    # game installation check
    if game_root is None:
        checks.append(PrerequisiteCheck(
            key="game",
            status=PrerequisiteStatus.MISSING,
            message=player_text('launcher.platform', 'doom_eternal_installation_is_not_configured'),
            details={},
        ))
    else:
        try:
            validated_root = validate_game_root(game_root)
            checks.append(PrerequisiteCheck(
                key="game",
                status=PrerequisiteStatus.OK,
                message=player_text('launcher.platform', 'doom_eternal_installation_validated'),
                details={"path": str(validated_root)},
            ))
        except ValueError as error:
            checks.append(PrerequisiteCheck(
                key="game",
                status=PrerequisiteStatus.INVALID,
                message=str(error),
                details={"path": str(game_root)},
            ))

    # check the installed core pair
    checks.append(probe_meathook(game_root))

    # client runtime check
    if client_dir is not None:
        packaged_bridge = client_dir / "bridge_client.py"
        if packaged_bridge.is_file() or getattr(sys, "frozen", False):
            checks.append(PrerequisiteCheck(
                key="client_runtime",
                status=PrerequisiteStatus.OK,
                message=player_text('launcher.platform', 'bundled_client_runtime_validated'),
                details={"path": str(client_dir)},
            ))
        else:
            checks.append(PrerequisiteCheck(
                key="client_runtime",
                status=PrerequisiteStatus.MISSING,
                message=player_text('launcher.platform', 'client_runtime_bridge_is_missing_value_0', value_0=f'{packaged_bridge}'),
                details={"expected_path": str(packaged_bridge)},
            ))

    # linux steam launch options override check
    if os.name != "nt" and config is not None:
        launch_opts = str(config.get("steam_launch_options") or "")
        if REQUIRED_DLL_OVERRIDE in launch_opts:
            checks.append(PrerequisiteCheck(
                key="linux_steam_override",
                status=PrerequisiteStatus.OK,
                message=player_text('launcher.platform', 'steam_launch_option_configured_with_msimg32_override'),
                details={"configured": True},
            ))
        else:
            checks.append(PrerequisiteCheck(
                key="linux_steam_override",
                status=PrerequisiteStatus.NEEDS_USER_ACTION,
                message=player_text('launcher.platform', 'set_steam_launch_option_winedlloverrides_value_0_command', value_0=f'{REQUIRED_DLL_OVERRIDE}'),
                details={"required_override": REQUIRED_DLL_OVERRIDE, "configured": False},
            ))

    return RuntimePrerequisiteReport(tuple(checks))


ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33
_WINDOWS_TRANSIENT_WINERRORS = frozenset({ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION})
_WINDOWS_MUTEX_TIMEOUT_MS = 30000
_WINDOWS_PUBLISH_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.0)


class FilePublicationError(OSError):
    """Rich failure for launcher-owned temp/incoming file publication.

    Carries operation, source, destination, and attempt count alongside the
    underlying errno/winerror/filename evidence. Never includes secrets.
    """

    def __init__(
        self,
        operation: object,
        source: Path | str,
        destination: Path | str,
        attempts: int,
        *,
        cause: BaseException | None = None,
    ):
        self.operation = str(operation)
        self.source_path = str(source)
        self.destination_path = str(destination)
        self.attempt_count = int(attempts)
        self.cause_errno = getattr(cause, "errno", None) if cause is not None else None
        self.cause_winerror = getattr(cause, "winerror", None) if cause is not None else None
        self.cause_filename = getattr(cause, "filename", None) if cause is not None else None
        self.cause_filename2 = getattr(cause, "filename2", None) if cause is not None else None
        detail = f"{type(cause).__name__}: {cause}" if cause is not None else player_text('launcher.platform', 'unknown_error')
        message = (
            player_text('launcher.platform', 'windows_file_publication_failed_operation_value_0_source_value_1', value_0=f'{self.operation}', value_1=f'{self.source_path}', value_2=f'{self.destination_path}', value_3=f'{self.cause_errno}', value_4=f'{self.cause_winerror}', value_5=f'{self.attempt_count}', value_6=f'{detail}')
        )
        super().__init__(message)
        # NOTE: OSError core fields (errno/filename) are deliberately left
        # unset: assigning them post-hoc makes OSError.__str__ discard the
        # rich message above and reformat as "[Errno N] None: ...". Filesystem
        # evidence stays available via the cause_* attributes instead.
        if cause is not None:
            self.__cause__ = cause


def _is_transient_windows_publish_error(error: OSError) -> bool:
    """Classify Windows sharing/access failures safe for bounded publication retry."""
    if getattr(error, "winerror", None) in _WINDOWS_TRANSIENT_WINERRORS:
        return True
    return isinstance(error, PermissionError) and getattr(error, "errno", None) == 13


def publish_file(
    source: Path | str,
    destination: Path | str,
    *,
    operation: str,
    timeout: float = 3.0,
) -> Path:
    """Publish a launcher-owned temp/incoming file to its final destination.

    Publication uses same-filesystem atomic os.replace. Linux uses one attempt.
    On Windows, transient
    sharing/access failures (winerror 5/32/33, errno 13) are retried with
    bounded backoff totaling at most ~timeout seconds. Only use where the
    launcher owns source and publication and retrying the replace is safe.
    """
    source_path = Path(source)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    attempts = 0
    delays = _WINDOWS_PUBLISH_RETRY_DELAYS if os.name == "nt" else ()
    deadline = time.monotonic() + max(0.0, timeout)
    delay_index = 0
    while True:
        attempts += 1
        try:
            os.replace(source_path, destination_path)
            return destination_path
        except OSError as error:
            retryable = os.name == "nt" and _is_transient_windows_publish_error(error)
            wait = delays[min(delay_index, len(delays) - 1)] if retryable and delays else 0.0
            delay_index += 1
            if not retryable or time.monotonic() + wait > deadline:
                raise FilePublicationError(operation, source_path, destination_path, attempts, cause=error) from error
            time.sleep(wait)


def _dependency_mutex_name(root: Path | str, cache_key: str) -> str:
    """Derive the Local\\ session mutex name serializing one dependency cache entry."""
    identity = f"{Path(root).resolve()}|{cache_key}"
    return "Local\\DoomEAP.DependencyCache." + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _acquire_windows_mutex(name: str, timeout_ms: int = _WINDOWS_MUTEX_TIMEOUT_MS) -> tuple[int, bool]:
    """Acquire a named Windows mutex. Returns (handle, abandoned)."""
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool

    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        code = kernel32.GetLastError()
        error = OSError(player_text('launcher.platform', 'windows_dependency_cache_lock_creation_failed_name_value_0_winer', value_0=f'{name}', value_1=f'{code}'))
        error.winerror = code  # type: ignore[attr-defined]
        raise error
    result = kernel32.WaitForSingleObject(handle, timeout_ms)
    if result == 0x0:
        return int(handle), False
    if result == 0x80:
        return int(handle), True
    kernel32.CloseHandle(handle)
    if result == 0x102:
        raise TimeoutError(
            player_text('launcher.platform', 'windows_dependency_cache_lock_timeout_name_value_0_timeout_ms_va', value_0=f'{name}', value_1=f'{timeout_ms}')
        )
    code = kernel32.GetLastError()
    error = OSError(player_text('launcher.platform', 'windows_dependency_cache_lock_wait_failed_name_value_0_winerror', value_0=f'{name}', value_1=f'{code}'))
    error.winerror = code  # type: ignore[attr-defined]
    raise error


def _release_windows_mutex(handle: int) -> None:
    """Release a named Windows mutex handle. CloseHandle always runs."""
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel32.ReleaseMutex.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    try:
        kernel32.ReleaseMutex(ctypes.c_void_p(handle))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


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
    "EternalModInjector.bat",
    "EternalModManager.exe",
    "base/BlangParser.dll",
    "base/DEternal_loadMods.exe",
    "base/DEternal_patchManifest.exe",
    "base/EternalPatcher.def",
    "base/EternalPatcher.exe",
    "base/EternalPatcher.exe.config",
    "base/Newtonsoft.Json.dll",
    "base/idRehash.exe",
    "base/opusdec.exe",
    "base/opusenc.exe",
    "base/rs_data",
    "base/zlib64.dll",
)


WINDOWS_MOD_INJECTOR = DependencySpec(
    name="EternalModInjector",
    version="2026-09-17",
    url="https://gamebanana.com/dl/1819101",
    sha256="567fddbae0c6298b9cfecf6ac7e2b129ec109c53f1b09691aea1fb75f08a0f41",
    executable_glob="**/EternalModInjector.bat",
    archive_type="zip",
    expected_size=5182727,
)

# compatibility alias
WINDOWS_MOD_MANAGER = WINDOWS_MOD_INJECTOR

LINUX_MOD_INJECTOR = DependencySpec(
    name="EternalModInjectorShell",
    version="6.66-rev3.16",
    url=("https://github.com/leveste/EternalBasher/releases/download/v6.66-rev3.16/EternalModInjectorShell.tar.gz"),
    sha256="f891336c7481d5be4fe51e20a4ca7f40a6b9d3b9ef00948859b4b3c28523a9bf",
    executable_glob="**/EternalModInjectorShell.sh",
    archive_type="tar.gz",
    expected_size=16754097,
)


IDFILE_DECOMPRESSOR_LINUX = DependencySpec(
    name="idFileDeCompressor",
    version="v1.0.1",
    url="https://github.com/brunoanc/idFileDeCompressorGo/releases/download/v1.0.1/idFileDeCompressor",
    sha256="8bd14670fdb5a47533c5a59e9b6e1e32eaa3f06ce8b7ad2b4a8df520348b9ab8",
    executable_glob="idFileDeCompressor",
    archive_type="file",
    commit="67cd82781edd0595b0513707907ab63cdaa2691a",
    platforms=frozenset({"linux"}),
    expected_size=946404,
)

IDFILE_DECOMPRESSOR_WINDOWS = DependencySpec(
    name="idFileDeCompressor",
    version="v1.0.1",
    url="https://github.com/brunoanc/idFileDeCompressorGo/releases/download/v1.0.1/idFileDeCompressor.exe",
    sha256="02488dfb2999542cee4b7c0713ec03dd534ddd876169aac43b0217b362427a06",
    executable_glob="idFileDeCompressor.exe",
    archive_type="file",
    commit="67cd82781edd0595b0513707907ab63cdaa2691a",
    platforms=frozenset({"windows"}),
    expected_size=877056,
)


def idfile_decompressor_spec(platform_name: str | None = None) -> DependencySpec | None:
    selected = (platform_name or (
        "windows" if os.name == "nt" else "linux" if sys.platform.startswith("linux") else sys.platform
    )).casefold()
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


def _core_file_error(path: Path, error: Exception) -> RuntimeError:
    message = player_text('launcher.platform', 'core_runtime_file_could_not_be_read_or_installed_value_0_value_1', value_0=f'{path}', value_1=f'{error}')
    if sys.platform != "win32":
        return RuntimeError(message)
    script = ''.join((player_text('launcher.platform', 'erroractionpreference_stop'), '\n', player_text('launcher.platform', 'console_outputencoding_system_text_utf8encoding_new'), '\n', "Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Windows Defender/Operational'; Id=1116,1117,5007; StartTime=(Get-Date).AddDays(-7)} -MaxEvents 200 | ForEach-Object {", '\n', player_text('launcher.platform', 'xml_xml_toxml_data'), '\n', player_text('launcher.platform', 'foreach_item_in_xml_event_eventdata_data_data_item_name_item_tex'), '\n', player_text('launcher.platform', 'time_timecreated_touniversaltime_tostring_o_id_id_data_data'), '\n', player_text('launcher.platform', 'convertto_json_depth_4_compress'), '\n', ''))
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, encoding="utf-8", errors="replace", timeout=5,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise ValueError(player_text('launcher.platform', 'defender_history_query_failed'))
        events = json.loads(result.stdout)
        if isinstance(events, dict):
            events = [events]
        candidates = {str(candidate).casefold() for candidate in
                      (path, getattr(error, "filename", None), getattr(error, "cause_filename", None),
                       getattr(error, "source_path", None)) if candidate}
        for event in events:
            data = event["data"]
            resources = {resource.strip().removeprefix("file:_").removeprefix("file:").casefold()
                         for resource in data.get("Path", "").split(";")}
            if event["id"] not in (1116, 1117) or not resources & candidates:
                continue
            threat_id = data.get("Threat ID", "")
            setting = next((record for record in events if record["id"] == 5007 and threat_id and
                            f"\\ThreatIDDefaultAction\\{threat_id} =" in record["data"].get("New Value", "")), None)
            allowed = setting and setting["data"]["New Value"].endswith("= 0x6")
            detail = (player_text('launcher.platform', 'an_allow_action_is_recorded_at_value_0_retry_setup_to_restore_th', value_0=f"{setting['time']}")
                      if allowed else player_text('launcher.platform', 'review_this_detection_in_windows_security_protection_history_bef'))
            return RuntimeError(player_text('launcher.platform', 'value_0_defender_detected_value_1_at_value_2_value_3', value_0=f'{message}', value_1=f"{data.get('Threat Name', 'this file')}", value_2=f"{event['time']}", value_3=f'{detail}'))
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        return RuntimeError(message + player_text('launcher.platform', 'protection_history_could_not_be_read_select_help_save_support_re'))
    return RuntimeError(message + player_text('launcher.platform', 'no_matching_detection_was_found_select_help_save_support_report'))


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
            detected = ", ".join(player_text('launcher.platform', 'value_0_pid_value_1', value_0=f"{item['name']}", value_1=f"{item['pid']}") for item in processes)
            error = RuntimeError(player_text('launcher.platform', 'close_doom_eternal_before_repairing_its_connection_files_detecte', value_0=f'{detected}'))
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


def _install_core_pair(root, state_dir, consent, local_artifact, force_repair, journal=None):
    """Install a verified local Core distribution without replacing foreign providers."""
    from ..contracts.core_distribution import verify_runtime
    from .launcher_core_install import NAMES
    installed = probe_meathook(root)
    if local_artifact is None:
        if installed.ok:
            return GameLinkResult(state="verified", message=installed.message, path=str(root / "sentinel_core.dll"),
                                  sha256=str(installed.details["sha256"]), ownership="verified")
        raise RuntimeError(player_text('launcher.platform', 'select_the_matching_distribution_json_file'))
    manifest = local_artifact / "distribution.json" if local_artifact.is_dir() else local_artifact
    try:
        value, contents = verify_runtime(manifest, bootstrap_from_archive=True)
    except OSError as error:
        raise _core_file_error(Path(error.filename) if error.filename else manifest, error) from error
    if installed.ok:
        from ..contracts.core_distribution import version_key
        current = version_key(str(installed.details["version"]))
        incoming_version = version_key(value["version"])
        if current > incoming_version:
            return GameLinkResult(state="verified", message=installed.message, path=str(root / "sentinel_core.dll"),
                                  sha256=str(installed.details["sha256"]), ownership="verified")
        if installed.details["build_id"] == value["build_id"] and not force_repair:
            return GameLinkResult("verified", installed.message, str(root / "sentinel_core.dll"), str(installed.details["sha256"]), "verified")
    if (root / "XINPUT1_3.dll").exists():
        raise RuntimeError(player_text('launcher.platform', 'xinput1_3_dll_conflicts_with_doomeap_uninstall_the_mod_that_inst'))
    existing = root / "sentinel-distribution.json"
    owned_partial = _owned_core_partial(root, existing, manifest) if not installed.ok else False
    if any((root / name).exists() for name in ("sentinel_core.dll", "msimg32.dll")) and not installed.ok and not owned_partial:
        spec = DependencySpec(player_text('launcher.platform', 'back_up_and_replace_conflicting_connection_files'), value["version"],
            str(root), value["build_id"], "", "local")
        if not force_repair or not consent(spec):
            raise RuntimeError(player_text('launcher.platform', 'core_bootstrap_ownership_cannot_be_verified_foreign_files_are_pr'))
    incoming = {name: contents[name] for name in NAMES[:2]}
    incoming[NAMES[2]] = manifest.read_bytes()
    try:
        backup = _replace_core_pair(root, state_dir, incoming, journal)
    except OSError as error:
        raise _core_file_error(Path(error.filename) if error.filename else root / "sentinel_core.dll", error) from error
    return GameLinkResult(state="repaired" if owned_partial else "installed", message=player_text('launcher.platform', 'game_connection_files_value_0_installed', value_0=f"{value['version']}"),
                          path=str(root / "sentinel_core.dll"), sha256=hashlib.sha256(contents["sentinel_core.dll"]).hexdigest(),
                          ownership="launcher_installed", backup_path=str(backup))


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
        raise InstallationRecoveryRequired(player_text('launcher.platform', 'installation_recovery_is_pending'))


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
                raise InstallationRecoveryRequired(player_text('launcher.platform', 'game_configuration_is_busy')) from error
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
            "name": spec.name,
            "version": spec.version,
            "platform": self._platform_name(),
            "state": state,
            "source_url": spec.url,
            "expected_size": spec.expected_size,
            "expected_sha256": spec.sha256,
            **details,
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
                        "name": spec.name,
                        "version": spec.version,
                        "platform": self._platform_name(),
                        "state": "cache_lock_abandoned",
                        "source_url": spec.url,
                    })
                except Exception:
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
    def _extract(cls, spec: DependencySpec, archive: Path, destination: Path) -> None:
        if spec.archive_type == "file":
            target_name = spec.executable_glob or archive.name
            target_file = destination / target_name
            shutil.copy2(archive, target_file)
            return
        elif spec.archive_type == "zip":
            with zipfile.ZipFile(archive) as source:
                for member in source.infolist():
                    if not cls._safe_member(member.filename):
                        raise ValueError(player_text('launcher.platform', 'unsafe_archive_path_value_0', value_0=f'{member.filename}'))
                    mode = member.external_attr >> 16
                    if mode & 0o170000 == 0o120000:
                        raise ValueError(player_text('launcher.platform', 'archive_symlink_rejected_value_0', value_0=f'{member.filename}'))
                source.extractall(destination)
        elif spec.archive_type == "tar.gz":
            with tarfile.open(archive, "r:gz") as source:
                members = source.getmembers()
                for member in members:
                    if (
                        not cls._safe_member(member.name)
                        or member.issym()
                        or member.islnk()
                        or not (member.isfile() or member.isdir())
                    ):
                        raise ValueError(player_text('launcher.platform', 'unsafe_archive_member_value_0', value_0=f'{member.name}'))
                source.extractall(destination, members=members)
        else:
            raise ValueError(player_text('launcher.platform', 'unsupported_archive_type_value_0', value_0=f'{spec.archive_type}'))

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

    def _installed_with_reason(self, spec: DependencySpec, destination: Path) -> tuple[InstalledDependency | None, str]:
        receipt_path = destination / self.RECEIPT
        if not receipt_path.is_file():
            return None, "receipt_missing"
        try:
            receipt = InstalledDependency(**json.loads(receipt_path.read_text(encoding="utf-8")))
            executable = Path(receipt.executable)
        except Exception:
            return None, "receipt_invalid"
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
                except Exception:
                    pass
        canonical_destination = destination.resolve()
        canonical_executable = executable.resolve()
        details: dict[str, object] = {
            "cache_path": str(executable),
            "cache_path_display": str(executable),
            "cache_path_canonical": str(canonical_executable),
            "cache_root_display": str(destination),
            "cache_root_canonical": str(canonical_destination),
            "executable_display": str(executable),
            "executable_canonical": str(canonical_executable),
            "executable_path": str(executable),
            "executable_path_canonical": str(canonical_executable),
            "containment": self.is_contained(executable, destination),
            "cache_exists": executable.is_file(),
            "cached_size": None,
            "cached_sha256": None,
            "hash_valid": False,
            "mode": None,
            "executable": False,
            "verification_stage": reason,
        }
        if executable.is_file():
            stat_result = executable.stat()
            details.update({
                "cached_size": stat_result.st_size,
                "cached_sha256": self._sha256(executable),
                "hash_valid": self._sha256(executable) == spec.sha256,
                "mode": oct(stat_result.st_mode & 0o777),
                "executable": os.access(executable, os.X_OK),
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
            raise RuntimeError(player_text('launcher.platform', 'dependency_value_0_is_unsupported_on_platform_value_1', value_0=f'{spec.name}', value_1=f'{selected}'))
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
                raise PermissionError(player_text('launcher.platform', 'dependency_acquisition_declined_value_0', value_0=f'{spec.name}'))

            with tempfile.TemporaryDirectory(prefix=f".{spec.name}-", dir=self.root) as temporary_name:
                temporary = Path(temporary_name)
                archive = temporary / "artifact"
                if local_artifact is None:
                    try:
                        download = self.transport.fetch(spec.url, archive)
                    except Exception as error:
                        self._record_acquisition(
                            spec, "failed", verification_stage="transport",
                            failure_reason=f"{type(error).__name__}: {error}", final_url=spec.url,
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
                actual = self._sha256(archive)
                actual_size = archive.stat().st_size
                self._record_acquisition(
                    spec,
                    "downloaded",
                    final_url=final_url,
                    content_length=content_length,
                    actual_download_size=actual_size,
                    actual_download_sha256=actual,
                )
                if spec.expected_size is not None and actual_size != spec.expected_size:
                    self._record_acquisition(
                        spec, "failed", verification_stage="download_size",
                        failure_reason="download_size_mismatch", actual_download_size=actual_size,
                        actual_download_sha256=actual, final_url=final_url,
                    )
                    raise ValueError(player_text('launcher.platform', 'size_mismatch_for_value_0_expected_value_1_got_value_2', value_0=f'{spec.name}', value_1=f'{spec.expected_size}', value_2=f'{actual_size}'))
                if actual != spec.sha256:
                    self._record_acquisition(
                        spec, "failed", verification_stage="download_sha256",
                        failure_reason="download_sha256_mismatch", actual_download_size=actual_size,
                        actual_download_sha256=actual, final_url=final_url,
                    )
                    raise ValueError(player_text('launcher.platform', 'sha_256_mismatch_for_value_0_expected_value_1_got_value_2', value_0=f'{spec.name}', value_1=f'{spec.sha256}', value_2=f'{actual}'))

                extracted = temporary / "extracted"
                extracted.mkdir()
                self._extract(spec, archive, extracted)
                executables = sorted(extracted.glob(spec.executable_glob))
                if len(executables) != 1:
                    raise ValueError(player_text('launcher.platform', 'expected_one_value_0_found_value_1', value_0=f'{spec.executable_glob}', value_1=f'{len(executables)}'))
                executable_relative = executables[0].relative_to(extracted)
                if os.name != "nt":
                    executables[0].chmod(executables[0].stat().st_mode | 0o100)

                staged = self.root / f".{self._cache_key(spec)}.incoming"
                shutil.rmtree(staged, ignore_errors=True)
                shutil.copytree(extracted, staged)
                receipt = InstalledDependency(
                    name=spec.name,
                    version=spec.version,
                    artifact_sha256=actual,
                    source_url=spec.url if local_artifact is None else str(local_artifact.resolve()),
                    root=str(destination.resolve()),
                    executable=str((destination / executable_relative).resolve()),
                    commit=spec.commit,
                )
                (staged / self.RECEIPT).write_text(
                    json.dumps(asdict(receipt), indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
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
                except Exception:
                    if previous.exists() and not destination.exists():
                        publish_file(previous, destination, operation="dependency_rollback")
                    raise
                if previous.exists():
                    shutil.rmtree(previous) if previous.is_dir() else previous.unlink()
                if not Path(receipt.executable).is_file():
                    raise RuntimeError(player_text('launcher.platform', 'dependency_installation_failed'))
                verified = self._installed(spec, destination)
                if verified is None:
                    _, reason = self._installed_with_reason(spec, destination)
                    self._record_acquisition(
                        spec,
                        "failed",
                        verification_stage="post_publish",
                        failure_reason=reason,
                        cache_path=str(receipt.executable),
                        cache_root_display=str(destination),
                        cache_root_canonical=str(destination.resolve()),
                        executable_display=str(receipt.executable),
                        executable_canonical=str(Path(receipt.executable).resolve()),
                        containment=self.is_contained(Path(receipt.executable), destination),
                    )
                    raise RuntimeError(player_text('launcher.platform', 'dependency_installation_failed_verification_value_0', value_0=f'{reason}'))
                executable = Path(verified.executable)
                self._record_acquisition(
                    spec, "published", verification_stage="post_publish",
                    cache_path=str(executable), cached_size=executable.stat().st_size,
                    cached_sha256=self._sha256(executable), mode=oct(executable.stat().st_mode & 0o777),
                    executable=os.access(executable, os.X_OK), final_url=final_url,
                    content_length=content_length, actual_download_size=actual_size,
                    actual_download_sha256=actual,
                )
                return verified

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
            and not name.replace('\\', '/').removeprefix('./').startswith('ap_enemy_resources/'))


def _packagemapspec_owner(
    packagemapspec: Path,
    runtime_map: str,
) -> tuple[str, int, list[str]]:
    document = json.loads(packagemapspec.read_text(encoding="utf-8"))
    files = document.get("files")
    maps = document.get("maps")
    refs = document.get("mapFileRefs")
    if not isinstance(files, list) or not isinstance(maps, list) or not isinstance(refs, list):
        raise ValueError(player_text('launcher.platform', 'packagemapspec_json_has_invalid_resource_tables'))
    map_indices = [
        index for index, record in enumerate(maps)
        if isinstance(record, dict) and record.get("name") == runtime_map
    ]
    if len(map_indices) != 1:
        raise ValueError(player_text('launcher.platform', 'runtime_map_value_0_has_no_unique_packagemapspec_record', value_0=f'{runtime_map!r}'))
    map_index = map_indices[0]
    candidates: list[tuple[int, str]] = []
    for relation in refs:
        if not isinstance(relation, dict) or relation.get("map") != map_index:
            continue
        file_index = relation.get("file")
        if not isinstance(file_index, int) or not 0 <= file_index < len(files):
            raise ValueError(player_text('launcher.platform', 'runtime_map_value_0_has_invalid_resource_reference', value_0=f'{runtime_map!r}'))
        record = files[file_index]
        name = record.get("name") if isinstance(record, dict) else None
        if _vanilla_resource_name(name):
            candidates.append((file_index, name))
    if not candidates:
        raise ValueError(player_text('launcher.platform', 'runtime_map_value_0_has_no_resource_reference', value_0=f'{runtime_map!r}'))
    return candidates[0][1], candidates[0][0], [name for _, name in candidates]


def installation_error_details(error: Exception, step: str) -> dict:
    return {
        "required_resources": [],
        **getattr(error, "details", {}),
        "failure_step": getattr(error, "details", {}).get("failure_step", step),
        "exception_type": type(error).__name__,
        "code": getattr(error, "code", None),
        "verification_error": str(error),
        "traceback": "".join(traceback.format_exception(type(error), error, error.__traceback__, limit=6)),
    }


@contextmanager
def installation_step(step: str):
    try:
        yield
    except Exception as error:
        error.details = installation_error_details(error, step)
        raise


def _installation_failure(result: AdapterResult, error: Exception, step: str, event_sink,
                          game_root: Path | None = None, staged_mod: Path | None = None) -> AdapterResult:
    details = {**result.details, **installation_error_details(error, step), "post_install_verification": "failed"}
    if not details["required_resources"] and game_root is not None and staged_mod is not None:
        try:
            details["required_resources"], _ = _required_linux_resource_specs(staged_mod, game_root / "base/packagemapspec.json")
        except Exception as evidence_error:
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
    return player_text('launcher.platform', 'installation_failed_at_value_0_value_1_value_2', value_0=f'{step}', value_1=f'{type(error).__name__}', value_2=f'{error}')


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
        decoded = player_text('launcher.platform', 'unavailable_value_0_value_1', value_0=f'{type(error).__name__}', value_1=f'{error}')
    return {
        "settings": {key: next((line.split("=", 1)[1] for line in text.splitlines()
                                if line.startswith(f":{key}=")), None)
                     for key in ('HAS_CHECKED_RESOURCES', 'RESET_BACKUPS', 'ASSET_VERSION')},
        "settings_backup_lines": sum(line.rstrip().endswith(".backup") for line in text.splitlines()),
        "backup_files": sum(path.is_file() for path in base.rglob("*.backup")),
        "rehash_lines": len(lines),
        "rehash_bank_lines": sum("ap_enemy_resources/" in line.replace("\\", "/") for line in lines),
        "bank_files": sorted(path.name for path in (base / "ap_enemy_resources").glob("*")),
        "bank_marker": spec.is_file() and "doomEAPEnemyResourceBanks" in spec.read_text(encoding="utf-8-sig"),
        "meta_decoded": decoded,
    }


def _log_injector_state(game_root: Path, phase: str, event_sink) -> dict:
    try:
        summary = injector_state_summary(game_root)
    except (OSError, ValueError) as error:
        summary = {"error": f"{type(error).__name__}: {error}"}
    if event_sink is not None:
        for key, value in summary.items():
            for line in json.dumps(value, indent=2).splitlines():
                event_sink("log", message=player_text('launcher.platform', 'injector_value_0_value_1_value_2', value_0=f'{phase}', value_1=f'{key}', value_2=f'{line}'))
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
        if value is None or not name.endswith(".resources"):
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
        lines.append(player_text('launcher.installation_adapters', 'has_checked_resources_0'))
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
            raise ValueError(player_text('launcher.platform', 'invalid_injector_hold'))
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
        require_doom_closed(player_text('launcher.platform', 'recovering_injector_resources'))
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
        require_doom_closed(player_text('launcher.platform', 'preparing_injector_resources'))
        _recover_injector_resources(game_root)
        _log_injector_state(game_root, player_text('launcher.platform', 'before_preparation'), event_sink)
        changes = _injector_mapping_changes(game_root)
        record = _create_injector_hold(game_root, changes)
        directory = game_root / "base/ap_enemy_resources"
        failure = None
        try:
            if directory.exists():
                directory.rename(game_root / INJECTOR_HOLD / directory.name)
            _publish_injector_inputs(game_root, changes, record)
            record["phase"] = InjectorHoldPhase.PREPARED
            _write_injector_hold(game_root, record)
            _log_injector_state(game_root, "before", event_sink)
            yield InjectorResourceHold(game_root, record)
        except BaseException as error:
            failure = error
            raise
        finally:
            _finish_injector_hold(game_root, record, failure, event_sink)


def _tag_resource_candidates(members):
    candidates = []
    for name in members:
        match = re.search(r"/maps/(game/(?:dlc|dlc2)/[^/]+/[^/]+)\.entities$", f"/{name}")
        if match is not None and match[1] != 'game/dlc/hub/hub':
            candidates.append((match[1], name.split('/', 1)[0], name))
    return sorted(candidates)


def _required_tag_resource(members, packagemapspec):
    candidates = _tag_resource_candidates(members)
    for runtime_map, staged_owner, member in candidates:
        try:
            expected_owner, _, _ = _packagemapspec_owner(packagemapspec, runtime_map)
        except (OSError, ValueError):
            continue
        if PurePosixPath(expected_owner).name.removesuffix('.resources') == staged_owner:
            return ('tag', runtime_map, member)
    return ('tag', candidates[0][0], None) if candidates else None


def _required_linux_resource_specs(
    staged_mod: Path,
    packagemapspec: Path,
) -> tuple[list[dict[str, object]], list[str]]:
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
    for label, runtime_map in (
        ("e1m1", "game/sp/e1m1_intro/e1m1_intro"),
        ("hub", "game/hub/hub"),
    ):
        required.append((label, runtime_map, None))

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

    tag_request = _required_tag_resource(members, packagemapspec)
    if tag_request is not None:
        required.append(tag_request)

    results: list[dict[str, object]] = []
    warnings: list[str] = []
    for label, runtime_map, member in required:
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
        meta_path = resource_path.with_suffix(".meta")
        path_marker = ""
        payload_present = False
        path_present = False
        if selected_member is not None:
            path_marker = selected_member.split("/", 1)[1]
            path_present = _contains_bytes(resource_path, path_marker.encode("utf-8"))
            payload_present = _contains_bytes(resource_path, payloads[selected_member])
        status = "verified" if (
            selected_member is not None
            and resource_path.is_file()
            and meta_path.is_file()
            and path_present
            and payload_present
        ) else "failed"
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
                (owner_error, not owner_error),
                (player_text('launcher.platform', 'missing_staged_member'), selected_member is not None),
                (player_text('launcher.platform', 'missing_resource_file'), resource_path.is_file()),
                (player_text('launcher.platform', 'missing_meta_file'), meta_path.is_file()),
                (player_text('launcher.platform', 'resource_path_missing_from_container'), path_present),
                (player_text('launcher.platform', 'room_payload_missing_from_container'), payload_present),
            ) if not present),
        }
        results.append(result)
        if status != "verified":
            warnings.append(f"{label}:{expected_owner}:{result['failure_reason']}")
    return results, warnings


def _required_resource_output_issues(
    output: str,
    results: Sequence[Mapping[str, object]],
) -> list[str]:
    names = {
        str(value).casefold()
        for result in results
        for value in (
            result.get("label"), result.get("expected_owner"),
            result.get("runtime_map"), result.get("staged_owner"),
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
        raise ValueError(player_text('launcher.platform', 'staged_room_zip_is_a_symbolic_link'))
    staged = staged_input.resolve()
    mods = (root / "Mods").resolve()
    if staged.parent != mods or not staged.is_file():
        raise ValueError(player_text('launcher.platform', 'staged_room_zip_is_outside_configured_game_mods_folder'))
    if not zipfile.is_zipfile(staged):
        raise ValueError(player_text('launcher.platform', 'staged_room_zip_is_invalid'))
    packagemapspec = root / "base" / "packagemapspec.json"
    if not packagemapspec.is_file():
        raise FileNotFoundError(player_text('launcher.platform', 'packagemapspec_json_is_missing_from_current_game_resources'))
    results, missing = _required_linux_resource_specs(staged, packagemapspec)
    output_issues = _required_resource_output_issues(
        f"{stdout}\n{stderr}", results
    )
    if output_issues:
        raise LinuxVerificationError(
            player_text('launcher.platform', 'injectorshell_reported_required_resource_warnings_skips') + " | ".join(output_issues),
            {"required_resources": results, "output_issues": output_issues},
        )
    if missing:
        raise LinuxVerificationError(
            player_text('launcher.platform', 'required_resource_evidence_missing') + ", ".join(missing),
            {"required_resources": results},
        )
    return {
        "state": "verified",
        "mods_path": str(mods),
        "packagemapspec_path": str(packagemapspec),
        "packagemapspec_sha256": hashlib.sha256(packagemapspec.read_bytes()).hexdigest(),
        "required_resources": results,
    }


def _stage_mod(mod_zip: Path, game_root: Path) -> Path:
    require_doom_closed(player_text('launcher.platform', 'staging_a_room_mod'))
    if not mod_zip.is_file() or not zipfile.is_zipfile(mod_zip):
        raise ValueError(player_text('launcher.platform', 'mod_is_not_a_valid_zip_value_0', value_0=f'{mod_zip}'))
    mods = game_root / "Mods"
    mods.mkdir(parents=True, exist_ok=True)
    destination = mods / mod_zip.name
    incoming = destination.with_name(f".{destination.name}.incoming")
    shutil.copy2(mod_zip, incoming)
    publish_file(incoming, destination, operation="room_mod_stage")
    return destination


LEGACY_DOOM_AP_MOD_NAMES = frozenset(
    {
        "ap_mod.zip",
        "doometernalarchipelagoalpha.zip",
        "doometernalarchipelagobeta.zip",
        "doometernalarchipelagoprealpha.zip",
    }
)
LEGACY_DOOM_AP_VERSIONED_MOD = re.compile(
    r"doometernalarchipelago-(?:[0-9a-f]{16}|v[0-9][0-9a-z._-]*)\.zip",
    re.IGNORECASE,
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
    require_doom_closed(player_text('launcher.platform', 'staging_a_room_mod'))
    if not mod_zip.is_file() or not zipfile.is_zipfile(mod_zip):
        raise ValueError(player_text('launcher.platform', 'mod_is_not_a_valid_zip_value_0', value_0=f'{mod_zip}'))
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
            raise RuntimeError(player_text('launcher.platform', 'room_mod_is_a_symbolic_link_value_0', value_0=f'{candidate}'))
        candidates.append(candidate)
    if destination.exists() and destination not in candidates:
        raise RuntimeError(player_text('launcher.platform', 'refusing_to_overwrite_an_unowned_room_mod_value_0', value_0=f'{destination}'))
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
        raise RuntimeError(player_text('launcher.platform', 'room_mod_ownership_hash_does_not_match_value_0', value_0=f'{candidate}'))
    return True


def _stage_journaled_mod(source, destination, candidates, journal, removal_sink):
    content = source.read_bytes()
    for candidate in candidates:
        if candidate != destination:
            journal.expect(candidate, None)
    journal.expect(destination, hashlib.sha256(content).hexdigest())
    require_doom_closed(player_text('launcher.platform', 'publishing_a_room_mod'))
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
        "schema": 1,
        "manifest_hash": manifest_hash,
        "staged_mod": str(destination.resolve()),
        "staged_sha256": expected_sha,
    }
    try:
        shutil.copy2(mod_zip, incoming)
        if hashlib.sha256(incoming.read_bytes()).hexdigest() != expected_sha or not zipfile.is_zipfile(incoming):
            raise RuntimeError(player_text('launcher.platform', 'room_mod_copy_verification_failed'))
        require_doom_closed(player_text('launcher.platform', 'publishing_a_room_mod'))
        for candidate in candidates:
            backup = incoming.with_name(f"{incoming.name}.{candidate.name}.bak")
            publish_file(candidate, backup, operation="room_mod_backup")
            backups.append((candidate, backup))
        publish_file(incoming, destination, operation="room_mod_stage")
        published = True
        ownership_receipt.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        publish_file(temporary, ownership_receipt, operation="room_receipt_publish")
    except Exception:
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
        _write_installer_settings(settings_file,
            "\n".join((
                player_text('launcher.installation_adapters', 'asset_version_2026_04_03'),
                player_text('launcher.installation_adapters', 'auto_launch_game_0'),
                player_text('launcher.installation_adapters', 'auto_update_0'),
                "",
            )),
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
            new_lines.append(player_text('launcher.installation_adapters', 'auto_launch_game_0'))
            has_auto_launch = True
        elif stripped.startswith(":AUTO_UPDATE="):
            new_lines.append(player_text('launcher.installation_adapters', 'auto_update_0'))
            has_auto_update = True
        else:
            new_lines.append(line)

    if not has_auto_launch:
        new_lines.append(player_text('launcher.installation_adapters', 'auto_launch_game_0'))
    if not has_auto_update:
        new_lines.append(player_text('launcher.installation_adapters', 'auto_update_0'))

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
            player_text('launcher.platform', 'windows_mod_injector_package_is_corrupted_or_incomplete_missing', value_0=f'{len(missing_members)}', value_1=f"{', '.join(missing_members)}")
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
        raise RuntimeError(player_text('launcher.platform', 'eternalmodinjector_bat_is_missing_after_staging_into_game_root'))
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
            raise RuntimeError(player_text('launcher.platform', 'staging_validation_failed_for_value_0', value_0=f'{rel_path}'))
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

    # 1. Deterministic recovery from previous crash or stale hold
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
                        player_text('launcher.platform', 'stale_sandbox_hold_recovery_integrity_failure_expected_sha_256_v', value_0=f'{expected_sha}', value_1=f'{recovered_sha}')
                    )
            finally:
                tx_file.unlink(missing_ok=True)
        if event_sink is not None:
            event_sink(
                "injector_sandbox_guard",
                state="recovered_stale_hold",
                verified=True,
                sha256=recovered_sha,
                size=recovered_size,
            )

    # 2. Ambiguous state check: fail closed without overwriting either file
    if original.exists() and hold.exists():
        raise RuntimeError(
            player_text('launcher.platform', 'ambiguous_sandbox_state_both_value_0_and_hold_file_value_1_exist', value_0=f'{original}', value_1=f'{hold}')
        )

    # 3. If sandbox is absent, upstream injector simply skips optional sandbox patching
    if not original.is_file():
        yield
        return

    # record pre-run identity
    original_size = original.stat().st_size
    original_sha = hashlib.sha256(original.read_bytes()).hexdigest()

    # 5. Persist transaction record in state directory if available
    if tx_file is not None:
        tx_file.parent.mkdir(parents=True, exist_ok=True)
        tx_data = {
            "original": str(original),
            "hold": str(hold),
            "sha256": original_sha,
            "size": original_size,
            "timestamp": datetime.now().isoformat(),
        }
        tx_file.write_text(json.dumps(tx_data, indent=2) + "\n", encoding="utf-8")

    # atomically move original -> hold
    publish_file(original, hold, operation="sandbox_hold")
    if event_sink is not None:
        event_sink(
            "injector_sandbox_guard",
            state="held",
            sha256=original_sha,
            size=original_size,
        )

    # 7. Execute injector with guaranteed restoration in finally
    try:
        yield
    finally:
        if hold.is_file():
            publish_file(hold, original, operation="sandbox_restore")
            restored_size = original.stat().st_size
            restored_sha = hashlib.sha256(original.read_bytes()).hexdigest()

            if tx_file and tx_file.is_file():
                tx_file.unlink(missing_ok=True)

            if restored_sha != original_sha or restored_size != original_size:
                raise RuntimeError(
                    player_text('launcher.platform', 'sandbox_restoration_integrity_check_failed_expected_sha_256_valu', value_0=f'{original_sha}', value_1=f'{original_size}', value_2=f'{restored_sha}', value_3=f'{restored_size}')
                )

            if event_sink is not None:
                event_sink(
                    "injector_sandbox_guard",
                    state="restored",
                    verified=True,
                    sha256=restored_sha,
                    size=restored_size,
                )


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
            player_text('launcher.platform', 'ambiguous_sandbox_state_both_value_0_and_hold_file_value_1_exist', value_0=f'{original}', value_1=f'{hold}')
        )
    if original.exists() or not hold.is_file():
        return
    publish_file(hold, original, operation="sandbox_recover")
    recovered_sha = hashlib.sha256(original.read_bytes()).hexdigest()
    recovered_size = original.stat().st_size
    if tx_file is not None and tx_file.is_file():
        try:
            tx_data = json.loads(tx_file.read_text(encoding="utf-8"))
            expected_sha = tx_data.get("sha256")
            if expected_sha and recovered_sha != expected_sha:
                raise RuntimeError(
                    player_text('launcher.platform', 'stale_sandbox_hold_recovery_integrity_failure_expected_sha_256_v', value_0=f'{expected_sha}', value_1=f'{recovered_sha}')
                )
        finally:
            tx_file.unlink(missing_ok=True)
    if event_sink is not None:
        event_sink(
            "injector_sandbox_guard",
            state="recovered_stale_hold",
            verified=True,
            sha256=recovered_sha,
            size=recovered_size,
        )


def finalize_mod_installation(game_root, staged, result, *, event_sink=None, journal=None):
    try:
        from .campaign_resources import enable_campaign_resources
        resources = enable_campaign_resources(game_root, staged, journal=journal)
    except Exception as error:
        return _installation_failure(result, error, player_text('launcher.campaign_resources', 'prepare_banks'), event_sink, game_root, staged)
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
    except Exception as error:
        return _installation_failure(result, error, "verify", event_sink, game_root, staged)
    return AdapterResult(state="applied", message=result.message, command=result.command,
        returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
        details={**result.details, **verification, "post_install_verification": verification["state"]})


def __getattr__(name):
    if name in {"WindowsModInjectorAdapter", "WindowsModManagerAdapter", "LinuxModManagerAdapter"}:
        from . import installation_adapters
        return getattr(installation_adapters, name)
    raise AttributeError(name)


@dataclass(frozen=True)
class SteamInstallation:
    library_root: Path
    game_root: Path
    manifest: Path


def query_windows_registry_steam_roots(
    registry_query: Callable[[], Sequence[Path]] | None = None,
) -> tuple[Path, ...]:
    """Discover installed Steam client roots from authoritative Windows registry locations."""
    if registry_query is not None:
        return tuple(dict.fromkeys(registry_query()))
    if os.name != "nt":
        return ()
    roots: list[Path] = []
    try:
        import winreg

        registry_values = (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        )
        for hive, key_name, value_name in registry_values:
            try:
                with winreg.OpenKey(hive, key_name) as key:
                    value, _ = winreg.QueryValueEx(key, value_name)
                if value and isinstance(value, str):
                    path = Path(value.replace("/", "\\")).expanduser()
                    roots.append(path)
            except (FileNotFoundError, OSError):
                continue
    except ImportError:
        pass
    return tuple(dict.fromkeys(roots))


class SteamInstallationLocator:
    """Locate Steam App ID 782330 without starting Steam or game."""

    APP_MANIFEST = f"appmanifest_{DOOM_ETERNAL_APP_ID}.acf"

    def __init__(
        self,
        candidate_roots: Sequence[Path] | None = None,
        *,
        platform_name: str | None = None,
        registry_query: Callable[[], Sequence[Path]] | None = None,
        environment: Mapping[str, str] | None = None,
    ):
        self.candidate_roots = tuple(
            self.default_roots(
                platform_name=platform_name,
                registry_query=registry_query,
                environment=environment,
            )
            if candidate_roots is None
            else candidate_roots
        )

    @staticmethod
    def default_roots(
        *,
        platform_name: str | None = None,
        registry_query: Callable[[], Sequence[Path]] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> tuple[Path, ...]:
        env = os.environ if environment is None else environment
        platform_name = platform_name or ("windows" if os.name == "nt" else "linux")
        roots: list[Path] = []
        if platform_name == "windows":
            roots.extend(query_windows_registry_steam_roots(registry_query))
            for variable in (player_text('launcher.platform', 'programfiles_x86'), 'PROGRAMFILES', 'PROGRAMW6432'):
                value = env.get(variable)
                if value:
                    roots.append(Path(value) / "Steam")
        else:
            home = Path.home()
            roots.extend(
                (
                    home / ".local/share/Steam",
                    home / ".steam/steam",
                    home / ".steam/root",
                    home / ".steam",
                )
            )
        return tuple(dict.fromkeys(roots))

    @staticmethod
    def _declared_libraries(steam_root: Path) -> tuple[Path, ...]:
        configuration = steam_root / "steamapps/libraryfolders.vdf"
        libraries = [steam_root]
        if configuration.is_file():
            text = configuration.read_text(encoding="utf-8", errors="replace")
            parsed_paths: list[Path] = []
            try:
                document = ValveVdfParser.parse(text)
                for _top_key, top_val in document.items():
                    if isinstance(top_val, dict):
                        for item_key, item_val in top_val.items():
                            if isinstance(item_val, dict):
                                for prop_key, prop_val in item_val.items():
                                    if prop_key.casefold() == "path" and isinstance(prop_val, str):
                                        parsed_paths.append(Path(prop_val))
                            elif isinstance(item_val, str) and item_key.isdigit():
                                parsed_paths.append(Path(item_val))
            except Exception:
                pass

            for raw in re.findall(r'"path"\s+"((?:\\\\.|[^"\\])*)"', text, re.IGNORECASE):
                value = raw.replace("\\\\", "\\")
                parsed_paths.append(Path(value))
            for raw in re.findall(r'"\d+"\s+"((?:\\\\.|[^"\\])*)"', text):
                value = raw.replace("\\\\", "\\")
                parsed_paths.append(Path(value))

            for path in parsed_paths:
                if path not in libraries:
                    libraries.append(path)
        return tuple(dict.fromkeys(libraries))

    @staticmethod
    def _installation(library: Path) -> SteamInstallation | None:
        manifest = library / "steamapps" / SteamInstallationLocator.APP_MANIFEST
        if not manifest.is_file():
            return None
        text = manifest.read_text(encoding="utf-8", errors="replace")
        installdir: str | None = None
        try:
            document = ValveVdfParser.parse(text)
            for _top_key, top_val in document.items():
                if isinstance(top_val, dict):
                    for prop_key, prop_val in top_val.items():
                        if prop_key.casefold() == "installdir" and isinstance(prop_val, str):
                            installdir = prop_val
                            break
        except Exception:
            pass
        if not installdir:
            match = re.search(r'"installdir"\s+"([^"]+)"', text, re.IGNORECASE)
            if match is not None:
                installdir = match.group(1)
        if not installdir:
            return None
        game_root = library / "steamapps/common" / installdir
        if not (game_root / "DOOMEternalx64vk.exe").is_file() or not (game_root / "base").is_dir():
            return None
        return SteamInstallation(library.resolve(), game_root.resolve(), manifest.resolve())

    def inspect_discovery(
        self, manual_game_root: Path | None = None
    ) -> tuple[tuple[SteamInstallation, ...], DiscoverySentinel]:
        installations: list[SteamInstallation] = []
        trace: list[dict[str, object]] = []
        any_root_exists = False
        any_manifest_exists = False
        any_installdir_found = False

        if manual_game_root is not None:
            game_root = manual_game_root.resolve()
            valid = (game_root / "DOOMEternalx64vk.exe").is_file() and (game_root / "base").is_dir()
            trace.append({
                "source": "manual",
                "path": str(game_root),
                "valid": valid,
            })
            if not valid:
                return (), DiscoverySentinel(
                    DiscoveryStatus.INVALID.value,
                    reason=player_text('launcher.platform', 'invalid_doom_eternal_installation_value_0', value_0=f'{game_root}'),
                    trace=tuple(trace),
                )
            installations.append(SteamInstallation(game_root.parent, game_root, Path()))

        for root in self.candidate_roots:
            root_exists = root.is_dir()
            root_info: dict[str, object] = {
                "root": str(root),
                "exists": root_exists,
            }
            if not root_exists:
                trace.append(root_info)
                continue

            any_root_exists = True
            vdf_path = root / "steamapps/libraryfolders.vdf"
            vdf_exists = vdf_path.is_file()
            declared = self._declared_libraries(root)
            root_info["vdf_path"] = str(vdf_path)
            root_info["vdf_exists"] = vdf_exists
            root_info["declared_library_count"] = len(declared)
            root_info["declared_libraries"] = [str(lib) for lib in declared]

            lib_checks: list[dict[str, object]] = []
            for library in declared:
                manifest_path = library / "steamapps" / self.APP_MANIFEST
                manifest_exists = manifest_path.is_file()
                check: dict[str, object] = {
                    "library": str(library),
                    "manifest_path": str(manifest_path),
                    "manifest_exists": manifest_exists,
                }
                if manifest_exists:
                    any_manifest_exists = True
                    installation = self._installation(library)
                    if installation is not None:
                        any_installdir_found = True
                        check["game_root"] = str(installation.game_root)
                        check["valid"] = True
                        if not any(item.game_root == installation.game_root for item in installations):
                            installations.append(installation)
                    else:
                        text = manifest_path.read_text(encoding="utf-8", errors="replace")
                        match = re.search(r'"installdir"\s+"([^"]+)"', text, re.IGNORECASE)
                        if match:
                            any_installdir_found = True
                            candidate_game = library / "steamapps/common" / match.group(1)
                            check["game_root"] = str(candidate_game)
                            check["valid"] = False
                            check["has_exe"] = (candidate_game / "DOOMEternalx64vk.exe").is_file()
                            check["has_base"] = (candidate_game / "base").is_dir()
                        else:
                            check["valid"] = False
                            check["error"] = player_text('launcher.platform', 'installdir_missing_from_manifest')
                lib_checks.append(check)

            root_info["checks"] = lib_checks
            trace.append(root_info)

        inst_roots = tuple(str(item.game_root) for item in installations)
        if len(inst_roots) == 1:
            sentinel = DiscoverySentinel(
                DiscoveryStatus.FOUND.value,
                path=inst_roots[0],
                reason=player_text('launcher.doctor', 'game_folder_found'),
                trace=tuple(trace),
            )
        elif len(inst_roots) > 1:
            sentinel = DiscoverySentinel(
                DiscoveryStatus.AMBIGUOUS.value,
                candidates=inst_roots,
                reason=player_text('launcher.platform', 'more_than_one_game_folder_was_found_choose_the_one_used_by_steam'),
                trace=tuple(trace),
            )
        elif not any_root_exists:
            sentinel = DiscoverySentinel(
                DiscoveryStatus.NOT_FOUND.value,
                reason=player_text('launcher.platform', 'steam_was_not_found_choose_your_doom_eternal_folder'),
                trace=tuple(trace),
            )
        elif not any_manifest_exists:
            sentinel = DiscoverySentinel(
                DiscoveryStatus.NOT_FOUND.value,
                reason=player_text('launcher.platform', 'doom_eternal_was_not_found_in_steam_s_libraries_choose_its_game'),
                trace=tuple(trace),
            )
        elif any_installdir_found and not installations:
            sentinel = DiscoverySentinel(
                DiscoveryStatus.INVALID.value,
                reason=player_text('launcher.platform', 'game_files_are_missing_verify_doom_eternal_in_steam'),
                trace=tuple(trace),
            )
        else:
            sentinel = DiscoverySentinel(
                DiscoveryStatus.NOT_FOUND.value,
                reason=player_text('launcher.platform', 'steam_was_not_found_choose_the_game_folder'),
                trace=tuple(trace),
            )

        return tuple(installations), sentinel

    def discover(self, manual_game_root: Path | None = None) -> tuple[SteamInstallation, ...]:
        installations, _ = self.inspect_discovery(manual_game_root)
        return installations

    def discover_sentinel(self, manual_game_root: Path | None = None) -> DiscoverySentinel:
        _, sentinel = self.inspect_discovery(manual_game_root)
        return sentinel

    def diagnose(self, manual_game_root: Path | None = None) -> dict[str, object]:
        installations, sentinel = self.inspect_discovery(manual_game_root)
        return {
            "sentinel": asdict(sentinel),
            "installations": [
                {
                    "library_root": str(item.library_root),
                    "game_root": str(item.game_root),
                    "manifest": str(item.manifest),
                }
                for item in installations
            ],
        }


class ProcessInspectionError(RuntimeError):
    """The launcher cannot establish whether DOOM Eternal is closed."""


def require_doom_closed(operation: str) -> None:
    processes = detect_doom_processes()
    if processes:
        detected = ", ".join(player_text('launcher.platform', 'value_0_pid_value_1', value_0=f"{item['name']}", value_1=f"{item['pid']}") for item in processes)
        error = RuntimeError(player_text('launcher.platform', 'close_doom_eternal_before_value_0_detected_value_1', value_0=f'{operation}', value_1=f'{detected}'))
        error.processes = processes
        raise error


def detect_doom_processes(
    *,
    process_list: Sequence[Sequence[str]] | None = None,
) -> tuple[dict[str, object], ...]:
    """Return bounded process facts suitable for diagnostics."""
    supported_names = {
        "doometernalx64vk.exe": "doometernalx64vk.exe",
        "doometernalx64vk": "doometernalx64vk",
        "doometernalx64v": "doometernalx64vk.exe",
        "ap_client.exe": "ap_client.exe",
        "ap_client": "ap_client",
    }

    def basename(value: object) -> str:
        text = str(value).strip().strip("\"'")
        return text.replace("\\", "/").rsplit("/", 1)[-1].casefold()

    rows: list[dict[str, object]] = []
    if process_list is None:
        if os.name == "nt":
            try:
                completed = subprocess.run(
                    ["tasklist", "/fo", "csv", "/nh"],
                    capture_output=True, text=True, timeout=3, check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                completed.check_returncode()
                process_list = tuple((row[0], row[1]) for row in csv.reader(completed.stdout.splitlines()) if row)
            except (OSError, subprocess.SubprocessError, IndexError, UnicodeError) as error:
                raise ProcessInspectionError(
                    player_text('launcher.platform', 'could_not_inspect_running_processes_cannot_safely_change_game_fi')
                ) from error
        else:
            try:
                completed = subprocess.run(
                    ["ps", "-eo", "pid=,comm=,args="],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                completed.check_returncode()
                process_list = tuple(
                    tuple(line.split(None, 2))
                    for line in completed.stdout.splitlines()
                    if line.strip()
                )
            except (OSError, subprocess.SubprocessError, UnicodeError) as error:
                raise ProcessInspectionError(
                    player_text('launcher.platform', 'could_not_inspect_running_processes_cannot_safely_change_game_fi')
                ) from error
    for row in process_list:
        values = tuple(row)
        if len(values) < 2:
            continue
        # tasklist emits name,pid; ps emits pid,comm,args
        windows_row = not str(values[0]).isdigit()
        process_name, pid = (values[0], values[1]) if windows_row else (values[1], values[0])
        command = re.findall(r'''"[^"]*"|'[^']*'|\S+''', str(values[2])) if not windows_row and len(values) > 2 else []
        executable = command[0].strip("\"'")[:512] if command else ""
        name = supported_names.get(basename(process_name))
        matched_by = "process_name"
        if name is None and command:
            # the proton script outlives the game while it waits for wineserver, so only wine processes count
            if len(command) > 1 and basename(command[0]) in {"wine", "wine64", "wine-preloader", "wine64-preloader"}:
                command = command[1:]
            name = supported_names.get(basename(command[0]))
            matched_by = "launch_target"
        if name is None:
            continue
        rows.append({"name": name, "pid": str(pid), "process_name": str(process_name)[:128],
                     "command_executable": executable, "matched_by": matched_by})
    return tuple(rows)


def launch_doom_via_steam(*, opener: Callable[[str], object] | None = None) -> str:
    """Open DOOM Eternal through its Steam game URL."""
    (opener or webbrowser.open)(STEAM_GAME_URL)
    return STEAM_GAME_URL


def read_handshake_probe(path: Path) -> dict[str, object]:
    """Read native handshake marker without opening, changing, or creating it."""
    try:
        lines = path.read_text(encoding="utf-8", errors="strict").splitlines()[:32]
    except (OSError, UnicodeError):
        return {"status": "unavailable", "path": str(path)}
    values: dict[str, str] = {}
    for line in lines:
        if "=" in line:
            key, value = line.split("=", 1)
            if re.fullmatch(r"[A-Za-z0-9_]{1,40}", key):
                values[key] = value[:200]
    state = values.get("state", "")
    if state not in {"menu", "gameplay"}:
        return {"status": "invalid", "path": str(path)}
    try:
        epoch = int(values.get("epoch", "-1"))
    except ValueError:
        return {"status": "invalid", "path": str(path)}
    result: dict[str, object] = {"status": "ok", "state": state, "epoch": epoch}
    if state == "gameplay":
        result.update({"slot": values.get("slot", ""), "map_name": values.get("map_name", "")})
    return result


@dataclass(frozen=True)
class LaunchOptionPlan:
    previous: str
    proposed: str
    diff: str


class ValveVdfParser:
    """Small KeyValues parser used only for reading Steam configuration."""

    TOKEN = re.compile(r'\s*(?://[^\n]*(?:\n|$)|("(?:\\.|[^"\\])*")|([{}])|([^\s{}"]+))')

    @classmethod
    def parse(cls, text: str) -> dict[str, object]:
        tokens: list[str] = []
        position = 0
        while position < len(text):
            match = cls.TOKEN.match(text, position)
            if match is None:
                if not text[position:].strip():
                    break
                raise ValueError(player_text('launcher.platform', 'steam_settings_could_not_be_updated_select_help_save_support_rep'))
            position = match.end()
            quoted, brace, bare = match.groups()
            if quoted is not None:
                tokens.append(bytes(quoted[1:-1], "utf-8").decode("unicode_escape"))
            elif brace is not None:
                tokens.append(brace)
            elif bare is not None:
                tokens.append(bare)
        index = 0

        def mapping(expect_close: bool) -> dict[str, object]:
            nonlocal index
            result: dict[str, object] = {}
            while index < len(tokens):
                if tokens[index] == "}":
                    if not expect_close:
                        raise ValueError(player_text('launcher.platform', 'steam_settings_could_not_be_updated_select_help_save_support_rep'))
                    index += 1
                    return result
                key = tokens[index]
                index += 1
                if index >= len(tokens):
                    raise ValueError(player_text('launcher.platform', 'steam_settings_could_not_be_updated_select_help_save_support_rep'))
                if tokens[index] == "{":
                    index += 1
                    result[key] = mapping(True)
                else:
                    result[key] = tokens[index]
                    index += 1
            if expect_close:
                raise ValueError(player_text('launcher.platform', 'steam_settings_could_not_be_updated_select_help_save_support_rep'))
            return result

        document = mapping(False)
        if index != len(tokens):
            raise ValueError(player_text('launcher.platform', 'steam_settings_could_not_be_updated_select_help_save_support_rep'))
        return document


class SteamLaunchOptionsManager:
    """Detect and compose reversible options without writing Steam VDF files."""

    @staticmethod
    def _get(mapping: dict[str, object], key: str) -> object | None:
        return next((value for name, value in mapping.items() if name.casefold() == key.casefold()), None)

    @classmethod
    def detect(cls, localconfig: Path) -> str:
        try:
            text = localconfig.read_text(encoding="utf-8", errors="strict")
            document = ValveVdfParser.parse(text)
        except (OSError, UnicodeError, ValueError) as error:
            raise ValueError(player_text('launcher.platform', 'steam_settings_could_not_be_updated_select_help_save_support_rep')) from error
        current: object = document
        for key in ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps", DOOM_ETERNAL_APP_ID):
            if not isinstance(current, dict):
                return "%command%"
            current = cls._get(current, key)
            if current is None:
                return "%command%"
        if not isinstance(current, dict):
            return "%command%"
        options = cls._get(current, "LaunchOptions")
        return str(options) if options else "%command%"

    ASSIGNMENT = re.compile(r"(?:^|\s)WINEDLLOVERRIDES=(?:\"([^\"]*)\"|'([^']*)'|(\S+))")

    @classmethod
    def compose(cls, current: str) -> str:
        if "%command%" not in current:
            raise ValueError(player_text('launcher.platform', 'keep_exactly_one_command_in_the_launch_option'))
        match = cls.ASSIGNMENT.search(current)
        values: list[str] = []
        if match:
            raw = next(value for value in match.groups() if value is not None)
            values = [value for value in raw.split(";") if value]
            current = f"{current[: match.start()]} {current[match.end() :]}".strip()
        filtered = [value for value in values if value.split("=", 1)[0].casefold() != "xinput1_3"]
        filtered.append(REQUIRED_DLL_OVERRIDE)
        override = player_text('launcher.platform', 'winedlloverrides_value_0', value_0=f"{';'.join(filtered)}")
        return f"{override} {current}".strip()


    @classmethod
    def compose_bridge(cls, current: str, bridge_script: Path, *, delay: int = 5) -> str:
        """Place Linux bridge wrapper before %command% while preserving command arguments."""
        if delay < 0:
            raise ValueError(player_text('launcher.platform', 'enter_a_launch_delay_of_0_or_more'))
        try:
            tokens = shlex.split(current, posix=True)
        except ValueError as error:
            raise ValueError(player_text('launcher.platform', 'the_steam_launch_option_has_unmatched_quotation_marks_check_the')) from error
        if tokens.count("%command%") != 1:
            raise ValueError(player_text('launcher.platform', 'keep_exactly_one_command_in_the_launch_option'))
        script = str(bridge_script)
        override_tokens = [token for token in tokens if token.startswith("WINEDLLOVERRIDES=")]
        delay_tokens = [token for token in tokens if token.startswith("AP_CLIENT_DELAY=")]
        script_tokens = [token for token in tokens if Path(token).name == "run_bridge.sh"]

        def override_values() -> list[str]:
            values: list[str] = []
            for token in override_tokens:
                values.extend(value for value in token.split("=", 1)[1].split(";") if value)
            return values

        existing_overrides = override_values()
        required_present = any(
            value.split("=", 1)[0].casefold() == "xinput1_3"
            and value.split("=", 1)[1].casefold() == "n,b"
            for value in existing_overrides
            if "=" in value
        )
        if (
            len(override_tokens) == 1
            and len(delay_tokens) == 1
            and delay_tokens[0] == f"AP_CLIENT_DELAY={delay}"
            and script_tokens == [script]
            and tokens.index(script) < tokens.index("%command%")
            and required_present
        ):
            return current

        filtered_overrides = [
            value
            for value in existing_overrides
            if value.split("=", 1)[0].casefold() != "xinput1_3"
        ]
        filtered_overrides.append(REQUIRED_DLL_OVERRIDE)
        remaining = [
            token
            for token in tokens
            if not token.startswith((player_text('launcher.platform', 'winedlloverrides'), player_text('launcher.platform', 'ap_client_delay')))
            and Path(token).name != "run_bridge.sh"
        ]
        command_index = remaining.index("%command%")
        before_command = remaining[:command_index]
        after_command = remaining[command_index:]
        leading_environment: list[str] = []
        while before_command and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", before_command[0]):
            leading_environment.append(before_command.pop(0))
        rendered = [
            player_text('launcher.platform', 'winedlloverrides_value_0', value_0=f"{';'.join(filtered_overrides)}"),
            player_text('launcher.platform', 'ap_client_delay_value_0', value_0=f'{delay}'),
            *(shlex.quote(token) for token in leading_environment),
            shlex.quote(script),
            *(shlex.quote(token) for token in before_command),
            *(shlex.quote(token) for token in after_command),
        ]
        return " ".join(rendered)

    @classmethod
    def plan_bridge(cls, current: str, bridge_script: Path, *, delay: int = 5) -> LaunchOptionPlan:
        proposed = cls.compose_bridge(current, bridge_script, delay=delay)
        diff = "\n".join(
            difflib.unified_diff(
                [current + "\n"],
                [proposed + "\n"],
                fromfile="current",
                tofile="proposed",
            )
        )
        return LaunchOptionPlan(current, proposed, diff)

    @classmethod
    def plan(cls, current: str) -> LaunchOptionPlan:
        proposed = cls.compose(current)
        diff = "\n".join(
            difflib.unified_diff(
                [current + "\n"],
                [proposed + "\n"],
                fromfile="current",
                tofile="proposed",
            )
        )
        return LaunchOptionPlan(current, proposed, diff)

    @staticmethod
    def backup(plan: LaunchOptionPlan, backup_path: Path, *, consent: bool) -> str:
        if not consent:
            raise PermissionError(player_text('launcher.platform', 'steam_launch_option_change_cancelled'))
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"app_id": DOOM_ETERNAL_APP_ID, "launch_options": plan.previous}
        temporary = backup_path.with_suffix(f"{backup_path.suffix}.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        publish_file(temporary, backup_path, operation="steam_backup_publish")
        return plan.proposed

    @staticmethod
    def restore(backup_path: Path) -> str:
        payload = json.loads(backup_path.read_text(encoding="utf-8"))
        if payload.get("app_id") != DOOM_ETERNAL_APP_ID:
            raise ValueError(player_text('launcher.platform', 'steam_settings_could_not_be_updated_select_help_save_support_rep'))
        return str(payload["launch_options"])


def redact_secrets(text: str, secrets: Sequence[str] = ()) -> str:
    for secret in sorted((value for value in secrets if value), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?i)(password|passwd|token|secret|authorization)(\s*[=:]\s*)([^\s&]+)",
        r"\1\2[REDACTED]",
        text,
    )
    return text


AMMO_REFILL_BIND_COMMAND = "condump AP_REFILL_REQUEST.txt"
AMMO_HOTKEY_STATE_HEADER = 'AP_AMMO_REFILL_HOTKEY_V1'
AMMO_HOTKEY_STATE_FILENAME = "ammo_refill_hotkey.state"


def read_ap_hotkey_state(base: Path | None, *, special: bool = False) -> str | None:
    if base is None:
        return None
    name = "special_toggle_hotkey.state" if special else AMMO_HOTKEY_STATE_FILENAME
    header = 'AP_SPECIAL_TOGGLE_HOTKEY_V1' if special else AMMO_HOTKEY_STATE_HEADER
    path = base / "ap_queue" / name
    legacy = False
    if not path.exists():
        path = base / name
        legacy = True
        if not path.exists():
            return None
    with path.open("rb") as stream:
        raw = stream.read(128)
    tokens = raw.decode("ascii").split()
    if legacy and (len(tokens) != 2 or tokens[0] != header):
        return None
    if len(raw) >= 128 or (len(tokens) != 1 and (len(tokens) != 2 or tokens[0] != header)):
        raise ValueError(player_text('launcher.platform', 'steam_settings_could_not_be_updated_select_help_save_support_rep'))
    return tokens[-1]


def write_ap_hotkey_states(base: Path | None, refill: str, special: str) -> Path | None:
    """Publish validated shortcuts together, restoring both files on failure."""
    if base is None:
        return None
    queue = base / "ap_queue"
    queue.mkdir(parents=True, exist_ok=True)
    paths = [queue / AMMO_HOTKEY_STATE_FILENAME, queue / "special_toggle_hotkey.state"]
    previous = [path.read_bytes() if path.exists() else None for path in paths]
    try:
        for path, header, token in zip(paths, (AMMO_HOTKEY_STATE_HEADER, 'AP_SPECIAL_TOGGLE_HOTKEY_V1'), (refill, special), strict=False):
            _atomic_write_bytes(path, f"{header} {token or 'UNBOUND'}\n".encode("ascii"))
    except OSError:
        for path, content in zip(paths, previous, strict=False):
            if content is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write_bytes(path, content)
        raise
    for path, header in zip(paths, (AMMO_HOTKEY_STATE_HEADER, 'AP_SPECIAL_TOGGLE_HOTKEY_V1'), strict=False):
        legacy = base / path.name
        try:
            tokens = legacy.read_bytes().decode("ascii").split()
            if len(tokens) == 2 and tokens[0] == header:
                legacy.unlink()
        except (OSError, UnicodeError):
            pass
    return paths[0]


def resolve_doom_config_path(config: Mapping[str, object]) -> Path | None:
    """Resolve authoritative DOOMEternalConfig.cfg path from configured or discovered save paths."""
    save_val = config.get("save_games_dir")
    if save_val:
        try:
            p = Path(str(save_val)).expanduser().resolve()
            cfg = p / "DOOMEternalConfig.cfg"
            if cfg.is_file():
                return cfg
            if p.is_dir():
                return cfg
        except (OSError, TypeError, ValueError):
            pass

    game_root_val = config.get("game_root") or config.get("doom_base_dir")
    if game_root_val:
        try:
            root = Path(str(game_root_val)).expanduser().resolve()
            for parent in (root, *root.parents):
                if parent.name.casefold() == "steamapps":
                    proton_path = (
                        parent.parent
                        / player_text('launcher.doctor', 'steamapps_compatdata_782330_pfx_drive_c_users_steamuser_saved_ga')
                    )
                    cfg = proton_path / "DOOMEternalConfig.cfg"
                    if cfg.is_file():
                        return cfg
                    if proton_path.is_dir():
                        return cfg
                    break
        except (OSError, TypeError, ValueError):
            pass

    try:
        win_path = Path.home() / 'Saved Games' / 'id Software' / "DOOMEternal" / "base"
        cfg = win_path / "DOOMEternalConfig.cfg"
        if cfg.is_file():
            return cfg
        if win_path.is_dir():
            return cfg
    except (OSError, TypeError, ValueError):
        pass

    if save_val:
        try:
            return Path(str(save_val)).expanduser().resolve() / "DOOMEternalConfig.cfg"
        except (OSError, TypeError, ValueError):
            pass
    return None


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path = path.resolve()
    temporary = path.with_name(f".{path.name}.doomeap_{os.getpid()}_{hashlib.sha256(content).hexdigest()[:8]}.tmp")
    try:
        with open(temporary, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        publish_file(temporary, path, operation="atomic_bytes_publish")
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def cleanup_stale_doom_config_bind(config: Mapping[str, object], *, is_game_running: bool = False) -> bool:
    """Remove only stale DoomEAP-owned condump bind from player's DOOMEternalConfig.cfg when game is closed."""
    if is_game_running:
        return False
    cfg_path = resolve_doom_config_path(config)
    if cfg_path is None or not cfg_path.is_file():
        return False
    try:
        raw = cfg_path.read_bytes()
    except OSError:
        return False

    bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
    raw_content = raw[len(bom):]
    try:
        text = raw_content.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        try:
            text = raw_content.decode("latin-1")
            encoding = "latin-1"
        except UnicodeDecodeError:
            return False

    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=False)
    modified = False
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if (stripped.startswith(("bind ", "bindSecondary "))) and AMMO_REFILL_BIND_COMMAND in stripped:
            modified = True
            continue
        new_lines.append(line)

    if modified:
        output_text = newline.join(new_lines) + newline
        new_bytes = bom + output_text.encode(encoding)
        try:
            _atomic_write_bytes(cfg_path, new_bytes)
            return True
        except OSError:
            return False
    return False


def cleanup_legacy_doomeap_cfg(base_dir: Path | None) -> bool:
    """Remove legacy doomeap.cfg when safely recognized as DoomEAP-owned."""
    if base_dir is None:
        return False
    target = base_dir / "doomeap.cfg"
    if not target.is_file():
        return False
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        for line in lines:
            if line.startswith(("//", "#")):
                continue
            if line.startswith("unbind "):
                continue
            if AMMO_REFILL_BIND_COMMAND in line:
                continue
            return False
        target.unlink()
        return True
    except OSError:
        return False
