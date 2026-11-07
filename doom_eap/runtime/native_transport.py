from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from doom_eap.runtime import sentinel_protocol_generated as codec
from doom_eap.strings import text as player_text


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


class StaleNativeResult(asyncio.CancelledError):
    pass


class NativeWorker:
    def __init__(self, identity, process_probe):
        self.identity = identity
        self.process_probe = process_probe
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="native-owner")

    async def call(self, function, *args, process=None, **kwargs):
        identity = self.identity()

        def execute():
            if identity != self.identity():
                raise StaleNativeResult("Room or runtime session changed before native operation")
            if process is not None and self.process_probe() != process:
                raise StaleNativeResult("Game process changed before native operation")
            try:
                result = function(*args, **kwargs)
            finally:
                if process is not None and self.process_probe() != process:
                    raise StaleNativeResult("Game process changed during native operation")
            return result

        try:
            result = await asyncio.get_running_loop().run_in_executor(self.executor, execute)
        except (OSError, RuntimeError, ValueError):
            if identity != self.identity():
                raise StaleNativeResult("Room or runtime session changed during native operation") from None
            raise
        if identity != self.identity():
            raise StaleNativeResult("Room or runtime session changed during native operation")
        return result

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)


def hints_key(team, slot):
    if (
        not isinstance(team, int)
        or isinstance(team, bool)
        or not isinstance(slot, int)
        or isinstance(slot, bool)
    ):
        return None
    return f"_read_hints_{team}_{slot}"


class ProtocolFeed:
    def __init__(self):
        self._echoes = deque()

    def invalidate(self):
        self._echoes.clear()

    def record_echo(self, accepted):
        self._echoes.append((accepted, time.monotonic() + 5.0))

    def consume_echo(self, event):
        now = time.monotonic()
        while self._echoes and self._echoes[0][1] < now:
            self._echoes.popleft()
        if not any(
            segment.get("type") == "player" and segment.get("self") is True
            for segment in event.get("segments", ())
            if isinstance(segment, dict)
        ):
            return False
        plain = event.get("plain")
        if not isinstance(plain, str):
            return False
        for index, (text, _) in enumerate(self._echoes):
            if plain.endswith(text):
                del self._echoes[index]
                return True
        return False


@dataclass(frozen=True)
class ProtocolNames:
    item_names: object
    location_names: object
    player_names: Mapping
    slot_concerns_self: Callable[[int], bool]


def format_archipelago_event(context, args):
    parts = args.get("data") if isinstance(args, dict) else None
    if not isinstance(parts, list | tuple):
        parts = (None,)
    segments = []
    plain_parts = []
    for part in parts[:ARCHIPELAGO_EVENT_SEGMENT_COUNT]:
        try:
            segment, raw_text = _format_archipelago_part(context, part)
        except (AttributeError, TypeError, ValueError, LookupError, AssertionError):
            segment, raw_text = _fallback_archipelago_segment(part)
        segments.append(segment)
        plain_parts.append(raw_text if isinstance(raw_text, str) else player_text("hints", "unavailable"))
    return {
        "schema": ARCHIPELAGO_EVENT_SCHEMA,
        "plain": _bounded_event_text("".join(plain_parts), ARCHIPELAGO_EVENT_PLAIN_LIMIT),
        "segments": segments,
    }


def emit_hints(key, source, names, emit, logger, update_kind="DATA_RECEIVED") -> None:
    if key is None:
        return
    records, row_arities = _hint_records(source, names)
    if row_arities:
        logger.warning(
            "HINTS_DATA_REJECTED key=%s source_type=%s source_count=%s rejected=%d row_arities=%s",
            key, type(source).__name__, len(source) if isinstance(source, list | tuple) else "n/a",
            len(row_arities), row_arities,
        )
    logger.info("HINTS_%s key=%s records=%d", update_kind, key, len(records))
    emit("hints", hints=records)
    return records


def _decode_prefix(name: str, body: bytes) -> tuple[dict[str, Any], bytes]:
    value, end = codec._decode_type(name, body, 0)
    return value, body[end:]


def _same_process(scope: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return all(scope[key] == expected[key] for key in ("pid", "process_created", "instance_id"))


def _bounded_event_text(value, limit):
    """Return bounded plain text with control characters removed."""
    if not isinstance(value, str):
        return ""
    value = value.replace("\r", " ").replace("\n", " ")
    output = []
    for character in value:
        if not character.isprintable():
            continue
        if len(output) >= limit:
            break
        output.append(character)
    return "".join(output)


def _fallback_archipelago_segment(part):
    raw_text = part.get("text") if isinstance(part, dict) else None
    if not isinstance(raw_text, str):
        raw_text = player_text("hints", "unavailable")
    return {"type": "text", "text": _bounded_event_text(raw_text, ARCHIPELAGO_EVENT_SEGMENT_LIMIT)}, raw_text


def _valid_part_type(part):
    from NetUtils import JSONTypes

    if not isinstance(part, dict):
        return None
    part_type = part.get("type", JSONTypes.text.value)
    part_type = getattr(part_type, "value", part_type)
    return part_type if isinstance(part_type, str) else None


def _item_event_classification(flags):
    if not isinstance(flags, int) or isinstance(flags, bool) or flags < 0:
        return None
    from doom_eap.content.item_classification import (
        ITEM_CLASSIFICATION_PROGRESSION,
        ITEM_CLASSIFICATION_TRAP,
        ITEM_CLASSIFICATION_USEFUL,
    )

    if flags & ITEM_CLASSIFICATION_TRAP:
        return "trap"
    if flags & ITEM_CLASSIFICATION_PROGRESSION:
        return "progression"
    if flags & ITEM_CLASSIFICATION_USEFUL:
        return "useful"
    return "filler"


def _format_archipelago_part(context, part: JSONMessagePart):
    from NetUtils import JSONTypes

    part_type = _valid_part_type(part)
    if part_type in _ARCHIPELAGO_TEXT_TYPES:
        raw_text = part.get("text")
        if isinstance(raw_text, str):
            return {"type": "text", "text": _bounded_event_text(raw_text, ARCHIPELAGO_EVENT_SEGMENT_LIMIT)}, raw_text
    elif part_type in _ARCHIPELAGO_ITEM_TYPES:
        return _format_item_part(context, part, part_type)
    elif part_type in _ARCHIPELAGO_LOCATION_TYPES:
        return _format_location_part(context, part, part_type)
    elif part_type in {JSONTypes.player_id.value, JSONTypes.player_name.value}:
        return _format_player_part(context, part, part_type)
    return _fallback_archipelago_segment(part)


def _format_item_part(context, part, part_type):
    from NetUtils import JSONTypes

    raw_text = part.get("text") if isinstance(part, dict) else None
    classification = _item_event_classification(part.get("flags", 0)) if isinstance(part, dict) else None
    if not isinstance(raw_text, str) or classification is None:
        return _fallback_archipelago_segment(part)
    if part_type == JSONTypes.item_id.value:
        player = part.get("player")
        if not isinstance(player, int) or isinstance(player, bool):
            return _fallback_archipelago_segment(part)
        try:
            item_text = context.item_names.lookup_in_slot(int(raw_text), player)
        except (AttributeError, TypeError, ValueError, KeyError, LookupError, AssertionError):
            return _fallback_archipelago_segment(part)
        if not isinstance(item_text, str):
            return _fallback_archipelago_segment(part)
        raw_text = item_text
    return {
        "type": "item",
        "text": _bounded_event_text(raw_text, ARCHIPELAGO_EVENT_SEGMENT_LIMIT),
        "classification": classification,
    }, raw_text


def _format_location_part(context, part, part_type):
    from NetUtils import JSONTypes

    raw_text = part.get("text") if isinstance(part, dict) else None
    if not isinstance(raw_text, str):
        return _fallback_archipelago_segment(part)
    if part_type == JSONTypes.location_id.value:
        player = part.get("player")
        if not isinstance(player, int) or isinstance(player, bool):
            return _fallback_archipelago_segment(part)
        try:
            location_text = context.location_names.lookup_in_slot(int(raw_text), player)
        except (AttributeError, TypeError, ValueError, KeyError, LookupError, AssertionError):
            return _fallback_archipelago_segment(part)
        if not isinstance(location_text, str):
            return _fallback_archipelago_segment(part)
        raw_text = location_text
    return {"type": "location", "text": _bounded_event_text(raw_text, ARCHIPELAGO_EVENT_SEGMENT_LIMIT)}, raw_text


def _player_name(context, raw_text):
    if not isinstance(raw_text, str):
        return None
    try:
        player = int(raw_text)
    except (TypeError, ValueError):
        return None
    try:
        name = context.player_names.get(player, raw_text)
    except (AttributeError, TypeError):
        return None
    if not isinstance(name, str):
        return None
    return player, name


def _format_player_part(context, part, part_type):
    from NetUtils import JSONTypes

    raw_text = part.get("text") if isinstance(part, dict) else None
    player = part.get("player") if isinstance(part, dict) else None
    if part_type == JSONTypes.player_id.value:
        resolved = _player_name(context, raw_text)
        if resolved is None:
            return _fallback_archipelago_segment(part)
        player, raw_text = resolved
    if not isinstance(raw_text, str) or not isinstance(player, int) or isinstance(player, bool):
        return _fallback_archipelago_segment(part)
    try:
        is_self = bool(context.slot_concerns_self(player))
    except (AttributeError, TypeError, ValueError, LookupError, AssertionError):
        return _fallback_archipelago_segment(part)
    return {
        "type": "player",
        "text": _bounded_event_text(raw_text, ARCHIPELAGO_EVENT_SEGMENT_LIMIT),
        "self": is_self,
    }, raw_text


def _hint_record(hint, names):
    from NetUtils import HintStatus

    try:
        status = HintStatus(hint.status)
        status_value, status_name = int(status), status.name
    except (TypeError, ValueError):
        status_value, status_name = int(HintStatus.HINT_UNSPECIFIED), "HINT_UNSPECIFIED"
    try:
        item_name = names.item_names.lookup_in_slot(hint.item, hint.receiving_player)
    except (KeyError, LookupError, AttributeError):
        item_name = player_text("hints", "unknown_item", item_id=str(hint.item))
    try:
        location_name = names.location_names.lookup_in_slot(hint.location, hint.finding_player)
    except (KeyError, LookupError, AttributeError):
        location_name = player_text("hints", "unknown_location", location_id=str(hint.location))
    return {
        "receiving_player": hint.receiving_player,
        "receiving_player_name": names.player_names.get(hint.receiving_player, str(hint.receiving_player)),
        "finding_player": hint.finding_player,
        "finding_player_name": names.player_names.get(hint.finding_player, str(hint.finding_player)),
        "location": hint.location,
        "location_name": location_name,
        "item": hint.item,
        "item_name": item_name,
        "found": hint.found,
        "entrance": hint.entrance,
        "item_flags": hint.item_flags,
        "status": status_value,
        "status_name": status_name,
    }


def _parse_hint(raw):
    from NetUtils import Hint

    if isinstance(raw, dict):
        if not _valid_hint_mapping(raw):
            return None
        raw = tuple(raw[field] for field in _HINT_FIELDS)
    elif not isinstance(raw, list | tuple) or not 5 <= len(raw) <= 8:
        return None
    try:
        return Hint(*raw)
    except (TypeError, ValueError):
        return None


def _hint_records(source, names):
    records, row_arities = [], []
    if isinstance(source, list | tuple):
        for raw in source:
            hint = _parse_hint(raw)
            if hint is None:
                row_arities.append(len(raw) if isinstance(raw, list | tuple) else type(raw).__name__)
            else:
                records.append(_hint_record(hint, names))
    elif source is not None:
        row_arities.append(type(source).__name__)
    return records, row_arities


def _valid_hint_mapping(raw):
    if raw.get("class") != "Hint" or set(raw) != set(_HINT_FIELDS) | {"class"}:
        return False
    if not all(isinstance(raw[field], int) and not isinstance(raw[field], bool)
               for field in _HINT_FIELDS[:4] + _HINT_FIELDS[6:]):
        return False
    return isinstance(raw["found"], bool) and isinstance(raw["entrance"], str)


if TYPE_CHECKING:
    from NetUtils import JSONMessagePart


ARCHIPELAGO_EVENT_SCHEMA = 1
ARCHIPELAGO_EVENT_PLAIN_LIMIT = 512
ARCHIPELAGO_EVENT_SEGMENT_LIMIT = 128
ARCHIPELAGO_EVENT_SEGMENT_COUNT = 64
_ARCHIPELAGO_TEXT_TYPES = frozenset({
    "text",
    "color",
    "hint_status",
})
_ARCHIPELAGO_ITEM_TYPES = frozenset({
    "item_name",
    "item_id",
})
_ARCHIPELAGO_LOCATION_TYPES = frozenset({
    "location_name",
    "location_id",
})
_HINT_FIELDS = ("receiving_player", "finding_player", "location", "item", "found", "entrance", "item_flags", "status")
