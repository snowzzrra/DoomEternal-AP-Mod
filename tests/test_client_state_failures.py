import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("unreadable", [False, True])
def test_state_load_failure_blocks_receipts_and_reconnect_for_session(bridge, tmp_path, monkeypatch, unreadable):
    from doom_eap.runtime import bridge_config

    async def _assert_blocked_receipt_state(bridge_config, context, events, original):
        assert not context.item_state_ready and context.item_delivery_blocked
        assert events[-1][0] == "item_history_status" and events[-1][1]["status"] == "blocked"
        assert context.items.items_processed == 12
        assert bridge_config.CLIENT_STATE_FILE.read_bytes() == original
        assert not await context.items.process_pending_item_receipts("packet")


    async def run():
        context = bridge.DoomEternalContext.__new__(bridge.DoomEternalContext)
        context.items = bridge.bridge_items.ItemRuntime(context)
        context.checks = bridge.bridge_checks.CheckRuntime(context)
        context.receipt_session = bridge.ReceiptSession(processed_boundary=12)
        context.state_key = "room"
        context.room_delivery = None
        context.item_state_ready = True
        context._item_delivery_lock = asyncio.Lock()
        context.items_received = [SimpleNamespace(item=7770024, location=1, player=1, flags=0)]
        monkeypatch.setattr(context, "reset_room_session_authority", lambda reason: None)
        events = []
        monkeypatch.setattr(bridge_config, "emit_launcher_event", lambda kind, **data: events.append((kind, data)))
        original = b'{"version": 2, "sessions": {"room": {"processed_items": 12}}}' if unreadable else b'{invalid'
        bridge_config.CLIENT_STATE_FILE.write_bytes(original)
        with monkeypatch.context() as patching:
            if unreadable:
                def denied(*args, **kwargs):
                    raise PermissionError("read denied")
                patching.setattr(Path, "read_text", denied)
            context.items.initialize_item_state({})
        await _assert_blocked_receipt_state(bridge_config, context, events, original)
        bridge_config.CLIENT_STATE_FILE.unlink()
        context.items.initialize_item_state({})
        assert not context.item_state_ready and context.items.items_processed == 12

    asyncio.run(run())


def test_receipt_observation_failure_retains_original_state_and_blocks(bridge, tmp_path, monkeypatch):
    from doom_eap.runtime import bridge_config

    context = bridge.DoomEternalContext.__new__(bridge.DoomEternalContext)
    context.items = bridge.bridge_items.ItemRuntime(context)
    context.checks = bridge.bridge_checks.CheckRuntime(context)
    context.receipt_session = bridge.ReceiptSession(processed_boundary=7)
    context.item_state_ready = True
    context.session_state = {"processed_items": 7, "receipt_history": {"processed_boundary": 7}}
    context.client_state = {"version": 2, "sessions": {"room": context.session_state}}
    context.goals = SimpleNamespace(cultist_path=None)
    context.deathlink = SimpleNamespace(seen_events=set())
    context.save_observer = SimpleNamespace(observation_document={})
    original = copy.deepcopy(context.session_state)
    bridge_config.client_state_store().commit(context.client_state)
    persisted = bridge_config.CLIENT_STATE_FILE.read_bytes()
    monkeypatch.setattr(context, "reset_room_session_authority", lambda reason: None)
    events = []
    monkeypatch.setattr(bridge_config, "emit_launcher_event", lambda kind, **data: events.append((kind, data)))
    def denied(*args, **kwargs):
        raise PermissionError("replace denied")
    monkeypatch.setattr("doom_eap.runtime.inventory.os.replace", denied)
    with pytest.raises(PermissionError, match="replace denied"):
        monkeypatch.setattr(context.items, "validate_item_history_prefix", lambda: True)
        context.items_received = [SimpleNamespace(item=7770024, location=index, player=1) for index in range(8)]
        context.items.observe_received_item_history()
    assert context.items.items_processed == 7 and context.session_state == original
    assert bridge_config.CLIENT_STATE_FILE.read_bytes() == persisted
    assert not context.item_state_ready and context.item_delivery_blocked
    assert events[-1][1]["status"] == "blocked"
