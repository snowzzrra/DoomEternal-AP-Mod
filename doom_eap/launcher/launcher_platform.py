from __future__ import annotations

import csv
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import ClassVar

from doom_eap.strings import PlayerError


@dataclass(frozen=True)
class LauncherUserPaths:
    """Per-user launcher roots for configuration, state, cache, and logs."""

    config_dir: Path
    state_dir: Path
    data_dir: Path


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
        detail = f"{type(cause).__name__}: {cause}" if cause is not None else 'unknown error'
        message = (
            'Windows file publication failed: operation={value_0} source={value_1} destination={value_2} errno={value_3} winerror={value_4} attempts={value_5} ({value_6})'.format(value_0=f'{self.operation}', value_1=f'{self.source_path}', value_2=f'{self.destination_path}', value_3=f'{self.cause_errno}', value_4=f'{self.cause_winerror}', value_5=f'{self.attempt_count}', value_6=f'{detail}')
        )
        super().__init__(message)
        # setting errno/filename makes OSError.__str__ discard the detailed message
        if cause is not None:
            self.__cause__ = cause


def _is_transient_windows_publish_error(error: OSError) -> bool:
    """Classify Windows sharing/access failures safe for bounded publication retry."""
    if getattr(error, "winerror", None) in _WINDOWS_TRANSIENT_WINERRORS:
        return True
    return isinstance(error, PermissionError) and getattr(error, "errno", None) == 13


def publish_file(source: Path | str, destination: Path | str, *, operation: str, timeout: float=3.0) -> Path:
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
        error = OSError('Windows dependency cache lock creation failed: name={value_0} winerror={value_1}'.format(value_0=f'{name}', value_1=f'{code}'))
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
            'Windows dependency cache lock timeout: name={value_0} timeout_ms={value_1}'.format(value_0=f'{name}', value_1=f'{timeout_ms}')
        )
    code = kernel32.GetLastError()
    error = OSError('Windows dependency cache lock wait failed: name={value_0} winerror={value_1}'.format(value_0=f'{name}', value_1=f'{code}'))
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


def launcher_user_paths(
    *,
    environment: dict[str, str] | None = None,
    platform_name: str | None = None,
) -> LauncherUserPaths:
    env = os.environ if environment is None else environment
    platform_name = platform_name or ("windows" if os.name == "nt" else "linux")
    if platform_name == "windows":
        root = Path(env.get("LOCALAPPDATA") or (Path.home() / "AppData/Local"))
        root = root / 'Doom Eternal Archipelago'
        return LauncherUserPaths(root / "config", root / "state", root / "data")
    root_name = "doom-eternal-archipelago"
    return LauncherUserPaths(
        Path(env.get('XDG_CONFIG_HOME') or Path.home() / '.config') / root_name,
        Path(env.get('XDG_STATE_HOME') or Path.home() / '.local/state') / root_name,
        Path(env.get('XDG_DATA_HOME') or Path.home() / '.local/share') / root_name,
    )


def _migrate_launcher_file(source, destination, relative):
    if relative.name == "launcher.json" and len(relative.parts) == 1:
        try:
            document = json.loads(source.read_text(encoding="utf-8"))
            if isinstance(document, dict):
                document.pop("password", None)
                destination.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                return
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    shutil.copy2(source, destination)


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
        _migrate_launcher_file(source, destination, relative)


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
        raise ValueError('Choose the folder containing DOOMEternalx64vk.exe.')
    return root


def validate_save_directory(path: Path) -> Path:
    root = path.expanduser().resolve()
    if not root.is_dir():
        raise ValueError('Choose an existing save folder ending in id Software/DOOMEternal/base.')
    if not is_saved_games_base_shape(root):
        raise ValueError('Choose an existing save folder ending in id Software/DOOMEternal/base.')
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


def known_folder_saved_games(*, platform: str | None=None, environ: Mapping[str, str] | None=None) -> Path | None:
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
                ('Data1', wintypes.DWORD), ('Data2', wintypes.WORD), ('Data3', wintypes.WORD),
                ('Data4', wintypes.BYTE * 8),
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
                / 'steamapps/compatdata/782330/pfx/drive_c/users/steamuser/Saved Games'
                / SAVED_GAMES_PROVIDER
                / SAVED_GAMES_TITLE
                / SAVED_GAMES_LEAF
            )
    return None


def _fresh_writer_markers(root, names, cutoff, live):
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
    return _fresh_writer_markers(root, names, cutoff, live)


@dataclass(frozen=True)
class SavedGamesSelection:
    path: Path | None
    source: str
    reason: str
    repaired: bool = False
    previous_path: str | None = None
    rejected: tuple[dict[str, object], ...] = ()
    live_evidence: tuple[str, ...] = ()


def _forbidden_save_verdict(configured_forbidden, configured_path, app_dir, client_dir, configured_verdict, target):
    if configured_forbidden:
        if str(app_dir) and _is_within_directory(configured_path, str(app_dir), platform=target):
            configured_verdict = "application_directory_not_saved_games"
        elif str(client_dir) and _is_within_directory(configured_path, str(client_dir), platform=target):
            configured_verdict = "application_directory_not_saved_games"
        else:
            configured_verdict = "game_installation_not_saved_games"
    return configured_verdict


def _configured_save_path(configured, target, forbidden, app_dir, client_dir):
    configured_text = str(configured) if configured is not None and str(configured) else None
    configured_path = Path(configured_text).expanduser() if configured_text else None
    configured_verdict = saved_games_shape_verdict(configured_path, platform=target) if configured_path else "unconfigured"
    configured_forbidden = bool(
        configured_path is not None
        and any(_is_within_directory(configured_path, scope, platform=target) for scope in forbidden)
    )
    configured_verdict = _forbidden_save_verdict(configured_forbidden, configured_path, app_dir, client_dir, configured_verdict, target)
    return configured_text, configured_path, configured_verdict, configured_forbidden


def _configured_save_exists(configured_path, configured_verdict, configured_forbidden):
    configured_usable = (
        configured_path is not None
        and configured_verdict == "valid_saved_games_base"
        and not configured_forbidden
    )
    try:
        configured_exists = bool(configured_path is not None and configured_path.is_dir())
    except (OSError, ValueError, RuntimeError):
        configured_exists = False
    return configured_usable and configured_exists


def _ordered_save_candidates(known_base, candidates, target, rejected):
    ordered: list[Path] = []
    seen: set[str] = set()
    for raw in ((known_base,) if known_base is not None else ()) + tuple(candidates):
        if raw is None or not str(raw):
            continue
        try:
            candidate = Path(str(raw)).expanduser()
        except (OSError, TypeError, ValueError, RuntimeError):
            rejected.append({"path": str(raw), "reason": "unparseable"})
            continue
        key = _saved_games_norm(candidate, platform=target)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(candidate)
    return ordered


def _viable_save_candidates(ordered, target, forbidden, now, rejected):
    viable: list[Path] = []
    live: dict[str, tuple[str, ...]] = {}
    for candidate in ordered:
        verdict = saved_games_shape_verdict(candidate, platform=target)
        if verdict != "valid_saved_games_base":
            rejected.append({"path": str(candidate), "reason": verdict})
            continue
        if any(_is_within_directory(candidate, scope, platform=target) for scope in forbidden):
            rejected.append({"path": str(candidate), "reason": "application_directory_not_saved_games"})
            continue
        viable.append(candidate)
        markers = saved_games_live_markers(candidate, now=now)
        if markers:
            live[_saved_games_norm(candidate, platform=target)] = markers
    return viable, live


def _unresolved_save_selection(viable, configured_text, rejected):
    if not viable:
        reason = "no_marker_evidence" if configured_text is None else "stale_config_no_viable_candidate"
        if len(rejected) > 1 and configured_text is not None:
            reason = "ambiguous" if any(str(item.get("path")) != configured_text for item in rejected) else reason
        return SavedGamesSelection(path=None, source="unresolved", reason=reason, rejected=tuple(rejected))
    return SavedGamesSelection(path=None, source="unresolved", reason="ambiguous", rejected=tuple(rejected))


def _known_viable_save(known_base, target, viable):
    known_key = _saved_games_norm(Path(str(known_base)).expanduser(), platform=target) if known_base else None
    known_viable = next((item for item in viable if _saved_games_norm(item, platform=target) == known_key), None) if known_key else None
    return known_viable


def _select_viable_save(known_base, target, viable, live, configured_text, rejected, game_running):
    known_viable = _known_viable_save(known_base, target, viable)
    if known_viable is not None:
        return SavedGamesSelection(
            path=known_viable, source='canonical_saved_games_base',
            reason='canonical Saved Games base selected over invalid configured path',
            repaired=configured_text is not None, previous_path=configured_text, rejected=tuple(rejected),
            live_evidence=live.get(_saved_games_norm(known_viable, platform=target), ()),
        )
    live_viable = [item for item in viable if _saved_games_norm(item, platform=target) in live]
    if len(live_viable) == 1 and (len(viable) == 1 or game_running):
        only = live_viable[0]
        return SavedGamesSelection(
            path=only, source='live_writer_evidence',
            reason='single live AP writer candidate selected over invalid configured path',
            repaired=configured_text is not None, previous_path=configured_text, rejected=tuple(rejected),
            live_evidence=live.get(_saved_games_norm(only, platform=target), ()),
        )
    if len(viable) == 1 and configured_text is None:
        only = viable[0]
        return SavedGamesSelection(
            path=only, source='single_viable_candidate', reason='no configured path; single viable candidate selected',
            rejected=tuple(rejected), live_evidence=live.get(_saved_games_norm(only, platform=target), ()),
        )
    return _unresolved_save_selection(viable, configured_text, rejected)


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
    """Prefer valid configured saves, the known folder, then live writer evidence."""
    target = platform if platform is not None else os.name
    forbidden = [
        str(scope)
        for scope in (app_dir, client_dir, game_root)
        if scope is not None and str(scope)
    ]
    rejected: list[dict[str, object]] = []

    def _reject(candidate: object, reason: str) -> None:
        rejected.append({"path": str(candidate), "reason": reason})

    configured_text, configured_path, configured_verdict, configured_forbidden = _configured_save_path(configured, target, forbidden, app_dir, client_dir)
    if configured_text is not None:
        _reject(configured_text, configured_verdict if configured_verdict != "valid_saved_games_base" or configured_forbidden else "configured")
    configured_exists = _configured_save_exists(configured_path, configured_verdict, configured_forbidden)
    if configured_exists:
        return SavedGamesSelection(
            path=configured_path, source='configured_saved_games_dir',
            reason='configured Saved Games base has valid shape and exists', rejected=tuple(rejected),
        )

    ordered = _ordered_save_candidates(known_base, candidates, target, rejected)

    viable, live = _viable_save_candidates(ordered, target, forbidden, now, rejected)

    return _select_viable_save(known_base, target, viable, live, configured_text, rejected, game_running)


DOOM_ETERNAL_APP_ID = "782330"
REQUIRED_DLL_OVERRIDE = "msimg32=n,b"
STEAM_GAME_URL = f"steam://rungameid/{DOOM_ETERNAL_APP_ID}"

WINDOWS_MANUAL_INSTALL_GUIDE_URL = (
    "https://github.com/snowzzrra/DoomEternal-AP-Mod/blob/main/docs/INSTALL.md#windows-manual-installation"
)


@dataclass(frozen=True)
class SteamInstallation:
    library_root: Path
    game_root: Path
    manifest: Path


def query_windows_registry_steam_roots(registry_query: Callable[[], Sequence[Path]] | None=None) -> tuple[Path, ...]:
    """Discover installed Steam client roots from authoritative Windows registry locations."""
    if registry_query is not None:
        return tuple(dict.fromkeys(registry_query()))
    if os.name != "nt":
        return ()
    roots: list[Path] = []
    try:
        import winreg

        registry_values = (
            (winreg.HKEY_CURRENT_USER, 'Software\\Valve\\Steam', 'SteamPath'),
            (winreg.HKEY_LOCAL_MACHINE, 'SOFTWARE\\WOW6432Node\\Valve\\Steam', 'InstallPath'),
            (winreg.HKEY_LOCAL_MACHINE, 'SOFTWARE\\Valve\\Steam', 'InstallPath'),
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
            self.default_roots(platform_name=platform_name, registry_query=registry_query, environment=environment)
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
            for variable in ('PROGRAMFILES(X86)', 'PROGRAMFILES', 'PROGRAMW6432'):
                value = env.get(variable)
                if value:
                    roots.append(Path(value) / "Steam")
        else:
            home = Path.home()
            roots.extend((home / '.local/share/Steam', home / '.steam/steam', home / '.steam/root', home / '.steam'))
        return tuple(dict.fromkeys(roots))

    @staticmethod
    def _vdf_library_entries(top_val, parsed_paths):
        for item_key, item_val in top_val.items():
            if isinstance(item_val, dict):
                for prop_key, prop_val in item_val.items():
                    if prop_key.casefold() == "path" and isinstance(prop_val, str):
                        parsed_paths.append(Path(prop_val))
            elif isinstance(item_val, str) and item_key.isdigit():
                parsed_paths.append(Path(item_val))

    @staticmethod
    def _vdf_library_paths(text, parsed_paths):
        document = ValveVdfParser.parse(text)
        for _top_key, top_val in document.items():
            if isinstance(top_val, dict):
                SteamInstallationLocator._vdf_library_entries(top_val, parsed_paths)

    @staticmethod
    def _declared_libraries(steam_root: Path) -> tuple[Path, ...]:
        configuration = steam_root / "steamapps/libraryfolders.vdf"
        libraries = [steam_root]
        if configuration.is_file():
            text = configuration.read_text(encoding="utf-8", errors="replace")
            parsed_paths: list[Path] = []
            try:
                SteamInstallationLocator._vdf_library_paths(text, parsed_paths)
            except ValueError:
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
    def _vdf_install_directory(text, installdir):
        document = ValveVdfParser.parse(text)
        for _top_key, top_val in document.items():
            if isinstance(top_val, dict):
                for prop_key, prop_val in top_val.items():
                    if prop_key.casefold() == "installdir" and isinstance(prop_val, str):
                        installdir = prop_val
                        break
        return installdir

    @staticmethod
    def _installation(library: Path) -> SteamInstallation | None:
        manifest = library / "steamapps" / SteamInstallationLocator.APP_MANIFEST
        if not manifest.is_file():
            return None
        text = manifest.read_text(encoding="utf-8", errors="replace")
        installdir: str | None = None
        try:
            installdir = SteamInstallationLocator._vdf_install_directory(text, installdir)
        except ValueError:
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

    def _inspect_library_manifest(self, manifest_exists, library, manifest_path, check, installations, any_manifest_exists, any_installdir_found):
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
                    check["error"] = 'installdir missing from manifest'
        return any_manifest_exists, any_installdir_found

    def _inspect_libraries(self, declared, installations, root_info, any_manifest_exists, any_installdir_found):
        lib_checks: list[dict[str, object]] = []
        for library in declared:
            manifest_path = library / "steamapps" / self.APP_MANIFEST
            manifest_exists = manifest_path.is_file()
            check: dict[str, object] = {
                'library': str(library), 'manifest_path': str(manifest_path), 'manifest_exists': manifest_exists,
            }
            any_manifest_exists, any_installdir_found = self._inspect_library_manifest(manifest_exists, library, manifest_path, check, installations, any_manifest_exists, any_installdir_found)
            lib_checks.append(check)

        root_info["checks"] = lib_checks
        return any_manifest_exists, any_installdir_found

    def _inspect_steam_roots(self, installations, trace):
        any_root_exists = False
        any_manifest_exists = False
        any_installdir_found = False
        for root in self.candidate_roots:
            root_exists = root.is_dir()
            root_info: dict[str, object] = {'root': str(root), 'exists': root_exists}
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

            any_manifest_exists, any_installdir_found = self._inspect_libraries(declared, installations, root_info, any_manifest_exists, any_installdir_found)
            trace.append(root_info)

        return any_root_exists, any_manifest_exists, any_installdir_found

    def _discovery_result(self, installations, trace, any_root_exists, any_manifest_exists, any_installdir_found):
        inst_roots = tuple(str(item.game_root) for item in installations)
        if len(inst_roots) == 1:
            sentinel = DiscoverySentinel(
                DiscoveryStatus.FOUND.value, path=inst_roots[0], reason='Game folder found.', trace=tuple(trace),
            )
        elif len(inst_roots) > 1:
            sentinel = DiscoverySentinel(
                DiscoveryStatus.AMBIGUOUS.value, candidates=inst_roots,
                reason='More than one game folder was found. Choose the one used by Steam.', trace=tuple(trace),
            )
        elif not any_root_exists:
            sentinel = DiscoverySentinel(
                DiscoveryStatus.NOT_FOUND.value, reason='Steam was not found. Choose your DOOM Eternal folder.',
                trace=tuple(trace),
            )
        elif not any_manifest_exists:
            sentinel = DiscoverySentinel(
                DiscoveryStatus.NOT_FOUND.value,
                reason="DOOM Eternal was not found in Steam's libraries. Choose its game folder.", trace=tuple(trace),
            )
        elif any_installdir_found and not installations:
            sentinel = DiscoverySentinel(
                DiscoveryStatus.INVALID.value, reason='Game files are missing. Verify DOOM Eternal in Steam.',
                trace=tuple(trace),
            )
        else:
            sentinel = DiscoverySentinel(
                DiscoveryStatus.NOT_FOUND.value, reason='Steam was not found. Choose the game folder.',
                trace=tuple(trace),
            )

        return tuple(installations), sentinel

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
            trace.append({'source': 'manual', 'path': str(game_root), 'valid': valid})
            if not valid:
                return (), DiscoverySentinel(
                    DiscoveryStatus.INVALID.value,
                    reason='invalid DOOM Eternal installation: {value_0}'.format(value_0=f'{game_root}'),
                    trace=tuple(trace),
                )
            installations.append(SteamInstallation(game_root.parent, game_root, Path()))

        any_root_exists, any_manifest_exists, any_installdir_found = self._inspect_steam_roots(installations, trace)

        return self._discovery_result(installations, trace, any_root_exists, any_manifest_exists, any_installdir_found)

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
                    'library_root': str(item.library_root), 'game_root': str(item.game_root),
                    'manifest': str(item.manifest),
                }
                for item in installations
            ],
        }


class ProcessInspectionError(RuntimeError):
    """The launcher cannot establish whether DOOM Eternal is closed."""


def require_doom_closed(operation: str) -> None:
    processes = detect_doom_processes(include_clients=True)
    clients = [item for item in processes if str(item["name"]).startswith("ap_client")]
    processes = [item for item in processes if item not in clients]
    if processes:
        detected = ", ".join('{value_0} (PID {value_1})'.format(value_0=f"{item['name']}", value_1=f"{item['pid']}") for item in processes)
        error = RuntimeError('Close DOOM Eternal before {value_0}. Detected: {value_1}'.format(value_0=f'{operation}', value_1=f'{detected}'))
        error.processes = processes
        raise error
    if clients:
        raise PlayerError("launcher.setup", "another_session_running")


def _running_processes():
    windows = os.name == "nt"
    command = ["tasklist", "/fo", "csv", "/nh"] if windows else ["ps", "-eo", "pid=,comm=,args="]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=3, check=False,
                                   creationflags=subprocess.CREATE_NO_WINDOW if windows else 0)
        completed.check_returncode()
        if windows:
            return tuple((row[0], row[1]) for row in csv.reader(completed.stdout.splitlines()) if row)
        return tuple(tuple(line.split(None, 2)) for line in completed.stdout.splitlines() if line.strip())
    except (OSError, subprocess.SubprocessError, IndexError, UnicodeError) as error:
        raise ProcessInspectionError(
            'Could not inspect running processes. Cannot safely change game files; close DOOM Eternal and retry.'
        ) from error


def _process_launch_target(name, command, supported_names, basename, matched_by):
    if name is None and command:
        # the proton script outlives the game while it waits for wineserver, so only wine processes count
        if len(command) > 1 and basename(command[0]) in {"wine", "wine64", "wine-preloader", "wine64-preloader"}:
            command = command[1:]
        name = supported_names.get(basename(command[0]))
        matched_by = "launch_target"
    return name, matched_by


def _matching_processes(process_list, supported_names, basename, rows):
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
        name, matched_by = _process_launch_target(name, command, supported_names, basename, matched_by)
        if name is None:
            continue
        rows.append({"name": name, "pid": str(pid), "process_name": str(process_name)[:128],
                     "command_executable": executable, "matched_by": matched_by})


def detect_doom_processes(
    *,
    process_list: Sequence[Sequence[str]] | None = None,
    include_clients: bool = False,
) -> tuple[dict[str, object], ...]:
    """Return bounded process facts suitable for diagnostics."""
    supported_names = {
        'doometernalx64vk.exe': 'doometernalx64vk.exe', 'doometernalx64vk': 'doometernalx64vk',
        'doometernalx64v': 'doometernalx64vk.exe',
    }
    if include_clients:
        supported_names.update({"ap_client.exe": "ap_client.exe", "ap_client": "ap_client"})

    def basename(value: object) -> str:
        text = str(value).strip().strip("\"'")
        return text.replace("\\", "/").rsplit("/", 1)[-1].casefold()

    rows: list[dict[str, object]] = []
    if process_list is None:
        process_list = _running_processes()
    _matching_processes(process_list, supported_names, basename, rows)
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
    def _tokenize(cls, text):
        tokens: list[str] = []
        position = 0
        while position < len(text):
            match = cls.TOKEN.match(text, position)
            if match is None:
                if not text[position:].strip():
                    break
                raise ValueError('Steam settings could not be updated. Select Help -> SAVE SUPPORT REPORT.')
            position = match.end()
            quoted, brace, bare = match.groups()
            if quoted is not None:
                tokens.append(bytes(quoted[1:-1], "utf-8").decode("unicode_escape"))
            elif brace is not None:
                tokens.append(brace)
            elif bare is not None:
                tokens.append(bare)
        return tokens

    @classmethod
    def parse(cls, text: str) -> dict[str, object]:
        tokens = cls._tokenize(text)
        document, index = cls._mapping(tokens, 0, False)
        if index != len(tokens):
            raise ValueError('Steam settings could not be updated. Select Help -> SAVE SUPPORT REPORT.')
        return document

    @classmethod
    def _mapping(cls, tokens, index, expect_close):
        result = {}
        while index < len(tokens):
            if tokens[index] == "}":
                if not expect_close:
                    raise ValueError('Steam settings could not be updated. Select Help -> SAVE SUPPORT REPORT.')
                return result, index + 1
            key = tokens[index]
            index += 1
            if index >= len(tokens):
                raise ValueError('Steam settings could not be updated. Select Help -> SAVE SUPPORT REPORT.')
            if tokens[index] == "{":
                result[key], index = cls._mapping(tokens, index + 1, True)
            else:
                result[key] = tokens[index]
                index += 1
        if expect_close:
            raise ValueError('Steam settings could not be updated. Select Help -> SAVE SUPPORT REPORT.')
        return result, index


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
            raise ValueError('Steam settings could not be updated. Select Help -> SAVE SUPPORT REPORT.') from error
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
            raise ValueError('Keep exactly one %command% in the launch option.')
        match = cls.ASSIGNMENT.search(current)
        values: list[str] = []
        if match:
            raw = next(value for value in match.groups() if value is not None)
            values = [value for value in raw.split(";") if value]
            current = f"{current[: match.start()]} {current[match.end() :]}".strip()
        filtered = [value for value in values if value.split("=", 1)[0].casefold() != "xinput1_3"]
        filtered.append(REQUIRED_DLL_OVERRIDE)
        override = 'WINEDLLOVERRIDES="{value_0}"'.format(value_0=f"{';'.join(filtered)}")
        return f"{override} {current}".strip()


    @classmethod
    def _bridge_environment(cls, bridge_script, tokens):
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
        return script, override_tokens, delay_tokens, script_tokens, existing_overrides

    @classmethod
    def _bridge_option_matches(cls, existing_overrides, override_tokens, delay_tokens, script_tokens, delay, script, tokens, current):
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
            return True
        return False

    @classmethod
    def _bridge_remaining_tokens(cls, tokens):
        remaining = [
            token
            for token in tokens
            if not token.startswith(('WINEDLLOVERRIDES=', 'AP_CLIENT_DELAY='))
            and Path(token).name != "run_bridge.sh"
        ]
        return remaining

    @classmethod
    def _render_bridge_option(cls, existing_overrides, tokens, delay, script):
        filtered_overrides = [
            value
            for value in existing_overrides
            if value.split("=", 1)[0].casefold() != "xinput1_3"
        ]
        filtered_overrides.append(REQUIRED_DLL_OVERRIDE)
        remaining = cls._bridge_remaining_tokens(tokens)
        command_index = remaining.index("%command%")
        before_command = remaining[:command_index]
        after_command = remaining[command_index:]
        leading_environment: list[str] = []
        while before_command and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", before_command[0]):
            leading_environment.append(before_command.pop(0))
        rendered = [
            'WINEDLLOVERRIDES="{value_0}"'.format(value_0=f"{';'.join(filtered_overrides)}"),
            'AP_CLIENT_DELAY={value_0}'.format(value_0=f'{delay}'),
            *(shlex.quote(token) for token in leading_environment), shlex.quote(script),
            *(shlex.quote(token) for token in before_command), *(shlex.quote(token) for token in after_command),
        ]
        return " ".join(rendered)

    @classmethod
    def compose_bridge(cls, current: str, bridge_script: Path, *, delay: int = 5) -> str:
        """Place Linux bridge wrapper before %command% while preserving command arguments."""
        if delay < 0:
            raise ValueError('Enter a launch delay of 0 or more.')
        try:
            tokens = shlex.split(current, posix=True)
        except ValueError as error:
            raise ValueError('The Steam launch option has unmatched quotation marks. Check the option or select Help -> SAVE SUPPORT REPORT.') from error
        if tokens.count("%command%") != 1:
            raise ValueError('Keep exactly one %command% in the launch option.')
        script, override_tokens, delay_tokens, script_tokens, existing_overrides = cls._bridge_environment(bridge_script, tokens)
        if cls._bridge_option_matches(existing_overrides, override_tokens, delay_tokens, script_tokens, delay, script, tokens, current):
            return current

        return cls._render_bridge_option(existing_overrides, tokens, delay, script)

    @classmethod
    def plan_bridge(cls, current: str, bridge_script: Path, *, delay: int = 5) -> LaunchOptionPlan:
        proposed = cls.compose_bridge(current, bridge_script, delay=delay)
        diff = "\n".join(
            difflib.unified_diff([current + '\n'], [proposed + '\n'], fromfile='current', tofile='proposed')
        )
        return LaunchOptionPlan(current, proposed, diff)

    @classmethod
    def plan(cls, current: str) -> LaunchOptionPlan:
        proposed = cls.compose(current)
        diff = "\n".join(
            difflib.unified_diff([current + '\n'], [proposed + '\n'], fromfile='current', tofile='proposed')
        )
        return LaunchOptionPlan(current, proposed, diff)

    @staticmethod
    def backup(plan: LaunchOptionPlan, backup_path: Path, *, consent: bool) -> str:
        if not consent:
            raise PermissionError('Steam launch option change cancelled.')
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
            raise ValueError('Steam settings could not be updated. Select Help -> SAVE SUPPORT REPORT.')
        return str(payload["launch_options"])


def redact_secrets(text: str, secrets: Sequence[str] = ()) -> str:
    for secret in sorted((value for value in secrets if value), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub('(?i)(password|passwd|token|secret|authorization)(\\s*[=:]\\s*)([^\\s&]+)', '\\1\\2[REDACTED]', text)
    return text


AMMO_REFILL_BIND_COMMAND = "condump AP_REFILL_REQUEST.txt"
AMMO_HOTKEY_STATE_HEADER = 'AP_AMMO_REFILL_HOTKEY_V1'
AMMO_HOTKEY_STATE_FILENAME = "ammo_refill_hotkey.state"


def _decode_hotkey_state(raw, legacy, header):
    tokens = raw.decode("ascii").split()
    if legacy and (len(tokens) != 2 or tokens[0] != header):
        return None
    if len(raw) >= 128 or (len(tokens) != 1 and (len(tokens) != 2 or tokens[0] != header)):
        raise ValueError('Steam settings could not be updated. Select Help -> SAVE SUPPORT REPORT.')
    return tokens[-1]


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
    return _decode_hotkey_state(raw, legacy, header)


def _remove_legacy_hotkeys(paths, base):
    for path, header in zip(paths, (AMMO_HOTKEY_STATE_HEADER, 'AP_SPECIAL_TOGGLE_HOTKEY_V1'), strict=False):
        legacy = base / path.name
        try:
            tokens = legacy.read_bytes().decode("ascii").split()
            if len(tokens) == 2 and tokens[0] == header:
                legacy.unlink()
        except (OSError, UnicodeError):
            pass


def _restore_hotkeys(paths, previous):
    for path, content in zip(paths, previous, strict=False):
        if content is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write_bytes(path, content)


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
        _restore_hotkeys(paths, previous)
        raise
    _remove_legacy_hotkeys(paths, base)
    return paths[0]


def _steam_proton_config(root):
    for parent in (root, *root.parents):
        if parent.name.casefold() == "steamapps":
            proton_path = (
                parent.parent
                / 'steamapps/compatdata/782330/pfx/drive_c/users/steamuser/Saved Games/id Software/DOOMEternal/base'
            )
            cfg = proton_path / "DOOMEternalConfig.cfg"
            if cfg.is_file():
                return cfg
            if proton_path.is_dir():
                return cfg
            break


def _proton_config_path(game_root_val):
    if game_root_val:
        try:
            root = Path(str(game_root_val)).expanduser().resolve()
            return _steam_proton_config(root)
        except (OSError, TypeError, ValueError):
            pass


def _configured_doom_config(save_val):
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


def resolve_doom_config_path(config: Mapping[str, object]) -> Path | None:
    """Resolve authoritative DOOMEternalConfig.cfg path from configured or discovered save paths."""
    save_val = config.get("save_games_dir")
    configured_path = _configured_doom_config(save_val)
    if configured_path is not None:
        return configured_path

    game_root_val = config.get("game_root") or config.get("doom_base_dir")
    proton_config = _proton_config_path(game_root_val)
    if proton_config is not None:
        return proton_config

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


def _strip_condump_bind(raw):
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

    return bom, text, encoding, newline, modified, new_lines


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

    bom, text, encoding, newline, modified, new_lines = _strip_condump_bind(raw)
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
