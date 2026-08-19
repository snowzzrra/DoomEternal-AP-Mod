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
from collections import deque
from pathlib import Path
from typing import NamedTuple

from doom_eap.runtime.bootstrap_actions import (
    BOOTSTRAP_ACTIONS,
    BOOTSTRAP_REVISION,
    BOOTSTRAP_STAT_PRIMITIVE,
    received_any_suit_upgrade,
)
from doom_eap.contracts.campaign_goal_contract import CAMPAIGN_GOAL_CONTRACT
from doom_eap.contracts.challenge_registry import (
    aggregate_ready,
    canonical_map_name,
    load_challenge_registry,
)
from doom_eap.runtime.deathlink_receive import DeathLinkReceiver, ReceiveState, discard_unclaimed_command
from doom_eap.contracts.foundation import (
    compile_item_delivery_plan,
    load_foundation_contracts,
    load_primitive_registry,
)
from doom_eap.content.item_classification import (
    load_item_classification_identity,
    normalize_network_classification,
    notification_style_for_item,
)
from doom_eap.contracts.item_contracts import DEFAULT_DEATH_LINK_MODE, start_inventory_eligible
from doom_eap.runtime.item_reconciliation import (
    AP_RECEIPT_FEEDBACK,
    CLIENT_STATE_VERSION,
    HISTORICAL_OWNERSHIP,
    NEW_RECEIPT,
    PRESENTATION_REPAIR,
    RECONCILIATION_REPAIR,
    compile_reconciliation_plan,
    default_session_state,
    load_policy_registry,
    migrate_client_state,
    migrate_legacy_session_key,
    normalize_session_state,
    observe_received_items,
    receipt_history_fingerprint,
    receipt_identity,
)
from doom_eap.runtime.observer_lifecycle import (
    RuntimeObservationLease,
    SaveObserverBaselineStore,
    observer_registry_revision,
    unlockable_record_complete,
)
from doom_eap.contracts.publisher_contracts import (
    load_publisher_contracts,
)
from doom_eap.runtime.publisher_runtime import (
    PublisherEngine,
    publisher_acknowledged,
    quarantine_malformed_event,
    read_map_event,
)
from doom_eap.content.automap_visual_registry import (
    index_automap_visual_registry,
    load_automap_visual_registry,
)
from doom_eap.runtime.rune_reconciliation import (
    RUNE_WRITER_EVIDENCE,
    RuneNativeState,
    compile_rune_reconciliation_plan,
    rune_item_perk_mapping,
    rune_plan_already_recorded,
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
)
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
)
LAUNCHER_EVENTS_ENABLED = os.environ.get("DOOM_AP_LAUNCHER_EVENTS") == "1"
ARCHIPELAGO_EVENT_SCHEMA = 1
ARCHIPELAGO_EVENT_PLAIN_LIMIT = 512
ARCHIPELAGO_EVENT_SEGMENT_LIMIT = 128
ARCHIPELAGO_EVENT_SEGMENT_COUNT = 64
_ARCHIPELAGO_TEXT_TYPES = frozenset({
    "text",
    "color",
    "hint_status",
})
_ARCHIPELAGO_ITEM_TYPES = frozenset({
    "item_name",
    "item_id",
})
_ARCHIPELAGO_LOCATION_TYPES = frozenset({
    "location_name",
    "location_id",
})
FAST_TRAVEL_MISSION_COMPLETE_IDS = {
    "e1m1_intro": 7770122, "e1m2_war": 7770123, "e1m3_cult": 7770124,
    "e1m4_boss": 7770162, "e2m1_nest": 7770210, "e2m2_base": 7770248,
    "e2m3_core": 7770289, "e2m4_boss": 7770290, "e3m1_slayer": 7770337,
    "e3m2_hell": 7770362, "e3m2_hell_b": 7770387, "e3m3_maykr": 7770411,
    "e3m4_boss": 7770414,
}
FAST_TRAVEL_RETRY_BASE_SECONDS = 1.0
FAST_TRAVEL_RETRY_MAX_SECONDS = 8.0
AUTOMAP_CLEANUP_RETRY_BASE_SECONDS = 1.0
AUTOMAP_CLEANUP_RETRY_MAX_SECONDS = 8.0


def build_materialization_epoch(native_epoch, marker_mtime_ns):
    if (
        isinstance(native_epoch, bool)
        or not isinstance(native_epoch, int)
        or isinstance(marker_mtime_ns, bool)
        or not isinstance(marker_mtime_ns, int)
        or native_epoch < 0
        or marker_mtime_ns < 0
    ):
        return None
    return f"{native_epoch}:{marker_mtime_ns}"


def valid_materialization_epoch(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9]+:[0-9]+", value) is not None


def valid_fast_travel_delivery_key(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    if not all(isinstance(component, str) and component for component in value):
        return None
    if not re.fullmatch(r"[0-9]+:[0-9]+", value[2]):
        return None
    return tuple(value)


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


def _bounded_event_text(value, limit):
    """Return bounded plain text with control characters removed."""
    if not isinstance(value, str):
        return ""
    value = value.replace("\r", " ").replace("\n", " ")
    output = []
    for character in value:
        if not character.isprintable():
            continue
        if len(output) >= limit:
            break
        output.append(character)
    return "".join(output)


def _fallback_archipelago_segment(part):
    raw_text = part.get("text") if isinstance(part, dict) else None
    if not isinstance(raw_text, str):
        raw_text = "[unavailable]"
    return {"type": "text", "text": _bounded_event_text(raw_text, ARCHIPELAGO_EVENT_SEGMENT_LIMIT)}, raw_text


def _valid_part_type(part):
    if not isinstance(part, dict):
        return None
    part_type = part.get("type", JSONTypes.text.value)
    part_type = getattr(part_type, "value", part_type)
    return part_type if isinstance(part_type, str) else None


def _item_event_classification(flags):
    if not isinstance(flags, int) or isinstance(flags, bool) or flags < 0:
        return None
    from doom_eap.content.item_classification import (
        ITEM_CLASSIFICATION_PROGRESSION,
        ITEM_CLASSIFICATION_TRAP,
        ITEM_CLASSIFICATION_USEFUL,
    )

    if flags & ITEM_CLASSIFICATION_TRAP:
        return "trap"
    if flags & ITEM_CLASSIFICATION_PROGRESSION:
        return "progression"
    if flags & ITEM_CLASSIFICATION_USEFUL:
        return "useful"
    return "filler"


def _format_archipelago_part(context, part: "JSONMessagePart"):
    part_type = _valid_part_type(part)
    if part_type in _ARCHIPELAGO_TEXT_TYPES:
        raw_text = part.get("text") if isinstance(part, dict) else None
        if isinstance(raw_text, str):
            return {"type": "text", "text": _bounded_event_text(raw_text, ARCHIPELAGO_EVENT_SEGMENT_LIMIT)}, raw_text
        return _fallback_archipelago_segment(part)

    if part_type in _ARCHIPELAGO_ITEM_TYPES:
        raw_text = part.get("text") if isinstance(part, dict) else None
        classification = _item_event_classification(part.get("flags", 0)) if isinstance(part, dict) else None
        if not isinstance(raw_text, str) or classification is None:
            return _fallback_archipelago_segment(part)
        if part_type == JSONTypes.item_id.value:
            player = part.get("player")
            if not isinstance(player, int) or isinstance(player, bool):
                return _fallback_archipelago_segment(part)
            try:
                item_text = context.item_names.lookup_in_slot(int(raw_text), player)
            except (AttributeError, TypeError, ValueError, KeyError, LookupError, AssertionError):
                return _fallback_archipelago_segment(part)
            if not isinstance(item_text, str):
                return _fallback_archipelago_segment(part)
            raw_text = item_text
        return {
            "type": "item",
            "text": _bounded_event_text(raw_text, ARCHIPELAGO_EVENT_SEGMENT_LIMIT),
            "classification": classification,
        }, raw_text

    if part_type in _ARCHIPELAGO_LOCATION_TYPES:
        raw_text = part.get("text") if isinstance(part, dict) else None
        if not isinstance(raw_text, str):
            return _fallback_archipelago_segment(part)
        if part_type == JSONTypes.location_id.value:
            player = part.get("player")
            if not isinstance(player, int) or isinstance(player, bool):
                return _fallback_archipelago_segment(part)
            try:
                location_text = context.location_names.lookup_in_slot(int(raw_text), player)
            except (AttributeError, TypeError, ValueError, KeyError, LookupError, AssertionError):
                return _fallback_archipelago_segment(part)
            if not isinstance(location_text, str):
                return _fallback_archipelago_segment(part)
            raw_text = location_text
        return {"type": "location", "text": _bounded_event_text(raw_text, ARCHIPELAGO_EVENT_SEGMENT_LIMIT)}, raw_text

    if part_type in {JSONTypes.player_id.value, JSONTypes.player_name.value}:
        raw_text = part.get("text") if isinstance(part, dict) else None
        player = part.get("player") if isinstance(part, dict) else None
        if part_type == JSONTypes.player_id.value:
            if not isinstance(raw_text, str):
                return _fallback_archipelago_segment(part)
            try:
                player = int(raw_text)
            except (TypeError, ValueError):
                return _fallback_archipelago_segment(part)
            try:
                player_text = context.player_names.get(player, raw_text)
            except (AttributeError, TypeError):
                return _fallback_archipelago_segment(part)
            if not isinstance(player_text, str):
                return _fallback_archipelago_segment(part)
            raw_text = player_text
        if not isinstance(raw_text, str) or not isinstance(player, int) or isinstance(player, bool):
            return _fallback_archipelago_segment(part)
        try:
            is_self = bool(context.slot_concerns_self(player))
        except Exception:
            return _fallback_archipelago_segment(part)
        return {
            "type": "player",
            "text": _bounded_event_text(raw_text, ARCHIPELAGO_EVENT_SEGMENT_LIMIT),
            "self": is_self,
        }, raw_text

    return _fallback_archipelago_segment(part)


def format_archipelago_event(context, args):
    parts = args.get("data") if isinstance(args, dict) else None
    if not isinstance(parts, (list, tuple)):
        parts = (None,)
    segments = []
    plain_parts = []
    for part in parts[:ARCHIPELAGO_EVENT_SEGMENT_COUNT]:
        try:
            segment, raw_text = _format_archipelago_part(context, part)
        except Exception:
            segment, raw_text = _fallback_archipelago_segment(part)
        segments.append(segment)
        plain_parts.append(raw_text if isinstance(raw_text, str) else "[unavailable]")
    return {
        "schema": ARCHIPELAGO_EVENT_SCHEMA,
        "plain": _bounded_event_text("".join(plain_parts), ARCHIPELAGO_EVENT_PLAIN_LIMIT),
        "segments": segments,
    }

ENABLE_ITEM_NOTIFICATIONS = False
ITEM_DELIVERY_BATCH_SIZE = 16
PACKET_TIMING_RANGE_LIMIT = 256
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
from NetUtils import ClientStatus, JSONMessagePart, JSONTypes  # noqa: E402

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
EXECUTION_CLASS_HEADER = "AP_EXECUTION_CLASS_V1"
PLAYER_RUNTIME = "PLAYER_RUNTIME"
MAP_ENTITY_SAFE = "MAP_ENTITY_SAFE"
VALID_EXECUTION_CLASSES = frozenset({PLAYER_RUNTIME, MAP_ENTITY_SAFE})
MAP_ENTITY_OPERATION_HEADER = "AP_MAP_ENTITY_OPERATION_V1"
CHECKED_VISUAL_HIDE = "CHECKED_VISUAL_HIDE"
FAST_TRAVEL_UNLOCK = "FAST_TRAVEL_UNLOCK"
VALID_MAP_ENTITY_OPERATIONS = frozenset({CHECKED_VISUAL_HIDE, FAST_TRAVEL_UNLOCK})
MATERIALIZATION_LEASE_HEADER = "AP_MATERIALIZATION_LEASE_V1"
MATERIALIZATION_LEASE_MARKER = "active_materialization_lease"
RPC_GATE_PATH = os.path.join(DOOM_BASE_DIR, "ap_rpc_enabled")
GAMEPLAY_SAVE_EVIDENCE_PATH = Path(DOOM_BASE_DIR) / "ap_gameplay_save.state"
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


def load_client_state():
    empty_state = {"version": CLIENT_STATE_VERSION, "sessions": {}}
    if not CLIENT_STATE_FILE.is_file():
        return empty_state
    try:
        raw_state = json.loads(CLIENT_STATE_FILE.read_text(encoding="utf-8"))
        state, migrated = migrate_client_state(raw_state)
        if migrated:
            logger.info(
                "[State] STATE_MIGRATED from=1 to=%s sessions=%s",
                CLIENT_STATE_VERSION,
                len(state["sessions"]),
            )
            try:
                save_client_state(state)
            except OSError as error:
                logger.warning("[State] Could not persist migrated state: %s", error)
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
        return empty_state


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
    """Return valid primary slots newest-first."""
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
        map_name = canonical_map_name(values.get("map_name", "")) or ""
    except (OSError, UnicodeError, ValueError):
        return None
    if state == "menu":
        return GameplaySaveEvidence(state, epoch, "", "")
    if (
        state != "gameplay"
        or epoch < 0
        or not re.fullmatch(r"GAME-AUTOSAVE\d+", slot_directory)
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

SPOOL_ID_MAX_BYTES = 128
SPOOL_ID_HASH_HEX_LENGTH = 20
_WINDOWS_ILLEGAL_SPOOL_ID_CHARS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_SPOOL_ID_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
})


def validate_spool_id(command_id):
    """Reject command IDs that cannot be one filesystem component."""
    if not isinstance(command_id, str) or not command_id:
        raise ValueError("spool command ID must be a non-empty string")
    if len(command_id.encode("utf-8")) > SPOOL_ID_MAX_BYTES:
        raise ValueError(
            f"spool command ID exceeds {SPOOL_ID_MAX_BYTES} UTF-8 bytes"
        )
    if any(character in command_id for character in "/\\"):
        raise ValueError("spool command ID contains a path separator")
    if any(
        ord(character) < 32 or ord(character) == 127
        for character in command_id
    ):
        raise ValueError("spool command ID contains a control character")
    if any(character in _WINDOWS_ILLEGAL_SPOOL_ID_CHARS for character in command_id):
        raise ValueError("spool command ID contains a Windows-illegal character")
    if command_id.endswith((".", " ")):
        raise ValueError("spool command ID has a Windows-illegal trailing character")
    if command_id in {".", ".."}:
        raise ValueError("spool command ID is a traversal component")
    windows_stem = command_id.split(".", 1)[0].upper()
    if windows_stem in _WINDOWS_RESERVED_SPOOL_ID_NAMES:
        raise ValueError("spool command ID is a Windows-reserved device name")
    return command_id


def stable_spool_id(prefix, *logical_components):
    """Return bounded ID for logical coalescing identity."""
    validate_spool_id(prefix)
    canonical = json.dumps(
        logical_components,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return validate_spool_id(
        f"{prefix}-{digest[:SPOOL_ID_HASH_HEX_LENGTH]}"
    )


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


def command_spool_exists(command_id, state_key=None, room_scoped=True):
    if room_scoped:
        command_id = room_scoped_command_id(command_id, state_key)
    validate_spool_id(command_id)
    queued_path = os.path.join(QUEUE_DIR, f"{command_id}.cmd")
    processing_path = os.path.join(QUEUE_DIR, f"{command_id}.processing")
    return os.path.exists(queued_path) or os.path.exists(processing_path)


def queue_session_namespace(state_key):
    """Opaque durable queue namespace derived from room identity."""
    if not isinstance(state_key, str) or not state_key:
        return None
    return hashlib.sha256(state_key.encode("utf-8")).hexdigest()[:16]


def active_queue_session_namespace():
    """Read native queue authority published for current AP room."""
    marker = Path(QUEUE_DIR) / "active_session_namespace"
    try:
        value = marker.read_text(encoding="ascii").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    return value if re.fullmatch(r"[0-9a-f]{16}", value) else None


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
        os.replace(temporary, marker)
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
    namespace = queue_session_namespace(state_key) if state_key else None
    if namespace is None:
        namespace = active_queue_session_namespace()
    if namespace is None:
        return command_id
    prefix = f"recv-{namespace}-"
    if command_id.startswith(prefix):
        return command_id
    return f"{prefix}{command_id}"


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


def bootstrap_activation(action_name):
    action = BOOTSTRAP_ACTIONS[action_name]
    return f"ai_ScriptCmdEnt {action['entity_name']} activate"


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
):
    """Atomically enqueue one command without overwriting another command.

    A coalesced command has at most one queued or in-flight spool file. This is
    used for telemetry requests so menus/loading screens cannot accumulate a
    large condump backlog behind the player-state gate.
    """
    try:
        if execution_class not in VALID_EXECUTION_CLASSES:
            logger.error("[Queue] Refusing command with invalid execution class: %r", execution_class)
            return False
        if execution_class == MAP_ENTITY_SAFE:
            if operation not in VALID_MAP_ENTITY_OPERATIONS:
                logger.error("[Queue] Refusing MAP_ENTITY_SAFE command with invalid operation: %r", operation)
                return False
        elif operation is not None:
            logger.error("[Queue] Refusing PLAYER_RUNTIME command with map operation: %r", operation)
            return False
        command_id = coalesce_key or f"{time.time_ns():020d}-{uuid.uuid4().hex}"
        if room_scoped:
            command_id = room_scoped_command_id(command_id, state_key)
        validate_spool_id(command_id)
        os.makedirs(QUEUE_DIR, exist_ok=True)
        if coalesce_key:
            if command_spool_exists(command_id, room_scoped=room_scoped):
                if delivery_fields is not None:
                    log_delivery_event(
                        "QUEUE_DUPLICATE_REJECT",
                        command_id=command_id,
                        reason="spool_exists",
                        **delivery_fields,
                    )
                if already_queued_ok and arm_rpc:
                    set_rpc_execution(True)
                return already_queued_ok

        temporary_path = os.path.join(
            QUEUE_DIR, f".{command_id}-{uuid.uuid4().hex}.tmp"
        )
        command_path = os.path.join(QUEUE_DIR, f"{command_id}.cmd")
        if materialization_lease is not None and not valid_materialization_epoch(materialization_lease):
            logger.error("[Queue] Refusing command with invalid materialization lease: %r", materialization_lease)
            return False
        payload = f"{EXECUTION_CLASS_HEADER} {execution_class}\n"
        if execution_class == MAP_ENTITY_SAFE:
            payload += f"{MAP_ENTITY_OPERATION_HEADER} {operation}\n"
        if materialization_lease is not None:
            payload += f"{MATERIALIZATION_LEASE_HEADER} {materialization_lease}\n"
        payload += cmd.strip() + "\n"
        with open(temporary_path, "x", encoding="utf-8", newline="\n") as f:
            f.write(payload)
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
                if already_queued_ok and arm_rpc:
                    set_rpc_execution(True)
                return already_queued_ok
            finally:
                try:
                    os.remove(temporary_path)
                except FileNotFoundError:
                    pass
            processing_path = os.path.join(QUEUE_DIR, f"{command_id}.processing")
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
                if already_queued_ok and arm_rpc:
                    set_rpc_execution(True)
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
        for line in self.ctx.rune_diagnostic_lines():
            self.output(line)

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
        receiver = self.ctx.deathlink_receiver
        self.output(
            f"DeathLink mode={self.ctx.death_link_mode} enabled={self.ctx.death_link_enabled} "
            f"policy={'single_dispatch' if receiver.mode == 'soft' else 'retry_until_confirmed'}"
        )
        for entry in receiver.instrumentation_dicts()[-8:]:
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
        self._queue_session_authoritative = False
        invalidate_queue_session_namespace("bridge_start")
        # Process-local packet timing. Ranges keep ReceivedItems callback work O(1).
        self._packet_received_ranges = deque(maxlen=PACKET_TIMING_RANGE_LIMIT)
        self._item_session_generation = 0
        self.tracker_alive = False
        self.tracker_restart_count = 0
        self.last_tracker_error = None
        self.last_heartbeat_timestamp = None
        self.last_processed_event_id = None
        self.items_processed = 0
        self.item_state_ready = False
        self.reconnect_resync_attempted = False
        self._automatic_resync_noop_signature = None
        self._automatic_resync_noop_logged_at = None
        self.client_state = {"version": CLIENT_STATE_VERSION, "sessions": {}}
        self.state_key = ""
        self.session_state = {}
        self.death_link_enabled = False
        self.death_link_mode = DEFAULT_DEATH_LINK_MODE
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
        self.native_gameplay_epoch = None
        self.last_accepted_map_evidence_epoch = None
        self.pending_level_ready = {}
        self.completed_level_ready_epochs = set()
        self.level_ready_in_flight = set()
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
        # Pending lethal commands stay process-local. Seen event identities persist per
        # room so reconnect/transport replay cannot create a second logical event.
        self.received_deathlink_event_ids: set[str] = set()
        self.deathlink_receiver = DeathLinkReceiver(
            wait_timeout=DEATHLINK_RECEIVE_TIMEOUT,
            confirm_timeout=DEATHLINK_CONFIRM_TIMEOUT,
            retry_interval=DEATHLINK_KILL_INTERVAL,
            total_timeout=DEATHLINK_TOTAL_TIMEOUT,
            late_suppression_grace=DEATHLINK_LATE_SUPPRESSION_GRACE,
            max_attempts=DEATHLINK_MAX_ATTEMPTS,
            mode=self.death_link_mode,
        )
        self.deathlink_instrumentation = []
        self.last_goal_details_mtime = None
        self.final_sin_completion_candidate = None
        self.cultist_autosave_path = None
        self.mission_locations_in_flight = set()
        self.mission_goal_in_flight = False
        self.publisher_effects_in_flight = set()
        self.last_rpc_map_name = None
        self.room_seed_name = None
        self.current_map_name = None
        self.automap_cleanup_epoch = None
        self.automap_cleanup_session = uuid.uuid4().hex[:8]
        self.automap_cleanup_submitted = {}
        self.automap_local_cleanup_owned = set()
        self.automap_cleanup_retry = {}
        self.automap_cleanup_status = {}
        self.server_checked_locations_ready = False
        self.fast_travel_submitted = {}
        self.fast_travel_eligibility_snapshot = None
        self.fast_travel_epoch_state = None
        self.fast_travel_last_transition = None
        self._launcher_connection_failure_reported = False

    def on_print_json(self, args: dict):
        try:
            super().on_print_json(args)
        except Exception:
            logger.exception("[Bridge] Archipelago PrintJSON logging failed")
        try:
            emit_launcher_event("archipelago", **format_archipelago_event(self, args))
        except Exception:
            logger.exception("[Bridge] Archipelago PrintJSON event formatting failed")

    def reset_queue_session_authority(self, reason):
        self._queue_session_authoritative = False
        invalidate_queue_session_namespace(reason)

    def _report_launcher_connection_failure(self, message):
        if (
            not LAUNCHER_EVENTS_ENABLED
            or self._launcher_connection_failure_reported
        ):
            return
        self._launcher_connection_failure_reported = True
        emit_launcher_event(
            "error",
            code="connection_failed",
            message=message,
        )
        self.disconnected_intentionally = True
        self.cancel_autoreconnect()
        self.exit_event.set()

    def handle_connection_loss(self, msg: str) -> None:
        state_key = self.state_key
        self.reset_queue_session_authority("connection_loss")
        self.deathlink_receiver.abandon(time.monotonic(), "disconnect")
        discard_queued_coalesced_command(DEATHLINK_KILL_COALESCE_KEY, state_key)
        super().handle_connection_loss(msg)
        self._report_launcher_connection_failure(msg)

    async def connection_closed(self):
        state_key = self.state_key
        self.reset_queue_session_authority("connection_closed")
        self.deathlink_receiver.abandon(time.monotonic(), "disconnect")
        discard_queued_coalesced_command(DEATHLINK_KILL_COALESCE_KEY, state_key)
        unexpected_launcher_close = (
            LAUNCHER_EVENTS_ENABLED
            and self.server is not None
            and not self.disconnected_intentionally
            and not self.exit_event.is_set()
        )
        await super().connection_closed()
        if unexpected_launcher_close:
            self._report_launcher_connection_failure(
                "Disconnected from the Archipelago server"
            )

    def queue_dev_commands(self, commands, action):
        """Spool isolated dev commands without touching receipt/bootstrap state."""
        self.dev_counter += 1
        correlation = f"devtest-{self.dev_session_id}-{self.dev_counter:04d}"
        for index, command in enumerate(commands):
            if not re.fullmatch(r"ai_ScriptCmdEnt [A-Za-z0-9_]+ activate", command):
                raise ValueError("Directed tests accept only map-side entity activation")
            command_id = f"{correlation}-cmd-{index:02d}"
            if not send_command(command, coalesce_key=command_id, state_key=self.state_key):
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
            # Durable item state cannot authorize queue work until matching
            # Connected rebinds state_key to this room.
            self.reset_queue_session_authority("room_info")
            self.room_seed_name = args.get("seed_name")
        elif cmd == "ReceivedItems":
            self._on_received_items_packet(args)
        elif cmd == "Connected":
            previous_state_key = self.state_key
            self.initialize_item_state()
            if previous_state_key and previous_state_key != self.state_key:
                abandoned = self.deathlink_receiver.abandon(time.monotonic(), "room_changed")
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
            configured_mode = slot_data.get("death_link_mode", DEFAULT_DEATH_LINK_MODE)
            if configured_mode not in {"soft", "hardcore"}:
                configured_mode = DEFAULT_DEATH_LINK_MODE
            self.death_link_mode = "soft"
            self.deathlink_receiver.configure_mode(self.death_link_mode)
            self.death_link_enabled = bool(slot_data.get("death_link", False))
            logger.info(
                "[DeathLink] enabled=%s receive_policy=single_burst",
                self.death_link_enabled,
            )
            materialized = {}
            configured = slot_data.get("starting_inventory", {})
            if isinstance(configured, dict):
                by_name = {entry["name"]: item_id for item_id, entry in ITEM_CLASSIFICATION_IDENTITY.items()}
                for name, quantity in configured.items():
                    if (
                        name in by_name
                        and start_inventory_eligible(by_name[name])
                        and isinstance(quantity, int)
                        and quantity > 0
                    ):
                        materialized[by_name[name]] = materialized.get(by_name[name], 0) + quantity
            weapon = slot_data.get("starting_weapon")
            if isinstance(weapon, str):
                by_name = {entry["name"]: item_id for item_id, entry in ITEM_CLASSIFICATION_IDENTITY.items()}
                if weapon in by_name:
                    item_id = by_name[weapon]
                    if start_inventory_eligible(item_id):
                        materialized[item_id] = materialized.get(item_id, 0) + 1
            processed_receipt_count = min(self.items_processed, len(self.items_received))
            for receipt in self.items_received[:processed_receipt_count]:
                item_id = receipt.item
                if materialized.get(item_id, 0) > 0:
                    materialized[item_id] -= 1
            self._materialized_receipt_counts = materialized
            self._death_link_task = asyncio.create_task(
                self.update_death_link(self.death_link_enabled)
            )
            self.server_checked_locations_ready = isinstance(args.get("checked_locations"), (list, tuple, set, frozenset))
            self.onboard_bootstrap("on_connect")
            self.reconcile_checked_automap_cleanup("server_connected")
            self.reconcile_fast_travel_unlock("connected")
            asyncio.create_task(self.check_mission_challenge_locations())
            if self._item_delivery_wakeup:
                self._schedule_item_delivery("connected")
            emit_launcher_event(
                "connected",
                seed_name=self.room_seed_name,
                endpoint=str(getattr(self, "server_address", "") or ""),
                team=args.get("team", self.team),
                slot=args.get("slot", self.slot),
                slot_data=args.get("slot_data", {}),
                missing_locations=sorted(args.get("missing_locations", [])),
                checked_locations=sorted(args.get("checked_locations", [])),
            )
        elif cmd == "ConnectionRefused":
            self.reset_queue_session_authority("connection_refused")
            self._report_launcher_connection_failure(
                "Archipelago connection was refused"
            )
        elif cmd == "RoomUpdate" and "checked_locations" in args:
            self.server_checked_locations_ready = isinstance(args.get("checked_locations"), (list, tuple, set, frozenset))
            self.reconcile_checked_automap_cleanup("server_checked_update")
            self.reconcile_fast_travel_unlock("server_checked_update")
            asyncio.create_task(self.check_mission_challenge_locations())
        elif cmd == "Bounced" and "DeathLink" in args.get("tags", []):
            data = args.get("data", {})
            if (
                data.get("time") == self.last_death_link
                and data.get("time") != self.confirmed_death_echo
            ):
                logger.info("[DeathLink] Server received and echoed the death.")
                self.confirmed_death_echo = data.get("time")

    def _on_received_items_packet(self, args):
        """Record packet metadata and wake delivery without doing delivery work."""
        packet_received_ns = time.monotonic_ns()
        packet_items = args.get("items", ()) if isinstance(args, dict) else ()
        try:
            packet_item_count = len(packet_items)
        except TypeError:
            packet_item_count = 0
        authoritative_count = len(self.items_received)
        packet_start_index = args.get("index") if isinstance(args, dict) else None
        accepted_start_index = (
            packet_start_index
            if isinstance(packet_start_index, int)
            and not isinstance(packet_start_index, bool)
            and packet_start_index >= 0
            else None
        )
        if accepted_start_index is None:
            packet_start_index = max(0, authoritative_count - packet_item_count)
        packet_accepted = (
            accepted_start_index is not None
            and accepted_start_index <= authoritative_count
            and accepted_start_index + packet_item_count == authoritative_count
        )
        if packet_accepted and accepted_start_index == 0:
            self._packet_received_ranges.clear()
        if (
            packet_accepted
            and packet_item_count
            and accepted_start_index is not None
        ):
            self._packet_received_ranges.append(
                (
                    accepted_start_index,
                    accepted_start_index + packet_item_count,
                    packet_received_ns,
                )
            )
        log_delivery_event(
            "ITEM_PACKET_RECEIVED",
            packet_start_index=packet_start_index,
            packet_count=packet_item_count,
            authoritative_count=authoritative_count,
            packet_received_monotonic_ns=packet_received_ns,
        )
        self._schedule_item_delivery("packet")

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
        for start, end, timestamp_ns in reversed(self._packet_received_ranges):
            if start <= receipt_index < end:
                return timestamp_ns
        return None

    async def process_pending_item_receipts(self, trigger):
        """Consume authoritative receipts once, in increasing receive-index order."""
        async with self._item_delivery_lock:
            self._item_delivery_wakeup = False
            if not self.item_state_ready:
                self._item_delivery_waiting_for_state = True
                self._item_delivery_wakeup = True
                return False
            self._item_delivery_waiting_for_state = False
            captured_state_key = getattr(self, "state_key", "")
            captured_generation = getattr(self, "_item_session_generation", 0)
            try:
                history_observation = self.observe_received_item_history()
                duplicate_indices = {
                    receipt.index for receipt in history_observation.duplicates
                }
            except ValueError as exc:
                logger.error("[Tracking] ReceivedItems history rejected: %s", exc)
                return False

            batch_count = 0
            while len(self.items_received) > self.items_processed:
                item_index = self.items_processed
                network_item = self.items_received[item_index]
                item_id = network_item.item
                packet_received_ns = self._packet_received_timestamp(item_index)
                duplicate = item_index in duplicate_indices
                log_delivery_event(
                    "RECEIPT_CLASSIFIED",
                    receipt_index=item_index,
                    item_id=item_id,
                    trigger=trigger,
                    classification="duplicate" if duplicate else "new",
                    packet_received_monotonic_ns=packet_received_ns,
                )
                if duplicate:
                    logger.info(
                        "[To Game] Duplicate authoritative receipt acknowledged "
                        "without replay: index=%s item_id=%s",
                        item_index,
                        item_id,
                    )
                    self.items_processed += 1
                    self.persist_session_state()
                    batch_count += 1
                    if batch_count >= ITEM_DELIVERY_BATCH_SIZE:
                        batch_count = 0
                        await asyncio.sleep(0)
                        if (
                            captured_state_key != getattr(self, "state_key", "")
                            or captured_generation
                            != getattr(self, "_item_session_generation", 0)
                        ):
                            return False
                    continue

                materialized = getattr(self, "_materialized_receipt_counts", {})
                if materialized.get(item_id, 0) > 0:
                    materialized[item_id] -= 1
                    logger.info(
                        "[To Game] Materialized starting receipt acknowledged without replay: "
                        "index=%s item_id=%s",
                        item_index,
                        item_id,
                    )
                    self._record_processed_receipt(network_item)
                    self.items_processed += 1
                    self.persist_session_state()
                    batch_count += 1
                    if batch_count >= ITEM_DELIVERY_BATCH_SIZE:
                        batch_count = 0
                        await asyncio.sleep(0)
                        if (
                            captured_state_key != getattr(self, "state_key", "")
                            or captured_generation
                            != getattr(self, "_item_session_generation", 0)
                        ):
                            return False
                    continue

                log_delivery_event(
                    "ITEM_RECEIPT",
                    receipt_index=item_index,
                    item_id=item_id,
                    item_name=self.delivery_item_name(item_id)
                    if item_id in ITEM_CLASSIFICATION_IDENTITY else None,
                    trigger=trigger,
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
                    break

                definition = ITEM_ID_TO_COMMAND[item_id]
                if isinstance(definition, dict) and definition.get("type") == "no_op":
                    logger.info(f"[To Game] Runtime-only item {item_id} acknowledged.")
                    self._record_processed_receipt(network_item)
                    self.items_processed += 1
                    self.persist_session_state()
                    batch_count += 1
                    if batch_count >= ITEM_DELIVERY_BATCH_SIZE:
                        batch_count = 0
                        await asyncio.sleep(0)
                        if (
                            captured_state_key != getattr(self, "state_key", "")
                            or captured_generation
                            != getattr(self, "_item_session_generation", 0)
                        ):
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
                    break
                else:
                    self.item_delivery_blocked = False
                    self.item_delivery_blocked_info = None

                logger.info(f"[To Game] Item received! {item_id} -> {description}")
                self._record_processed_receipt(network_item)
                self.items_processed += 1
                self.persist_session_state()
                self.onboard_bootstrap("on_item_received")

                batch_count += 1
                if batch_count >= ITEM_DELIVERY_BATCH_SIZE:
                    batch_count = 0
                    await asyncio.sleep(0)
                    if (
                        captured_state_key != getattr(self, "state_key", "")
                        or captured_generation
                        != getattr(self, "_item_session_generation", 0)
                    ):
                        return False

            self._packet_received_ranges = deque(
                (
                    timing_range
                    for timing_range in self._packet_received_ranges
                    if timing_range[1] > self.items_processed
                ),
                maxlen=PACKET_TIMING_RANGE_LIMIT,
            )
            return True

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
        """Accept fresh FirstThink markers as live-map and load-epoch authority."""
        lease = getattr(self, "runtime_observation_lease", None)
        evidence_epoch = getattr(evidence, "epoch", None)
        if evidence_epoch is not None:
            self.native_gameplay_epoch = evidence_epoch
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
        if started is not None and newest_mtime < started:
            return False
        known_marker_mtime = max(
            getattr(self, "last_accepted_marker_mtime", 0) or 0,
            (getattr(self, "pending_map_identity", None) or {}).get("mtime_ns", 0),
            (getattr(self, "cached_map_identity", None) or {}).get("mtime_ns", 0),
        )
        if newest_mtime <= known_marker_mtime:
            return self.advance_known_map_materialization(evidence)
        marker_data = parse_active_map_marker(newest_path, newest_mtime)
        if marker_data is None:
            self.invalidate_map_identity("malformed_marker")
            return False
        self.invalidate_active_save_proof()
        self.fast_travel_eligibility_snapshot = None
        self.fast_travel_epoch_state = None
        self.fast_travel_last_transition = None
        self.fast_travel_submitted.clear()
        self.mission_select_observation_map = None
        self.mission_select_observation_epoch = None
        self.current_map_name = None
        self.cached_map_identity = None
        self.pending_map_identity = None
        marker_data = {
            **marker_data,
            "native_gameplay_epoch": newest_mtime,
            "gameplay_epoch": build_materialization_epoch(newest_mtime, newest_mtime),
            "evidence_mtime_ns": gameplay_evidence_mtime_ns(),
            "evidence_epoch": evidence_epoch,
            "materialization_evidence_epoch": (
                evidence_epoch
                if evidence is not None
                and getattr(evidence, "state", None) == "gameplay"
                and canonical_map_name(getattr(evidence, "map_name", ""))
                == canonical_map_name(marker_data.get("runtime_map", ""))
                else None
            ),
        }
        if lease is not None:
            lease.observe_gameplay_loaded(newest_mtime)
        self.accept_map_identity(marker_data, evidence_epoch)
        self.snapshot_fast_travel_eligibility(marker_data=marker_data)
        for _, marker_path in markers:
            cleanup_active_map_marker_file(marker_path)
        return True

    def advance_known_map_materialization(self, evidence):
        """Advance epoch from a fresh native load edge without changing map identity."""
        cached = getattr(self, "cached_map_identity", None)
        if (
            not isinstance(cached, dict)
            or evidence is None
            or getattr(evidence, "state", None) != "gameplay"
            or getattr(evidence, "provisional", False)
        ):
            return False
        evidence_epoch = getattr(evidence, "epoch", None)
        if isinstance(evidence_epoch, bool) or not isinstance(evidence_epoch, int):
            return False
        if canonical_map_name(getattr(evidence, "map_name", "")) != canonical_map_name(
            cached.get("runtime_map", "")
        ):
            return False
        bound_epoch = cached.get("materialization_evidence_epoch")
        if bound_epoch is None:
            cached["materialization_evidence_epoch"] = evidence_epoch
            cached["evidence_epoch"] = evidence_epoch
            return False
        if evidence_epoch == bound_epoch:
            return False
        evidence_mtime = gameplay_evidence_mtime_ns()
        epoch = build_materialization_epoch(evidence_epoch, evidence_mtime)
        if not valid_materialization_epoch(epoch) or epoch == cached.get("gameplay_epoch"):
            return False

        self.invalidate_active_save_proof()
        self.fast_travel_eligibility_snapshot = None
        self.fast_travel_epoch_state = None
        self.fast_travel_last_transition = None
        self.fast_travel_submitted.clear()
        marker_data = {
            **cached,
            "native_gameplay_epoch": evidence_epoch,
            "gameplay_epoch": epoch,
            "evidence_mtime_ns": evidence_mtime,
            "evidence_epoch": evidence_epoch,
            "materialization_evidence_epoch": evidence_epoch,
            "secondary_materialization": True,
        }
        lease = getattr(self, "runtime_observation_lease", None)
        if lease is not None:
            lease.observe_gameplay_loaded(evidence_mtime)
        self.accept_map_identity(marker_data, evidence_epoch)
        self.snapshot_fast_travel_eligibility(marker_data=marker_data)
        logger.info(
            "[MAP] MATERIALIZATION_EPOCH_SECONDARY map=%s epoch=%s "
            "source=native_same_map_load_edge",
            marker_data.get("map_key", "<unknown>"),
            epoch,
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
        if getattr(self, "last_marker_reject_reason", None) != reason:
            logger.info("[MAP] MAP_IDENTITY_MARKER_REJECTED reason=%s", reason)
            self.last_marker_reject_reason = reason
        self.current_map_name = None
        self.cached_map_identity = None
        publish_materialization_lease(None)
        if clear_pending:
            self.pending_map_identity = None
        self.mission_select_observation_map = None
        self.mission_select_observation_epoch = None
        self.fast_travel_eligibility_snapshot = None
        self.fast_travel_epoch_state = None
        self.fast_travel_last_transition = None
        return None

    def store_pending_map_identity(self, marker_data):
        self.pending_map_identity = {**marker_data, "evidence_epoch": None}
        self.current_map_name = None
        self.cached_map_identity = None
        publish_materialization_lease(None)
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

    def accept_map_identity(self, marker_data, evidence_epoch=None):
        marker_data = {
            **marker_data,
            "evidence_epoch": evidence_epoch,
        }
        self.pending_map_identity = None
        self.cached_map_identity = marker_data
        self.current_map_name = marker_data["runtime_map"]
        materialized_epoch = marker_data.get("gameplay_epoch")
        publish_materialization_lease(materialized_epoch)
        if (
            isinstance(materialized_epoch, str)
            and valid_fast_travel_delivery_key(("room", "map", materialized_epoch))
            and materialized_epoch
            not in getattr(self, "completed_level_ready_epochs", set())
        ):
            pending_level_ready = getattr(self, "pending_level_ready", {})
            self.pending_level_ready = pending_level_ready
            pending_level_ready.setdefault(
                materialized_epoch, marker_data.get("path")
            )
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

        cached = self.cached_map_identity
        if not isinstance(cached, dict):
            if evidence is not None and getattr(evidence, "state", None) != "gameplay":
                self.invalidate_active_save_proof()
                return self.invalidate_map_identity("menu")
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

        def fail_proof(reason):
            self.runtime_observers_frozen = True
            self.mission_select_observation_map = None
            self.mission_select_observation_epoch = None
            self.log_save_proof_rejected(
                reason,
                evidence_slot=evidence_slot,
                marker_map=marker_map,
                candidate_slot=candidate_slot,
                candidate_mtime=candidate_mtime,
                active_slot=self.active_save_slot,
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
                self.reconcile_fast_travel_unlock("save_proof")
                return continued
            return fail_proof("no_gameplay_evidence")

        if evidence and evidence.state != "gameplay":
            self.invalidate_active_save_proof()
            return fail_proof("menu")

        if evidence and evidence.provisional and marker is None:
            continued = continue_authoritative_active()
            if continued is not None:
                self.reconcile_fast_travel_unlock("save_proof")
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

        active_map = marker_map
        if not active_map:
            return fail_proof("map_marker_unavailable")
        continue_target_map = canonical_map_name(details.get("mapName", ""))
        mission_select_required = bool(
            active_map in MISSION_CHALLENGE_RUNTIME_MAPS
            and continue_target_map
            and continue_target_map != active_map
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
                    self.mission_select_observation_map = active_map
                    self.mission_select_observation_epoch = lease.gameplay_loaded_ns
                    if getattr(self, "last_accepted_mission_select_epoch", None) != lease.gameplay_loaded_ns:
                        self.last_accepted_mission_select_epoch = lease.gameplay_loaded_ns
                        logger.info(
                            "[OBSERVER] MISSION_SELECT_LEASE_ACCEPTED slot=%s map=%s "
                            "load_epoch=%s save_mtime_ns=%s",
                            selected.slot_directory,
                            active_map,
                            lease.gameplay_loaded_ns,
                            selected.mtime_ns,
                        )
                else:
                    self.mission_select_observation_map = None
                    self.mission_select_observation_epoch = None
            elif live:
                self.mission_select_observation_map = None
                self.mission_select_observation_epoch = None
            if not live and not mission_select_live:
                if reason != self.last_observer_lease_block:
                    logger.info("[OBSERVER] LIVE_LEASE_BLOCKED reason=%s", reason)
                    self.last_observer_lease_block = reason
                return fail_proof(reason)
            self.last_observer_lease_block = None

        is_current_active_slot = (
            self.active_save_slot == selected.slot_directory
            and self.active_save_path == str(selected.path)
        )

        details_token = details.get("_mtime_ns", selected.mtime_ns)

        if not is_current_active_slot:
            if self.active_native_evidence_epoch == proof_evidence_epoch and self.active_save_slot is not None:
                return fail_proof("unproven_epoch")

            # SAVE_PROOF_ACCEPTED MUST precede SAVE_SLOT_ACTIVE
            self.log_save_proof_accepted(
                selected.slot_directory,
                active_map,
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
                selected, details, active_map, proof_load_epoch
            )
            self.reconcile_fast_travel_unlock("save_proof")
            return selected
        else:
            if not self.mission_select_observation_map:
                self.mission_select_observation_epoch = None
            if evidence_epoch is not None and self.active_native_evidence_epoch != evidence_epoch:
                self.log_save_proof_accepted(
                    selected.slot_directory,
                    active_map,
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
                selected, details, active_map, proof_load_epoch
            )
            self.reconcile_fast_travel_unlock("save_proof")
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
        self.reset_queue_session_authority("initialize_item_state")
        previous_state_key = self.state_key
        self._item_session_generation = getattr(self, "_item_session_generation", 0) + 1
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
            self.items_processed = 0
            self.item_state_ready = False
            self._packet_received_ranges.clear()
            return
        sessions = self.client_state["sessions"]
        self.state_key, migrated_from = migrate_legacy_session_key(
            sessions,
            seed_name=effective_seed_name,
            team=self.team,
            slot=self.slot,
        )
        if previous_state_key != self.state_key:
            self._packet_received_ranges.clear()
        if self.state_key is None:
            logger.warning(
                "[State] Slot identity incomplete or unsafe; refusing session state reuse."
            )
            self.state_key = ""
            self.session_state = default_session_state()
            self.items_processed = 0
            self.item_state_ready = False
            self._packet_received_ranges.clear()
            return
        if migrated_from is not None:
            logger.info(
                "[State] STATE_MIGRATED from=%s to=%s reason=legacy_session",
                migrated_from,
                self.state_key,
            )
        existing_session = sessions.get(self.state_key)
        if not isinstance(existing_session, dict):
            existing_session = default_session_state()
        self.session_state = normalize_session_state(existing_session)
        self.session_state.setdefault("goal_sent", False)
        self.session_state.setdefault("cultist_autosave_path", None)
        self.session_state.setdefault("save_slot_observations", {})
        self.automap_cleanup_submitted = {}
        self.automap_local_cleanup_owned = set()
        self.fast_travel_submitted = {}
        self.session_state.pop("automap_cleanup", None)
        self.session_state.pop("fast_travel_delivered", None)
        self.automap_cleanup_epoch = None
        sessions[self.state_key] = self.session_state
        processed = self.session_state.get("processed_items", 0)
        if not isinstance(processed, int) or processed < 0:
            processed = 0
            self.session_state["processed_items"] = 0
        self.items_processed = processed
        self.cultist_autosave_path = self.session_state.get(
            "cultist_autosave_path"
        )
        # Process restarts preserve bounded event identities while lethal
        # transport state begins fresh for the new connection.
        self.session_state.pop("deathlinked", None)
        seen_deathlinks = self.session_state.get("received_deathlink_event_ids", [])
        if not isinstance(seen_deathlinks, list):
            seen_deathlinks = []
        self.received_deathlink_event_ids = {
            value for value in seen_deathlinks[-64:] if isinstance(value, str) and value
        }
        self.session_state["received_deathlink_event_ids"] = sorted(
            self.received_deathlink_event_ids
        )[-64:]
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
        self.reconnect_resync_attempted = False
        bootstrap = self.session_state.get("bootstrap")
        if not isinstance(bootstrap, dict):
            bootstrap = {"revision": BOOTSTRAP_REVISION, "actions": {}}
            self.session_state["bootstrap"] = bootstrap
        bootstrap.setdefault("revision", BOOTSTRAP_REVISION)
        if not isinstance(bootstrap.get("actions"), dict):
            bootstrap["actions"] = {}
        reconciliation = self.session_state.setdefault(
            "perk_reconciliation", {"epoch": 0, "delivered": {}}
        )
        if not isinstance(reconciliation, dict):
            reconciliation = {"epoch": 0, "delivered": {}}
            self.session_state["perk_reconciliation"] = reconciliation
        raw_reconciliation_epoch = reconciliation.get("epoch", 0)
        if isinstance(raw_reconciliation_epoch, bool) or not isinstance(raw_reconciliation_epoch, int):
            raw_reconciliation_epoch = 0
        reconciliation["epoch"] = raw_reconciliation_epoch + 1
        if not isinstance(reconciliation.get("delivered"), dict):
            reconciliation["delivered"] = {}
        reconciliation.setdefault("delivered", {})
        save_client_state(self.client_state)
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
        self.session_state["processed_items"] = self.items_processed
        history = self.session_state.setdefault("receipt_history", {})
        if not isinstance(history, dict):
            history = {}
            self.session_state["receipt_history"] = history
        history["processed_boundary"] = self.items_processed
        self.session_state["cultist_autosave_path"] = self.cultist_autosave_path
        self.session_state.pop("deathlinked", None)
        self.session_state["received_deathlink_event_ids"] = sorted(
            self.received_deathlink_event_ids
        )[-64:]
        self.session_state["save_slot_observations"] = self.save_slot_observations
        self.session_state.pop("automap_cleanup", None)
        self.session_state.pop("fast_travel_delivered", None)
        self.session_state.pop("sticky_mastery_observed", None)
        self.session_state.pop("weapon_masteries_observed", None)
        save_client_state(self.client_state)

    def reset_item_state(self):
        self.items_processed = 0
        self.session_state["processed_items"] = 0
        self.session_state["receipt_history"] = {
            "processed_boundary": 0,
            "highest_observed_index": -1,
            "receipt_ids": [],
            "receipt_counts": {},
            "owned_item_ids": [],
        }
        self.session_state["item_resync"] = {}
        self.session_state["rune_reconciliation"] = {}
        self.reconnect_resync_attempted = False
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

    def _processed_receipt_ids(self):
        history = self.session_state.setdefault("receipt_history", {})
        if not isinstance(history, dict):
            history = {}
            self.session_state["receipt_history"] = history
        receipt_counts = history.get("receipt_counts", {})
        if not isinstance(receipt_counts, dict):
            receipt_counts = {}
            history["receipt_counts"] = receipt_counts
        return {
            value: count
            for value, count in receipt_counts.items()
            if isinstance(value, str)
            and value
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count > 0
        }

    def _record_processed_receipt(self, network_item):
        receipt_id = receipt_identity(network_item)
        if receipt_id is None:
            return
        history = self.session_state.setdefault("receipt_history", {})
        receipt_counts = history.setdefault("receipt_counts", {})
        if not isinstance(receipt_counts, dict):
            receipt_counts = {}
            history["receipt_counts"] = receipt_counts
        receipt_counts[receipt_id] = receipt_counts.get(receipt_id, 0) + 1

    def observe_received_item_history(self):
        """Record authoritative ownership summary without delivering effects."""
        observation = observe_received_items(
            self.items_received,
            self.items_processed,
            self._processed_receipt_ids(),
        )
        history = self.session_state.setdefault("receipt_history", {})
        owned_item_ids = sorted(set(observation.receipt_item_ids))
        changed = (
            history.get("processed_boundary") != self.items_processed
            or history.get("highest_observed_index")
            != observation.highest_observed_index
            or history.get("owned_item_ids") != owned_item_ids
        )
        history["processed_boundary"] = self.items_processed
        history["highest_observed_index"] = observation.highest_observed_index
        history["owned_item_ids"] = owned_item_ids
        if changed:
            self.persist_session_state()
        return observation

    def _reconciliation_eligibility(self, *, require_connection):
        """Apply same gameplay/map proof to manual and automatic resync."""
        if not self.item_state_ready:
            return None, "item state is not ready"
        if require_connection and (
            not getattr(self, "server", None)
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
        marker = self.read_active_map_identity(evidence=evidence)
        if marker is None:
            return None, "active AP map marker is unavailable"
        active_map = canonical_map_name(marker["runtime_map"])
        supported = {
            canonical_map_name(name)
            for name in load_foundation_contracts()["active_maps"].values()
        }
        if active_map not in supported:
            return None, "active map has no ap_rpc_v3 reconciliation entities"
        if self.items_processed > len(self.items_received):
            return None, "authoritative received-item history is incomplete"
        return evidence, None

    def _compile_reconciliation_plan_for_evidence(self, evidence):
        seed = getattr(self, "room_seed_name", None) or getattr(self, "seed_name", None)
        identity = f"{seed}-{self.team}-{self.slot}"
        observation = observe_received_items(
            self.items_received,
            self.items_processed,
            self._processed_receipt_ids(),
        )
        received = observation.historical_authoritative_item_ids
        return compile_reconciliation_plan(
            received,
            ITEM_ID_TO_COMMAND,
            ITEM_REPLAY_POLICIES,
            identity,
            evidence.epoch,
        )

    def apply_reconciliation_plan(self, plan, *, intent=RECONCILIATION_REPAIR, reason="manual"):
        """Queue silent reconcile commands through one manual/automatic path."""
        if intent != RECONCILIATION_REPAIR:
            return False, f"unsupported reconciliation intent: {intent!r}"
        for command in plan.commands:
            logger.info(
                "RESYNC_QUEUE reason=%s spool=%s item=%s stage=%s policy=%s",
                reason,
                command.spool_id,
                command.item_id,
                command.stage,
                command.policy,
            )
            if not send_command(
                command.command,
                coalesce_key=command.spool_id,
                already_queued_ok=True,
                state_key=self.state_key,
                delivery_fields={
                    "item_id": command.item_id,
                    "item_name": command.name,
                    "stage": command.stage,
                    "source": "reconciliation",
                    "intent": intent,
                },
            ):
                return False, f"failed to spool {command.spool_id}; rerun is safe"
        return True, None

    def manual_reconcile_inventory(self):
        """Queue a policy-compiled manual replay without mutating AP receipt state."""
        evidence, error = self._reconciliation_eligibility(require_connection=True)
        if error:
            return None, error
        try:
            plan = self._compile_reconciliation_plan_for_evidence(evidence)
        except ValueError as error:
            return None, str(error)

        logger.info(
            "RESYNC_START reason=manual epoch=%s boundary=%s",
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
            "skipped_never_replay=%s",
            len(plan.commands),
            plan.replayed,
            plan.special_stages,
            plan.skipped_never_replay,
        )
        queued, error = self.apply_reconciliation_plan(plan, reason="manual")
        if not queued:
            return None, error
        if not plan.commands:
            logger.info("RESYNC_NOOP reason=manual detail=no_commands")
        logger.info(
            "RESYNC_COMPLETE reason=manual commands=%s status=%s",
            len(plan.commands),
            "noop" if not plan.commands else "complete",
        )
        return plan, None

    def log_automatic_resync_noop(self, reason, detail, epoch, history_fingerprint):
        """Log changed automatic NOOPs immediately and unchanged ones periodically."""
        signature = (reason, str(detail), epoch, history_fingerprint)
        now = time.monotonic()
        previous = getattr(self, "_automatic_resync_noop_signature", None)
        logged_at = getattr(self, "_automatic_resync_noop_logged_at", None)
        if previous == signature and logged_at is not None and now - logged_at < 300.0:
            return False
        self._automatic_resync_noop_signature = signature
        self._automatic_resync_noop_logged_at = now
        logger.info(
            "RESYNC_NOOP reason=%s detail=%s epoch=%s fingerprint=%s",
            reason,
            detail,
            epoch,
            history_fingerprint,
        )
        return True

    def automatic_reconcile_inventory(self, reason):
        """Run one guarded resync for a new lifecycle/history fingerprint."""
        fingerprint = receipt_history_fingerprint(self.items_received)
        evidence, error = self._reconciliation_eligibility(require_connection=True)
        if error:
            self.log_automatic_resync_noop(
                reason,
                error,
                getattr(self, "active_native_evidence_epoch", None),
                fingerprint,
            )
            return None, error
        state = self.session_state.get("item_resync")
        if not isinstance(state, dict):
            state = {}
            self.session_state["item_resync"] = state
        if (
            state.get("runtime_epoch") == evidence.epoch
            and state.get("history_fingerprint") == fingerprint
            and state.get("status") in {"complete", "noop"}
        ):
            self.log_automatic_resync_noop(
                reason,
                "already_applied",
                evidence.epoch,
                fingerprint,
            )
            return None, None

        logger.info(
            "RESYNC_START reason=%s epoch=%s boundary=%s",
            reason,
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
            plan = self._compile_reconciliation_plan_for_evidence(evidence)
        except ValueError as error:
            state.update(
                runtime_epoch=evidence.epoch,
                history_fingerprint=fingerprint,
                status="blocked",
                reason=reason,
            )
            self.persist_session_state()
            self.log_automatic_resync_noop(reason, error, evidence.epoch, fingerprint)
            return None, str(error)

        logger.info(
            "RESYNC_PLAN reason=%s commands=%s replayed=%s special_stages=%s "
            "skipped_never_replay=%s",
            reason,
            len(plan.commands),
            plan.replayed,
            plan.special_stages,
            plan.skipped_never_replay,
        )
        queued, error = self.apply_reconciliation_plan(plan, reason=reason)
        if not queued:
            state.update(
                runtime_epoch=evidence.epoch,
                history_fingerprint=fingerprint,
                status="blocked",
                reason=reason,
            )
            self.persist_session_state()
            self.log_automatic_resync_noop(reason, error, evidence.epoch, fingerprint)
            return None, error

        status = "noop" if not plan.commands else "complete"
        state.update(
            runtime_epoch=evidence.epoch,
            history_fingerprint=fingerprint,
            status=status,
            reason=reason,
            processed_boundary=self.items_processed,
            timestamp=time.time(),
        )
        self.persist_session_state()
        if status == "noop":
            self.log_automatic_resync_noop(
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

    def rune_native_state(self):
        """Read distinct Rune surfaces only from lifecycle-proven active save."""
        if not self.has_authoritative_save_proof() or not self.active_save_slot:
            return None, "authoritative active-save proof required"
        lease = getattr(self, "runtime_observation_lease", None)
        evidence_epoch = getattr(lease, "gameplay_loaded_ns", None)
        if evidence_epoch is None:
            evidence_epoch = self.active_native_evidence_epoch or "unknown"
        return RuneNativeState.from_game_details(
            self.active_game_details(),
            save_slot=self.active_save_slot,
            evidence_epoch=evidence_epoch,
        ), None

    def compile_owned_rune_plan(self):
        if not self.item_state_ready:
            return None, "item state is not ready"
        native, error = self.rune_native_state()
        if error:
            return None, error
        try:
            mapping = rune_item_perk_mapping(ITEM_ID_TO_COMMAND, REVISION_ONE_RUNE_IDS)
            plan = compile_rune_reconciliation_plan(
                self.received_item_ids(processed_only=True),
                native,
                mapping,
                expected_rune_item_ids=REVISION_ONE_RUNE_IDS,
            )
        except ValueError as error:
            return None, str(error)
        return plan, None

    def reconcile_owned_runes(self, trigger, *, force=False):
        """Plan Rune reconciliation once per native/AP fingerprint."""
        plan, error = self.compile_owned_rune_plan()
        if error:
            logger.info("RUNE_RECONCILE_NOOP trigger=%s detail=%s", trigger, error)
            return None, error
        state = self.session_state.setdefault("rune_reconciliation", {})
        if not force and rune_plan_already_recorded(state, plan):
            logger.info(
                "RUNE_RECONCILE_NOOP trigger=%s detail=already_planned fingerprint=%s",
                trigger,
                plan.fingerprint,
            )
            return plan, None
        state.update(
            fingerprint=plan.fingerprint,
            status=plan.status,
            trigger=trigger,
            timestamp=time.time(),
            repair_candidates=len(plan.repairs),
        )
        self.persist_session_state()
        if plan.repairs:
            logger.warning(
                "RUNE_RECONCILE_BLOCKED trigger=%s candidates=%s fingerprint=%s "
                "writer=%s",
                trigger,
                len(plan.repairs),
                plan.fingerprint,
                RUNE_WRITER_EVIDENCE,
            )
        else:
            logger.info(
                "RUNE_RECONCILE_NOOP trigger=%s detail=native_state_coherent "
                "owned=%s fingerprint=%s",
                trigger,
                len(plan.entries),
                plan.fingerprint,
            )
        return plan, None

    def rune_diagnostic_lines(self):
        native, error = self.rune_native_state()
        authority = "authoritative"
        if error:
            candidate = self.active_game_details()
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
        plan, plan_error = self.compile_owned_rune_plan()
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
            if plan.repairs:
                lines.append(f"Writer proof: {RUNE_WRITER_EVIDENCE}")
        return lines

    def advance_automap_cleanup_epoch(self):
        """Open one idempotent cleanup pass after a level-ready marker."""
        previous_epoch = self.automap_cleanup_epoch
        marker = getattr(self, "cached_map_identity", None)
        epoch = marker.get("gameplay_epoch") if isinstance(marker, dict) else None
        if not valid_materialization_epoch(epoch):
            self.automap_cleanup_epoch = None
            if previous_epoch != self.automap_cleanup_epoch:
                self.automap_cleanup_retry.clear()
                self.automap_cleanup_status.clear()
                self.automap_cleanup_submitted.clear()
                self.automap_local_cleanup_owned.clear()
            return None
        self.automap_cleanup_epoch = epoch
        if previous_epoch != epoch:
            self.automap_cleanup_retry.clear()
            self.automap_cleanup_status.clear()
            self.automap_cleanup_submitted.clear()
            self.automap_local_cleanup_owned.clear()
        return self.automap_cleanup_epoch

    def _fast_travel_transition(self, event, *, reason=None, trigger=None):
        """Emit one lifecycle transition for current Fast Travel epoch."""
        state = getattr(self, "fast_travel_epoch_state", None)
        if not isinstance(state, dict):
            state = {}
        signature = (
            event,
            state.get("identity"),
            state.get("map_key"),
            state.get("epoch"),
            reason,
        )
        if signature == getattr(self, "fast_travel_last_transition", None):
            return
        self.fast_travel_last_transition = signature
        if self.fast_travel_epoch_state is state:
            state["status"] = event.lower()
        if reason is not None:
            state["pending_reason"] = reason
        logger.info(
            "[FastTravel] %s identity=%s map=%s epoch=%s completed_before_epoch=%s reason=%s trigger=%s",
            event,
            state.get("identity") or "<none>",
            state.get("map_key") or "<none>",
            state.get("epoch") or "<none>",
            state.get("completed_before_epoch", False),
            reason or "<none>",
            trigger or "<none>",
        )

    def _fast_travel_snapshot_mismatch(self, snapshot):
        """Reject epoch work when room, load, or accepted map identity changed."""
        identity, map_key, epoch = snapshot
        if identity != self.get_ap_state_key():
            return "identity_mismatch"
        accepted = getattr(self, "cached_map_identity", None)
        if not isinstance(accepted, dict):
            return "map_unavailable"
        if accepted.get("gameplay_epoch") != epoch:
            return "map_epoch_mismatch"
        accepted_map_key = accepted.get("map_key")
        if not isinstance(accepted_map_key, str):
            accepted_runtime_map = canonical_map_name(accepted.get("runtime_map", ""))
            accepted_map_key = next(
                (
                    key
                    for key, runtime_map in KNOWN_CATALOG_MAPS.items()
                    if runtime_map == accepted_runtime_map
                ),
                None,
            )
        if accepted_map_key != map_key:
            return "map_mismatch"
        return None

    def reconcile_fast_travel_unlock(self, trigger):
        """Activate native Fast Travel once per room/map/load epoch."""
        snapshot = getattr(self, "fast_travel_eligibility_snapshot", None)
        state = getattr(self, "fast_travel_epoch_state", None)
        if not isinstance(state, dict):
            self._fast_travel_transition("PENDING", reason="epoch_unavailable", trigger=trigger)
            return False
        if not isinstance(snapshot, tuple) or len(snapshot) != 3:
            if state.get("completed_before_epoch"):
                self._fast_travel_transition(
                    "PENDING", reason=state.get("ineligible_reason") or "snapshot_unavailable", trigger=trigger
                )
            return False
        identity, map_key, epoch = snapshot
        delivery_key = valid_fast_travel_delivery_key((identity, map_key, epoch))
        if delivery_key is None:
            self._fast_travel_transition(
                "PENDING", reason="malformed_epoch", trigger=trigger
            )
            return False
        snapshot_mismatch = self._fast_travel_snapshot_mismatch(snapshot)
        if snapshot_mismatch:
            self._fast_travel_transition(
                "PENDING", reason=snapshot_mismatch, trigger=trigger
            )
            return False
        if delivery_key in self.fast_travel_submitted:
            self._fast_travel_transition("COMMAND_QUEUED_UNVERIFIED", trigger=trigger)
            return False
        if not self.has_authoritative_save_proof():
            self._fast_travel_transition("PENDING", reason="save_proof_unavailable", trigger=trigger)
            return False
        if not getattr(self, "item_state_ready", False):
            self._fast_travel_transition("PENDING", reason="item_state_unavailable", trigger=trigger)
            return False
        if not rpc_execution_enabled():
            self._fast_travel_transition("PENDING", reason="rpc_not_ready", trigger=trigger)
            return False
        now = time.monotonic()
        retry_deadline = state.get("retry_deadline")
        retry_waiting = isinstance(retry_deadline, (int, float)) and now < retry_deadline
        if retry_waiting:
            state["status"] = "pending"
            return False
        if retry_deadline is None:
            self._fast_travel_transition("READY", trigger=trigger)
        command = "ai_ScriptCmdEnt ap_fast_travel_unlock activate"
        if not send_command(
            command,
            coalesce_key=stable_spool_id(
                "fast-travel", identity, map_key, epoch
            ),
            already_queued_ok=True,
            state_key=self.state_key,
            materialization_lease=epoch,
            execution_class=MAP_ENTITY_SAFE,
            operation=FAST_TRAVEL_UNLOCK,
        ):
            retry_attempt = state.get("retry_attempt", 0)
            if isinstance(retry_attempt, bool) or not isinstance(retry_attempt, int):
                retry_attempt = 0
            retry_attempt += 1
            backoff = min(
                FAST_TRAVEL_RETRY_MAX_SECONDS,
                FAST_TRAVEL_RETRY_BASE_SECONDS
                * (2 ** min(retry_attempt - 1, 3)),
            )
            state["retry_attempt"] = retry_attempt
            state["retry_deadline"] = now + backoff
            self._fast_travel_transition("RETRY", reason="queue_unavailable", trigger=trigger)
            return False
        state["retry_deadline"] = None
        state["retry_attempt"] = 0
        self.fast_travel_submitted[delivery_key] = time.time()
        self._fast_travel_transition("COMMAND_QUEUED_UNVERIFIED", trigger=trigger)
        return True

    def snapshot_fast_travel_eligibility(self, marker_data=None):
        """Capture server-history eligibility once for each gameplay epoch."""
        if not isinstance(marker_data, dict):
            marker_data = (
                getattr(self, "cached_map_identity", None)
                or getattr(self, "pending_map_identity", None)
            )
        if not isinstance(marker_data, dict):
            return None
        epoch = marker_data.get("gameplay_epoch")
        existing = getattr(self, "fast_travel_epoch_state", None)
        if isinstance(existing, dict) and existing.get("epoch") is not None:
            return getattr(self, "fast_travel_eligibility_snapshot", None)
        if epoch is None:
            return None

        identity = self.get_ap_state_key()
        runtime_map = canonical_map_name(marker_data.get("runtime_map", ""))
        map_key = next(
            (key for key, value in KNOWN_CATALOG_MAPS.items() if value == runtime_map),
            None,
        )
        mission_id = FAST_TRAVEL_MISSION_COMPLETE_IDS.get(map_key)
        checked = getattr(self, "checked_locations", None)
        history_available = isinstance(checked, (set, frozenset, list, tuple))
        completed_before_epoch = bool(history_available and mission_id in checked)
        ineligible_reason = None
        if not identity:
            ineligible_reason = "room_identity_unavailable"
        elif map_key not in FAST_TRAVEL_MAP_KEYS or mission_id is None:
            ineligible_reason = "map_not_supported"
        elif not history_available:
            ineligible_reason = "server_history_unavailable"
        elif not completed_before_epoch:
            ineligible_reason = "not_completed_before_epoch"

        self.fast_travel_epoch_state = {
            "identity": identity,
            "map_key": map_key,
            "epoch": epoch,
            "completed_before_epoch": completed_before_epoch,
            "ineligible_reason": ineligible_reason,
            "status": "epoch",
            "pending_reason": None,
            "retry_attempt": 0,
            "retry_deadline": None,
        }
        self.fast_travel_eligibility_snapshot = (
            (identity, map_key, epoch)
            if not ineligible_reason
            else None
        )
        self.fast_travel_last_transition = None
        self._fast_travel_transition("EPOCH")
        if ineligible_reason:
            self._fast_travel_transition("INELIGIBLE", reason=ineligible_reason)
        return self.fast_travel_eligibility_snapshot

    async def process_level_ready(self, newest_path=None):
        """Run complete level-ready reconciliation for one accepted load epoch."""
        evidence = read_gameplay_save_evidence()
        self.read_active_map_identity(evidence=evidence)
        self.snapshot_fast_travel_eligibility()
        marker = getattr(self, "cached_map_identity", None)
        epoch = marker.get("gameplay_epoch") if isinstance(marker, dict) else None
        pending = getattr(self, "pending_level_ready", {})
        if not isinstance(epoch, str) or epoch not in pending:
            return False
        in_flight = getattr(self, "level_ready_in_flight", set())
        if epoch in in_flight:
            return False
        evidence_state = getattr(evidence, "state", None)
        if evidence_state != "gameplay":
            pending_signature = (epoch, "evidence_not_gameplay", evidence_state)
            if pending_signature != getattr(self, "last_level_ready_pending_signature", None):
                self.last_level_ready_pending_signature = pending_signature
                logger.info(
                    "[RPC] LEVEL_READY_PENDING epoch=%s reason=evidence_not_gameplay state=%s",
                    epoch,
                    evidence_state or "unavailable",
                )
                logger.info(
                    "[MAP] RUNTIME_EFFECTS_PENDING reason=evidence_not_gameplay state=%s",
                    evidence_state or "unavailable",
                )
            return False
        if not self.has_authoritative_save_proof():
            pending_signature = (epoch, "save_proof_unavailable", evidence_state)
            if pending_signature != getattr(self, "last_level_ready_pending_signature", None):
                self.last_level_ready_pending_signature = pending_signature
                logger.info(
                    "[RPC] LEVEL_READY_PENDING epoch=%s reason=save_proof_unavailable",
                    epoch,
                )
                logger.info(
                    "[MAP] RUNTIME_EFFECTS_PENDING reason=save_proof_unavailable state=%s",
                    evidence_state,
                )
            return False
        in_flight.add(epoch)
        self.level_ready_in_flight = in_flight
        self.last_level_ready_pending_signature = None
        logger.info("[RPC] LEVEL_READY_EXECUTE epoch=%s", epoch)
        source_path = pending.get(epoch) or newest_path or marker.get("path")
        try:
            if not rpc_execution_enabled():
                set_rpc_execution(True)
            reconciliation_epoch = self.advance_reconciliation_epoch("level_ready")
            logger.info(
                "[RPC] Level-ready signal received (%s). RPC armed; "
                "perk reconciliation epoch %s queued behind native safety gate.",
                os.path.basename(source_path) if source_path else "<marker>",
                reconciliation_epoch,
            )
            self.reconcile_owned_runes("level_ready")
            self.advance_automap_cleanup_epoch()
            self.reconcile_checked_automap_cleanup("level_ready")
            await self.check_mission_challenge_locations()
            self.automatic_reconcile_inventory("level_ready")
            self.reconcile_fast_travel_unlock("level_ready")
            pending.pop(epoch, None)
            self.completed_level_ready_epochs.add(epoch)
            return True
        finally:
            in_flight.discard(epoch)

    def reconcile_checked_automap_cleanup(self, trigger):
        """Remove only isolated AP visuals for server-checked map locations."""
        if not valid_materialization_epoch(self.automap_cleanup_epoch):
            return False
        map_name = canonical_map_name(self.current_map_name or "")
        map_key = next(
            (key for key, runtime_map in KNOWN_CATALOG_MAPS.items()
             if canonical_map_name(runtime_map) == map_name),
            None,
        )
        entries = [
            entry for entry in AUTOMAP_VISUALS_BY_MAP.get(map_key or "", {}).values()
            if entry["classification"] == "visible_cleanup"
        ]
        if not entries:
            return False
        room_identity = self.get_ap_state_key()
        epoch = self.automap_cleanup_epoch
        if not room_identity:
            self._automap_cleanup_transition((epoch, "", map_name, ""), "PENDING", "room_identity_unavailable")
            return False
        checked = getattr(self, "checked_locations", None)
        if not self.server_checked_locations_ready or not isinstance(checked, (set, frozenset, list, tuple)):
            self._automap_cleanup_transition((epoch, room_identity, map_name, ""), "PENDING", "checked_locations_unavailable")
            return False
        if not rpc_execution_enabled():
            self._automap_cleanup_transition((epoch, room_identity, map_name, ""), "PENDING", "rpc_not_ready")
            return False
        checked = set(checked)
        changed = False
        now = time.monotonic()
        for entry in sorted(entries, key=lambda item: item["location_id"]):
            location_id = entry["location_id"]
            if location_id not in checked:
                continue
            entity_name = entry["reconciliation_entity"]
            delivery_key = (room_identity, map_name, str(location_id))
            runtime_key = (epoch, *delivery_key)
            if (epoch, location_id) in self.automap_local_cleanup_owned:
                self._automap_cleanup_transition(
                    runtime_key, "LOCAL_FLOW_OWNS_EFFECT", trigger
                )
                continue
            if self.automap_cleanup_submitted.get(delivery_key) == self.automap_cleanup_epoch:
                self._automap_cleanup_transition(
                    runtime_key, "COMMAND_QUEUED_UNVERIFIED", trigger
                )
                continue
            retry = self.automap_cleanup_retry.setdefault(runtime_key, {"attempt": 0, "deadline": 0.0})
            if now < retry["deadline"]:
                self._automap_cleanup_transition(runtime_key, "RETRY_WAIT", trigger)
                continue
            command_id = stable_spool_id(
                "automap-cleanup",
                self.automap_cleanup_session,
                room_identity,
                map_name,
                location_id,
                self.automap_cleanup_epoch,
            )
            command = f"ai_ScriptCmdEnt {entity_name} activate"
            if not send_command(
                command,
                coalesce_key=command_id,
                already_queued_ok=True,
                state_key=self.state_key,
                materialization_lease=epoch,
                execution_class=MAP_ENTITY_SAFE,
                operation=CHECKED_VISUAL_HIDE,
            ):
                retry["attempt"] += 1
                retry["deadline"] = now + min(
                    AUTOMAP_CLEANUP_RETRY_MAX_SECONDS,
                    AUTOMAP_CLEANUP_RETRY_BASE_SECONDS * (2 ** min(retry["attempt"] - 1, 3)),
                )
                self._automap_cleanup_transition(runtime_key, "RETRY", "queue_unavailable")
                continue
            self.automap_cleanup_submitted[delivery_key] = self.automap_cleanup_epoch
            self.automap_cleanup_retry.pop(runtime_key, None)
            changed = True
            self._automap_cleanup_transition(
                runtime_key, "COMMAND_QUEUED_UNVERIFIED", trigger
            )
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

    def record_local_automap_cleanup_ownership(self, location_id, event_paths):
        """Bind local pickup cleanup ownership to current materialization epoch."""
        marker = getattr(self, "cached_map_identity", None)
        if not isinstance(marker, dict):
            return False
        epoch = marker.get("gameplay_epoch")
        map_key = marker.get("map_key")
        entry = AUTOMAP_VISUALS_BY_MAP.get(map_key or "", {}).get(location_id)
        if not valid_materialization_epoch(epoch) or not entry:
            return False
        if entry.get("classification") != "visible_cleanup":
            return False
        try:
            event_mtime = max(Path(path).stat().st_mtime_ns for path in event_paths)
        except (OSError, ValueError):
            return False
        if event_mtime < marker.get("mtime_ns", 0):
            return False
        self.automap_local_cleanup_owned.add((epoch, location_id))
        self._automap_cleanup_transition(
            (epoch, self.get_ap_state_key() or "", marker.get("runtime_map", ""), str(location_id)),
            "LOCAL_FLOW_OWNS_EFFECT",
            "native_ap_check_event",
        )
        return True

    def _automap_cleanup_transition(self, delivery_key, status, reason):
        previous = self.automap_cleanup_status.get(delivery_key)
        if previous == status:
            return
        self.automap_cleanup_status[delivery_key] = status
        logger.info(
            "[Automap] cleanup transition status=%s previous=%s reason=%s key=%s",
            status, previous, reason, delivery_key,
        )

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
        """Archive dev1 jobs under their versioned namespace."""
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
                            already_queued_ok=True, state_key=self.state_key):
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
            if state["status"] == "queued" and not command_spool_exists(
                self.bootstrap_command_id(action_name), self.state_key
            ):
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
                network_item.item,
                item_index,
                intent=PRESENTATION_REPAIR,
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
        *,
        intent=HISTORICAL_OWNERSHIP,
        include_notification=None,
        classification=None,
    ):
        if intent not in {
            NEW_RECEIPT,
            HISTORICAL_OWNERSHIP,
            RECONCILIATION_REPAIR,
            PRESENTATION_REPAIR,
        }:
            return None, f"unsupported item delivery intent: {intent!r}"
        if intent == HISTORICAL_OWNERSHIP:
            return [], "historical ownership observed"
        receipt = intent == NEW_RECEIPT and (
            True if include_notification is None else bool(include_notification)
        )
        receipt = receipt and (
            ITEM_REPLAY_POLICIES[item_id].receipt_feedback == AP_RECEIPT_FEEDBACK
        )
        # Package capability validator marker: receipt=ENABLE_ITEM_NOTIFICATIONS.
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
        namespace = queue_session_namespace(self.state_key)
        if namespace is None:
            raise RuntimeError("cannot create receipt command without active AP identity")
        return f"recv-{namespace}-{item_index:06d}-item-{item_id}-{suffix}"

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
        intent=HISTORICAL_OWNERSHIP,
        include_notification=None,
        classification=None,
        packet_received_ns=None,
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
            packet_to_spool_ms = (
                (time.monotonic_ns() - packet_received_ns) / 1_000_000
                if packet_received_ns is not None
                else None
            )
            if not send_command(
                commands[command_index],
                coalesce_key=command_id,
                already_queued_ok=True,
                state_key=self.state_key,
                delivery_fields={
                    "receipt_index": item_index,
                    "item_id": item_id,
                    "item_name": self.delivery_item_name(item_id),
                    "command_ordinal": command_index,
                    "packet_received_monotonic_ns": packet_received_ns,
                    "packet_to_spool_ms": packet_to_spool_ms,
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
        if event_id in self.received_deathlink_event_ids:
            logger.info("[DeathLink] Ignored persisted duplicate event %s.", event_id[:12])
            return
        result = self.deathlink_receiver.receive(event_id, now)
        if result.detail == "duplicate":
            logger.info("[DeathLink] Ignored duplicate received event %s.", event_id[:12])
            return
        if result.state is ReceiveState.FAILED:
            logger.warning("[DeathLink] Rejected %s: bounded receive queue is full.", event_id[:12])
            return
        self.received_deathlink_event_ids.add(event_id)
        self.persist_session_state()
        logger.info("[DeathLink] Received logical event %s; queued for safe gameplay.", event_id[:12])

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
                state_key=self.state_key,
            ),
            command_in_flight=lambda: command_spool_exists(
                DEATHLINK_KILL_COALESCE_KEY, self.state_key
            ),
        )
        self.deathlink_instrumentation.append(
            {
                "event_id": result.event_id,
                "state": result.state.value if result.state else None,
                "detail": result.detail,
                "mode": self.death_link_mode,
                "attempts": self.deathlink_receiver.active.attempts
                if self.deathlink_receiver.active
                and self.deathlink_receiver.active.event_id == result.event_id
                else None,
                "timestamp": time.time(),
            }
        )
        self.deathlink_instrumentation = self.deathlink_instrumentation[-128:]
        event_id = (result.event_id or "unknown")[:12]
        active = self.deathlink_receiver.active
        if result.detail == "dispatched":
            hit_num = active.attempts if active else 1
            logger.info(
                "[DeathLink] %s hit %d queued; command in flight.",
                event_id,
                hit_num,
            )
        elif result.detail == "burst_wait":
            logger.info(
                "[DeathLink] %s hit 1 delivered; waiting ~500ms before second hit.",
                event_id,
            )
        elif result.state is ReceiveState.APPLIED:
            logger.info(
                "[DeathLink] %s lethal burst complete (%s).",
                event_id,
                result.detail,
            )
        elif result.state is ReceiveState.RESOLVED:
            logger.info(
                "[DeathLink] %s lethal burst resolved (%s).",
                event_id,
                result.detail,
            )
        elif result.state in {ReceiveState.EXPIRED, ReceiveState.FAILED}:
            discard_queued_coalesced_command(DEATHLINK_KILL_COALESCE_KEY)
            state_name = result.state.value.lower() if result.state else "unknown"
            logger.warning(
                "[DeathLink] %s %s (%s); event cleared without claiming success.",
                event_id,
                state_name,
                result.detail,
            )

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
        receive_result = self.deathlink_receiver.confirm_local_death(time.monotonic())
        if receive_result.detail in {
            "echo_suppressed",
            "late_echo_suppressed",
            "second_hit_cancelled_player_dead",
        }:
            discard_queued_coalesced_command(
                DEATHLINK_KILL_COALESCE_KEY, self.state_key
            )
            logger.info(
                "[DeathLink] %s confirmed by death telemetry; linked echo suppressed (%s).",
                (receive_result.event_id or "unknown")[:12],
                receive_result.detail,
            )
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
        marker = self.read_active_map_identity(evidence=read_gameplay_save_evidence())
        active_map = canonical_map_name(marker["runtime_map"]) if marker else ""
        if not active_map:
            return
        details = self.active_game_details()
        if not details:
            return
        record_map = canonical_map_name(details.get("mapName", ""))
        if not record_map:
            return
        mtime = details.get("_mtime_ns")
        if mtime == self.last_goal_details_mtime:
            return
        self.last_goal_details_mtime = mtime

        details_path = details.get("_path")
        if not details_path:
            return

        is_completed = details.get("completed") == "1"
        key = (details_path, record_map)
        prev_status = self.session_map_completion_states.get(key)
        self.session_map_completion_states[key] = "1" if is_completed else "0"

        # Edge fallback triggers only on a fresh in-session transition from incomplete to completed
        fresh_completion = (prev_status == "0" and is_completed)

        if record_map == CULTIST_BASE_MAP:
            if self.cultist_autosave_path != details_path:
                self.cultist_autosave_path = details_path
                self.persist_session_state()
                logger.info(
                    f"[Goal] Tracking Cultist Base completion from {details_path}."
                )
            return

        completed_cultist_base = (
            record_map != CULTIST_BASE_MAP
            and is_completed
            and details_path == self.cultist_autosave_path
        )
        if completed_cultist_base:
            await self.send_campaign_goal("legacy save fallback")

        if fresh_completion and record_map in {"e3m4_boss", "game/sp/e3m4_boss/e3m4_boss"}:
            matching = [p for p in PUBLISHERS if p.key == "final_sin_mission_complete"]
            for publisher in matching:
                await DoomEternalContext.execute_publisher(
                    self, publisher, "save_fallback", "Final Sin save fallback"
                )

        if fresh_completion and record_map in {"e1m4_boss", "game/sp/e1m4_boss/e1m4_boss"}:
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

        if marker is None:
            self.last_rpc_map_name = None
            self.current_map_name = None
            return
        map_name = marker["runtime_map"]

        self.current_map_name = map_name
        if getattr(evidence, "state", None) != "gameplay":
            return
        self.snapshot_fast_travel_eligibility()
        self.reconcile_fast_travel_unlock("map_ready")
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
            sleep_duration = 0.05 if self.deathlink_receiver.active is not None else 1.0
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
                self.record_local_automap_cleanup_ownership(location_id, paths)
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
                    evidence = read_gameplay_save_evidence()
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

                markers = discover_telemetry_markers()
                if markers:
                    _, newest_path = markers[-1]
                    try:
                        self.ingest_visible_runtime_lifecycle(
                            evidence=evidence,
                            lifecycle_markers=markers,
                        )
                    except Exception as e:
                        logger.error("[RPC] Auto-RPC failed to consume telemetry ready file %s: %s", newest_path, e)

                    for _mtime, path in markers:
                        try:
                            os.remove(path)
                        except OSError:
                            pass

                if not await self.process_level_ready(newest_path if markers else None):
                    await self.check_mission_challenge_locations()
                    self.reconcile_fast_travel_unlock("readiness")

                await self.process_pending_item_receipts("tracker")

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

    

async def amain(launch_args=None):
    start_bridge_logger()
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

    ctx.server_task = asyncio.create_task(server_loop(ctx), name="server loop")
    def report_server_stop(task):
        if ctx.exit_event.is_set():
            ctx.reset_queue_session_authority("server_loop_stopped")
            return
        ctx.reset_queue_session_authority("server_loop_stopped")
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
    ctx.run_cli()

    await ctx.exit_event.wait()
    emit_launcher_event("client_stopping")
    item_delivery_task = getattr(ctx, "_item_delivery_task", None)
    if item_delivery_task is not None and not item_delivery_task.done():
        item_delivery_task.cancel()
        await asyncio.gather(item_delivery_task, return_exceptions=True)
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
