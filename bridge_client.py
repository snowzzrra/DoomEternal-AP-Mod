import asyncio
import atexit
import os
import sys
import glob
import time
import re
import shutil
import subprocess
import uuid

import json
from pathlib import Path

try:
    from .save_decrypt import decrypt, steam_id64
except ImportError:
    from save_decrypt import decrypt, steam_id64

CONFIG_FILE = Path(
    os.environ.get("DOOM_AP_CONFIG_FILE", Path(__file__).with_name("ap_config.json"))
)


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

import Utils
import colorama
from CommonClient import (
    CommonContext,
    server_loop,
    gui_enabled,
    ClientCommandProcessor,
    get_base_parser,
    logger,
)
from NetUtils import ClientStatus

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
                raise RuntimeError(f"DOOM Eternal Client Setup cancelled. Please create ap_config.json manually with 'doom_base_dir' and 'save_games_dir'.")
                
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
        "Select DOOM Eternal Base Directory (the base folder containing DOOMEternalx64vk.exe)",
        lambda p: normalize_doom_base_dir(p) if p else None,
        "Could not find DOOMEternalx64vk.exe and classicwads. Select .../DOOMEternal/base, not .../common/base."
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
INV_DUMP_DIR = SAVE_GAMES_DIR
CULTIST_BASE_MAP = "game/sp/e1m3_cult/e1m3_cult"
DOOM_HUNTER_BASE_MAP = "game/sp/e1m4_boss/e1m4_boss"
DEATHLINK_KILL_INTERVAL = 2.0
DEATHLINK_KILL_COALESCE_KEY = "deathlink-kill"
CHECK_EVENT_PREFIX = "ap_event_"
GOAL_EVENT_PREFIX = "ap_transition_"
GOAL_EVENT_FILENAME = "ap_transition_e1m3_cult_to_e1m4_boss.evt"
TELEMETRY_DUMP_PREFIX = "ap_telemetry"
LEGACY_TELEMETRY_DUMP_PREFIX = "ap_condump"
ITEM_MAPPING_REVISION = 2
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

def discover_steam_remote():
    configured = config.get("steam_remote_dir")
    if configured:
        path = Path(configured).expanduser()
        inferred_id = 0
        try:
            inferred_id = int(path.parents[1].name)
        except (IndexError, ValueError):
            pass
        return path, parse_int(config.get("steam_id3"), inferred_id)

    def add_remote_glob(candidate_root, discovered):
        if candidate_root:
            discovered.extend(Path(candidate_root).joinpath("userdata").glob("*/782330/remote"))

    home = Path.home()
    homes = [home]
    if os.name != "nt" and home.is_absolute():
        var_home = Path("/var") / home.relative_to("/")
        if var_home != home:
            homes.append(var_home)
    candidates = [
        path
        for candidate_home in homes
        for path in candidate_home.joinpath(
            ".local/share/Steam/userdata"
        ).glob("*/782330/remote")
    ]
    steam_roots = []
    program_files = os.environ.get("PROGRAMFILES(X86)")
    if program_files:
        steam_roots.append(Path(program_files) / "Steam")
    program_files_w6432 = os.environ.get("PROGRAMW6432")
    if program_files_w6432:
        steam_roots.append(Path(program_files_w6432) / "Steam")
    configured_steam_root = config.get("steam_root_dir")
    if configured_steam_root:
        steam_roots.append(Path(configured_steam_root).expanduser())
    try:
        doom_base = Path(DOOM_BASE_DIR)
        for parent in [doom_base, *doom_base.parents]:
            if parent.name.lower() == "steamapps":
                steam_roots.append(parent.parent)
                break
    except NameError:
        pass
    for steam_root in steam_roots:
        add_remote_glob(steam_root, candidates)

    for path in candidates:
        if path.is_dir():
            return path, int(path.parents[1].name)
    return Path(), parse_int(config.get("steam_id3"), 0)


STEAM_REMOTE_DIR, STEAM_ID3 = discover_steam_remote()
if STEAM_REMOTE_DIR and STEAM_REMOTE_DIR.is_dir():
    remote_path = str(STEAM_REMOTE_DIR)
    if (
        config.get("steam_remote_dir") != remote_path
        or parse_int(config.get("steam_id3"), 0) != STEAM_ID3
    ):
        config["steam_remote_dir"] = remote_path
        config["steam_id3"] = STEAM_ID3
        save_config()

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


def newest_save_file(filename):
    if not STEAM_ID3 or not STEAM_REMOTE_DIR.is_dir():
        return None
    candidates = sorted(
        STEAM_REMOTE_DIR.glob(f"GAME-AUTOSAVE*/{filename}"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    return candidates[0] if candidates else None


def death_probe_available():
    if not DEATH_PROBE.is_file() or not OODLE_DLL.is_file():
        return False
    if os.name == "nt":
        return True
    return (
        PROTON_PATH.is_file()
        and STEAM_COMPAT_DATA.is_dir()
        and STEAM_INSTALL.is_dir()
    )


def probe_checkpoint_death(path):
    """Return True while DOOM's numCheckpointDeaths field is nonzero."""
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

    if os.name == "nt":
        command = [str(runtime_probe), runtime_oodle.name, runtime_save.name]
        environment = None
    else:
        DEATH_PROBE_COMPAT_DATA.mkdir(parents=True, exist_ok=True)
        proton_command = [
            str(PROTON_PATH),
            "run",
            runtime_probe.name,
            runtime_oodle.name,
            runtime_save.name,
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
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    if result.returncode == 20:
        return True
    if result.returncode == 0:
        return False
    raise RuntimeError(f"save_death_probe exited with code {result.returncode}")


# Load item definitions
ITEMS_FILE = os.path.join(os.path.dirname(__file__), "data", "items.json")
with open(ITEMS_FILE, "r", encoding="utf-8") as f:
    # Keys in JSON are strings, convert them to ints
    _raw_items = json.load(f)
    ITEM_ID_TO_COMMAND = {int(k): v for k, v in _raw_items.items()}

RUNTIME_LOCATIONS_FILE = os.path.join(
    os.path.dirname(__file__), "data", "runtime_locations.json"
)
with open(RUNTIME_LOCATIONS_FILE, "r", encoding="utf-8") as f:
    RUNTIME_LOCATIONS = json.load(f)
CULTIST_BASE_COMPLETE_LOCATION = RUNTIME_LOCATIONS[
    "Cultist Base - Mission Complete"
]

# Load ALL level manifests dynamically
DECL_TO_LOCATION = {}
MANIFESTS_DIR = os.path.join(os.path.dirname(__file__), "manifests")
if os.path.exists(MANIFESTS_DIR):
    for filename in os.listdir(MANIFESTS_DIR):
        if filename.endswith(".json"):
            with open(os.path.join(MANIFESTS_DIR, filename), "r", encoding="utf-8") as f:
                manifest_data = json.load(f)
                DECL_TO_LOCATION.update(manifest_data)

poll_counter = 0

def send_command(cmd, coalesce_key=None, arm_rpc=True):
    """Atomically enqueue one command without overwriting another command.

    A coalesced command has at most one queued or in-flight spool file. This is
    used for telemetry requests so menus/loading screens cannot accumulate a
    large condump backlog behind the player-state gate.
    """
    try:
        os.makedirs(QUEUE_DIR, exist_ok=True)
        command_id = coalesce_key or f"{time.time_ns():020d}-{uuid.uuid4().hex}"
        if coalesce_key:
            queued_path = os.path.join(QUEUE_DIR, f"{coalesce_key}.cmd")
            processing_path = os.path.join(QUEUE_DIR, f"{coalesce_key}.processing")
            if os.path.exists(queued_path) or os.path.exists(processing_path):
                return False

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
                return False
            finally:
                try:
                    os.remove(temporary_path)
                except FileNotFoundError:
                    pass
            if os.path.exists(processing_path):
                try:
                    os.remove(command_path)
                except FileNotFoundError:
                    pass
                return False
        else:
            os.replace(temporary_path, command_path)
        if arm_rpc:
            set_rpc_execution(True)
        return True
    except Exception as e:
        logger.error(f"[Error] Failed to enqueue game command: {e}")
        return False

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
        rf"^{CHECK_EVENT_PREFIX}(\d+)(?:_\d+)?\.txt$",
        basename,
    )
    if filename_match:
        return int(filename_match.group(1))

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            contents = f.read()
    except OSError:
        return None

    content_match = re.search(r"AP_CHECK_EVENT_(\d+)", contents)
    if content_match:
        return int(content_match.group(1))
    return None


def parse_goal_transition_event(path):
    data = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
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
    return data


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
        with open(latest_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            for line in lines:
                lower_line = line.lower()
                if lower_line.startswith("mapname:"):
                    _, value = line.split(":", 1)
                    map_name = value.strip()
                if "idbloatedentity::activate" in lower_line and "ap_check_" in lower_line:
                    match = re.search(r'(ap_check_[a-z0-9_]+)', lower_line)
                    if match:
                        checks_found.add(match.group(1).upper())
                        
        cleanup_telemetry_dumps()

        return list(checks_found), map_name
    except Exception as e:
        logger.error(f"[Error] Failed to process telemetry condump: {e}")
        return [], None

def read_game_details():
    path = newest_save_file("game.details")
    if not path:
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
        return values
    except Exception as error:
        logger.error(f"[Save] Failed to decrypt {path}: {error}")
        return None

class DoomCommandProcessor(ClientCommandProcessor):
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
        """Queue a givePlayerPerk command for a targeted in-game test."""
        if not perk_path.startswith("perk/player/"):
            self.output("Usage: /doom_perk perk/player/...")
            return
        send_command(f"ai_ScriptCmdEnt player1 givePlayerPerk {perk_path}")
        self.output(f"Queued perk test: {perk_path}")

    def _cmd_doom_item(self, item_id: str = ""):
        """Activate an injected AP item command by numeric item ID."""
        try:
            parsed_id = int(item_id)
        except ValueError:
            self.output("Usage: /doom_item <numeric item id>")
            return
        if parsed_id not in ITEM_ID_TO_COMMAND:
            self.output(f"Unknown Doom Eternal item ID: {parsed_id}")
            return
        command = ITEM_ID_TO_COMMAND[parsed_id]
        if isinstance(command, dict) and command.get("type") == "progressive_perk":
            self.output(
                "Progressive items require a stage. "
                "Use /doom_progressive_item <item id> <stage index>."
            )
            return
        send_command(f"ai_ScriptCmdEnt {RPC_ENTITY_PREFIX}_{parsed_id} activate")
        self.output(f"Queued AP item test: {parsed_id} -> {command}")

    def _cmd_doom_direct_chainsaw(self):
        """Queue the raw chainsaw give command, bypassing injected AP entities."""
        send_command("give weapon/player/chainsaw")
        self.output(
            "Queued direct chainsaw give command. If this works but "
            "/doom_item 7770010 does not, the injected AP entity path is broken."
        )

    def _cmd_doom_progressive_item(
        self, item_id: str = "", stage: str = ""
    ):
        """Activate one stage of a progressive AP item for a targeted test."""
        try:
            parsed_id = int(item_id)
            parsed_stage = int(stage)
        except ValueError:
            self.output(
                "Usage: /doom_progressive_item <item id> <stage index>"
            )
            return
        command = ITEM_ID_TO_COMMAND.get(parsed_id)
        perks = command.get("perks", []) if isinstance(command, dict) else []
        if (
            not isinstance(command, dict)
            or command.get("type") != "progressive_perk"
            or not 0 <= parsed_stage < len(perks)
        ):
            self.output("Unknown progressive item or invalid stage.")
            return
        if send_command(
            f"ai_ScriptCmdEnt {RPC_ENTITY_PREFIX}_{parsed_id}_{parsed_stage} activate"
        ):
            self.output(
                f"Queued progressive item test: {parsed_id} stage {parsed_stage} "
                f"-> {perks[parsed_stage]}"
            )

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
        """Show command-spool, save-monitor, and DeathLink status."""
        details = read_game_details()
        self.output(f"Queue directory: {QUEUE_DIR}")
        queue_path = Path(QUEUE_DIR)
        pending_commands = list(queue_path.glob("*.cmd")) if queue_path.is_dir() else []
        processing_commands = (
            list(queue_path.glob("*.processing")) if queue_path.is_dir() else []
        )
        failed_commands = list(queue_path.glob("*.failed")) if queue_path.is_dir() else []
        self.output(
            f"Queued commands: pending={len(pending_commands)} "
            f"processing={len(processing_commands)} failed={len(failed_commands)}"
        )
        self.output(
            f"RPC intent: {'ARMED' if rpc_execution_enabled() else 'DISARMED'}"
        )
        self.output("RPC safety gate: native read-only memory probe")
        self.output(
            "ap_client.exe must log: RPC memory gate OPEN -> "
            "Executing queued command -> Command completed"
        )
        self.output(f"Telemetry directory: {INV_DUMP_DIR}")
        self.output(f"Check event files: {len(check_event_files())}")
        self.output(f"Telemetry dump files: {len(telemetry_dump_files())}")
        self.output(f"Steam remote directory: {STEAM_REMOTE_DIR}")
        self.output(f"DeathLink enabled: {getattr(self.ctx, 'death_link_enabled', False)}")
        self.output(
            f"Received DeathLink pending: "
            f"{getattr(self.ctx, 'deathlinked', False)}"
        )
        if getattr(self.ctx, "item_state_ready", False):
            self.output(
                f"Item state: {self.ctx.items_processed}/"
                f"{len(self.ctx.items_received)} processed "
                f"key={self.ctx.state_key}"
            )
            self.output(
                f"Cultist Base goal sent: {self.ctx.session_state.get('goal_sent', False)}"
            )
        else:
            self.output("Item state: waiting for slot connection")
        self.output(
            f"Death detector: {'game_duration.dat' if death_probe_available() else 'game.details fallback'}"
        )
        if details:
            self.output(
                f"Save: map={details.get('mapName')} diedLastGame={details.get('diedLastGame')} "
                f"time={details.get('time')} path={details.get('_path')}"
            )
        else:
            self.output("Save monitor: no readable game.details found")

class DoomEternalContext(CommonContext):
    command_processor: type = DoomCommandProcessor
    game = "Doom Eternal"
    items_handling = 0b111

    def __init__(self, server_address, password):
        super().__init__(server_address, password)
        self.tracking_task = None
        self.items_processed = 0
        self.item_state_ready = False
        self.client_state = {"version": 1, "sessions": {}}
        self.state_key = ""
        self.session_state = {}
        self.death_link_enabled = False
        self.previous_checkpoint_death = None
        self.last_duration_mtime = None
        self.death_probe_warning = None
        self.confirmed_death_echo = None
        self.previous_died_last_game = None
        self.last_details_mtime = None
        self.last_details_path = None
        self.deathlinked = False
        self.last_deathlink_kill_attempt = 0.0
        self.last_goal_details_mtime = None
        self.cultist_autosave_path = None
        self.last_rpc_map_name = None
        self.room_seed_name = None

    async def server_auth(self, password_requested: bool = False):
        if password_requested and not self.password:
            await super(DoomEternalContext, self).server_auth(password_requested)
        await self.get_username()
        await self.send_connect()

    def on_package(self, cmd: str, args: dict):
        if cmd == "RoomInfo":
            self.room_seed_name = args.get("seed_name")
        elif cmd == "Connected":
            self.initialize_item_state()
            self.death_link_enabled = bool(args.get("slot_data", {}).get("death_link", False))
            asyncio.create_task(self.update_death_link(self.death_link_enabled))
        elif cmd == "Bounced" and "DeathLink" in args.get("tags", []):
            data = args.get("data", {})
            if (
                data.get("time") == self.last_death_link
                and data.get("time") != self.confirmed_death_echo
            ):
                logger.info("[DeathLink] Server received and echoed the death.")
                self.confirmed_death_echo = data.get("time")

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
                "deathlinked": False,
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
        self.deathlinked = bool(self.session_state.get("deathlinked", False))
        self.item_state_ready = True
        save_client_state(self.client_state)
        logger.info(
            f"[State] Loaded {self.items_processed} processed items for "
            f"{self.state_key}."
        )

    def persist_session_state(self):
        if not self.item_state_ready:
            return
        self.session_state["processed_items"] = self.items_processed
        self.session_state["cultist_autosave_path"] = self.cultist_autosave_path
        self.session_state["deathlinked"] = self.deathlinked
        save_client_state(self.client_state)

    def reset_item_state(self):
        self.items_processed = 0
        self.session_state["processed_items"] = 0
        self.session_state["item_mapping_revision"] = ITEM_MAPPING_REVISION
        self.session_state.pop("mapping_repair_indices", None)
        save_client_state(self.client_state)

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
            activation, description = self.item_activation_command(
                network_item.item, item_index
            )
            if activation is None or not send_command(activation):
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

    def item_activation_command(self, item_id, item_index):
        definition = ITEM_ID_TO_COMMAND.get(item_id)
        if not isinstance(definition, dict):
            return (
                f"ai_ScriptCmdEnt {RPC_ENTITY_PREFIX}_{item_id} activate",
                definition,
            )
        if definition.get("type") != "progressive_perk":
            return (
                f"ai_ScriptCmdEnt {RPC_ENTITY_PREFIX}_{item_id} activate",
                definition,
            )

        perks = definition.get("perks", [])
        stage = self.progressive_stage(item_id, item_index)
        if stage >= len(perks):
            return None, f"progressive stage {stage} exceeds {len(perks)} stages"
        return (
            f"ai_ScriptCmdEnt {RPC_ENTITY_PREFIX}_{item_id}_{stage} activate",
            f"stage {stage}: {perks[stage]}",
        )

    def on_deathlink(self, data: dict):
        super().on_deathlink(data)
        if self.death_link_enabled:
            self.deathlinked = True
            self.last_deathlink_kill_attempt = 0.0
            self.persist_session_state()
            logger.info(
                "[DeathLink] Received. Will retry the in-game death until "
                "the save confirms it."
            )

    def queue_received_deathlink(self):
        if (
            not self.death_link_enabled
            or not self.deathlinked
        ):
            return

        now = time.monotonic()
        if now - self.last_deathlink_kill_attempt < DEATHLINK_KILL_INTERVAL:
            return
        self.last_deathlink_kill_attempt = now
        if send_command(
            "ai_ScriptCmdEnt ap_deathlink activate",
            coalesce_key=DEATHLINK_KILL_COALESCE_KEY,
        ):
            logger.info(
                "[DeathLink] Queued received death; awaiting save confirmation."
            )

    async def check_game_duration_death(self):
        path = newest_save_file("game_duration.dat")
        if not path:
            return False

        mtime = path.stat().st_mtime_ns
        if mtime == self.last_duration_mtime:
            return True

        try:
            died = await asyncio.to_thread(probe_checkpoint_death, path)
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
        self.last_duration_mtime = mtime
        if self.previous_checkpoint_death is None:
            self.previous_checkpoint_death = died
            logger.info(
                f"[Save] Monitoring {path} numCheckpointDeaths for DeathLink."
            )
            return True

        transitioned_to_dead = died and not self.previous_checkpoint_death
        self.previous_checkpoint_death = died
        if transitioned_to_dead:
            logger.info("[DeathLink] numCheckpointDeaths changed 0 -> 1.")
            await self.report_local_death()
        return True

    async def check_game_details_death(self):
        details = read_game_details()
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
        if self.deathlinked:
            self.deathlinked = False
            discard_queued_coalesced_command(DEATHLINK_KILL_COALESCE_KEY)
            self.persist_session_state()
            logger.info("[DeathLink] Suppressed echo from received DeathLink.")
            return

        await self.send_death(f"{self.auth or 'The Doom Slayer'} was slain.")

    async def send_campaign_goal(self, source_description):
        if not self.server or not self.server.socket or self.server.socket.closed:
            return False

        messages = []
        if CULTIST_BASE_COMPLETE_LOCATION not in self.locations_checked:
            self.locations_checked.add(CULTIST_BASE_COMPLETE_LOCATION)
            messages.append(
                {
                    "cmd": "LocationChecks",
                    "locations": [CULTIST_BASE_COMPLETE_LOCATION],
                }
            )
        messages.append(
            {"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}
        )

        await self.send_msgs(messages)
        self.session_state["goal_sent"] = True
        self.persist_session_state()
        logger.info(
            "[Goal] Cultist Base completed. Goal reported to Archipelago via "
            f"{source_description}."
        )
        return True

    async def check_campaign_goal_event(self):
        event_paths = goal_event_files()
        if not event_paths:
            return False

        if self.session_state.get("goal_sent", False):
            for path in event_paths:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    logger.warning(
                        "[Goal] Could not remove stale goal transition event "
                        f"{os.path.basename(path)} yet: {error}"
                    )
            return True

        for path in event_paths:
            event = parse_goal_transition_event(path)
            if event is None:
                logger.warning(
                    "[Goal] Malformed goal transition event "
                    f"{os.path.basename(path)}; removing it."
                )
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    logger.warning(
                        "[Goal] Could not remove malformed goal transition "
                        f"event {os.path.basename(path)} yet: {error}"
                    )
                continue

            if (
                event.get("from_map") != CULTIST_BASE_MAP
                or event.get("to_map") != DOOM_HUNTER_BASE_MAP
            ):
                logger.warning(
                    "[Goal] Ignoring unexpected goal transition event "
                    f"{os.path.basename(path)}: "
                    f"{event.get('from_map')} -> {event.get('to_map')}"
                )
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    logger.warning(
                        "[Goal] Could not remove unexpected goal transition "
                        f"event {os.path.basename(path)} yet: {error}"
                    )
                continue

            try:
                sent = await self.send_campaign_goal(
                    "native transition event"
                )
            except Exception as error:
                logger.error(
                    "[Goal] Failed to send goal from native transition event; "
                    f"preserving file for retry: {error}"
                )
                return True
            if not sent:
                return True

            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as error:
                logger.warning(
                    "[Goal] Goal sent but transition event file "
                    f"{os.path.basename(path)} could not be removed yet: {error}"
                )
            return True

        return True

    async def check_campaign_goal_save_fallback(self):
        details = read_game_details()
        if not details:
            return
        mtime = details.get("_mtime_ns")
        if mtime == self.last_goal_details_mtime:
            return
        self.last_goal_details_mtime = mtime

        details_path = details.get("_path")
        map_name = details.get("mapName")
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
            and details.get("completed") == "1"
            and details_path == self.cultist_autosave_path
        )
        if not completed_cultist_base:
            return

        await self.send_campaign_goal("legacy save fallback")

    async def check_campaign_goal(self):
        if (
            not self.item_state_ready
            or self.session_state.get("goal_sent", False)
        ):
            await self.check_campaign_goal_event()
            return

        if await self.check_campaign_goal_event():
            return

        await self.check_campaign_goal_save_fallback()

    def check_rpc_autopause(self):
        details = read_game_details()
        if not details:
            self.last_rpc_map_name = None
            return

        map_name = details.get("mapName")
        if self.last_rpc_map_name is None:
            self.last_rpc_map_name = map_name
            return

        if map_name != self.last_rpc_map_name:
            logger.info(
                f"[RPC] Map transition observed: "
                f"{self.last_rpc_map_name} -> {map_name}. "
                "Queued commands remain armed; the native memory gate controls "
                "safe execution."
            )
            self.last_rpc_map_name = map_name

    async def death_monitor_loop(self):
        while not self.exit_event.is_set():
            self.check_rpc_autopause()
            self.queue_received_deathlink()
            used_duration = False
            if death_probe_available():
                used_duration = await self.check_game_duration_death()
            if not used_duration:
                await self.check_game_details_death()
            await self.check_campaign_goal()
            await asyncio.sleep(1.0)

    async def flush_check_event_files(self):
        event_paths_by_location = {}
        unknown_event_paths = []
        for path in check_event_files():
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
            if location_id not in self.server_locations:
                logger.warning(
                    "[Trigger] AP event location "
                    f"{location_id} is not part of the connected slot; "
                    "leaving file in place."
                )
                continue
            if location_id not in self.locations_checked:
                pending_locations.append(location_id)

        if not pending_locations:
            return

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
        while not self.exit_event.is_set():
            if self.server and self.server.socket and not self.server.socket.closed:
                if not self.repair_item_mappings():
                    await asyncio.sleep(0.25)
                    continue
                    
                # Auto-RPC Resume Check
                if not rpc_execution_enabled():
                    ready_path = os.path.join(INV_DUMP_DIR, "ap_telemetry_ready.txt")
                    if os.path.exists(ready_path):
                        try:
                            os.remove(ready_path)
                            set_rpc_execution(True)
                            logger.info(
                                "[RPC] Level-ready signal received. RPC armed; "
                                "waiting for the native memory safety gate."
                            )
                        except Exception as e:
                            logger.error(f"[RPC] Auto-RPC failed to delete telemetry ready file: {e}")

                # Persist each item only after its durable spool file exists.
                while (
                    self.item_state_ready
                    and len(self.items_received) > self.items_processed
                ):
                    item_index = self.items_processed
                    network_item = self.items_received[item_index]
                    item_id = network_item.item
                    if item_id not in ITEM_ID_TO_COMMAND:
                        logger.error(
                            f"[To Game] No command mapping for item {item_id}; "
                            "delivery paused. The seed/APWorld and bridge build "
                            "are out of sync."
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

                    activation, description = self.item_activation_command(
                        item_id, item_index
                    )
                    if activation is None:
                        logger.error(
                            f"[To Game] Cannot deliver item {item_id}: {description}"
                        )
                        self.items_processed += 1
                        self.persist_session_state()
                        continue
                    if not send_command(activation):
                        logger.error(
                            f"[To Game] Failed to spool item {item_id}; "
                            "will retry without advancing item state."
                        )
                        break

                    logger.info(
                        f"[To Game] Item received! {item_id} -> {description}"
                    )
                    self.items_processed += 1
                    self.persist_session_state()

                await self.flush_check_event_files()

            await asyncio.sleep(4.0)

    def make_gui(self):
        from kvui import GameManager

        class DoomEternalManager(GameManager):
            logging_pairs = [("Client", "Archipelago")]
            base_title = "DOOM Eternal Archipelago Client"

        return DoomEternalManager

async def amain(launch_args=None):
    Utils.init_logging("DoomEternalClient")
    parser = get_base_parser()
    parser.add_argument('--name', default=None, help="Player name no Archipelago")
    args = parser.parse_args(launch_args)

    ctx = DoomEternalContext(args.connect, args.password)
    ctx.auth = args.name
    set_rpc_execution(False)
    ctx.tracking_task = asyncio.create_task(ctx.tracker_loop())
    ctx.death_task = asyncio.create_task(ctx.death_monitor_loop())

    logger.info("=== DOOM ETERNAL ARCHIPELAGO CLIENT ===")
    if not args.connect or not args.name:
        logger.info(
            "Use the GUI connection fields, or pass --connect and --name "
            "on the command line."
        )
    else:
        logger.info(f"Auto-connecting to {args.connect} as {args.name}...")
    
    ctx.server_task = asyncio.create_task(server_loop(ctx), name="server loop")

    if gui_enabled:
        ctx.run_gui()
    ctx.run_cli()

    await ctx.exit_event.wait()
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
