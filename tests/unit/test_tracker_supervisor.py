"""Pytest unit tests for tracker supervisor and health status."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bridge_client


def _new_context():
    ctx = bridge_client.DoomEternalContext.__new__(
        bridge_client.DoomEternalContext
    )
    ctx.exit_event = asyncio.Event()
    ctx.current_map_name = None
    ctx.active_save_slot = None
    ctx.last_processed_event_id = None
    ctx.server = None
    ctx.auth = None
    return ctx


def test_tracker_supervisor_clean_shutdown():
    async def run():
        ctx = _new_context()
        ctx.exit_event.set()
        
        task = asyncio.create_task(ctx.tracker_supervisor())
        await task
        assert ctx.tracker_alive is False

    asyncio.run(run())


def test_tracker_supervisor_restarts_on_exception():
    async def run():
        ctx = _new_context()
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
    ctx = _new_context()
    ctx.tracker_alive = True
    ctx.tracker_restart_count = 3
    ctx.last_tracker_error = "ValueError: test error"
    ctx.last_heartbeat_timestamp = 100.0

    output_lines = []
    processor = bridge_client.DoomCommandProcessor.__new__(
        bridge_client.DoomCommandProcessor
    )
    processor.ctx = ctx
    processor.output = lambda msg: output_lines.append(msg)

    with patch("time.time", return_value=110.0):
        processor._cmd_doom_status()

    output = "\n".join(output_lines)
    assert "Tracker alive: True" in output
    assert "Last heartbeat age: 10.0s" in output
    assert "Restart count: 3" in output
    assert "Last error summary: ValueError: test error" in output
