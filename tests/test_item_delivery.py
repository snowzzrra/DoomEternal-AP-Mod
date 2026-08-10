import asyncio
import importlib
import json
import sys
from collections import deque, namedtuple
from functools import wraps
from types import ModuleType, SimpleNamespace

NetworkItem = namedtuple("NetworkItem", "item location player flags")


def _bridge_module():
    try:
        return importlib.import_module("bridge_client")
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
            pass

        common.ClientCommandProcessor = ClientCommandProcessor
        common.CommonContext = CommonContext
        common.get_base_parser = lambda: None
        common.gui_enabled = lambda: False
        common.server_loop = lambda *_args, **_kwargs: None
        net = ModuleType("NetUtils")
        net.ClientStatus = SimpleNamespace(CLIENT_GOAL=30)
        sys.modules.update(
            colorama=colorama,
            Utils=utils,
            CommonClient=common,
            NetUtils=net,
        )
        return importlib.import_module("bridge_client")


bridge = _bridge_module()


def _run_async(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


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
    context.current_map_name = None
    context.active_save_slot = None
    context.output = lambda *_args, **_kwargs: None
    context.persist_session_state = lambda: None
    context.onboard_bootstrap = lambda *_args, **_kwargs: None
    context.reconcile_owned_perks = lambda *_args, **_kwargs: None
    context.delivery_item_name = lambda item_id: f"item-{item_id}"
    context._record_processed_receipt = lambda item: None
    context.observe_received_item_history = lambda: SimpleNamespace(duplicates=())
    bridge.received_item_classification = lambda *_args, **_kwargs: {}
    return context


def _spool(context, calls, result=True):
    def spool(item_id, item_index, **_kwargs):
        calls.append((item_index, item_id))
        return result, f"item-{item_id}"

    context.spool_item_commands = spool


def _receipt_commands(context, item_id, item_index):
    commands, _description = context.item_activation_commands(
        item_id,
        item_index,
        intent=bridge.NEW_RECEIPT,
        include_notification=True,
        classification=bridge.ITEM_CLASSIFICATIONS[item_id],
    )
    assert commands is not None
    return commands


def test_native_only_receipts_keep_effects_without_notification():
    context = _context(
        [NetworkItem(7770014, 1, 1, 1), NetworkItem(7770022, 2, 1, 0)]
    )

    for item_index, item_id in enumerate((7770014, 7770022)):
        commands = _receipt_commands(context, item_id, item_index)
        assert commands
        assert any(f"ap_rpc_v3_{item_id}" in command for command in commands)
        assert not any("ap_notify_item_" in command for command in commands)


def test_ap_receipt_has_exactly_one_notification_and_repeats_at_new_index():
    item_id = 7770001
    context = _context(
        [NetworkItem(item_id, 1, 1, 1), NetworkItem(item_id, 2, 1, 1)]
    )

    first = _receipt_commands(context, item_id, 0)
    repeated = _receipt_commands(context, item_id, 1)

    for commands in (first, repeated):
        assert any(f"ap_rpc_v3_{item_id}" in command for command in commands)
        assert sum("ap_notify_item_" in command for command in commands) == 1
    assert any("ap_notify_item_major_7770001_a" in command for command in first)
    assert any("ap_notify_item_major_7770001_b" in command for command in repeated)


@_run_async
async def test_packet_signal_schedules_immediate_processor_without_tracker():
    context = _context()
    calls = []

    async def processor(trigger):
        calls.append(trigger)

    context.process_pending_item_receipts = processor
    context._on_received_items_packet({"index": 0, "items": [NetworkItem(1, 1, 1, 0)]})
    await asyncio.sleep(0)

    assert calls == ["packet"]


@_run_async
async def test_packet_event_has_counts_and_explicit_clocks(monkeypatch):
    context = _context([NetworkItem(1, 1, 1, 0)])
    records = []
    monkeypatch.setattr(
        bridge.logger,
        "info",
        lambda _format, payload: records.append(json.loads(payload)),
    )
    monkeypatch.setattr(bridge.time, "time_ns", lambda: 123)
    monkeypatch.setattr(bridge.time, "monotonic_ns", lambda: 456)
    context.process_pending_item_receipts = lambda _trigger: asyncio.sleep(0)

    context._on_received_items_packet(
        {"index": 0, "items": [NetworkItem(1, 1, 1, 0)]}
    )
    await context._item_delivery_task

    assert records == [
        {
            "authoritative_count": 1,
            "event": "ITEM_PACKET_RECEIVED",
            "monotonic_ns": 456,
            "packet_count": 1,
            "packet_received_monotonic_ns": 456,
            "packet_start_index": 0,
            "wall_time_ns": 123,
        }
    ]


def test_packet_timings_only_record_accepted_ranges_and_stay_bounded():
    context = _context([NetworkItem(1, 1, 1, 0)])
    context._schedule_item_delivery = lambda _trigger: None
    context._packet_received_ranges.append((9, 10, 1))

    context._on_received_items_packet(
        {"index": 2, "items": [NetworkItem(1, 1, 1, 0)]}
    )
    assert list(context._packet_received_ranges) == [(9, 10, 1)]

    context._on_received_items_packet(
        {"index": 0, "items": [NetworkItem(1, 1, 1, 0)]}
    )
    assert len(context._packet_received_ranges) == 1
    assert context._packet_received_ranges[0][:2] == (0, 1)

    for index in range(bridge.PACKET_TIMING_RANGE_LIMIT + 20):
        context.items_received.append(NetworkItem(1, index + 2, 1, 0))
        context._on_received_items_packet(
            {"index": index + 1, "items": [NetworkItem(1, index + 2, 1, 0)]}
        )
    assert len(context._packet_received_ranges) == bridge.PACKET_TIMING_RANGE_LIMIT


@_run_async
async def test_pending_items_are_consumed_in_authoritative_order():
    context = _context([NetworkItem(2, 2, 1, 0), NetworkItem(1, 1, 1, 0)])
    calls = []
    _spool(context, calls)
    bridge.ITEM_ID_TO_COMMAND = {1: "simple", 2: "simple"}
    bridge.ITEM_CLASSIFICATION_IDENTITY = {}

    await context.process_pending_item_receipts("packet")

    assert calls == [(0, 2), (1, 1)]
    assert context.items_processed == 2


@_run_async
async def test_byte_identical_receipts_at_distinct_indices_are_both_delivered():
    item = NetworkItem(7, -2, 1, 0)
    context = _context([item, item])
    calls = []
    _spool(context, calls)
    bridge.ITEM_ID_TO_COMMAND = {7: "simple"}
    bridge.ITEM_CLASSIFICATION_IDENTITY = {}

    await context.process_pending_item_receipts("packet")

    assert calls == [(0, 7), (1, 7)]
    assert context.items_processed == 2


@_run_async
async def test_delivery_yields_in_bounded_batches_and_stops_stale_session(monkeypatch):
    items = [NetworkItem(3, index, 1, 0) for index in range(20)]
    context = _context(items)
    bridge.ITEM_ID_TO_COMMAND = {3: {"type": "no_op"}}
    bridge.ITEM_CLASSIFICATION_IDENTITY = {}
    yields = []

    async def change_session(_delay):
        yields.append(context.items_processed)
        context._item_session_generation += 1

    monkeypatch.setattr(bridge.asyncio, "sleep", change_session)

    result = await context.process_pending_item_receipts("packet")

    assert result is False
    assert yields == [bridge.ITEM_DELIVERY_BATCH_SIZE]
    assert context.items_processed == bridge.ITEM_DELIVERY_BATCH_SIZE


@_run_async
async def test_rapid_signals_coalesce_and_retain_follow_up_wakeup():
    context = _context()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def processor(trigger):
        calls.append(trigger)
        started.set()
        await release.wait()

    context.process_pending_item_receipts = processor
    for _ in range(3):
        context._on_received_items_packet({"index": 0, "items": []})
    await started.wait()
    context._on_received_items_packet({"index": 0, "items": []})
    release.set()
    await context._item_delivery_task

    assert calls == ["packet", "packet"]


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
async def test_tracker_fallback_uses_same_consumer():
    context = _context([NetworkItem(6, 6, 1, 0)])
    calls = []
    _spool(context, calls)
    bridge.ITEM_ID_TO_COMMAND = {6: "simple"}
    bridge.ITEM_CLASSIFICATION_IDENTITY = {}
    await context.process_pending_item_receipts("tracker")

    assert calls == [(0, 6)]
    assert context.items_processed == 1


@_run_async
async def test_unsafe_gameplay_still_creates_durable_spool_without_native_execution(
    monkeypatch, tmp_path
):
    context = _context([NetworkItem(4, 4, 1, 0)])
    bridge.ITEM_ID_TO_COMMAND = {4: "simple"}
    bridge.ITEM_CLASSIFICATION_IDENTITY = {}
    context.runtime_observers_frozen = True
    context.item_activation_commands = lambda *_args, **_kwargs: (["give-item-4"], "item-4")
    context.client_state = {}
    context.session_state = {}
    context.delivery_item_name = lambda _item_id: "item-4"
    native_calls = []
    monkeypatch.setattr(bridge, "QUEUE_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "save_client_state", lambda _state: None)
    monkeypatch.setattr(bridge, "set_rpc_execution", lambda enabled: native_calls.append(enabled))
    # Python may arm native gate, but never executes command or checks gameplay safety here.
    context.has_authoritative_save_proof = lambda: (_ for _ in ()).throw(
        AssertionError("receipt delivery consulted gameplay safety")
    )

    await context.process_pending_item_receipts("packet")

    command_id = context.item_command_id(4, 0, 0, "give-item-4")
    assert (tmp_path / f"{command_id}.cmd").read_text() == "give-item-4\n"
    assert native_calls == [True]


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
