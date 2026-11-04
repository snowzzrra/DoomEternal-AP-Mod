"""Persist Ammo Refill spending and recovery; the delivery coordinator executes paid effects."""

import asyncio
import json
import time
from dataclasses import replace
from uuid import uuid4

from . import sentinel_protocol_generated as codec
from .ammo_refill import AMMO_REFILL_CAPACITY, AMMO_REFILL_CONFIRMATION_TIMEOUT
from .ledger import (
    AmmoChange,
    AmmoKind,
    AmmoOperation,
    AmmoState,
    CommandChange,
    CommandPlan,
    CommandState,
    Conflict,
    ReplayPolicy,
    RevisionConflict,
)


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
                and await self.owner() in (None, self.writer_id) and await self._recover())

    async def synchronize(self, received, scope):
        async with self._lock:
            owner = await self.owner()
            if owner not in (None, self.writer_id) or not await self._recover():
                return None
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
            return await self._settle(row, value)
        except (TimeoutError, ConnectionError, OSError, ValueError):
            await self._uncertain(row, "storage_response_missing")
            return False

    async def _settle(self, row, value):
        operation = row.operation
        request = json.loads(operation.request)
        if json.dumps(value, sort_keys=True).encode() != operation.request:
            await self._uncertain(row, "storage_operation_unconfirmed")
            return False
        suffix = "consumed" if operation.kind is AmmoKind.SPEND else "discarded"
        total = await self.storage.maximum(self._key(f"ammo_refill_{suffix}"), operation.absolute_target, self.timeout)
        if total != operation.absolute_target:
            await self._uncertain(row, "storage_total_conflict")
            return False
        totals = await self.ledger.ammo_totals(self.room)
        totals = replace(totals, **{suffix: max(getattr(totals, suffix), total)},
                         reserved=totals.reserved - request["amount"], observations=operation.request)
        commands = await self._release_commands(operation, request["crucible"])
        change = AmmoChange(operation.operation_id, row.revision, AmmoState.CONFIRMED, operation.request, None)
        await self.delivery.ammo(changes=(change,), totals=totals, commands=commands)
        return True

    async def _release_commands(self, operation, crucible):
        if operation.kind is not AmmoKind.SPEND:
            return ()
        commands = []
        for ordinal in range(2 if crucible else 1):
            command = await self.ledger.command(self.room, f"{operation.operation_id}:{ordinal}")
            commands.append(CommandChange(command.plan.command_id, command.revision, CommandState.QUEUED,
                                          None, operation.request, None))
        return tuple(commands)

    async def _uncertain(self, row, reason):
        if row.state is not AmmoState.UNKNOWN:
            change = AmmoChange(row.operation.operation_id, row.revision, AmmoState.UNKNOWN, b"", reason)
            await self.delivery.ammo(changes=(change,))

    async def recover(self):
        async with self._lock:
            return await self.owner() in (None, self.writer_id) and await self._recover()

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
            except (TimeoutError, ConnectionError, OSError, ValueError):
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
