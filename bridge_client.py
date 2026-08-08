import asyncio
import atexit
import glob
import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import NamedTuple

from bootstrap_actions import (
    BOOTSTRAP_ACTIONS,
    BOOTSTRAP_REVISION,
    BOOTSTRAP_STAT_PRIMITIVE,
    received_any_suit_upgrade,
)
from campaign_goal_contract import CAMPAIGN_GOAL_CONTRACT
from challenge_registry import (
    aggregate_ready,
    canonical_map_name,
    load_challenge_registry,
)
from deathlink_receive import DeathLinkReceiver, ReceiveState
from foundation import (
    compile_item_delivery_plan,
    load_foundation_contracts,
    load_primitive_registry,
)
from item_classification import (
    load_item_classification_identity,
    normalize_network_classification,
    notification_style_for_item,
)
from item_reconciliation import (
    compile_reconciliation_plan,
    load_policy_registry,
)
from observer_lifecycle import (
    RuntimeObservationLease,
    SaveObserverBaselineStore,
    observer_registry_revision,
    unlockable_record_complete,
)
from publisher_contracts import (
    load_publisher_contracts,
)
from publisher_runtime import (
    PublisherEngine,
    publisher_acknowledged,
    quarantine_malformed_event,
    read_map_event,
)

try:
    from .save_decrypt import decrypt, steam_id64
except ImportError:
    from save_decrypt import decrypt, steam_id64

CONFIG_FILE = Path(
    os.environ.get("DOOM_AP_CONFIG_FILE", Path(__file__).with_name("ap_config.json"))
)
BRIDGE_FILE = Path(__file__).resolve()
BRIDGE_SHA256 = hashlib.sha256(BRIDGE_FILE.read_bytes()).hexdigest()
_CONTENT_IDENTITY = json.loads(
    (Path(__file__).resolve().with_name("data") / "content_identity.json").read_text(encoding="utf-8")
)
BRIDGE_PROTOCOL = _CONTENT_IDENTITY["bridge_protocol_version"]
BRIDGE_REVISION = f"mission-unified-{BRIDGE_SHA256[:12]}"
TRANSITION_HANDLER = "unified"
GAME_NAME = _CONTENT_IDENTITY["game"]
DEATHLINK_RECEIVE_TIMEOUT = 20.0
DEATHLINK_CONFIRM_TIMEOUT = 10.0
DEATHLINK_MESSAGES = (
    "{player} didn't rip and tear enough.",
    "{player} was sent back to the Fortress.",
    "{player} picked a fight with Hell and lost.",
    "{player}'s ripping and tearing privileges were revoked.",
)
LAUNCHER_EVENTS_ENABLED = os.environ.get("DOOM_AP_LAUNCHER_EVENTS") == "1"


def emit_launcher_event(event_type: str, **payload):
    if not LAUNCHER_EVENTS_ENABLED:
        return
    event = {"type": event_type, **payload}
    print("AP_EVENT " + json.dumps(event, sort_keys=True, separators=(",", ":")), flush=True)

ENABLE_ITEM_NOTIFICATIONS = False
try:
    _identity_path = Path(__file__).resolve().with_name("bridge_identity.json")
    if _identity_path.is_file():
        _identity = json.loads(_identity_path.read_text(encoding="utf-8"))
        if _identity.get("item_notifications", {}).get("enabled"):
            ENABLE_ITEM_NOTIFICATIONS = True
except Exception:
    pass
def abort_setup(message):
    print(message, file=sys.stderr)
    if os.name == "nt":
        try:
            import tkinter as tk
            import tkinter.messagebox as messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("DOOM Eternal AP setup error", message)
            root.destroy()
        except Exception:
            pass
    raise RuntimeError(message)


def load_config():
    if not CONFIG_FILE.exists():
        return {}
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
    except json.JSONDecodeError as error:
        abort_setup(
            f"{CONFIG_FILE} is not valid JSON: {error}. "
            "Use forward slashes in Windows paths, or escape backslashes as \\\\."
        )
    if not isinstance(loaded, dict):
        abort_setup(f"{CONFIG_FILE} must contain a JSON object.")
    return loaded


def save_config():
    with CONFIG_FILE.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(config, file, indent=4)
        file.write("\n")


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
    classicwads = base_dir / "classicwads"

    if executable.is_file() and classicwads.is_dir():
        return str(base_dir)

    raise ValueError(
        "Expected either the DOOM Eternal installation directory or its base "
        "directory.\n"
        f"Checked executable: {executable}\n"
        f"Checked classicwads: {classicwads}\n"
        "Examples:\n"
        "  Windows: D:/SteamLibrary/steamapps/common/DOOMEternal\n"
        "  Windows: D:/SteamLibrary/steamapps/common/DOOMEternal/base\n"
        "  Linux: /path/to/steamapps/common/DOOMEternal\n"
        "  Linux: /path/to/steamapps/common/DOOMEternal/base"
    )


def normalize_save_games_dir(path):
    selected = Path(path).expanduser()
    candidates = [selected]
    name = selected.name.lower()
    parent_name = selected.parent.name.lower()
    grandparent_name = selected.parent.parent.name.lower()
    if name == "base" and parent_name == "doometernal" and grandparent_name == "id software":
        candidates.insert(0, selected)
    elif name == "doometernal":
        candidates.insert(0, selected / "base")
    elif name == "id software":
        candidates.insert(0, selected / "DOOMEternal" / "base")
    else:
        candidates.insert(0, selected / "id Software" / "DOOMEternal" / "base")
    for candidate in candidates:
        if candidate.is_dir():
            return str(candidate)
    raise ValueError(
        "Expected the DOOM Eternal save base directory, for example "
        "C:/Users/<user>/Saved Games/id Software/DOOMEternal/base"
    )


config = load_config()

AP_SOURCE_PATH = os.environ.get("ARCHIPELAGO_SOURCE")
if AP_SOURCE_PATH:
    sys.path.insert(0, os.path.abspath(AP_SOURCE_PATH))

import colorama  # noqa: E402
import Utils  # noqa: E402
from CommonClient import (  # noqa: E402
    ClientCommandProcessor,
    CommonContext,
    get_base_parser,
    gui_enabled,
    server_loop,
)
from NetUtils import ClientStatus  # noqa: E402

if "doom_base_dir" in config and "save_games_dir" in config:
    try:
        DOOM_BASE_DIR = normalize_doom_base_dir(config["doom_base_dir"])
        SAVE_GAMES_DIR = normalize_save_games_dir(config["save_games_dir"])
    except ValueError as error:
        abort_setup(f"{CONFIG_FILE} has invalid paths: {error}")
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
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes('-topmost', True)
                path = filedialog.askdirectory(title=title)
                root.destroy()
            except Exception:
                pass

            if not path and has_tty:
                print(f"\n{title}")
                path = input("Enter Path: ").strip()

            if not path:
                raise RuntimeError("DOOM Eternal Client Setup cancelled. Please create ap_config.json manually with 'doom_base_dir' and 'save_games_dir'.")

            try:
                normalized = validation_func(path)
            except ValueError:
                normalized = None
            if normalized:
                return normalized

            if has_tty:
                print(f"Validation Error: {error_msg}")
            else:
                try:
                    import tkinter.messagebox as messagebox
                    root = tk.Tk()
                    root.withdraw()
                    messagebox.showerror("Validation Error", error_msg)
                    root.destroy()
                except Exception:
                    pass

    DOOM_BASE_DIR = prompt_for_dir(
        "Select the DOOM Eternal installation folder or its base folder",
        lambda p: normalize_doom_base_dir(p) if p else None,
        (
            "Could not validate the DOOM Eternal installation. Select either "
            ".../DOOMEternal or .../DOOMEternal/base. "
            "DOOMEternalx64vk.exe must be in DOOMEternal and classicwads "
            "must be inside DOOMEternal/base."
        ),
    )

    SAVE_GAMES_DIR = prompt_for_dir(
        "Select DOOM Saved Games Directory (.../Saved Games/id Software/DOOMEternal/base)",
        lambda p: normalize_save_games_dir(p) if p else None,
        "Could not find the DOOM Eternal save base directory."
    )

    config["doom_base_dir"] = DOOM_BASE_DIR
    config["save_games_dir"] = SAVE_GAMES_DIR
    save_config()
    if sys.stdin and sys.stdin.isatty():
        print("Configuration saved to ap_config.json!\n")

QUEUE_DIR = os.path.join(DOOM_BASE_DIR, "ap_queue")
RPC_GATE_PATH = os.path.join(DOOM_BASE_DIR, "ap_rpc_enabled")
GAMEPLAY_SAVE_EVIDENCE_PATH = Path(DOOM_BASE_DIR) / "ap_gameplay_save.state"
INV_DUMP_DIR = SAVE_GAMES_DIR
CULTIST_BASE_MAP = "game/sp/e1m3_cult/e1m3_cult"
DOOM_HUNTER_BASE_MAP = "game/sp/e1m4_boss/e1m4_boss"
DEATHLINK_KILL_INTERVAL = 2.0
DEATHLINK_KILL_COALESCE_KEY = "deathlink-kill"
CHECK_EVENT_PREFIX = "ap_event_"
GOAL_EVENT_PREFIX = "ap_transition_"
# Kept for state/tests created by the v0.2.1 single-transition monitor.
GOAL_EVENT_FILENAME = "ap_transition_e1m3_cult_to_e1m4_boss.evt"
TELEMETRY_DUMP_PREFIX = "ap_telemetry"
LEGACY_TELEMETRY_DUMP_PREFIX = "ap_condump"
ITEM_MAPPING_REVISION = 5
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
}
REVISION_TWO_SUIT_IDS = {7770021}
REVISION_FOUR_FLAME_BELCH_IDS = {7770012}
REVISION_FIVE_EQUIPMENT_LAUNCHER_IDS = {7770011, 7770013}


def discover_client_state_file():
    configured = config.get("client_state_file")
    if configured:
        return Path(configured)
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        root = Path(
            os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
        )
    return root / "doom-eternal-ap" / "client_state.json"


CLIENT_STATE_FILE = discover_client_state_file()


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


BRIDGE_LOG_DIR = discover_bridge_log_dir()
BRIDGE_LOG_PATH = BRIDGE_LOG_DIR / "bridge.log"


def configure_bridge_logger():
    bridge_logger = logging.getLogger("doom_eternal_ap.bridge")
    bridge_logger.setLevel(logging.DEBUG)
    bridge_logger.propagate = False
    return bridge_logger


def start_bridge_logger(path=None):
    """Start a fresh production log; imports (including tests) never write it."""
    target = Path(path or BRIDGE_LOG_PATH)
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


logger = configure_bridge_logger()


def load_client_state():
    if not CLIENT_STATE_FILE.is_file():
        return {"version": 1, "sessions": {}}
    try:
        state = json.loads(CLIENT_STATE_FILE.read_text(encoding="utf-8"))
        if state.get("version") != 1 or not isinstance(state.get("sessions"), dict):
            raise ValueError("unsupported client state format")
        return state
    except Exception as error:
        quarantine = CLIENT_STATE_FILE.with_name(
            f"{CLIENT_STATE_FILE.name}.corrupt-{time.time_ns()}"
        )
        try:
            os.replace(CLIENT_STATE_FILE, quarantine)
        except OSError:
            pass
        logger.warning(f"[State] Invalid state file quarantined: {error}")
        return {"version": 1, "sessions": {}}


def save_client_state(state):
    CLIENT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CLIENT_STATE_FILE.with_name(
        f".{CLIENT_STATE_FILE.name}.{uuid.uuid4().hex}.tmp"
    )
    with temporary.open("x", encoding="utf-8", newline="\n") as file:
        json.dump(state, file, indent=2, sort_keys=True)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, CLIENT_STATE_FILE)

DOOM_STEAM_APP_ID = "782330"


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


def normalize_steam_remote_dir(path):
    """Accept remote, 782330, account, userdata, or Steam-root selections."""
    selected = Path(path).expanduser()

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
        raise ValueError(
            "Expected a DOOM Eternal Steam remote directory such as "
            "C:/Program Files (x86)/Steam/userdata/<ACCOUNT_ID>/782330/remote"
        )

    valid.sort(
        key=lambda pair: _steam_remote_candidate_score(pair[0]),
        reverse=True,
    )
    return valid[0]


def _steam_remote_candidate_score(remote):
    duration_files = list(
        remote.glob("GAME-AUTOSAVE*/game_duration.dat")
    )
    details_files = list(remote.glob("GAME-AUTOSAVE*/game.details"))
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
    duration_files = list(
        remote.glob("GAME-AUTOSAVE*/game_duration.dat")
    )
    details_files = list(remote.glob("GAME-AUTOSAVE*/game.details"))
    save_files = duration_files + details_files

    newest = None
    for save_file in save_files:
        try:
            mtime = save_file.stat().st_mtime
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)

    if newest is None:
        save_description = "no autosave files found yet"
    else:
        save_description = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(newest),
        )

    return (
        f"{remote} (Steam account {steam_id3}; "
        f"latest save: {save_description})"
    )


def prompt_for_steam_remote(candidates):
    """Ask only when automatic discovery cannot choose a usable directory."""
    has_tty = bool(sys.stdin and sys.stdin.isatty())

    if candidates and has_tty:
        print("\nFound DOOM Eternal Steam save directories:")
        for index, (remote, steam_id3) in enumerate(candidates, start=1):
            print(
                f"  {index}. "
                f"{_describe_steam_remote_candidate(remote, steam_id3)}"
            )

        while True:
            answer = input(
                f"Choose the active Steam account [1-{len(candidates)}] "
                "(default 1): "
            ).strip()
            if not answer:
                return candidates[0]
            try:
                index = int(answer)
            except ValueError:
                index = 0
            if 1 <= index <= len(candidates):
                return candidates[index - 1]
            print("Invalid selection.")

    while True:
        selected = None
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(
                title=(
                    "Select Steam userdata account or "
                    "DOOM Eternal 782330 remote folder"
                )
            )
            root.destroy()
        except Exception:
            selected = None

        if not selected and has_tty:
            print(
                "\nSelect the Steam directory that contains "
                "userdata/<ACCOUNT_ID>/782330/remote."
            )
            print(
                "Windows example: "
                "C:/Program Files (x86)/Steam"
            )
            selected = input("Steam path: ").strip()

        if not selected:
            return None, 0

        try:
            return normalize_steam_remote_dir(selected)
        except ValueError as error:
            if has_tty:
                print(f"Validation error: {error}")
                continue

            try:
                import tkinter as tk
                import tkinter.messagebox as messagebox

                root = tk.Tk()
                root.withdraw()
                messagebox.showerror(
                    "DOOM Eternal Steam save directory",
                    str(error),
                )
                root.destroy()
            except Exception:
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


DEATH_PROBE = Path(__file__).with_name("save_death_probe.exe")
DEATH_PROBE_RUNTIME = Path(__file__).parent / f".death-probe-{os.getpid()}"


def discover_oodle_dll():
    configured = config.get("oodle_dll")
    candidates = [
        Path(configured) if configured else Path(),
        Path(DOOM_BASE_DIR).parent / "oo2core_8_win64.dll",
        Path(DOOM_BASE_DIR) / "oo2core_8_win64.dll",
    ]
    return next((path for path in candidates if path.is_file()), Path())


def discover_proton():
    configured = config.get("proton_path")
    if configured and Path(configured).is_file():
        return Path(configured)

    common_dir = Path(DOOM_BASE_DIR).parent.parent
    candidates = sorted(
        common_dir.glob("Proton*/proton"),
        key=lambda path: ("Experimental" not in path.parent.name, path.parent.name),
    )
    return next((path for path in candidates if path.is_file()), Path())


def discover_compat_data():
    for parent in Path(SAVE_GAMES_DIR).parents:
        if parent.name == "pfx":
            return parent.parent
    return Path()


def discover_steam_install():
    if STEAM_REMOTE_DIR is None:
        return Path()

    for parent in STEAM_REMOTE_DIR.parents:
        if parent.name == "userdata":
            return parent.parent
    return Path()


OODLE_DLL = discover_oodle_dll()
PROTON_PATH = discover_proton()
STEAM_COMPAT_DATA = discover_compat_data()
STEAM_INSTALL = discover_steam_install()
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


def cleanup_death_probe_runtime():
    shutil.rmtree(DEATH_PROBE_RUNTIME, ignore_errors=True)


atexit.register(cleanup_death_probe_runtime)


class PrimarySaveSelection(NamedTuple):
    slot_directory: str
    path: Path
    mtime_ns: int

    @property
    def cache_key(self):
        return (self.slot_directory, str(self.path), self.mtime_ns)


class GameplaySaveEvidence(NamedTuple):
    state: str
    epoch: int
    slot_directory: str
    map_name: str
    provisional: bool = False


def primary_save_candidates(filename="game_duration.dat"):
    """Return valid primary slots newest-first; backups never qualify."""
    if (
        STEAM_REMOTE_DIR is None
        or STEAM_ID3 <= 0
        or not STEAM_REMOTE_DIR.is_dir()
    ):
        return []

    candidates = []
    for path in STEAM_REMOTE_DIR.glob(f"GAME-AUTOSAVE*/{filename}"):
        if not re.fullmatch(r"GAME-AUTOSAVE\d+", path.parent.name):
            continue
        try:
            stat = path.stat()
            full_path = path.resolve()
        except OSError:
            continue
        if not path.is_file() or stat.st_size <= 0:
            continue
        candidates.append(
            PrimarySaveSelection(path.parent.name, full_path, stat.st_mtime_ns)
        )
    return sorted(
        candidates,
        key=lambda selected: (
            selected.mtime_ns,
            int(selected.slot_directory.removeprefix("GAME-AUTOSAVE")),
        ),
        reverse=True,
    )


def active_primary_save(filename="game_duration.dat"):
    """Compatibility view of the newest candidate, not an active-slot proof."""
    candidates = primary_save_candidates(filename)
    return candidates[0] if candidates else None


def primary_save_for_slot(slot_directory, filename="game_duration.dat"):
    for selected in primary_save_candidates(filename):
        if selected.slot_directory == slot_directory:
            return selected
    return None


def read_gameplay_save_evidence(path=None):
    """Read the native gameplay/slot handshake published by ap_client.exe."""
    path = Path(path or GAMEPLAY_SAVE_EVIDENCE_PATH)
    try:
        values = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        state = values.get("state", "")
        epoch = int(values.get("epoch", "-1"))
        slot_directory = values.get("slot", "")
        map_name = canonical_map_name(values.get("map_name", ""))
    except (OSError, UnicodeError, ValueError):
        return None
    if state == "menu":
        return GameplaySaveEvidence(state, epoch, "", "")
    if (
        state != "gameplay"
        or epoch < 0
        or not re.fullmatch(r"GAME-AUTOSAVE\d+", slot_directory)
        or not map_name
    ):
        return None
    return GameplaySaveEvidence(
        state,
        epoch,
        slot_directory,
        map_name,
        values.get("provisional", "false").lower() == "true",
    )


def mastery_save_selection():
    return active_primary_save("game_duration.dat")


def mastery_save_file():
    """Compatibility path view of the dynamically selected primary save."""
    selected = mastery_save_selection()
    return selected.path if selected else None


def sticky_mastery_save_file():
    """Compatibility name for Sticky's shared primary-save reader."""
    return mastery_save_file()


def active_slot_file(filename):
    """Return a companion file from the newest game_duration primary slot."""
    selected = mastery_save_selection()
    if selected is None:
        return None
    path = selected.path.parent / filename
    return path if path.is_file() else None


def death_probe_available():
    if (
        STEAM_REMOTE_DIR is None
        or STEAM_ID3 <= 0
        or not STEAM_REMOTE_DIR.is_dir()
        or not DEATH_PROBE.is_file()
        or not OODLE_DLL.is_file()
    ):
        return False

    if os.name == "nt":
        return True

    return (
        PROTON_PATH.is_file()
        and STEAM_COMPAT_DATA.is_dir()
        and STEAM_INSTALL.is_dir()
    )


def _read_serialized_uint(payload, offset):
    """Read one width-prefixed little-endian unsigned value."""
    if offset >= len(payload):
        raise ValueError("metric value width is missing")
    width = payload[offset]
    if width < 1 or width > 8 or offset + 1 + width > len(payload):
        raise ValueError(f"invalid metric value width {width}")
    return (
        int.from_bytes(payload[offset + 1:offset + 1 + width], "little"),
        offset + 1 + width,
    )


MASTERY_MANAGER = b"UnlockableManager_0_1_2"
MASTERY_MANAGER_TYPE = b"idUnlockableManager_2"
STICKY_UNLOCKABLE = b"weapon_mastery/shotgun/sticky_bomb"


def _read_structured_bool(payload, offset, field):
    if not payload.startswith(field, offset):
        raise ValueError(f"unlockable record missing {field.decode('ascii').strip()}")
    value_offset = offset + len(field)
    try:
        value = {0x0B: False, 0x0C: True}[payload[value_offset]]
    except (IndexError, KeyError) as error:
        raise ValueError(f"unlockable record has invalid {field.decode('ascii').strip()}") from error
    return value, value_offset + 1


def _mastery_manager_type_offset(payload):
    manager_offset = payload.find(MASTERY_MANAGER)
    if manager_offset < 0 or payload.find(MASTERY_MANAGER, manager_offset + 1) >= 0:
        raise ValueError("native unlockable manager is missing or ambiguous")
    manager_type_offset = payload.find(MASTERY_MANAGER_TYPE, manager_offset)
    if (
        manager_type_offset < 0
        or payload.find(MASTERY_MANAGER_TYPE, manager_type_offset + 1) >= 0
    ):
        raise ValueError("native unlockable manager type is missing or ambiguous")
    return manager_type_offset


def read_unlockable_record(payload, entry):
    """Decode one exact native unlockable record; global stats are ignored."""
    signal = entry["signal"]
    unlockable = signal["unlockable"].encode("ascii")
    stat = signal["rule_0_statname"].encode("ascii")
    manager_type_offset = _mastery_manager_type_offset(payload)
    record_prefix = (
        bytes([len(unlockable) * 2]) + unlockable
        + b"\x0e\x0c$numUnlockableRules"
    )
    record_offset = payload.find(record_prefix, manager_type_offset)
    if (
        record_offset < manager_type_offset
        or payload.find(record_prefix, record_offset + 1) >= 0
    ):
        if record_offset < 0:
            return None
        raise ValueError(f"{signal['unlockable']}: native record is ambiguous")

    cursor = record_offset + len(record_prefix)
    rule_count, cursor = _read_serialized_uint(payload, cursor)
    satisfied, cursor = _read_structured_bool(payload, cursor, b" rule_0_satisfied")
    if not payload.startswith(b" rule_0_statCount", cursor):
        raise ValueError(f"{signal['unlockable']}: missing rule_0_statCount")
    stat_count, cursor = _read_serialized_uint(
        payload, cursor + len(b" rule_0_statCount")
    )
    if not payload.startswith(b"&rule_0_statDuration", cursor):
        raise ValueError(f"{signal['unlockable']}: missing rule_0_statDuration")
    stat_duration, cursor = _read_serialized_uint(
        payload, cursor + len(b"&rule_0_statDuration")
    )
    stat_prefix = b"\x1erule_0_statname\x0a"
    if not payload.startswith(stat_prefix, cursor):
        raise ValueError(f"{signal['unlockable']}: missing rule_0_statname")
    cursor += len(stat_prefix)
    stat_len = payload[cursor] // 2
    cursor += 1
    stat_bytes = payload[cursor:cursor + stat_len]
    cursor += stat_len
    unlocked, cursor = _read_structured_bool(
        payload, cursor, b"(unlockableIsUnlocked"
    )
    return {
        "numUnlockableRules": rule_count,
        "rule_0_statname": stat_bytes.decode("ascii", errors="ignore"),
        "rule_0_statCount": stat_count,
        "rule_0_statDuration": stat_duration,
        "rule_0_satisfied": satisfied,
        "unlockableIsUnlocked": unlocked,
    }


def read_weapon_mastery_record(payload, entry):
    """Compatibility wrapper for one exact Weapon Mastery record."""
    return read_unlockable_record(payload, entry)


def read_weapon_mastery_records(payload):
    """Return only structured records that exist in the fixed vanilla manager."""
    records = {}
    for entry in WEAPON_MASTERY_ENTRIES:
        try:
            record = read_weapon_mastery_record(payload, entry)
            if record is not None:
                records[entry["signal"]["unlockable"]] = record
        except Exception as error:
            logger.warning(
                "[Mastery] RECORD_PARSE_ERROR unlockable=%s error=%s",
                entry["signal"]["unlockable"], error,
            )
    return records


def read_mission_challenge_records(payload):
    """Return exact challenge records from the native manager."""
    records = {}
    for entry in MISSION_CHALLENGE_ENTRIES:
        try:
            record = read_unlockable_record(payload, entry)
            if record is not None:
                records[entry["signal"]["unlockable"]] = record
        except Exception as error:
            logger.warning(
                "[Challenge] RECORD_PARSE_ERROR unlockable=%s error=%s",
                entry["signal"]["unlockable"], error,
            )
    return records


def read_sticky_mastery_record(payload):
    """Compatibility view retaining Sticky's exact runtime-PASS record shape."""
    record = read_weapon_mastery_record(payload, STICKY_MASTERY_ENTRY)
    if record is None:
        raise ValueError("Sticky native record is missing")
    return {
        key: record[key]
        for key in (
            "rule_0_statname", "rule_0_statCount", "rule_0_satisfied",
            "unlockableIsUnlocked",
        )
    }


def probe_game_duration(path):
    """Return checkpoint-death and native unlockable records from one save."""
    DEATH_PROBE_RUNTIME.mkdir(parents=True, exist_ok=True)
    runtime_probe = DEATH_PROBE_RUNTIME / DEATH_PROBE.name
    runtime_oodle = DEATH_PROBE_RUNTIME / OODLE_DLL.name
    if not runtime_probe.exists():
        shutil.copy2(DEATH_PROBE, runtime_probe)
    if not runtime_oodle.exists():
        shutil.copy2(OODLE_DLL, runtime_oodle)

    encrypted = path.read_bytes()
    aad = f"{steam_id64(STEAM_ID3)}MANCUBUS{path.name}"
    runtime_save = DEATH_PROBE_RUNTIME / "game_duration.dat"
    runtime_save.write_bytes(decrypt(encrypted, aad))

    runtime_unpacked = DEATH_PROBE_RUNTIME / "game_duration.full.bin"
    if os.name == "nt":
        command = [
            str(runtime_probe), runtime_oodle.name, runtime_save.name,
            runtime_unpacked.name,
        ]
        environment = None
    else:
        DEATH_PROBE_COMPAT_DATA.mkdir(parents=True, exist_ok=True)
        proton_command = [
            str(PROTON_PATH),
            "run",
            runtime_probe.name,
            runtime_oodle.name,
            runtime_save.name,
            runtime_unpacked.name,
        ]
        if DISTROBOX_HOST_EXEC:
            command = [
                DISTROBOX_HOST_EXEC,
                "env",
                f"STEAM_COMPAT_DATA_PATH={DEATH_PROBE_COMPAT_DATA}",
                f"STEAM_COMPAT_CLIENT_INSTALL_PATH={STEAM_INSTALL}",
                *proton_command,
            ]
            environment = None
        else:
            command = proton_command
            environment = os.environ.copy()
            environment["STEAM_COMPAT_DATA_PATH"] = str(DEATH_PROBE_COMPAT_DATA)
            environment["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(STEAM_INSTALL)

    result = subprocess.run(
        command,
        cwd=DEATH_PROBE_RUNTIME,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode in {0, 20}:
        unpacked = runtime_unpacked.read_bytes()
        mastery_records = read_weapon_mastery_records(unpacked)
        snapshot = {
            "mastery_records": mastery_records,
            "mission_challenge_records": read_mission_challenge_records(unpacked),
        }
        sticky_record = mastery_records.get(STICKY_UNLOCKABLE.decode("ascii"))
        if sticky_record is not None:
            snapshot.update({
                key: sticky_record[key]
                for key in (
                    "rule_0_statname", "rule_0_statCount", "rule_0_satisfied",
                    "unlockableIsUnlocked",
                )
            })
        snapshot["checkpoint_death"] = result.returncode == 20
        return snapshot

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    raise RuntimeError(
        "save_death_probe exited with code "
        f"{result.returncode}; stdout={stdout!r}; stderr={stderr!r}"
    )


def probe_checkpoint_death(path):
    """Compatibility wrapper used by focused DeathLink tests."""
    return probe_game_duration(path)["checkpoint_death"]


# Load item definitions
ITEMS_FILE = os.path.join(os.path.dirname(__file__), "data", "items.json")
with open(ITEMS_FILE, encoding="utf-8") as f:
    # Keys in JSON are strings, convert them to ints
    _raw_items = json.load(f)
    ITEM_ID_TO_COMMAND = {int(k): v for k, v in _raw_items.items()}
ITEM_REPLAY_POLICIES_FILE = os.path.join(
    os.path.dirname(__file__), "data", "item_replay_policies.json"
)
ITEM_REPLAY_POLICIES = load_policy_registry(
    Path(ITEM_REPLAY_POLICIES_FILE), ITEM_ID_TO_COMMAND
)
ITEM_CLASSIFICATIONS_FILE = os.path.join(
    os.path.dirname(__file__), "data", "item_classifications.json"
)
_item_classification_document = json.loads(
    Path(ITEM_CLASSIFICATIONS_FILE).read_text(encoding="utf-8")
)
if (
    _item_classification_document.get("item_mapping_revision")
    != ITEM_MAPPING_REVISION
):
    raise RuntimeError(
        "Packaged item classification revision diverges from item mapping"
    )
ITEM_CLASSIFICATION_IDENTITY = load_item_classification_identity(
    Path(ITEM_CLASSIFICATIONS_FILE)
)
ITEM_CLASSIFICATIONS = {
    item_id: entry["classification"]
    for item_id, entry in ITEM_CLASSIFICATION_IDENTITY.items()
}
if set(ITEM_CLASSIFICATIONS) != set(ITEM_ID_TO_COMMAND):
    raise RuntimeError(
        "Packaged item classifications diverge from the item command mapping"
    )


def received_item_classification(item_id, network_classification):
    """Use the packaged identity when a compatible server reports stale flags."""
    if item_id not in ITEM_CLASSIFICATIONS:
        raise ValueError(f"item {item_id} has no packaged classification")
    expected = ITEM_CLASSIFICATIONS[item_id]
    if network_classification is None:
        classification = expected
    else:
        classification = int(network_classification)
        normalized = normalize_network_classification(item_id, classification)
        expected_normalized = normalize_network_classification(item_id, expected)
        if normalized != expected_normalized:
            logger.warning(
                "[To Game] CLASSIFICATION_MISMATCH item_id=%s server_flags=%s "
                "packaged_flags=%s mapping_revision=%s; using packaged identity",
                item_id, classification, expected, ITEM_MAPPING_REVISION,
            )
    notification_style_for_item(item_id, expected)
    return expected

RUNTIME_LOCATIONS_FILE = os.path.join(
    os.path.dirname(__file__), "data", "runtime_locations.json"
)
with open(RUNTIME_LOCATIONS_FILE, encoding="utf-8") as f:
    RUNTIME_LOCATIONS = json.load(f)
CULTIST_BASE_COMPLETE_LOCATION = RUNTIME_LOCATIONS[
    "Cultist Base - Mission Complete"
]
DOOM_HUNTER_BASE_COMPLETE_LOCATION = RUNTIME_LOCATIONS[
    "Doom Hunter Base - Mission Complete"
]
CHALLENGE_LOCATION_REGISTRY = load_challenge_registry()
OBSERVER_REGISTRY_REVISION = observer_registry_revision(CHALLENGE_LOCATION_REGISTRY)
WEAPON_MASTERY_ENTRIES = tuple(CHALLENGE_LOCATION_REGISTRY["weapon_masteries"])
WEAPON_MASTERY_BY_UNLOCKABLE = {
    entry["signal"]["unlockable"]: entry
    for entry in WEAPON_MASTERY_ENTRIES
}
MISSION_CHALLENGE_ENTRIES = tuple(
    CHALLENGE_LOCATION_REGISTRY["mission_challenges"]
)
MISSION_CHALLENGE_BY_UNLOCKABLE = {
    entry["signal"]["unlockable"]: entry
    for entry in MISSION_CHALLENGE_ENTRIES
}
KNOWN_CATALOG_MAPS = {
    "e1m1_intro": "game/sp/e1m1_intro/e1m1_intro",
    "e1m2_war": "game/sp/e1m2_war/e1m2_war",
    "e1m3_cult": "game/sp/e1m3_cult/e1m3_cult",
    "e1m4_boss": "game/sp/e1m4_boss/e1m4_boss",
    "e2m1_nest": "game/sp/e2m1_nest/e2m1_nest",
    "e2m2_base": "game/sp/e2m2_base/e2m2_base",
    "e2m3_core": "game/sp/e2m3_core/e2m3_core",
    "e2m4_boss": "game/sp/e2m4_boss/e2m4_boss",
    "e3m1_slayer": "game/sp/e3m1_slayer/e3m1_slayer",
    "e3m2_hell": "game/sp/e3m2_hell/e3m2_hell",
    "e3m2_hell_b": "game/sp/e3m2_hell_b/e3m2_hell_b",
    "e3m3_maykr": "game/sp/e3m3_maykr/e3m3_maykr",
    "e3m4_boss": "game/sp/e3m4_boss/e3m4_boss",
    "hub": "game/hub/hub",
}

MISSION_CHALLENGE_RUNTIME_MAP_BY_UNLOCKABLE = {
    entry["signal"]["unlockable"]: canonical_map_name(entry["runtime_map"])
    for entry in MISSION_CHALLENGE_ENTRIES
}
MISSION_CHALLENGE_RUNTIME_MAPS = frozenset(
    (set(MISSION_CHALLENGE_RUNTIME_MAP_BY_UNLOCKABLE.values()) | set(KNOWN_CATALOG_MAPS.values()))
    - {"game/hub/hub", ""}
)
ALL_MISSION_CHALLENGES_ENTRIES = list(
    CHALLENGE_LOCATION_REGISTRY.get("all_mission_challenges", [])
)
STICKY_MASTERY_ENTRY = WEAPON_MASTERY_BY_UNLOCKABLE[
    "weapon_mastery/shotgun/sticky_bomb"
]
STICKY_MASTERY_LOCATION = STICKY_MASTERY_ENTRY["location_id"]
PUBLISHERS = load_publisher_contracts()
PUBLISHER_ENGINE = PublisherEngine(PUBLISHERS)
PUBLISHER_MAP_EVENT_FILENAMES = frozenset(
    trigger["filename"]
    for publisher in PUBLISHERS
    for trigger in publisher.triggers_for("map_event_file")
)
# Load ALL level manifests dynamically
DECL_TO_LOCATION = {}
MANIFESTS_DIR = os.path.join(os.path.dirname(__file__), "manifests")
if os.path.exists(MANIFESTS_DIR):
    for filename in os.listdir(MANIFESTS_DIR):
        if filename.endswith(".json"):
            with open(os.path.join(MANIFESTS_DIR, filename), encoding="utf-8") as f:
                manifest_data = json.load(f)
                DECL_TO_LOCATION.update(manifest_data)

# Prototype-only cleanup registry. Each target removes one AP visual entity and
# has no edge to a vanilla pickup, relay, objective, or progression entity.
AUTOMAP_COMPLETION_BY_MAP = {
    canonical_map_name("game/sp/e1m1_intro/e1m1_intro"): {
        7770015: "ap_remove_location_visual_7770015",
    },
}

poll_counter = 0


def log_delivery_event(event: str, **fields) -> None:
    """Emit bounded, correlation-friendly delivery diagnostics only."""
    record = {"event": event, **{key: value for key, value in fields.items() if value is not None}}
    logger.info("DELIVERY_EVENT %s", json.dumps(record, sort_keys=True, separators=(",", ":")))


def command_spool_exists(command_id):
    queued_path = os.path.join(QUEUE_DIR, f"{command_id}.cmd")
    processing_path = os.path.join(QUEUE_DIR, f"{command_id}.processing")
    return os.path.exists(queued_path) or os.path.exists(processing_path)


def hold_orphaned_dev_jobs():
    """On bridge restart, keep dev jobs visible but require explicit resume."""
    os.makedirs(QUEUE_DIR, exist_ok=True)
    held = []
    for pattern in ("devtest-*.cmd", "devtest-*.processing"):
        for source_name in sorted(glob.glob(os.path.join(QUEUE_DIR, pattern))):
            source = Path(source_name)
            target = source.with_suffix(".held")
            if target.exists():
                target = source.with_name(source.name + ".held")
            try:
                os.replace(source, target)
                held.append(target)
            except OSError as error:
                logger.error("[DevLab] Could not hold orphaned job %s: %s", source, error)
    return held


def dev_job_paths():
    paths = []
    for suffix in ("cmd", "processing", "held"):
        paths.extend(Path(QUEUE_DIR).glob(f"devtest-*.{suffix}"))
    return sorted(paths)


def is_item_delivery_activation(command):
    return command.strip().startswith(f"ai_ScriptCmdEnt {RPC_ENTITY_PREFIX}_")


def delegated_rpc_command(item_id, command_index=None):
    entity_name = f"{RPC_ENTITY_PREFIX}_{item_id}"
    if command_index is not None:
        entity_name = f"{entity_name}_{command_index}"
    return f"ai_ScriptCmdEnt {entity_name} activate"


def bootstrap_activation(action_name):
    action = BOOTSTRAP_ACTIONS[action_name]
    return f"ai_ScriptCmdEnt {action['entity_name']} activate"


def send_command(
    cmd,
    coalesce_key=None,
    arm_rpc=True,
    already_queued_ok=False,
    delivery_fields=None,
):
    """Atomically enqueue one command without overwriting another command.

    A coalesced command has at most one queued or in-flight spool file. This is
    used for telemetry requests so menus/loading screens cannot accumulate a
    large condump backlog behind the player-state gate.
    """
    try:
        os.makedirs(QUEUE_DIR, exist_ok=True)
        command_id = coalesce_key or f"{time.time_ns():020d}-{uuid.uuid4().hex}"
        if coalesce_key:
            if command_spool_exists(coalesce_key):
                if delivery_fields is not None:
                    log_delivery_event(
                        "QUEUE_DUPLICATE_REJECT",
                        command_id=command_id,
                        reason="spool_exists",
                        **delivery_fields,
                    )
                return already_queued_ok

        temporary_path = os.path.join(
            QUEUE_DIR, f".{command_id}-{uuid.uuid4().hex}.tmp"
        )
        command_path = os.path.join(QUEUE_DIR, f"{command_id}.cmd")
        with open(temporary_path, "x", encoding="utf-8", newline="\n") as f:
            f.write(cmd.strip() + "\n")
            f.flush()
            os.fsync(f.fileno())
        if coalesce_key:
            try:
                os.link(temporary_path, command_path)
            except FileExistsError:
                if delivery_fields is not None:
                    log_delivery_event(
                        "QUEUE_DUPLICATE_REJECT",
                        command_id=command_id,
                        reason="cmd_exists",
                        **delivery_fields,
                    )
                return already_queued_ok
            finally:
                try:
                    os.remove(temporary_path)
                except FileNotFoundError:
                    pass
            processing_path = os.path.join(QUEUE_DIR, f"{coalesce_key}.processing")
            if os.path.exists(processing_path):
                try:
                    os.remove(command_path)
                except FileNotFoundError:
                    pass
                if delivery_fields is not None:
                    log_delivery_event(
                        "QUEUE_DUPLICATE_REJECT",
                        command_id=command_id,
                        reason="processing_exists",
                        **delivery_fields,
                    )
                return already_queued_ok
        else:
            os.replace(temporary_path, command_path)
        if arm_rpc:
            set_rpc_execution(True)
        if delivery_fields is not None:
            log_delivery_event(
                "SPOOL_CREATE",
                command_id=command_id,
                path=Path(command_path).name,
                **delivery_fields,
            )
        return True
    except Exception as e:
        logger.error(f"[Error] Failed to enqueue game command: {e}")
        return False


def expected_item_job_activation(item_id, command_index):
    definition = ITEM_ID_TO_COMMAND.get(item_id)
    try:
        if isinstance(definition, dict) and definition.get("type") == "progressive_perk":
            plan = compile_item_delivery_plan(
                item_id, ITEM_ID_TO_COMMAND, stage=command_index
            )
            return plan.commands[0].command, None
        plan = compile_item_delivery_plan(item_id, ITEM_ID_TO_COMMAND)
        if command_index >= len(plan.commands):
            return None, f"command index {command_index} exceeds delivery plan"
        return plan.commands[command_index].command, None
    except ValueError as error:
        return None, str(error)


def migrate_direct_item_command_jobs():
    """Rewrite old queued item jobs to map-side RPC activations.

    Only .cmd belongs to the bridge. A .processing file is owned by the native
    client's in-memory queue and must never be renamed or rewritten here.
    Native startup recovery handles interrupted .processing jobs exactly once.
    """
    try:
        os.makedirs(QUEUE_DIR, exist_ok=True)
    except Exception as error:
        logger.error(f"[Queue] Could not create queue directory for migration: {error}")
        return

    for pattern in ("*.cmd",):
        for source_path in sorted(glob.glob(os.path.join(QUEUE_DIR, pattern))):
            path = Path(source_path)
            try:
                command = path.read_text(encoding="utf-8").strip()
            except Exception as error:
                logger.error(f"[Queue] Could not read queued job for migration: {path}: {error}")
                continue
            match = re.match(
                r"recv-(\d+)-item-(\d+)-cmd-(\d+)\.(cmd|processing)$",
                path.name,
            )
            if not match:
                continue

            # A map-side activation is already the safe canonical payload.
            # Its suffix is authoritative (not the cmd-NN filename), so keep
            # the file contents byte-for-byte unchanged.
            if re.fullmatch(
                rf"ai_ScriptCmdEnt {RPC_ENTITY_PREFIX}_[0-9]+(?:_[0-9]+)? activate",
                command,
            ):
                continue

            legacy_effect_prefixes = (
                "give ", "chrispy ", "g_giveExtraLives ",
                "ai_ScriptCmdEnt player1 givePlayerPerk ",
            )
            if not command.startswith(legacy_effect_prefixes):
                continue

            receive_index = int(match.group(1))
            item_id = int(match.group(2))
            command_index = int(match.group(3))
            replacement, error = expected_item_job_activation(item_id, command_index)
            if replacement is None:
                logger.error(
                    f"[Queue] Direct item command left untouched; {error}: {path.name}"
                )
                continue
            if command == replacement:
                continue

            command_id = (
                f"recv-{receive_index:06d}-item-{item_id}-cmd-{command_index:02d}"
            )
            target_path = Path(QUEUE_DIR) / f"{command_id}.cmd"
            temporary_path = Path(QUEUE_DIR) / f".{command_id}-{uuid.uuid4().hex}.tmp"
            try:
                with temporary_path.open("x", encoding="utf-8", newline="\n") as file:
                    file.write(replacement + "\n")
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary_path, target_path)
                if path != target_path:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                logger.warning(
                    "MIGRATED_DIRECT_ITEM_COMMAND_TO_MAP_ENTITY "
                    f"command_id={command_id} old={command!r} new={replacement!r}"
                )
            except Exception as error:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
                logger.error(f"[Queue] Failed to migrate unsafe command {path}: {error}")

def telemetry_dump_files():
    files = set()
    for prefix in (TELEMETRY_DUMP_PREFIX, LEGACY_TELEMETRY_DUMP_PREFIX):
        files.update(
            glob.glob(os.path.join(INV_DUMP_DIR, f"{prefix}*.txt"))
        )
    return sorted(files)


def check_event_files():
    return sorted(glob.glob(os.path.join(INV_DUMP_DIR, f"{CHECK_EVENT_PREFIX}*.txt")))


def goal_event_files():
    return sorted(glob.glob(os.path.join(DOOM_BASE_DIR, f"{GOAL_EVENT_PREFIX}*.evt")))


def extract_location_id_from_event(path):
    basename = os.path.basename(path)
    filename_match = re.match(
        rf"^{CHECK_EVENT_PREFIX}(\d+)(?:_.*)?\.txt$",
        basename,
    )
    if filename_match:
        return int(filename_match.group(1))

    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            contents = f.read()
    except OSError:
        return None

    content_match = re.search(r"AP_CHECK_EVENT_(\d+)", contents)
    if content_match:
        return int(content_match.group(1))
    return None


def quarantine_event_file(path, old_state_key=None, new_state_key=None, reason="session_changed"):
    path = Path(path)
    if not path.exists():
        return
    quarantine_base = Path(INV_DUMP_DIR) / "ap_event_quarantine"
    timestamp_folder = time.strftime("%Y%m%d_%H%M%S")
    dest_dir = quarantine_base / timestamp_folder
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_path = dest_dir / path.name
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    location_id = extract_location_id_from_event(path)

    meta = {
        "filename": path.name,
        "parsed_location_id": location_id,
        "mtime_ns": mtime_ns,
        "old_state_key": old_state_key,
        "new_state_key": new_state_key,
        "reason": reason,
        "quarantined_at": time.time(),
    }

    try:
        shutil.move(str(path), str(dest_path))
        meta_path = dest_dir / f"{path.name}.meta.json"
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        logger.warning(
            "[Quarantine] Quarantined event file %s -> %s (reason: %s, location_id: %s)",
            path.name, dest_path, reason, location_id,
        )
    except Exception as err:
        logger.error("[Quarantine] Failed to quarantine event file %s: %s", path.name, err)


ACTIVE_MAP_MARKER_PREFIX = "ap_active_map"
def discover_active_map_markers():
    """Discover all suffixed map start identity files in INV_DUMP_DIR, ordered by mtime."""
    patterns = [
        os.path.join(INV_DUMP_DIR, f"{ACTIVE_MAP_MARKER_PREFIX}*.txt"),
        os.path.join(INV_DUMP_DIR, f"{TELEMETRY_DUMP_PREFIX}*.txt"),
    ]
    valid_files = []
    seen_paths = set()
    for pattern in patterns:
        for path in glob.glob(pattern):
            if path in seen_paths:
                continue
            seen_paths.add(path)
            basename = os.path.basename(path)
            if (
                re.match(rf"^{ACTIVE_MAP_MARKER_PREFIX}(?:_.*)?\.txt$", basename)
                or re.match(rf"^{TELEMETRY_DUMP_PREFIX}(?:_.*)?\.txt$", basename)
            ):
                try:
                    st = os.stat(path)
                    valid_files.append((st.st_mtime_ns, path))
                except OSError:
                    pass
    valid_files.sort(key=lambda item: item[0])
    return valid_files


def parse_active_map_marker(path, mtime_ns):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except OSError:
        return None

    matches = list(
        re.finditer(
            r"AP_ACTIVE_MAP_V1\s+map_key=(\S+)\s+runtime_map=(\S+)\s+marker=(\S+)",
            content,
        )
    )
    if not matches:
        return None

    last_match = matches[-1]
    map_key = last_match.group(1).rstrip(";")
    runtime_map = canonical_map_name(last_match.group(2).rstrip(";"))
    marker = last_match.group(3).rstrip(";")

    if map_key not in KNOWN_CATALOG_MAPS:
        return None

    expected_runtime = KNOWN_CATALOG_MAPS[map_key]
    if runtime_map != expected_runtime:
        return None

    expected_marker = f"AP_MAP_START_{map_key.upper()}"
    if marker != expected_marker:
        return None

    return {
        "map_key": map_key,
        "runtime_map": runtime_map,
        "marker": marker,
        "mtime_ns": mtime_ns,
        "path": path,
    }


def discover_telemetry_markers():
    """Discover all suffixed telemetry marker files in INV_DUMP_DIR, ordered by mtime."""
    pattern = os.path.join(INV_DUMP_DIR, f"{TELEMETRY_DUMP_PREFIX}*.txt")
    valid_files = []
    for path in glob.glob(pattern):
        basename = os.path.basename(path)
        if re.match(rf"^{TELEMETRY_DUMP_PREFIX}(?:_.*)?\.txt$", basename):
            try:
                st = os.stat(path)
                valid_files.append((st.st_mtime_ns, path))
            except OSError:
                pass
    valid_files.sort(key=lambda item: item[0])
    return valid_files


def parse_goal_transition_event(path, include_raw=False):
    data = {}
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                data[key] = value
    except OSError:
        return None

    if not data.get("from_map") or not data.get("to_map"):
        return None
    raw_from_map = data["from_map"]
    raw_to_map = data["to_map"]
    data["from_map"] = canonical_map_name(data["from_map"])
    data["to_map"] = canonical_map_name(data["to_map"])
    if include_raw:
        data["raw_from_map"] = raw_from_map
        data["raw_to_map"] = raw_to_map
    return data


def log_mission_bridge_identity():
    logger.info("BRIDGE_REVISION=%s", BRIDGE_REVISION)
    logger.info("BRIDGE_FILE=%s", BRIDGE_FILE)
    logger.info("BRIDGE_SHA256=%s", BRIDGE_SHA256)
    logger.info("BRIDGE_PROTOCOL=%s", BRIDGE_PROTOCOL)
    logger.info("GAME_NAME=%s", GAME_NAME)
    logger.info("TRANSITION_HANDLER=%s", TRANSITION_HANDLER)


def cleanup_telemetry_dumps():
    """Remove completed telemetry files before DOOM chooses a suffixed name."""
    removed_all = True
    for path in telemetry_dump_files():
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as error:
            removed_all = False
            logger.warning(
                f"[Telemetry] Could not remove {os.path.basename(path)} yet: "
                f"{error}"
            )
    return removed_all


def request_telemetry_dump():
    # A dump may arrive after the previous 1.5 second read window. Preserve it
    # for the next read instead of deleting an unchecked location and asking
    # DOOM for another file.
    if telemetry_dump_files():
        return False
    return send_command(
        f"condump {TELEMETRY_DUMP_PREFIX}.txt",
        coalesce_key="telemetry",
    )


def discard_queued_coalesced_command(coalesce_key):
    """Cancel queued and imported forms of one coalesced command."""
    for suffix in (".cmd", ".processing"):
        try:
            os.remove(os.path.join(QUEUE_DIR, f"{coalesce_key}{suffix}"))
        except FileNotFoundError:
            pass


def set_rpc_execution(enabled):
    if enabled:
        temporary_path = f"{RPC_GATE_PATH}.{uuid.uuid4().hex}.tmp"
        with open(temporary_path, "w", encoding="utf-8") as f:
            f.write("enabled\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, RPC_GATE_PATH)
    else:
        try:
            os.remove(RPC_GATE_PATH)
        except FileNotFoundError:
            pass

def rpc_execution_enabled():
    return os.path.isfile(RPC_GATE_PATH)


def read_telemetry_dump():
    files = telemetry_dump_files()

    if not files:
        return [], None

    latest_file = max(files, key=os.path.getmtime)
    try:
        if time.time() - os.path.getmtime(latest_file) < 0.5:
            return [], None
    except OSError:
        return [], None

    checks_found = set()
    map_name = None

    try:
        with open(latest_file, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            for line in lines:
                lower_line = line.lower()
                if lower_line.startswith("mapname:"):
                    _, value = line.split(":", 1)
                    map_name = canonical_map_name(value.strip())
                if "idbloatedentity::activate" in lower_line and "ap_check_" in lower_line:
                    match = re.search(r'(ap_check_[a-z0-9_]+)', lower_line)
                    if match:
                        checks_found.add(match.group(1).upper())

        cleanup_telemetry_dumps()

        return list(checks_found), map_name
    except Exception as e:
        logger.error(f"[Error] Failed to process telemetry condump: {e}")
        return [], None

def read_game_details_for_selection(selected):
    if selected is None:
        return None
    path = selected.path.parent / "game.details"
    if not path.is_file():
        return None

    aad = f"{steam_id64(STEAM_ID3)}MANCUBUS{path.name}"
    try:
        plaintext = decrypt(path.read_bytes(), aad).decode("utf-8")
        values = {}
        for line in plaintext.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        values["_path"] = str(path)
        values["_mtime_ns"] = path.stat().st_mtime_ns
        if "mapName" in values:
            values["mapName"] = canonical_map_name(values["mapName"])
        return values
    except Exception as error:
        logger.error(f"[Save] Failed to decrypt {path}: {error}")
        return None


def read_game_details():
    """Compatibility view; runtime observers use lifecycle-proven selections."""
    path = active_slot_file("game.details")
    if not path:
        return None
    selected = PrimarySaveSelection(path.parent.name, path.parent / "game_duration.dat", 0)
    return read_game_details_for_selection(selected)

class DoomCommandProcessor(ClientCommandProcessor):
    def _cmd_ap_reconcile(self):
        """Manually restore replay-safe AP inventory after Mission Reset."""
        plan, error = self.ctx.manual_reconcile_inventory()
        if error:
            self.output(f"AP reconcile rejected: {error}")
            return
        self.output(
            f"replayed={plan.replayed} special_stages={plan.special_stages} "
            f"skipped_never_replay={plan.skipped_never_replay} "
            f"skipped_unproven={plan.skipped_unproven}"
        )

    def _cmd_doom_rpc_on(self):
        """Arm RPC commands; the native memory gate still enforces safe gameplay."""
        set_rpc_execution(True)
        self.output(
            "RPC execution armed manually. The native memory gate opens only "
            "during safe gameplay."
        )

    def _cmd_doom_rpc_off(self):
        """Disarm all RPC commands until explicitly or automatically re-armed."""
        set_rpc_execution(False)
        self.output("RPC execution paused. Queued commands will be preserved.")

    def _cmd_doom_perk(self, perk_path: str = ""):
        """Reject the legacy raw perk command in favor of registered AP items."""
        self.output("Removed: use /doom_test_item <registered perk item ID>.")

    def _cmd_doom_item(self, item_id: str = ""):
        """Compatibility alias for the canonical directed-test item command."""
        return self._cmd_doom_test_item(item_id)

    def _cmd_doom_direct_chainsaw(self):
        """Queue the raw chainsaw give command, bypassing injected AP entities."""
        self.output("Removed: use /doom_test_item 7770010 through the map-side pipeline.")

    def _cmd_doom_progressive_item(
        self, item_id: str = "", stage: str = ""
    ):
        """Compatibility alias for the canonical staged lab item command."""
        return self._cmd_doom_test_item(item_id, "--stage", stage)

    def _cmd_doom_items_reset(self, confirmation: str = ""):
        """Reset exactly-once item history for the connected seed."""
        if confirmation != "CONFIRM":
            self.output("Usage: /doom_items_reset CONFIRM")
            return
        if rpc_execution_enabled():
            self.output("Pause RPC with /doom_rpc_off before resetting item history.")
            return
        if not self.ctx.item_state_ready:
            self.output("Connect to a slot before resetting item history.")
            return
        self.ctx.reset_item_state()
        self.output(
            "Item history reset. All received items, including consumables and traps, "
            "will be queued again."
        )

    def _cmd_doom_status(self):
        """Show user-facing integration and tracker status."""
        ctx = getattr(self, "ctx", None)
        alive = getattr(ctx, "tracker_alive", False)
        degraded = getattr(ctx, "tracker_degraded", False) or getattr(ctx, "item_delivery_blocked", False)
        status_str = "DEGRADED" if degraded else ("running" if alive else "stopped")
        hb_ts = getattr(ctx, "last_heartbeat_timestamp", None)
        hb_age = f"{time.time() - hb_ts:.1f}s" if hb_ts else "never"
        restarts = getattr(ctx, "tracker_restart_count", 0)
        last_err = getattr(ctx, "last_tracker_error", "none")
        backoff = getattr(ctx, "tracker_backoff", 1.0)
        consec_err = getattr(ctx, "consecutive_same_error_count", 0)
        blocked_info = getattr(ctx, "item_delivery_blocked_info", None)

        self.output(f"DOOM integration status: {status_str}")
        self.output(f"Tracker alive: {alive}")
        self.output(f"Last heartbeat age: {hb_age}")
        self.output(f"Restart count: {restarts}")
        self.output(f"Consecutive error count: {consec_err}")
        self.output(f"Current backoff: {backoff:.1f}s")
        self.output(f"Last error summary: {last_err}")
        if blocked_info:
            self.output(f"Blocked item: index={blocked_info.get('index')} id={blocked_info.get('item_id')} name={blocked_info.get('item_name')}")
        self.output(f"Detailed diagnostics: {BRIDGE_LOG_DIR}")

    def _cmd_doom_onboarding_status(self):
        """Show the compact, safe bootstrap onboarding state."""
        for line in self.ctx.onboarding_status_lines():
            self.output(line)

    def _cmd_doom_test_plan(
        self, item_id: str = "", stage_flag: str = "", stage_value: str = ""
    ):
        """Compile and display an item plan without creating a spool."""
        if item_id == "location":
            entry = load_foundation_contracts()["location_entrypoints"].get(stage_flag)
            if not entry:
                self.output("Usage: /doom_test_plan location <registered location id>")
                return
            record = load_primitive_registry()["primitives"][entry["primitive_id"]]
            self.output(
                f"location={stage_flag} primitive={entry['primitive_id']} "
                f"evidence={record['status']} entity={entry['entity']} map={entry['map']} "
                f"current_map={self.ctx.current_map_name or 'unknown'} destructive=yes"
            )
            return
        try:
            parsed_id = int(item_id)
            stage = None
            if stage_flag:
                if stage_flag != "--stage":
                    raise ValueError("expected --stage <index>")
                stage = int(stage_value)
            plan = compile_item_delivery_plan(
                parsed_id, ITEM_ID_TO_COMMAND, stage=stage
            )
        except (ValueError, TypeError) as error:
            self.output(f"Usage: /doom_test_plan <item id> [--stage N] ({error})")
            return
        record = load_primitive_registry()["primitives"][plan.primitive_id]
        map_supported = canonical_map_name(self.ctx.current_map_name) in {
            canonical_map_name(name)
            for name in load_foundation_contracts()["active_maps"].values()
        }
        self.output(
            f"item={plan.item_id} family={plan.family} primitive={plan.primitive_id} "
            f"evidence={record['status']} map={self.ctx.current_map_name or 'unknown'} "
            f"entities_expected={'yes' if map_supported else 'unknown'}"
        )
        for command in plan.commands:
            self.output(
                f"{command.index}: entity={command.entity} command={command.command}"
            )
        if not plan.commands:
            self.output("No gameplay command: runtime-only/no-op item.")

    def _cmd_doom_test_item(
        self, item_id: str = "", stage_flag: str = "", stage_value: str = ""
    ):
        """Execute the canonical item plan without simulating a NetworkItem."""
        try:
            parsed_id = int(item_id)
            stage = None
            if stage_flag:
                if stage_flag != "--stage":
                    raise ValueError("expected --stage <index>")
                stage = int(stage_value)
            plan = compile_item_delivery_plan(
                parsed_id, ITEM_ID_TO_COMMAND, stage=stage
            )
        except (ValueError, TypeError) as error:
            self.output(f"Usage: /doom_test_item <item id> [--stage N] ({error})")
            return
        correlation = self.ctx.queue_dev_plan(plan, "item")
        if correlation:
            self.output(
                f"Queued {len(plan.commands)} map-side command(s): {correlation}; effect unconfirmed."
            )

    def _cmd_doom_test_entity(self, entity: str = "", confirmation: str = ""):
        """Activate one allowlisted AP/test entity."""
        allowed = bool(re.fullmatch(r"ap_rpc_v3_[0-9]+(?:_[0-9]+)?", entity))
        allowed = allowed or entity.startswith("ap_test_")
        allowed = allowed or bool(re.fullmatch(r"ap_bootstrap_v[12]_[a-z_]+", entity))
        allowed = allowed or entity in set(DECL_TO_LOCATION)
        allowed = allowed or entity == "ap_independent_rocket_launcher_7770056"
        if not allowed:
            self.output("Entity rejected by the directed-test allowlist.")
            return
        correlation = self.ctx.queue_dev_commands(
            [f"ai_ScriptCmdEnt {entity} activate"], f"entity:{entity}"
        )
        self.output(f"Queued allowlisted entity: {correlation}; effect unconfirmed.")

    def _cmd_doom_test_bootstrap(self, action_name: str = ""):
        """Activate a historical bootstrap without touching persisted state."""
        if action_name == "suit_page":
            self.output("No active Suit Page bootstrap candidate.")
            self.output("The v2 stat-only candidate failed runtime validation.")
            return
        contracts = load_foundation_contracts()
        entity = contracts["bootstrap_test_entrypoints"].get(action_name)
        if not entity or action_name not in BOOTSTRAP_ACTIONS:
            self.output("Usage: /doom_test_bootstrap rune_page|frag_acquired|ice_acquired")
            return
        before = json.dumps(self.ctx.session_state.get("bootstrap", {}), sort_keys=True)
        correlation = self.ctx.queue_dev_commands(
            [f"ai_ScriptCmdEnt {entity} activate"], f"bootstrap:{action_name}"
        )
        after = json.dumps(self.ctx.session_state.get("bootstrap", {}), sort_keys=True)
        if before != after:
            raise RuntimeError("Dev bootstrap mutated production bootstrap state")
        self.output(
            f"Queued experimental {action_name}: {correlation}. Record menu state manually."
        )

    def _cmd_doom_test_location(
        self, location_id: str = "", confirmation: str = ""
    ):
        """Activate a registered map entrypoint; never fabricate a LocationCheck."""
        try:
            parsed_id = int(location_id)
        except ValueError:
            parsed_id = -1
        entry = load_foundation_contracts()["location_entrypoints"].get(str(parsed_id))
        if not entry:
            self.output("No registered directed-test entrypoint for that location.")
            return
        if confirmation != "--confirm":
            self.output(
                f"This can change the save/check. Re-run /doom_test_location {parsed_id} --confirm"
            )
            return
        if canonical_map_name(self.ctx.current_map_name) != canonical_map_name(entry["map"]):
            self.output(f"Wrong map: requires {entry['map']}, current={self.ctx.current_map_name}")
            return
        correlation = self.ctx.queue_dev_commands(
            [f"ai_ScriptCmdEnt {entry['entity']} activate"],
            f"location:{parsed_id}",
        )
        self.output(
            f"Queued map-side location entrypoint: {correlation}; check/objective remain runtime evidence."
        )

    def _cmd_doom_test_status(self):
        """Show isolated directed-test state."""
        self.output(f"map={self.ctx.current_map_name or 'unknown'}")
        self.output(f"last_action={self.ctx.dev_last_action or '-'}")
        self.output(f"last_correlation={self.ctx.dev_last_correlation or '-'}")
        self.output(f"pending_dev_jobs={len(dev_job_paths())}")
        self.output("primitive_registry=foundation.py (embedded registry)")
        self.output(f"logs={BRIDGE_LOG_DIR}")

    def _cmd_doom_test_resume(self):
        """Resume held jobs from a previous test process."""
        resumed = 0
        for source in list(Path(QUEUE_DIR).glob("devtest-*.held")):
            target = source.with_suffix(".cmd")
            if target.exists():
                continue
            os.replace(source, target)
            resumed += 1
        if resumed:
            set_rpc_execution(True)
        self.output(f"Resumed {resumed} held dev job(s).")

    def _cmd_doom_test_discard(self, confirmation: str = ""):
        """Archive, never delete, pending test jobs."""
        if confirmation != "--confirm":
            self.output("Usage: /doom_test_discard --confirm")
            return
        discarded = 0
        for source in dev_job_paths():
            target = source.with_name(source.name + ".discarded")
            os.replace(source, target)
            discarded += 1
        self.output(f"Archived {discarded} dev job(s).")

class DoomEternalContext(CommonContext):
    command_processor: type = DoomCommandProcessor
    game = GAME_NAME
    items_handling = 0b111

    def __init__(self, server_address, password):
        super().__init__(server_address, password)
        held_jobs = hold_orphaned_dev_jobs()
        if held_jobs:
            logger.warning("[Test] Held %d orphaned test job(s); use /doom_test_resume", len(held_jobs))
        self.dev_session_id = uuid.uuid4().hex[:8]
        self.dev_counter = 0
        self.dev_last_action = None
        self.dev_last_correlation = None
        self.tracking_task = None
        self.tracker_alive = False
        self.tracker_restart_count = 0
        self.last_tracker_error = None
        self.last_heartbeat_timestamp = None
        self.last_processed_event_id = None
        self.items_processed = 0
        self.item_state_ready = False
        self.client_state = {"version": 1, "sessions": {}}
        self.state_key = ""
        self.session_state = {}
        self.death_link_enabled = False
        self.previous_checkpoint_death = None
        self.checkpoint_death_by_save_slot = {}
        self.last_duration_cache_key = None
        self.death_probe_warning = None
        self.active_save_slot = None
        self.active_save_path = None
        self.active_save_token = None
        self.active_native_evidence_epoch = None
        self.active_save_proof_evidence_epoch = None
        self.active_save_proof_load_epoch = None
        self.active_save_proof_authoritative = False
        self.active_save_proof_slot = None
        self.runtime_observers_frozen = True
        self.runtime_observation_lease = RuntimeObservationLease()
        self.mission_select_observation_map = None
        self.mission_select_observation_epoch = None
        self.cached_map_identity = None
        self.pending_map_identity = None
        self.last_accepted_marker_mtime = None
        self.last_accepted_map_evidence_epoch = None
        self.session_map_completion_states = {}
        self.last_observer_lease_block = None
        self.save_candidate_tokens = {}
        self.last_save_slot_rejection = None
        self.save_slot_observations = {}
        self.selected_observation_slot = None
        self.last_mastery_records = {}
        self.weapon_masteries_observed = {}
        self.mastery_slot_warnings = set()
        self.last_mission_challenge_records = {}
        self.mission_challenges_observed = {}
        self.all_mission_challenges_observed: dict[str, bool] = {}
        self.mission_challenge_slot_warnings = set()
        self.last_sticky_record = None
        self.sticky_mastery_observed = False
        self.sticky_mastery_slot_warning = False
        self.confirmed_death_echo = None
        self.previous_died_last_game = None
        self.last_details_mtime = None
        self.last_details_path = None
        # Receive state is deliberately process-local. A restart drops pending
        # DeathLink delivery instead of replaying an old lethal command.
        self.deathlink_receiver = DeathLinkReceiver(
            wait_timeout=DEATHLINK_RECEIVE_TIMEOUT,
            confirm_timeout=DEATHLINK_CONFIRM_TIMEOUT,
        )
        self.last_goal_details_mtime = None
        self.final_sin_completion_candidate = None
        self.cultist_autosave_path = None
        self.mission_locations_in_flight = set()
        self.mission_goal_in_flight = False
        self.publisher_effects_in_flight = set()
        self.last_rpc_map_name = None
        self.room_seed_name = None
        self.current_map_name = None
        self.automap_cleanup_epoch = 0
        self.automap_cleanup_session = uuid.uuid4().hex[:8]
        self.automap_cleanup_delivered = {}

    def queue_dev_commands(self, commands, action):
        """Spool isolated dev commands without touching receipt/bootstrap state."""
        self.dev_counter += 1
        correlation = f"devtest-{self.dev_session_id}-{self.dev_counter:04d}"
        for index, command in enumerate(commands):
            if not re.fullmatch(r"ai_ScriptCmdEnt [A-Za-z0-9_]+ activate", command):
                raise ValueError("Directed tests accept only map-side entity activation")
            command_id = f"{correlation}-cmd-{index:02d}"
            if not send_command(command, coalesce_key=command_id):
                return None
            logger.info(
                "[Test] correlation=%s action=%s map=%s command_id=%s command=%s effect=unknown",
                correlation, action, self.current_map_name, command_id, command,
            )
        self.dev_last_action = action
        self.dev_last_correlation = correlation
        return correlation

    def queue_dev_plan(self, plan, action):
        return self.queue_dev_commands(
            [command.command for command in plan.commands],
            f"{action}:{plan.item_id}:{plan.family}:{plan.primitive_id}",
        )

    async def server_auth(self, password_requested: bool = False):
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.get_username()
        await self.send_connect()

    def on_package(self, cmd: str, args: dict):
        if cmd == "RoomInfo":
            self.room_seed_name = args.get("seed_name")
        elif cmd == "Connected":
            self.initialize_item_state()
            self.death_link_enabled = bool(args.get("slot_data", {}).get("death_link", False))
            self._death_link_task = asyncio.create_task(
                self.update_death_link(self.death_link_enabled)
            )
            self.onboard_bootstrap("on_connect")
            self.reconcile_checked_automap_cleanup("server_connected")
            asyncio.create_task(self.check_mission_challenge_locations())
            emit_launcher_event(
                "connected",
                seed_name=self.room_seed_name,
                team=args.get("team", self.team),
                slot=args.get("slot", self.slot),
                slot_data=args.get("slot_data", {}),
                missing_locations=sorted(args.get("missing_locations", [])),
                checked_locations=sorted(args.get("checked_locations", [])),
            )
        elif cmd == "ConnectionRefused":
            emit_launcher_event(
                "error",
                code="connection_refused",
                message="Archipelago connection was refused",
            )
        elif cmd == "RoomUpdate" and "checked_locations" in args:
            self.reconcile_checked_automap_cleanup("server_checked_update")
            asyncio.create_task(self.check_mission_challenge_locations())
        elif cmd == "Bounced" and "DeathLink" in args.get("tags", []):
            data = args.get("data", {})
            if (
                data.get("time") == self.last_death_link
                and data.get("time") != self.confirmed_death_echo
            ):
                logger.info("[DeathLink] Server received and echoed the death.")
                self.confirmed_death_echo = data.get("time")

    def observation_slot_for_source(self, source):
        if isinstance(source, PrimarySaveSelection):
            return source.slot_directory
        try:
            parent = Path(source).parent.name
        except (TypeError, ValueError):
            parent = ""
        if re.fullmatch(r"GAME-AUTOSAVE\d+", parent):
            return parent
        return self.active_save_slot or "<synthetic>"

    def select_save_observation_slot(self, slot_directory):
        """Select a slot without trusting legacy observed=true persistence."""
        if getattr(self, "selected_observation_slot", None) == slot_directory:
            return
        self.selected_observation_slot = slot_directory
        state = self.save_slot_observations.setdefault(slot_directory, {})
        state.pop("weapon_masteries", None)
        state.pop("mission_challenges", None)
        self.weapon_masteries_observed = {
            unlockable: False for unlockable in WEAPON_MASTERY_BY_UNLOCKABLE
        }
        self.mission_challenges_observed = {
            unlockable: False for unlockable in MISSION_CHALLENGE_BY_UNLOCKABLE
        }
        self.all_mission_challenges_observed = {}
        self.sticky_mastery_observed = False

    def has_authoritative_save_proof(self):
        if self.runtime_observers_frozen:
            return False
        proof_auth = getattr(self, "active_save_proof_authoritative", None)
        if proof_auth is False:
            return False
        lease = getattr(self, "runtime_observation_lease", None)
        if lease is not None and getattr(self, "active_save_proof_load_epoch", None) != getattr(lease, "gameplay_loaded_ns", None):
            return False
        if self.active_save_slot is not None:
            proof_slot = getattr(self, "active_save_proof_slot", self.active_save_slot)
            return bool(proof_slot == self.active_save_slot)
        return True

    def invalidate_active_save_proof(self):
        """Clear proof authority at a gameplay/process lifecycle boundary."""
        self.active_save_proof_authoritative = False
        self.active_save_proof_slot = None
        self.active_save_proof_evidence_epoch = None
        self.active_save_proof_load_epoch = None
        self.runtime_observers_frozen = True

    def ingest_visible_runtime_lifecycle(self, evidence=None, lifecycle_markers=None):
        """Consume a newly visible load before any observer can continue authority."""
        lease = getattr(self, "runtime_observation_lease", None)
        evidence_epoch = getattr(evidence, "epoch", None)
        proof_evidence_epoch = getattr(
            self, "active_save_proof_evidence_epoch", None
        )
        if (
            getattr(self, "active_save_proof_authoritative", False)
            and evidence_epoch is not None
            and proof_evidence_epoch is not None
            and evidence_epoch != proof_evidence_epoch
        ):
            self.invalidate_active_save_proof()

        markers = (
            lifecycle_markers
            if lifecycle_markers is not None
            else discover_active_map_markers()
        )
        if not markers:
            return (
                getattr(self, "pending_map_identity", None)
                or getattr(self, "cached_map_identity", None)
            )
        newest_mtime, newest_path = markers[-1]
        current = getattr(lease, "gameplay_loaded_ns", None) if lease else None
        started = getattr(lease, "started_ns", None) if lease else None
        if started is not None and newest_mtime < started:
            return (
                getattr(self, "pending_map_identity", None)
                or getattr(self, "cached_map_identity", None)
            )
        if current is not None and newest_mtime <= current:
            return (
                getattr(self, "pending_map_identity", None)
                or getattr(self, "cached_map_identity", None)
            )
        if lease is not None:
            lease.observe_gameplay_loaded(newest_mtime)
        self.invalidate_active_save_proof()
        self.mission_select_observation_map = None
        self.mission_select_observation_epoch = None
        self.current_map_name = None
        self.cached_map_identity = None
        self.pending_map_identity = None
        marker_data = parse_active_map_marker(newest_path, newest_mtime)
        if marker_data is None:
            return self.invalidate_map_identity("malformed_marker")
        return self.store_pending_map_identity(
            {**marker_data, "gameplay_epoch": newest_mtime}
        )

    def activate_save_selection(self, selected):
        old_slot = self.active_save_slot
        path_changed = str(selected.path) != self.active_save_path
        if old_slot != selected.slot_directory or path_changed:
            logger.info(
                "SAVE_SLOT_ACTIVE old=%s new=%s path=%s",
                old_slot or "<none>",
                selected.slot_directory,
                selected.path,
            )
        self.active_save_slot = selected.slot_directory
        self.active_save_path = str(selected.path)
        self.active_save_token = selected.mtime_ns
        self.active_save_proof_authoritative = True
        self.active_save_proof_slot = selected.slot_directory
        self.select_save_observation_slot(selected.slot_directory)
        self.previous_checkpoint_death = self.checkpoint_death_by_save_slot.get(
            selected.slot_directory
        )

    def invalidate_save_observation_slot(self, slot_directory):
        """Discard local authority when a slot directory has been recreated."""
        self.save_slot_observations[slot_directory] = {}
        self.selected_observation_slot = None
        self.select_save_observation_slot(slot_directory)
        self.checkpoint_death_by_save_slot.pop(slot_directory, None)
        self.previous_checkpoint_death = None

    def log_save_proof_rejected(
        self, reason, evidence_slot=None, evidence_map=None, candidate_slot=None, candidate_mtime=None, active_slot=None, details_map=None
    ):
        rejection = (
            reason,
            evidence_slot or "<none>",
            evidence_map or "<none>",
            candidate_slot or "<none>",
            candidate_mtime or 0,
            active_slot or self.active_save_slot or "<none>",
            details_map or "<none>",
        )
        if rejection == getattr(self, "last_save_proof_rejection", None):
            return
        self.last_save_proof_rejection = rejection
        logger.info(
            "SAVE_PROOF_REJECTED reason=%s evidence_slot=%s evidence_map=%s "
            "candidate_slot=%s candidate_mtime=%s active_slot=%s details_map=%s",
            *rejection,
        )
        logger.info(
            "SAVE_SLOT_REJECTED slot=%s reason=%s path=%s",
            candidate_slot or "<none>",
            reason,
            candidate_slot or "<none>",
        )

    def invalidate_map_identity(self, reason, *, clear_pending=True):
        if getattr(self, "last_marker_reject_reason", None) != reason:
            logger.info("[MAP] MAP_IDENTITY_MARKER_REJECTED reason=%s", reason)
            self.last_marker_reject_reason = reason
        self.current_map_name = None
        self.cached_map_identity = None
        if clear_pending:
            self.pending_map_identity = None
        self.mission_select_observation_map = None
        self.mission_select_observation_epoch = None
        return None

    def store_pending_map_identity(self, marker_data):
        self.pending_map_identity = {**marker_data, "evidence_epoch": None}
        self.current_map_name = None
        self.cached_map_identity = None
        self.runtime_observers_frozen = True
        if getattr(self, "last_pending_marker_mtime", None) != marker_data["mtime_ns"]:
            self.last_pending_marker_mtime = marker_data["mtime_ns"]
            logger.info(
                "[MAP] MAP_IDENTITY_MARKER_PENDING map=%s runtime_map=%s "
                "source=map_start_event epoch=%s",
                marker_data["map_key"],
                marker_data["runtime_map"],
                marker_data["gameplay_epoch"],
            )
        return self.pending_map_identity

    def accept_map_identity(self, marker_data, evidence_epoch):
        marker_data = {
            **marker_data,
            "evidence_epoch": evidence_epoch,
        }
        self.pending_map_identity = None
        self.cached_map_identity = marker_data
        self.current_map_name = marker_data["runtime_map"]
        marker_mtime = marker_data["mtime_ns"]
        if self.last_accepted_marker_mtime != marker_mtime:
            self.last_accepted_marker_mtime = marker_mtime
            self.last_accepted_map_evidence_epoch = evidence_epoch
            self.last_marker_reject_reason = None
            logger.info(
                "[MAP] MAP_IDENTITY_MARKER_ACCEPTED map=%s runtime_map=%s "
                "source=map_start_event epoch=%s",
                marker_data["map_key"],
                marker_data["runtime_map"],
                marker_data["gameplay_epoch"],
            )
        return marker_data

    def read_active_map_identity(self, evidence=None):
        lease = getattr(self, "runtime_observation_lease", None)
        self.ingest_visible_runtime_lifecycle(evidence=evidence)
        if lease is not None and not lease.process_probe():
            self.invalidate_active_save_proof()
            return self.invalidate_map_identity("game_not_running")

        pending = getattr(self, "pending_map_identity", None)
        if evidence is None:
            if pending is not None:
                self.current_map_name = None
                self.cached_map_identity = None
                return None
            cached = self.cached_map_identity
            lease_epoch = (
                lease.gameplay_loaded_ns
                if lease is not None and lease.gameplay_loaded_ns
                else None
            )
            if (
                cached is not None
                and (lease_epoch is None or cached.get("gameplay_epoch") == lease_epoch)
            ):
                return self.accept_map_identity(
                    cached, cached.get("evidence_epoch")
                )
            return self.invalidate_map_identity("gameplay_not_loaded")

        if getattr(evidence, "state", None) != "gameplay":
            self.invalidate_active_save_proof()
            if pending is not None:
                self.current_map_name = None
                self.cached_map_identity = None
                return None
            return self.invalidate_map_identity("menu")

        lease_epoch = (
            lease.gameplay_loaded_ns
            if lease is not None and lease.gameplay_loaded_ns
            else None
        )
        evidence_epoch = getattr(evidence, "epoch", None)

        if pending is not None:
            pending_mtime = pending.get("mtime_ns", 0)
            if lease is not None and lease.started_ns and pending_mtime < lease.started_ns:
                return self.invalidate_map_identity("stale_marker")
            if (
                lease_epoch is not None
                and pending.get("gameplay_epoch") != lease_epoch
            ):
                return self.invalidate_map_identity("epoch_mismatch")
            return self.accept_map_identity(pending, evidence_epoch)

        cached = self.cached_map_identity
        markers = discover_active_map_markers()
        if not markers:
            if cached is not None:
                cached_mtime = cached.get("mtime_ns", 0)
                if lease is not None and lease.started_ns and cached_mtime < lease.started_ns:
                    return self.invalidate_map_identity("stale_marker")
                if lease_epoch is not None and cached.get("gameplay_epoch") != lease_epoch:
                    return self.invalidate_map_identity("epoch_mismatch")
                cached_evidence_epoch = cached.get("evidence_epoch")
                if (
                    cached_evidence_epoch is not None
                    and cached_evidence_epoch != evidence_epoch
                ):
                    return self.invalidate_map_identity("epoch_mismatch")
                return self.accept_map_identity(cached, evidence_epoch)
            return None

        newest_mtime, newest_path = markers[-1]
        if (
            cached is not None
            and lease_epoch is not None
            and cached.get("gameplay_epoch") == lease_epoch
            and cached.get("evidence_epoch") in (None, evidence_epoch)
            and newest_mtime <= cached.get("mtime_ns", 0)
        ):
            return self.accept_map_identity(cached, evidence_epoch)

        marker_data = parse_active_map_marker(newest_path, newest_mtime)
        if marker_data is None:
            return self.invalidate_map_identity("malformed_marker")

        if lease is not None and lease.started_ns and newest_mtime < lease.started_ns:
            return self.invalidate_map_identity("stale_marker")

        if lease_epoch is not None and newest_mtime < lease_epoch:
            return self.invalidate_map_identity("epoch_mismatch")

        if (
            newest_mtime == self.last_accepted_marker_mtime
            and self.last_accepted_map_evidence_epoch is not None
            and self.last_accepted_map_evidence_epoch != evidence_epoch
        ):
            return self.invalidate_map_identity("epoch_mismatch")

        marker_data = {
            **marker_data,
            "gameplay_epoch": lease_epoch or newest_mtime,
        }
        return self.accept_map_identity(marker_data, evidence_epoch)

    def log_save_proof_accepted(
        self, slot, map_name, epoch, duration_token, details_token, proof="non_provisional_fresh_map_match"
    ):
        logger.info(
            "SAVE_PROOF_ACCEPTED slot=%s map=%s epoch=%s game_duration_token=%s "
            "game_details_token=%s proof=%s",
            slot,
            map_name,
            epoch,
            duration_token,
            details_token,
            proof,
        )

    def update_save_slot_lifecycle(self):
        """Keep an authoritative slot through transient samples; prove switches."""
        candidates = primary_save_candidates()
        for selected in candidates:
            token = (str(selected.path), selected.mtime_ns)
            if self.save_candidate_tokens.get(selected.slot_directory) != token:
                self.save_candidate_tokens[selected.slot_directory] = token
                logger.info(
                    "SAVE_SLOT_CANDIDATE slot=%s path=%s mtime_ns=%s",
                    selected.slot_directory,
                    selected.path,
                    selected.mtime_ns,
                )

        evidence = read_gameplay_save_evidence()
        self.ingest_visible_runtime_lifecycle(evidence=evidence)
        marker = self.read_active_map_identity(evidence=evidence)
        marker_map = marker["runtime_map"] if marker else None

        evidence_slot = evidence.slot_directory if (evidence and getattr(evidence, "slot_directory", None)) else None
        evidence_state = evidence.state if (evidence and getattr(evidence, "state", None)) else None
        evidence_epoch = evidence.epoch if (evidence and getattr(evidence, "epoch", None) is not None) else None

        lease = getattr(self, "runtime_observation_lease", None)
        lease_epoch = lease.gameplay_loaded_ns if (lease and getattr(lease, "gameplay_loaded_ns", None)) else None

        proof_evidence_epoch = (
            evidence_epoch
            if evidence_epoch is not None
            else getattr(self, "active_save_proof_evidence_epoch", None)
        )
        proof_load_epoch = lease_epoch

        newest = candidates[0] if candidates else None
        active = primary_save_for_slot(self.active_save_slot) if self.active_save_slot else None
        candidate_slot = newest.slot_directory if newest else None
        candidate_mtime = newest.mtime_ns if newest else 0

        newer_unproven_candidate = bool(
            newest
            and active
            and newest.slot_directory != active.slot_directory
            and newest.mtime_ns > active.mtime_ns
        )

        def fail_proof(reason, details_map=None):
            self.runtime_observers_frozen = True
            self.mission_select_observation_map = None
            self.mission_select_observation_epoch = None
            self.log_save_proof_rejected(
                reason,
                evidence_slot=evidence_slot,
                evidence_map=marker_map or (evidence.map_name if evidence else None),
                candidate_slot=candidate_slot,
                candidate_mtime=candidate_mtime,
                active_slot=self.active_save_slot,
                details_map=details_map,
            )
            return None

        def continue_authoritative_active():
            if self.mission_select_observation_map:
                return None
            if (
                not getattr(self, "active_save_proof_authoritative", False)
                or getattr(self, "active_save_proof_slot", None)
                != self.active_save_slot
                or getattr(self, "active_save_proof_load_epoch", None) != proof_load_epoch
                or active is None
                or newer_unproven_candidate
            ):
                return None
            self.active_save_path = str(active.path)
            if self.active_save_token != active.mtime_ns:
                self.invalidate_save_observation_slot(active.slot_directory)
                self.active_save_token = active.mtime_ns
            self.runtime_observers_frozen = False
            return active

        if lease is not None and not lease.process_probe():
            self.invalidate_active_save_proof()
            if self.last_observer_lease_block != "game_not_running":
                logger.info("[OBSERVER] LIVE_LEASE_BLOCKED reason=game_not_running")
                self.last_observer_lease_block = "game_not_running"
            return fail_proof("game_not_running")

        if evidence is None and marker is None:
            if newer_unproven_candidate:
                return fail_proof("no_gameplay_evidence")
            continued = continue_authoritative_active()
            if continued is not None:
                return continued
            return fail_proof("no_gameplay_evidence")

        if evidence and evidence.state != "gameplay":
            self.invalidate_active_save_proof()
            return fail_proof("menu")

        if evidence and evidence.provisional and marker is None:
            continued = continue_authoritative_active()
            if continued is not None:
                return continued
            return fail_proof("provisional")

        target_slot = evidence_slot or (active.slot_directory if active else None) or candidate_slot
        if not target_slot or not re.match(r"^GAME-AUTOSAVE[0-9]+$", target_slot):
            return fail_proof("invalid_evidence_slot")

        selected = primary_save_for_slot(target_slot)
        if selected is None:
            return fail_proof("no_gameplay_evidence")

        details = read_game_details_for_selection(selected)
        if not details:
            return fail_proof("no_game_details")

        details_map = canonical_map_name(details.get("mapName"))
        expected_map = marker_map or (evidence.map_name if evidence else None) or canonical_map_name(self.current_map_name or "")
        if lease is not None:
            try:
                evidence_mtime_ns = Path(GAMEPLAY_SAVE_EVIDENCE_PATH).stat().st_mtime_ns
            except OSError:
                evidence_mtime_ns = marker["mtime_ns"] if marker else 0
            live, reason = lease.validate(
                evidence_mtime_ns=evidence_mtime_ns,
                evidence_state=evidence.state if evidence else "gameplay",
                evidence_map=marker_map or (evidence.map_name if evidence else ""),
                current_map=expected_map,
                details_map=details_map,
            )
            if live:
                self.mission_select_observation_map = None
                self.mission_select_observation_epoch = None
            if not live:
                mission_select_live = False
                if (
                    (reason == "map_mismatch" or marker_map is not None)
                    and expected_map in MISSION_CHALLENGE_RUNTIME_MAPS
                    and details_map != expected_map
                ):
                    mission_select_live, reason = lease.validate_mission_select(
                        evidence_mtime_ns=evidence_mtime_ns or (marker["mtime_ns"] if marker else 0),
                        evidence_state=evidence.state if evidence else "gameplay",
                        evidence_map=expected_map,
                        current_map=expected_map,
                        mission_map=expected_map,
                        save_mtime_ns=selected.mtime_ns,
                    )
                if mission_select_live:
                    self.mission_select_observation_map = expected_map
                    self.mission_select_observation_epoch = lease.gameplay_loaded_ns
                    if getattr(self, "last_accepted_mission_select_epoch", None) != lease.gameplay_loaded_ns:
                        self.last_accepted_mission_select_epoch = lease.gameplay_loaded_ns
                        logger.info(
                            "[OBSERVER] MISSION_SELECT_LEASE_ACCEPTED slot=%s map=%s "
                            "load_epoch=%s save_mtime_ns=%s stale_details_map=%s",
                            selected.slot_directory,
                            expected_map,
                            lease.gameplay_loaded_ns,
                            selected.mtime_ns,
                            details_map,
                        )
                else:
                    self.mission_select_observation_map = None
                    self.mission_select_observation_epoch = None
            if not live and not mission_select_live:
                if reason != self.last_observer_lease_block:
                    logger.info("[OBSERVER] LIVE_LEASE_BLOCKED reason=%s", reason)
                    self.last_observer_lease_block = reason
                return fail_proof(reason, details_map=details_map)
            self.last_observer_lease_block = None

        if self.mission_select_observation_map:
            pass
        elif expected_map == "game/hub/hub":
            if details_map != "game/hub/hub":
                return fail_proof("map_mismatch", details_map=details_map)
        else:
            if details_map != expected_map:
                return fail_proof("map_mismatch", details_map=details_map)

        is_current_active_slot = (
            self.active_save_slot == selected.slot_directory
            and self.active_save_path == str(selected.path)
        )

        details_token = details.get("_mtime_ns", selected.mtime_ns)

        if not is_current_active_slot:
            if self.active_native_evidence_epoch == proof_evidence_epoch and self.active_save_slot is not None:
                return fail_proof("unproven_epoch", details_map=details_map)

            # SAVE_PROOF_ACCEPTED MUST precede SAVE_SLOT_ACTIVE
            self.log_save_proof_accepted(
                selected.slot_directory,
                expected_map,
                proof_evidence_epoch,
                selected.mtime_ns,
                details_token,
                proof="non_provisional_fresh_map_match",
            )
            self.activate_save_selection(selected)
            self.active_native_evidence_epoch = proof_evidence_epoch
            self.active_save_proof_authoritative = True
            self.active_save_proof_slot = selected.slot_directory
            self.active_save_proof_evidence_epoch = proof_evidence_epoch
            self.active_save_proof_load_epoch = proof_load_epoch
            self.runtime_observers_frozen = False
            self.arm_final_sin_completion_candidate(
                selected, details, expected_map, proof_load_epoch
            )
            return selected
        else:
            if not self.mission_select_observation_map:
                self.mission_select_observation_epoch = None
            if evidence_epoch is not None and self.active_native_evidence_epoch != evidence_epoch:
                self.log_save_proof_accepted(
                    selected.slot_directory,
                    expected_map,
                    evidence_epoch,
                    selected.mtime_ns,
                    details_token,
                    proof="non_provisional_fresh_map_match",
                )
                if self.active_save_token != selected.mtime_ns:
                    self.invalidate_save_observation_slot(selected.slot_directory)
                self.active_native_evidence_epoch = evidence_epoch
                self.active_save_token = selected.mtime_ns

            self.active_save_proof_authoritative = True
            self.active_save_proof_slot = selected.slot_directory
            self.active_save_proof_evidence_epoch = proof_evidence_epoch
            self.active_save_proof_load_epoch = proof_load_epoch
            self.runtime_observers_frozen = False
            self.arm_final_sin_completion_candidate(
                selected, details, expected_map, proof_load_epoch
            )
            return selected

    def active_game_details(self):
        selected = self.update_save_slot_lifecycle()
        return read_game_details_for_selection(selected) if selected else None

    def get_ap_state_key(self):
        if not getattr(self, "server", None) or not getattr(self, "auth", None) or not getattr(self, "state_key", None):
            return None
        effective_seed_name = getattr(self, "room_seed_name", None) or getattr(self, "seed_name", None) or "unknown_seed"
        team = getattr(self, "team", 0)
        slot = getattr(self, "slot", 0)
        auth = str(self.auth or "unknown_auth")
        return f"{effective_seed_name}:{team}:{slot}:{auth}:{BRIDGE_REVISION}"

    def check_and_update_event_session(self):
        current_key = self.get_ap_state_key()
        if not current_key:
            return False

        session_file = Path(INV_DUMP_DIR) / "ap_event_session.json"
        old_key = None
        if session_file.exists():
            try:
                data = json.loads(session_file.read_text(encoding="utf-8"))
                old_key = data.get("ap_state_key")
            except Exception:
                pass

        if old_key != current_key:
            quarantine_reason = "session_changed" if old_key else "unbound_preexisting"
            self.quarantine_unbound_physical_events(
                old_state_key=old_key,
                new_state_key=current_key,
                reason=quarantine_reason,
            )
            tmp = session_file.with_name(f".ap_event_session.{uuid.uuid4().hex}.tmp")
            tmp.write_text(
                json.dumps({"ap_state_key": current_key, "updated_at": time.time()}, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, session_file)
            logger.info("[Session] Persisted new physical event state key: %s (old: %s)", current_key, old_key)
        return True

    def quarantine_unbound_physical_events(
        self, old_state_key=None, new_state_key=None, reason="session_changed"
    ):
        for path in check_event_files():
            if os.path.basename(path) in PUBLISHER_MAP_EVENT_FILENAMES:
                continue
            quarantine_event_file(
                path,
                old_state_key=old_state_key,
                new_state_key=new_state_key,
                reason=reason,
            )

    def initialize_item_state(self):
        self.client_state = load_client_state()
        effective_seed_name = self.room_seed_name or self.seed_name
        if not effective_seed_name or self.team is None or self.slot is None:
            logger.warning(
                "[State] Slot identity incomplete at connect time; "
                "falling back to legacy state key."
            )
            effective_seed_name = effective_seed_name or "None"
            self.team = 0 if self.team is None else self.team
            self.slot = 0 if self.slot is None else self.slot
        self.state_key = f"{effective_seed_name}:{self.team}:{self.slot}"
        sessions = self.client_state["sessions"]
        legacy_key = f"None:{self.team}:{self.slot}"
        if self.state_key not in sessions and legacy_key in sessions:
            sessions[self.state_key] = sessions.pop(legacy_key)
            logger.info(
                f"[State] Migrated legacy session key {legacy_key} -> "
                f"{self.state_key}."
            )
        self.session_state = sessions.setdefault(
            self.state_key,
            {
                "processed_items": 0,
                "goal_sent": False,
                "cultist_autosave_path": None,
                "save_slot_observations": {},
            },
        )
        processed = self.session_state.get("processed_items", 0)
        if not isinstance(processed, int) or processed < 0:
            processed = 0
            self.session_state["processed_items"] = 0
        self.items_processed = processed
        self.cultist_autosave_path = self.session_state.get(
            "cultist_autosave_path"
        )
        # Never restore received DeathLink state across a process restart.
        self.session_state.pop("deathlinked", None)
        raw_save_observations = self.session_state.get("save_slot_observations", {})
        if not isinstance(raw_save_observations, dict):
            raw_save_observations = {}
        self.save_slot_observations = {
            slot_directory: state
            for slot_directory, state in raw_save_observations.items()
            if re.fullmatch(r"GAME-AUTOSAVE\d+", str(slot_directory))
            and isinstance(state, dict)
        }
        self.session_state["save_slot_observations"] = self.save_slot_observations
        self.selected_observation_slot = None
        self.session_state.pop("sticky_mastery_observed", None)
        self.session_state.pop("weapon_masteries_observed", None)
        self.weapon_masteries_observed = {}
        self.mission_challenges_observed = {}
        self.all_mission_challenges_observed = {}
        self.sticky_mastery_observed = False
        self.item_state_ready = True
        self.session_state.setdefault("bootstrap", {"revision": BOOTSTRAP_REVISION, "actions": {}})
        reconciliation = self.session_state.setdefault(
            "perk_reconciliation", {"epoch": 0, "delivered": {}}
        )
        reconciliation["epoch"] = int(reconciliation.get("epoch", 0)) + 1
        reconciliation.setdefault("delivered", {})
        save_client_state(self.client_state)
        logger.info(
            f"[State] Loaded {self.items_processed} processed items for "
            f"{self.state_key}."
        )
        self.check_and_update_event_session()

    def persist_session_state(self):
        if not self.item_state_ready:
            return
        self.session_state["processed_items"] = self.items_processed
        self.session_state["cultist_autosave_path"] = self.cultist_autosave_path
        self.session_state.pop("deathlinked", None)
        self.session_state["save_slot_observations"] = self.save_slot_observations
        self.session_state.pop("sticky_mastery_observed", None)
        self.session_state.pop("weapon_masteries_observed", None)
        save_client_state(self.client_state)

    def reset_item_state(self):
        self.items_processed = 0
        self.session_state["processed_items"] = 0
        self.session_state["item_mapping_revision"] = ITEM_MAPPING_REVISION
        self.session_state.pop("mapping_repair_indices", None)
        self.session_state.pop("item_command_groups", None)
        self.session_state["perk_reconciliation"] = {
            "epoch": 1,
            "delivered": {},
        }
        save_client_state(self.client_state)

    def received_rune_count(self):
        return sum(item.item in REVISION_ONE_RUNE_IDS for item in self.items_received)

    def received_item_ids(self, processed_only=False):
        items = self.items_received[: self.items_processed] if processed_only else self.items_received
        return {item.item for item in items}

    def manual_reconcile_inventory(self):
        """Queue a policy-compiled manual replay without mutating AP state."""
        if (
            not self.item_state_ready
            or not getattr(self, "server", None)
            or not self.server.socket
            or self.server.socket.closed
        ):
            return None, "connected AP session required"
        if getattr(self, "team", None) is None or getattr(self, "slot", None) is None:
            return None, "current AP team/slot identity is incomplete"
        seed = getattr(self, "room_seed_name", None) or getattr(self, "seed_name", None)
        if not seed or not re.fullmatch(r"[A-Za-z0-9_.-]+", str(seed)):
            return None, "seed identity is missing or unsafe for deterministic spool IDs"

        evidence = read_gameplay_save_evidence()
        if evidence is None or evidence.state != "gameplay":
            return None, "confirmed gameplay epoch required; menus are not eligible"
        if self.runtime_observers_frozen:
            return None, "gameplay observers are not level-ready"
        if self.active_save_slot != evidence.slot_directory:
            return None, "gameplay evidence does not match the active save slot"
        if self.active_native_evidence_epoch != evidence.epoch:
            return None, "gameplay evidence does not match the active epoch"
        if canonical_map_name(self.current_map_name or "") != evidence.map_name:
            return None, "gameplay evidence does not match the active map"
        supported = {
            canonical_map_name(name)
            for name in load_foundation_contracts()["active_maps"].values()
        }
        if evidence.map_name not in supported:
            return None, "active map has no ap_rpc_v3 reconciliation entities"
        if self.items_processed > len(self.items_received):
            return None, "authoritative received-item history is incomplete"

        identity = f"{seed}-{self.team}-{self.slot}"
        received = [
            network_item.item
            for network_item in self.items_received[: self.items_processed]
        ]
        try:
            plan = compile_reconciliation_plan(
                received,
                ITEM_ID_TO_COMMAND,
                ITEM_REPLAY_POLICIES,
                identity,
                evidence.epoch,
            )
        except ValueError as error:
            return None, str(error)

        for selection in plan.selections:
            if selection.commands:
                for command in selection.commands:
                    logger.info(
                        "[AP Reconcile] item=%s name=%s receipts=%s policy=%s command=%s",
                        selection.item_id, selection.name, selection.received_count,
                        selection.policy, command,
                    )
            else:
                logger.info(
                    "[AP Reconcile] item=%s name=%s receipts=%s policy=%s command=SKIP",
                    selection.item_id, selection.name, selection.received_count,
                    selection.policy,
                )
        for command in plan.commands:
            logger.info(
                "[AP Reconcile] spool=%s item=%s stage=%s policy=%s command=%s",
                command.spool_id, command.item_id, command.stage,
                command.policy, command.command,
            )
            if not send_command(
                command.command,
                coalesce_key=command.spool_id,
                already_queued_ok=True,
            ):
                return None, f"failed to spool {command.spool_id}; rerun is safe"
        logger.info(
            "[AP Reconcile] replayed=%s special_stages=%s "
            "skipped_never_replay=%s skipped_unproven=%s",
            plan.replayed, plan.special_stages, plan.skipped_never_replay,
            plan.skipped_unproven,
        )
        return plan, None

    def reconciliation_epoch(self):
        state = self.session_state.setdefault(
            "perk_reconciliation", {"epoch": 1, "delivered": {}}
        )
        return int(state.setdefault("epoch", 1))

    def advance_reconciliation_epoch(self, trigger):
        state = self.session_state.setdefault(
            "perk_reconciliation", {"epoch": 0, "delivered": {}}
        )
        state["epoch"] = int(state.get("epoch", 0)) + 1
        state["trigger"] = trigger
        state["timestamp"] = time.time()
        self.persist_session_state()
        return state["epoch"]

    def reconcile_owned_perks(self, trigger):
        """Reapply desired Rune ownership once per connect/level epoch."""
        if not self.item_state_ready or not self.current_map_name:
            return False
        supported = {
            canonical_map_name(name)
            for name in load_foundation_contracts()["active_maps"].values()
        }
        if canonical_map_name(self.current_map_name) not in supported:
            return False
        received_ids = self.received_item_ids(processed_only=True)
        state = self.session_state.setdefault(
            "perk_reconciliation", {"epoch": 1, "delivered": {}}
        )
        delivered = state.setdefault("delivered", {})
        epoch = self.reconciliation_epoch()
        candidates = [
            *(('rune', item_id) for item_id in sorted(received_ids & REVISION_ONE_RUNE_IDS)),
        ]
        changed = False
        for kind, item_id in candidates:
            key = f"{kind}:{item_id}"
            if int(delivered.get(key, -1)) >= epoch:
                continue
            commands, description = self.item_activation_commands(item_id, 0)
            if commands is None:
                logger.error("[Reconcile] Cannot compile %s %s: %s", kind, item_id, description)
                continue
            queued = True
            for command_index, command in enumerate(commands):
                # Native queue order is lexical; keep reconciliations after
                # recv-* prerequisite jobs created in the same pass.
                command_id = f"zz-reconcile-{kind}-{item_id}-e{epoch}-c{command_index}"
                if not send_command(command, coalesce_key=command_id, already_queued_ok=True):
                    queued = False
                    break
            if not queued:
                continue
            delivered[key] = epoch
            changed = True
            logger.info(
                "[Reconcile] %s %s queued for epoch %s (%s): %s",
                kind, item_id, epoch, trigger, description,
            )
        state["last_trigger"] = trigger
        state["timestamp"] = time.time()
        self.persist_session_state()
        return changed

    def advance_automap_cleanup_epoch(self):
        """Open one idempotent cleanup pass after a level-ready marker."""
        self.automap_cleanup_epoch += 1
        return self.automap_cleanup_epoch

    def reconcile_checked_automap_cleanup(self, trigger):
        """Remove only isolated AP visuals for server-checked map locations."""
        map_name = canonical_map_name(self.current_map_name or "")
        targets = AUTOMAP_COMPLETION_BY_MAP.get(map_name, {})
        if not targets:
            return False
        checked = set(getattr(self, "checked_locations", set()))
        checked.update(getattr(self, "locations_checked", set()))
        changed = False
        for location_id, entity_name in sorted(targets.items()):
            if location_id not in checked:
                continue
            delivery_key = (map_name, location_id)
            if self.automap_cleanup_delivered.get(delivery_key) == self.automap_cleanup_epoch:
                continue
            command_id = (
                f"automap-cleanup-{self.automap_cleanup_session}-"
                f"{location_id}-e{self.automap_cleanup_epoch}"
            )
            command = f"ai_ScriptCmdEnt {entity_name} activate"
            if not send_command(
                command,
                coalesce_key=command_id,
                already_queued_ok=True,
            ):
                continue
            self.automap_cleanup_delivered[delivery_key] = self.automap_cleanup_epoch
            changed = True
            logger.info(
                "[Automap] Checked-state cleanup queued location=%s map=%s "
                "epoch=%s trigger=%s target=%s",
                location_id,
                map_name,
                self.automap_cleanup_epoch,
                trigger,
                entity_name,
            )
        return changed

    def bootstrap_actions(self):
        bootstrap = self.session_state.setdefault(
            "bootstrap", {"revision": BOOTSTRAP_REVISION, "actions": {}}
        )
        actions = bootstrap.setdefault("actions", {})
        # dev1 stored entries by bare action name. Preserve them as revision 1
        # evidence rather than treating consumption as confirmation or replaying
        # them under revision 2.
        for action_name in (*BOOTSTRAP_ACTIONS, "suit_page"):
            legacy = actions.pop(action_name, None)
            if legacy is not None:
                legacy.setdefault("revision", 1)
                legacy.setdefault("action", action_name)
                if legacy.get("status") == "applied":
                    legacy["status"] = "delivered_effect_unknown"
                    legacy["legacy_status"] = "applied"
                actions.setdefault(f"v1:{action_name}", legacy)
        bootstrap["revision"] = BOOTSTRAP_REVISION
        return actions

    def bootstrap_action_state(self, action_name, revision=None):
        revision = BOOTSTRAP_REVISION if revision is None else revision
        state_key = f"v{revision}:{action_name}"
        state = self.bootstrap_actions().setdefault(state_key, {
            "revision": revision,
            "action": action_name, "trigger": None, "status": "pending",
            "last_map": None, "timestamp": None,
            "reapply_on_map_load": False,
        })
        return state

    def bootstrap_eligible(self, action_name):
        action = BOOTSTRAP_ACTIONS[action_name]
        if action["required_ap_ownership"] == "at_least_one_rune":
            return self.received_rune_count() > 0
        if action["required_ap_ownership"] == "at_least_one_suit_page_unlocker":
            return received_any_suit_upgrade(self.received_item_ids())
        if action["required_ap_ownership"] == "frag_grenade":
            return 7770011 in self.received_item_ids()
        if action["required_ap_ownership"] == "ice_bomb":
            return 7770013 in self.received_item_ids()
        return False

    def bootstrap_ineligibility_reason(self, action_name):
        if self.bootstrap_eligible(action_name):
            return "eligible"
        return {
            "rune_page": "needs AP Rune",
            "suit_page": "needs AP Suit Upgrade",
            "frag_acquired": "needs AP Frag Grenade",
            "ice_acquired": "needs AP Ice Bomb",
        }.get(action_name, "ownership predicate unmet")

    def bootstrap_command_id(self, action_name):
        action = BOOTSTRAP_ACTIONS[action_name]
        return f"bootstrap-v{action['revision']}-{action_name}"

    def quarantine_v1_bootstrap_spools(self):
        """Archive stale dev1 jobs so an absent v1 entity is never invoked."""
        for action_name in (*BOOTSTRAP_ACTIONS, "suit_page"):
            command_id = f"bootstrap-v1-{action_name}"
            for suffix in (".cmd", ".processing"):
                source = Path(QUEUE_DIR, f"{command_id}{suffix}")
                if not source.exists():
                    continue
                target = source.with_suffix(".quarantined")
                try:
                    os.replace(source, target)
                    state = self.bootstrap_action_state(action_name, revision=1)
                    state.update(status="quarantined_runtime_invalid", timestamp=time.time())
                    self.persist_session_state()
                    logger.warning("[Bootstrap] Quarantined v1 spool: %s", source.name)
                except OSError as error:
                    logger.error("[Bootstrap] Could not quarantine v1 spool %s: %s", source, error)

    def enqueue_bootstrap(self, action_name, trigger):
        """Persist the separate action state only after the durable spool exists."""
        action = BOOTSTRAP_ACTIONS[action_name]
        state = self.bootstrap_action_state(action_name)
        non_replayable = {
            "delivered_effect_unknown",
            "delivered_effect_unknown_legacy",
            "confirmed",
            "skipped",
        }
        if state["status"] in non_replayable or not self.bootstrap_eligible(action_name):
            return False
        if canonical_map_name(self.current_map_name) not in {
            canonical_map_name(name) for name in action["maps_supported"]
        }:
            state.update(status="pending", trigger=trigger, timestamp=time.time())
            self.persist_session_state()
            return False
        command_id = self.bootstrap_command_id(action_name)
        if not send_command(bootstrap_activation(action_name), coalesce_key=command_id,
                            already_queued_ok=True):
            state.update(status="retryable_failure", trigger=trigger, timestamp=time.time())
            self.persist_session_state()
            return False
        state.update(status="queued", trigger=trigger, last_map=self.current_map_name,
                     timestamp=time.time(), revision=action["revision"])
        self.persist_session_state()
        logger.info(
            "[Bootstrap] v%s entity=%s primitive_class=%s inherit=%s map=%s spool=%s trigger=%s",
            action["revision"], action["entity_name"],
            BOOTSTRAP_STAT_PRIMITIVE["class"],
            BOOTSTRAP_STAT_PRIMITIVE["inherit"] or "<none>",
            self.current_map_name, command_id, trigger,
        )
        return True

    def reconcile_bootstrap_spool(self):
        self.quarantine_v1_bootstrap_spools()
        for action_name in BOOTSTRAP_ACTIONS:
            state = self.bootstrap_action_state(action_name)
            if state["status"] == "queued" and not command_spool_exists(self.bootstrap_command_id(action_name)):
                state["status"] = "delivered_effect_unknown"
                state["timestamp"] = time.time()
                logger.info("[Bootstrap] v2 spool consumed; effect remains unknown: %s", action_name)
                self.persist_session_state()

    def onboard_bootstrap(self, trigger):
        # V1/V2 are retained as evidence, not foundations. All four actions are
        # experimental and must only run through /doom_test_bootstrap in a lab.
        if not any(action.get("automatic_enabled") for action in BOOTSTRAP_ACTIONS.values()):
            return
        if not self.item_state_ready or not rpc_execution_enabled():
            return
        self.reconcile_bootstrap_spool()
        for action_name, action in BOOTSTRAP_ACTIONS.items():
            if trigger in action["trigger_policy"]:
                if (
                    trigger == "on_supported_map_load"
                    and self.bootstrap_action_state(action_name)["status"] != "pending"
                ):
                    continue
                self.enqueue_bootstrap(action_name, trigger)

    def onboarding_status_lines(self):
        lines = [
            f"Bootstrap revision: {BOOTSTRAP_REVISION}",
            f"Current map: {self.current_map_name or 'unknown'}",
        ]
        for action_name in BOOTSTRAP_ACTIONS:
            state = self.bootstrap_action_state(action_name)
            status = state.get("status", "pending")
            eligible = self.bootstrap_eligible(action_name)
            reason = "eligible" if eligible else self.bootstrap_ineligibility_reason(action_name)
            lines.append(
                f"v2 {action_name}: eligible={'yes' if eligible else 'no'}, "
                f"state={status}, trigger={state.get('trigger') or '-'}, "
                f"map={state.get('last_map') or '-'}, reason={reason}"
            )
            legacy = self.bootstrap_actions().get(f"v1:{action_name}")
            if legacy:
                lines.append(
                    f"v1 {action_name}: state={legacy.get('status', 'pending')} "
                    "(delivered_effect_unknown)"
                )
        lines.append(f"Technical log: {BRIDGE_LOG_DIR}")
        return lines

    def repair_item_mappings(self):
        """Deliver items skipped by older bridge mappings without replaying others."""
        revision = int(self.session_state.get("item_mapping_revision", 0))
        if revision >= ITEM_MAPPING_REVISION:
            return True
        if len(self.items_received) < self.items_processed:
            return False

        repaired = {
            int(index)
            for index in self.session_state.get("mapping_repair_indices", [])
        }
        repair_ids = set()
        if revision < 1:
            repair_ids.update(REVISION_ONE_RUNE_IDS)
        if revision < 2:
            repair_ids.update(REVISION_TWO_SUIT_IDS)
        if revision < 4:
            repair_ids.update(REVISION_FOUR_FLAME_BELCH_IDS)
        if revision < 5:
            repair_ids.update(REVISION_FIVE_EQUIPMENT_LAUNCHER_IDS)
        repair_indices = [
            index
            for index, network_item in enumerate(
                self.items_received[: self.items_processed]
            )
            if network_item.item in repair_ids
        ]
        for item_index in repair_indices:
            if item_index in repaired:
                continue
            network_item = self.items_received[item_index]
            spooled, description = self.spool_item_commands(
                network_item.item, item_index, receipt=False
            )
            if not spooled:
                return False
            repaired.add(item_index)
            self.session_state["mapping_repair_indices"] = sorted(repaired)
            save_client_state(self.client_state)
            logger.info(
                f"[State] Recovered item affected by an older mapping "
                f"{network_item.item} at receive index {item_index}: "
                f"{description}"
            )
            return False

        self.session_state["item_mapping_revision"] = ITEM_MAPPING_REVISION
        self.session_state.pop("mapping_repair_indices", None)
        save_client_state(self.client_state)
        return True

    def progressive_stage(self, item_id, item_index):
        return sum(
            1
            for received in self.items_received[:item_index]
            if received.item == item_id
        )

    def receipt_notification_slot(self, item_id, item_index):
        """Alternate the HUD identity per item, not merely per global receipt."""
        if len(self.items_received) > item_index:
            ordinal = self.progressive_stage(item_id, item_index)
        else:
            # Keeps direct/dev spools deterministic when no NetworkItem history exists.
            ordinal = item_index
        return ("a", "b")[ordinal % 2]

    def item_activation_commands(
        self,
        item_id,
        item_index,
        receipt=False,
        classification=None,
    ):
        if receipt and classification is None:
            classification = ITEM_CLASSIFICATIONS.get(item_id)
        definition = ITEM_ID_TO_COMMAND.get(item_id)
        stage = (
            self.progressive_stage(item_id, item_index)
            if isinstance(definition, dict)
            and definition.get("type") == "progressive_perk"
            else None
        )
        try:
            plan = compile_item_delivery_plan(
                item_id,
                ITEM_ID_TO_COMMAND,
                stage=stage,
                receipt=receipt,
                classification=classification,
                notification_slot=(
                    self.receipt_notification_slot(item_id, item_index)
                    if receipt else None
                ),
            )
        except ValueError as error:
            return None, str(error)
        return [command.command for command in plan.commands], plan.description

    def item_command_id(self, item_id, item_index, command_index, command):
        if command.startswith("ai_ScriptCmdEnt ap_notify_item_"):
            suffix = "notify"
        else:
            suffix = f"effect-{command_index:02d}"
        return f"recv-{item_index:06d}-item-{item_id}-{suffix}"

    def delivery_item_name(self, item_id):
        identity = ITEM_CLASSIFICATION_IDENTITY.get(item_id)
        if identity is not None:
            return identity["name"]
        return f"Unknown item (ID: {item_id})"

    def spool_item_commands(
        self,
        item_id,
        item_index,
        *,
        receipt: bool,
        classification=None,
    ):
        commands, description = self.item_activation_commands(
            item_id,
            item_index,
            receipt=receipt,
            classification=classification,
        )
        if commands is None:
            return False, description

        groups = self.session_state.setdefault("item_command_groups", {})
        group_key = str(item_index)
        group = groups.setdefault(
            group_key,
            {
                "item_id": item_id,
                "next_command": 0,
                "total_commands": len(commands),
            },
        )
        if group.get("item_id") != item_id:
            return False, "stored command group belongs to a different item"

        next_command = int(group.get("next_command", 0))
        if next_command < 0 or next_command > len(commands):
            return False, "stored command group index is invalid"

        for command_index in range(next_command, len(commands)):
            command_id = self.item_command_id(
                item_id, item_index, command_index, commands[command_index]
            )
            if not send_command(
                commands[command_index],
                coalesce_key=command_id,
                already_queued_ok=True,
                delivery_fields={
                    "receipt_index": item_index,
                    "item_id": item_id,
                    "item_name": self.delivery_item_name(item_id),
                    "command_ordinal": command_index,
                    "source": "cmd",
                    "active_map": getattr(self, "current_map_name", None),
                    "slot": getattr(self, "active_save_slot", None),
                    "bridge_revision": BRIDGE_REVISION,
                    "protocol_version": BRIDGE_PROTOCOL,
                },
            ):
                return False, description
            group["next_command"] = command_index + 1
            group["total_commands"] = len(commands)
            save_client_state(self.client_state)

        groups.pop(group_key, None)
        if not groups:
            self.session_state.pop("item_command_groups", None)
        save_client_state(self.client_state)
        return True, description

    def on_deathlink(self, data: dict):
        super().on_deathlink(data)
        if not self.death_link_enabled:
            return
        now = time.monotonic()
        event_id = hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        result = self.deathlink_receiver.receive(event_id, now)
        if result.detail == "duplicate":
            logger.info("[DeathLink] Ignored duplicate received event %s.", event_id[:12])
            return
        if result.state is ReceiveState.FAILED:
            logger.warning("[DeathLink] Rejected %s: bounded receive queue is full.", event_id[:12])
            return
        logger.info("[DeathLink] Received %s; queued for safe gameplay.", event_id[:12])

    def queue_received_deathlink(self):
        if not self.death_link_enabled:
            return
        result = self.deathlink_receiver.advance(
            now=time.monotonic(),
            safe_gameplay=(
                not self.runtime_observers_frozen
                and self.has_authoritative_save_proof()
            ),
            dispatch=lambda: send_command(
                "ai_ScriptCmdEnt ap_deathlink activate",
                coalesce_key=DEATHLINK_KILL_COALESCE_KEY,
            ),
        )
        if result.state is ReceiveState.DISPATCHED_ONCE and result.detail == "dispatched":
            logger.info("[DeathLink] %s dispatched once; awaiting confirmation.", (result.event_id or "unknown")[:12])
        elif result.state in {ReceiveState.EXPIRED, ReceiveState.FAILED}:
            state_name = result.state.value.lower() if result.state else "unknown"
            logger.warning("[DeathLink] %s %s (%s); receive state cleared.", (result.event_id or "unknown")[:12], state_name, result.detail)

    async def check_game_duration_death(self):
        selected = self.update_save_slot_lifecycle()
        if not selected:
            # The durable-save path is available but deliberately frozen until
            # native gameplay evidence promotes a slot. Do not fall back to a
            # newest-mtime game.details reader while in menus.
            return True
        path = selected.path
        cache_key = selected.cache_key
        if cache_key == self.last_duration_cache_key:
            return True

        try:
            snapshot = await asyncio.to_thread(probe_game_duration, path)
        except Exception as error:
            warning = str(error)
            if warning != self.death_probe_warning:
                logger.warning(
                    "[DeathLink] game_duration probe failed; using "
                    f"game.details fallback: {error}"
                )
                self.death_probe_warning = warning
            return False

        self.death_probe_warning = None
        self.last_duration_cache_key = cache_key
        self.observe_weapon_masteries(snapshot["mastery_records"], selected)
        self.observe_mission_challenges(
            snapshot["mission_challenge_records"], selected
        )
        died = snapshot["checkpoint_death"]
        previous = self.checkpoint_death_by_save_slot.get(selected.slot_directory)
        if previous is None:
            self.checkpoint_death_by_save_slot[selected.slot_directory] = died
            self.previous_checkpoint_death = died
            logger.info(
                f"[Save] Monitoring {path} numCheckpointDeaths for DeathLink."
            )
            return True

        transitioned_to_dead = died and not previous
        self.checkpoint_death_by_save_slot[selected.slot_directory] = died
        self.previous_checkpoint_death = died
        if transitioned_to_dead:
            logger.info("[DeathLink] numCheckpointDeaths changed 0 -> 1.")
            await self.report_local_death()
        return True

    def observe_save_edges(self, observer_key, records, entries, slot_directory):
        if not self.item_state_ready:
            return set()
        identity = (
            getattr(self, "room_seed_name", None)
            or getattr(self, "seed_name", None)
            or self.state_key
            or "unknown"
        )
        binding_key = SaveObserverBaselineStore.binding_key(
            session_identity=str(identity),
            team=int(getattr(self, "team", 0) or 0),
            slot=int(getattr(self, "slot", 0) or 0),
            doom_save_slot=slot_directory,
            registry_revision=OBSERVER_REGISTRY_REVISION,
        )
        acknowledged = {
            key
            for key, entry in entries.items()
            if entry["location_id"] in getattr(self, "checked_locations", set())
        }
        pending, created, new_edges = SaveObserverBaselineStore(
            self.session_state
        ).observe(
            binding_key=binding_key,
            observer_key=observer_key,
            records=records,
            acknowledged_records=acknowledged,
        )
        sessions = self.client_state.get("sessions", {})
        if sessions.get(self.state_key) is self.session_state:
            self.persist_session_state()
        if created:
            logger.info(
                "[OBSERVER] BASELINE_CREATED session=%s save_slot=%s records=%s",
                self.state_key,
                slot_directory,
                sum(records.values()),
            )
        for key in sorted(new_edges):
            logger.info("[OBSERVER] EDGE_COMPLETE key=%s", key)
        return pending

    def observe_weapon_masteries(self, records, path):
        """Observe only each mastery record's own native completion predicate."""
        slot_directory = self.observation_slot_for_source(path)
        self.select_save_observation_slot(slot_directory)
        if not self.has_authoritative_save_proof():
            return
        completion_states = {
            unlockable: (
                unlockable in records
                and unlockable_record_complete(records[unlockable], entry["signal"])
            )
            for unlockable, entry in WEAPON_MASTERY_BY_UNLOCKABLE.items()
        }
        pending_edges = self.observe_save_edges(
            "weapon_masteries",
            completion_states,
            WEAPON_MASTERY_BY_UNLOCKABLE,
            slot_directory,
        )
        for unlockable in WEAPON_MASTERY_BY_UNLOCKABLE:
            self.weapon_masteries_observed.setdefault(unlockable, False)
        for unlockable, record in records.items():
            entry = WEAPON_MASTERY_BY_UNLOCKABLE.get(unlockable)
            if entry is None:
                continue
            signal = entry["signal"]
            observed_record = (
                int(record["numUnlockableRules"]),
                record["rule_0_statname"],
                int(record["rule_0_statCount"]),
                int(record["rule_0_statDuration"]),
                bool(record["rule_0_satisfied"]),
                bool(record["unlockableIsUnlocked"]),
            )
            record_key = (slot_directory, unlockable)
            if observed_record != self.last_mastery_records.get(record_key):
                logger.info(
                    "[Mastery] RECORD unlockable=%s rules=%s stat=%s count=%s "
                    "duration=%s satisfied=%s unlocked=%s save_slot=%s source=%s",
                    unlockable,
                    *observed_record,
                    slot_directory,
                    path,
                )
                self.last_mastery_records[record_key] = observed_record

            if unlockable not in pending_edges:
                continue
            self.weapon_masteries_observed[unlockable] = True
            if unlockable == STICKY_UNLOCKABLE.decode("ascii"):
                self.sticky_mastery_observed = True
                self.last_sticky_record = observed_record[1:]

    def observe_physical_event_challenges(self):
        """Return server-derived predicates without mutating completion state."""
        checked_locations = getattr(self, "checked_locations", set())
        ready = set()

        for entry in MISSION_CHALLENGE_ENTRIES:
            signal = entry["signal"]
            unlockable = signal["unlockable"]
            if signal.get("kind") == "physical_event_equivalent":
                phys_ids = signal.get("physical_location_ids", [])
                required_count = signal.get("required_count", 1)
                source_ids = set(phys_ids)
                matched_ids = source_ids.intersection(checked_locations)
                if len(matched_ids) >= required_count:
                    ready.add(unlockable)
        return ready

    def observe_mission_challenges(self, records, path):
        """Observe durable native records and derive the all-challenges check."""
        slot_directory = self.observation_slot_for_source(path)
        self.select_save_observation_slot(slot_directory)
        self.observe_physical_event_challenges()

        if not self.has_authoritative_save_proof():
            return

        save_entries = {
            unlockable: entry
            for unlockable, entry in MISSION_CHALLENGE_BY_UNLOCKABLE.items()
            if entry["signal"]["kind"] in {"unlockable_record", "stat_threshold"}
            and (
                not self.mission_select_observation_map
                or MISSION_CHALLENGE_RUNTIME_MAP_BY_UNLOCKABLE.get(unlockable)
                == self.mission_select_observation_map
            )
        }
        completion_states = {
            unlockable: (
                unlockable in records
                and unlockable_record_complete(records[unlockable], entry["signal"])
            )
            for unlockable, entry in save_entries.items()
        }
        observer_key = "mission_challenges"
        if self.mission_select_observation_map:
            observer_key = (
                f"mission_challenges:mission_select:"
                f"{self.mission_select_observation_epoch}:"
                f"{self.mission_select_observation_map}"
            )
        pending_edges = self.observe_save_edges(
            observer_key,
            completion_states,
            save_entries,
            slot_directory,
        )
        for unlockable, record in records.items():
            entry = MISSION_CHALLENGE_BY_UNLOCKABLE.get(unlockable)
            if entry is None:
                continue
            signal = entry["signal"]
            if signal["kind"] not in {"unlockable_record", "stat_threshold"}:
                continue
            observed_record = (
                int(record["numUnlockableRules"]),
                record["rule_0_statname"],
                int(record["rule_0_statCount"]),
                int(record["rule_0_statDuration"]),
                bool(record["rule_0_satisfied"]),
                bool(record["unlockableIsUnlocked"]),
            )
            record_key = (slot_directory, unlockable)
            if observed_record != self.last_mission_challenge_records.get(record_key):
                logger.info(
                    "[Challenge] RECORD unlockable=%s rules=%s stat=%s count=%s "
                    "duration=%s satisfied=%s unlocked=%s save_slot=%s source=%s",
                    unlockable,
                    *observed_record,
                    slot_directory,
                    path,
                )
                self.last_mission_challenge_records[record_key] = observed_record

            if observed_record[0] != signal["numUnlockableRules"]:
                logger.warning(
                    "[Challenge] REGISTRY_MISMATCH unlockable=%s field=numUnlockableRules expected=%s observed=%s",
                    unlockable, signal["numUnlockableRules"], observed_record[0],
                )
            if observed_record[1] != signal["rule_0_statname"]:
                logger.warning(
                    "[Challenge] REGISTRY_MISMATCH unlockable=%s field=rule_0_statname expected=%s observed=%s",
                    unlockable, signal["rule_0_statname"], observed_record[1],
                )
            if observed_record[3] != signal["rule_0_statDuration"]:
                logger.warning(
                    "[Challenge] REGISTRY_MISMATCH unlockable=%s field=rule_0_statDuration expected=%s observed=%s",
                    unlockable, signal["rule_0_statDuration"], observed_record[3],
                )
            expected_count = signal.get("rule_0_statCount")
            if expected_count is not None and observed_record[2] < expected_count:
                logger.warning(
                    "[Challenge] REGISTRY_MISMATCH unlockable=%s "
                    "field=rule_0_statCount expected_at_least=%s observed=%s",
                    unlockable, expected_count, observed_record[2],
                )

            if unlockable not in pending_edges:
                continue
            self.mission_challenges_observed[unlockable] = True

    def observe_sticky_mastery(self, snapshot, path):
        """Sticky compatibility wrapper used by the proven 24→25 regression."""
        record = {
            "numUnlockableRules": STICKY_MASTERY_ENTRY["signal"]["numUnlockableRules"],
            "rule_0_statDuration": STICKY_MASTERY_ENTRY["signal"]["rule_0_statDuration"],
            **snapshot,
        }
        self.observe_weapon_masteries(
            {STICKY_UNLOCKABLE.decode("ascii"): record}, path
        )

    async def check_weapon_mastery_location(self, entry):
        if not self.item_state_ready or not self.has_authoritative_save_proof():
            return
        unlockable = entry["signal"]["unlockable"]
        if not self.weapon_masteries_observed.get(unlockable):
            return
        location_id = entry["location_id"]
        if location_id in self.checked_locations or location_id in self.locations_checked:
            return
        if location_id not in self.server_locations:
            if location_id not in self.mastery_slot_warnings:
                logger.warning(
                    "[Mastery] LOCATION id=%s unlockable=%s slot=absent",
                    location_id,
                    unlockable,
                )
                self.mastery_slot_warnings.add(location_id)
            return
        if not self.server or not self.server.socket or self.server.socket.closed:
            return
        try:
            logger.info(
                "[Mastery] LOCATION_CHECK_SEND id=%s unlockable=%s "
                "source=vanilla_save_predicate",
                location_id,
                unlockable,
            )
            await self.send_msgs([
                {"cmd": "LocationChecks", "locations": [location_id]}
            ])
        except Exception as error:
            logger.error(
                "[Mastery] LOCATION_CHECK_RETRY id=%s unlockable=%s error=%s",
                location_id,
                unlockable,
                error,
            )
            return
        self.locations_checked.add(location_id)
        logger.info("[Mastery] LOCATION_CHECK_ACK id=%s", location_id)

    async def check_weapon_mastery_locations(self):
        for entry in WEAPON_MASTERY_ENTRIES:
            await self.check_weapon_mastery_location(entry)

    async def check_mission_challenge_location(self, entry):
        if not self.item_state_ready:
            return
        is_physical = entry["signal"].get("kind") == "physical_event_equivalent"
        if not is_physical and not self.has_authoritative_save_proof():
            return
        unlockable = entry["signal"]["unlockable"]
        if is_physical:
            physical_ids = set(entry["signal"].get("physical_location_ids", ()))
            required_count = int(entry["signal"].get("required_count", 1))
            if len(physical_ids.intersection(self.checked_locations)) < required_count:
                return
        elif not self.mission_challenges_observed.get(unlockable):
            return
        location_id = entry["location_id"]
        if location_id in self.checked_locations:
            return
        if location_id not in self.server_locations:
            if location_id not in self.mission_challenge_slot_warnings:
                logger.warning(
                    "[Challenge] LOCATION id=%s unlockable=%s slot=absent",
                    location_id,
                    unlockable,
                )
                self.mission_challenge_slot_warnings.add(location_id)
            return
        if not self.server or not self.server.socket or self.server.socket.closed:
            return
        source_name = "physical_event_equivalent" if is_physical else "vanilla_save_predicate"
        try:
            logger.info(
                "[Challenge] LOCATION_CHECK_SEND id=%s unlockable=%s "
                "source=%s save_slot=%s",
                location_id,
                unlockable,
                source_name,
                self.active_save_slot or "<synthetic>",
            )
            await self.send_msgs([
                {"cmd": "LocationChecks", "locations": [location_id]}
            ])
        except Exception as error:
            logger.error(
                "[Challenge] LOCATION_CHECK_RETRY id=%s unlockable=%s error=%s",
                location_id,
                unlockable,
                error,
            )
            return
        logger.info("[Challenge] LOCATION_CHECK_QUEUED id=%s awaiting=server_ack", location_id)

    async def check_mission_challenge_locations(self):
        self.ingest_visible_runtime_lifecycle()
        self.observe_physical_event_challenges()
        for entry in MISSION_CHALLENGE_ENTRIES:
            await self.check_mission_challenge_location(entry)
        await self.check_all_mission_challenges_location()

    async def check_all_mission_challenges_location(self):
        """Publish aggregates only from server-authoritative checked children."""
        if not self.item_state_ready:
            return
        checked = set(self.checked_locations)
        for aggregate in ALL_MISSION_CHALLENGES_ENTRIES:
            signal = aggregate["signal"]
            children = set(signal["children"])
            if not aggregate_ready(signal, checked):
                continue
            location_id = aggregate["location_id"]
            if location_id in checked:
                continue
            if location_id not in self.server_locations:
                if location_id not in self.mission_challenge_slot_warnings:
                    logger.warning(
                        "[Challenge] ALL_LOCATION id=%s slot=absent", location_id
                    )
                    self.mission_challenge_slot_warnings.add(location_id)
                continue
            if not self.server or not self.server.socket or self.server.socket.closed:
                continue
            logger.info(
                "[Challenge] ALL_LOCATION_CHECK_SEND id=%s authority=server_checked_locations "
                "children=%s",
                location_id,
                sorted(children),
            )
            try:
                await self.send_msgs([
                    {"cmd": "LocationChecks", "locations": [location_id]}
                ])
            except Exception as error:
                logger.error(
                    "[Challenge] ALL_LOCATION_CHECK_RETRY id=%s error=%s",
                    location_id,
                    error,
                )
                continue
            logger.info(
                "[Challenge] ALL_LOCATION_CHECK_QUEUED id=%s awaiting=server_ack",
                location_id,
            )

    async def check_sticky_mastery_location(self):
        """Sticky compatibility wrapper preserving its exact send contract."""
        await self.check_weapon_mastery_location(STICKY_MASTERY_ENTRY)

    async def check_game_details_death(self):
        details = self.active_game_details()
        if not details:
            return

        died = details.get("diedLastGame") == "1"
        mtime = details.get("_mtime_ns")
        details_path = details.get("_path")
        if self.previous_died_last_game is None:
            self.previous_died_last_game = died
            self.last_details_mtime = mtime
            self.last_details_path = details_path
            logger.info(
                f"[Save] Monitoring {details.get('_path')} for DeathLink."
            )
            return

        if details_path != self.last_details_path:
            self.previous_died_last_game = died
            self.last_details_mtime = mtime
            self.last_details_path = details_path
            logger.info(
                "[Save] Active autosave changed; DeathLink baseline reset to "
                f"{details_path}."
            )
            return

        changed = mtime != self.last_details_mtime
        transitioned_to_dead = changed and died and not self.previous_died_last_game
        self.previous_died_last_game = died
        self.last_details_mtime = mtime

        if transitioned_to_dead:
            await self.report_local_death()

    async def report_local_death(self):
        if not self.death_link_enabled:
            return
        receive_result = self.deathlink_receiver.confirm_local_death()
        if receive_result.state is ReceiveState.CONFIRMED:
            discard_queued_coalesced_command(DEATHLINK_KILL_COALESCE_KEY)
            logger.info("[DeathLink] %s confirmed; suppressed linked echo.", (receive_result.event_id or "unknown")[:12])
            return
        player = self.auth or "The Doom Slayer"
        await self.send_death(random.choice(DEATHLINK_MESSAGES).format(player=player))

    def record_publisher_ack(self, publisher_key, effect_index, effect):
        state = self.session_state.setdefault("publisher_acknowledgements", {})
        publisher_state = state.setdefault(publisher_key, {})
        publisher_state[str(effect_index)] = {
            "strategy": effect["strategy"],
            "location_id": effect.get("location_id"),
        }
        if hasattr(self, "persist_session_state"):
            self.persist_session_state()

    async def send_publisher_effect(
        self, publisher, effect_index, effect, source_description
    ):
        strategy = effect["strategy"]
        effect_key = (publisher.key, effect_index)
        if not hasattr(self, "publisher_effects_in_flight"):
            self.publisher_effects_in_flight = set()
        checked_locations = getattr(self, "checked_locations", set())
        if strategy == "preserved_native_target":
            return True
        if strategy == "location_check":
            location_id = effect["location_id"]
            if location_id in checked_locations:
                self.publisher_effects_in_flight.discard(effect_key)
                DoomEternalContext.record_publisher_ack(
                    self, publisher.key, effect_index, effect
                )
                logger.info(
                    "[PUBLISHER] EFFECT_ACK key=%s effect=location_check location_id=%s",
                    publisher.key,
                    location_id,
                )
                return True
            if location_id in self.locations_checked or effect_key in self.publisher_effects_in_flight:
                logger.info(
                    "[PUBLISHER] FALLBACK_SUPPRESSED key=%s reason=already_dispatched",
                    publisher.key,
                )
                return False
            if location_id not in self.server_locations:
                logger.warning(
                    "[PUBLISHER] EFFECT_BLOCKED key=%s effect=location_check "
                    "location_id=%s reason=slot_absent",
                    publisher.key,
                    location_id,
                )
                return False
            message = {"cmd": "LocationChecks", "locations": [location_id]}
        elif strategy == "campaign_goal":
            if self.session_state.get("goal_sent", False):
                self.publisher_effects_in_flight.discard(effect_key)
                DoomEternalContext.record_publisher_ack(
                    self, publisher.key, effect_index, effect
                )
                logger.info(
                    "[PUBLISHER] EFFECT_ACK key=%s effect=campaign_goal",
                    publisher.key,
                )
                return True
            if effect_key in self.publisher_effects_in_flight:
                logger.info(
                    "[PUBLISHER] FALLBACK_SUPPRESSED key=%s reason=in_flight",
                    publisher.key,
                )
                return False
            message = {"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}
        else:
            raise ValueError(f"unsupported publisher effect strategy: {strategy}")

        if not self.server or not self.server.socket or self.server.socket.closed:
            return False
        self.publisher_effects_in_flight.add(effect_key)
        logger.info(
            "[PUBLISHER] EFFECT_SEND key=%s effect=%s location_id=%s source=%s",
            publisher.key,
            strategy,
            effect.get("location_id", ""),
            source_description,
        )
        try:
            await self.send_msgs([message])
        except Exception:
            self.publisher_effects_in_flight.discard(effect_key)
            raise
        if strategy == "location_check":
            self.locations_checked.add(effect["location_id"])
            if effect["location_id"] in getattr(self, "checked_locations", set()):
                self.publisher_effects_in_flight.discard(effect_key)
                DoomEternalContext.record_publisher_ack(
                    self, publisher.key, effect_index, effect
                )
                logger.info(
                    "[PUBLISHER] EFFECT_ACK key=%s effect=location_check location_id=%s",
                    publisher.key,
                    effect["location_id"],
                )
                return True
            return False
        self.session_state["goal_sent"] = True
        self.publisher_effects_in_flight.discard(effect_key)
        DoomEternalContext.record_publisher_ack(
            self, publisher.key, effect_index, effect
        )
        logger.info(
            "[PUBLISHER] EFFECT_ACK key=%s effect=campaign_goal",
            publisher.key,
        )
        return True

    async def execute_publisher(self, publisher, trigger_strategy, source_description):
        if publisher_acknowledged(
            publisher,
            getattr(self, "checked_locations", set()),
            self.session_state.get("goal_sent", False),
        ):
            logger.info(
                "[PUBLISHER] FALLBACK_SUPPRESSED key=%s reason=already_acknowledged",
                publisher.key,
            )
            return True
        logger.info(
            "[PUBLISHER] TRIGGER_OBSERVED key=%s strategy=%s",
            publisher.key,
            trigger_strategy,
        )
        results = []
        for index, effect in enumerate(publisher.effects):
            try:
                results.append(
                    await DoomEternalContext.send_publisher_effect(
                        self,
                        publisher, index, effect, source_description
                    )
                )
            except Exception as error:
                logger.error(
                    "[PUBLISHER] EFFECT_RETRY key=%s effect=%s error=%s",
                    publisher.key,
                    effect["strategy"],
                    error,
                )
                results.append(False)
        return all(results)

    async def send_mission_complete(
        self, location_id, source_description, report_goal=False
    ):
        """Compatibility wrapper routed through the declarative effect sender."""
        matching = next(
            (
                publisher
                for publisher in PUBLISHERS
                if any(
                    effect["strategy"] == "location_check"
                    and effect["location_id"] == location_id
                    for effect in publisher.effects
                )
            ),
            None,
        )
        if matching is None and report_goal:
            matching = next(
                publisher
                for publisher in PUBLISHERS
                if any(effect["strategy"] == "campaign_goal" for effect in publisher.effects)
            )
        if matching is None:
            return False
        results = []
        for index, effect in enumerate(matching.effects):
            if effect["strategy"] == "preserved_native_target":
                continue
            if effect["strategy"] == "campaign_goal" and not report_goal:
                continue
            if effect["strategy"] == "location_check" and location_id is None:
                continue
            results.append(
                await DoomEternalContext.send_publisher_effect(
                    self, matching, index, effect, source_description
                )
            )
        return bool(results) and all(results)

    async def send_campaign_goal(self, source_description):
        return await DoomEternalContext.send_mission_complete(
            self,
            None,
            source_description,
            report_goal=True,
        )

    async def check_campaign_goal_event(self):
        """Consume independent map files and native transition triggers."""
        observed = False
        quarantine_root = Path(INV_DUMP_DIR) / "quarantine"
        for trigger_key, publishers in PUBLISHER_ENGINE.publishers_by_trigger.items():
            if trigger_key[0] != "map_event_file":
                continue
            filename = trigger_key[1]
            trigger = next(
                item
                for publisher in publishers
                for item in publisher.triggers_for("map_event_file")
                if item["filename"] == filename
            )
            path = Path(INV_DUMP_DIR) / filename
            if not path.exists():
                continue
            observed = True
            valid, contents, digest = read_map_event(path, trigger["marker"])
            if not valid:
                quarantine_malformed_event(
                    path,
                    key=",".join(publisher.key for publisher in publishers),
                    contents=contents,
                    sha256=digest,
                    quarantine_root=quarantine_root,
                )
                logger.warning(
                    "[PUBLISHER] EVENT_MALFORMED key=%s filename=%s sha256=%s",
                    ",".join(publisher.key for publisher in publishers),
                    filename,
                    digest,
                )
                continue
            completed = [
                await DoomEternalContext.execute_publisher(
                    self,
                    publisher,
                    "map_event_file",
                    f"map event {filename}",
                )
                for publisher in publishers
            ]
            if all(completed):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as error:
                    logger.warning(
                        "[PUBLISHER] EVENT_CLEANUP_RETRY key=%s filename=%s error=%s",
                        ",".join(publisher.key for publisher in publishers),
                        filename,
                        error,
                    )

        for path in goal_event_files():
            observed = True
            event = parse_goal_transition_event(path, include_raw=True)
            if event is None:
                try:
                    raw = Path(path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    raw = ""
                digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                quarantine_malformed_event(
                    Path(path),
                    key="native_transition",
                    contents=raw,
                    sha256=digest,
                    quarantine_root=quarantine_root,
                )
                logger.warning(
                    "[PUBLISHER] EVENT_MALFORMED key=native_transition filename=%s sha256=%s",
                    os.path.basename(path),
                    digest,
                )
                continue
            matching = PUBLISHER_ENGINE.observe("native_transition", event)
            if not matching:
                logger.info(
                    "[PUBLISHER] TRANSITION_IGNORED from=%s to=%s reason=no_contract",
                    event["from_map"],
                    event["to_map"],
                )
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                continue
            completed = []
            for publisher in matching:
                completed.append(
                    await DoomEternalContext.execute_publisher(
                        self,
                        publisher,
                        "native_transition",
                        f"native transition {event['from_map']} -> {event['to_map']}",
                    )
                )
            if all(completed):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    logger.warning(
                        "[PUBLISHER] EVENT_CLEANUP_RETRY key=native_transition "
                        "filename=%s error=%s",
                        os.path.basename(path),
                        error,
                    )
        return observed

    def clear_final_sin_completion_candidate(self, reason):
        candidate = getattr(self, "final_sin_completion_candidate", None)
        if candidate is not None:
            logger.info(
                "[Goal] FINAL_SIN_COMPLETION_CANDIDATE_CLEARED slot=%s load_epoch=%s reason=%s",
                candidate["slot"],
                candidate["load_epoch"],
                reason,
            )
        self.final_sin_completion_candidate = None

    def arm_final_sin_completion_candidate(
        self, selected, details, runtime_map, load_epoch
    ):
        if (
            canonical_map_name(runtime_map)
            != canonical_map_name(CAMPAIGN_GOAL_CONTRACT["runtime_map"])
            or load_epoch is None
        ):
            return
        details_path = details.get("_path")
        details_token = details.get("_mtime_ns", selected.mtime_ns)
        if not details_path or details_token is None:
            return
        existing = getattr(self, "final_sin_completion_candidate", None)
        identity = (selected.slot_directory, load_epoch, str(details_path))
        if existing is not None and identity == (
            existing["slot"],
            existing["load_epoch"],
            existing["details_path"],
        ):
            return
        self.final_sin_completion_candidate = {
            "slot": selected.slot_directory,
            "load_epoch": load_epoch,
            "details_path": str(details_path),
            "details_token_at_arm": int(details_token),
            "completed_at_arm": str(details.get("completed", "0")),
        }
        logger.info(
            "[Goal] FINAL_SIN_COMPLETION_CANDIDATE_ARMED slot=%s load_epoch=%s "
            "details_token=%s completed=%s",
            selected.slot_directory,
            load_epoch,
            details_token,
            details.get("completed", "0"),
        )

    async def evaluate_final_sin_completion_candidate(self):
        candidate = getattr(self, "final_sin_completion_candidate", None)
        if candidate is None:
            return False

        if (
            getattr(self, "active_save_proof_authoritative", False)
            and getattr(self, "active_save_proof_slot", None)
            and self.active_save_proof_slot != candidate["slot"]
        ):
            self.clear_final_sin_completion_candidate("different_authoritative_slot")
            return False

        selected = primary_save_for_slot(candidate["slot"])
        details = read_game_details_for_selection(selected) if selected else None
        if (
            selected is not None
            and details
            and selected.slot_directory == candidate["slot"]
        ):
            details_path = details.get("_path")
            details_token = details.get("_mtime_ns", selected.mtime_ns)
            if (
                str(details_path) == candidate["details_path"]
                and details_token is not None
                and int(details_token) > candidate["details_token_at_arm"]
            ):
                if details.get("completed") != "1":
                    self.clear_final_sin_completion_candidate("fresh_incomplete_details")
                    return False
                publisher = next(
                    item for item in PUBLISHERS
                    if item.key == "final_sin_mission_complete"
                )
                published = await DoomEternalContext.execute_publisher(
                    self,
                    publisher,
                    "save_fallback",
                    "Final Sin Mission Select completed edge",
                )
                if published:
                    self.clear_final_sin_completion_candidate("publisher_acknowledged")
                return published

        lease = getattr(self, "runtime_observation_lease", None)
        if lease is not None and not lease.process_probe():
            self.clear_final_sin_completion_candidate("game_process_ended")
        return False

    async def check_campaign_goal_save_fallback(self):
        if await self.evaluate_final_sin_completion_candidate():
            return
        details = self.active_game_details()
        if not details:
            return
        mtime = details.get("_mtime_ns")
        if mtime == self.last_goal_details_mtime:
            return
        self.last_goal_details_mtime = mtime

        details_path = details.get("_path")
        map_name = canonical_map_name(details.get("mapName"))
        if not details_path or not map_name:
            return

        is_completed = details.get("completed") == "1"
        key = (details_path, map_name)
        prev_status = self.session_map_completion_states.get(key)
        self.session_map_completion_states[key] = "1" if is_completed else "0"

        # Edge fallback triggers only on a fresh in-session transition from incomplete to completed
        fresh_completion = (prev_status == "0" and is_completed)

        if map_name == CULTIST_BASE_MAP:
            if self.cultist_autosave_path != details_path:
                self.cultist_autosave_path = details_path
                self.persist_session_state()
                logger.info(
                    f"[Goal] Tracking Cultist Base completion from {details_path}."
                )
            return

        completed_cultist_base = (
            map_name != CULTIST_BASE_MAP
            and is_completed
            and details_path == self.cultist_autosave_path
        )
        if completed_cultist_base:
            await self.send_campaign_goal("legacy save fallback")

        if fresh_completion and map_name in {"e3m4_boss", "game/sp/e3m4_boss/e3m4_boss"}:
            matching = [p for p in PUBLISHERS if p.key == "final_sin_mission_complete"]
            for publisher in matching:
                await DoomEternalContext.execute_publisher(
                    self, publisher, "save_fallback", "Final Sin save fallback"
                )

        if fresh_completion and map_name in {"e1m4_boss", "game/sp/e1m4_boss/e1m4_boss"}:
            matching = [p for p in PUBLISHERS if p.key == "doom_hunter_base_mission_complete"]
            for publisher in matching:
                await DoomEternalContext.execute_publisher(
                    self, publisher, "save_fallback", "Doom Hunter Base save fallback"
                )

    async def check_campaign_goal(self):
        if not self.item_state_ready:
            await self.check_campaign_goal_event()
            return

        if await self.check_campaign_goal_event():
            return

        await self.check_campaign_goal_save_fallback()

    def check_rpc_autopause(self):
        evidence = read_gameplay_save_evidence()
        marker = self.read_active_map_identity(evidence=evidence)

        if marker is not None:
            map_name = marker["runtime_map"]
        elif evidence and evidence.state == "gameplay":
            map_name = evidence.map_name
        else:
            self.last_rpc_map_name = None
            self.current_map_name = None
            return

        self.current_map_name = map_name
        if self.last_rpc_map_name is None:
            self.last_rpc_map_name = map_name
            self.onboard_bootstrap("on_supported_map_load")
            return

        if map_name != self.last_rpc_map_name:
            logger.info(
                f"[RPC] Map transition observed: "
                f"{self.last_rpc_map_name} -> {map_name}. "
                "Queued commands remain armed; the native memory gate controls "
                "safe execution."
            )
            self.last_rpc_map_name = map_name
            self.onboard_bootstrap("on_supported_map_load")

    async def death_monitor_loop(self):
        while not self.exit_event.is_set():
            self.check_rpc_autopause()
            self.queue_received_deathlink()
            used_duration = False
            if death_probe_available():
                used_duration = await self.check_game_duration_death()
            if not used_duration:
                await self.check_game_details_death()
            await self.check_weapon_mastery_locations()
            await self.check_mission_challenge_locations()
            await self.check_campaign_goal()
            await asyncio.sleep(1.0)

    async def flush_check_event_files(self):
        get_key = getattr(self, "get_ap_state_key", None)
        state_key = get_key() if get_key else None
        if state_key:
            self.check_and_update_event_session()

        event_paths_by_location = {}
        unknown_event_paths = []
        for path in check_event_files():
            if os.path.basename(path) in PUBLISHER_MAP_EVENT_FILENAMES:
                continue
            location_id = extract_location_id_from_event(path)
            if location_id is None:
                unknown_event_paths.append(path)
                continue
            event_paths_by_location.setdefault(location_id, []).append(path)

        for path in unknown_event_paths:
            logger.warning(
                "[Trigger] Could not identify AP event location from "
                f"{os.path.basename(path)}; leaving file in place."
            )

        pending_locations = []
        for location_id, paths in event_paths_by_location.items():
            if location_id in self.checked_locations:
                for path in paths:
                    try:
                        os.remove(path)
                    except FileNotFoundError:
                        pass
                    except OSError as error:
                        logger.warning(
                            "[Trigger] Could not remove acknowledged AP event "
                            f"{os.path.basename(path)} yet: {error}"
                        )
                continue
            if getattr(self, "item_state_ready", False) and self.server_locations and location_id not in self.server_locations:
                logger.warning(
                    "[Trigger] AP event location %s not in connected slot; quarantining.",
                    location_id,
                )
                for path in paths:
                    quarantine_event_file(
                        path,
                        old_state_key=state_key,
                        new_state_key=state_key,
                        reason="location_not_in_connected_slot",
                    )
                continue
            if location_id not in self.server_locations:
                logger.warning(
                    "[Trigger] AP event location %s is not part of the connected slot; leaving file in place.",
                    location_id,
                )
                continue
            if location_id not in self.locations_checked:
                pending_locations.append(location_id)

        if not pending_locations:
            return

        self.last_processed_event_id = pending_locations[-1]
        try:
            await self.send_msgs(
                [{"cmd": "LocationChecks", "locations": pending_locations}]
            )
        except Exception as error:
            logger.error(
                "[Trigger] Failed to send AP check events; preserving files "
                f"for retry: {error}"
            )
            return

        for location_id in pending_locations:
            logger.info(
                "[Trigger] Native AP event detected -> Queued "
                f"Location {location_id}"
            )
            self.locations_checked.add(location_id)

    async def tracker_loop(self):
        logger.info(
            "[Tracking] Starting Doom Eternal runtime tracking loop "
            "(polling every 4 seconds)."
        )
        logger.info(
            "[RPC] Auto-RPC waits for telemetry-ready, then the native memory "
            "gate permits execution only in safe gameplay. Check delivery prefers "
            "native ap_event files over telemetry polls."
        )
        self.last_heartbeat_timestamp = time.time()
        self.heartbeat_iteration_count = 0

        while not self.exit_event.is_set():
            self.last_heartbeat_timestamp = time.time()
            self.heartbeat_iteration_count += 1
            if self.heartbeat_iteration_count % 15 == 0:
                logger.info(
                    "[Tracking] TRACKER_HEARTBEAT active_slot=%s map=%s items_processed=%d/%d",
                    self.active_save_slot or "<none>",
                    self.current_map_name or "<none>",
                    self.items_processed,
                    len(self.items_received),
                )

            if self.server and self.server.socket and not self.server.socket.closed:
                try:
                    migrate_direct_item_command_jobs()
                    self.onboard_bootstrap("on_reconnect")
                    self.reconcile_owned_perks("connect_or_reconnect")
                    self.reconcile_checked_automap_cleanup("connect_or_reconnect")
                    if not self.repair_item_mappings():
                        await asyncio.sleep(0.25)
                        continue
                except Exception as exc:
                    logger.warning("[Tracking] Error during reconnection reconciliation: %s", exc)

                markers = discover_telemetry_markers()
                if markers:
                    newest_mtime, newest_path = markers[-1]
                    try:
                        self.ingest_visible_runtime_lifecycle(
                            evidence=read_gameplay_save_evidence(),
                            lifecycle_markers=markers,
                        )
                        if not rpc_execution_enabled():
                            set_rpc_execution(True)
                        epoch = self.advance_reconciliation_epoch("level_ready")
                        logger.info(
                            "[RPC] Level-ready signal received (%s). RPC armed; "
                            "perk reconciliation epoch %s queued behind native safety gate.",
                            os.path.basename(newest_path),
                            epoch,
                        )
                        self.reconcile_owned_perks("level_ready")
                        self.advance_automap_cleanup_epoch()
                        self.reconcile_checked_automap_cleanup("level_ready")
                    except Exception as e:
                        logger.error("[RPC] Auto-RPC failed to consume telemetry ready file %s: %s", newest_path, e)

                    for _mtime, path in markers:
                        try:
                            os.remove(path)
                        except OSError:
                            pass

                # Consume the load epoch and map marker before observing challenges;
                # a marker can make the current poll's challenge observation eligible.
                await self.check_mission_challenge_locations()

                while (
                    self.item_state_ready
                    and len(self.items_received) > self.items_processed
                ):
                    item_index = self.items_processed
                    network_item = self.items_received[item_index]
                    item_id = network_item.item
                    log_delivery_event(
                        "ITEM_RECEIPT",
                        receipt_index=item_index,
                        item_id=item_id,
                        item_name=self.delivery_item_name(item_id)
                        if item_id in ITEM_CLASSIFICATION_IDENTITY else None,
                        active_map=self.current_map_name,
                        slot=self.active_save_slot,
                        bridge_revision=BRIDGE_REVISION,
                        protocol_version=BRIDGE_PROTOCOL,
                    )
                    if item_id not in ITEM_ID_TO_COMMAND:
                        logger.error(
                            f"[To Game] No command mapping for item {item_id}; "
                            "delivery paused. The seed/APWorld and bridge build "
                            "are out of sync."
                        )
                        self.output(
                            f"Missing item mapping for DOOM Eternal item {item_id}. "
                            "Check the local bridge logs."
                        )
                        break

                    definition = ITEM_ID_TO_COMMAND[item_id]
                    if (
                        isinstance(definition, dict)
                        and definition.get("type") == "no_op"
                    ):
                        logger.info(
                            f"[To Game] Runtime-only item {item_id} acknowledged."
                        )
                        self.items_processed += 1
                        self.persist_session_state()
                        continue

                    try:
                        classification = received_item_classification(
                            item_id, getattr(network_item, "flags", None)
                        )
                        spooled, description = self.spool_item_commands(
                            item_id,
                            item_index,
                            receipt=ENABLE_ITEM_NOTIFICATIONS,
                            classification=classification,
                        )
                        if not spooled:
                            item_name = getattr(self, "item_names", {}).get(item_id, f"Item_{item_id}")
                            logger.error(
                                "[To Game] ITEM_DELIVERY_BLOCKED index=%d item_id=%d item_name=%s description=%s",
                                item_index, item_id, item_name, description
                            )
                            self.item_delivery_blocked = True
                            self.item_delivery_blocked_info = {
                                "index": item_index,
                                "item_id": item_id,
                                "item_name": item_name,
                                "description": description,
                            }
                            break
                    except Exception as error:
                        item_name = getattr(self, "item_names", {}).get(item_id, f"Item_{item_id}")
                        tb = traceback.format_exc()
                        logger.error(
                            "[To Game] ITEM_DELIVERY_BLOCKED index=%d item_id=%d item_name=%s type=%s msg=%s\n%s",
                            item_index, item_id, item_name, type(error).__name__, str(error), tb
                        )
                        self.item_delivery_blocked = True
                        self.item_delivery_blocked_info = {
                            "index": item_index,
                            "item_id": item_id,
                            "item_name": item_name,
                            "exception_type": type(error).__name__,
                            "exception_message": str(error),
                            "traceback": tb,
                        }
                        break
                    else:
                        self.item_delivery_blocked = False
                        self.item_delivery_blocked_info = None

                    logger.info(
                        f"[To Game] Item received! {item_id} -> {description}"
                    )
                    self.items_processed += 1
                    self.persist_session_state()
                    self.onboard_bootstrap("on_item_received")
                    self.reconcile_owned_perks("item_received")

                try:
                    self.reconcile_owned_perks("post_item_scan")
                except Exception as exc:
                    logger.warning("[Tracking] Error during post_item_scan perk reconciliation: %s", exc)

                await self.flush_check_event_files()

            await asyncio.sleep(4.0)

    async def tracker_supervisor(self):
        logger.info("[Supervisor] TRACKER_STARTED")
        self.tracker_alive = True
        self.tracker_restart_count = getattr(self, "tracker_restart_count", 0)
        self.last_tracker_error = getattr(self, "last_tracker_error", None)
        self.last_heartbeat_timestamp = time.time()
        self.tracker_backoff = getattr(self, "tracker_backoff", 1.0)
        self.last_error_fingerprint = getattr(self, "last_error_fingerprint", None)
        self.consecutive_same_error_count = getattr(self, "consecutive_same_error_count", 0)
        self.tracker_degraded = getattr(self, "tracker_degraded", False)

        while not self.exit_event.is_set():
            try:
                await self.tracker_loop()
                break
            except asyncio.CancelledError:
                logger.info("[Supervisor] TRACKER_STOPPED (clean shutdown)")
                self.tracker_alive = False
                raise
            except Exception as exc:
                self.tracker_restart_count += 1
                tb = traceback.format_exc()
                lineno = exc.__traceback__.tb_lineno if exc.__traceback__ else 0
                fingerprint = f"{type(exc).__name__}:{exc}:{lineno}"
                if fingerprint == self.last_error_fingerprint:
                    self.consecutive_same_error_count += 1
                    self.tracker_backoff = min(30.0, self.tracker_backoff * 2.0)
                else:
                    self.last_error_fingerprint = fingerprint
                    self.consecutive_same_error_count = 1
                    self.tracker_backoff = 1.0

                self.last_tracker_error = f"{type(exc).__name__}: {exc}"
                self.tracker_degraded = True
                if self.consecutive_same_error_count <= 2:
                    logger.error(
                        "[Supervisor] TRACKER_CRASH type=%s msg=%s ap_state_key=%s "
                        "current_map=%s active_save_slot=%s last_processed_event=%s "
                        "last_heartbeat_age=%.1fs traceback:\n%s",
                        type(exc).__name__,
                        str(exc),
                        self.get_ap_state_key(),
                        self.current_map_name,
                        self.active_save_slot,
                        getattr(self, "last_processed_event_id", None),
                        time.time() - (self.last_heartbeat_timestamp or time.time()),
                        tb,
                    )
                logger.info(
                    "[Supervisor] TRACKER_RESTART count=%d backoff=%.1fs consecutive_errors=%d fingerprint=%s",
                    self.tracker_restart_count,
                    self.tracker_backoff,
                    self.consecutive_same_error_count,
                    fingerprint,
                )
                await asyncio.sleep(self.tracker_backoff)
        self.tracker_alive = False

    def make_gui(self):
        from kvui import GameManager

        class DoomEternalManager(GameManager):
            logging_pairs = (("Client", "Archipelago"),)
            base_title = "DOOM Eternal Archipelago Client"

        return DoomEternalManager

async def amain(launch_args=None):
    start_bridge_logger()
    Utils.init_logging("DoomEternalClient")
    parser = get_base_parser()
    parser.add_argument('--name', default=None, help="Player name no Archipelago")
    args = parser.parse_args(launch_args)
    args.password = args.password or os.environ.get("DOOM_AP_PASSWORD")

    ctx = DoomEternalContext(args.connect, args.password)
    ctx.auth = args.name
    set_rpc_execution(False)
    ctx.tracking_task = asyncio.create_task(ctx.tracker_supervisor())
    ctx.death_task = asyncio.create_task(ctx.death_monitor_loop())

    log_mission_bridge_identity()
    emit_launcher_event("client_started")
    logger.info("=== DOOM ETERNAL ARCHIPELAGO CLIENT ===")
    if not args.connect or not args.name:
        logger.info(
            "Use the GUI connection fields, or pass --connect and --name "
            "on the command line."
        )
    else:
        logger.info(f"Auto-connecting to {args.connect} as {args.name}...")
        emit_launcher_event("connecting")

    ctx.server_task = asyncio.create_task(server_loop(ctx), name="server loop")
    def report_server_stop(task):
        if ctx.exit_event.is_set():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            error = None
        if error is not None:
            emit_launcher_event(
                "error",
                code="server_loop_failed",
                message=f"{type(error).__name__}: {error}",
            )
        else:
            emit_launcher_event("disconnected")
    ctx.server_task.add_done_callback(report_server_stop)

    if gui_enabled:
        ctx.run_gui()
    ctx.run_cli()

    await ctx.exit_event.wait()
    emit_launcher_event("client_stopping")
    await ctx.shutdown()
    await asyncio.gather(
        ctx.tracking_task,
        ctx.death_task,
        return_exceptions=True,
    )

def launch(*launch_args):
    colorama.init()
    asyncio.run(amain(launch_args))
    colorama.deinit()


if __name__ == '__main__':
    launch(*sys.argv[1:])
