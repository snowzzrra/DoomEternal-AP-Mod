import asyncio
import copy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    configuration = tmp_path / "config.json"
    configuration.write_text(json.dumps({
        "doom_base_dir": str(tmp_path / "base"), "save_games_dir": str(tmp_path / "saves"),
        "client_state_file": str(tmp_path / "state.json"), "bridge_log_path": str(tmp_path / "bridge.log"),
    }))
    monkeypatch.setenv("DOOM_AP_CONFIG_FILE", str(configuration))
    monkeypatch.setenv("DOOM_AP_APPLICATION_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(root.parent / "Archipelago"))
    monkeypatch.syspath_prepend(str(root / "packaging/standalone_runtime"))
    module = importlib.import_module("doom_eap.runtime.bridge_client")
    monkeypatch.setattr(module, "CLIENT_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(module, "CONFIG_FILE", configuration)
    return module


@pytest.mark.parametrize("unreadable", [False, True])
def test_state_load_failure_blocks_receipts_and_reconnect_for_session(bridge, tmp_path, monkeypatch, unreadable):
    async def run():
        context = bridge.DoomEternalContext.__new__(bridge.DoomEternalContext)
        context.receipt_session = bridge.ReceiptSession(processed_boundary=12)
        context.state_key = "room"
        context.item_state_ready = True
        context._item_delivery_lock = asyncio.Lock()
        context.items_received = [SimpleNamespace(item=7770024, location=1, player=1, flags=0)]
        monkeypatch.setattr(context, "reset_queue_session_authority", lambda reason: None)
        events = []
        monkeypatch.setattr(bridge, "emit_launcher_event", lambda kind, **data: events.append((kind, data)))
        original = b'{"version": 2, "sessions": {"room": {"processed_items": 12}}}' if unreadable else b'{invalid'
        bridge.CLIENT_STATE_FILE.write_bytes(original)
        with monkeypatch.context() as patching:
            if unreadable:
                def denied(*args, **kwargs):
                    raise PermissionError("read denied")
                patching.setattr(Path, "read_text", denied)
            context.initialize_item_state({})
        assert not context.item_state_ready and context.item_delivery_blocked
        assert events[-1][0] == "item_history_status" and events[-1][1]["status"] == "blocked"
        assert context.items_processed == 12
        assert bridge.CLIENT_STATE_FILE.read_bytes() == original
        assert not await context.process_pending_item_receipts("packet")
        bridge.CLIENT_STATE_FILE.unlink()
        context.initialize_item_state({})
        assert not context.item_state_ready and context.items_processed == 12

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["acknowledge", "observe"])
def test_receipt_commit_failure_restores_cursor_and_history_and_blocks(bridge, tmp_path, monkeypatch, operation):
    context = bridge.DoomEternalContext.__new__(bridge.DoomEternalContext)
    context.receipt_session = bridge.ReceiptSession(processed_boundary=7)
    context.item_state_ready = True
    context.session_state = {"processed_items": 7, "receipt_history": {"processed_boundary": 7}}
    context.client_state = {"version": 2, "sessions": {"room": context.session_state}}
    context.goals = SimpleNamespace(cultist_path=None)
    context.deathlink = SimpleNamespace(seen_events=set())
    context.save_observer = SimpleNamespace(observation_document={})
    original = copy.deepcopy(context.session_state)
    bridge.client_state_store().commit(context.client_state)
    persisted = bridge.CLIENT_STATE_FILE.read_bytes()
    monkeypatch.setattr(context, "reset_queue_session_authority", lambda reason: None)
    events = []
    monkeypatch.setattr(bridge, "emit_launcher_event", lambda kind, **data: events.append((kind, data)))
    def denied(*args, **kwargs):
        raise PermissionError("replace denied")
    monkeypatch.setattr("doom_eap.runtime.client_state_store.os.replace", denied)
    with pytest.raises(PermissionError, match="replace denied"):
        if operation == "acknowledge":
            context._acknowledge_receipt(SimpleNamespace(item=7770024, location=1, player=1))
        else:
            monkeypatch.setattr(context, "validate_item_history_prefix", lambda: True)
            context.items_received = [SimpleNamespace(item=7770024, location=index, player=1) for index in range(8)]
            context.observe_received_item_history()
    assert context.items_processed == 7 and context.session_state == original
    assert bridge.CLIENT_STATE_FILE.read_bytes() == persisted
    assert not context.item_state_ready and context.item_delivery_blocked
    assert events[-1][1]["status"] == "blocked"


def test_runtime_preferences_use_atomic_replacement(bridge, monkeypatch):
    original = bridge.CONFIG_FILE.read_bytes()
    monkeypatch.setattr(bridge, "config", {"item_notifications": False})
    def denied(*args, **kwargs):
        raise PermissionError("replace denied")
    with monkeypatch.context() as patching:
        patching.setattr("doom_eap.runtime.client_state_store.os.replace", denied)
        with pytest.raises(PermissionError, match="replace denied"):
            bridge.save_config()
    assert bridge.CONFIG_FILE.read_bytes() == original
    bridge.save_config()
    assert json.loads(bridge.CONFIG_FILE.read_text()) == {"item_notifications": False}
