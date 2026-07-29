"""Pytest unit tests for suffixed event file discovery and cleanup."""

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch
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


def test_extract_location_id_from_multi_suffixed_filename():
    assert bridge_client.extract_location_id_from_event("ap_event_7770292.txt") == 7770292
    assert bridge_client.extract_location_id_from_event("ap_event_7770292_0.txt") == 7770292
    assert bridge_client.extract_location_id_from_event("ap_event_7770292_0_1.txt") == 7770292
    assert bridge_client.extract_location_id_from_event("ap_event_7770292_foo_bar.txt") == 7770292


def test_flush_check_event_files_cleans_all_aliases_on_ack():
    async def run():
        with tempfile.TemporaryDirectory() as tmp_dir:
            inv_dump = Path(tmp_dir)
            f1 = inv_dump / "ap_event_7770292.txt"
            f2 = inv_dump / "ap_event_7770292_0.txt"
            f3 = inv_dump / "ap_event_7770292_0_1.txt"
            f1.write_text("AP_CHECK_EVENT_7770292", encoding="utf-8")
            f2.write_text("AP_CHECK_EVENT_7770292", encoding="utf-8")
            f3.write_text("AP_CHECK_EVENT_7770292", encoding="utf-8")

            with patch.object(bridge_client, "INV_DUMP_DIR", str(inv_dump)):
                ctx = bridge_client.DoomEternalContext(None, None)
                ctx.checked_locations = {7770292}
                
                await ctx.flush_check_event_files()

                assert not f1.exists()
                assert not f2.exists()
                assert not f3.exists()

    asyncio.run(run())


def test_flush_check_event_files_quarantines_non_slot_location():
    async def run():
        with tempfile.TemporaryDirectory() as tmp_dir:
            inv_dump = Path(tmp_dir)
            f1 = inv_dump / "ap_event_7779999.txt"
            f1.write_text("AP_CHECK_EVENT_7779999", encoding="utf-8")

            with patch.object(bridge_client, "INV_DUMP_DIR", str(inv_dump)):
                ctx = bridge_client.DoomEternalContext(None, None)
                ctx.server = types.SimpleNamespace()
                ctx.auth = "Player"
                ctx.state_key = "Seed:0:1"
                ctx.server_locations = {7770001, 7770002}
                ctx.checked_locations = set()

                await ctx.flush_check_event_files()

                assert not f1.exists()
                quarantine_dir = inv_dump / "ap_event_quarantine"
                assert len(list(quarantine_dir.rglob("ap_event_7779999.txt"))) == 1

    asyncio.run(run())
