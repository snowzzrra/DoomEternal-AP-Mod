"""Low-volume transport and bridge-loop timing, without packet contents."""
import asyncio
from functools import partial, wraps
import json
import logging
import os
import re
import socket
import time
from urllib.parse import parse_qsl, unquote, urlsplit

from websockets.frames import OP_PONG
from websockets.legacy.client import WebSocketClientProtocol

from doom_eap.launcher.launcher_platform import redact_secrets


logger = logging.getLogger("doom_eternal_ap.bridge")
_slow_calls = {}
_last_slow_call = None


def timed_sync_call(function):
    @wraps(function)
    def measured(*args, **kwargs):
        started = time.monotonic()
        try:
            return function(*args, **kwargs)
        finally:
            elapsed = time.monotonic() - started
            if elapsed >= 0.25:
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    pass
                else:
                    global _last_slow_call
                    name = function.__qualname__
                    now = time.monotonic()
                    _last_slow_call = (now, name)
                    if now - _slow_calls.get(name, float("-inf")) >= 60:
                        _slow_calls[name] = now
                        logger.warning("BRIDGE_SYNC_SLOW call=%s duration_ms=%.1f", name, elapsed * 1000)
    return measured


class DiagnosticProtocol(WebSocketClientProtocol):
    def __init__(self, *args, diagnostics, **kwargs):
        # asyncio creates the protocol after TCP connects and before TLS starts
        diagnostics.tls_started = time.monotonic()
        diagnostics.tcp_dns_ms = (diagnostics.tls_started - diagnostics.transport_started) * 1000
        super().__init__(*args, **kwargs)
        self.diagnostics = diagnostics
        self.last_message = self.last_pong = None

    async def handshake(self, *args, **kwargs):
        started = time.monotonic()
        self.diagnostics.transport_ms = (started - self.diagnostics.transport_started) * 1000
        self.diagnostics.tls_ms = ((started - self.diagnostics.tls_started) * 1000
                                   if self.transport.get_extra_info("ssl_object") else 0)
        try:
            return await super().handshake(*args, **kwargs)
        finally:
            self.diagnostics.websocket_ms = (time.monotonic() - started) * 1000

    async def recv(self):
        data = await super().recv()
        self.last_message = time.monotonic()
        return data

    async def read_frame(self, max_size):
        frame = await super().read_frame(max_size)
        if frame.opcode == OP_PONG:
            self.last_pong = time.monotonic()
        return frame


class ConnectionDiagnostics:
    def __init__(self, websockets, context):
        self.websockets = websockets
        self.context = context
        self.attempt = 0
        self.protocol = None
        self.connected_at = None
        self.secrets = {context.password} if context.password else set()
        self.target = {}
        self.transport_started = 0
        self.transport_ms = self.websocket_ms = None
        self.tcp_dns_ms = self.tls_started = self.tls_ms = None
        self._ping_waiter = None
        self.dns_task = None

    def __getattr__(self, name):
        return getattr(self.websockets, name)

    def safe_text(self, value):
        from doom_eap.launcher.launcher_doctor import sanitize_support_value

        text = redact_secrets(str(value), (*self.secrets, self.context.password or ""))
        text = re.sub(r"(?i)\b(wss?|https?)://[^\s\"'<>]+", r"\1://[REDACTED]", text)
        return str(sanitize_support_value(text)).replace("\r", " ").replace("\n", " ")[:512]

    def log(self, event, *, warning=False, **fields):
        (logger.warning if warning else logger.info)("%s %s", event, json.dumps(fields, sort_keys=True))

    async def resolve(self, target, result):
        started = time.monotonic()
        try:
            rows = await asyncio.get_running_loop().getaddrinfo(
                target["host"], target["port"], type=socket.SOCK_STREAM)
            result.update(resolved_ips=sorted({row[4][0] for row in rows}), dns_outcome="resolved")
        except Exception as error:
            result.update(dns_outcome="failed", dns_exception_class=type(error).__name__,
                          dns_message=self.safe_text(error))
        except asyncio.CancelledError:
            result["dns_outcome"] = "cancelled"
            raise
        finally:
            result["dns_ms"] = round((time.monotonic() - started) * 1000, 1)
            self.log("AP_DNS_RESULT", warning=result["dns_outcome"] == "failed", **target, **result)

    async def close(self):
        if self.dns_task is not None:
            self.dns_task.cancel()
            await asyncio.gather(self.dns_task, return_exceptions=True)

    async def connect(self, uri, **kwargs):
        self.attempt += 1
        self.protocol = None
        self.connected_at = None
        self.transport_ms = self.websocket_ms = None
        self.tcp_dns_ms = self.tls_started = self.tls_ms = None
        self._ping_waiter = None
        started = time.monotonic()
        dns = dict(dns_ms=None, resolved_ips=[], dns_outcome="incomplete")
        self.target = {}
        try:
            if self.context.password:
                self.secrets.add(self.context.password)
            parsed = urlsplit(uri)
            self.secrets.update(unquote(value) for value in (parsed.username, parsed.password) if value)
            self.secrets.update(value for _, value in parse_qsl(parsed.query) if value)
            self.target = dict(host=parsed.hostname, port=kwargs.get("port") or parsed.port or 38281,
                               scheme=parsed.scheme)
            self.log("AP_CONNECT_ATTEMPT", **self.target, attempt=self.attempt,
                     proxy_env=any(value for key, value in os.environ.items()
                                   if key.casefold() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}),
                     ping_interval=kwargs.get("ping_interval", 20), ping_timeout=kwargs.get("ping_timeout", 20))
            # a parallel DNS probe preserves transport fallback, TLS checks and connection deadlines
            await self.close()
            self.dns_task = asyncio.create_task(self.resolve(dict(self.target, attempt=self.attempt), dns),
                                                name="AP DNS diagnostics")
            self.transport_started = time.monotonic()
            self.protocol = await self.websockets.connect(
                uri, create_protocol=partial(DiagnosticProtocol, diagnostics=self), **kwargs)
            self.connected_at = time.monotonic()
            self.log("AP_CONNECT_RESULT", **self.target, attempt=self.attempt, outcome="connected",
                     **dns,
                     tcp_tls_dns_ms=round(self.transport_ms, 1), websocket_ms=round(self.websocket_ms, 1),
                     tcp_dns_ms=round(self.tcp_dns_ms, 1), tls_ms=round(self.tls_ms, 1),
                     total_ms=round((self.connected_at - started) * 1000, 1),
                     peer=self.protocol.remote_address, exception_class=None, message=None)
            return self.protocol
        except Exception as error:
            if self.tls_started is not None and self.tls_ms is None:
                self.tls_ms = (time.monotonic() - self.tls_started) * 1000 if self.target.get("scheme") == "wss" else 0
            if self.tcp_dns_ms is None and self.transport_started >= started:
                self.tcp_dns_ms = (time.monotonic() - self.transport_started) * 1000
            self.log("AP_CONNECT_RESULT", warning=True, **self.target, attempt=self.attempt, outcome="failed",
                     **dns,
                     tcp_dns_ms=round(self.tcp_dns_ms, 1) if self.tcp_dns_ms is not None else None,
                     tls_ms=round(self.tls_ms, 1) if self.tls_ms is not None else None,
                     tcp_tls_dns_ms=(round(self.transport_ms, 1) if self.transport_ms is not None else
                                     round((time.monotonic() - self.transport_started) * 1000, 1)
                                     if self.transport_started >= started else None),
                     websocket_ms=round(self.websocket_ms, 1) if self.websocket_ms is not None else None,
                     total_ms=round((time.monotonic() - started) * 1000, 1),
                     exception_class=type(error).__name__, message=self.safe_text(error))
            raise

    def disconnected(self):
        protocol = self.protocol
        now = time.monotonic()
        retry = bool(self.context.server_address and self.context.username
                     and not self.context.disconnected_intentionally and not self.context.exit_event.is_set())
        received = protocol.close_rcvd if protocol else None
        sent = protocol.close_sent if protocol else None
        first = received if protocol and protocol.close_rcvd_then_sent else sent
        self.log("AP_DISCONNECT", **self.target, attempt=self.attempt,
                 close_code=first.code if first else (1006 if protocol else None),
                 reason=self.safe_text(first.reason) if first else "",
                 closed_by=("remote" if protocol.close_rcvd_then_sent else "local") if first else "unknown",
                 last_message_age_s=round(now - protocol.last_message, 2) if protocol and protocol.last_message else None,
                 last_pong_age_s=round(now - protocol.last_pong, 2) if protocol and protocol.last_pong else None,
                 uptime_s=round(now - self.connected_at, 2) if self.connected_at is not None else None,
                 reconnect_attempt=self.attempt + 1 if retry else None,
                 backoff_s=self.context.current_reconnect_delay if retry else None)

    def _pong_done(self, future):
        if future.cancelled():
            return
        try:
            rtt = future.result()
        except Exception as error:
            self.log("AP_PING", warning=True, outcome="failed", exception_class=type(error).__name__,
                     message=self.safe_text(error))
        else:
            self.log("AP_PING", warning=rtt >= 1, outcome="pong", rtt_ms=round(rtt * 1000, 1))

    async def monitor_loop(self):
        last_warning, next_ping, ping_sent = float("-inf"), 0, 0
        while True:
            started = time.monotonic()
            await asyncio.sleep(0.5)
            now = time.monotonic()
            lag = now - started - 0.5
            if lag > 1 and now - last_warning >= 60:
                last_warning = now
                recent = _last_slow_call and _last_slow_call[0] >= started
                self.log("BRIDGE_LOOP_LAG", warning=True, lag_ms=round(lag * 1000, 1),
                         last_sync_call=_last_slow_call[1] if recent else None)
            protocol = self.protocol
            if protocol and protocol.open and now >= next_ping:
                next_ping = now + 60
                if self._ping_waiter is not None and not self._ping_waiter.done():
                    self.log("AP_PING", warning=True, outcome="pending", age_s=round(now - ping_sent, 1))
                    continue
                try:
                    self._ping_waiter = await protocol.ping()
                    ping_sent = time.monotonic()
                    self._ping_waiter.add_done_callback(self._pong_done)
                except self.websockets.ConnectionClosed:
                    pass
