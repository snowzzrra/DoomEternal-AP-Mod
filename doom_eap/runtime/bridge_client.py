from doom_eap.runtime.materialization import authored_effects_allowed, reconciliation_session_block, reconciliation_runtime_block
from doom_eap.runtime.receipt_delivery import base_special_receipt_allowed
from doom_eap.runtime.physical_checks import PhysicalChecks
from doom_eap.runtime.task_supervision import SessionTasks
from doom_eap.runtime.level_ready import LevelReady
from doom_eap.runtime.location_setup import LocationSetup
from doom_eap.runtime.location_names import resolve_placement_records
from doom_eap.runtime.protocol_feed import ProtocolFeed, hints_key
import asyncio
import atexit
import csv
import ctypes
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
from doom_eap.contracts.materialization import MaterializationScope
from doom_eap.runtime.materialization import compile_automatic_plan
from doom_eap.runtime.death_observation import DeathObservation
from doom_eap.contracts.goal_policy import GoalPolicy
from doom_eap.contracts.check_observation import CheckObservation
from doom_eap.runtime.check_publication import CheckPublication
from doom_eap.runtime.goal_progress import GoalProgress
from doom_eap.runtime.publisher_dispatch import PublisherDispatch
from doom_eap.runtime.save_checks import SaveChecks, SaveCheckReadiness
from doom_eap.contracts.receipt_delivery import ReceiptIntent, ReceiptFeedbackFacts, ReceiptPlan, ReceiptPublicationScope
from doom_eap.runtime.receipt_delivery import ReceiptDelivery, compile_receipt_plan, requires_progressive_observation
from doom_eap.runtime.receipt_publication import ReceiptPublication, receipt_command_id
from doom_eap.runtime.save_check_observation import SaveCheckBinding, SaveCheckObservations
from doom_eap.runtime.deathlink_session import DeathLinkSession
from doom_eap.runtime.deathlink_publication import DeathLinkPublication, DEATHLINK_KILL_COALESCE_KEY
from doom_eap.runtime.ammo_refill import (
    AMMO_REFILL_ITEM_ID, AMMO_REFILL_CAPACITY, AmmoRefill, AmmoCommandScope, active_crucible, ammo_readiness,
)
from doom_eap.runtime.ammo_adapters import AmmoCommandPublication, AmmoStorage, AmmoRequestPump
from doom_eap.runtime.bootstrap import Bootstrap, BootstrapOwnership
from doom_eap.runtime.checked_visuals import CheckedVisuals
from doom_eap.runtime.fast_travel import FastTravel
from doom_eap.runtime.materialization_coordinator import MaterializationCoordinator
from doom_eap.runtime.reconciliation_publication import ReconciliationPublisher
from doom_eap.runtime import save_files
from doom_eap.runtime.save_records import read_unlockable_record
from doom_eap.runtime.save_observer import SaveObserver, SaveObserverBaselineStore
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from doom_eap.contracts.save_observation import (
    GameplaySaveEvidence, PrimarySaveSelection, expected_save_prefix_for_campaign,
)

from doom_eap.runtime.bootstrap_actions import (
    BOOTSTRAP_ACTIONS,
    BOOTSTRAP_REVISION,
)
from doom_eap.contracts.campaign_goal_contract import CAMPAIGN_GOAL_CONTRACT
from doom_eap.contracts.command_publication import (
    build_materialization_epoch,
    PLAYER_RUNTIME, MAP_ENTITY_SAFE, TRANSIENT_EFFECT,
    CHECKED_VISUAL_HIDE, FAST_TRAVEL_UNLOCK,
    MATERIALIZATION_LEASE_HEADER, MATERIALIZATION_LEASE_MARKER,
    queue_session_namespace, stable_spool_id, valid_materialization_epoch,
)
from doom_eap.runtime.command_spool import CommandSpool, discard_unclaimed_command
from doom_eap.runtime.client_state_store import ClientStateStore
from doom_eap.contracts.challenge_registry import (
    canonical_map_name,
    load_challenge_registry,
)
from doom_eap.runtime.deathlink_receive import DeathLinkReceiver
from doom_eap.contracts.foundation import (
    compile_item_delivery_plan,
    load_foundation_contracts,
    load_primitive_registry,
)
from doom_eap.content.item_classification import (
    ITEM_CLASSIFICATION_TRAP,
    load_item_classification_identity,
    normalize_network_classification,
    notification_style_for_item,
)
from doom_eap.contracts.item_contracts import DEFAULT_DEATH_LINK_MODE, start_inventory_eligible
from doom_eap.contracts.receipt_delivery import HISTORICAL_OWNERSHIP, NEW_RECEIPT, PRESENTATION_REPAIR, RECONCILIATION_REPAIR
from doom_eap.runtime.item_reconciliation import receipt_item_ids, progressive_receipt_stage, fresh_receipt_owned_count, ReceiptSession, processed_receipt_counts, project_receipt_history, record_processed_receipt, reset_receipt_history, validate_session_receipt_prefix, effective_ownership, AP_RECEIPT_FEEDBACK, CLIENT_STATE_VERSION, default_session_state, load_policy_registry, migrate_client_state, migrate_legacy_session_key, normalize_session_state, observe_received_items, receipt_history_fingerprint, receipt_identity, validate_receipt_history_prefix
from doom_eap.runtime.observer_lifecycle import RuntimeObservationLease, observer_registry_revision
from doom_eap.content.publisher_loader import (
    load_publisher_contracts,
)
from doom_eap.contracts.publisher_contracts import PublisherEngine, publisher_acknowledged
from doom_eap.runtime.publisher_runtime import quarantine_malformed_event, read_map_event
from doom_eap.content.automap_visual_registry import (
    index_automap_visual_registry,
    load_automap_visual_registry,
)
from doom_eap.runtime.rune_reconciliation import (
    RuneReconciliation,
    RuneNativeState,
)
from doom_eap.runtime.context_registry import (
    CONTEXT_BY_MAP,
    CONTEXT_BY_IDENTITY,
    GOAL_CAPABILITIES,
    SUPPORT_RUNE_IDS,
    classify_runtime_context,
    resolve_context_evidence,
    context_item_ids,
    evaluate_dlc_availability,
    validate_slot_contract,
)
from doom_eap.runtime.lifecycle import RuntimeLifecycle
from doom_eap.runtime.transient_files import TransientPublication, read_transient_runtime
from doom_eap.runtime.transient_effects import (
    TRANSIENT_EFFECTS,
    TransientEffectManager,
)

try:
    from .save_decrypt import decrypt, steam_id64
except ImportError:
    from doom_eap.runtime.save_decrypt import decrypt, steam_id64

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
    mod_file = (module_file or Path(__file__)).resolve()

    candidate = app_dir / "bridge_client.py"
    if candidate.is_file():
        sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()
        return candidate, sha256, f"mission-unified-{sha256[:12]}"

    if mod_file.is_file():
        sha256 = hashlib.sha256(mod_file.read_bytes()).hexdigest()
        return mod_file, sha256, f"mission-unified-{sha256[:12]}"

    for id_path in (app_dir / "bridge_identity.json", root / "data" / "bridge_identity.json"):
        if id_path.is_file():
            try:
                doc = json.loads(id_path.read_text(encoding="utf-8"))
                if isinstance(doc, dict) and "sha256" in doc:
                    sha = str(doc["sha256"])
                    rev = str(doc.get("revision", f"mission-unified-{sha[:12]}"))
                    file_ref = Path(sys.executable).resolve() if frozen else mod_file
                    return file_ref, sha, rev
            except Exception:
                pass

    content_id_path = root / "data" / "content_identity.json"
    if content_id_path.is_file():
        sha256 = hashlib.sha256(content_id_path.read_bytes()).hexdigest()
    else:
        sha256 = hashlib.sha256(b"doom-eternal-archipelago-bridge-runtime").hexdigest()

    file_ref = Path(sys.executable).resolve() if frozen else mod_file
    return file_ref, sha256, f"mission-unified-{sha256[:12]}"


BRIDGE_FILE, BRIDGE_SHA256, BRIDGE_REVISION = resolve_bridge_identity(
    APPLICATION_DIR, REPO_ROOT, Path(__file__).resolve()
)
_CONTENT_IDENTITY = json.loads(
    (REPO_ROOT / "data" / "content_identity.json").read_text(encoding="utf-8")
)
BRIDGE_PROTOCOL = _CONTENT_IDENTITY["bridge_protocol_version"]
TRANSITION_HANDLER = "unified"
GAME_NAME = _CONTENT_IDENTITY["game"]
# In-game Ammo Refill request channel: the player's DOOM bind for
# AP_USE_REFILL_CHARGE executes `condump AP_REFILL_REQUEST.txt`, and the bridge
# consumes that file from SAVE_GAMES_DIR as one refill request.
AMMO_REFILL_REQUEST_FILENAME = "AP_REFILL_REQUEST.txt"
AMMO_REFILL_REQUEST_RE = re.compile(r"^AP_REFILL_REQUEST(?:_(\d+))?\.txt$")


def doom_process_identity():
    """Return current game PID plus process-start identity when available."""
    executable = "doometernalx64vk.exe"
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {executable}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for row in csv.reader(result.stdout.splitlines()):
                if len(row) >= 2 and row[0].casefold() == executable:
                    pid = int(row[1].replace(",", ""))
                    try:
                        class _FileTime(ctypes.Structure):
                            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

                        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                        handle = kernel32.OpenProcess(0x1000, False, pid)
                        if handle:
                            created = _FileTime()
                            exited = _FileTime()
                            ok = kernel32.GetProcessTimes(
                                handle,
                                ctypes.byref(created),
                                ctypes.byref(exited),
                                ctypes.byref(_FileTime()),
                                ctypes.byref(_FileTime()),
                            )
                            kernel32.CloseHandle(handle)
                            if ok:
                                creation_ticks = (created.high << 32) | created.low
                                return f"windows:{pid}:{creation_ticks}"
                    except (AttributeError, OSError):
                        pass
                    return f"windows:{pid}"
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        return None

    for process_dir in Path("/proc").glob("[0-9]*"):
        try:
            comm = (process_dir / "comm").read_text(encoding="utf-8").strip()
            executable_path = os.path.basename(os.readlink(process_dir / "exe"))
            if executable not in {comm.casefold(), executable_path.casefold()}:
                continue
            stat = (process_dir / "stat").read_text(encoding="utf-8")
            fields = stat.rsplit(")", 1)[1].split()
            start_time = fields[19]
            return f"linux:{process_dir.name}:{start_time}"
        except (OSError, IndexError, UnicodeError):
            continue
    return None


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
DEATHLINK_MESSAGES = (
    "{player} didn't rip and tear enough.",
    "{player} was sent back to the Fortress.",
    "{player} picked a fight with Hell and lost.",
    "{player}'s ripping and tearing privileges were revoked.",
    "{player} didn't control the buttons they pressed.",
)
LAUNCHER_EVENTS_ENABLED = os.environ.get("DOOM_AP_LAUNCHER_EVENTS") == "1"
FAST_TRAVEL_MISSION_COMPLETE_IDS = {
    "e1m1_intro": 7770122, "e1m2_war": 7770123, "e1m3_cult": 7770124,
    "e1m4_boss": 7770162, "e2m1_nest": 7770210, "e2m2_base": 7770248,
    "e2m3_core": 7770289, "e2m4_boss": 7770290, "e3m1_slayer": 7770337,
    "e3m2_hell": 7770362, "e3m2_hell_b": 7770387, "e3m3_maykr": 7770411,
    "e3m4_boss": 7770414,
    "e4m1_rig": 7770432, "e4m2_swamp": 7770443, "e4m3_mcity": 7770457,
}


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













ENABLE_ITEM_NOTIFICATIONS = False
ITEM_DELIVERY_BATCH_SIZE = 16
try:
    _identity_path = APPLICATION_DIR / "bridge_identity.json"
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

    if executable.is_file() and base_dir.is_dir():
        return str(base_dir)

    raise ValueError(
        "Expected either the DOOM Eternal installation directory or its base "
        "directory.\n"
        f"Checked executable: {executable}\n"
        f"Checked base directory: {base_dir}\n"
        "Examples:\n"
        "  Windows: D:/SteamLibrary/steamapps/common/DOOMEternal\n"
        "  Windows: D:/SteamLibrary/steamapps/common/DOOMEternal/base\n"
        "  Linux: /path/to/steamapps/common/DOOMEternal\n"
        "  Linux: /path/to/steamapps/common/DOOMEternal/base"
    )


SAVE_GAMES_SELECTION: dict[str, object] | None = None


def normalize_save_games_dir(path):
    from doom_eap.launcher.launcher_platform import (
        doom_saved_games_base,
        select_saved_games_dir,
    )

    global SAVE_GAMES_SELECTION
    SAVE_GAMES_SELECTION = None
    selected = Path(path).expanduser()
    name = selected.name.lower()
    if name == "base" and selected.parent.name.lower() == "doometernal" and selected.parent.parent.name.lower() == "id software":
        probe = selected
    elif name == "doometernal":
        probe = selected / "base"
    elif name == "id software":
        probe = selected / "DOOMEternal" / "base"
    else:
        shaped = selected / "id Software" / "DOOMEternal" / "base"
        probe = shaped if shaped.is_dir() else selected
    game_root = None
    try:
        doom_base = globals().get("DOOM_BASE_DIR") or (config or {}).get("doom_base_dir")
        if doom_base:
            game_root = Path(doom_base).expanduser()
            if game_root.name.casefold() == "base":
                game_root = game_root.parent
    except (OSError, TypeError, ValueError, RuntimeError):
        game_root = None
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
            "Expected the DOOM Eternal save base directory, for example "
            "C:/Users/<user>/Saved Games/id Software/DOOMEternal/base. "
            f"Selection failed: {selection.reason}."
            + (f" Rejected: {rejected}." if rejected else "")
        )
    SAVE_GAMES_SELECTION = {
        "selected_path": str(selection.path),
        "source": selection.source,
        "reason": selection.reason,
        "repaired": selection.repaired,
        "previous_path": selection.previous_path,
    }
    return str(selection.path)


config = load_config()
SAVE_GAMES_SELECTION_SOURCE = "unresolved"
SAVE_GAMES_SELECTION_REASON = "configuration has not been resolved"

AP_SOURCE_PATH = os.environ.get("ARCHIPELAGO_SOURCE")
if AP_SOURCE_PATH:
    sys.path.insert(0, os.path.abspath(AP_SOURCE_PATH))

import colorama  # noqa: E402
import Utils  # noqa: E402
import CommonClient as APCommonClient  # noqa: E402
from CommonClient import (  # noqa: E402
    ClientCommandProcessor,
    CommonContext,
    get_base_parser,
    gui_enabled,
    server_loop,
)
from doom_eap.runtime.protocol_feed_format import (  # noqa: E402
    ARCHIPELAGO_EVENT_PLAIN_LIMIT,
    ProtocolNames,
    emit_hints,
    format_archipelago_event,
    _bounded_event_text,
)
from NetUtils import ClientStatus  # noqa: E402

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
            "DOOMEternalx64vk.exe must be in DOOMEternal and base "
            "folder must exist."
        ),
    )

    SAVE_GAMES_DIR = prompt_for_dir(
        "Select DOOM Saved Games Directory (.../Saved Games/id Software/DOOMEternal/base)",
        lambda p: normalize_save_games_dir(p) if p else None,
        "Could not find the DOOM Eternal save base directory."
    )
    SAVE_GAMES_SELECTION_SOURCE = "interactive_selection"
    SAVE_GAMES_SELECTION_REASON = "path selected during client setup"

    config["doom_base_dir"] = DOOM_BASE_DIR
    config["save_games_dir"] = SAVE_GAMES_DIR
    save_config()
    if sys.stdin and sys.stdin.isatty():
        print("Configuration saved to ap_config.json!\n")

QUEUE_DIR = os.path.join(DOOM_BASE_DIR, "ap_queue")
TAG_INVENTORY_SETTLE_SECONDS = 1.0
RPC_GATE_PATH = os.path.join(DOOM_BASE_DIR, "ap_rpc_enabled")
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
ITEM_MAPPING_REVISION = 7
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
REVISION_TWO_SUIT_IDS = {7770021}
REVISION_FOUR_FLAME_BELCH_IDS = {7770012}
REVISION_FIVE_EQUIPMENT_LAUNCHER_IDS = {7770011, 7770013}


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
    """Start a fresh production log for an active client session."""
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
    logger.info("QUEUE_DIR=%s", QUEUE_DIR)
    logger.info("RPC_GATE_PATH=%s", RPC_GATE_PATH)


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


DEATH_PROBE = APPLICATION_DIR / "save_death_probe.exe"
DEATH_PROBE_RUNTIME = APPLICATION_DIR / f".death-probe-{os.getpid()}"


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


def primary_save_candidates(filename="game_duration.dat", slot_prefix=None):
    """Return valid primary slots newest-first."""
    return save_files.primary_save_candidates(
        STEAM_REMOTE_DIR, STEAM_ID3, filename, slot_prefix,
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
    return save_files.read_gameplay_save_evidence(path or GAMEPLAY_SAVE_EVIDENCE_PATH)


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


STICKY_UNLOCKABLE = b"weapon_mastery/shotgun/sticky_bomb"


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


def read_checkpoint_deaths(unpacked: bytes) -> int | None:
    marker = b"numCheckpointDeaths\x01"
    idx = unpacked.find(marker)
    if idx == -1 or idx + len(marker) >= len(unpacked):
        return None
    return int(unpacked[idx + len(marker)])


def probe_game_duration(path):
    unpacked, result_code, stdout = save_files.unpack_game_duration(
        path, steam_id=STEAM_ID3, runtime_directory=DEATH_PROBE_RUNTIME,
        probe_path=DEATH_PROBE, oodle_path=OODLE_DLL, compat_data=DEATH_PROBE_COMPAT_DATA,
        proton_path=PROTON_PATH, steam_install=STEAM_INSTALL, host_exec=DISTROBOX_HOST_EXEC,
    )
    mastery_records = read_weapon_mastery_records(unpacked)
    raw_deaths = read_checkpoint_deaths(unpacked)
    if raw_deaths is None and stdout:
        m = re.search(r"numCheckpointDeaths=(\d+)", stdout)
        if m:
            raw_deaths = int(m.group(1))
    snapshot = {
        "mastery_records": mastery_records,
        "mission_challenge_records": read_mission_challenge_records(unpacked),
        "raw_num_checkpoint_deaths": raw_deaths if raw_deaths is not None else (1 if result_code == 20 else 0),
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
    snapshot["checkpoint_death"] = snapshot["raw_num_checkpoint_deaths"] > 0
    return snapshot


def probe_checkpoint_death(path):
    """Compatibility wrapper used by focused DeathLink tests."""
    return probe_game_duration(path)["checkpoint_death"]


# Load item definitions
ITEMS_FILE = REPO_ROOT / "data" / "items.json"
with open(ITEMS_FILE, encoding="utf-8") as f:
    # Keys in JSON are strings, convert them to ints
    _raw_items = json.load(f)
    ITEM_ID_TO_COMMAND = {int(k): v for k, v in _raw_items.items()}
ITEM_REPLAY_POLICIES_FILE = REPO_ROOT / "data" / "item_replay_policies.json"
ITEM_REPLAY_POLICIES = load_policy_registry(
    Path(ITEM_REPLAY_POLICIES_FILE), ITEM_ID_TO_COMMAND
)
ITEM_CLASSIFICATIONS_FILE = REPO_ROOT / "data" / "item_classifications.json"
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

RUNTIME_LOCATIONS_FILE = REPO_ROOT / "data" / "runtime_locations.json"
with open(RUNTIME_LOCATIONS_FILE, encoding="utf-8") as f:
    RUNTIME_LOCATIONS = json.load(f)
EXULTIA_COMPLETE_LOCATION = RUNTIME_LOCATIONS[
    "Exultia - Mission Complete"
]
CULTIST_BASE_COMPLETE_LOCATION = RUNTIME_LOCATIONS[
    "Cultist Base - Mission Complete"
]
DOOM_HUNTER_BASE_COMPLETE_LOCATION = RUNTIME_LOCATIONS[
    "Doom Hunter Base - Mission Complete"
]


def should_materialize_dash(
    randomize_dash,
    received,
    checked_locations,
    server_checked_ready,
):
    if randomize_dash:
        return 7770015 in received
    return bool(
        server_checked_ready
        and EXULTIA_COMPLETE_LOCATION in checked_locations
    )


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
def _validated_catalog_maps(active_maps):
    """Validate and canonicalize runtime map identity from packaged contracts."""
    if not isinstance(active_maps, dict) or not active_maps:
        raise ValueError("foundation active_maps must be a non-empty object")
    validated = {}
    seen_runtime_maps = set()
    for raw_map_key, raw_runtime_map in active_maps.items():
        if not isinstance(raw_map_key, str) or not raw_map_key.strip():
            raise ValueError("foundation active_maps contains an invalid map key")
        if raw_map_key != raw_map_key.strip():
            raise ValueError(f"foundation active_maps map key is not canonical: {raw_map_key!r}")
        if not isinstance(raw_runtime_map, str) or not raw_runtime_map.strip():
            raise ValueError(f"foundation active_maps[{raw_map_key!r}] has an invalid runtime map")
        runtime_map = canonical_map_name(raw_runtime_map)
        if not runtime_map:
            raise ValueError(f"foundation active_maps[{raw_map_key!r}] has an empty canonical runtime map")
        if runtime_map in seen_runtime_maps:
            raise ValueError(f"foundation active_maps contains duplicate runtime map: {runtime_map}")
        validated[raw_map_key] = runtime_map
        seen_runtime_maps.add(runtime_map)
    return validated


KNOWN_CATALOG_MAPS = _validated_catalog_maps(
    load_foundation_contracts().get("active_maps")
)


def _catalog_map_key(runtime_map):
    runtime_map = canonical_map_name(runtime_map)
    matches = [
        map_key
        for map_key, catalog_runtime_map in KNOWN_CATALOG_MAPS.items()
        if catalog_runtime_map == runtime_map
    ]
    return matches[0] if len(matches) == 1 else None


FAST_TRAVEL_MAP_KEYS = frozenset(
    map_key for map_key, runtime_map in KNOWN_CATALOG_MAPS.items()
    if map_key != "hub" and runtime_map in {
        "game/sp/e1m1_intro/e1m1_intro",
        "game/sp/e1m2_battle/e1m2_battle",
        "game/sp/e1m3_cult/e1m3_cult",
        "game/sp/e1m4_boss/e1m4_boss",
        "game/sp/e2m1_nest/e2m1_nest",
        "game/sp/e2m2_base/e2m2_base",
        "game/sp/e2m3_core/e2m3_core",
        "game/sp/e2m4_boss/e2m4_boss",
        "game/sp/e3m1_slayer/e3m1_slayer",
        "game/sp/e3m2_hell/e3m2_hell",
        "game/sp/e3m2_hell_b/e3m2_hell_b",
        "game/sp/e3m3_maykr/e3m3_maykr",
        "game/sp/e3m4_boss/e3m4_boss",
        "game/dlc/e4m1_rig/e4m1_rig",
        "game/dlc/e4m2_swamp/e4m2_swamp",
        "game/dlc/e4m3_mcity/e4m3_mcity",
    }
)

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
MANIFESTS_DIR = REPO_ROOT / "manifests"
if os.path.exists(MANIFESTS_DIR):
    for filename in os.listdir(MANIFESTS_DIR):
        if filename.endswith(".json"):
            with open(os.path.join(MANIFESTS_DIR, filename), encoding="utf-8") as f:
                manifest_data = json.load(f)
                DECL_TO_LOCATION.update(manifest_data)

AUTOMAP_VISUAL_REGISTRY = load_automap_visual_registry()
AUTOMAP_VISUALS_BY_MAP = index_automap_visual_registry(AUTOMAP_VISUAL_REGISTRY)

poll_counter = 0

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
    fields.setdefault("bridge_revision", globals().get("BRIDGE_REVISION"))
    fields.setdefault("protocol_version", globals().get("BRIDGE_PROTOCOL"))
    fields.setdefault("mapping_revision", globals().get("ITEM_MAPPING_REVISION"))
    log_delivery_event(event, **fields)


def command_spool_exists(command_id, state_key=None, room_scoped=True):
    return command_spool().exists(command_id, state_key, room_scoped)


def command_spool():
    """Compose the publication adapter without making it discover bridge state."""
    return CommandSpool(
        QUEUE_DIR, arm_rpc=set_rpc_execution, log_delivery=log_delivery_event, logger=logger,
    )


def active_queue_session_namespace():
    """Read native queue authority published for current AP room."""
    return command_spool().active_namespace()


def publish_materialization_lease(epoch):
    """Atomically publish current gameplay materialization for native queue use."""
    marker = Path(QUEUE_DIR) / MATERIALIZATION_LEASE_MARKER
    if not valid_materialization_epoch(epoch):
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            logger.error("[Queue] Could not clear materialization lease: %s", error)
        return False

    temporary = None
    try:
        os.makedirs(QUEUE_DIR, exist_ok=True)
        contents = f"{MATERIALIZATION_LEASE_HEADER} {epoch}\n"
        if marker.is_file() and marker.read_text(encoding="ascii") == contents:
            return True
        temporary = marker.with_name(f".{MATERIALIZATION_LEASE_MARKER}-{uuid.uuid4().hex}.tmp")
        with temporary.open("x", encoding="ascii", newline="\n") as file:
            file.write(contents)
            file.flush()
            os.fsync(file.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, marker)
                break
            except (PermissionError, OSError):
                if attempt == 4:
                    raise
                time.sleep(0.01 * (attempt + 1))
        logger.info("[Queue] Published materialization lease: %s", epoch)
        return True
    except (OSError, UnicodeError) as error:
        logger.error("[Queue] Could not publish materialization lease: %s", error)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        return False


def room_scoped_command_id(command_id, state_key=None):
    """Use native receipt gate namespace for every room-bound spool job."""
    return command_spool().scoped_id(command_id, state_key)


def quarantine_incompatible_receipt_jobs(state_key):
    """Hold bridge-owned queued receipts not belonging to active AP identity.

    `.processing` is native-owned and deliberately untouched. Native recovery is
    the only owner of that suffix.
    """
    namespace = queue_session_namespace(state_key)
    if namespace is None:
        return
    os.makedirs(QUEUE_DIR, exist_ok=True)
    expected = re.compile(rf"^recv-{re.escape(namespace)}-.*\.cmd$")
    for source in sorted(Path(QUEUE_DIR).glob("recv-*.cmd")):
        if expected.fullmatch(source.name):
            continue
        target = source.with_suffix(".held")
        if target.exists():
            target = source.with_name(f"{source.name}.held")
        try:
            os.replace(source, target)
            logger.warning("[Queue] Held foreign or legacy receipt job: %s", source.name)
        except OSError as error:
            logger.error("[Queue] Could not hold receipt job %s: %s", source, error)


def ensure_queue_session_namespace(state_key):
    """Keep active room identity available for native-owned queue recovery."""
    namespace = queue_session_namespace(state_key)
    if namespace is None:
        return False
    marker = Path(QUEUE_DIR) / "active_session_namespace"
    try:
        current = marker.read_text(encoding="ascii")
    except FileNotFoundError:
        current = None
    except (OSError, UnicodeError) as error:
        logger.warning("[Queue] Could not read session namespace marker: %s", error)
        current = None

    if current is not None and current.strip() == namespace:
        return True

    try:
        os.makedirs(QUEUE_DIR, exist_ok=True)
    except OSError as error:
        logger.error("[Queue] Could not create queue directory for session namespace: %s", error)
        return False

    temporary = Path(QUEUE_DIR) / f".active_session_namespace-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="ascii", newline="\n") as file:
            file.write(namespace + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, marker)
        logger.info("[Queue] Refreshed active session namespace: %s", namespace)
        return True
    except OSError as error:
        logger.error("[Queue] Could not publish session namespace marker: %s", error)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        return False


def invalidate_queue_session_namespace(reason="authority_reset"):
    """Remove durable queue authority until a connected identity is proven."""
    marker = Path(QUEUE_DIR) / "active_session_namespace"
    try:
        marker.unlink()
        logger.info("[Queue] Invalidated active session namespace: %s", reason)
    except FileNotFoundError:
        pass
    except OSError as error:
        logger.error(
            "[Queue] Could not invalidate session namespace marker (%s): %s",
            reason,
            error,
        )


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


def deathlink_publication():
    return DeathLinkPublication(send_command, command_spool_exists, discard_queued_coalesced_command)


def reconciliation_publisher():
    return ReconciliationPublisher(send_command, command_spool_exists, logger)


def send_command(
    cmd,
    coalesce_key=None,
    arm_rpc=True,
    already_queued_ok=False,
    delivery_fields=None,
    state_key=None,
    room_scoped=True,
    materialization_lease=None,
    execution_class=PLAYER_RUNTIME,
    operation=None,
    diagnostic=False,
    transient_scope=None,
):
    """Compatibility port: acceptance is not native execution or gameplay proof."""
    return command_spool().publish(
        cmd, coalesce_key=coalesce_key, arm_rpc=arm_rpc,
        already_queued_ok=already_queued_ok, delivery_fields=delivery_fields,
        state_key=state_key, room_scoped=room_scoped,
        materialization_lease=materialization_lease, execution_class=execution_class,
        operation=operation, diagnostic=diagnostic, transient_scope=transient_scope,
    ).accepted


def expected_item_job_activation(item_id, command_index):
    definition = ITEM_ID_TO_COMMAND.get(item_id)
    try:
        if isinstance(definition, dict) and definition.get("type") in {
            "progressive_perk", "progressive_item",
        }:
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


def migrate_direct_item_command_jobs(state_key):
    """Rewrite old queued item jobs to map-side RPC activations.

    Only .cmd belongs to the bridge. A .processing file is owned by the native
    client's in-memory queue and remains under queue ownership.
    Native startup recovery handles interrupted .processing jobs exactly once.
    """
    try:
        os.makedirs(QUEUE_DIR, exist_ok=True)
    except Exception as error:
        logger.error(f"[Queue] Could not create queue directory for migration: {error}")
        return

    namespace = queue_session_namespace(state_key)
    if namespace is None:
        return
    for pattern in (f"recv-{namespace}-*.cmd",):
        for source_path in sorted(glob.glob(os.path.join(QUEUE_DIR, pattern))):
            path = Path(source_path)
            try:
                command = path.read_text(encoding="utf-8").strip()
            except Exception as error:
                logger.error(f"[Queue] Could not read queued job for migration: {path}: {error}")
                continue
            match = re.match(
                rf"recv-{re.escape(namespace)}-(\d+)-item-(\d+)-cmd-(\d+)\.(cmd|processing)$",
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

            command_id = f"recv-{namespace}-{receive_index:06d}-item-{item_id}-cmd-{command_index:02d}"
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


def ammo_refill_request_files(directory=None):
    """Return only DoomEAP refill request files in safe creation order."""
    root = directory if directory is not None else globals().get("SAVE_GAMES_DIR")
    if not root:
        return []
    candidates = glob.glob(os.path.join(str(root), "AP_REFILL_REQUEST*.txt"))
    valid_files = []
    for path in set(candidates):
        match = AMMO_REFILL_REQUEST_RE.fullmatch(os.path.basename(path))
        if match is None:
            continue
        try:
            mtime_ns = os.stat(path).st_mtime_ns
        except OSError:
            continue
        suffix = int(match.group(1) or 0)
        valid_files.append((mtime_ns, suffix, os.path.basename(path), path))
    valid_files.sort()
    return [path for _, _, _, path in valid_files]


def cleanup_ammo_refill_request_files(directory=None):
    """Remove only stale DoomEAP Ammo Refill request files."""
    removed = 0
    for path in ammo_refill_request_files(directory):
        try:
            os.remove(path)
        except FileNotFoundError:
            continue
        except OSError as error:
            logger.warning("[Ammo Refill] Could not remove stale request %s: %s", path, error)
        else:
            removed += 1
    return removed


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

    return RuntimeLifecycle.parse_authored_marker(
        content, path, mtime_ns, KNOWN_CATALOG_MAPS, CONTEXT_BY_MAP,
    )


def cleanup_active_map_marker_file(path):
    """Remove a consumed or stale active-map condump file."""
    if not path:
        return False
    basename = os.path.basename(path)
    if not re.match(rf"^{ACTIVE_MAP_MARKER_PREFIX}(?:_.*)?\.txt$", basename):
        return False
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError as error:
        logger.warning(
            f"[MAP] Could not remove active map marker {basename}: {error}"
        )
        return False


def cleanup_active_map_markers(preserve_path=None):
    """Remove consumed or stale active-map condump files to prevent progressive suffixed names."""
    pattern = os.path.join(INV_DUMP_DIR, f"{ACTIVE_MAP_MARKER_PREFIX}*.txt")
    for path in glob.glob(pattern):
        if preserve_path and os.path.abspath(path) == os.path.abspath(preserve_path):
            continue
        basename = os.path.basename(path)
        if not re.match(rf"^{ACTIVE_MAP_MARKER_PREFIX}(?:_.*)?\.txt$", basename):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                content = f.read(256)
            if "AP_ACTIVE_MAP_V1" not in content and "echo AP_ACTIVE_MAP_V1" not in content:
                continue
        except OSError:
            continue
        cleanup_active_map_marker_file(path)


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
        room_scoped=False,
    )


def request_support_condump():
    """Queue only bounded support condump operation through diagnostic path."""
    return send_command(
        "condump AP_SUPPORT_FILE.txt",
        coalesce_key="support-condump",
        arm_rpc=False,
        room_scoped=False,
        diagnostic=True,
    )


def discard_queued_coalesced_command(coalesce_key, state_key=None):
    """Cancel only an unclaimed command; consumer owns every .processing file."""
    discard_unclaimed_command(
        Path(QUEUE_DIR), room_scoped_command_id(coalesce_key, state_key)
    )


def set_rpc_execution(enabled: bool) -> bool:
    if enabled:
        temporary_path = f"{RPC_GATE_PATH}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temporary_path, "w", encoding="utf-8") as f:
                f.write("enabled\n")
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(5):
                try:
                    os.replace(temporary_path, RPC_GATE_PATH)
                    return True
                except (PermissionError, OSError):
                    if attempt == 4:
                        raise
                    time.sleep(0.01 * (attempt + 1))
        finally:
            if os.path.exists(temporary_path):
                try:
                    os.remove(temporary_path)
                except OSError:
                    pass
        return True
    else:
        for attempt in range(5):
            try:
                os.remove(RPC_GATE_PATH)
                return True
            except FileNotFoundError:
                return True
            except (PermissionError, OSError):
                if attempt == 4:
                    if os.path.exists(RPC_GATE_PATH):
                        raise
                    return True
                time.sleep(0.01 * (attempt + 1))
        return not os.path.exists(RPC_GATE_PATH)

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
    return save_files.read_game_details_for_selection(selected, STEAM_ID3, logger)


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
        rune_plan, rune_error = self.ctx.reconcile_owned_runes("manual", force=True)
        if rune_error:
            self.output(f"Rune reconcile: {rune_error}")
        else:
            self.output(
                f"Rune reconcile: status={rune_plan.status} "
                f"noop={len(rune_plan.noops)} candidates={len(rune_plan.repairs)}"
            )

    def _cmd_doom_rune_diag(self):
        """Show AP Rune ownership and distinct native Rune state surfaces."""
        for line in self.ctx.observe_rune_diagnostic_lines():
            self.output(line)

    def _cmd_doom_context_diag(self):
        """Show bounded campaign-context and DLC evidence."""
        for key, value in self.ctx.context_support_report().items():
            self.output(f"{key}={json.dumps(value, sort_keys=True)}")

    def _cmd_doom_rpc_on(self):
        """Arm RPC commands; the native memory gate still enforces safe gameplay."""
        try:
            set_rpc_execution(True)
            self.output(
                "RPC execution armed manually. The native memory gate opens only "
                "during safe gameplay."
            )
        except Exception as error:
            logger.error("[RPC] Failed to arm RPC execution: %s", error)
            self.output(f"Failed to arm RPC execution: {error}")

    def _cmd_doom_rpc_off(self):
        """Disarm all RPC commands until explicitly or automatically re-armed."""
        try:
            set_rpc_execution(False)
            self.output("RPC execution paused. Queued commands will be preserved.")
        except Exception as error:
            logger.error("[RPC] Failed to pause RPC execution: %s", error)
            self.output(f"Failed to pause RPC execution: {error}")

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

    def _cmd_doom_deathlink_diag(self):
        """Show bounded DeathLink receive evidence and active policy."""
        receiver = self.ctx.deathlink
        self.output(
            f"DeathLink mode={self.ctx.death_link_mode} enabled={self.ctx.death_link_enabled} "
            f"policy={'single_dispatch' if receiver.mode == 'soft' else 'retry_until_confirmed'}"
        )
        for entry in receiver.instrumentation()[-8:]:
            self.output(
                "DeathLink event={event_id} state={state} detail={detail} "
                "attempts={attempts} deliveries={deliveries}".format(**entry)
            )

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
        """Activate a registered map entrypoint."""
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
        """Archive pending test jobs for diagnostics."""
        if confirmation != "--confirm":
            self.output("Usage: /doom_test_discard --confirm")
            return
        discarded = 0
        for source in dev_job_paths():
            target = source.with_name(source.name + ".discarded")
            os.replace(source, target)
            discarded += 1
        self.output(f"Archived {discarded} dev job(s).")

GOAL_POLICY = GoalPolicy(DOOM_LOCATION_NAMES, DLC_MISSION_PREFIXES, GOAL_CAPABILITIES, GOAL_ENDPOINT_LOCATION_IDS, GOAL_REQUIREMENT_SUFFIXES)


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
        self._item_delivery_lock = asyncio.Lock()
        self._item_delivery_task = None
        self._item_delivery_wakeup = False
        self._item_delivery_waiting_for_state = False
        self.receipt_session = ReceiptSession()
        self._queue_session_authoritative = False
        self.ammo = AmmoRefill(
            AmmoCommandPublication(send_command, discard_queued_coalesced_command, rpc_execution_enabled, set_rpc_execution, logger),
            AmmoStorage(self.send_msgs), lambda *args, **kwargs: emit_launcher_event(*args, **kwargs), logger,
        )
        self.ammo_requests = AmmoRequestPump(self.ammo, self.request_ammo_refill, self.exit_event, logger)
        invalidate_queue_session_namespace("bridge_start")
        self.tracker_alive = False
        self.tracker_restart_count = 0
        self.last_tracker_error = None
        self.last_heartbeat_timestamp = None
        self.item_state_ready = False
        self.reconnect_resync_attempted = False
        self.client_state = {"version": CLIENT_STATE_VERSION, "sessions": {}}
        self.state_key = ""
        self.base_directory = DOOM_BASE_DIR
        self.transient_effect_manager = TransientEffectManager(TransientPublication(self.base_directory, send_command), os.getpid())
        self.session_state = {}
        self.goals = GoalProgress(CAMPAIGN_GOAL_CONTRACT["runtime_map"], CULTIST_BASE_MAP, logger)
        self.publisher_dispatch = PublisherDispatch(PUBLISHERS, self.goals, logger)
        self.physical_checks = PhysicalChecks(logger)
        self.level_ready = LevelReady(logger)
        self.session_tasks = SessionTasks()
        self.location_setup = LocationSetup(logger)
        self.protocol_feed = ProtocolFeed()
        self.receipt_delivery = ReceiptDelivery(logger)
        self.save_checks = SaveChecks(WEAPON_MASTERY_BY_UNLOCKABLE, MISSION_CHALLENGE_BY_UNLOCKABLE, MISSION_CHALLENGE_RUNTIME_MAP_BY_UNLOCKABLE, ALL_MISSION_CHALLENGES_ENTRIES, logger)
        self.death_observer = DeathObservation(logger)
        self.save_observer = SaveObserver()
        self.runtime_observation_lease = RuntimeObservationLease()
        self.save_observer.clear_mission_select()
        self.last_observer_lease_block = None
        # Pending lethal commands stay process-local. Seen event identities persist per
        # room so reconnect/transport replay cannot create a second logical event.
        self.deathlink = DeathLinkSession(DeathLinkReceiver(
            wait_timeout=DEATHLINK_RECEIVE_TIMEOUT,
            confirm_timeout=DEATHLINK_CONFIRM_TIMEOUT,
            retry_interval=DEATHLINK_KILL_INTERVAL,
            total_timeout=DEATHLINK_TOTAL_TIMEOUT,
            late_suppression_grace=DEATHLINK_LATE_SUPPRESSION_GRACE,
            max_attempts=DEATHLINK_MAX_ATTEMPTS,
            mode=DEFAULT_DEATH_LINK_MODE,
        ), DEATHLINK_MESSAGES, emit_launcher_event, logger)
        self.room_seed_name = None
        self._connected_slot_data = {}
        self.runtime_lifecycle = RuntimeLifecycle()
        self.dlc_evidence = evaluate_dlc_availability(None)
        self.materialization = MaterializationCoordinator(logger)
        self.runes = RuneReconciliation(logger)
        self.bootstrap = Bootstrap(logger)
        self.checked_visuals = CheckedVisuals(KNOWN_CATALOG_MAPS, AUTOMAP_VISUALS_BY_MAP, uuid.uuid4().hex[:8], logger)
        self.server_checked_locations_ready = False
        self.fast_travel = FastTravel(KNOWN_CATALOG_MAPS, FAST_TRAVEL_MAP_KEYS, FAST_TRAVEL_MISSION_COMPLETE_IDS, logger)
        self._launcher_connection_failure_reported = False
        self._room_session_established = False
        self._launcher_connection_loss_reported = False

    def on_print_json(self, args: dict):
        try:
            super().on_print_json(args)
        except Exception:
            logger.exception("[Bridge] Archipelago PrintJSON logging failed")
        try:
            event = format_archipelago_event(self.protocol_names(), args)
            if not self.protocol_feed.consume_echo(event):
                emit_launcher_event("archipelago", **event)
        except Exception:
            logger.exception("[Bridge] Archipelago PrintJSON event formatting failed")

    async def send_launcher_chat(self, text: str) -> None:
        """Send launcher text through CommonClient's canonical Say path."""
        if not self.server or not self.server.socket.open or self.server.socket.closed:
            raise RuntimeError("Archipelago connection is unavailable")
        accepted = self.on_user_say(text)
        if accepted is None:
            raise RuntimeError("Archipelago rejected message")
        self.protocol_feed.record_echo(accepted)
        await self.send_msgs([{"cmd": "Say", "text": accepted}])


    def _launcher_hints_key(self):
        return hints_key(self.team, self.slot)

    def protocol_names(self):
        return ProtocolNames(self.item_names, self.location_names, dict(self.player_names), self.slot_concerns_self)


    def _emit_launcher_hints(self, update_kind="DATA_RECEIVED"):
        key = self._launcher_hints_key()
        if key is not None:
            emit_hints(key, self.stored_data.get(key, []), self.protocol_names(), emit_launcher_event, logger, update_kind)


    def reset_queue_session_authority(self, reason):
        self.runtime_lifecycle.invalidate_work()
        self.session_tasks.invalidate()
        self.physical_checks.invalidate()
        self.level_ready.invalidate()
        self.location_setup.invalidate()
        self.protocol_feed.invalidate()
        self.ammo_requests.reset()
        self.ammo.invalidate(reason)
        self.deathlink.invalidate_outbound()
        self.goals.invalidate_publication()
        self.publisher_dispatch.invalidate()
        self.save_checks.invalidate()
        self._queue_session_authoritative = False
        manager = getattr(self, "transient_effect_manager", None)
        if manager is not None:
            self.reset_transient_effects(reason)
        invalidate_queue_session_namespace(reason)

    def _report_launcher_connection_failure(
        self, message, *, code="connection_failed", reason_codes=None,
        technical_message=None,
    ):
        if (
            not LAUNCHER_EVENTS_ENABLED
            or self._launcher_connection_failure_reported
        ):
            return
        self._launcher_connection_failure_reported = True
        emit_launcher_event(
            "error",
            code=code,
            message=message,
            technical_message=technical_message or message,
            reason_codes=list(reason_codes or ()),
        )
        self.disconnected_intentionally = True
        self.cancel_autoreconnect()
        self.exit_event.set()

    @property
    def cached_map_identity(self):
        return self.runtime_lifecycle.map_identity.cached_marker

    @property
    def pending_map_identity(self):
        return self.runtime_lifecycle.map_identity.pending_marker

    @property
    def current_map_name(self):
        return self.runtime_lifecycle.map_identity.current_map

    @property
    def published_materialization_lease(self):
        return self.runtime_lifecycle.map_epochs.published_materialization_lease

    @property
    def native_gameplay_epoch(self):
        return self.runtime_lifecycle.map_epochs.native_gameplay_epoch

    @property
    def last_accepted_marker_mtime(self):
        return self.runtime_lifecycle.map_epochs.accepted_marker_mtime

    @property
    def last_accepted_map_evidence_epoch(self):
        return self.runtime_lifecycle.map_epochs.accepted_marker_evidence_epoch

    @property
    def context_identity(self):
        return self.runtime_lifecycle.snapshot.identity

    @property
    def context_campaign(self):
        return self.runtime_lifecycle.snapshot.campaign

    @property
    def context_capabilities(self):
        return self.runtime_lifecycle.snapshot.capabilities

    @property
    def pending_context_transition(self):
        return self.runtime_lifecycle.snapshot.pending_transition

    def _refresh_runtime_context(self, slot_data, evidence=None):
        """Bind exact runtime context without consulting location projections."""
        use_dlc = bool(slot_data.get("use_dlc_content"))
        self.dlc_evidence = evaluate_dlc_availability(DOOM_BASE_DIR if use_dlc else None)
        base_maps = load_foundation_contracts()["active_maps"].values()
        marker = getattr(self, "cached_map_identity", None) or {}
        evidence_map = getattr(evidence, "map_name", "")
        resolution = resolve_context_evidence(
            marker.get("runtime_map", ""), evidence_map,
            materialization_suspended=bool(marker.get("materialization_suspended")),
            base_maps=base_maps,
        )
        context = resolution.context
        source = resolution.source
        if resolution.rejection is not None:
            self._record_context_evidence_rejection(*resolution.rejection)
        if context is None:
            return CONTEXT_BY_IDENTITY.get(getattr(self, "context_identity", "unknown"))

        transition = self.runtime_lifecycle.bind_context(context)
        if transition is not None:
            self.reset_transient_effects("context_transition")
            if self.runtime_lifecycle.record_transition(transition, context.identity):
                logger.info(
                    "CONTEXT_TRANSITION previous=%s current=%s identity=%s source=%s status=detected",
                    transition[0], transition[1], context.identity, source,
                )
        return context

    def _record_context_evidence_rejection(self, reason, marker_identity, evidence_identity):
        result = self.runtime_lifecycle.record_evidence_rejection(reason, marker_identity, evidence_identity)
        if result is not None:
            logger.warning("CONTEXT_EVIDENCE_REJECTED reason=%s marker=%s evidence=%s count=%s", *result)

    def _active_materialization_lease(self, context=None):
        marker = self.cached_map_identity or {}
        marker_context = classify_runtime_context(marker.get("runtime_map", ""))
        return self.runtime_lifecycle.materialization_lease(
            getattr(marker_context, "campaign", None), getattr(context, "campaign", None),
        )

    def authored_map_runtime_ready(self, evidence=None):
        evidence = evidence or read_gameplay_save_evidence()
        marker = self.cached_map_identity or {}
        context = classify_runtime_context(marker.get("runtime_map", ""))
        lease = self._active_materialization_lease(context)
        socket = getattr(getattr(self, "server", None), "socket", None)
        observation = self.runtime_observation_lease
        return authored_effects_allowed(
            evidence=evidence, campaign=getattr(context, "campaign", None),
            epoch=marker.get("gameplay_epoch"), lease=lease,
            process_running=bool(lease and observation.process_probe()),
            item_ready=self.item_state_ready, room_identity=self.get_ap_state_key(),
            connected=socket is not None and not socket.closed,
            queue_authoritative=self._queue_session_authoritative,
        )

    def runtime_effects_ready(self, evidence=None):
        """Canonical admission predicate for map-scoped runtime effects."""
        evidence = evidence or read_gameplay_save_evidence()
        marker = getattr(self, "cached_map_identity", None)
        context = classify_runtime_context(
            marker.get("runtime_map", "") if isinstance(marker, Mapping) else ""
        )
        if context is not None and context.campaign != "Base":
            return self.authored_map_runtime_ready(evidence)
        return bool(
            evidence is not None
            and evidence.state == "gameplay"
            and evidence.native_safe
            and self.has_authoritative_save_proof()
        )

    def _observe_transient_runtime(self):
        evidence = read_gameplay_save_evidence()
        return read_transient_runtime(
            self.base_directory, self.state_key,
            evidence is not None and evidence.state == "gameplay" and evidence.native_safe,
        )

    def reset_transient_effects(self, reason):
        self.transient_effect_manager.reset(reason, self._observe_transient_runtime())

    @property
    def context_materialization_status(self):
        return self.materialization.snapshot.status

    @property
    def context_materialization_mode(self):
        return self.materialization.snapshot.mode

    @property
    def context_materialization_block(self):
        return self.materialization.snapshot.block

    def context_support_report(self):
        marker = getattr(self, "cached_map_identity", None) or {}
        materialization = self.session_state.get("context_materialization", {})
        return {
            "context_identity": self.context_identity,
            "campaign": self.context_campaign,
            "active_map_key": marker.get("map_key"),
            "active_runtime_map": marker.get("runtime_map"),
            "capabilities": sorted(self.context_capabilities),
            "use_dlc_content": bool(getattr(self, "_connected_slot_data", {}).get("use_dlc_content")),
            "dlc": self.dlc_evidence.report(),
            "materialization": self.context_materialization_status,
            "materialization_mode": self.context_materialization_mode,
            "materialization_block": self.context_materialization_block,
            "special_stage": materialization.get("special_stage", 0),
        }

    def _trigger_live_context_materialization(self):
        self.materialization.invalidate_completed_ownership()
        evidence = read_gameplay_save_evidence()
        if not self.runtime_effects_ready(evidence):
            self.materialization.trigger("receipt")
            return
        _plan, error = self._context_materialize_inventory(evidence, trigger="receipt")
        if error:
            logger.warning("CONTEXT_MATERIALIZATION_RETRY detail=%s", error)

    def _poll_materialization_completion(self):
        return self.materialization.poll_completion(reconciliation_publisher())

    def _context_materialize_inventory(self, evidence, *, trigger="context", manual=False):
        """Reconcile AP-owned persistent state for each accepted context lease."""
        if trigger is not None:
            self.materialization.trigger(str(trigger))
        if not self.runtime_effects_ready(evidence):
            return None, "runtime effects are not level-ready"
        slot_data = getattr(self, "_connected_slot_data", {})
        try:
            validate_slot_contract(slot_data)
        except ValueError as error:
            self.materialization.block(str(error))
            return None, str(error)
        context = self._refresh_runtime_context(slot_data, evidence)
        admitted, error = self.materialization.admit_context(
            context, use_dlc_content=bool(slot_data.get("use_dlc_content")),
            dlc_block=self.dlc_evidence.reason if self.dlc_evidence.blocks_enabled else None,
        )
        if not admitted:
            return None, error
        transition = self.pending_context_transition
        materialization_lease = self._active_materialization_lease(context)
        ownership = effective_ownership(
            self.items_received[: self.items_processed],
            randomize_chainsaw=bool(slot_data.get("randomize_chainsaw", False)),
            randomize_dash=bool(slot_data.get("randomize_dash", False)),
            checked_locations=frozenset(getattr(self, "checked_locations", ())),
            local_checked_locations=frozenset(getattr(self, "locations_checked", ())),
            server_checked_ready=getattr(self, "server_checked_locations_ready", False),
            hell_on_earth_locations=HELL_ON_EARTH_LOCATION_IDS,
            exultia_complete_location=EXULTIA_COMPLETE_LOCATION, slot=self.slot,
        )
        scope = MaterializationScope(
            self.room_seed_name, self.team, self.slot, self.state_key,
            materialization_lease, evidence.epoch, str(trigger or "context"),
            str(self.get_ap_state_key() or "unbound"), manual,
        )
        outcome = self.materialization.reconcile(
            scope, context, ownership, transition, ITEM_ID_TO_COMMAND, ITEM_REPLAY_POLICIES,
            slot_data.get("special_weapon"), reconciliation_publisher(), self.persist_session_state,
        )
        if outcome.complete_transition:
            self.runtime_lifecycle.complete_transition()
        return outcome.plan, outcome.error

    def handle_connection_loss(self, msg: str) -> None:
        state_key = self.state_key
        self.reset_transient_effects("connection_loss")
        self.reset_queue_session_authority("connection_loss")
        self.deathlink.abandon(time.monotonic(), "disconnect")
        discard_queued_coalesced_command(DEATHLINK_KILL_COALESCE_KEY, state_key)
        super().handle_connection_loss(msg)
        if self._room_session_established:
            if LAUNCHER_EVENTS_ENABLED and not self._launcher_connection_loss_reported:
                self._launcher_connection_loss_reported = True
                emit_launcher_event(
                    "connection_lost",
                    code="connection_closed",
                    message="The room connection was interrupted; reconnecting automatically.",
                    technical_message=msg,
                )
        else:
            self._report_launcher_connection_failure(msg, technical_message=msg)

    async def connection_closed(self):
        state_key = self.state_key
        self.reset_transient_effects("connection_closed")
        self.reset_queue_session_authority("connection_closed")
        self.deathlink.abandon(time.monotonic(), "disconnect")
        discard_queued_coalesced_command(DEATHLINK_KILL_COALESCE_KEY, state_key)
        unexpected_launcher_close = (
            LAUNCHER_EVENTS_ENABLED
            and self.server is not None
            and not self.disconnected_intentionally
            and not self.exit_event.is_set()
        )
        await super().connection_closed()
        if unexpected_launcher_close and not self._room_session_established:
            self._report_launcher_connection_failure(
                "Disconnected from the Archipelago server",
                code="connection_closed",
            )
        elif unexpected_launcher_close and not self._launcher_connection_loss_reported:
            self._launcher_connection_loss_reported = True
            emit_launcher_event(
                "connection_lost",
                code="connection_closed",
                message="The room connection was interrupted; reconnecting automatically.",
                technical_message="Disconnected from the Archipelago server",
            )

    def queue_dev_commands(self, commands, action):
        """Spool isolated dev commands without touching receipt/bootstrap state."""
        self.dev_counter += 1
        correlation = f"devtest-{self.dev_session_id}-{self.dev_counter:04d}"
        for index, command in enumerate(commands):
            if not re.fullmatch(r"ai_ScriptCmdEnt [A-Za-z0-9_]+ activate", command):
                raise ValueError("Directed tests accept only map-side entity activation")
            command_id = f"{correlation}-cmd-{index:02d}"
            if not send_command(
                command,
                coalesce_key=command_id,
                state_key=self.state_key,
                room_scoped=False,
            ):
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
        if password_requested and LAUNCHER_EVENTS_ENABLED:
            self._report_launcher_connection_failure(
                "The room requires a password, or the supplied password was rejected.",
                code="invalid_password",
                reason_codes=["InvalidPassword"],
                technical_message="Archipelago requested password authentication",
            )
            return
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.get_username()
        await self.send_connect()

    def _observe_ammo_receipts(self):
        return self.ammo.observe_receipts(receipt_item_ids(self.items_received).count(AMMO_REFILL_ITEM_ID))

    def _refresh_ammo_refill_charge(self):
        return self._observe_ammo_receipts()

    def _ammo_refill_balance_payload(self):
        self._observe_ammo_receipts()
        return self.ammo.balance()

    def _refresh_ammo_refill_receipt_projection(self, item_id):
        if item_id != AMMO_REFILL_ITEM_ID:
            return False
        self._observe_ammo_receipts()
        self.ammo.emit_balance(source="item_received")
        return True

    def _schedule_ammo_refill_overflow_normalization(self, source):
        self._observe_ammo_receipts()
        self.ammo.schedule_overflow(source)

    def _ammo_refill_readiness(self):
        socket = getattr(getattr(self, "server", None), "socket", None)
        evidence = read_gameplay_save_evidence()
        return ammo_readiness(
            placement_ready=bool(self.location_setup.complete), item_ready=bool(self.item_state_ready),
            connected=bool(socket is not None and not socket.closed),
            namespace_match=bool(self._queue_session_authoritative and queue_session_namespace(self.state_key) is not None),
            marker=self.runtime_lifecycle.map_identity.cached_marker, active_lease=self._active_materialization_lease(),
            native_safe=bool(evidence is not None and evidence.native_safe), runtime_ready=self.runtime_effects_ready(evidence),
            active_slot=self.active_save_slot, available=self.ammo.available,
        )

    def _configure_ammo_refill_storage(self):
        self.ammo_requests.reset()
        self.ammo.bind(self.state_key)
        self._observe_ammo_receipts()
        keys = self.ammo.storage_keys
        if any(key is None for key in keys):
            return
        for key in keys:
            self.stored_data.pop(key, None)
            self.stored_data_notification_keys.add(key)
        self.session_tasks.start(self.ammo.load_storage)
        self.ammo.emit_balance(status="loading", source="initial_storage_load", message="Ammo Refill storage loading")

    def _consume_ammo_refill_storage(self, value):
        self._observe_ammo_receipts()
        self.ammo.consume_consumed(value)

    def _consume_ammo_refill_discarded_storage(self, value):
        self._observe_ammo_receipts()
        self.ammo.consume_discarded(value)

    async def request_ammo_refill(self):
        self._observe_ammo_receipts()
        scope = AmmoCommandScope(
            self.state_key, self.runtime_lifecycle.map_identity.current_map, self.active_save_slot,
            active_crucible(self._connected_slot_data.get("special_weapon"),
                            receipt_item_ids(self.items_received[: self.items_processed])),
        )
        return await self.ammo.request(scope, self._ammo_refill_readiness())

    def _consume_ammo_refill_request_file(self):
        return self.ammo_requests.consume(ammo_refill_request_files())

    def on_package(self, cmd: str, args: dict):
        if cmd == "RoomInfo":
            self.reset_transient_effects("room_info")
            # Durable item state cannot authorize queue work until matching
            # Connected rebinds state_key to this room.
            self.reset_queue_session_authority("room_info")
            self.room_seed_name = args.get("seed_name")
        elif cmd == "ReceivedItems":
            self._on_received_items_packet(args)
        elif cmd == "Connected":
            self._room_session_established = True
            self._launcher_connection_loss_reported = False
            previous_state_key = self.state_key
            self.initialize_item_state()
            if previous_state_key and previous_state_key != self.state_key:
                abandoned = self.deathlink.abandon(time.monotonic(), "room_changed")
                discard_queued_coalesced_command(
                    DEATHLINK_KILL_COALESCE_KEY, previous_state_key
                )
                if abandoned:
                    logger.warning(
                        "[DeathLink] Cleared room-bound receive events after slot change: %s.",
                        ", ".join(event_id[:12] for event_id in abandoned),
                    )
            slot_data = args.get("slot_data", {})
            if not isinstance(slot_data, dict):
                slot_data = {}
            try:
                slot_data = validate_slot_contract(slot_data)
            except ValueError as error:
                message = f"Unsupported DOOM Eternal 0.5-D slot contract: {error}"
                logger.error("[Contract] Connected slot rejected: %s", error)
                self._report_launcher_connection_failure(
                    message,
                    code="room_incompatible",
                    technical_message=message,
                )
                return
            self._connected_slot_data = slot_data
            self.goals.connected(self.persist_session_state)
            self._refresh_runtime_context(slot_data)
            self.deathlink.configure(slot_data.get("death_link", False))
            logger.info(
                "[DeathLink] enabled=%s receive_policy=single_burst",
                self.death_link_enabled,
            )
            self.receipt_session.configure_starting_materialization(
                starting_inventory=slot_data.get("starting_inventory", {}),
                starting_weapon=slot_data.get("starting_weapon"),
                item_identity=ITEM_CLASSIFICATION_IDENTITY,
                processed_receipts=self.items_received[:min(self.items_processed, len(self.items_received))],
                eligible=start_inventory_eligible,
            )
            self._death_link_task = self.session_tasks.start(
                lambda: self.update_death_link(self.death_link_enabled)
            )
            self.server_checked_locations_ready = False
            try:
                active = self.location_setup.begin(args, _doom_location_ids())
            except ValueError as error:
                self._fail_placement_scout(str(error))
                return
            self.locations_info.clear()
            if not active:
                self._try_complete_location_scouts()
            else:
                self.session_tasks.start(self._scout_active_locations)
            self._configure_ammo_refill_storage()
        elif cmd == "LocationInfo":
            self._consume_location_info(args)
        elif cmd in {"Retrieved", "SetReply"}:
            ammo_key, discarded_key = self.ammo.storage_keys
            if cmd == "Retrieved" and ammo_key in args.get("keys", {}):
                value = args["keys"].get(ammo_key)
                self._consume_ammo_refill_storage(0 if value is None else value)
            elif cmd == "SetReply" and args.get("key") == ammo_key:
                self._consume_ammo_refill_storage(args.get("value"))
            if cmd == "Retrieved" and discarded_key in args.get("keys", {}):
                value = args["keys"].get(discarded_key)
                self._consume_ammo_refill_discarded_storage(0 if value is None else value)
            elif cmd == "SetReply" and args.get("key") == discarded_key:
                self._consume_ammo_refill_discarded_storage(args.get("value"))
            key = self._launcher_hints_key()
            if (cmd == "Retrieved" and key in args.get("keys", {})) or (
                cmd == "SetReply" and args.get("key") == key
            ):
                self._emit_launcher_hints("DATA_RECEIVED" if cmd == "Retrieved" else "UPDATED")
        elif cmd == "ConnectionRefused":
            self.reset_queue_session_authority("connection_refused")
            reasons = args.get("errors", [])
            if not isinstance(reasons, (list, tuple)):
                reasons = [reasons]
            self._report_launcher_connection_failure(
                "Archipelago rejected the room login.",
                code="connection_refused",
                reason_codes=reasons,
                technical_message="ConnectionRefused: " + ", ".join(map(str, reasons)),
            )
        elif cmd == "RoomUpdate" and "checked_locations" in args:
            self.server_checked_locations_ready = isinstance(args.get("checked_locations"), (list, tuple, set, frozenset))
            if self.server_checked_locations_ready:
                self.checked_locations.update(args["checked_locations"])
                self.snapshot_fast_travel_eligibility(refresh=True)
            self.publisher_dispatch.observe_protocol(DoomEternalContext.check_observation(self))
            self.reconcile_checked_automap_cleanup("server_checked_update")
            self.reconcile_fast_travel_unlock("server_checked_update")
            self.session_tasks.start(self.check_mission_challenge_locations)
        elif cmd == "Bounced" and "DeathLink" in args.get("tags", []):
            data = args.get("data", {})
            self.deathlink.observe_echo(data.get("time"), self.last_death_link)


    def _fail_placement_scout(self, message):
        if not self.location_setup.fail(message):
            return
        self.server_checked_locations_ready = False
        self._report_launcher_connection_failure(
            message, code="malformed_server_data", technical_message=message
        )


    async def _scout_active_locations(self):
        result = await self.location_setup.scout(self.check_publication())
        if not result.current:
            return
        if result.error:
            self._fail_placement_scout(result.error)
            return
        self._try_complete_location_scouts()


    def _consume_location_info(self, args):
        error = self.location_setup.consume(args, frozenset(self.locations_info))
        if error:
            self._fail_placement_scout(error)
            return
        self._try_complete_location_scouts()


    def _try_complete_location_scouts(self):
        if not self.location_setup.ready_to_resolve:
            return
        try:
            placements = resolve_placement_records(
                self.location_setup.received_ids, self.locations_info, self.slot_info, self.protocol_names(), self.slot
            )
        except Exception as error:
            self._fail_placement_scout(f"Placement resolution failed: {error}")
            return
        self.location_setup.resolved()
        self._complete_connected(placements)



    def _complete_connected(self, placements):
        self.server_checked_locations_ready = True
        self.onboard_bootstrap("on_connect")
        self.reconcile_checked_automap_cleanup("server_connected")
        self.reconcile_fast_travel_unlock("connected")
        self.session_tasks.start(self.check_mission_challenge_locations)
        if self._item_delivery_wakeup:
            self._schedule_item_delivery("connected")
        balance = self._ammo_refill_balance_payload()
        emit_launcher_event(
            "connected",
            seed_name=self.room_seed_name,
            endpoint=str(getattr(self, "server_address", "") or ""),
            team=self.team,
            slot=self.slot,
            slot_data=getattr(self, "_connected_slot_data", {}),
            missing_locations=sorted(self.missing_locations),
            checked_locations=sorted(self.checked_locations),
            placements=placements,
            placement_scouts_complete=True,
            ammo_refills_available=balance["available"],
            ammo_refills_consumed=balance["consumed"],
            ammo_refills_authoritative=balance["authoritative"],
        )

    def _on_received_items_packet(self, args):
        """Record packet metadata and wake delivery without doing delivery work."""
        packet_received_ns = time.monotonic_ns()
        packet_items = args.get("items", ()) if isinstance(args, dict) else ()
        try:
            packet_item_count = len(packet_items)
        except TypeError:
            packet_item_count = 0
        authoritative_count = len(self.items_received)
        packet_start_index, packet_accepted = self.receipt_session.observe_packet(
            args.get("index") if isinstance(args, dict) else None,
            packet_item_count, authoritative_count, packet_received_ns,
        )
        log_item_event(
            "ITEM_PACKET_OBSERVATION",
            packet_start_index=packet_start_index,
            packet_count=packet_item_count,
            authoritative_count=authoritative_count,
            boundary_before=getattr(self, "items_processed", 0),
            boundary_after=getattr(self, "items_processed", 0),
            received_count=authoritative_count,
            processed_count=getattr(self, "items_processed", 0),
            state_key=getattr(self, "state_key", None) or "unresolved",
            success=packet_accepted,
            packet_received_monotonic_ns=packet_received_ns,
        )
        self._schedule_item_delivery("packet")
        self._schedule_ammo_refill_overflow_normalization("received_items")

    def _schedule_item_delivery(self, trigger):
        """Coalesce packet wakeups into one event-loop delivery runner."""
        exit_event = getattr(self, "exit_event", None)
        if exit_event is not None and exit_event.is_set():
            return
        self._item_delivery_wakeup = True
        task = getattr(self, "_item_delivery_task", None)
        if task is None or task.done():
            self._item_delivery_task = asyncio.get_running_loop().create_task(
                self._run_scheduled_item_delivery()
            )

    async def _run_scheduled_item_delivery(self):
        try:
            while self._item_delivery_wakeup:
                self._item_delivery_wakeup = False
                await self.process_pending_item_receipts("packet")
                if self._item_delivery_waiting_for_state:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[Tracking] ITEM_DELIVERY_RUNNER_CRASH")

    def _packet_received_timestamp(self, receipt_index):
        return self.receipt_session.packet_timestamp(receipt_index)

    def _packet_receipt_is_live_tail(self, receipt_index):
        return self.receipt_session.packet_is_live_tail(receipt_index)

    async def process_pending_item_receipts(self, trigger):
        """Consume authoritative receipts once, in increasing receive-index order."""
        async with self._item_delivery_lock:
            self._item_delivery_wakeup = False
            if not self.item_state_ready:
                log_item_event(
                    "ITEM_DELIVERY_BLOCKED",
                    reason="item_state_not_ready",
                    trigger=trigger,
                    boundary=getattr(self, "items_processed", None),
                    state_key=getattr(self, "state_key", None),
                )
                self._item_delivery_waiting_for_state = True
                self._item_delivery_wakeup = True
                return False
            self._item_delivery_waiting_for_state = False
            receipt_session_token = self.receipt_session.capture(getattr(self, "state_key", ""))
            try:
                history_observation = self.observe_received_item_history()
                duplicate_indices = {
                    receipt.index for receipt in history_observation.duplicates
                }
            except ValueError as exc:
                logger.error("[Tracking] ReceivedItems history rejected: %s", exc)
                log_item_event(
                    "ITEM_DELIVERY_BLOCKED",
                    reason="history_incompatible",
                    detail=str(exc),
                    trigger=trigger,
                    boundary=getattr(self, "items_processed", None),
                    received_count=len(self.items_received),
                    state_key=getattr(self, "state_key", None),
                    bridge_revision=BRIDGE_REVISION,
                    mapping_revision=ITEM_MAPPING_REVISION,
                )
                return False

            batch_count = 0
            fresh_receipt_boundary = self.items_processed
            while len(self.items_received) > self.items_processed:
                item_index = self.items_processed
                network_item = self.items_received[item_index]
                item_id = network_item.item
                packet_received_ns = self._packet_received_timestamp(item_index)
                duplicate = item_index in duplicate_indices
                observation_key = (
                    getattr(self, "state_key", ""),
                    item_index,
                    receipt_identity(network_item),
                )
                pending = not duplicate and self.receipt_session.was_observed(observation_key)
                log_delivery_event(
                    "ITEM_RECEIPT_CLASSIFIED",
                    receipt_index=item_index,
                    item_id=item_id,
                    receipt_id=receipt_identity(network_item),
                    trigger=trigger,
                    classification="duplicate" if duplicate else "pending" if pending else "new",
                    boundary=self.items_processed,
                    state_key=getattr(self, "state_key", None),
                    bridge_revision=BRIDGE_REVISION,
                    mapping_revision=ITEM_MAPPING_REVISION,
                    packet_received_monotonic_ns=packet_received_ns,
                )
                if not duplicate:
                    self.receipt_session.note_observation(observation_key)
                if duplicate:
                    logger.info(
                        "[To Game] Duplicate authoritative receipt acknowledged "
                        "without replay: index=%s item_id=%s",
                        item_index,
                        item_id,
                    )
                    self.receipt_session.advance()
                    self.persist_session_state()
                    log_item_event(
                        "ITEM_RECEIPT_ACK",
                        receipt_index=item_index,
                        item_id=item_id,
                        receipt_id=receipt_identity(network_item),
                        outcome="duplicate_no_replay",
                        boundary=self.items_processed,
                        trigger=trigger,
                        state_key=getattr(self, "state_key", None),
                    )
                    batch_count += 1
                    if batch_count >= ITEM_DELIVERY_BATCH_SIZE:
                        batch_count = 0
                        await asyncio.sleep(0)
                        if not self.receipt_session.is_current(receipt_session_token, getattr(self, "state_key", "")):
                            return False
                    continue

                if self.receipt_session.consume_starting_materialization(item_id):
                    logger.info(
                        "[To Game] Materialized starting receipt acknowledged without replay: "
                        "index=%s item_id=%s",
                        item_index,
                        item_id,
                    )
                    self._record_processed_receipt(network_item)
                    self.receipt_session.advance()
                    self.persist_session_state()
                    log_item_event(
                        "ITEM_RECEIPT_ACK",
                        receipt_index=item_index,
                        item_id=item_id,
                        receipt_id=receipt_identity(network_item),
                        outcome="materialized_no_replay",
                        boundary=self.items_processed,
                        trigger=trigger,
                        state_key=getattr(self, "state_key", None),
                    )
                    batch_count += 1
                    if batch_count >= ITEM_DELIVERY_BATCH_SIZE:
                        batch_count = 0
                        await asyncio.sleep(0)
                        if not self.receipt_session.is_current(receipt_session_token, getattr(self, "state_key", "")):
                            return False
                    continue

                log_delivery_event(
                    "ITEM_RECEIPT",
                    receipt_index=item_index,
                    item_id=item_id,
                    receipt_id=receipt_identity(network_item),
                    item_name=self.delivery_item_name(item_id)
                    if item_id in ITEM_CLASSIFICATION_IDENTITY else None,
                    trigger=trigger,
                    boundary=self.items_processed,
                    history_fingerprint=receipt_history_fingerprint(self.items_received),
                    state_key=getattr(self, "state_key", None),
                    mapping_revision=ITEM_MAPPING_REVISION,
                    packet_received_monotonic_ns=packet_received_ns,
                    active_map=self.current_map_name,
                    slot=self.active_save_slot,
                    bridge_revision=BRIDGE_REVISION,
                    protocol_version=BRIDGE_PROTOCOL,
                )
                if item_id not in ITEM_ID_TO_COMMAND:
                    logger.error(
                        f"[To Game] No command mapping for item {item_id}; delivery paused. "
                        "The seed/APWorld and bridge build are out of sync."
                    )
                    self.output(
                        f"Missing item mapping for DOOM Eternal item {item_id}. "
                        "Check the local bridge logs."
                    )
                    log_item_event(
                        "ITEM_DELIVERY_BLOCKED",
                        reason="missing_mapping",
                        receipt_index=item_index,
                        item_id=item_id,
                        boundary=self.items_processed,
                        trigger=trigger,
                        state_key=getattr(self, "state_key", None),
                        bridge_revision=BRIDGE_REVISION,
                        mapping_revision=ITEM_MAPPING_REVISION,
                    )
                    break

                if item_id in TRANSIENT_EFFECTS:
                    live_tail_receipt = self._packet_receipt_is_live_tail(item_index)
                    if live_tail_receipt:
                        applied, description, retryable = (
                            self.transient_effect_manager.apply_receipt(item_id, self._observe_transient_runtime())
                        )
                    else:
                        applied = False
                        description = "historical transient receipt is not replayable"
                        retryable = False
                    if not applied:
                        if retryable:
                            logger.info(
                                "[To Game] Transient item %s pending: %s",
                                item_id, description,
                            )
                            break
                        logger.info(
                            "[To Game] Transient item %s skipped: %s",
                            item_id, description,
                        )
                    self._record_processed_receipt(network_item)
                    self.receipt_session.advance()
                    self.persist_session_state()
                    log_item_event(
                        "ITEM_RECEIPT_ACK",
                        receipt_index=item_index,
                        item_id=item_id,
                        receipt_id=receipt_identity(network_item),
                        outcome=(
                            "transient_effect_armed"
                            if applied
                            else (
                                "transient_effect_skipped_unsafe"
                                if live_tail_receipt
                                else "transient_effect_skipped_historical"
                            )
                        ),
                        detail=description,
                        boundary=self.items_processed,
                        trigger=trigger,
                        state_key=getattr(self, "state_key", None),
                    )
                    batch_count += 1
                    continue

                definition = ITEM_ID_TO_COMMAND[item_id]
                runtime_context = self._refresh_runtime_context(
                    getattr(self, "_connected_slot_data", {})
                )
                special_ids = {7770007, 7770009, 7770901, 7770902}
                defer_special = item_id in special_ids and not self._base_special_receipt_allowed(
                    item_id, item_index, runtime_context
                )
                materialization_lease = self._active_materialization_lease(runtime_context)
                replay_policy = ITEM_REPLAY_POLICIES.get(item_id)
                context_allows_item = bool(
                    runtime_context is not None
                    and item_id in context_item_ids(runtime_context, {item_id})
                )
                defer_replayable = bool(
                    replay_policy is not None
                    and replay_policy.policy in {"replay_idempotent", "replay_manual_only"}
                    and (materialization_lease is None or not context_allows_item)
                )
                if item_id in SUPPORT_RUNE_IDS or defer_special or defer_replayable:
                    logger.info(
                        "[To Game] Item %s deferred to supported runtime context.",
                        item_id,
                    )
                    notified, notification_error = self.spool_deferred_receipt_notification(
                        item_id,
                        item_index,
                        fresh_receipt_boundary=fresh_receipt_boundary,
                        excluded_receipt_indices=duplicate_indices,
                    )
                    if not notified:
                        logger.warning(
                            "[To Game] Deferred item %s notification pending: %s",
                            item_id,
                            notification_error,
                        )
                        break
                    self._record_processed_receipt(network_item)
                    self.receipt_session.advance()
                    self.persist_session_state()
                    log_item_event(
                        "ITEM_RECEIPT_DEFERRED",
                        receipt_index=item_index,
                        item_id=item_id,
                        receipt_id=receipt_identity(network_item),
                        reason="context_deferred",
                        boundary=self.items_processed,
                        trigger=trigger,
                        state_key=getattr(self, "state_key", None),
                    )
                    if item_id in SUPPORT_RUNE_IDS or materialization_lease is None or (
                        runtime_context is not None and runtime_context.campaign != "Base"
                    ):
                        self._trigger_live_context_materialization()
                    batch_count += 1
                    continue
                if (
                    replay_policy is not None
                    and not (
                        isinstance(definition, dict)
                        and definition.get("type") == "no_op"
                    )
                    and (materialization_lease is None or not context_allows_item)
                ):
                    logger.info(
                        "[To Game] Item %s pending compatible materialization lease.",
                        item_id,
                    )
                    break
                if isinstance(definition, dict) and definition.get("type") == "no_op":
                    logger.info(f"[To Game] Runtime-only item {item_id} acknowledged.")
                    self._record_processed_receipt(network_item)
                    self.receipt_session.advance()
                    self.persist_session_state()
                    self._refresh_ammo_refill_receipt_projection(item_id)
                    log_item_event(
                        "ITEM_RECEIPT_ACK",
                        receipt_index=item_index,
                        item_id=item_id,
                        receipt_id=receipt_identity(network_item),
                        outcome="runtime_only_no_replay",
                        boundary=self.items_processed,
                        trigger=trigger,
                        state_key=getattr(self, "state_key", None),
                    )
                    batch_count += 1
                    if batch_count >= ITEM_DELIVERY_BATCH_SIZE:
                        batch_count = 0
                        await asyncio.sleep(0)
                        if not self.receipt_session.is_current(receipt_session_token, getattr(self, "state_key", "")):
                            return False
                    continue

                try:
                    classification = received_item_classification(
                        item_id, getattr(network_item, "flags", None)
                    )
                    spooled, description = self.spool_item_commands(
                        item_id,
                        item_index,
                        intent=NEW_RECEIPT,
                        include_notification=ENABLE_ITEM_NOTIFICATIONS,
                        classification=classification,
                        packet_received_ns=packet_received_ns,
                        materialization_lease=materialization_lease,
                        fresh_receipt_boundary=fresh_receipt_boundary,
                        excluded_receipt_indices=duplicate_indices,
                        context_identity=(
                            runtime_context.identity
                            if runtime_context is not None
                            else None
                        ),
                    )
                    if not spooled:
                        item_name = self.delivery_item_name(item_id)
                        logger.error(
                            "[To Game] ITEM_DELIVERY_BLOCKED index=%d item_id=%d "
                            "item_name=%s description=%s",
                            item_index, item_id, item_name, description,
                        )
                        self.item_delivery_blocked = True
                        self.item_delivery_blocked_info = {
                            "index": item_index,
                            "item_id": item_id,
                            "item_name": item_name,
                            "description": description,
                        }
                        log_item_event(
                            "ITEM_DELIVERY_BLOCKED",
                            reason="spool_rejected",
                            receipt_index=item_index,
                            item_id=item_id,
                            item_name=item_name,
                            detail=description,
                            boundary=self.items_processed,
                            trigger=trigger,
                            state_key=getattr(self, "state_key", None),
                        )
                        break
                except Exception as error:
                    item_name = self.delivery_item_name(item_id)
                    tb = traceback.format_exc()
                    logger.error(
                        "[To Game] ITEM_DELIVERY_BLOCKED index=%d item_id=%d "
                        "item_name=%s type=%s msg=%s\n%s",
                        item_index, item_id, item_name, type(error).__name__, str(error), tb,
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
                    log_item_event(
                        "ITEM_DELIVERY_BLOCKED",
                        reason="spool_exception",
                        receipt_index=item_index,
                        item_id=item_id,
                        item_name=item_name,
                        exception_type=type(error).__name__,
                        detail=str(error),
                        boundary=self.items_processed,
                        trigger=trigger,
                        state_key=getattr(self, "state_key", None),
                    )
                    break
                else:
                    self.item_delivery_blocked = False
                    self.item_delivery_blocked_info = None

                logger.info(f"[To Game] Item received! {item_id} -> {description}")
                self._record_processed_receipt(network_item)
                self.receipt_session.advance()
                self.persist_session_state()
                log_item_event(
                    "ITEM_RECEIPT_ACK",
                    receipt_index=item_index,
                    item_id=item_id,
                    receipt_id=receipt_identity(network_item),
                    outcome="delivered",
                    boundary=self.items_processed,
                    trigger=trigger,
                    state_key=getattr(self, "state_key", None),
                    bridge_revision=BRIDGE_REVISION,
                    mapping_revision=ITEM_MAPPING_REVISION,
                )
                self.onboard_bootstrap("on_item_received")

                batch_count += 1
                if batch_count >= ITEM_DELIVERY_BATCH_SIZE:
                    batch_count = 0
                    await asyncio.sleep(0)
                    if not self.receipt_session.is_current(receipt_session_token, getattr(self, "state_key", "")):
                        return False

            self.receipt_session.prune_processed_packets()
            return True

    def observation_slot_for_source(self, source):
        if isinstance(source, PrimarySaveSelection):
            return source.slot_directory
        try:
            parent = Path(source).parent.name
        except (TypeError, ValueError):
            parent = ""
        if re.fullmatch(r"(?:GAME|DLC[12]|HORDE)-AUTOSAVE\d+", parent):
            return parent
        return self.active_save_slot or "<synthetic>"

    def select_save_observation_slot(self, slot_directory):
        """Select a slot without trusting legacy observed=true persistence."""
        if not self.save_observer.select_observation_slot(slot_directory):
            return
        self.save_checks.reset_observations()

    @property
    def selected_observation_slot(self):
        return self.save_observer.observation_slot

    @property
    def mission_select_observation_map(self):
        return self.save_observer.mission_select.map_name

    @property
    def mission_select_observation_epoch(self):
        return self.save_observer.mission_select.epoch

    @property
    def active_save_slot(self):
        return self.save_observer.selection.slot

    @property
    def active_save_path(self):
        return self.save_observer.selection.path

    @property
    def active_save_token(self):
        return self.save_observer.selection.token

    @property
    def active_native_evidence_epoch(self):
        return self.save_observer.selection.native_evidence_epoch

    @property
    def active_save_proof_authoritative(self):
        return self.save_observer.readiness.authoritative

    @property
    def active_save_proof_slot(self):
        return self.save_observer.readiness.slot

    @property
    def active_save_proof_evidence_epoch(self):
        return self.save_observer.readiness.evidence_epoch

    @property
    def active_save_proof_load_epoch(self):
        return self.save_observer.readiness.load_epoch

    @property
    def runtime_observers_frozen(self):
        return self.save_observer.readiness.frozen

    def has_authoritative_save_proof(self):
        lease = getattr(self, "runtime_observation_lease", None)
        return self.save_observer.has_authoritative_proof(
            lease_present=lease is not None,
            gameplay_loaded_ns=getattr(lease, "gameplay_loaded_ns", None),
        )

    def invalidate_active_save_proof(self):
        """Clear proof authority at a gameplay/process lifecycle boundary."""
        self.save_observer.invalidate_proof()

    def ingest_visible_runtime_lifecycle(self, evidence=None, lifecycle_markers=None):
        """Accept fresh FirstThink markers as live-map and load-epoch authority."""
        lease = getattr(self, "runtime_observation_lease", None)
        evidence_epoch = getattr(evidence, "epoch", None)
        if evidence_epoch is not None:
            self.runtime_lifecycle.record_native_epoch(evidence_epoch)
        if lease is not None and not lease.process_probe():
            return False

        markers = (
            lifecycle_markers
            if lifecycle_markers is not None
            else discover_active_map_markers()
        )
        if not markers:
            return self.advance_known_map_materialization(evidence)
        newest_mtime, newest_path = markers[-1]
        started = getattr(lease, "started_ns", None) if lease else None
        action = self.runtime_lifecycle.authored_timestamp_action(newest_mtime, started)
        if action == "ignore":
            return False
        if action == "native_fallback":
            return self.advance_known_map_materialization(evidence)
        marker_data = parse_active_map_marker(newest_path, newest_mtime)
        if marker_data is None:
            self.invalidate_map_identity("malformed_marker")
            return False
        self.invalidate_active_save_proof()
        self.fast_travel.invalidate(clear_submitted=True)
        self.save_observer.clear_mission_select()
        self.runtime_lifecycle.clear_map()
        self.runtime_lifecycle.clear_pending_marker()
        marker_data = self.runtime_lifecycle.authored_marker_proposal(
            marker_data, newest_mtime, gameplay_evidence_mtime_ns(), evidence_epoch,
        )
        if lease is not None:
            lease.observe_gameplay_loaded(newest_mtime)
        self.accept_map_identity(marker_data, evidence_epoch)
        self.snapshot_fast_travel_eligibility(marker_data=marker_data)
        for _, marker_path in markers:
            cleanup_active_map_marker_file(marker_path)
        return True

    def advance_known_map_materialization(self, evidence):
        """Advance epoch from a fresh native load edge or transition."""
        cached = getattr(self, "cached_map_identity", None)
        decision = self.runtime_lifecycle.classify_native_load(evidence, KNOWN_CATALOG_MAPS)
        if decision is None:
            return False
        evidence_epoch = decision.evidence_epoch
        if decision.action == "bind":
            self.runtime_lifecycle.bind_materialization_evidence(evidence_epoch)
            logger.info(
                "[MAP] MATERIALIZATION_GENERATION_BOUND map=%s evidence_epoch=%s "
                "source=first_same_map_evidence provisional=%s",
                cached.get("map_key", "<unknown>"),
                evidence_epoch,
                str(bool(getattr(evidence, "provisional", False))).lower(),
            )
            return False
        if decision.action == "suspend":
            return self.suspend_map_materialization(
                cached, evidence_epoch, "provisional_same_map_load_edge"
            )
        evidence_mtime = gameplay_evidence_mtime_ns()
        marker_data = self.runtime_lifecycle.native_marker_proposal(decision, evidence_mtime)
        if marker_data is None:
            return False
        epoch = marker_data["gameplay_epoch"]

        lease = getattr(self, "runtime_observation_lease", None)
        if lease is not None:
            lease.observe_gameplay_loaded(evidence_mtime)
        self.invalidate_active_save_proof()
        self.fast_travel.invalidate(clear_submitted=True)
        if decision.action != "reload":
            self.save_observer.clear_mission_select()
        self.accept_map_identity(marker_data, evidence_epoch)
        self.snapshot_fast_travel_eligibility(marker_data=marker_data)
        self.advance_automap_cleanup_epoch()
        if decision.action == "reload":
            logger.info(
                "[MAP] MATERIALIZATION_EPOCH_SECONDARY map=%s epoch=%s "
                "source=native_same_map_load_edge evidence_provisional=%s",
                marker_data.get("map_key", "<unknown>"), epoch,
                bool(getattr(evidence, "provisional", False)),
            )
        else:
            event = "MAP_INITIALIZE_EVIDENCE" if decision.action == "initialize" else "MAP_TRANSITION_EVIDENCE"
            logger.info(
                "[MAP] %s map=%s epoch=%s runtime_map=%s",
                event, decision.map_key, epoch, decision.runtime_map,
            )
        return True

    def suspend_map_materialization(self, cached, evidence_epoch, reason):
        """Suspend map authority until a fresh authored marker reacquires context."""
        if cached.get("materialization_suspended"):
            return False
        self.invalidate_active_save_proof()
        self.runtime_lifecycle.suspend_marker(evidence_epoch, reason)
        publish_materialization_lease(None)
        self.runtime_lifecycle.record_lease_publication(None, False)
        self.runtime_lifecycle.clear_context()
        logger.info(
            "[MAP] MATERIALIZATION_SUSPENDED map=%s evidence_epoch=%s reason=%s",
            cached.get("map_key", "<unknown>"), evidence_epoch, reason,
        )
        return True

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
        self.save_observer.update_selection(
            slot=selected.slot_directory, path=str(selected.path), token=selected.mtime_ns,
        )
        self.save_observer.activate_slot(selected.slot_directory)
        self.select_save_observation_slot(selected.slot_directory)

    def invalidate_save_observation_slot(self, slot_directory):
        """Discard local authority when a slot directory has been recreated."""
        self.save_observer.invalidate_observation_slot(slot_directory)
        self.select_save_observation_slot(slot_directory)

    def log_save_proof_rejected(
        self, reason, evidence_slot=None, marker_map=None, candidate_slot=None,
        candidate_mtime=None, active_slot=None,
    ):
        rejection = (
            reason,
            evidence_slot or "<none>",
            marker_map or "<none>",
            candidate_slot or "<none>",
            candidate_mtime or 0,
            active_slot or self.active_save_slot or "<none>",
        )
        if rejection == getattr(self, "last_save_proof_rejection", None):
            return
        self.last_save_proof_rejection = rejection
        logger.info(
            "SAVE_PROOF_REJECTED reason=%s evidence_slot=%s marker_map=%s "
            "candidate_slot=%s candidate_mtime=%s active_slot=%s",
            *rejection,
        )
        logger.info(
            "SAVE_SLOT_REJECTED slot=%s reason=%s path=%s",
            candidate_slot or "<none>",
            reason,
            candidate_slot or "<none>",
        )

    def invalidate_map_identity(self, reason, *, clear_pending=True):
        self.reset_transient_effects(reason)
        if getattr(self, "last_marker_reject_reason", None) != reason:
            logger.info("[MAP] MAP_IDENTITY_MARKER_REJECTED reason=%s", reason)
            self.last_marker_reject_reason = reason
        self.runtime_lifecycle.clear_map()
        publish_materialization_lease(None)
        self.runtime_lifecycle.record_lease_publication(None, False)
        if reason == "game_not_running":
            self.runtime_lifecycle.clear_context(clear_log=True)
        if clear_pending:
            self.runtime_lifecycle.clear_pending_marker()
        self.save_observer.clear_mission_select()
        self.fast_travel.invalidate(clear_submitted=False)
        return None

    def store_pending_map_identity(self, marker_data):
        self.runtime_lifecycle.stage_marker(marker_data)
        publish_materialization_lease(None)
        self.save_observer.set_frozen(True)
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

    def accept_map_identity(self, marker_data, evidence_epoch=None):
        previous = getattr(self, "cached_map_identity", None)
        if isinstance(previous, Mapping) and any(
            previous.get(key) != marker_data.get(key)
            for key in ("map_key", "runtime_map", "gameplay_epoch")
        ):
            self.reset_transient_effects("map_transition")
            self.fast_travel.invalidate(clear_submitted=True)
        marker_data = self.runtime_lifecycle.accept_marker(marker_data, evidence_epoch)
        materialized_epoch = marker_data.get("gameplay_epoch")
        published = publish_materialization_lease(materialized_epoch)
        self.runtime_lifecycle.record_lease_publication(materialized_epoch, published)
        self.level_ready.queue(materialized_epoch, marker_data.get("path"))
        marker_mtime = marker_data.get("mtime_ns", 0)
        if self.runtime_lifecycle.observe_marker_timestamp(marker_mtime, evidence_epoch):
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

        cached = self.cached_map_identity
        if not isinstance(cached, Mapping):
            if evidence is not None and getattr(evidence, "state", None) != "gameplay":
                self.invalidate_active_save_proof()
                return self.invalidate_map_identity("menu")
            return None
        if cached.get("materialization_suspended"):
            return None
        marker_mtime = cached.get("mtime_ns", 0)
        if lease is not None and lease.started_ns and marker_mtime < lease.started_ns:
            return self.invalidate_map_identity("stale_marker")
        if evidence is not None and getattr(evidence, "state", None) != "gameplay":
            self.invalidate_active_save_proof()
            hold_signature = (
                marker_mtime,
                getattr(evidence, "state", None),
                bool(getattr(evidence, "provisional", False)),
            )
            if hold_signature != getattr(self, "last_map_identity_hold_signature", None):
                self.last_map_identity_hold_signature = hold_signature
                logger.info(
                    "[MAP] MAP_IDENTITY_HELD map=%s epoch=%s evidence_state=%s",
                    cached.get("map_key", "<unknown>"),
                    cached.get("gameplay_epoch", "<unknown>"),
                    getattr(evidence, "state", "<unknown>"),
                )
            return self.accept_map_identity(cached, getattr(evidence, "epoch", None))
        self.last_map_identity_hold_signature = None
        return self.accept_map_identity(cached, getattr(evidence, "epoch", None))

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
        evidence = read_gameplay_save_evidence()
        self.ingest_visible_runtime_lifecycle(evidence=evidence)
        marker = self.read_active_map_identity(evidence=evidence)
        marker_map = marker["runtime_map"] if marker else None
        marker_context = classify_runtime_context(marker_map) if marker_map else None
        evidence_context = None
        if (
            evidence
            and evidence.state == "gameplay"
            and getattr(evidence, "native_safe", False)
            and getattr(evidence, "map_name", None)
        ):
            evidence_context = classify_runtime_context(evidence.map_name)
        transition_context = marker_context or evidence_context
        active_campaign = transition_context.campaign if transition_context else None
        expected_prefix = expected_save_prefix_for_campaign(active_campaign)

        active_family_mismatch, prior_evidence_epoch = self.save_observer.observe_expected_family(expected_prefix)

        if marker_map and evidence and getattr(evidence, "map_name", None):
            if (
                marker_context is not None
                and evidence_context is not None
                and marker_context.identity != evidence_context.identity
            ):
                self._record_context_evidence_rejection(
                    "mission_select_save_details",
                    marker_context.identity,
                    evidence_context.identity,
                )

        evidence_slot = evidence.slot_directory if (evidence and getattr(evidence, "slot_directory", None)) else None
        if expected_prefix and evidence_slot and not evidence_slot.startswith(expected_prefix):
            evidence_slot = None
        evidence_epoch = evidence.epoch if (evidence and getattr(evidence, "epoch", None) is not None) else None

        candidates = (
            primary_save_candidates(slot_prefix=expected_prefix)
            if expected_prefix
            else primary_save_candidates()
        )
        for selected in candidates:
            if self.save_observer.observe_candidate(selected):
                logger.info(
                    "SAVE_SLOT_CANDIDATE slot=%s path=%s mtime_ns=%s",
                    selected.slot_directory,
                    selected.path,
                    selected.mtime_ns,
                )

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

        provisional_family_switch = self.save_observer.permits_provisional_family_switch(
            evidence, marker_absent=marker is None, context_known=evidence_context is not None,
            expected_prefix=expected_prefix, active_family_mismatch=active_family_mismatch,
            newest=newest, active=active, evidence_epoch=evidence_epoch,
            prior_evidence_epoch=prior_evidence_epoch,
        )

        def fail_proof(reason):
            self.save_observer.set_frozen(True)
            self.save_observer.clear_mission_select()
            self.log_save_proof_rejected(
                reason,
                evidence_slot=evidence_slot,
                marker_map=marker_map,
                candidate_slot=candidate_slot,
                candidate_mtime=candidate_mtime,
                active_slot=self.active_save_slot,
            )
            return None

        decision = self.save_observer.select_candidate(
            evidence=evidence, marker_present=marker is not None,
            evidence_slot=evidence_slot, expected_prefix=expected_prefix,
            newest=newest, active=active,
            process_running=lease is None or lease.process_probe(),
            load_epoch=proof_load_epoch,
            mission_select_map=self.mission_select_observation_map,
            provisional_family_switch=provisional_family_switch,
        )
        if decision.action == "reject":
            if decision.reason == "game_not_running" and self.last_observer_lease_block != "game_not_running":
                logger.info("[OBSERVER] LIVE_LEASE_BLOCKED reason=game_not_running")
                self.last_observer_lease_block = "game_not_running"
            return fail_proof(decision.reason)
        if decision.action == "continue":
            self.save_observer.update_selection(path=str(decision.continued.path))
            if decision.reset_observation_slot:
                self.invalidate_save_observation_slot(decision.continued.slot_directory)
            self.save_observer.continue_selection(decision.continued)
            self.reconcile_fast_travel_unlock("save_proof")
            return decision.continued

        selected = primary_save_for_slot(decision.target_slot)
        if selected is None:
            return fail_proof("no_gameplay_evidence")

        details = read_game_details_for_selection(selected)
        if not details:
            return fail_proof("no_game_details")

        active_map = marker_map or (
            canonical_map_name(evidence.map_name)
            if provisional_family_switch and evidence
            else None
        )
        if not active_map:
            return fail_proof("map_marker_unavailable")
        continue_target_map = canonical_map_name(details.get("mapName", ""))
        marker_context = classify_runtime_context(active_map)
        details_context = classify_runtime_context(continue_target_map)
        cross_campaign_details_lag = bool(
            marker_context is not None
            and details_context is not None
            and marker_context.campaign != details_context.campaign
        )
        if cross_campaign_details_lag:
            self._record_context_evidence_rejection(
                "cross_campaign_save_details_lag",
                marker_context.identity,
                details_context.identity,
            )
        mission_select_required = self.save_observer.requires_mission_select(
            active_map, continue_target_map,
            cross_campaign_details_lag=cross_campaign_details_lag,
            challenge_maps=MISSION_CHALLENGE_RUNTIME_MAPS,
        )
        if lease is not None:
            try:
                evidence_mtime_ns = Path(GAMEPLAY_SAVE_EVIDENCE_PATH).stat().st_mtime_ns
            except OSError:
                evidence_mtime_ns = marker["mtime_ns"] if marker else 0
            live, reason = (False, "mission_select_required") if mission_select_required else lease.validate(
                evidence_mtime_ns=evidence_mtime_ns,
                evidence_state=evidence.state if evidence else "gameplay",
                current_map=active_map,
            )
            mission_select_live = False
            if not live:
                if mission_select_required:
                    mission_select_live, reason = lease.validate_mission_select(
                        evidence_mtime_ns=evidence_mtime_ns or (marker["mtime_ns"] if marker else 0),
                        evidence_state=evidence.state if evidence else "gameplay",
                        current_map=active_map,
                        mission_map=active_map,
                        save_mtime_ns=selected.mtime_ns,
                    )
                if mission_select_live:
                    if self.save_observer.accept_mission_select(active_map, lease.gameplay_loaded_ns):
                        logger.info(
                            "[OBSERVER] MISSION_SELECT_LEASE_ACCEPTED slot=%s map=%s "
                            "load_epoch=%s save_mtime_ns=%s",
                            selected.slot_directory,
                            active_map,
                            lease.gameplay_loaded_ns,
                            selected.mtime_ns,
                        )
                else:
                    self.save_observer.clear_mission_select()
            elif live:
                self.save_observer.clear_mission_select()
            if not live and not mission_select_live:
                if reason != self.last_observer_lease_block:
                    logger.info("[OBSERVER] LIVE_LEASE_BLOCKED reason=%s", reason)
                    self.last_observer_lease_block = reason
                return fail_proof(reason)
            self.last_observer_lease_block = None

        proof = self.save_observer.plan_proof(
            selected, proof_evidence_epoch=proof_evidence_epoch, evidence_epoch=evidence_epoch,
        )

        details_token = details.get("_mtime_ns", selected.mtime_ns)

        if proof.action == "reject":
            return fail_proof(proof.reason)

        if proof.action == "activate":

            # SAVE_PROOF_ACCEPTED MUST precede SAVE_SLOT_ACTIVE
            self.log_save_proof_accepted(
                selected.slot_directory,
                active_map,
                proof_evidence_epoch,
                selected.mtime_ns,
                details_token,
                proof=(
                    "provisional_cross_campaign_load_edge"
                    if provisional_family_switch
                    else "non_provisional_fresh_map_match"
                ),
            )
            self.activate_save_selection(selected)
            self.save_observer.update_selection(native_evidence_epoch=proof_evidence_epoch)
            self.save_observer.accept_proof(selected.slot_directory, proof_evidence_epoch, proof_load_epoch)
            if provisional_family_switch:
                evidence_mtime = gameplay_evidence_mtime_ns()
                materialization_epoch = build_materialization_epoch(
                    evidence_epoch, evidence_mtime
                )
                if lease is not None:
                    lease.observe_gameplay_loaded(evidence_mtime)
                marker_data = self.runtime_lifecycle.new_map_proposal(
                    _catalog_map_key(active_map), active_map, evidence_epoch,
                    evidence_mtime, materialization_epoch,
                )
                self.accept_map_identity(marker_data, evidence_epoch)
                self.snapshot_fast_travel_eligibility(marker_data=marker_data)
                self.advance_automap_cleanup_epoch()
                logger.info(
                    "[MAP] MAP_TRANSITION_EVIDENCE map=%s epoch=%s runtime_map=%s "
                    "source=provisional_cross_campaign_load_edge",
                    marker_data["map_key"], materialization_epoch, active_map,
                )
            self.arm_final_sin_completion_candidate(
                selected, details, active_map, proof_load_epoch
            )
            self.reconcile_fast_travel_unlock("save_proof")
            return selected
        else:
            if not self.mission_select_observation_map:
                self.save_observer.clear_mission_select()
            if proof.new_evidence:
                self.log_save_proof_accepted(
                    selected.slot_directory,
                    active_map,
                    evidence_epoch,
                    selected.mtime_ns,
                    details_token,
                    proof="non_provisional_fresh_map_match",
                )
                if proof.reset_observation_slot:
                    self.invalidate_save_observation_slot(selected.slot_directory)
                self.save_observer.update_selection(native_evidence_epoch=evidence_epoch, token=selected.mtime_ns)

            self.save_observer.accept_proof(selected.slot_directory, proof_evidence_epoch, proof_load_epoch)
            self.arm_final_sin_completion_candidate(
                selected, details, active_map, proof_load_epoch
            )
            self.reconcile_fast_travel_unlock("save_proof")
            return selected

    def observe_active_game_details(self):
        selected = self.update_save_slot_lifecycle()
        return read_game_details_for_selection(selected) if selected else None

    def get_ap_state_key(self):
        if not getattr(self, "server", None) or not getattr(self, "auth", None) or not getattr(self, "state_key", None):
            return None
        effective_seed_name = getattr(self, "room_seed_name", None) or getattr(self, "seed_name", None) or "unknown_seed"
        team = getattr(self, "team", 0)
        slot = getattr(self, "slot", 0)
        auth = str(self.auth or "unknown_auth")
        # Bridge builds share one durable AP session. Runtime revision belongs
        # in diagnostics, not receipt-session identity.
        return f"{effective_seed_name}:{team}:{slot}:{auth}"

    def migrate_revision_bound_session(self, sessions, state_key):
        """Adopt one pre-stable revision-bound session without trusting it blindly."""
        candidates = sorted(
            key for key in sessions
            if isinstance(key, str)
            and key.startswith(f"{state_key}:")
            and isinstance(sessions.get(key), dict)
        )
        if not candidates:
            return None

        selected = candidates[-1]
        if len(candidates) > 1 and self.items_received:
            compatible = []
            for candidate in candidates:
                session = sessions[candidate]
                history = session.get("receipt_history")
                processed = session.get("processed_items", 0)
                ok, _ = validate_receipt_history_prefix(
                    self.items_received, processed, history
                )
                if ok:
                    compatible.append(candidate)
            if len(compatible) == 1:
                selected = compatible[0]
            else:
                log_item_event(
                    "ITEM_STATE_INCOMPATIBLE",
                    reason="multiple_revision_sessions",
                    state_key=state_key,
                    candidates=candidates,
                    compatible=compatible,
                    boundary_before=0,
                    boundary_after=0,
                    success=False,
                    bridge_revision=BRIDGE_REVISION,
                )

        sessions[state_key] = sessions.pop(selected)
        log_item_event(
            "ITEM_STATE_MIGRATION",
            reason="bridge_revision_removed_from_session_identity",
            from_state_key=selected,
            state_key=state_key,
            boundary_before=sessions[state_key].get("processed_items", 0),
            boundary_after=sessions[state_key].get("processed_items", 0),
            session_count=1,
            success=True,
            bridge_revision=BRIDGE_REVISION,
        )
        return selected

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
        self.reset_queue_session_authority("initialize_item_state")
        initialization_boundary_before = getattr(self, "items_processed", 0)
        previous_state_key = self.state_key
        self.receipt_session.begin_rebind()
        self.client_state = load_client_state()
        effective_seed_name = self.room_seed_name or self.seed_name
        if (
            not isinstance(effective_seed_name, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", effective_seed_name)
            or isinstance(self.team, bool)
            or not isinstance(self.team, int)
            or self.team < 0
            or isinstance(self.slot, bool)
            or not isinstance(self.slot, int)
            or self.slot < 0
        ):
            logger.warning(
                "[State] Slot identity incomplete or unsafe; refusing session state reuse."
            )
            self.state_key = ""
            self.session_state = default_session_state()
            self.receipt_session.restore_boundary(0)
            self.item_state_ready = False
            self.receipt_session.clear_packet_ranges()
            log_item_event(
                "ITEM_STATE_SESSION_INIT",
                state_key="unresolved",
                state_key_status="unresolved",
                reason="invalid_slot_identity",
                boundary_before=initialization_boundary_before,
                boundary_after=0,
                received_count=len(self.items_received),
                processed_count=0,
                success=False,
            )
            return
        sessions = self.client_state["sessions"]
        self.state_key, migrated_from = migrate_legacy_session_key(
            sessions,
            seed_name=effective_seed_name,
            team=self.team,
            slot=self.slot,
        )
        if previous_state_key != self.state_key:
            self.receipt_session.clear_packet_ranges()
        if self.state_key is None:
            logger.warning(
                "[State] Slot identity incomplete or unsafe; refusing session state reuse."
            )
            self.state_key = ""
            self.session_state = default_session_state()
            self.receipt_session.restore_boundary(0)
            self.item_state_ready = False
            self.receipt_session.clear_packet_ranges()
            log_item_event(
                "ITEM_STATE_SESSION_INIT",
                state_key="unresolved",
                state_key_status="unresolved",
                reason="session_key_unresolved",
                boundary_before=initialization_boundary_before,
                boundary_after=0,
                received_count=len(self.items_received),
                processed_count=0,
                success=False,
            )
            return
        if self.state_key not in sessions:
            self.migrate_revision_bound_session(sessions, self.state_key)
        if migrated_from is not None:
            migrated_session = sessions.get(self.state_key)
            migrated_boundary = (
                migrated_session.get("processed_items", 0)
                if isinstance(migrated_session, dict)
                else 0
            )
            log_item_event(
                "ITEM_STATE_MIGRATION",
                from_state_key=migrated_from,
                state_key=self.state_key,
                reason="legacy_session_identity",
                boundary_before=initialization_boundary_before,
                boundary_after=migrated_boundary,
                session_count=len(sessions),
                processed_count=migrated_boundary,
                success=True,
            )
            logger.info(
                "[State] STATE_MIGRATED from=%s to=%s reason=legacy_session",
                migrated_from,
                self.state_key,
            )
        existing_session = sessions.get(self.state_key)
        if not isinstance(existing_session, dict):
            existing_session = default_session_state()
        self.session_state = normalize_session_state(existing_session)
        self.goals.bind(self.state_key, self.session_state)
        self.save_checks.bind()
        self.receipt_delivery.bind(self.session_state)
        self.publisher_dispatch.bind(self.session_state.setdefault("publisher_acknowledgements", {}))
        self.save_observer.bind_baselines(SaveObserverBaselineStore(self.session_state))
        self.materialization.bind(
            self.session_state.setdefault("context_materialization", {}), self.session_state["item_resync"],
        )
        self.session_state.setdefault("cultist_autosave_path", None)
        self.session_state.setdefault("save_slot_observations", {})
        self.checked_visuals.rebind()
        self.fast_travel.rebind()
        self.session_state.pop("automap_cleanup", None)
        self.session_state.pop("fast_travel_delivered", None)
        sessions[self.state_key] = self.session_state
        self.receipt_session.restore_boundary(self.session_state["processed_items"])
        # Process restarts preserve bounded event identities while lethal
        # transport state begins fresh for the new connection.
        self.session_state.pop("deathlinked", None)
        self.deathlink.bind(self.state_key, self.session_state)
        raw_save_observations = self.session_state.get("save_slot_observations", {})
        self.session_state["save_slot_observations"] = self.save_observer.restore_slot_observations(raw_save_observations)
        self.session_state.pop("sticky_mastery_observed", None)
        self.session_state.pop("weapon_masteries_observed", None)
        self.item_state_ready = True
        self.reconnect_resync_attempted = False
        self.bootstrap.bind(self.session_state["bootstrap"])
        self.runes.bind(self.session_state.setdefault("rune_reconciliation", {}), self.session_state["perk_reconciliation"])
        try:
            save_client_state(
                self.client_state,
                reason="item_state_initialize",
                boundary=self.items_processed,
                boundary_before=initialization_boundary_before,
            )
        except Exception as error:
            log_item_event(
                "ITEM_STATE_SESSION_INIT",
                state_key=self.state_key,
                state_key_status="resolved",
                reason="state_save_failed",
                detail=str(error),
                boundary_before=initialization_boundary_before,
                boundary_after=self.items_processed,
                received_count=len(self.items_received),
                processed_count=self.items_processed,
                success=False,
            )
            raise
        log_item_event(
            "ITEM_STATE_SESSION_INIT",
            state_key=self.state_key,
            state_key_status="resolved",
            reason="initialized",
            boundary_before=initialization_boundary_before,
            boundary_after=self.items_processed,
            received_count=len(self.items_received),
            processed_count=self.items_processed,
            success=True,
        )
        logger.info(
            f"[State] Loaded {self.items_processed} processed items for "
            f"{self.state_key}."
        )
        if not ensure_queue_session_namespace(self.state_key):
            logger.error(
                "[Queue] Refusing queue authority because session namespace publish failed."
            )
            return
        quarantine_incompatible_receipt_jobs(self.state_key)
        self._queue_session_authoritative = True
        self.check_and_update_event_session()

    def persist_session_state(self):
        if not self.item_state_ready:
            return
        client_state_store().commit_session(
            self.client_state, self.session_state, processed_items=self.items_processed,
            cultist_autosave_path=self.cultist_autosave_path,
            received_deathlink_event_ids=self.deathlink.seen_events,
            save_slot_observations=self.save_observer.observation_document,
        )

    def reset_item_state(self):
        self.reset_transient_effects("item_state_reset")
        boundary_before = self.items_processed
        self.receipt_session.restore_boundary(0)
        reset_receipt_history(self.session_state)
        self.materialization.reset_automatic()
        self.runes.reset()
        self.receipt_delivery.reset(ITEM_MAPPING_REVISION)
        self.reconnect_resync_attempted = False
        try:
            save_client_state(
                self.client_state,
                reason="item_state_reset",
                boundary=self.items_processed,
                boundary_before=boundary_before,
            )
        except Exception as error:
            log_item_event(
                "ITEM_STATE_RESET",
                state_key=getattr(self, "state_key", None),
                reason="state_save_failed",
                detail=str(error),
                boundary_before=boundary_before,
                boundary_after=self.items_processed,
                received_count=len(self.items_received),
                processed_count=self.items_processed,
                success=False,
            )
            raise
        log_item_event(
            "ITEM_STATE_RESET",
            state_key=getattr(self, "state_key", None),
            reason="explicit_reset",
            boundary_before=boundary_before,
            boundary_after=self.items_processed,
            received_count=len(self.items_received),
            processed_count=self.items_processed,
            success=True,
        )

    @property
    def items_processed(self):
        return self.receipt_session.processed_boundary

    def received_rune_count(self):
        return sum(item_id in REVISION_ONE_RUNE_IDS for item_id in receipt_item_ids(self.items_received))

    def received_item_ids(self, processed_only=False):
        items = self.items_received[: self.items_processed] if processed_only else self.items_received
        return frozenset(receipt_item_ids(items))

    def _processed_receipt_ids(self):
        return processed_receipt_counts(self.session_state)

    def _record_processed_receipt(self, network_item):
        return record_processed_receipt(self.session_state, network_item)

    def validate_item_history_prefix(self):
        compatible, detail = validate_session_receipt_prefix(
            self.session_state,
            self.items_received,
            self.items_processed,
        )
        log_item_event(
            "ITEM_HISTORY_COMPATIBILITY",
            status="compatible" if compatible else "incompatible",
            reason=detail,
            boundary=self.items_processed,
            received_count=len(self.items_received),
            history_fingerprint=receipt_history_fingerprint(self.items_received),
            state_key=getattr(self, "state_key", None),
            bridge_revision=BRIDGE_REVISION,
            mapping_revision=ITEM_MAPPING_REVISION,
        )
        if not compatible:
            raise ValueError(f"received-item history is incompatible: {detail}")
        return True

    def observe_received_item_history(self):
        """Record authoritative ownership summary without delivering effects."""
        self.validate_item_history_prefix()
        observation = observe_received_items(
            self.items_received,
            self.items_processed,
            self._processed_receipt_ids(),
        )
        changed = project_receipt_history(
            self.session_state, self.items_received, self.items_processed, observation,
        )
        if changed:
            self.persist_session_state()
        return observation

    def _reconciliation_eligibility(self, *, require_connection):
        socket = getattr(getattr(self, "server", None), "socket", None)
        block = reconciliation_session_block(
            item_ready=self.item_state_ready, require_connection=require_connection,
            connected=socket is not None and not socket.closed,
            team=getattr(self, "team", None), slot=getattr(self, "slot", None),
            seed=getattr(self, "room_seed_name", None) or getattr(self, "seed_name", None),
        )
        if block:
            return None, block
        evidence = read_gameplay_save_evidence()
        gameplay = evidence is not None and evidence.state == "gameplay"
        marker = self.read_active_map_identity(evidence=evidence) if gameplay else None
        context = classify_runtime_context(marker["runtime_map"]) if marker else None
        block = reconciliation_runtime_block(
            evidence=evidence, marker=marker,
            runtime_ready=bool(marker and self.runtime_effects_ready(evidence)),
            campaign=getattr(context, "campaign", None), active_slot=self.active_save_slot,
            active_epoch=self.active_native_evidence_epoch,
            active_map=canonical_map_name(marker["runtime_map"]) if marker else None,
            supported_maps={canonical_map_name(name) for name in load_foundation_contracts()["active_maps"].values()},
            boundary=self.items_processed, history_length=len(self.items_received),
        )
        return (None, block) if block else (evidence, None)

    def apply_reconciliation_plan(
        self,
        plan,
        *,
        intent=RECONCILIATION_REPAIR,
        reason="manual",
        materialization_lease=None,
        context_identity=None,
    ):
        """Queue silent reconcile commands through one manual/automatic path."""
        return reconciliation_publisher().publish(
            plan, state_key=self.state_key, intent=intent, reason=reason,
            materialization_lease=materialization_lease, context_identity=context_identity,
        )

    def _manual_reconcile_inventory_unlocked(self):
        """Queue current-context persistent ownership without mutating AP receipt state."""
        evidence, error = self._reconciliation_eligibility(require_connection=True)
        if error:
            return None, error
        try:
            plan, error = self._context_materialize_inventory(
                evidence, trigger="manual", manual=True
            )
        except ValueError as error:
            log_item_event(
                "ITEM_RECONCILIATION_BLOCKED",
                reason="history_incompatible",
                detail=str(error),
                boundary=self.items_processed,
                state_key=getattr(self, "state_key", None),
            )
            return None, str(error)
        if error:
            return None, error
        if plan is None:
            return None, "current context materialization returned no plan"

        logger.info(
            "RESYNC_START reason=manual context=%s epoch=%s boundary=%s",
            self.context_identity,
            evidence.epoch,
            self.items_processed,
        )
        logger.info(
            "RESYNC_HISTORY reason=manual receipts=%s boundary=%s fingerprint=%s",
            self.items_processed,
            self.items_processed,
            receipt_history_fingerprint(self.items_received),
        )
        logger.info(
            "RESYNC_PLAN reason=manual commands=%s replayed=%s special_stages=%s "
            "skipped_never_replay=%s skipped_manual_replay=%s",
            len(plan.commands),
            plan.replayed,
            plan.special_stages,
            plan.skipped_never_replay,
            plan.skipped_manual_replay,
        )
        if not plan.commands:
            logger.info("RESYNC_NOOP reason=manual detail=no_commands")
        logger.info(
            "RESYNC_QUEUED reason=manual commands=%s status=%s",
            len(plan.commands),
            "noop" if not plan.commands else "queued",
        )
        return plan, None

    def manual_reconcile_inventory(self):
        return self._manual_reconcile_inventory_unlocked()

    async def manual_reconcile_inventory_async(self):
        async with self._item_delivery_lock:
            return self._manual_reconcile_inventory_unlocked()

    def automatic_reconcile_inventory(self, reason):
        """Run one guarded resync for a new lifecycle/history fingerprint."""
        if reason in {"reconnect", "level_ready"}:
            self.materialization.trigger(reason)
            return None, None
        fingerprint = receipt_history_fingerprint(self.items_received)
        evidence, error = self._reconciliation_eligibility(require_connection=True)
        if error:
            self.materialization.log_automatic_noop(
                reason,
                error,
                getattr(self, "active_native_evidence_epoch", None),
                fingerprint,
            )
            return None, error
        context = self._refresh_runtime_context(
            getattr(self, "_connected_slot_data", {}), evidence
        )
        if context is None or context.identity == "unknown":
            return None, "active runtime context is unrecognized"
        if (
            context.campaign != "Base"
            and not getattr(self, "_connected_slot_data", {}).get("use_dlc_content")
        ):
            self.materialization.log_automatic_noop(reason, "dlc_context_ignored", evidence.epoch, fingerprint)
            return None, None
        materialization_lease = self._active_materialization_lease(context)
        if materialization_lease is None:
            self.materialization.log_automatic_noop(
                reason,
                "active context has no materialization lease",
                evidence.epoch,
                fingerprint,
            )
            return None, "active context has no materialization lease"
        if self.materialization.automatic_already_applied(evidence.epoch, fingerprint):
            self.materialization.log_automatic_noop(
                reason,
                "already_applied",
                evidence.epoch,
                fingerprint,
            )
            return None, None

        logger.info(
            "RESYNC_START reason=%s lease=%s save_epoch=%s boundary=%s",
            reason,
            materialization_lease,
            evidence.epoch,
            self.items_processed,
        )
        logger.info(
            "RESYNC_HISTORY reason=%s receipts=%s boundary=%s fingerprint=%s",
            reason,
            self.items_processed,
            self.items_processed,
            fingerprint,
        )
        try:
            observation = observe_received_items(
                self.items_received,
                self.items_processed,
                self._processed_receipt_ids(),
            )
            self.validate_item_history_prefix()
            scope = MaterializationScope(
                self.room_seed_name or getattr(self, "seed_name", None), self.team, self.slot, self.state_key,
                materialization_lease, evidence.epoch, reason, str(self.get_ap_state_key() or "unbound"),
            )
            plan = compile_automatic_plan(
                observation.historical_authoritative_item_ids, context, scope,
                ITEM_ID_TO_COMMAND, ITEM_REPLAY_POLICIES,
            )
        except ValueError as error:
            self.materialization.record_automatic_failure(reason, evidence.epoch, fingerprint)
            self.persist_session_state()
            log_item_event(
                "ITEM_RECONCILIATION_BLOCKED",
                reason="history_incompatible",
                detail=str(error),
                boundary=self.items_processed,
                state_key=getattr(self, "state_key", None),
                trigger=reason,
            )
            self.materialization.log_automatic_noop(reason, error, evidence.epoch, fingerprint)
            return None, str(error)

        logger.info(
            "RESYNC_PLAN reason=%s commands=%s replayed=%s special_stages=%s "
            "skipped_never_replay=%s skipped_manual_replay=%s",
            reason,
            len(plan.commands),
            plan.replayed,
            plan.special_stages,
            plan.skipped_never_replay,
            plan.skipped_manual_replay,
        )
        queued, error = self.apply_reconciliation_plan(
            plan,
            reason=reason,
            materialization_lease=materialization_lease,
            context_identity=context.identity,
        )
        if not queued:
            self.materialization.record_automatic_failure(reason, evidence.epoch, fingerprint)
            self.persist_session_state()
            self.materialization.log_automatic_noop(reason, error, evidence.epoch, fingerprint)
            return None, error

        status = self.materialization.record_automatic_success(
            plan, reason, evidence.epoch, fingerprint, self.items_processed,
        )
        self.persist_session_state()
        if status == "noop":
            self.materialization.log_automatic_noop(
                reason, "no_commands", evidence.epoch, fingerprint
            )
        logger.info(
            "RESYNC_COMPLETE reason=%s commands=%s status=%s",
            reason,
            len(plan.commands),
            status,
        )
        return plan, None

    def reconciliation_epoch(self):
        return self.runes.epoch

    def advance_reconciliation_epoch(self, trigger):
        epoch = self.runes.advance(trigger)
        self.persist_session_state()
        return epoch

    def observe_rune_native_state(self):
        """Read distinct Rune surfaces only from lifecycle-proven active save."""
        if not self.has_authoritative_save_proof() or not self.active_save_slot:
            return None, "authoritative active-save proof required"
        lease = getattr(self, "runtime_observation_lease", None)
        evidence_epoch = getattr(lease, "gameplay_loaded_ns", None)
        if evidence_epoch is None:
            evidence_epoch = self.active_native_evidence_epoch or "unknown"
        return RuneNativeState.from_game_details(
            self.observe_active_game_details(),
            save_slot=self.active_save_slot,
            evidence_epoch=evidence_epoch,
        ), None

    def observe_owned_rune_plan(self):
        if not self.item_state_ready:
            return None, "item state is not ready"
        native, error = self.observe_rune_native_state()
        if error:
            return None, error
        try:
            plan = self.runes.compile(
                self.received_item_ids(processed_only=True), native, ITEM_ID_TO_COMMAND, REVISION_ONE_RUNE_IDS,
            )
        except ValueError as error:
            return None, str(error)
        return plan, None

    def reconcile_owned_runes(self, trigger, *, force=False):
        plan, error = self.observe_owned_rune_plan()
        if error:
            logger.info("RUNE_RECONCILE_NOOP trigger=%s detail=%s", trigger, error)
            return None, error
        seed = self.room_seed_name or getattr(self, "seed_name", None)
        return self.runes.reconcile(
            plan, trigger, slot_identity=f"{seed}-{self.team}-{self.slot}", state_key=self.state_key,
            publisher=reconciliation_publisher(), persist=self.persist_session_state, force=force,
        )

    def observe_rune_diagnostic_lines(self):
        native, error = self.observe_rune_native_state()
        authority = "authoritative"
        if error:
            candidate = self.observe_active_game_details()
            if not isinstance(candidate, dict):
                return [
                    "Rune diagnostic unavailable: "
                    f"authority=observational repair_allowed=no reason={error}"
                ]
            native = RuneNativeState.from_game_details(
                candidate,
                save_slot=self.active_save_slot or getattr(self, "selected_observation_slot", None) or "candidate",
                evidence_epoch=getattr(self, "active_native_evidence_epoch", None) or "unknown",
            )
            authority = "observational"
        plan, plan_error = self.observe_owned_rune_plan()
        owned_perks = (
            ", ".join(sorted(entry.perk for entry in plan.entries)) or "-"
            if plan is not None
            else f"unavailable ({plan_error})"
        )
        slots = tuple(native.equipped_slots) + (None, None, None)
        lines = [
            f"Rune authority={authority} repair_allowed={'yes' if authority == 'authoritative' else 'no'} "
            f"reason={plan_error or error or 'active-save proof'} slot={native.save_slot} "
            f"epoch={native.evidence_epoch} map={self.current_map_name or '-'}",
            f"AP-owned Rune perks: {owned_perks} | "
            f"available: {', '.join(sorted(native.available_perks)) or '-'} | "
            f"active: {', '.join(sorted(native.active_perks)) or '-'} | "
            f"registered: {', '.join(sorted(native.registered_runes)) or '-'}",
            f"Rune slots: 0={slots[0] or '-'} | 1={slots[1] or '-'} | "
            f"2={slots[2] or '-'} | "
            f"page={native.page_unlocked if native.page_unlocked is not None else 'unknown'} | "
            f"active_save={native.save_slot} | epoch={native.evidence_epoch}",
        ]
        if plan_error:
            lines.append(f"Plan: blocked ({plan_error})")
        else:
            lines.append(
                f"Plan: {plan.status}; noops={len(plan.noops)} "
                f"repair_candidates={len(plan.repairs)}"
            )
        return lines

    @property
    def automap_cleanup_epoch(self):
        return self.checked_visuals.epoch

    @property
    def automap_cleanup_status(self):
        return self.checked_visuals.status

    def advance_automap_cleanup_epoch(self):
        marker = self.runtime_lifecycle.map_identity.cached_marker
        return self.checked_visuals.advance_epoch(marker.get("gameplay_epoch") if marker else None)

    @property
    def fast_travel_epoch_state(self):
        return self.fast_travel.epoch_state

    @property
    def fast_travel_eligibility_snapshot(self):
        return self.fast_travel.eligibility

    def reconcile_fast_travel_unlock(self, trigger):
        return self.fast_travel.reconcile(
            trigger, map_identity=self.runtime_lifecycle.map_identity,
            room_identity=self.get_ap_state_key(), state_key=self.state_key,
            runtime_ready=self.runtime_effects_ready(), item_ready=self.item_state_ready,
            rpc_ready=rpc_execution_enabled(), send=send_command,
        )

    def snapshot_fast_travel_eligibility(self, marker_data=None, *, refresh=False):
        return self.fast_travel.capture(
            self.runtime_lifecycle.map_identity, self.get_ap_state_key(),
            getattr(self, "checked_locations", None), marker_data, refresh=refresh,
        )

    async def process_level_ready(self, newest_path=None):
        """Adapt native observations and apply the ordered effects of an accepted job."""
        evidence = read_gameplay_save_evidence()
        self.read_active_map_identity(evidence=evidence)
        self.snapshot_fast_travel_eligibility()
        marker = self.cached_map_identity
        epoch = marker.get("gameplay_epoch") if isinstance(marker, Mapping) else None
        if not self.level_ready.can_start(epoch):
            return False
        if not self.level_ready.observe_readiness(epoch, self.runtime_effects_ready(evidence), getattr(evidence, "state", None)):
            return False
        slot_data = getattr(self, "_connected_slot_data", {})
        active_context = self._refresh_runtime_context(slot_data)
        use_dlc = slot_data.get("use_dlc_content")
        admission = self.level_ready.start(
            epoch, active_context, use_dlc, self.dlc_evidence if use_dlc else None,
            newest_path or marker.get("path"),
        )
        if admission.job is None:
            self.materialization.block(admission.block)
            return False
        job = admission.job
        receipt_token = self.receipt_session.capture(self.state_key)
        try:
            if not rpc_execution_enabled():
                set_rpc_execution(True)
            if job.settle_inventory:
                logger.info("[Context] TAG_INVENTORY_SETTLE epoch=%s delay_ms=%s",
                            epoch, int(TAG_INVENTORY_SETTLE_SECONDS * 1000))
                await asyncio.sleep(TAG_INVENTORY_SETTLE_SECONDS)
                if not self.receipt_session.is_current(receipt_token, self.state_key) or not self.level_ready.is_current(job):
                    return False
                evidence = read_gameplay_save_evidence()
                self.read_active_map_identity(evidence=evidence)
                if not self.level_ready.observe_resume(
                    job, self.runtime_lifecycle.map_identity, self.runtime_effects_ready(evidence), settled=True,
                ):
                    return False
            reconciliation_epoch = self.advance_reconciliation_epoch("level_ready")
            logger.info(
                "[RPC] Level-ready signal received (%s). RPC armed; "
                "perk reconciliation epoch %s queued behind native safety gate.",
                os.path.basename(job.source_path) if job.source_path else "<marker>", reconciliation_epoch,
            )
            self.reconcile_owned_runes("level_ready")
            self.advance_automap_cleanup_epoch()
            self.reconcile_checked_automap_cleanup("level_ready")
            await self.check_mission_challenge_locations()
            if not self.receipt_session.is_current(receipt_token, self.state_key) or not self.level_ready.is_current(job):
                return False
            evidence = read_gameplay_save_evidence()
            self.read_active_map_identity(evidence=evidence)
            if not self.level_ready.observe_resume(job, self.runtime_lifecycle.map_identity, self.runtime_effects_ready(evidence)):
                return False
            _, context_error = self._context_materialize_inventory(evidence, trigger="level_ready")
            if context_error == "materialization queued; awaiting native application":
                logger.info("[Context] LEVEL_READY_PENDING reason=native_materialization_pending")
                return False
            if context_error:
                logger.info("[Context] LEVEL_READY_PENDING reason=%s", context_error)
                return False
            self.reconcile_fast_travel_unlock("level_ready")
            self.level_ready.complete(job)
            return True
        finally:
            self.level_ready.finish(job)

    @property
    def pending_level_ready(self):
        return self.level_ready.pending

    @property
    def completed_level_ready_epochs(self):
        return self.level_ready.completed


    def reconcile_checked_automap_cleanup(self, trigger):
        return self.checked_visuals.reconcile(
            trigger, map_identity=self.runtime_lifecycle.map_identity,
            room_identity=self.get_ap_state_key(), state_key=self.state_key,
            checked=getattr(self, "checked_locations", None),
            checked_ready=self.server_checked_locations_ready,
            runtime_ready=self.runtime_effects_ready(), rpc_ready=rpc_execution_enabled(), send=send_command,
        )

    def record_local_automap_cleanup_ownership(self, location_id, event_paths):
        try:
            event_mtime = max(Path(path).stat().st_mtime_ns for path in event_paths)
        except (OSError, ValueError):
            return False
        return self.checked_visuals.record_local_ownership(
            location_id, map_identity=self.runtime_lifecycle.map_identity,
            room_identity=self.get_ap_state_key(), event_mtime=event_mtime,
        )

    def bootstrap_actions(self):
        return self.bootstrap.actions

    def bootstrap_action_state(self, action_name, revision=None):
        return self.bootstrap.action_state(action_name, revision)

    def _bootstrap_ownership(self):
        return BootstrapOwnership(frozenset(self.received_item_ids()), self.received_rune_count() > 0)

    def bootstrap_eligible(self, action_name):
        return self.bootstrap.eligible(action_name, self._bootstrap_ownership())

    def bootstrap_ineligibility_reason(self, action_name):
        return self.bootstrap.ineligibility_reason(action_name, self._bootstrap_ownership())

    def enqueue_bootstrap(self, action_name, trigger):
        return self.bootstrap.enqueue(
            action_name, trigger, ownership=self._bootstrap_ownership(), current_map=self.current_map_name,
            state_key=self.state_key, spool=command_spool(), persist=self.persist_session_state,
        )

    def onboard_bootstrap(self, trigger):
        self.bootstrap.onboard(
            trigger, ownership=self._bootstrap_ownership(), map_identity=self.runtime_lifecycle.map_identity,
            state_key=self.state_key, item_ready=self.item_state_ready, rpc_ready=rpc_execution_enabled(),
            spool=command_spool(), persist=self.persist_session_state,
        )

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

    def _persist_receipt_progress(self, reason):
        save_client_state(self.client_state, reason=reason, boundary=self.items_processed)

    def repair_item_mappings(self):
        repair = self.receipt_delivery.next_mapping_repair(receipt_item_ids(self.items_received), self.items_processed,
            ITEM_MAPPING_REVISION, {1: REVISION_ONE_RUNE_IDS, 2: REVISION_TWO_SUIT_IDS,
                4: REVISION_FOUR_FLAME_BELCH_IDS, 5: REVISION_FIVE_EQUIPMENT_LAUNCHER_IDS})
        if repair.action == "done":
            return True
        if repair.action == "deferred":
            return False
        if repair.action == "complete":
            self.receipt_delivery.finish_mapping_repair(ITEM_MAPPING_REVISION, self._persist_receipt_progress)
            return True
        spooled, description = self.spool_item_commands(repair.item_id, repair.item_index, intent=PRESENTATION_REPAIR)
        if spooled:
            self.receipt_delivery.record_mapping_repair(repair, description, self._persist_receipt_progress)
        return False

    def progressive_stage(self, item_id, item_index, *, excluded_receipt_indices=None):
        """Resolve finite progressive stage from effective authoritative ownership."""
        if (
            isinstance(item_index, bool)
            or not isinstance(item_index, int)
            or item_index < 0
        ):
            return 0
        if excluded_receipt_indices is None:
            if self.items_received:
                observation = self.observe_received_item_history()
                excluded_receipt_indices = {
                    receipt.index for receipt in observation.duplicates
                }
            else:
                excluded_receipt_indices = set()
        return progressive_receipt_stage(
            receipt_item_ids(self.items_received), item_id, item_index,
            frozenset(excluded_receipt_indices), ITEM_ID_TO_COMMAND.get(item_id),
        )

    def _fresh_receipt_owned_count(
        self, item_id, item_index, boundary=None, excluded_receipt_indices=None
    ):
        """Count only current authoritative receipts eligible for receipt feedback."""
        boundary = (
            getattr(self, "items_processed", 0)
            if boundary is None
            else boundary
        )
        if excluded_receipt_indices is None:
            excluded_receipt_indices = set()
        return fresh_receipt_owned_count(
            receipt_item_ids(self.items_received), item_id, item_index, boundary,
            frozenset(excluded_receipt_indices),
        )

    def item_activation_commands(self, item_id, item_index, *, intent=HISTORICAL_OWNERSHIP,
            include_notification=None, classification=None, fresh_receipt_boundary=None,
            excluded_receipt_indices=None, suppress_local_toast=True):
        request = ReceiptIntent(item_id, item_index, intent, include_notification, classification, suppress_local_toast)
        network_item = (self.items_received[item_index] if isinstance(item_index, int) and not isinstance(item_index, bool)
                        and 0 <= item_index < len(self.items_received) else None)
        facts = ReceiptFeedbackFacts(
            self._fresh_receipt_owned_count(item_id, item_index, fresh_receipt_boundary, excluded_receipt_indices),
            self.items_processed, getattr(network_item, "location", None), getattr(self, "server_checked_locations_ready", False),
            frozenset(getattr(self, "checked_locations", ()) or ()),
            frozenset(getattr(self, "locations_info", {}) or {}), self.location_setup.received_ids,
        )
        stage = (self.progressive_stage(item_id, item_index, excluded_receipt_indices=excluded_receipt_indices)
                 if requires_progressive_observation(request, ITEM_ID_TO_COMMAND) else None)
        plan = compile_receipt_plan(request, facts, stage, ITEM_ID_TO_COMMAND, ITEM_REPLAY_POLICIES, ITEM_CLASSIFICATIONS)
        return (list(plan.commands) if plan.commands is not None else None), plan.description

    def item_command_id(self, item_id, item_index, command_index, command):
        return receipt_command_id(self.state_key, item_id, item_index, command_index, command)

    def receipt_publication_scope(self, packet_received_ns=None, materialization_lease=None, context_identity=None):
        return ReceiptPublicationScope(self.state_key, self.current_map_name, self.active_save_slot, BRIDGE_REVISION,
            BRIDGE_PROTOCOL, packet_received_ns, materialization_lease, context_identity)

    def delivery_item_name(self, item_id):
        identity = ITEM_CLASSIFICATION_IDENTITY.get(item_id)
        if identity is not None:
            return identity["name"]
        return f"Unknown item (ID: {item_id})"

    def spool_deferred_receipt_notification(
        self,
        item_id,
        item_index,
        *,
        fresh_receipt_boundary=None,
        excluded_receipt_indices=None,
    ):
        """Queue local receipt feedback without queuing deferred gameplay effects."""
        if not ENABLE_ITEM_NOTIFICATIONS:
            return True, "item notifications disabled"
        if not ensure_queue_session_namespace(self.state_key):
            self.reset_queue_session_authority("notification_namespace_unavailable")
            return False, "active notification namespace unavailable"
        commands, description = self.item_activation_commands(
            item_id,
            item_index,
            intent=NEW_RECEIPT,
            include_notification=True,
            fresh_receipt_boundary=fresh_receipt_boundary,
            excluded_receipt_indices=excluded_receipt_indices,
            suppress_local_toast=False,
        )
        plan = ReceiptPlan(tuple(commands) if commands is not None else None, description)
        return self.receipt_delivery.deferred_notification(plan, item_id, item_index, self.delivery_item_name(item_id),
            self.receipt_publication_scope(), ReceiptPublication(send_command), self.persist_session_state)

    def spool_item_commands(
        self,
        item_id,
        item_index,
        *,
        intent=HISTORICAL_OWNERSHIP,
        include_notification=None,
        classification=None,
        packet_received_ns=None,
        materialization_lease=None,
        fresh_receipt_boundary=None,
        excluded_receipt_indices=None,
        context_identity=None,
    ):
        if getattr(self, "_queue_session_authoritative", self.item_state_ready) is False:
            return False, "queue session is not bound to current connection"
        if not ensure_queue_session_namespace(self.state_key):
            self.reset_queue_session_authority("namespace_publish_failed")
            return False, "active queue session namespace unavailable"

        commands, description = self.item_activation_commands(
            item_id,
            item_index,
            intent=intent,
            include_notification=include_notification,
            classification=classification,
            fresh_receipt_boundary=fresh_receipt_boundary,
            excluded_receipt_indices=excluded_receipt_indices,
        )
        plan = ReceiptPlan(tuple(commands) if commands is not None else None, description)
        return self.receipt_delivery.spool(plan, item_id, item_index, self.delivery_item_name(item_id),
            self.receipt_publication_scope(packet_received_ns, materialization_lease, context_identity),
            ReceiptPublication(send_command), self._persist_receipt_progress)

    @property
    def death_link_enabled(self):
        return self.deathlink.enabled

    @property
    def death_link_mode(self):
        return self.deathlink.mode

    def on_deathlink(self, data: dict):
        super().on_deathlink(data)
        event_id = self.deathlink.receive(data, self.persist_session_state)
        if event_id is None:
            return
        source = _bounded_event_text(str(data.get("source") or "Another player"), 128)
        cause = _bounded_event_text(str(data.get("cause") or ""), 512)
        emit_launcher_event(
            "deathlink",
            direction="received",
            event_id=event_id,
            source=source,
            cause=cause,
            message=cause or f"{source} sent you a DeathLink.",
        )


    def queue_received_deathlink(self):
        self.deathlink.advance(
            not self.runtime_observers_frozen and self.has_authoritative_save_proof(),
            deathlink_publication(),
        )

    async def check_game_duration_death(self):
        selected = self.update_save_slot_lifecycle()
        if not selected:
            # The durable-save path is available but deliberately frozen until
            # native gameplay evidence promotes a slot. Do not fall back to a
            # newest-mtime game.details reader while in menus.
            return True
        path = selected.path
        if self.save_observer.duration_is_current(selected):
            return True

        receipt_token = self.receipt_session.capture(self.state_key)
        observation_token = self.save_observer.capture_observation()

        def observation_current():
            return (
                self.receipt_session.is_current(receipt_token, self.state_key)
                and self.save_observer.observation_is_current(observation_token)
            )

        try:
            snapshot = await asyncio.to_thread(probe_game_duration, path)
        except Exception as error:
            if not observation_current():
                return True
            self.death_observer.probe_failed(error)
            return False

        if not observation_current():
            return True
        self.death_observer.probe_succeeded()
        self.save_observer.accept_duration(selected)
        self.observe_weapon_masteries(snapshot["mastery_records"], selected)
        self.observe_mission_challenges(
            snapshot["mission_challenge_records"], selected
        )
        marker = self.read_active_map_identity()
        event_identity = self.death_observer.observe_checkpoint(
            snapshot, selected, self.active_save_proof_load_epoch,
            marker["runtime_map"] if marker else "unknown",
        )
        if event_identity is None:
            return True
        self.reset_transient_effects("death_boundary")
        logger.info(
            "[DeathLink] LOCAL_DEATH_OBSERVED event_identity=%s",
            event_identity,
        )
        if not self.death_link_enabled:
            logger.info(
                "[DeathLink] DEATHLINK_OUTBOUND_SUPPRESSED reason=death_link_disabled"
            )
            return True

        await self.report_local_death()
        return True

    def save_check_observations(self):
        scope = SaveCheckBinding(
            str(self.room_seed_name or getattr(self, "seed_name", None) or self.state_key or "unknown"),
            int(getattr(self, "team", 0) or 0), int(getattr(self, "slot", 0) or 0), self.state_key,
            OBSERVER_REGISTRY_REVISION, frozenset(getattr(self, "checked_locations", set())),
            self.item_state_ready, self.client_state.get("sessions", {}).get(self.state_key) is self.session_state,
        )
        return SaveCheckObservations(self.save_observer, scope, self.persist_session_state, logger)

    def observe_weapon_masteries(self, records, path):
        slot_directory = self.observation_slot_for_source(path)
        self.select_save_observation_slot(slot_directory)
        self.save_checks.observe_masteries(records, path, slot_directory, self.save_check_observations(),
            authoritative=self.has_authoritative_save_proof())

    def observe_mission_challenges(self, records, path):
        slot_directory = self.observation_slot_for_source(path)
        self.select_save_observation_slot(slot_directory)
        self.save_checks.observe_challenges(records, path, slot_directory,
            self.mission_select_observation_map, self.mission_select_observation_epoch, self.save_check_observations(),
            authoritative=self.has_authoritative_save_proof())

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

    def save_check_readiness(self):
        return SaveCheckReadiness(self.item_state_ready, self.has_authoritative_save_proof(), self.active_save_slot)

    async def check_weapon_mastery_location(self, entry):
        await self.save_checks.check_mastery(entry, self.save_check_readiness(),
            self.check_observation(), self.check_publication())

    async def check_weapon_mastery_locations(self):
        token = self.receipt_session.capture(self.state_key)
        for entry in WEAPON_MASTERY_ENTRIES:
            await self.check_weapon_mastery_location(entry)
            if not self.receipt_session.is_current(token, self.state_key):
                return

    async def check_mission_challenge_location(self, entry):
        await self.save_checks.check_challenge(entry, self.save_check_readiness(),
            self.check_observation(), self.check_publication())

    async def check_mission_challenge_locations(self):
        self.ingest_visible_runtime_lifecycle()
        token = self.receipt_session.capture(self.state_key)
        for entry in MISSION_CHALLENGE_ENTRIES:
            await self.check_mission_challenge_location(entry)
            if not self.receipt_session.is_current(token, self.state_key):
                return
        await self.check_all_mission_challenges_location()

    async def check_all_mission_challenges_location(self):
        await self.save_checks.check_aggregates(self.save_check_readiness(),
            self.check_observation(), self.check_publication())

    async def check_sticky_mastery_location(self):
        """Sticky compatibility wrapper preserving its exact send contract."""
        await self.check_weapon_mastery_location(STICKY_MASTERY_ENTRY)

    async def check_game_details_death(self):
        details = self.observe_active_game_details()
        if self.death_observer.observe_details(details):
            logger.info("[DeathLink] LOCAL_DEATH_OBSERVED source=game.details path=%s", details.get("_path"))
            await self.report_local_death()

    async def report_local_death(self):
        await self.deathlink.report_local_death(self.auth, self.send_death, deathlink_publication())

    @staticmethod
    def goal_objective_ids(slot_data):
        return GOAL_POLICY.objective_ids(slot_data)

    @property
    def goal_dispatch_sent(self):
        return self.goals.sent

    @property
    def cultist_autosave_path(self):
        return self.goals.cultist_path

    def check_observation(self):
        return CheckObservation(
            frozenset(getattr(self, "checked_locations", None) or ()),
            frozenset(self.locations_checked), frozenset(self.server_locations),
            bool(getattr(self, "server_checked_locations_ready", False)),
            bool(self.server and self.server.socket and not self.server.socket.closed),
        )

    def check_publication(self):
        return CheckPublication(self.send_msgs, self.locations_checked.add, ClientStatus.CLIENT_GOAL)

    async def evaluate_campaign_goal(self, source_description):
        return await self.goals.evaluate(
            source_description, GOAL_POLICY.objective_ids(getattr(self, "_connected_slot_data", {})),
            DoomEternalContext.check_observation(self), DoomEternalContext.check_publication(self),
        )

    async def execute_publisher(self, publisher, trigger_strategy, source_description):
        self.publisher_dispatch.observe_protocol(DoomEternalContext.check_observation(self))
        return await self.publisher_dispatch.execute(
            publisher, trigger_strategy, source_description,
            DoomEternalContext.check_publication(self), self.persist_session_state,
        )

    async def send_mission_complete(self, location_id, source_description, report_goal=False):
        if report_goal:
            return await DoomEternalContext.evaluate_campaign_goal(self, source_description)
        self.publisher_dispatch.observe_protocol(DoomEternalContext.check_observation(self))
        return await self.publisher_dispatch.mission(
            location_id, source_description, DoomEternalContext.check_publication(self), self.persist_session_state,
        )

    async def send_campaign_goal(self, source_description):
        return await DoomEternalContext.evaluate_campaign_goal(
            self, source_description
        )

    async def check_campaign_goal_event(self):
        """Consume independent map files and native transition triggers."""
        receipt_token = self.receipt_session.capture(self.state_key)
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
            completed = []
            for publisher in publishers:
                completed.append(await DoomEternalContext.execute_publisher(
                    self,
                    publisher,
                    "map_event_file",
                    f"map event {filename}",
                ))
                if not self.receipt_session.is_current(receipt_token, self.state_key):
                    return observed
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
                if not self.receipt_session.is_current(receipt_token, self.state_key):
                    return observed
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
        self.goals.clear_candidate(reason)

    def arm_final_sin_completion_candidate(self, selected, details, runtime_map, load_epoch):
        self.goals.arm_candidate(selected, details, runtime_map, load_epoch)

    async def evaluate_final_sin_completion_candidate(self):
        candidate = self.goals.candidate
        if candidate is None:
            return False
        if not self.goals.candidate_slot_allowed(self.active_save_proof_authoritative, self.active_save_proof_slot):
            return False
        selected = primary_save_for_slot(candidate["slot"])
        details = read_game_details_for_selection(selected) if selected else None
        decision = self.goals.observe_candidate(selected, details)
        if decision == "publish":
            token = self.goals.candidate_token()
            publisher = next(item for item in PUBLISHERS if item.key == "final_sin_mission_complete")
            published = await DoomEternalContext.execute_publisher(
                self, publisher, "save_fallback", "Final Sin Mission Select completed edge",
            )
            if published:
                self.goals.acknowledge_candidate(token)
            return published
        if decision == "pending":
            lease = getattr(self, "runtime_observation_lease", None)
            if lease is not None and not lease.process_probe():
                self.goals.clear_candidate("game_process_ended")
        return False

    async def check_campaign_goal_save_fallback(self):
        receipt_token = self.receipt_session.capture(self.state_key)
        if await self.evaluate_final_sin_completion_candidate():
            return
        if not self.receipt_session.is_current(receipt_token, self.state_key):
            return
        marker = self.read_active_map_identity(evidence=read_gameplay_save_evidence())
        active_map = canonical_map_name(marker["runtime_map"]) if marker else ""
        if not active_map:
            return
        details = self.observe_active_game_details()
        if not details:
            return
        intent = self.goals.observe_completion(details, self.persist_session_state)
        if intent is not None:
            key, source = intent
            for publisher in (item for item in PUBLISHERS if item.key == key):
                await DoomEternalContext.execute_publisher(self, publisher, "save_fallback", source)

    async def check_campaign_goal(self):
        receipt_token = self.receipt_session.capture(self.state_key)
        if not self.item_state_ready:
            await self.check_campaign_goal_event()
            return

        await self.check_campaign_goal_event()
        if not self.receipt_session.is_current(receipt_token, self.state_key):
            return
        await self.check_campaign_goal_save_fallback()
        if not self.receipt_session.is_current(receipt_token, self.state_key):
            return
        await self.evaluate_campaign_goal("central objective evaluator")

    def check_rpc_autopause(self):
        evidence = read_gameplay_save_evidence()
        marker = self.read_active_map_identity(evidence=evidence)

        if marker is None:
            self.bootstrap.observe_supported_map(None)
            self.runtime_lifecycle.project_current_map(None)
            return
        map_name = marker["runtime_map"]

        self.runtime_lifecycle.project_current_map(marker)
        if getattr(evidence, "state", None) != "gameplay":
            return
        self.snapshot_fast_travel_eligibility()
        self.reconcile_fast_travel_unlock("map_ready")
        if self.bootstrap.observe_supported_map(map_name):
            self.onboard_bootstrap("on_supported_map_load")

    async def death_monitor_loop(self):
        while not self.exit_event.is_set():
            self.check_rpc_autopause()
            self.queue_received_deathlink()
            work_token = self.runtime_lifecycle.capture_work()
            used_duration = False
            if death_probe_available():
                used_duration = await self.check_game_duration_death()
            if not self.runtime_lifecycle.work_is_current(work_token) or self.exit_event.is_set():
                continue
            if not used_duration:
                await self.check_game_details_death()
            if not self.runtime_lifecycle.work_is_current(work_token) or self.exit_event.is_set():
                continue
            await self.check_weapon_mastery_locations()
            if not self.runtime_lifecycle.work_is_current(work_token) or self.exit_event.is_set():
                continue
            await self.check_mission_challenge_locations()
            if not self.runtime_lifecycle.work_is_current(work_token) or self.exit_event.is_set():
                continue
            await self.check_campaign_goal()
            if not self.runtime_lifecycle.work_is_current(work_token) or self.exit_event.is_set():
                continue
            self._consume_ammo_refill_request_file()
            sleep_duration = 0.05 if self.deathlink.receiving else 1.0
            await asyncio.sleep(sleep_duration)

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

        facts = self.check_observation()
        pending_locations = []
        for location_id, paths in event_paths_by_location.items():
            disposition = self.physical_checks.disposition(location_id, facts, self.item_state_ready)
            if disposition == "acknowledged":
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
            if disposition == "quarantine":
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
            if disposition == "outside_slot":
                logger.warning(
                    "[Trigger] AP event location %s is not part of the connected slot; leaving file in place.",
                    location_id,
                )
                continue
            if disposition == "submit":
                self.record_local_automap_cleanup_ownership(location_id, paths)
                pending_locations.append(location_id)

        await self.physical_checks.publish(pending_locations, self.check_publication())

    @property
    def last_processed_event_id(self):
        return self.physical_checks.last_event

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
                    evidence = read_gameplay_save_evidence()
                    if getattr(evidence, "state", None) == "not_running":
                        self.reset_transient_effects("game_exit")
                    else:
                        self.transient_effect_manager.tick(self._observe_transient_runtime())
                    markers = discover_telemetry_markers()
                    newest_path = None
                    if markers:
                        _, newest_path = markers[-1]
                        try:
                            self.ingest_visible_runtime_lifecycle(
                                evidence=evidence,
                                lifecycle_markers=markers,
                            )
                        except Exception as exc:
                            logger.error(
                                "[RPC] Auto-RPC failed to consume telemetry ready file %s: %s",
                                newest_path,
                                exc,
                            )
                        for _mtime, path in markers:
                            try:
                                os.remove(path)
                            except OSError:
                                pass
                    self._refresh_runtime_context(
                        getattr(self, "_connected_slot_data", {}), evidence
                    )
                    self._poll_materialization_completion()
                    if getattr(self, "_queue_session_authoritative", False):
                        if not ensure_queue_session_namespace(self.state_key):
                            self.reset_queue_session_authority("namespace_publish_failed")
                        else:
                            migrate_direct_item_command_jobs(self.state_key)
                    if getattr(evidence, "state", None) == "gameplay":
                        self.onboard_bootstrap("on_reconnect")
                        self.reconcile_checked_automap_cleanup("connect_or_reconnect")
                    if not self.repair_item_mappings():
                        await asyncio.sleep(0.25)
                        continue
                    if not self.reconnect_resync_attempted:
                        _, resync_error = self.automatic_reconcile_inventory("reconnect")
                        if resync_error is None:
                            self.reconnect_resync_attempted = True
                            self.reconcile_owned_runes("reconnect")
                except Exception as exc:
                    logger.warning("[Tracking] Error during reconnection reconciliation: %s", exc)

                work_token = self.runtime_lifecycle.capture_work()
                level_ready = await self.process_level_ready(newest_path if markers else None)
                if not self.runtime_lifecycle.work_is_current(work_token) or self.exit_event.is_set():
                    continue
                if not level_ready:
                    await self.check_mission_challenge_locations()
                    if not self.runtime_lifecycle.work_is_current(work_token) or self.exit_event.is_set():
                        continue
                    self.reconcile_fast_travel_unlock("readiness")
                if self.materialization.snapshot.triggers:
                    _, context_error = self._context_materialize_inventory(
                        evidence, trigger=None
                    )
                    if context_error:
                        logger.info(
                            "[Context] MATERIALIZATION_PENDING reason=%s",
                            context_error,
                        )

                await self.process_pending_item_receipts("tracker")
                if not self.runtime_lifecycle.work_is_current(work_token) or self.exit_event.is_set():
                    continue

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

    

async def launcher_control_loop(ctx):
    """Receive launcher IPC without invoking CommonClient's console parser."""
    while not ctx.exit_event.is_set():
        try:
            raw_line = await asyncio.to_thread(sys.stdin.readline)
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError) as error:
            logger.warning("[Launcher] Control input closed: %s", error)
            ctx.exit_event.set()
            return

        if raw_line == "":
            ctx.exit_event.set()
            return

        line = raw_line.rstrip("\r\n")
        if line == "/exit":
            ctx.exit_event.set()
            return
        if not line.startswith("AP_CONTROL "):
            continue

        try:
            control = json.loads(line[len("AP_CONTROL "):])
        except json.JSONDecodeError:
            emit_launcher_event("chat_send_failed", message="Invalid launcher control message")
            continue
        if not isinstance(control, dict):
            continue
        if control.get("type") == "support_condump":
            try:
                queued = await asyncio.to_thread(request_support_condump)
            except Exception as error:
                logger.warning("[Support] Diagnostic condump request failed: %s", error)
                emit_launcher_event("support_condump", status="error", message=str(error))
            else:
                status = "queued" if queued else "unavailable"
                emit_launcher_event("support_condump", status=status)
            continue
        if control.get("type") == "inventory_resync":
            try:
                plan, error = await ctx.manual_reconcile_inventory_async()
            except Exception as error:
                logger.warning("[Resync] Manual inventory resync failed: %s", error)
                emit_launcher_event(
                    "inventory_resync",
                    status="error",
                    message=_bounded_event_text(str(error), ARCHIPELAGO_EVENT_PLAIN_LIMIT),
                )
            else:
                if error:
                    emit_launcher_event(
                        "inventory_resync",
                        status="error",
                        message=_bounded_event_text(
                            str(error), ARCHIPELAGO_EVENT_PLAIN_LIMIT
                        ),
                    )
                elif plan is None:
                    emit_launcher_event(
                        "inventory_resync",
                        status="error",
                        message="manual inventory resync returned no plan",
                    )
                else:
                    emit_launcher_event(
                        "inventory_resync",
                        status="noop" if not plan.commands else "queued",
                        command_count=len(plan.commands),
                    )
            continue
        if control.get("type") == "ammo_refill":
            try:
                await ctx.request_ammo_refill()
            except Exception as error:
                logger.warning("[Ammo Refill] Launcher request failed: %s", error)
                emit_launcher_event(
                    "ammo_refill",
                    status="error",
                    message=_bounded_event_text(str(error), ARCHIPELAGO_EVENT_PLAIN_LIMIT),
                )
            continue
        if control.get("type") != "say":
            continue

        text = control.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            await ctx.send_launcher_chat(text)
        except Exception as error:
            logger.warning("[Launcher] Chat send failed: %s", error)
            emit_launcher_event("chat_send_failed", message=str(error))
        else:
            emit_launcher_event("chat_sent", text=text)


async def amain(launch_args=None):
    start_bridge_logger()
    log_effective_runtime_paths()
    Utils.init_logging("DoomEternalClient")
    parser = get_base_parser()
    parser.add_argument('--name', default=None, help="Player name no Archipelago")
    args = parser.parse_args(launch_args)
    args.password = args.password or os.environ.get("DOOM_AP_PASSWORD")

    ctx = DoomEternalContext(args.connect, args.password)
    ctx.auth = args.name
    try:
        set_rpc_execution(False)
    except Exception as error:
        logger.warning("[RPC] Initial RPC gate disarm failed: %s", error)
    cleanup_active_map_markers()
    cleanup_ammo_refill_request_files()
    cleanup_telemetry_dumps()
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

    if LAUNCHER_EVENTS_ENABLED:
        original_process_server_cmd = APCommonClient.process_server_cmd

        async def launcher_process_server_cmd(client_ctx, package):
            # CommonClient handles ConnectionRefused before calling on_package,
            # and raises away its machine-readable reason. The launcher owns the
            # retry UX, so preserve that packet before CommonClient can flatten it.
            if package.get("cmd") == "ConnectionRefused":
                client_ctx.on_package("ConnectionRefused", package)
                return
            await original_process_server_cmd(client_ctx, package)

        APCommonClient.process_server_cmd = launcher_process_server_cmd
    ctx.server_task = asyncio.create_task(server_loop(ctx), name="server loop")
    def report_server_stop(task):
        if ctx.exit_event.is_set():
            ctx.reset_queue_session_authority("server_loop_stopped")
            return
        ctx.reset_queue_session_authority("server_loop_stopped")
        if ctx._room_session_established and not ctx.disconnected_intentionally:
            # CommonClient owns autoreconnect after an established session.
            # connection_lost already describes this outage to the launcher.
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
        raise RuntimeError("DOOM Eternal bridge worker requires --nogui")
    ctx.input_task = asyncio.create_task(launcher_control_loop(ctx), name="Launcher control")

    await ctx.exit_event.wait()
    emit_launcher_event("client_stopping")
    ctx.reset_queue_session_authority("bridge_shutdown")
    tasks = [ctx.tracking_task, ctx.death_task, ctx.input_task]
    item_delivery_task = ctx._item_delivery_task
    if item_delivery_task is not None:
        tasks.append(item_delivery_task)
    for task in tasks:
        task.cancel()
    await ctx.session_tasks.close()
    await asyncio.gather(*tasks, return_exceptions=True)
    await ctx.shutdown()


def launch(*launch_args):
    colorama.init()
    asyncio.run(amain(launch_args))
    colorama.deinit()


if __name__ == '__main__':
    launch(*sys.argv[1:])
