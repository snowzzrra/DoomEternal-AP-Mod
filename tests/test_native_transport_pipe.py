"""Run the packaged native relay against a synthetic Windows pipe server."""

import asyncio
import ctypes
import os
import subprocess
import threading
from pathlib import Path

import pytest
from native_transport_support import Replies, codec, command_response, fixtures, frame, split

from doom_eap.runtime.delivery import classify
from doom_eap.runtime.ledger import CommandState
from doom_eap.runtime.native_transport import NativeTransport, ProtocolError, WireError

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows pipe and MSVC relay")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def relay():
    path = Path(os.environ.get("SENTINEL_TRANSPORT_RELAY", ROOT / ".tmp/native-transport-build/save_death_probe.exe"))
    assert path.is_file(), "Build native/probes with MSVC and set SENTINEL_TRANSPORT_RELAY"
    return str(path)


def process_scope():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.GetProcessTimes.argtypes = [ctypes.c_void_p, *([ctypes.c_void_p] * 4)]
    times = [ctypes.c_uint64() for _ in range(4)]
    assert kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(t) for t in times))
    return {"pid": os.getpid(), "process_created": times[0].value, "instance_id": [1] + [0] * 15, "generation": 1}


class PipeServer:
    def __init__(self, replies, mode="normal"):
        import _winapi

        self.api = _winapi
        self.replies, self.mode = replies, mode
        name = rf"\\.\pipe\sentinel_core.inspect.v2.{os.getpid()}"
        self.handle = _winapi.CreateNamedPipe(name, 3, 8, 1, 16384, 16384, 1000, 0)
        self.error = None
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def read(self, count):
        result = b""
        while len(result) < count:
            data, _ = self.api.ReadFile(self.handle, min(7, count - len(result)))
            if not data:
                raise EOFError
            result += data
        return result

    def serve(self):
        try:
            self.api.ConnectNamedPipe(self.handle, overlapped=False)
            while True:
                header = codec.decode("wire_header_2", self.read(32))
                body = self.read(header["payload_length"])
                response = self.replies.body(header["operation"], body)
                if self.mode == "drop_submit" and header["operation"] == 2:
                    return
                wire = frame(header["operation"], header["correlation_id"], response)
                for offset in range(0, len(wire), 3):
                    self.api.WriteFile(self.handle, wire[offset : offset + 3])
        except OSError as error:
            if error.winerror not in (109, 232):
                self.error = error
        except EOFError:
            pass
        finally:
            self.api.CloseHandle(self.handle)

    def start(self):
        self.thread.start()
        return self

    def join(self):
        self.thread.join(3)
        assert not self.thread.is_alive()
        assert self.error is None


@pytest.mark.parametrize("drop_submit", [False, True])
def test_native_reference_precedes_pipe_submit_and_survives_reconnect(relay, drop_submit):
    async def run():
        scope = process_scope()
        transport = NativeTransport((relay,), scope, timeout=3)
        value, payload = split("submission", fixtures()["submit"][32:])
        value["reference"]["scope"] = scope
        prepared = await transport.prepare(value, payload)
        reference = codec.decode("command_reference", prepared.reference)
        assert reference["command_id"] == "fixture"
        assert any(reference["fingerprint"])
        replies = Replies(scope, reference)
        server = PipeServer(replies, "drop_submit" if drop_submit else "normal").start()
        try:
            await transport.connect()
            helper_pid = transport._process.pid
            if drop_submit:
                with pytest.raises(ProtocolError):
                    await transport.submit(prepared)
            else:
                response = await transport.submit(prepared)
                assert response.result["balance_after"] == 3
                assert transport._process.pid == helper_pid
        finally:
            await transport.close()
            server.join()
        scope["generation"] = 2
        server = PipeServer(replies).start()
        try:
            await transport.connect()
            response = await transport.query(prepared.reference)
            await transport.cancel(prepared.reference)
            await transport.ack(prepared.reference, bytes(response.response["reply"]["dimensions"]["result_token"]))
            assert replies.operations == [6, 6, 2, 6, 6, 3, 4, 5]
        finally:
            await transport.close()
            server.join()

    asyncio.run(run())


def test_native_prepare_rejects_invalid_payload_without_pipe_access(relay):
    async def run():
        transport = NativeTransport((relay,), process_scope(), timeout=3)
        value, _ = split("submission", fixtures()["submit"][32:])
        try:
            with pytest.raises(WireError) as caught:
                await transport.prepare(value, b"\xff" * 4)
            assert caught.value.result == 4
        finally:
            await transport.close()

    asyncio.run(run())


@pytest.mark.parametrize("state,reason,error,execution,effect,expected", [
    (4, 18, 0, 0, 0, CommandState.STALE),
    (4, 6, 0, 0, 0, CommandState.STALE),
    (0, 0, 2, 0, 0, CommandState.STALE),
    (4, 17, 0, 0, 0, CommandState.REJECTED),
    (0, 0, 3, 0, 0, CommandState.REJECTED),
    (0, 0, 5, 0, 0, CommandState.REJECTED),
    (4, 18, 0, 1, 4, CommandState.EFFECT_UNKNOWN),
    (4, 6, 0, 2, 4, CommandState.EFFECT_UNKNOWN),
])
def test_native_relay_preserves_refusal_classification(relay, state, reason, error, execution, effect, expected):
    async def run():
        scope = process_scope()
        replies = Replies(scope)
        original = replies.body

        def body(operation, payload):
            result = original(operation, payload)
            if operation != 2:
                return result
            value, _ = split("command_response", result)
            return codec.encode("command_response", command_response(
                value["reply"]["reference"], retention=1, state=state, reason=reason,
                error=error, execution=execution, effect=effect, progress=3 if error else 2))

        replies.body = body
        server = PipeServer(replies).start()
        transport = NativeTransport((relay,), scope, timeout=3)
        try:
            await transport.connect()
            value, payload = split("submission", fixtures()["submit"][32:])
            value["reference"]["scope"] = scope
            prepared = await transport.prepare(value, payload)
            replies.reference = codec.decode("command_reference", prepared.reference)
            result = await transport.submit(prepared)
            assert result.response["execution"]["state"] == state
            assert result.response["execution"]["reason"] == reason
            assert result.response["error"] == error
            assert codec.encode("command_response", result.response) == result.body
            assert classify(result) is expected
        finally:
            await transport.close()
            server.join()
    asyncio.run(run())


def test_native_pipe_identity_mismatch_closes_connection(relay):
    async def run():
        scope = process_scope()
        transport = NativeTransport((relay,), scope, timeout=3)
        replies = Replies(scope)
        replies.hello["scope"] = dict(scope, process_created=scope["process_created"] + 1)
        server = PipeServer(replies).start()
        try:
            with pytest.raises(ProtocolError):
                await transport.connect()
            assert transport._process is None
        finally:
            await transport.close()
            server.join()

    asyncio.run(run())


def test_native_relay_rejects_oversized_frame_before_reading_body(relay):
    header = {
        "magic": codec.SCHEMA["protocol"]["magic"],
        "version": 2,
        "operation": 0,
        "payload_length": 0,
        "result": 0,
        "correlation_id": 1,
        "flags": 0,
        "reserved": 0,
    }
    raw = codec.encode("wire_header_2", header)
    raw = raw[:8] + (16384).to_bytes(4, "little") + raw[12:]
    result = subprocess.run(
        (relay, "--transport-v2", "7"),
        input=raw,
        capture_output=True,
        timeout=3,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 9
    assert result.stdout == b""
