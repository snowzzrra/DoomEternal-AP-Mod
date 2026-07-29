"""Pytest unit tests for tracker supervisor and health status."""

import asyncio
import importlib
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.modules.setdefault("Utils", types.SimpleNamespace(init_logging=lambda *args, **kwargs: None))
sys.modules.setdefault("colorama", types.SimpleNamespace(init=lambda: None, deinit=lambda: None))

class _DummyCommonContext:
    def __init__(self, server_address=None, password=None):
        self.server_address = server_address
        self.password = password
        self.exit_event = asyncio.Event()

class _DummyClientCommandProcessor:
    def __init__(self, ctx):
        self.ctx = ctx

common_client = types.ModuleType("CommonClient")
common_client.CommonContext = _DummyCommonContext
common_client.server_loop = lambda ctx: None
common_client.gui_enabled = False
common_client.ClientCommandProcessor = _DummyClientCommandProcessor
common_client.get_base_parser = lambda: __import__("argparse").ArgumentParser()
common_client.logger = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None)
sys.modules.setdefault("CommonClient", common_client)

net_utils = types.ModuleType("NetUtils")
net_utils.ClientStatus = types.SimpleNamespace(CLIENT_GOAL=30)
sys.modules.setdefault("NetUtils", net_utils)

import bridge_client


def test_tracker_supervisor_clean_shutdown():
    async def run():
        ctx = bridge_client.DoomEternalContext(None, None)
        ctx.exit_event.set()
        
        task = asyncio.create_task(ctx.tracker_supervisor())
        await task
        assert ctx.tracker_alive is False

    asyncio.run(run())


def test_tracker_supervisor_restarts_on_exception():
    async def run():
        ctx = bridge_client.DoomEternalContext(None, None)
        call_count = 0

        async def mock_tracker_loop():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Synthetic tracker crash test")
            ctx.exit_event.set()

        ctx.tracker_loop = mock_tracker_loop
        
        with patch("asyncio.sleep", new_callable=AsyncMock):
            task = asyncio.create_task(ctx.tracker_supervisor())
            await task

        assert call_count == 2
        assert ctx.tracker_restart_count == 1
        assert "RuntimeError: Synthetic tracker crash test" in ctx.last_tracker_error

    asyncio.run(run())


def test_doom_status_command():
    ctx = bridge_client.DoomEternalContext(None, None)
    ctx.tracker_alive = True
    ctx.tracker_restart_count = 3
    ctx.last_tracker_error = "ValueError: test error"
    ctx.last_heartbeat_timestamp = 100.0

    output_lines = []
    processor = bridge_client.DoomCommandProcessor(ctx)
    processor.output = lambda msg: output_lines.append(msg)

    with patch("time.time", return_value=110.0):
        processor._cmd_doom_status()

    output = "\n".join(output_lines)
    assert "Tracker alive: True" in output
    assert "Last heartbeat age: 10.0s" in output
    assert "Restart count: 3" in output
    assert "Last error summary: ValueError: test error" in output
