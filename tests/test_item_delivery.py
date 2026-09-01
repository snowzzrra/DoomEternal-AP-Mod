import asyncio
import enum
import importlib
import io
import sys
from collections import deque, namedtuple
from functools import wraps
from pathlib import Path
from types import ModuleType, SimpleNamespace

NetworkItem = namedtuple("NetworkItem", "item location player flags")


def _bridge_module():
    try:
        return importlib.import_module("doom_eap.runtime.bridge_client")
    except ModuleNotFoundError:
        colorama = ModuleType("colorama")
        colorama.init = lambda: None
        colorama.deinit = lambda: None
        utils = ModuleType("Utils")
        utils.init_logging = lambda *_args, **_kwargs: None
        common = ModuleType("CommonClient")

        class ClientCommandProcessor:
            pass

        class CommonContext:
            def on_print_json(self, _args):
                pass

        common.ClientCommandProcessor = ClientCommandProcessor
        common.CommonContext = CommonContext
        common.get_base_parser = lambda: None
        common.gui_enabled = lambda: False
        common.server_loop = lambda *_args, **_kwargs: None
        net = ModuleType("NetUtils")
        net.ClientStatus = SimpleNamespace(CLIENT_GOAL=30)
        net.HintStatus = enum.IntEnum(
            "HintStatus",
            {"HINT_UNSPECIFIED": 0, "HINT_NO_PRIORITY": 10, "HINT_AVOID": 20, "HINT_PRIORITY": 30, "HINT_FOUND": 40},
        )
        net.Hint = namedtuple(
            "Hint", "receiving_player finding_player location item found entrance item_flags status",
            defaults=("", 0, net.HintStatus.HINT_UNSPECIFIED),
        )
        net.JSONMessagePart = dict
        net.JSONTypes = enum.Enum(
            "JSONTypes",
            {
                name: name
                for name in (
                    "text", "color", "player_id", "player_name", "item_name",
                    "item_id", "location_name", "location_id", "hint_status",
                )
            },
            type=str,
        )
        sys.modules.update(
            colorama=colorama,
            Utils=utils,
            CommonClient=common,
            NetUtils=net,
        )
        return importlib.import_module("doom_eap.runtime.bridge_client")


bridge = _bridge_module()


def _run_async(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


@_run_async
async def test_launcher_control_forwards_supervisor_chat_frame(monkeypatch, tmp_path):
    from doom_eap.launcher.launcher_supervisor import BridgeSupervisor

    class Stdin:
        def __init__(self):
            self.writes = []

        def write(self, text):
            self.writes.append(text)

        def flush(self):
            pass

    class Process:
        def __init__(self):
            self.stdin = Stdin()

        def poll(self):
            return None

    supervisor = BridgeSupervisor(
        entrypoint=tmp_path / "bridge.py",
        application_dir=tmp_path,
        config_path=tmp_path / "config.json",
        profile_id="test-launcher-control",
        event_sink=lambda _event: None,
        log_sink=lambda _line: None,
    )
    supervisor._process = Process()
    text = "  !hint Super Shotgun  "
    supervisor.send_chat(text)

    context = SimpleNamespace(
        exit_event=asyncio.Event(),
        send_launcher_chat=__import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(),
    )
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO("".join(supervisor._process.stdin.writes)))

    await bridge.launcher_control_loop(context)

    context.send_launcher_chat.assert_awaited_once_with(text)
    assert context.exit_event.is_set()


def _context(items=(), *, processed=0, ready=True):
    context = object.__new__(bridge.DoomEternalContext)
    context.items_received = list(items)
    context.items_processed = processed
    context.item_state_ready = ready
    context._item_delivery_lock = asyncio.Lock()
    context._item_delivery_task = None
    context._item_delivery_wakeup = False
    context._item_delivery_waiting_for_state = False
    context._packet_received_ranges = deque(maxlen=bridge.PACKET_TIMING_RANGE_LIMIT)
    context._item_session_generation = 1
    context.state_key = "room:0:1"
    context.session_state = {"receipt_history": {"receipt_ids": [], "receipt_counts": {}}}
    context.client_state = {"version": bridge.CLIENT_STATE_VERSION, "sessions": {}}
    context.item_delivery_blocked = False
    context.item_delivery_blocked_info = None
    context.item_names = {}
    context.player_names = {1: "Self", 2: "Remote"}
    context.slot = 1
    context.team = 0
    context.slot_info = {}
    context.locations_info = {}
    context.slot_concerns_self = lambda player: player == context.slot
    context.current_map_name = None
    context.active_save_slot = None
    context.output = lambda *_args, **_kwargs: None
    context.persist_session_state = lambda: None
    context.onboard_bootstrap = lambda *_args, **_kwargs: None
    context.reconcile_owned_perks = lambda *_args, **_kwargs: None
    context.delivery_item_name = lambda item_id: f"item-{item_id}"
    context._record_processed_receipt = lambda item: None
    context.observe_received_item_history = lambda: SimpleNamespace(duplicates=())
    context.fast_travel_submitted = set()
    context.fast_travel_eligibility_snapshot = None
    context.fast_travel_epoch_state = None
    context.fast_travel_last_transition = None
    context.cached_map_identity = None
    context.pending_map_identity = None
    context.pending_level_ready = {}
    context.completed_level_ready_epochs = set()
    context.last_accepted_marker_mtime = 0
    context.last_accepted_map_evidence_epoch = None
    context.last_marker_reject_reason = None
    context.active_save_proof_authoritative = False
    context.active_save_proof_slot = None
    context.active_save_proof_evidence_epoch = None
    context.active_save_proof_load_epoch = None
    context.runtime_observers_frozen = False
    context.invalidate_map_identity = bridge.DoomEternalContext.invalidate_map_identity.__get__(context)
    context.invalidate_active_save_proof = bridge.DoomEternalContext.invalidate_active_save_proof.__get__(context)
    context.snapshot_fast_travel_eligibility = bridge.DoomEternalContext.snapshot_fast_travel_eligibility.__get__(context)
    context.accept_map_identity = bridge.DoomEternalContext.accept_map_identity.__get__(context)
    context.advance_known_map_materialization = bridge.DoomEternalContext.advance_known_map_materialization.__get__(context)
    context.ingest_visible_runtime_lifecycle = bridge.DoomEternalContext.ingest_visible_runtime_lifecycle.__get__(context)
    bridge.received_item_classification = lambda *_args, **_kwargs: {}
    return context


def test_archipelago_event_formatter_sanitizes_typed_parts_and_flags(monkeypatch):
    context = _context()
    args = {
        "data": [
            {"type": "player_id", "text": "1"},
            {"type": "text", "text": " found "},
            {"type": "player_id", "text": "2"},
            {"type": "item_name", "text": "Trap <item>", "flags": 0b111},
            {"type": "item_name", "text": "Progression", "flags": 0b011},
            {"type": "item_name", "text": "Useful", "flags": 0b010},
            {"type": "item_name", "text": "Filler", "flags": 0},
            {"type": "item_id", "text": "bad", "player": 1, "flags": 0},
            {"type": "unknown", "text": "<raw>"},
        ],
    }
    emitted = []
    monkeypatch.setattr(bridge, "emit_launcher_event", lambda event_type, **payload: emitted.append((event_type, payload)))
    context.on_print_json(args)
    assert emitted[0][0] == "archipelago"
    event = emitted[0][1]

    assert event["schema"] == 1
    assert [segment["self"] for segment in event["segments"][:3:2]] == [True, False]
    assert [segment["classification"] for segment in event["segments"][3:7]] == [
        "trap", "progression", "useful", "filler",
    ]
    assert event["segments"][7]["type"] == "text"
    assert event["segments"][8] == {"type": "text", "text": "<raw>"}
    assert "<raw>" in event["plain"]


def _spool(context, calls, result=True):
    def spool(item_id, item_index, **_kwargs):
        calls.append((item_index, item_id))
        return result, f"item-{item_id}"

    context.spool_item_commands = spool


@_run_async
async def test_duplicate_history_signal_has_no_second_delivery():
    item = NetworkItem(7, 7, 1, 0)
    context = _context([item])
    calls = []
    _spool(context, calls)
    bridge.ITEM_ID_TO_COMMAND = {7: "simple"}
    bridge.ITEM_CLASSIFICATION_IDENTITY = {}
    context.observe_received_item_history = lambda: SimpleNamespace(
        duplicates=(SimpleNamespace(index=0),)
    )

    await context.process_pending_item_receipts("packet")
    await context.process_pending_item_receipts("tracker")

    assert calls == []
    assert context.items_processed == 1


@_run_async
async def test_spool_failure_preserves_boundary_and_tracker_retry_succeeds():
    context = _context([NetworkItem(8, 8, 1, 0)])
    attempts = []

    def spool(item_id, item_index, **_kwargs):
        attempts.append(item_index)
        return (len(attempts) > 1), "item-8"

    context.spool_item_commands = spool
    bridge.ITEM_ID_TO_COMMAND = {8: "simple"}
    bridge.ITEM_CLASSIFICATION_IDENTITY = {}

    await context.process_pending_item_receipts("packet")
    assert context.items_processed == 0
    await context.process_pending_item_receipts("tracker")

    assert attempts == [0, 0]
    assert context.items_processed == 1


@_run_async
async def test_durable_spool_gate_arm_failure_rearms_on_tracker_retry(
    monkeypatch, tmp_path
):
    item = NetworkItem(8, 8, 1, 0)
    context = _context([item])
    context.item_activation_commands = lambda *_args, **_kwargs: (["give-item-8"], "item-8")
    context.spool_item_commands = bridge.DoomEternalContext.spool_item_commands.__get__(context)
    bridge.ITEM_ID_TO_COMMAND = {8: "simple"}
    bridge.ITEM_CLASSIFICATION_IDENTITY = {}
    monkeypatch.setattr(bridge, "QUEUE_DIR", str(tmp_path))
    gate_calls = []

    def arm_gate(enabled):
        gate_calls.append(enabled)
        if len(gate_calls) == 1:
            raise OSError("gate unavailable")

    monkeypatch.setattr(bridge, "set_rpc_execution", arm_gate)

    await context.process_pending_item_receipts("packet")
    command_id = context.item_command_id(8, 0, 0, "give-item-8")
    assert context.items_processed == 0
    assert (tmp_path / f"{command_id}.cmd").is_file()

    await context.process_pending_item_receipts("tracker")

    assert gate_calls == [True, True]
    assert context.items_processed == 1


@_run_async
async def test_reconnect_overlap_silent_and_new_tail_delivered():
    first = NetworkItem(10, 100, 1, 0)
    overlap = NetworkItem(9, 99, 1, 0)
    inserted = NetworkItem(8, -2, 1, 0)
    tail = NetworkItem(11, 101, 1, 0)
    context = _context([first, overlap], processed=2)
    context._processed_receipt_ids = lambda: {
        bridge.receipt_identity(first): 1,
        bridge.receipt_identity(overlap): 1,
    }
    context.observe_received_item_history = (
        bridge.DoomEternalContext.observe_received_item_history.__get__(context)
    )
    # CommonClient accepts index=0 by replacing its authoritative list.
    context.items_received = [inserted, first, overlap, tail]
    calls = []
    _spool(context, calls)
    bridge.ITEM_ID_TO_COMMAND = {8: "simple", 9: "simple", 10: "simple", 11: "simple"}
    bridge.ITEM_CLASSIFICATION_IDENTITY = {}

    await context.process_pending_item_receipts("tracker")

    assert calls == [(3, 11)]
    assert context.items_processed == 4


def test_set_rpc_execution_enable_and_disable_lifecycle(tmp_path, monkeypatch):
    gate_file = tmp_path / "ap_rpc_enabled"
    monkeypatch.setattr(bridge, "RPC_GATE_PATH", str(gate_file))

    assert not bridge.rpc_execution_enabled()
    assert bridge.set_rpc_execution(True)
    assert bridge.rpc_execution_enabled()
    assert gate_file.is_file()

    assert bridge.set_rpc_execution(False)
    assert not bridge.rpc_execution_enabled()
    assert not gate_file.exists()


def test_set_rpc_execution_permanent_disable_failure_raises(tmp_path, monkeypatch):
    import pytest

    gate_file = tmp_path / "ap_rpc_enabled"
    gate_file.write_text("enabled\n", encoding="utf-8")
    monkeypatch.setattr(bridge, "RPC_GATE_PATH", str(gate_file))

    def failing_remove(path):
        raise PermissionError("Access is denied")

    monkeypatch.setattr(bridge.os, "remove", failing_remove)

    with pytest.raises(PermissionError):
        bridge.set_rpc_execution(False)


def test_active_map_marker_lifecycle_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "INV_DUMP_DIR", str(tmp_path))

    # Create canonical marker and suffixed marker
    marker_content = (
        "AP_ACTIVE_MAP_V1 map_key=e1m1_intro runtime_map=game/sp/e1m1_intro/e1m1_intro "
        "marker=AP_MAP_START_E1M1_INTRO\n"
    )
    canonical = tmp_path / "ap_active_map_e1m1_intro.txt"
    suffixed = tmp_path / "ap_active_map_e1m1_intro_0.txt"
    unrelated = tmp_path / "sintaxe.txt"

    canonical.write_text(marker_content, encoding="utf-8")
    suffixed.write_text(marker_content, encoding="utf-8")
    unrelated.write_text("important user debug probe\n", encoding="utf-8")

    markers = bridge.discover_active_map_markers()
    assert len(markers) == 2

    ctx = _context([])
    ctx.last_accepted_marker_mtime = 0
    accepted = ctx.ingest_visible_runtime_lifecycle()
    assert accepted is True
    assert ctx.cached_map_identity["map_key"] == "e1m1_intro"
    assert ctx.current_map_name == "game/sp/e1m1_intro/e1m1_intro"

    # Consumed marker files are removed from disk
    assert not canonical.exists()
    assert not suffixed.exists()

    # Unrelated user file is strictly preserved
    assert unrelated.is_file()
    assert unrelated.read_text(encoding="utf-8") == "important user debug probe\n"

    # In-memory authority remains held
    assert ctx.cached_map_identity["map_key"] == "e1m1_intro"


def test_cleanup_active_map_markers_delete_failure_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "INV_DUMP_DIR", str(tmp_path))

    marker_content = (
        "AP_ACTIVE_MAP_V1 map_key=e1m1_intro runtime_map=game/sp/e1m1_intro/e1m1_intro "
        "marker=AP_MAP_START_E1M1_INTRO\n"
    )
    marker = tmp_path / "ap_active_map_e1m1_intro.txt"
    marker.write_text(marker_content, encoding="utf-8")

    def failing_remove(path):
        raise OSError("Permission denied")

    monkeypatch.setattr(bridge.os, "remove", failing_remove)

    # Should not raise
    bridge.cleanup_active_map_markers()
    assert marker.is_file()


@_run_async
async def test_launcher_chat_uses_commonclient_say_payload():
    context = object.__new__(bridge.DoomEternalContext)
    context.server = SimpleNamespace(socket=SimpleNamespace(open=True, closed=False))
    context.on_user_say = lambda text: text
    sent = []

    async def send_msgs(payload):
        sent.append(payload)

    context.send_msgs = send_msgs
    await context.send_launcher_chat("  !hint Super Shotgun  ")
    assert sent == [[{"cmd": "Say", "text": "  !hint Super Shotgun  "}]]


def _hint_context(*, team=1, slot=2, seed="test-seed"):
    context = object.__new__(bridge.DoomEternalContext)
    context.team = team
    context.slot = slot
    context.room_seed_name = seed
    context.stored_data = {}
    context.stored_data_notification_keys = set()
    context.player_names = {2: "Doom Slayer", 3: "Other Player"}
    context.item_names = SimpleNamespace(lookup_in_slot=lambda item, player: f"Item {item} for {player}")
    context.location_names = SimpleNamespace(lookup_in_slot=lambda location, player: f"Location {location} in {player}")
    context.locations_info = {}
    return context


def _local_archipelago_packet_handler():
    """Load local CommonClient handler without disturbing bridge import fixture."""
    archipelago_root = Path(__file__).resolve().parents[2] / "Archipelago"
    saved_fake_modules = {}
    for name in ("CommonClient", "Utils", "NetUtils"):
        module = sys.modules.get(name)
        if module is not None and not getattr(module, "__file__", None):
            saved_fake_modules[name] = module
            del sys.modules[name]
    saved_module_update = sys.modules.pop("ModuleUpdate", None)
    module_update = ModuleType("ModuleUpdate")
    module_update.update = lambda: None
    sys.modules["ModuleUpdate"] = module_update
    colorama = sys.modules.get("colorama")
    saved_colorama_fix = getattr(colorama, "just_fix_windows_console", None)
    if colorama is not None and saved_colorama_fix is None:
        colorama.just_fix_windows_console = lambda: None
    sys.path.insert(0, str(archipelago_root))
    try:
        return importlib.import_module("CommonClient").process_server_cmd
    finally:
        sys.path.remove(str(archipelago_root))
        if colorama is not None:
            if saved_colorama_fix is None:
                del colorama.just_fix_windows_console
            else:
                colorama.just_fix_windows_console = saved_colorama_fix
        if saved_module_update is None:
            del sys.modules["ModuleUpdate"]
        else:
            sys.modules["ModuleUpdate"] = saved_module_update
        sys.modules.update(saved_fake_modules)


@_run_async
async def test_launcher_hints_follow_canonical_storage_package_order(monkeypatch):
    context = _hint_context()
    key = "_read_hints_1_2"
    emitted = []
    sent = []
    monkeypatch.setattr(bridge, "emit_launcher_event", lambda event_type, **payload: emitted.append((event_type, payload)))
    context.state_key = None
    context.initialize_item_state = lambda: None
    context.deathlink_receiver = SimpleNamespace(configure_mode=lambda _mode: None)
    context.onboard_bootstrap = lambda _reason: None
    context.reconcile_checked_automap_cleanup = lambda _reason: None
    context.reconcile_fast_travel_unlock = lambda _reason: None
    context._item_delivery_wakeup = False
    context.items_processed = 0
    context.items_received = []
    context.auth = "Doom Slayer"
    context.game = "Doom Eternal"
    context.slot_info = {}
    context.locations_checked = set()
    context.locations_scouted = set()
    context.finished_game = False
    context.server_address = "ws://localhost:38281"
    context.ui = None
    context.consume_players_package = lambda _players: None

    async def noop(*_args):
        pass

    async def send_msgs(payload):
        sent.extend(payload)

    context.update_death_link = noop
    context.check_mission_challenge_locations = noop
    context.send_msgs = send_msgs
    monkeypatch.setattr(bridge.asyncio, "create_task", lambda coroutine: coroutine.close())

    process_server_cmd = _local_archipelago_packet_handler()
    await process_server_cmd(context, {
        "cmd": "Connected",
        "team": 1,
        "slot": 2,
        "slot_data": {
            "slot_data_revision": "0.5-D",
            "required_capabilities": ["cross_campaign_materialization_v1"],
            "use_dlc_content": False,
            "include_dlc_missions": False,
            "dlc_logic_timing": "late_game",
            "special_weapon": "progressive_special_weapon",
        },
        "missing_locations": [],
        "checked_locations": [],
        "players": [],
        "slot_info": {},
    })
    expected_keys = [
        key,
        "_read_item_name_groups_Doom Eternal",
        "_read_location_name_groups_Doom Eternal",
    ]
    assert context.team == 1
    assert context.slot == 2
    assert [message["cmd"] for message in sent] == ["Get", "SetNotify"]
    assert sent[0]["keys"] == sent[1]["keys"]
    assert set(sent[0]["keys"]) == set(expected_keys)
    assert [event_type for event_type, _payload in emitted] == ["connected"]

    two_rows = [
        {
            "class": "Hint",
            "receiving_player": 2,
            "finding_player": 3,
            "location": 100,
            "item": 200,
            "found": False,
            "entrance": "",
            "item_flags": 0,
            "status": 0,
        },
        {
            "class": "Hint",
            "receiving_player": 2,
            "finding_player": 3,
            "location": 101,
            "item": 201,
            "found": False,
            "entrance": "",
            "item_flags": 0,
            "status": 30,
        },
    ]
    await process_server_cmd(context, {"cmd": "Retrieved", "keys": {key: two_rows}})
    three_rows = [
        {**two_rows[0], "found": True, "status": 40},
        *two_rows[1:],
        {
            "class": "Hint",
            "receiving_player": 2,
            "finding_player": 3,
            "location": 102,
            "item": 202,
            "found": False,
            "entrance": "",
            "item_flags": 0,
            "status": 0,
        },
    ]
    await process_server_cmd(context, {"cmd": "SetReply", "key": key, "value": three_rows})

    hints = [payload["hints"] for event_type, payload in emitted if event_type == "hints"]
    assert [len(snapshot) for snapshot in hints] == [2, 3]
    assert hints[0][0]["item_name"] == "Item 200 for 2"
    assert hints[0][0]["status_name"] == "HINT_UNSPECIFIED"
    assert hints[1][0]["found"] is True
    assert hints[1][0]["status_name"] == "HINT_FOUND"


def test_launcher_hints_keep_valid_protocol_records_when_names_are_unknown(monkeypatch):
    context = _hint_context()
    context.item_names = SimpleNamespace(lookup_in_slot=lambda *_args: (_ for _ in ()).throw(LookupError()))
    context.location_names = SimpleNamespace(lookup_in_slot=lambda *_args: (_ for _ in ()).throw(LookupError()))
    context.stored_data["_read_hints_1_2"] = [(2, 3, 100, 200, False, "", 0, 30)]
    emitted = []
    monkeypatch.setattr(bridge, "emit_launcher_event", lambda event_type, **payload: emitted.append((event_type, payload)))

    context._emit_launcher_hints()

    hint = emitted[0][1]["hints"][0]
    assert hint["item_name"] == "Unknown item (200)"
    assert hint["location_name"] == "Unknown location (100)"


def test_launcher_hints_report_malformed_nonempty_payload(monkeypatch, caplog):
    context = _hint_context()
    context.stored_data["_read_hints_1_2"] = [(2, 3, 100, 200, False), (2, 3), "bad row"]
    emitted = []
    monkeypatch.setattr(bridge, "emit_launcher_event", lambda event_type, **payload: emitted.append((event_type, payload)))

    context._emit_launcher_hints()

    assert len(emitted[0][1]["hints"]) == 1
    assert "HINTS_DATA_REJECTED" in caplog.text
    assert "source_type=list" in caplog.text
    assert "rejected=2" in caplog.text
