from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from doom_eap.contracts.materialization import SUPPORT_RUNE_IDS
from doom_eap.runtime.connection_diagnostics import timed_sync_call
from doom_eap.runtime.inventory import CLIENT_STATE_VERSION, ClientStateStore, atomic_write_json, migrate_client_state
from doom_eap.strings import text as player_text

MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR if (MODULE_DIR / "data").is_dir() else Path(__file__).resolve().parents[2]
APPLICATION_DIR = Path(
    os.environ.get("DOOM_AP_APPLICATION_DIR", REPO_ROOT)
).resolve()
CONFIG_FILE = Path(
    os.environ.get("DOOM_AP_CONFIG_FILE", APPLICATION_DIR / "ap_config.json")
).expanduser().resolve()


def resolve_bridge_identity(
    application_dir: Path | None = None,
    repo_root: Path | None = None,
    module_file: Path | None = None,
    is_frozen: bool | None = None,
) -> tuple[Path, str, str]:
    """Resolve bridge client file reference, deterministic SHA-256, and revision string.

    In source/development mode and unpacked release packages, hashes physical Python source bytes.
    In frozen standalone mode (PyInstaller), computes deterministic identity without reading
    non-materialized source files.
    """
    frozen = bool(getattr(sys, "frozen", False)) if is_frozen is None else is_frozen
    app_dir = (application_dir or (Path(sys.executable).resolve().parent if frozen else Path(__file__).resolve().parent)).resolve()
    root = (repo_root or (Path(__file__).resolve().parent if (Path(__file__).resolve().parent / "data").is_dir() else Path(__file__).resolve().parents[2])).resolve()
    mod_file = (module_file or Path(__file__).with_name("bridge_client.py")).resolve()

    candidate = app_dir / "bridge_client.py"
    if candidate.is_file():
        sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()
        return candidate, sha256, f"mission-unified-{sha256[:12]}"

    if mod_file.is_file():
        sha256 = hashlib.sha256(mod_file.read_bytes()).hexdigest()
        return mod_file, sha256, f"mission-unified-{sha256[:12]}"

    identity = _packaged_bridge_identity(app_dir, root, frozen, mod_file)
    if identity is not None:
        return identity

    return _fallback_bridge_identity(root, frozen, mod_file)


def doom_process_identity():
    """Return current game PID plus process-start identity when available."""
    if os.name == "nt":
        return _windows_doom_identity()
    try:

        owner = APPLICATION_DIR / "APSessionOwner.exe"
        rows = windows_processes(config, owner)
        expected = windows_path(Path(DOOM_BASE_DIR).parent / "DOOMEternalx64vk.exe")
        if rows is None or len(rows) != 1:
            return None
        row = rows[0]
        if ((row["path"].casefold() != expected.casefold() and row.get("same_executable") is not True)
                or type(row["pid"]) is not int or type(row["created"]) is not int):
            return None
        return f"windows:{row['pid']}:{row['created']}"
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError):
        return None


def gameplay_evidence_mtime_ns():
    try:
        return Path(GAMEPLAY_SAVE_EVIDENCE_PATH).stat().st_mtime_ns
    except OSError:
        return None


def emit_launcher_event(event_type: str, **payload):
    if not LAUNCHER_EVENTS_ENABLED:
        return
    event = {"type": event_type, **payload}
    print("AP_EVENT " + json.dumps(event, sort_keys=True, separators=(",", ":")), flush=True)


def abort_setup(message):
    print(message, file=sys.stderr)
    if os.name == "nt" and not os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("HEADLESS") and not os.environ.get("CI"):
        _show_setup_error(player_text("activity", "setup_error_title"), message)
    raise RuntimeError(message)


def load_config():
    if not CONFIG_FILE.exists():
        return {}
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
    except json.JSONDecodeError as error:
        abort_setup(player_text("activity", "config_invalid_json", path=str(CONFIG_FILE), error=str(error)))
    if not isinstance(loaded, dict):
        abort_setup(player_text("activity", "config_object_required", path=str(CONFIG_FILE)))
    return loaded


def save_config():
    atomic_write_json(CONFIG_FILE, config, indent=4, sort_keys=False)


def parse_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_doom_base_dir(path):
    selected = Path(path).expanduser().resolve()

    if selected.name.lower() == "base":
        game_root = selected.parent
        base_dir = selected
    else:
        game_root = selected
        base_dir = selected / "base"

    executable = game_root / "DOOMEternalx64vk.exe"

    if executable.is_file() and base_dir.is_dir():
        return str(base_dir)

    raise ValueError(
        player_text("activity", "game_directory_invalid", newline="\n", executable=str(executable), base_dir=str(base_dir))
    )


def normalize_save_games_dir(path):
    from doom_eap.launcher.launcher_platform import doom_saved_games_base, select_saved_games_dir

    global SAVE_GAMES_SELECTION
    SAVE_GAMES_SELECTION = None
    selected = Path(path).expanduser()
    name = selected.name.lower()
    probe = _saved_games_probe(selected, name)
    game_root = _configured_game_root()
    known = None
    try:
        known = doom_saved_games_base(game_root=game_root)
    except (OSError, TypeError, ValueError, RuntimeError):
        known = None
    selection = select_saved_games_dir(
        str(probe),
        known_base=known,
        app_dir=APPLICATION_DIR,
        game_root=game_root,
    )
    if selection.path is None:
        rejected = "; ".join(
            f"{item.get('path')} ({item.get('reason')})"
            for item in selection.rejected[:4]
        )
        raise ValueError(
            player_text("activity", "save_directory_invalid", reason=str(selection.reason))
            + (player_text("activity", "save_directory_rejected", rejected=str(rejected)) if rejected else "")
        )
    SAVE_GAMES_SELECTION = {
        "selected_path": str(selection.path),
        "source": selection.source,
        "reason": selection.reason,
        "repaired": selection.repaired,
        "previous_path": selection.previous_path,
    }
    return str(selection.path)


def discover_client_state_file():
    configured = config.get("client_state_file")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = CONFIG_FILE.parent / path
        return path.resolve()
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        root = Path(
            os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
        )
    return root / "doom-eternal-ap" / "client_state.json"


def discover_bridge_log_dir():
    candidates = []
    configured = config.get("bridge_log_dir")
    if configured:
        candidates.append(Path(configured))
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        root = Path(
            os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
        )
    candidates.append(root / "doom-eternal-ap" / "logs")
    candidates.append(CONFIG_FILE.parent / "logs")

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-test"
            with probe.open("a", encoding="utf-8"):
                pass
            probe.unlink(missing_ok=True)
            return candidate
        except OSError:
            continue

    return Path.cwd() / "logs"


def configure_bridge_logger():
    bridge_logger = logging.getLogger("doom_eternal_ap.bridge")
    bridge_logger.setLevel(logging.DEBUG)
    bridge_logger.propagate = False
    return bridge_logger


def start_bridge_logger(path=None):
    """Start a fresh production log for an active client session."""
    target = Path(path or BRIDGE_LOG_PATH)
    _native_probe_log_signatures.clear()
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = target.with_name("bridge.previous.log")
    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler):
            logger.removeHandler(handler)
            handler.close()
    try:
        previous.unlink(missing_ok=True)
        if target.exists():
            target.replace(previous)
    except OSError:
        # Logging must not prevent a client connection when a host filesystem
        # momentarily refuses a rename.
        pass
    handler = logging.FileHandler(target, mode="w", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(levelname)s %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    return logger


def log_effective_runtime_paths():
    """Record bridge-owned configuration and effective runtime path surfaces."""
    logger.info("CONFIG_FILE=%s", CONFIG_FILE.resolve())
    logger.info("CLIENT_STATE_FILE=%s", CLIENT_STATE_FILE.resolve())
    logger.info("DOOM_BASE_DIR=%s", DOOM_BASE_DIR)
    logger.info("SAVE_GAMES_DIR=%s", SAVE_GAMES_DIR)
    logger.info("INV_DUMP_DIR=%s", INV_DUMP_DIR)
    logger.info("STEAM_REMOTE_DIR=%s", STEAM_REMOTE_DIR if STEAM_REMOTE_DIR is not None else "unavailable")
    logger.info("STEAM_ID3=%s", STEAM_ID3)
    logger.info("SAVE_GAMES_SELECTION_SOURCE=%s", SAVE_GAMES_SELECTION_SOURCE)
    logger.info("SAVE_GAMES_SELECTION_REASON=%s", SAVE_GAMES_SELECTION_REASON)


def client_state_store():
    """Compose the persistence adapter; callers retain current commit timing."""
    return ClientStateStore(
        CLIENT_STATE_FILE, version=CLIENT_STATE_VERSION, migrate=migrate_client_state,
        log_event=log_item_event, logger=logger,
    )


def load_client_state():
    return client_state_store().load()


def save_client_state(state, *, reason="state_update", boundary=None, boundary_before=None):
    return client_state_store().commit(
        state, reason=reason, boundary=boundary, boundary_before=boundary_before,
    )


def normalize_steam_remote_dir(path):
    """Accept remote, 782330, account, userdata, or Steam-root selections."""
    selected = Path(path).expanduser()

    direct_candidates = _steam_remote_paths(selected)
    valid = []
    for candidate in direct_candidates:
        try:
            candidate = candidate.resolve()
        except OSError:
            candidate = candidate.absolute()

        if not candidate.is_dir():
            continue
        if candidate.name.lower() != "remote":
            continue
        if candidate.parent.name != DOOM_STEAM_APP_ID:
            continue

        try:
            steam_id3 = int(candidate.parents[1].name)
        except (IndexError, ValueError):
            continue

        valid.append((candidate, steam_id3))

    if not valid:
        raise ValueError(player_text("activity", "steam_directory_invalid"))

    valid.sort(
        key=lambda pair: _steam_remote_candidate_score(pair[0]),
        reverse=True,
    )
    return valid[0]


def prompt_for_steam_remote(candidates):
    """Ask only when automatic discovery cannot choose a usable directory."""
    has_tty = bool(sys.stdin and sys.stdin.isatty())

    if candidates and has_tty:
        return _choose_steam_account(candidates)

    while True:
        selected = _choose_directory(player_text("activity", "select_steam_directory"))

        selected = _steam_path_input(selected, has_tty)

        if not selected:
            return None, 0

        try:
            return normalize_steam_remote_dir(selected)
        except ValueError as error:
            if has_tty:
                print(f'Validation error: {error!s}')
                continue

            if not _show_setup_error(player_text("activity", "steam_directory_title"), str(error)):
                return None, 0


def discover_steam_remote():
    configured = config.get("steam_remote_dir")
    configured_id = parse_int(config.get("steam_id3"), 0)

    if configured is not None:
        configured_text = str(configured).strip()
        if configured_text in ("", ".") or configured_id < 0:
            logger.warning(
                "[Setup] Invalid legacy Steam remote configuration "
                f"detected: {configured_text!r} / ID {configured_id}. "
                "Running auto-discovery again."
            )
            config.pop("steam_remote_dir", None)
            config.pop("steam_id3", None)
            save_config()
            configured = None
            configured_id = 0
        else:
            try:
                remote, inferred_id = normalize_steam_remote_dir(
                    configured_text
                )
                if configured_id not in (0, inferred_id):
                    logger.warning(
                        "[Setup] steam_id3 did not match the userdata "
                        f"directory; using inferred ID {inferred_id}."
                    )
                return remote, inferred_id
            except ValueError as error:
                logger.warning(
                    "[Setup] Stored steam_remote_dir is invalid: "
                    f"{error}. Running auto-discovery again."
                )
                config.pop("steam_remote_dir", None)
                config.pop("steam_id3", None)
                save_config()

    candidates = _discover_steam_remote_candidates()
    if candidates:
        chosen = candidates[0]
        if len(candidates) > 1:
            logger.info(
                "[Setup] Multiple Steam save directories found. "
                "Selected the candidate with the newest DOOM Eternal "
                f"autosave: {_describe_steam_remote_candidate(*chosen)}"
            )
        else:
            logger.info(
                "[Setup] Steam save directory discovered automatically: "
                f"{_describe_steam_remote_candidate(*chosen)}"
            )
        return chosen

    logger.warning(
        "[Setup] Could not discover a DOOM Eternal Steam save directory "
        "automatically. Manual selection is required for DeathLink SEND "
        "and save-based goal fallback."
    )
    return prompt_for_steam_remote(candidates)


def discover_oodle_dll():
    configured = config.get("oodle_dll")
    candidates = [
        Path(configured) if configured else Path(),
        Path(DOOM_BASE_DIR).parent / "oo2core_8_win64.dll",
        Path(DOOM_BASE_DIR) / "oo2core_8_win64.dll",
    ]
    return next((path for path in candidates if path.is_file()), Path())


def refresh_death_probe_proton():
    global PROTON_PATH, STEAM_COMPAT_DATA, STEAM_INSTALL
    if os.name == "nt":
        return

    try:
        command, environment = runtime(config)
        PROTON_PATH = Path(command[0])
        STEAM_COMPAT_DATA = Path(environment["STEAM_COMPAT_DATA_PATH"])
        STEAM_INSTALL = Path(environment["STEAM_COMPAT_CLIENT_INSTALL_PATH"])
    except (OSError, ValueError, RuntimeError, KeyError):
        PROTON_PATH = STEAM_COMPAT_DATA = STEAM_INSTALL = Path()


def cleanup_death_probe_runtime():
    shutil.rmtree(DEATH_PROBE_RUNTIME, ignore_errors=True)


def log_delivery_event(event: str, **fields) -> None:
    """Emit bounded, correlation-friendly delivery diagnostics only."""
    wall_time_ns = fields.pop("wall_time_ns", None)
    monotonic_ns = fields.pop("monotonic_ns", None)
    record = {
        "event": event,
        "wall_time_ns": time.time_ns() if wall_time_ns is None else wall_time_ns,
        "monotonic_ns": time.monotonic_ns() if monotonic_ns is None else monotonic_ns,
        **{key: value for key, value in fields.items() if value is not None},
    }
    logger.info("DELIVERY_EVENT %s", json.dumps(record, sort_keys=True, separators=(",", ":")))


def log_item_event(event: str, **fields) -> None:
    """Emit structured item diagnostics with a stable ITEM_* event name."""
    if not event.startswith("ITEM_"):
        event = f"ITEM_{event}"
    if _duplicate_probe_event(event, fields):
        return
    fields.setdefault("bridge_revision", globals().get("BRIDGE_REVISION"))
    fields.setdefault("protocol_version", globals().get("BRIDGE_PROTOCOL"))
    fields.setdefault("mapping_revision", globals().get("ITEM_MAPPING_REVISION"))
    log_delivery_event(event, **fields)


class ProtonSelectionError(RuntimeError):
    failure_domain = "proton_runtime"

    def __init__(self, reason, *, candidates=(), selected=None):
        super().__init__("Select the Proton version used by DOOM Eternal in Steam. "
                         "Choose the file named proton, not wine, DOOM Eternal or the pfx folder.")
        self.proton_reason = reason
        self.proton_candidates = sorted(str(path) for path in candidates)[:12]
        self.proton_selected = str(selected) if selected else None


def windows_path(path) -> str:
    value = str(Path(path).expanduser().resolve())
    return "Z:" + value.replace("/", "\\") if value.startswith("/") else value


def runtime(configuration):
    root = Path(configuration.get("game_root") or configuration["doom_base_dir"]).resolve()
    if root.name.casefold() == "base":
        root = root.parent
    prefix = Path(configuration.get("proton_compat_data_dir") or root.parents[1] / "compatdata/782330").resolve()
    if prefix.name != "782330" or not (prefix / "pfx").is_dir():
        raise RuntimeError("The selected DOOM Eternal Steam compatibility prefix is unavailable")
    selected = configuration.get("proton_executable")
    if selected:
        proton = Path(selected).resolve()
    else:
        proton = _prefix_proton(prefix)
    if proton.name != "proton" or not proton.is_file():
        raise ProtonSelectionError("invalid_selected_path", selected=proton)
    return [str(proton), "runinprefix"], _proton_environment(configuration, prefix)


def windows_processes(configuration, helper):
    """Return qualified guest identities, an empty tuple, or UNKNOWN (None)."""
    try:
        command, environment = runtime(configuration)
        root = Path(configuration.get("game_root") or configuration["doom_base_dir"]).resolve()
        expected = (root.parent if root.name.casefold() == "base" else root) / "DOOMEternalx64vk.exe"
        # the windowed supervisor requires stdin even for process queries
        response = subprocess.run([*command, windows_path(helper), "--process-identity",
                                   "--expected-game-exe", windows_path(expected)],
                                  env=environment, input=b"", capture_output=True, timeout=30, check=False)
        rows = json.loads(response.stdout).get("processes")
        if response.returncode or not isinstance(rows, list):
            return None
        if not _valid_process_rows(rows):
            return None
        return tuple(rows)
    except (OSError, ValueError, RuntimeError, AttributeError, subprocess.SubprocessError):
        return None


def _packaged_bridge_identity(app_dir, root, frozen, mod_file):
    for id_path in (app_dir / "bridge_identity.json", root / "data" / "bridge_identity.json"):
        if id_path.is_file():
            try:
                doc = json.loads(id_path.read_text(encoding="utf-8"))
                if isinstance(doc, dict) and "sha256" in doc:
                    sha = str(doc["sha256"])
                    rev = str(doc.get("revision", f"mission-unified-{sha[:12]}"))
                    file_ref = Path(sys.executable).resolve() if frozen else mod_file
                    return file_ref, sha, rev
            except (OSError, ValueError) as error:
                logging.getLogger("doom_eternal_ap.bridge").debug("Bridge identity unavailable at %s: %s", id_path, error)
    return None


def _fallback_bridge_identity(root, frozen, mod_file):
    content_id_path = root / "data" / "content_identity.json"
    if content_id_path.is_file():
        sha256 = hashlib.sha256(content_id_path.read_bytes()).hexdigest()
    else:
        sha256 = hashlib.sha256(b"doom-eternal-archipelago-bridge-runtime").hexdigest()

    file_ref = Path(sys.executable).resolve() if frozen else mod_file
    return file_ref, sha256, f"mission-unified-{sha256[:12]}"


def _doom_location_ids():
    locations = _doom_location_names()
    if not locations:
        raise ValueError("DOOM location-name data is malformed")
    return set(locations)


def _doom_location_names():
    identity = json.loads(
        (REPO_ROOT / "data" / "location_names.json").read_text(encoding="utf-8")
    )
    raw_locations = identity.get("locations")
    if not isinstance(raw_locations, dict):
        return {}
    try:
        return {int(location_id): name for location_id, name in raw_locations.items() if isinstance(name, str)}
    except (TypeError, ValueError):
        return {}


def _choose_directory(title):
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as error:
        logging.getLogger("doom_eternal_ap.bridge").debug("Directory dialog unavailable: %s", error)
        return None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(title=title)
        root.destroy()
        return selected
    except (tk.TclError, RuntimeError) as error:
        logging.getLogger("doom_eternal_ap.bridge").debug("Directory dialog failed: %s", error)
        return None


def _show_setup_error(title, message):
    try:
        import tkinter as tk
        import tkinter.messagebox as messagebox
    except ImportError as error:
        logging.getLogger("doom_eternal_ap.bridge").debug("Setup dialog unavailable: %s", error)
        return False
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
        return True
    except (tk.TclError, RuntimeError) as error:
        logging.getLogger("doom_eternal_ap.bridge").debug("Setup dialog failed: %s", error)
        return False


def _report_directory_error(has_tty, error_msg):
    if has_tty:
        print(f'Validation Error: {error_msg!s}')
    else:
        _show_setup_error(player_text("activity", "validation_error_title"), error_msg)


def _saved_games_probe(selected, name):
    if name == "base" and selected.parent.name.lower() == "doometernal" and selected.parent.parent.name.lower() == "id software":
        probe = selected
    elif name == "doometernal":
        probe = selected / "base"
    elif name == "id software":
        probe = selected / "DOOMEternal" / "base"
    else:
        shaped = selected / "id Software" / "DOOMEternal" / "base"
        probe = shaped if shaped.is_dir() else selected
    return probe


def _configured_game_root():
    game_root = None
    try:
        doom_base = globals().get("DOOM_BASE_DIR") or (config or {}).get("doom_base_dir")
        if doom_base:
            game_root = Path(doom_base).expanduser()
            if game_root.name.casefold() == "base":
                game_root = game_root.parent
    except (OSError, TypeError, ValueError, RuntimeError):
        game_root = None
    return game_root


def _unique_existing_paths(paths):
    unique = []
    seen = set()
    for raw_path in paths:
        if not raw_path:
            continue
        try:
            path = Path(raw_path).expanduser()
            key = os.path.normcase(os.path.abspath(str(path)))
        except (OSError, TypeError, ValueError):
            continue
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _windows_steam_roots():
    """Return likely Steam installation roots on Windows.

    The game library and the Steam installation are often on different drives.
    Steam userdata normally lives beside the Steam client, so the registry is
    the primary source instead of the DOOM installation path.
    """
    if os.name != "nt":
        return []

    roots = []

    try:
        import winreg

        registry_values = [
            (
                winreg.HKEY_CURRENT_USER,
                r"Software\Valve\Steam",
                "SteamPath",
            ),
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\WOW6432Node\Valve\Steam",
                "InstallPath",
            ),
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Valve\Steam",
                "InstallPath",
            ),
        ]

        for hive, key_name, value_name in registry_values:
            try:
                with winreg.OpenKey(hive, key_name) as key:
                    value, _ = winreg.QueryValueEx(key, value_name)
                if value:
                    roots.append(Path(value))
            except (FileNotFoundError, OSError):
                continue
    except ImportError:
        pass

    for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "PROGRAMW6432"):
        value = os.environ.get(variable)
        if value:
            roots.append(Path(value) / "Steam")

    configured_root = config.get("steam_root_dir")
    if configured_root:
        roots.append(Path(configured_root).expanduser())

    return _unique_existing_paths(roots)


def _linux_steam_roots():
    if os.name == "nt":
        return []

    home = Path.home()
    homes = [home]
    if home.is_absolute():
        try:
            var_home = Path("/var") / home.relative_to("/")
            if var_home != home:
                homes.append(var_home)
        except ValueError:
            pass

    roots = []
    for candidate_home in homes:
        roots.extend(
            [
                candidate_home / ".local/share/Steam",
                candidate_home / ".steam/steam",
                candidate_home
                / ".var/app/com.valvesoftware.Steam/data/Steam",
            ]
        )

    configured_root = config.get("steam_root_dir")
    if configured_root:
        roots.append(Path(configured_root).expanduser())

    return _unique_existing_paths(roots)


def _game_library_steam_root():
    """Return the Steam-library root inferred from the DOOM installation."""
    try:
        doom_base = Path(DOOM_BASE_DIR)
    except NameError:
        return None

    for parent in [doom_base, *doom_base.parents]:
        if parent.name.lower() == "steamapps":
            return parent.parent
    return None


def _steam_roots():
    roots = []
    roots.extend(_windows_steam_roots())
    roots.extend(_linux_steam_roots())

    library_root = _game_library_steam_root()
    if library_root is not None:
        roots.append(library_root)

    return _unique_existing_paths(roots)


def _steam_remote_paths(selected):
    direct_candidates = [selected]
    name = selected.name.lower()

    if name == "782330":
        direct_candidates.insert(0, selected / "remote")
    elif name.isdigit():
        direct_candidates.insert(
            0,
            selected / DOOM_STEAM_APP_ID / "remote",
        )
    elif name == "userdata":
        direct_candidates.extend(
            selected.glob(f"*/{DOOM_STEAM_APP_ID}/remote")
        )
    else:
        direct_candidates.extend(
            (selected / "userdata").glob(
                f"*/{DOOM_STEAM_APP_ID}/remote"
            )
        )

    return direct_candidates


def _steam_remote_candidate_score(remote):
    duration_files = [
        p for p in remote.glob("*-AUTOSAVE*/game_duration.dat")
        if re.fullmatch(r"(?:GAME|DLC[12]|HORDE)-AUTOSAVE\d+", p.parent.name)
    ]
    details_files = [
        p for p in remote.glob("*-AUTOSAVE*/game.details")
        if re.fullmatch(r"(?:GAME|DLC[12]|HORDE)-AUTOSAVE\d+", p.parent.name)
    ]
    save_files = duration_files + details_files

    newest_mtime = 0
    for save_file in save_files:
        try:
            newest_mtime = max(
                newest_mtime,
                save_file.stat().st_mtime_ns,
            )
        except OSError:
            continue

    return (
        bool(duration_files),
        bool(details_files),
        newest_mtime,
    )


def _discover_steam_remote_candidates():
    discovered = []
    seen = set()

    for steam_root in _steam_roots():
        userdata = steam_root / "userdata"
        if not userdata.is_dir():
            continue

        for remote in userdata.glob(
            f"*/{DOOM_STEAM_APP_ID}/remote"
        ):
            try:
                normalized, steam_id3 = normalize_steam_remote_dir(remote)
            except ValueError:
                continue

            key = os.path.normcase(
                os.path.abspath(str(normalized))
            )
            if key in seen:
                continue
            seen.add(key)
            discovered.append((normalized, steam_id3))

    discovered.sort(
        key=lambda pair: _steam_remote_candidate_score(pair[0]),
        reverse=True,
    )
    return discovered


def _describe_steam_remote_candidate(remote, steam_id3):
    duration_files = [
        p for p in remote.glob("*-AUTOSAVE*/game_duration.dat")
        if re.fullmatch(r"(?:GAME|DLC[12]|HORDE)-AUTOSAVE\d+", p.parent.name)
    ]
    details_files = [
        p for p in remote.glob("*-AUTOSAVE*/game.details")
        if re.fullmatch(r"(?:GAME|DLC[12]|HORDE)-AUTOSAVE\d+", p.parent.name)
    ]
    save_files = duration_files + details_files

    newest = None
    for save_file in save_files:
        try:
            mtime = save_file.stat().st_mtime
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)

    if newest is None:
        save_description = 'no autosave files found yet'
    else:
        save_description = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(newest),
        )

    return f'{remote!s} (Steam account {steam_id3!s}; latest save: {save_description!s})'


def _choose_steam_account(candidates):
    print('{newline}Found DOOM Eternal Steam save directories:'.format(newline='\n'))
    for index, (remote, steam_id3) in enumerate(candidates, start=1):
        print(
            f"  {index}. "
            f"{_describe_steam_remote_candidate(remote, steam_id3)}"
        )

    while True:
        answer = input(f'Choose the active Steam account [1-{len(candidates)!s}] (default 1): ').strip()
        if not answer:
            return candidates[0]
        try:
            index = int(answer)
        except ValueError:
            index = 0
        if 1 <= index <= len(candidates):
            return candidates[index - 1]
        print('Invalid selection.')


def _steam_path_input(selected, has_tty):
    if not selected and has_tty:
        print('{newline}Select the Steam directory that contains userdata/<ACCOUNT_ID>/782330/remote.'.format(newline='\n'))
        print('Windows example: C:/Program Files (x86)/Steam')
        selected = input('Steam path: ').strip()
    return selected


def _duplicate_probe_event(event, fields):
    if (event == "ITEM_NATIVE_PROBE_RESPONSE" and fields.get("returncode") == 0
            and fields.get("stage") in {"--native", "--inspect", "--save-admission"}):
        try:
            response = json.loads(fields.get("stdout", ""))
        except (TypeError, ValueError):
            response = None
        if isinstance(response, dict) and response.get("result") == "ok":
            signature = json.dumps({key: value for key, value in response.items()
                                    if key not in {"callback_sequence", "callback_at_ms", "context_sampled_at_ms"}},
                                   sort_keys=True, separators=(",", ":"))
            key = (fields["stage"], response.get("target_pid"), fields.get("sha256"))
            if _native_probe_log_signatures.get(key) == signature:
                return True
            _native_probe_log_signatures[key] = signature
    return False


def _windows_doom_identity():
    try:
        from doom_eap.runtime.observers import same_executable_path, windows_game_processes
        rows = windows_game_processes()
        if rows is None or len(rows) != 1:
            return None
        row = rows[0]
        base = globals().get("DOOM_BASE_DIR")
        expected = Path(str(base)).parent / "DOOMEternalx64vk.exe" if base else None
        if expected is None or not same_executable_path(row["path"], expected.resolve()):
            return None
        return f"windows:{row['pid']}:{row['created']}"
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _prefix_proton(prefix):
    info = prefix / "config_info"
    if not info.is_file():
        raise ProtonSelectionError("missing_config_info")
    if info.stat().st_size > 65536:
        raise ProtonSelectionError("invalid_config_info")
    try:
        lines = info.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ProtonSelectionError("unreadable_config_info") from error
    candidates = {Path(line) / "proton" for line in lines
                  if line.startswith("/") and (Path(line) / "proton").is_file()}
    if len(candidates) != 1:
        raise ProtonSelectionError("ambiguous" if candidates else "no_candidate", candidates=candidates)
    proton = candidates.pop().resolve()
    return proton


def _proton_environment(configuration, prefix):
    remote = Path(configuration["steam_remote_dir"]).resolve()
    if remote.name != "remote" or remote.parent.name != "782330" or remote.parents[2].name != "userdata":
        raise RuntimeError("Select DOOM Eternal's Steam userdata directory")
    environment = dict(os.environ, STEAM_COMPAT_DATA_PATH=str(prefix),
                       STEAM_COMPAT_CLIENT_INSTALL_PATH=str(remote.parents[3]), WINEPREFIX=str(prefix / "pfx"))
    return environment


@timed_sync_call
def _valid_process_rows(rows):
    if any(not isinstance(row, dict) or type(row.get("pid")) is not int or row["pid"] <= 0
           or type(row.get("created")) is not int or row["created"] <= 0
           or not isinstance(row.get("path"), str) for row in rows):
        return False
    return True


BRIDGE_FILE, BRIDGE_SHA256, BRIDGE_REVISION = resolve_bridge_identity(
    APPLICATION_DIR, REPO_ROOT, Path(__file__).with_name("bridge_client.py").resolve()
)
_CONTENT_IDENTITY = json.loads(
    (REPO_ROOT / "data" / "content_identity.json").read_text(encoding="utf-8")
)
BRIDGE_PROTOCOL = _CONTENT_IDENTITY["bridge_protocol_version"]
TRANSITION_HANDLER = "unified"
GAME_NAME = _CONTENT_IDENTITY["game"]
AMMO_REFILL_REQUEST_FILENAME = "AP_REFILL_REQUEST.txt"
AMMO_REFILL_REQUEST_RE = re.compile(r"^AP_REFILL_REQUEST(?:_(\d+))?\.txt$")
DOOM_LOCATION_NAMES = _doom_location_names()
HELL_ON_EARTH_LOCATION_IDS = frozenset(
    loc_id for loc_id, name in DOOM_LOCATION_NAMES.items()
    if name.startswith("Hell on Earth - ")
)
GOAL_ENDPOINT_LOCATION_IDS = {
    "Acquire the Unmaykr": 7770418,
    "Kill the Icon of Sin": 7770414,
    "Kill the Dark Lord": 7770419,
    "Complete the Full Saga": 7770419,
}
GOAL_ENDPOINT_LOCATION_NAMES = {
    "Acquire the Unmaykr": "Fortress of Doom - Unmaykr Acquired",
    "Kill the Icon of Sin": "Final Sin - Mission Complete",
    "Kill the Dark Lord": "The Dark Lord - Defeated",
    "Complete the Full Saga": "The Dark Lord - Defeated",
}


if any(
    DOOM_LOCATION_NAMES.get(location_id) != location_name
    for goal, location_id in GOAL_ENDPOINT_LOCATION_IDS.items()
    for location_name in (GOAL_ENDPOINT_LOCATION_NAMES[goal],)
):
    raise RuntimeError("packaged goal endpoint IDs diverge from canonical location names")


GOAL_REQUIREMENT_SUFFIXES = {
    "Complete All Enabled Missions": " - Mission Complete",
    "Complete All Included Missions": " - Mission Complete",
    "Complete All Slayer Gates": " - Slayer Gate Complete",
    "Complete All Escalation Encounters": " - Escalation Encounter Wave ",
    "Complete All Secret Encounters": " - Secret Encounter - ",
    "Complete All Mission Challenges": " - All Mission Challenges Completed",
    "Complete All Weapon Mastery Challenges": " - Weapon Mastery Challenge",
}
GOAL_NAMES = frozenset(GOAL_ENDPOINT_LOCATION_IDS)
GOAL_REQUIREMENT_NAMES = frozenset(GOAL_REQUIREMENT_SUFFIXES) | {"Acquire the Unmaykr"}
DLC_MISSION_PREFIXES = frozenset({
    "UAC Atlantica Facility",
    "The Blood Swamps",
    "The Holt",
    "The World Spear",
    "Reclaimed Earth",
    "Immora",
    "The Dark Lord",
})
DEATHLINK_RECEIVE_TIMEOUT = 20.0
DEATHLINK_CONFIRM_TIMEOUT = 8.0
DEATHLINK_TOTAL_TIMEOUT = 60.0
DEATHLINK_LATE_SUPPRESSION_GRACE = 15.0
DEATHLINK_MAX_ATTEMPTS = 1
LAUNCHER_EVENTS_ENABLED = os.environ.get("DOOM_AP_LAUNCHER_EVENTS") == "1"
FAST_TRAVEL_MISSION_COMPLETE_IDS = {
    "e1m1_intro": 7770122, "e1m2_war": 7770123, "e1m3_cult": 7770124,
    "e1m4_boss": 7770162, "e2m1_nest": 7770210, "e2m2_base": 7770248,
    "e2m3_core": 7770289, "e2m4_boss": 7770290, "e3m1_slayer": 7770337,
    "e3m2_hell": 7770362, "e3m2_hell_b": 7770387, "e3m3_maykr": 7770411,
    "e3m4_boss": 7770414,
    "e4m1_rig": 7770432, "e4m2_swamp": 7770443, "e4m3_mcity": 7770457,
}
ENABLE_ITEM_NOTIFICATIONS = False
ITEM_DELIVERY_BATCH_SIZE = 16


try:
    _identity_path = APPLICATION_DIR / "bridge_identity.json"
    if _identity_path.is_file():
        _identity = json.loads(_identity_path.read_text(encoding="utf-8"))
        if _identity.get("item_notifications", {}).get("enabled"):
            ENABLE_ITEM_NOTIFICATIONS = True
except (OSError, ValueError, AttributeError) as error:
    logging.getLogger("doom_eternal_ap.bridge").debug("Item notification configuration unavailable: %s", error)


SAVE_GAMES_SELECTION: dict[str, object] | None = None
config = load_config()
SAVE_GAMES_SELECTION_SOURCE = "unresolved"
SAVE_GAMES_SELECTION_REASON = "configuration has not been resolved"
AP_SOURCE_PATH = os.environ.get("ARCHIPELAGO_SOURCE")


if not getattr(sys, "frozen", False) and AP_SOURCE_PATH:
    sys.path.insert(0, os.path.abspath(AP_SOURCE_PATH))


if "doom_base_dir" in config and "save_games_dir" in config:
    try:
        DOOM_BASE_DIR = normalize_doom_base_dir(config["doom_base_dir"])
        SAVE_GAMES_DIR = normalize_save_games_dir(config["save_games_dir"])
        SAVE_GAMES_SELECTION_SOURCE = str(
            (SAVE_GAMES_SELECTION or {}).get("source") or "config.save_games_dir"
        )
        SAVE_GAMES_SELECTION_REASON = str(
            (SAVE_GAMES_SELECTION or {}).get("reason")
            or "configured path normalized to existing Saved Games base"
        )
    except ValueError as error:
        if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("CI"):
            DOOM_BASE_DIR = str(config.get("doom_base_dir", ""))
            SAVE_GAMES_DIR = str(config.get("save_games_dir", ""))
        else:
            abort_setup(player_text("activity", "config_invalid_paths", path=str(CONFIG_FILE), error=str(error)))
    if (
        config.get("doom_base_dir") != DOOM_BASE_DIR
        or config.get("save_games_dir") != SAVE_GAMES_DIR
    ):
        config["doom_base_dir"] = DOOM_BASE_DIR
        config["save_games_dir"] = SAVE_GAMES_DIR
        save_config()
else:
    def prompt_for_dir(title, validation_func, error_msg):
        path = None
        has_tty = sys.stdin and sys.stdin.isatty()
        while True:
            selected = _choose_directory(title)
            path = selected if selected is not None else path
            if not path and has_tty:
                print(f"\n{title}")
                path = input('Enter Path: ').strip()
            if not path:
                raise RuntimeError("DOOM Eternal Client Setup cancelled. Please create ap_config.json manually with 'doom_base_dir' and 'save_games_dir'.")
            try:
                normalized = validation_func(path)
            except ValueError:
                normalized = None
            if normalized:
                return normalized
            _report_directory_error(has_tty, error_msg)

    DOOM_BASE_DIR = prompt_for_dir(
        player_text("activity", "select_game_directory"),
        lambda p: normalize_doom_base_dir(p) if p else None,
        (player_text("activity", "game_directory_validation_failed")),
    )

    SAVE_GAMES_DIR = prompt_for_dir(
        player_text("activity", "select_save_directory"),
        lambda p: normalize_save_games_dir(p) if p else None,
        player_text("activity", "save_directory_validation_failed"),
    )
    SAVE_GAMES_SELECTION_SOURCE = "interactive_selection"
    SAVE_GAMES_SELECTION_REASON = "path selected during client setup"

    config["doom_base_dir"] = DOOM_BASE_DIR
    config["save_games_dir"] = SAVE_GAMES_DIR
    save_config()
    if sys.stdin and sys.stdin.isatty():
        print('Configuration saved to ap_config.json!{newline}'.format(newline='\n'))


TAG_INVENTORY_SETTLE_SECONDS = 1.0
GAMEPLAY_SAVE_EVIDENCE_PATH = Path(DOOM_BASE_DIR) / "ap_gameplay_save.state"
INV_DUMP_DIR = SAVE_GAMES_DIR
CULTIST_BASE_MAP = "game/sp/e1m3_cult/e1m3_cult"
DOOM_HUNTER_BASE_MAP = "game/sp/e1m4_boss/e1m4_boss"
DEATHLINK_KILL_INTERVAL = 2.0
CHECK_EVENT_PREFIX = "ap_event_"
GOAL_EVENT_PREFIX = "ap_transition_"
GOAL_EVENT_FILENAME = "ap_transition_e1m3_cult_to_e1m4_boss.evt"
TELEMETRY_DUMP_PREFIX = "ap_telemetry"
LEGACY_TELEMETRY_DUMP_PREFIX = "ap_condump"
ITEM_MAPPING_REVISION = 9
RPC_ENTITY_PREFIX = "ap_rpc_v3"
REVISION_ONE_RUNE_IDS = {
    7770085,
    7770086,
    7770087,
    7770089,
    7770090,
    7770091,
    7770093,
    7770094,
    7770095,
} | SUPPORT_RUNE_IDS
NORMAL_RUNE_ITEM_BITS = {item_id: 1 << index for index, item_id in enumerate((
    7770085, 7770086, 7770087, 7770089, 7770090, 7770091, 7770093, 7770094, 7770095,
))}
SUPPORT_RUNE_ITEM_BITS = {item_id: 1 << index for index, item_id in enumerate(sorted(SUPPORT_RUNE_IDS))}
REVISION_TWO_SUIT_IDS = {7770021}
REVISION_FOUR_FLAME_BELCH_IDS = {7770012}
REVISION_FIVE_EQUIPMENT_LAUNCHER_IDS = {7770011, 7770013}
REVISION_NINE_ICE_BOMB_IDS = {7770013}
CLIENT_STATE_FILE = discover_client_state_file()
BRIDGE_LOG_DIR = discover_bridge_log_dir()
BRIDGE_LOG_PATH = BRIDGE_LOG_DIR / "bridge.log"
logger = configure_bridge_logger()
_native_probe_log_signatures = {}
DOOM_STEAM_APP_ID = "782330"
STEAM_REMOTE_DIR, STEAM_ID3 = discover_steam_remote()


if (
    STEAM_REMOTE_DIR is not None
    and STEAM_ID3 > 0
    and STEAM_REMOTE_DIR.is_dir()
):
    remote_path = str(STEAM_REMOTE_DIR)
    if (
        config.get("steam_remote_dir") != remote_path
        or parse_int(config.get("steam_id3"), 0) != STEAM_ID3
    ):
        config["steam_remote_dir"] = remote_path
        config["steam_id3"] = STEAM_ID3
        save_config()
        logger.info(
            "[Setup] Saved Steam remote configuration: "
            f"{remote_path} / Steam account {STEAM_ID3}."
        )
else:
    STEAM_REMOTE_DIR = None
    STEAM_ID3 = 0
    logger.warning(
        "[Setup] Steam remote directory is unavailable. "
        "DeathLink SEND and save-based goal fallback are disabled "
        "until the path is configured."
    )


DEATH_PROBE = APPLICATION_DIR / "save_death_probe.exe"
DEATH_PROBE_RUNTIME = APPLICATION_DIR / f".death-probe-{os.getpid()}"
OODLE_DLL = discover_oodle_dll()
PROTON_PATH = STEAM_COMPAT_DATA = STEAM_INSTALL = Path()


refresh_death_probe_proton()


DEATH_PROBE_COMPAT_DATA = Path(
    config.get(
        "death_probe_compat_data",
        Path.home() / ".cache" / "doom-eap" / "death-probe-compat",
    )
)
DISTROBOX_HOST_EXEC = (
    shutil.which("distrobox-host-exec")
    if Path("/run/.containerenv").exists()
    else None
)


atexit.register(cleanup_death_probe_runtime)
