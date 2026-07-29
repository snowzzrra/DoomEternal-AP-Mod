"""Pytest unit tests for event session isolation and quarantine."""

import json
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch
import pytest

ROOT = Path(__file__).parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.modules.setdefault("Utils", types.SimpleNamespace(init_logging=lambda *args, **kwargs: None))
sys.modules.setdefault("colorama", types.SimpleNamespace(init=lambda: None, deinit=lambda: None))

class _DummyCommonContext:
    def __init__(self, server_address=None, password=None):
        self.server_address = server_address
        self.password = password

common_client = types.ModuleType("CommonClient")
common_client.CommonContext = _DummyCommonContext
common_client.server_loop = lambda ctx: None
common_client.gui_enabled = False
common_client.ClientCommandProcessor = object
common_client.get_base_parser = lambda: __import__("argparse").ArgumentParser()
common_client.logger = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None)
sys.modules.setdefault("CommonClient", common_client)

net_utils = types.ModuleType("NetUtils")
net_utils.ClientStatus = types.SimpleNamespace(CLIENT_GOAL=30)
sys.modules.setdefault("NetUtils", net_utils)

import bridge_client


def test_session_isolation_quarantines_preexisting_events_when_session_changes():
    with tempfile.TemporaryDirectory() as tmp_dir:
        inv_dump = Path(tmp_dir)
        event_file = inv_dump / "ap_event_7770291.txt"
        event_file.write_text("AP_CHECK_EVENT_7770291", encoding="utf-8")
        
        session_file = inv_dump / "ap_event_session.json"
        session_file.write_text(json.dumps({"ap_state_key": "OldSeed:0:1:Player:v0.3.4"}), encoding="utf-8")

        with patch.object(bridge_client, "INV_DUMP_DIR", str(inv_dump)):
            ctx = bridge_client.DoomEternalContext(None, None)
            ctx.server = types.SimpleNamespace()
            ctx.auth = "Player"
            ctx.state_key = "NewSeed:0:1"
            ctx.seed_name = "NewSeed"
            ctx.team = 0
            ctx.slot = 1

            assert ctx.check_and_update_event_session() is True
            assert not event_file.exists()

            quarantine_dir = inv_dump / "ap_event_quarantine"
            assert quarantine_dir.exists()
            quarantined_files = list(quarantine_dir.rglob("ap_event_7770291.txt"))
            assert len(quarantined_files) == 1

            meta_files = list(quarantine_dir.rglob("ap_event_7770291.txt.meta.json"))
            assert len(meta_files) == 1
            meta_data = json.loads(meta_files[0].read_text(encoding="utf-8"))
            assert meta_data["parsed_location_id"] == 7770291
            assert meta_data["reason"] == "session_changed"
            assert meta_data["old_state_key"] == "OldSeed:0:1:Player:v0.3.4"


def test_session_isolation_preserves_events_when_session_key_matches():
    with tempfile.TemporaryDirectory() as tmp_dir:
        inv_dump = Path(tmp_dir)
        event_file = inv_dump / "ap_event_7770291.txt"
        event_file.write_text("AP_CHECK_EVENT_7770291", encoding="utf-8")
        
        with patch.object(bridge_client, "INV_DUMP_DIR", str(inv_dump)):
            ctx = bridge_client.DoomEternalContext(None, None)
            ctx.server = types.SimpleNamespace()
            ctx.auth = "Player"
            ctx.state_key = "SameSeed:0:1"
            ctx.seed_name = "SameSeed"
            ctx.team = 0
            ctx.slot = 1

            current_key = ctx.get_ap_state_key()
            session_file = inv_dump / "ap_event_session.json"
            session_file.write_text(json.dumps({"ap_state_key": current_key}), encoding="utf-8")

            assert ctx.check_and_update_event_session() is True
            assert event_file.exists()
