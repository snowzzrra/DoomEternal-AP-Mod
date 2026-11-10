from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from dataclasses import dataclass, replace
from uuid import uuid4

from websockets.exceptions import WebSocketException

from doom_eap.runtime import sentinel_protocol_generated as codec
from doom_eap.runtime.ledger import (
    AmmoChange,
    AmmoKind,
    AmmoOperation,
    AmmoState,
    CommandChange,
    CommandPlan,
    CommandState,
    Conflict,
    LedgerError,
    ReplayPolicy,
    Resolution,
    RevisionConflict,
)
from doom_eap.runtime.native_transport import TransportError

AMMO_REFILL_ITEM_ID = 7770024
AMMO_REFILL_PRIMITIVE_ITEM_ID = 7770042
AMMO_REFILL_CAPACITY = 3
AMMO_REFILL_CONFIRMATION_TIMEOUT = 15
AMMO_REFILL_COMMANDS = ("give ammo", "judgementMeter_Set 3")
AMMO_UNAVAILABLE_MESSAGES = {
    "placement_connected": 'Disconnected from Archipelago; reconnect before using Ammo Refill',
    "item_state_ready": 'Ammo Refill unavailable: waiting for items from Archipelago',
    "connected": 'Disconnected from Archipelago; reconnect before using Ammo Refill',
    "namespace_match": "Ammo Refill unavailable: waiting for the current room's game session",
    "marker_valid": 'Ammo Refill unavailable: waiting for the current level to finish preparing',
    "lease_match": 'Ammo Refill unavailable: waiting for the current level to finish preparing',
    "native_safe": 'Ammo Refill unavailable: return to DOOM and unpause before trying again',
    "runtime_effects_ready": 'Ammo Refill unavailable: waiting for the active save and game session',
}


@dataclass(frozen=True)
class AmmoCommandScope:
    state_key: str
    current_map: str | None
    save_slot: str | None
    crucible_active: bool
    materialization_lease: str | None = None


def active_crucible(special_mode, processed_ids):
    if special_mode in {"the_crucible", "The Crucible"}:
        return 7770007 in processed_ids
    if special_mode in {"progressive_special_weapon", "Progressive Special Weapon"}:
        return 7770901 in processed_ids
    return False


class LedgerAmmoRefill:
    def __init__(self, delivery, storage, state_key, *, timeout=AMMO_REFILL_CONFIRMATION_TIMEOUT):
        self.delivery, self.storage, self.state_key = delivery, storage, state_key
        self.ledger, self.room = delivery.ledger, delivery.binding.room
        self.writer_id = self.ledger.profile_id
        self.timeout = timeout
        self._lock = asyncio.Lock()

    def _key(self, suffix):
        return f"doom_eap:{self.state_key}:{suffix}"

    async def claim(self):
        await self.delivery.require_owner()
        owner = await self.storage.create(self._key("ammo/writer"), self.writer_id, self.timeout)
        return owner == self.writer_id

    async def owner(self):
        async with self._lock:
            return await self._owner()

    async def _owner(self):
        await self.delivery.require_owner()
        return await self.storage.read(self._key("ammo/writer"), self.timeout)

    async def request(self, scope, readiness_result, received):
        async with self._lock:
            if not await self._spending_ready(scope, readiness_result):
                return None
            totals = await self._synchronize(received)
            if totals.credited <= totals.consumed + totals.discarded + totals.reserved:
                return None
            if not await self.claim():
                return None
            overflow = max(0, totals.credited - totals.consumed - totals.discarded - AMMO_REFILL_CAPACITY)
            if overflow:
                discard = await self._reserve(AmmoKind.DISCARD, overflow, scope)
                if not await self._publish(discard):
                    return None
            totals = await self.ledger.ammo_totals(self.room)
            if totals.credited <= totals.consumed + totals.discarded + totals.reserved:
                return None
            row = await self._reserve(AmmoKind.SPEND, 1, scope)
            return row.operation.operation_id if await self._publish(row) else None

    async def _spending_ready(self, scope, readiness_result):
        return (scope.state_key == self.state_key and readiness_result[0] is None
                and await self._owner() in (None, self.writer_id) and await self._recover())

    async def synchronize(self, received, scope):
        async with self._lock:
            owner = await self._owner()
            if owner not in (None, self.writer_id):
                return None
            if not await self._recover():
                return await self.ledger.ammo_totals(self.room)
            totals = await self._synchronize(received)
            overflow = max(0, totals.credited - totals.consumed - totals.discarded - AMMO_REFILL_CAPACITY)
            if overflow and owner == self.writer_id:
                row = await self._reserve(AmmoKind.DISCARD, overflow, scope)
                if not await self._publish(row):
                    return None
            return await self.ledger.ammo_totals(self.room)

    async def _synchronize(self, received):
        consumed = await self.storage.counter(self._key("ammo_refill_consumed"), self.timeout)
        discarded = await self.storage.counter(self._key("ammo_refill_discarded"), self.timeout)
        totals = await self.ledger.ammo_totals(self.room)
        updated = replace(totals, credited=max(totals.credited, received),
                          consumed=max(totals.consumed, consumed), discarded=max(totals.discarded, discarded))
        await self.delivery.ammo(totals=updated)
        return updated

    async def _reserve(self, kind, amount, scope):
        totals = await self.ledger.ammo_totals(self.room)
        operation_id = str(uuid4())
        baseline = totals.consumed if kind is AmmoKind.SPEND else totals.discarded
        target = baseline + amount
        request = json.dumps({"operation_id": operation_id, "writer_id": self.writer_id,
                              "kind": kind.value, "target": target, "amount": amount,
                              "crucible": scope.crucible_active}, sort_keys=True).encode()
        operation = AmmoOperation(operation_id, self._key(f"ammo/ops/{operation_id}"), request,
                                  kind, target, self.writer_id, time.time_ns() // 1000 + int(self.timeout * 1_000_000))
        plans = self._plans(operation, scope.crucible_active) if kind is AmmoKind.SPEND else ()
        await self.delivery.ammo((operation,), totals=replace(totals, reserved=totals.reserved + amount), plans=plans)
        return await self.ledger.ammo_operation(self.room, operation_id)

    def _plans(self, operation, crucible):
        plans = []
        for ordinal, variant in enumerate((3, 4) if crucible else (3,)):
            command_id = f"{operation.operation_id}:{ordinal}"
            plans.append(CommandPlan(
                command_id, None, operation.operation_id, f"ammo:{ordinal}", "acquisition", ordinal,
                None, None, "command", 1,
                codec.encode_variant(46, 1, variant, {}), self.delivery.context.context_id,
                ReplayPolicy.ADDITIVE, True, 1,
            ))
        return tuple(plans)

    async def _publish(self, row):
        try:
            value = await self.storage.create(row.operation.storage_key, json.loads(row.operation.request), self.timeout)
            settled = await self._settle(row, value)
            current = await self.ledger.ammo_operation(self.room, row.operation.operation_id)
            return settled or (current.state is AmmoState.REQUESTED and current.evidence == row.operation.request)
        except (TimeoutError, ConnectionError, OSError, ValueError, WebSocketException):
            await self._uncertain(row, "storage_response_missing")
            return False

    async def _settle(self, row, value):
        operation = row.operation
        request = json.loads(operation.request)
        if json.dumps(value, sort_keys=True).encode() != operation.request:
            await self._uncertain(row, "storage_operation_unconfirmed")
            return False
        if operation.kind is AmmoKind.SPEND:
            if not await self._release_commands(row, request["crucible"]):
                return False
        suffix = "consumed" if operation.kind is AmmoKind.SPEND else "discarded"
        total = await self.storage.maximum(self._key(f"ammo_refill_{suffix}"), operation.absolute_target, self.timeout)
        if total != operation.absolute_target:
            await self._uncertain(row, "storage_total_conflict")
            return False
        totals = await self.ledger.ammo_totals(self.room)
        totals = replace(totals, **{suffix: max(getattr(totals, suffix), total)},
                         reserved=totals.reserved - request["amount"], observations=operation.request)
        change = AmmoChange(operation.operation_id, row.revision, AmmoState.CONFIRMED, operation.request, None)
        await self.delivery.ammo(changes=(change,), totals=totals)
        return True

    async def _release_commands(self, row, crucible):
        operation = row.operation
        commands = await self._spend_commands(operation, crucible)
        if all(command.state is CommandState.CONFIRMED or command.resolution is Resolution.ACCEPTED_BY_PLAYER
               for command in commands):
            return True
        releases = tuple(CommandChange(command.plan.command_id, command.revision, CommandState.QUEUED,
                                       None, operation.request, command.next_eligible_at)
                         for command in commands if command.state is CommandState.PLANNED and not command.attempt_count)
        change = AmmoChange(operation.operation_id, row.revision, AmmoState.REQUESTED, operation.request, None)
        await self.delivery.ammo(changes=(change,), commands=releases)
        return False

    async def _spend_commands(self, operation, crucible):
        commands = []
        for ordinal in range(2 if crucible else 1):
            command = await self.ledger.command(self.room, f"{operation.operation_id}:{ordinal}")
            while command.replacement_id is not None:
                command = await self.ledger.command(self.room, command.replacement_id)
            commands.append(command)
        return tuple(commands)

    async def _uncertain(self, row, reason):
        if row.state is not AmmoState.UNKNOWN:
            change = AmmoChange(row.operation.operation_id, row.revision, AmmoState.UNKNOWN, b"", reason)
            await self.delivery.ammo(changes=(change,))

    async def recover(self):
        async with self._lock:
            return await self._owner() in (None, self.writer_id) and await self._recover()

    async def _recover(self):
        rows = await self.ledger.ammo_operations(
            self.room, (AmmoState.PREPARED, AmmoState.REQUESTED, AmmoState.UNKNOWN), None, 2,
        )
        for row in rows:
            if row.operation.writer_id != self.writer_id:
                raise Conflict("Ammo operation belongs to another profile")
            try:
                value = await self.storage.read(row.operation.storage_key, self.timeout)
                if not await self._settle(row, value):
                    return False
            except (TimeoutError, ConnectionError, OSError, ValueError, WebSocketException):
                await self._uncertain(row, "storage_response_missing")
                return False
        return True

    async def retry(self, operation_id, expected_revision):
        async with self._lock:
            row = await self.ledger.ammo_operation(self.room, operation_id)
            if row is None or row.revision != expected_revision:
                raise RevisionConflict("Ammo operation changed; refresh before resolving it")
            if row.state is not AmmoState.UNKNOWN or row.operation.writer_id != self.writer_id:
                raise Conflict("Only this profile's uncertain ammo operation can be retried")
            if not await self.claim():
                return False
            change = AmmoChange(operation_id, expected_revision, AmmoState.REQUESTED, row.evidence, "player_retry")
            await self.delivery.ammo(changes=(change,))
            row = await self.ledger.ammo_operation(self.room, operation_id)
            return await self._publish(row)


class AmmoStorage:
    def __init__(self, send):
        self._send = send
        self._replies = {}


    async def exchange(self, packet, timeout):
        key = packet["keys"][0] if packet["cmd"] == "Get" else packet["key"]
        expected = ("Retrieved" if packet["cmd"] == "Get" else "SetReply", key)
        future = asyncio.get_running_loop().create_future()
        if expected in self._replies:
            raise RuntimeError("Ammo storage request already pending")
        self._replies[expected] = future
        try:
            await asyncio.wait_for(self._send([packet]), timeout)
            return await asyncio.wait_for(future, timeout)
        finally:
            del self._replies[expected]

    def receive(self, cmd, args):
        if cmd == "Retrieved":
            values = args.get("keys", {})
        elif cmd == "SetReply":
            values = {args.get("key"): args.get("value")}
        else:
            return
        for key, value in values.items():
            future = self._replies.get((cmd, key))
            if future is not None and not future.done():
                future.set_result(value)

    async def create(self, key, value, timeout):
        return await self.exchange({"cmd": "Set", "key": key, "default": value,
                                    "operations": [], "want_reply": True}, timeout)

    async def read(self, key, timeout):
        return await self.exchange({"cmd": "Get", "keys": [key]}, timeout)

    async def counter(self, key, timeout):
        value = await self.read(key, timeout)
        if value is None:
            return 0
        if type(value) is not int or value < 0:
            raise ValueError("Invalid ammo total")
        return value

    async def maximum(self, key, value, timeout):
        reply = await self.exchange({"cmd": "Set", "key": key, "default": 0,
                                     "operations": [{"operation": "max", "value": value}],
                                     "want_reply": True}, timeout)
        if type(reply) is not int or reply < value:
            raise ValueError("Ammo total was refused")
        return reply


class AmmoRequestPump:
    """Remove request files once and supervise serial semantic requests for one room."""
    def __init__(self, request, exit_event, logger):
        self._request = request
        self._exit_event, self._logger = exit_event, logger
        self._queue = deque()
        self._task = None
        self._generation = 0

    def reset(self):
        self._generation += 1
        self._queue.clear()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self, generation):
        try:
            while self._queue and not self._exit_event.is_set() and generation == self._generation:
                try:
                    result = await self._request()
                except (LedgerError, TransportError, OSError, TimeoutError, WebSocketException) as error:
                    self._logger.warning("[Ammo Refill] Request deferred: %s", error)
                    result = None
                if result is None:
                    await asyncio.sleep(1)
                else:
                    self._queue.popleft()
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, ValueError):
            self._logger.exception("[Ammo Refill] Request queue failed")
        finally:
            if generation == self._generation:
                self._task = None

    def consume(self, paths):
        accepted = []
        for path in paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                continue
            except OSError as error:
                self._logger.warning("[Ammo Refill] Request removal failed for %s: %s", path, error)
                continue
            accepted.append(path)
        if not accepted:
            return False
        self.enqueue(accepted)
        return True

    def enqueue(self, requests=(None,)):
        self._queue.extend(requests)
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run(self._generation))
