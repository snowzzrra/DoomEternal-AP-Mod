import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest


def _socket_type(ctx, ending):
    class Socket:
        def __aiter__(self):
            return self
        async def __anext__(self):
            if ending == "intentional":
                ctx.disconnected_intentionally = True
                raise StopAsyncIteration
            if ending == "cancel":
                ctx.disconnected_intentionally = True
                raise asyncio.CancelledError
            raise ConnectionError("connection dropped")

    return Socket


@pytest.mark.parametrize("ending", ["drop", "intentional", "cancel", "invalid_tls"])
def test_ws_fallback_has_one_cleanup_and_reconnect_owner(monkeypatch, ending):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root.parent / "Archipelago"))
    monkeypatch.syspath_prepend(str(root / "packaging/standalone_runtime"))
    module = importlib.import_module("doom_eap.runtime.bridge_client")
    import websockets

    def _assert_fallback_connection_cleanup(attempts, cleanups, ctx, tls_context):
        assert len(cleanups) == 1 and len(attempts) == 2
        assert attempts[0][0] == "ws://localhost:38281" and attempts[0][1]["ssl"] is None
        assert attempts[1][0] == "wss://localhost:38281" and attempts[1][1]["ssl"] is tls_context
        assert all(options["ping_timeout"] is None for _, options in attempts)
        assert ctx.current_reconnect_delay == 10


    async def run():
        errors, attempts, cleanups, losses = [], [], [], []
        asyncio.get_running_loop().set_exception_handler(lambda loop, event: errors.append(event))
        ctx = SimpleNamespace(
            server=None,
            server_address="ws://localhost:38281",
            username="slot",
            password=None,
            cancel_autoreconnect=lambda: None,
            _messagebox_connection_loss=None,
            ui=None,
            starting_reconnect_delay=5,
            current_reconnect_delay=5,
            max_size=1024,
            disconnected_intentionally=False,
            autoreconnect_task=None,
            exit_event=asyncio.Event(),
            handle_connection_loss=losses.append,
        )

        Socket = _socket_type(ctx, ending)

        async def connect(address, **kwargs):
            attempts.append((address, kwargs))
            if address.startswith("ws://") or ending == "invalid_tls":
                raise websockets.InvalidMessage("invalid handshake")
            return Socket()

        async def closed():
            cleanups.append(True)
            ctx.server = None

        ctx.connection_closed = closed

        async def wait_for_reconnect(context):
            await asyncio.Event().wait()

        monkeypatch.setattr(
            module.APCommonClient,
            "websockets",
            SimpleNamespace(
                connect=connect, InvalidMessage=websockets.InvalidMessage, InvalidURI=websockets.InvalidURI
            ),
        )
        monkeypatch.setattr(module.APCommonClient, "server_autoreconnect", wait_for_reconnect)
        tls_context = object()
        monkeypatch.setattr(module.APCommonClient, "get_ssl_context", lambda: tls_context)
        task = asyncio.create_task(module.server_loop(ctx))
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        _assert_fallback_connection_cleanup(attempts, cleanups, ctx, tls_context)
        if ending in {"drop", "invalid_tls"}:
            assert ctx.autoreconnect_task is not None and len(losses) == 1
            ctx.autoreconnect_task.cancel()
            await asyncio.gather(ctx.autoreconnect_task, return_exceptions=True)
        else:
            assert ctx.autoreconnect_task is None and not losses
        assert not errors

    asyncio.run(run())
