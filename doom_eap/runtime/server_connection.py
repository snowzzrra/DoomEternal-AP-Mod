"""Archipelago transport fallback with one connection cleanup owner."""
import asyncio
import urllib.parse

import CommonClient as client


async def server_loop(ctx, address=None):
    if ctx.server and ctx.server.socket:
        client.logger.error("Already connected")
        return
    address = ctx.server_address if address is None else address
    if not address:
        client.logger.info("Please connect to an Archipelago server.")
        return
    ctx.cancel_autoreconnect()
    if ctx._messagebox_connection_loss:
        ctx._messagebox_connection_loss.dismiss()
        ctx._messagebox_connection_loss = None
    address = f"ws://{address}" if "://" not in address else address.replace("archipelago://", "ws://")
    server_url = urllib.parse.urlparse(address)
    if server_url.username:
        ctx.username = urllib.parse.unquote(server_url.username)
    if server_url.password:
        ctx.password = urllib.parse.unquote(server_url.password)

    def reconnect_hint():
        return ", type /connect to reconnect" if ctx.server_address else ""

    try:
        while True:
            client.logger.info("Connecting to Archipelago server at %s", address)
            try:
                socket = await client.websockets.connect(
                    address, port=server_url.port or 38281, ping_timeout=None, ping_interval=None,
                    ssl=client.get_ssl_context() if address.startswith("wss://") else None,
                    max_size=ctx.max_size)
                break
            except client.websockets.InvalidMessage:
                if not address.startswith("ws://"):
                    raise
                address = "wss://" + address[len("ws://"):]
        if ctx.ui is not None:
            ctx.ui.update_address_bar(server_url.netloc)
        ctx.server = client.Endpoint(socket)
        client.logger.info("Connected")
        ctx.server_address = address
        ctx.current_reconnect_delay = ctx.starting_reconnect_delay
        ctx.disconnected_intentionally = False
        async for data in ctx.server.socket:
            for msg in client.decode(data):
                await client.process_server_cmd(ctx, msg)
        client.logger.warning("Disconnected from multiworld server%s", reconnect_hint())
    except client.websockets.InvalidMessage:
        ctx.handle_connection_loss("Lost connection to the multiworld server due to InvalidMessage" + reconnect_hint())
    except ConnectionRefusedError:
        ctx.handle_connection_loss("Connection refused by the server. "
                                   "May not be running Archipelago on that address or port.")
    except client.websockets.InvalidURI:
        ctx.handle_connection_loss("Failed to connect to the multiworld server (invalid URI)")
    except asyncio.TimeoutError:
        ctx.handle_connection_loss("Failed to connect to the multiworld server. Connection timed out.")
    except OSError:
        ctx.handle_connection_loss("Failed to connect to the multiworld server")
    except Exception:
        ctx.handle_connection_loss("Lost connection to the multiworld server" + reconnect_hint())
    finally:
        await ctx.connection_closed()
        if (ctx.server_address and ctx.username and not ctx.disconnected_intentionally
                and not ctx.exit_event.is_set()):
            client.logger.info("... automatically reconnecting in %s seconds", ctx.current_reconnect_delay)
            assert ctx.autoreconnect_task is None
            ctx.autoreconnect_task = asyncio.create_task(client.server_autoreconnect(ctx), name="server auto reconnect")
        ctx.current_reconnect_delay *= 2
