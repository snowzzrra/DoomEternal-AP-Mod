"""Own asynchronous v2 transport through a persistent Windows pipe helper.

Delivery decisions, retries and durable result acknowledgement belong to the caller.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from . import sentinel_protocol_generated as codec


class Operation(IntEnum):
    PREPARE = 0  # local helper operation; never sent to the Core
    READ = 1
    SUBMIT = 2
    QUERY = 3
    CANCEL = 4
    ACK = 5
    HELLO = 6


class TransportError(RuntimeError):
    pass


class ProtocolError(TransportError):
    pass


class IdentityMismatch(TransportError):
    pass


class WireError(TransportError):
    def __init__(self, result: int):
        self.result = result
        super().__init__(f"Core wire error: {result}")


@dataclass(frozen=True)
class PreparedCommand:
    envelope: bytes
    reference: bytes


@dataclass(frozen=True)
class CommandResponse:
    response: dict[str, Any]
    result: dict[str, Any] | None
    body: bytes


def _decode_prefix(name: str, body: bytes) -> tuple[dict[str, Any], bytes]:
    value, end = codec._decode_type(name, body, 0)
    return value, body[end:]


def _same_process(scope: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return all(scope[key] == expected[key] for key in ("pid", "process_created", "instance_id"))


class NativeTransport:
    def __init__(
        self,
        command: Sequence[str],
        scope: Mapping[str, Any] | int,
        *,
        env: Mapping[str, str] | None = None,
        timeout: float = 5.0,
    ):
        """Use (probe,) on Windows or (proton, 'run', probe) with the game's prefix environment."""
        self.pid = scope if isinstance(scope, int) else scope["pid"]
        self.command = (*command, "--transport-v2", str(self.pid))
        self.scope = None if isinstance(scope, int) else codec.decode("scope", codec.encode("scope", scope))
        self.env = env
        self.timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._correlation = 0
        self._connected = False
        self.capabilities: frozenset[tuple[int, int]] = frozenset()

    def supports(self, command_type: int, revision: int = 1) -> bool:
        return (command_type, revision) in self.capabilities

    async def connect(self) -> dict[str, Any]:
        return await self.hello()

    async def hello(self) -> dict[str, Any]:
        body = await self._request(Operation.HELLO, b"")
        try:
            hello = codec.decode("hello_result", body)
        except (ValueError, KeyError, IndexError) as error:
            await self.close()
            raise ProtocolError("Invalid Core hello") from error
        if hello["scope"]["pid"] != self.pid or (self.scope is not None and not _same_process(hello["scope"], self.scope)):
            await self.close()
            raise IdentityMismatch("Core process or instance changed")
        self.scope = hello["scope"]
        self._connected = True
        self.capabilities = frozenset((entry["command_type"], entry["payload_revision"])
                                      for entry in hello["supported"])
        return hello

    async def observe(self, command_type: int, namespace: bytes, save_binding: bytes, **payload):
        if not self.supports(command_type):
            return None
        envelope = dict(scope=self.scope, namespace_id=namespace, save_binding=save_binding,
                        command_type=command_type, payload_revision=1)
        return await self.read(envelope, codec.encode_variant(command_type, 1, 0, payload))

    async def prepare(self, submission: Mapping[str, Any], payload: bytes) -> PreparedCommand:
        """Return ledger bytes before any effect submission; preserve the caller's command ID."""
        envelope = codec.encode("submission", submission) + payload
        reference = await self._request(Operation.PREPARE, envelope)
        try:
            decoded = codec.decode("command_reference", reference)
        except (ValueError, KeyError, IndexError) as error:
            await self.close()
            raise ProtocolError("Invalid prepared reference") from error
        del decoded["fingerprint"]
        if codec.encode("submission_identity", decoded) != codec.encode("submission_identity", submission["reference"]):
            await self.close()
            raise IdentityMismatch("Prepared reference does not match the submission")
        return PreparedCommand(envelope, reference)

    async def read(self, envelope: Mapping[str, Any], payload: bytes) -> dict[str, Any]:
        body = await self._request(Operation.READ, codec.encode("read_envelope", envelope) + payload)
        try:
            scope, result = _decode_prefix("scope", body)
            value = codec.decode(codec.COMMANDS[envelope["command_type"]]["result"], result)
        except (ValueError, KeyError, IndexError) as error:
            await self.close()
            raise ProtocolError("Invalid Core observation") from error
        if not _same_process(scope, self.scope):
            await self.close()
            raise IdentityMismatch("Observation belongs to another Core")
        if "namespace_id" in value and bytes(value["namespace_id"]).rstrip(b"\0") != envelope["namespace_id"]:
            await self.close()
            raise IdentityMismatch("Observation belongs to another room")
        self.scope = scope
        return value

    async def submit(self, command: PreparedCommand) -> CommandResponse:
        # disk recovery crosses the boundary again before any effect can be sent
        reference = await self._request(Operation.PREPARE, command.envelope)
        if reference != command.reference:
            raise IdentityMismatch("Stored reference does not match the submission")
        return await self._command(Operation.SUBMIT, command.envelope, command.reference)

    async def query(self, reference: bytes) -> CommandResponse:
        return await self._command(Operation.QUERY, reference, reference)

    async def cancel(self, reference: bytes) -> CommandResponse:
        return await self._command(Operation.CANCEL, reference, reference)

    async def ack(self, reference: bytes, result_token: bytes) -> CommandResponse:
        body = codec.encode(
            "ack", {"reference": codec.decode("command_reference", reference), "result_token": result_token}
        )
        return await self._command(Operation.ACK, body, reference)

    async def _command(self, operation: Operation, body: bytes, reference: bytes) -> CommandResponse:
        expected = codec.decode("command_reference", reference)
        if not _same_process(expected["scope"], self.scope):
            raise IdentityMismatch("Command belongs to another Core")
        response = await self._request(operation, body)
        try:
            value, tail = _decode_prefix("command_response", response)
            reply = value["reply"]
            result = codec.decode(codec.COMMANDS[expected["command_type"]]["result"], tail) if tail else None
        except (ValueError, KeyError, IndexError) as error:
            await self.close()
            raise ProtocolError("Invalid Core command response") from error
        if reply["actual_instance_id"] != self.scope["instance_id"] or reply["reference"] != expected:
            await self.close()
            raise IdentityMismatch("Core response does not match the command reference")
        if result is not None and "namespace_id" in result and bytes(result["namespace_id"]).rstrip(b"\0") != bytes(expected["namespace_id"]):
            await self.close()
            raise IdentityMismatch("Core result belongs to another room")
        return CommandResponse(value, result, response)

    async def _request(self, operation: Operation, body: bytes) -> bytes:
        if len(body) + 32 > codec.SCHEMA["protocol"]["max_frame"]:
            raise ValueError("Oversized transport frame")
        async with self._lock:
            if operation not in (Operation.HELLO, Operation.PREPARE) and not self._connected:
                raise TransportError("Connect before issuing Core operations")
            try:
                return await asyncio.wait_for(self._exchange(operation, body), self.timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                await self._stop()
                raise
            except (OSError, EOFError, ValueError, asyncio.IncompleteReadError) as error:
                await self._stop()
                raise ProtocolError("Native transport disconnected or returned an invalid frame") from error

    async def _exchange(self, operation: Operation, body: bytes) -> bytes:
        if self._process is None:
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self.env,
                **options,
            )
        process = self._process
        assert process.stdin is not None and process.stdout is not None
        self._correlation += 1
        header = {
            "magic": codec.SCHEMA["protocol"]["magic"],
            "version": 2,
            "operation": operation,
            "payload_length": len(body),
            "result": 0,
            "correlation_id": self._correlation,
            "flags": 0,
            "reserved": 0,
        }
        process.stdin.write(codec.encode("wire_header_2", header) + body)
        await process.stdin.drain()
        response = codec.decode("wire_header_2", await process.stdout.readexactly(32))
        if (
            response["operation"] != operation
            or response["correlation_id"] != self._correlation
            or response["flags"] != 1
            or response["payload_length"] + 32 > codec.SCHEMA["protocol"]["max_frame"]
        ):
            raise ValueError("Invalid transport response header")
        data = await process.stdout.readexactly(response["payload_length"])
        if response["result"]:
            raise WireError(response["result"])
        return data

    async def _stop(self) -> None:
        process, self._process = self._process, None
        self._connected = False
        self.capabilities = frozenset()
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            if os.name == "nt":
                if process.returncode is None:
                    process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await process.wait()
            if process.stdout is not None:
                await process.stdout.read()

    async def close(self) -> None:
        async with self._lock:
            await self._stop()
