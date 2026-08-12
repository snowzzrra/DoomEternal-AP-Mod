import asyncio
import importlib
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
