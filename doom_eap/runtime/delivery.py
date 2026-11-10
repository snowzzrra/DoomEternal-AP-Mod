from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import asdict, replace
from enum import Enum, IntEnum
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from doom_eap.contracts.foundation import classify_item_definition, compile_item_delivery_plan
from doom_eap.contracts.inventory_domain import (
    Currency,
    CurrencyGrant,
    EffectExpectation,
    EvidenceScope,
    InventoryEffect,
    MissionAccess,
    NativeEffectEvidence,
    SaveReadiness,
)
from doom_eap.contracts.receipt_delivery import SUPPORT_RUNE_IDS, MaterializationScope
from doom_eap.runtime import sentinel_protocol_generated as codec
from doom_eap.runtime.ammo import AMMO_REFILL_CAPACITY, AmmoCommandScope, AmmoStorage, LedgerAmmoRefill
from doom_eap.runtime.campaign import ACCESS_IDS, STAGES
from doom_eap.runtime.inventory import (
    NEVER_REPLAY,
    NORMAL_RUNES,
    InventoryObligation,
    NativeEffectKind,
    currency_grant_result,
    currency_observation,
    effect_confirmed,
    mission_access_observation,
    observe_delivery,
    observe_optional,
    ownership_from_ledger,
    progression_observation,
)
from doom_eap.runtime.ledger import (
    AmmoState,
    Attempt,
    AttemptChange,
    AttemptRow,
    Authority,
    CommandChange,
    CommandPlan,
    CommandRow,
    CommandState,
    Commit,
    Conflict,
    Context,
    EffectSource,
    Ledger,
    LedgerError,
    ManualResolution,
    NetworkReceipt,
    ReceiptDecision,
    ReplayPolicy,
    Resolution,
    ResolutionAction,
    RevisionConflict,
    RoomKey,
    SaveEpochState,
    SourceKind,
    StaleBinding,
    StorageError,
)
from doom_eap.runtime.ledger_schema import _writer_lock
from doom_eap.runtime.native_transport import (
    CommandResponse,
    IdentityMismatch,
    NativeTransport,
    PreparedCommand,
    TransportError,
)
from doom_eap.strings import text


class DeliveryMode(Enum):
    COMPARE = "compare"
    OWNER = "owner"


class Progress(IntEnum):
    QUEUED = 0
    CLAIMED = 1
    TERMINAL = 2
    UNAVAILABLE = 3


class Execution(IntEnum):
    NOT_ENTERED = 0
    ENTERED = 1
    UNKNOWN = 2


class Effect(IntEnum):
    NO_EFFECT = 0
    OBSERVED = 1
    DISPATCH_ONLY = 2
    PUBLICATION = 3
    UNKNOWN = 4


class Retention(IntEnum):
    FULL = 0
    COMPACT = 1
    NOT_FOUND = 2


class CommandError(IntEnum):
    NONE = 0
    NOT_FOUND = 4


def classify(response: CommandResponse, *, expectation: EffectExpectation | None = None,
             evidence: NativeEffectEvidence | None = None, inventory_request=None) -> CommandState:
    dimensions = response.response["reply"]["dimensions"]
    refusal = _refusal_state(response)
    if refusal is not None:
        return refusal
    if response.response["error"] != CommandError.NONE:
        return CommandState.EFFECT_UNKNOWN
    if dimensions["progress"] in (Progress.QUEUED, Progress.CLAIMED):
        return CommandState.DISPATCHED
    return _terminal_state(response, expectation, evidence, inventory_request)


class DeliveryCoordinator:
    def __init__(self, ledger=None, binding=None, context=None, transport=None, *, mode=DeliveryMode.COMPARE, is_current=None):
        self.ledger, self.binding, self.context, self.transport = ledger, binding, context, transport
        self.mode = mode
        self.is_current = is_current
        self._lock = asyncio.Lock()

    def compare(self, ownership: InventoryOwnership, **snapshot) -> InventoryPlan:
        from doom_eap.runtime.inventory import compare_inventory

        return compare_inventory(ownership, **snapshot)

    async def require_owner(self, *, effect=True):
        if self.is_current is not None and not self.is_current():
            raise StaleBinding("Delivery session is not active")
        if self.mode is not DeliveryMode.OWNER or (effect and self.transport is None):
            raise Conflict("Compare delivery cannot execute or change obligations")
        room = await self.ledger.room(self.binding.room)
        if room.authority is not Authority.LEDGER or room.active_binding != self.binding.connection_id:
            raise StaleBinding("Delivery writer is not active")
        if effect:
            self._require_context(room)
        if room.blocked_reason is not None:
            raise Conflict(room.blocked_reason)

    def _require_context(self, room):
        if self.context is None or room.active_context_id != self.context.context_id:
            raise StaleBinding("Delivery context is not active")

    async def ingest(self, start: int, receipts: tuple[NetworkReceipt, ...]) -> Commit:
        async with self._lock:
            await self.require_owner(effect=False)
            return await self.ledger.ingest(self.binding, start, receipts)

    async def plan(self, commands: tuple[CommandPlan, ...], decisions: tuple[ReceiptDecision, ...] = (),
                   *, sources: tuple[EffectSource, ...] = ()) -> Commit:
        async with self._lock:
            await self.require_owner()
            return await self.ledger.plan(self.binding, commands, decisions, sources=sources)

    async def satisfy(self, indices: tuple[int, ...], evidence: bytes):
        async with self._lock:
            await self.require_owner()
            return await self.ledger.satisfy(self.binding, indices, evidence)

    async def expire_unsent(self, command_id: str):
        async with self._lock:
            await self.require_owner()
            command = await self._command(command_id)
            original = await self.ledger.saved_context(self.binding.room, command.plan.context_id)
            if command.attempt_count or self._same_context(original):
                raise Conflict("Only unsent work from an old context can expire")
            await self.ledger.apply(self.binding, (CommandChange(command_id, command.revision, CommandState.STALE,
                                    "context_changed", b"", None),), (), ())
            await self._replace_unexecuted(await self._command(command_id))

    async def plan_battery(self, command: CommandPlan, amount: int, namespace: bytes, *, sources=()) -> Commit:
        async with self._lock:
            await self.require_owner()
            if amount not in (1, 2):
                raise ValueError("Battery grants require amount 1 or 2")
            existing = await self.ledger.command(self.binding.room, command.command_id)
            if existing is not None:
                command = _stored_battery_plan(command, existing.plan, amount)
            elif self.transport.supports(COMMAND_IDS["currencies"]):
                command = await self._battery_plan(command, amount, namespace)
            return await self.ledger.plan(self.binding, (command,), (), sources=sources)

    async def _battery_plan(self, command, amount, namespace):
        result = await self.transport.observe(COMMAND_IDS["currencies"], namespace,
                                              UUID(self.context.save_binding_id).bytes,
                                              currency=Currency.SENTINEL_BATTERY)
        observation = currency_observation(result)
        scope = self._evidence_scope(self.context, namespace)
        value = observation.values[Currency.SENTINEL_BATTERY]
        if observation.before_scope != scope or observation.after_scope != scope or value.gained is None:
            raise Conflict("Battery baseline is unavailable in this context")
        payload = codec.encode_variant(COMMAND_IDS["currencies"], 1, 1,
                                       dict(currency=Currency.SENTINEL_BATTERY, amount=amount,
                                            expected_gained=value.gained))
        return replace(command, kind="currencies", payload_revision=1, payload=payload)

    async def ammo(self, operations=(), changes=(), totals=None, commands=(), *, plans=()):
        async with self._lock:
            await self.require_owner()
            return await self.ledger.ammo(self.binding, operations, changes, totals, commands, plans=plans)

    async def _command(self, command_id: str) -> CommandRow:
        command = await self.ledger.command(self.binding.room, command_id)
        if command is None:
            raise Conflict("Delivery command does not exist in this room")
        return command

    async def _attempt(self, command: CommandRow) -> AttemptRow | None:
        attempts = await self.ledger.attempts(self.binding.room, command.plan.command_id,
                                            max(0, command.attempt_count - 1), 1)
        return attempts[0] if attempts else None

    async def _eligible(self, command: CommandRow) -> bool:
        if command.resolution is not None or command.state in TERMINAL:
            return False
        if command.next_eligible_at is not None and command.next_eligible_at > time.time_ns() // 1000:
            return False
        original = await self.ledger.saved_context(self.binding.room, command.plan.context_id)
        if not self._same_context(original):
            return False
        if not await self._ammo_authorized(command):
            return False
        return await self._dependency_ready(command)

    async def _dependency_ready(self, command):
        if command.plan.predecessor_id is None:
            return True
        predecessor = await self._command(command.plan.predecessor_id)
        while predecessor.resolution is Resolution.REPLACED:
            assert predecessor.replacement_id is not None
            predecessor = await self._command(predecessor.replacement_id)
        return predecessor.state is CommandState.CONFIRMED or predecessor.resolution is Resolution.ACCEPTED_BY_PLAYER

    async def _ammo_authorized(self, command):
        operation_id = command.plan.ammo_operation_id
        if operation_id is None:
            return True
        row = await self.ledger.ammo_operation(self.binding.room, operation_id)
        return row is not None and (row.state is AmmoState.CONFIRMED
                                   or (row.state is AmmoState.REQUESTED and row.evidence == row.operation.request))

    def _same_context(self, original):
        fields = ("pid", "process_created", "native_instance", "campaign", "map_name", "save_id",
                  "load_id", "native_generation", "save_binding_id")
        return original is not None and self.context is not None and all(getattr(original, field) == getattr(self.context, field) for field in fields)

    def _submission(self, command, namespace):
        scope = {"pid": self.context.pid, "process_created": int(self.context.process_created),
                 "instance_id": UUID(self.context.native_instance).bytes, "generation": self.context.native_generation}
        return {"reference": {"scope": scope, "namespace_id": namespace, "command_id": command.plan.command_id,
                              "command_type": COMMAND_IDS[command.plan.kind],
                              "payload_revision": command.plan.payload_revision},
                "save_binding": UUID(self.context.save_binding_id).bytes, "deadline_ms": 5000}

    async def prepare(self, command_id: str, namespace: bytes) -> PreparedCommand:
        async with self._lock:
            await self.require_owner()
            command = await self._command(command_id)
            stored = await self._attempt(command)
            if stored is not None:
                if not stored.attempt.reference:
                    raise Conflict("Legacy delivery needs the player's decision before retrying")
                return PreparedCommand(stored.attempt.envelope, stored.attempt.reference)
            if not await self._eligible(command):
                raise Conflict("Delivery command is waiting for its context or dependency")
            prepared = await self.transport.prepare(self._submission(command, namespace), command.plan.payload)
            attempt = Attempt(command_id, 1, str(uuid4()), self.binding, command.plan.context_id,
                              prepared.envelope, time.time_ns() // 1000 + 5_000_000,
                              command.revision, prepared.reference)
            await self.ledger.prepare(self.binding, attempt)
            return prepared

    async def send(self, command_id: str) -> CommandResponse | None:
        async with self._lock:
            await self.require_owner()
            command = await self._command(command_id)
            stored = await self._attempt(command)
            if stored is None:
                raise Conflict("Prepare delivery before sending it")
            if command.state in TERMINAL or command.resolution is not None:
                return None
            response = await self._query(command, stored)
            if response is None:
                return None
            if response.response["error"] == CommandError.NOT_FOUND:
                if not await self._eligible(command):
                    await self._record(command, stored, CommandState.EFFECT_UNKNOWN, response)
                    return response
                prepared = PreparedCommand(stored.attempt.envelope, stored.attempt.reference)
                await self.require_owner()
                response = await self.transport.submit(prepared)
            elif not await self._eligible(command):
                response = await self.transport.cancel(stored.attempt.reference)
                await self._record(command, stored, self._classify(command, response), response)
                return response
            if _refusal_state(response) is not None:
                await self._recover_response(command, stored, response, None, None)
            else:
                await self._record(command, stored, CommandState.DISPATCHED, response)
            return response

    async def _query(self, command: CommandRow, stored: AttemptRow) -> CommandResponse | None:
        if not stored.attempt.reference:
            await self._record(command, stored, CommandState.EFFECT_UNKNOWN, None)
            return None
        try:
            return await self.transport.query(stored.attempt.reference)
        except IdentityMismatch:
            await self._record(command, stored, CommandState.EFFECT_UNKNOWN, None)
            return None

    async def recover(self, command_id: str, *, expectation: EffectExpectation | None = None,
                      evidence: NativeEffectEvidence | None = None) -> CommandResponse | None:
        async with self._lock:
            await self.require_owner()
            command = await self._command(command_id)
            if command.state is CommandState.STALE:
                await self._replace_unexecuted(command)
                return None
            stored = await self._attempt(command)
            if stored is None:
                return None
            if not stored.attempt.reference:
                return await self._query(command, stored)
            expectation = await self._prepare_expectation(command, stored, expectation, evidence)
            if not _recoverable(command, evidence or expectation):
                return None
            response = await self._query(command, stored)
            if response is not None:
                await self._recover_response(command, stored, response, expectation, evidence)
            return response

    async def _prepare_expectation(self, command, stored, expectation, evidence):
        if evidence is not None and expectation is None:
            raise Conflict("Evidence needs the prepared delivery expectation")
        if expectation is None and command.plan.kind == "currencies":
            expectation = await self._currency_expectation(command, stored)
        if expectation is not None:
            await self._check_expectation(command, stored, expectation)
        return expectation

    async def _recover_response(self, command, stored, response, expectation, evidence):
        if expectation is not None and evidence is None:
            try:
                evidence = await self._native_evidence(command, stored, response, expectation)
            except (TransportError, OSError, ValueError):
                evidence = None
        state = self._classify(command, response, expectation=expectation, evidence=evidence)
        proof = _proof_bytes(expectation, evidence) if evidence is not None else b""
        await self._record(command, stored, state, response, proof=proof)
        if state is CommandState.STALE:
            await self._replace_unexecuted(await self._command(command.plan.command_id))

    async def _replace_unexecuted(self, command):
        if command.resolution is not None or command.state is not CommandState.STALE:
            return
        stored = await self._attempt(command)
        if stored is not None and not await self._retry_ready(command, stored):
            return
        replacement = await self._replacement_plan(command, stored)
        if stored is not None:
            replacement = replace(replacement, retry_limit=max(0, command.plan.retry_limit - 1))
        await self.ledger.resolve(self.binding, ManualResolution(
            command.plan.command_id, command.revision, ResolutionAction.TRY_AGAIN, replacement,
            stored.result if stored else b"unsent"))

    async def _retry_ready(self, command, stored):
        if not stored.attempt.reference:
            await self._record(command, stored, CommandState.EFFECT_UNKNOWN, None)
            return False
        if not stored.result:
            return False
        result, _ = codec._decode_type("command_response", stored.result, 0)
        response = CommandResponse(result, None, stored.result)
        if _refusal_state(response) is not CommandState.STALE:
            await self._record(command, stored, CommandState.EFFECT_UNKNOWN, response)
            return False
        if command.plan.retry_limit == 0:
            await self._record(command, stored, CommandState.STALE, response)
            return False
        return command.next_eligible_at is None or command.next_eligible_at <= time.time_ns() // 1000

    async def _currency_expectation(self, command, stored):
        _, grant = codec.decode_variant(COMMAND_IDS["currencies"], command.plan.payload_revision, command.plan.payload)
        original = await self.ledger.saved_context(self.binding.room, command.plan.context_id)
        reference = codec.decode("command_reference", stored.attempt.reference)
        return EffectExpectation(stored.attempt.reference, self._evidence_scope(original, reference["namespace_id"]),
                                 CurrencyGrant(Currency(grant["currency"]), grant["amount"], grant["expected_gained"]))

    @staticmethod
    def _evidence_scope(context, namespace):
        return EvidenceScope(context.pid, int(context.process_created), context.native_instance,
                             bytes(namespace).decode("ascii"), context.save_binding_id,
                             context.native_generation, context.map_name, SaveReadiness.READY)

    async def _native_evidence(self, command, stored, response, expectation):
        postcondition = expectation.postcondition
        if isinstance(postcondition, InventoryEffect):
            return None
        if isinstance(postcondition, CurrencyGrant):
            return _native_currency_evidence(command.plan.kind, stored.attempt.reference, response.result, postcondition)
        command_type = COMMAND_IDS["mission_access"] if isinstance(postcondition, MissionAccess) else COMMAND_IDS["progression"]
        if not getattr(self.transport, "supports", lambda _: False)(command_type):
            return None
        payload = dict(stage_id=postcondition.stage_id) if isinstance(postcondition, MissionAccess) else {}
        result = await self.transport.observe(command_type, expectation.scope.namespace.encode("ascii"),
                                              UUID(expectation.scope.save_binding).bytes, **payload)
        if result is None:
            return None
        return mission_access_observation(result) if isinstance(postcondition, MissionAccess) else progression_observation(result)

    @staticmethod
    def _classify(command, response, **proof):
        if command.plan.kind == "weapon_points" and _weapon_points_confirmed(command, response):
            return CommandState.CONFIRMED
        request = None
        if command.plan.kind == "inventory":
            tag, value = codec.decode_variant(COMMAND_IDS["inventory"], command.plan.payload_revision,
                                              command.plan.payload)
            if tag == 1:
                request = value
        return classify(response, inventory_request=request, **proof)

    async def _check_expectation(self, command, stored, expectation):
        original = await self.ledger.saved_context(self.binding.room, command.plan.context_id)
        reference = codec.decode("command_reference", stored.attempt.reference)
        scope = expectation.scope
        identity = (scope.pid, scope.process_created, scope.core_instance, scope.namespace, scope.save_binding,
                    scope.generation, scope.map_name)
        expected = (original.pid, int(original.process_created), original.native_instance,
                    bytes(reference["namespace_id"]).decode("ascii"), original.save_binding_id,
                    original.native_generation, original.map_name)
        if expectation.reference != stored.attempt.reference or identity != expected:
            raise Conflict("Evidence does not describe the prepared delivery")

    async def _record(self, command: CommandRow, stored: AttemptRow, state: CommandState,
                      response: CommandResponse | None, *, proof: bytes = b"") -> None:
        evidence = response.body if response is not None else b""
        state, reason, next_eligible_at = self._retry_schedule(command, state, response)
        terminal = state in TERMINAL
        if response is None:
            reason = "core_identity_changed" if stored.attempt.reference else "legacy_reference_missing"
        completion = AttemptChange(command.plan.command_id, stored.attempt.number, stored.revision,
                                   stored.acceptance if terminal else evidence,
                                   evidence if terminal else b"", time.time_ns() // 1000 if terminal else None)
        change = CommandChange(command.plan.command_id, command.revision, state, reason, proof or evidence, next_eligible_at)
        await self.ledger.apply(self.binding, (change,), (), (), attempts=(completion,))

    @staticmethod
    def _retry_schedule(command, state, response):
        reason = state.value if state in (CommandState.EFFECT_UNKNOWN, CommandState.REJECTED) else None
        if state is CommandState.STALE and response is not None and _refusal_state(response) is CommandState.STALE:
            if command.plan.retry_limit == 0:
                return CommandState.REJECTED, "retry_limit", None
            return state, reason, time.time_ns() // 1000 + 5_000_000
        return state, reason, None

    async def ack(self, command_id: str) -> CommandResponse | None:
        async with self._lock:
            await self.require_owner()
            command = await self._command(command_id)
            stored = await self._attempt(command)
            if stored is None or stored.completed_at is None:
                raise Conflict("Record the delivery result before acknowledging it")
            if not stored.attempt.reference or not stored.result:
                return None
            response, _ = codec._decode_type("command_response", stored.result, 0)
            if response["error"] or response["reply"]["dimensions"]["progress"] != Progress.TERMINAL:
                return None
            token = response["reply"]["dimensions"]["result_token"]
            return await self.transport.ack(stored.attempt.reference, token)

    async def resolve(self, command_id: str, expected_revision: int, action: ResolutionAction) -> str | None:
        async with self._lock:
            await self.require_owner()
            command = await self._command(command_id)
            if command.resolution is not None:
                expected = Resolution.REPLACED if action is ResolutionAction.TRY_AGAIN else Resolution.ACCEPTED_BY_PLAYER
                if command.resolution is not expected or command.revision != expected_revision + 1:
                    raise RevisionConflict("Delivery already has another resolution")
                return command.replacement_id
            if command.revision != expected_revision:
                raise RevisionConflict("Delivery changed; refresh Items before resolving it")
            if command.state not in (CommandState.EFFECT_UNKNOWN, CommandState.REJECTED):
                raise Conflict("Only blocked delivery can be resolved")
            stored = await self._attempt(command)
            response = await self._cancel(stored)
            replacement = None
            if action is ResolutionAction.TRY_AGAIN:
                replacement = await self._replacement_plan(command, stored)
            evidence = response.body if response is not None else b""
            await self.ledger.resolve(self.binding, ManualResolution(
                command_id, expected_revision, action, replacement, evidence))
            return replacement.command_id if replacement is not None else None

    async def _replacement_plan(self, command, stored):
        replacement_id = str(uuid4())
        replacement = replace(command.plan, command_id=replacement_id, supersedes_id=command.plan.command_id,
                              realization_key=replacement_id, context_id=self.context.context_id)
        if command.plan.kind not in ("currencies", "weapon_points"):
            return replacement
        from doom_eap.runtime.inventory import namespace_id
        namespace = (bytes(codec.decode("command_reference", stored.attempt.reference)["namespace_id"]) if stored and stored.attempt.reference else
                     namespace_id(self.binding.room.seed, self.binding.room.team, self.binding.room.slot,
                                  self.binding.room.generation_fp).encode("ascii"))
        kind = COMMAND_IDS[command.plan.kind]
        tag, grant = codec.decode_variant(kind, command.plan.payload_revision, command.plan.payload)
        if command.plan.kind == "currencies":
            return await self._battery_plan(replacement, grant["amount"], namespace)
        observed = await self.transport.observe(kind, namespace, UUID(self.context.save_binding_id).bytes)
        if observed is None or observed["native_exception"] or not observed["flags"] & 16 or observed["outcome"] not in (1, 2):
            raise Conflict("Weapon point baseline is unavailable in this context")
        return replace(replacement, payload=codec.encode_variant(kind, replacement.payload_revision, tag,
                       {**grant, "expected_gained": observed["gained_after"]}))

    async def _cancel(self, stored: AttemptRow | None) -> CommandResponse | None:
        if stored is None or not stored.attempt.reference:
            return None
        try:
            response = await self.transport.query(stored.attempt.reference)
            if response.response["reply"]["dimensions"]["progress"] in (Progress.QUEUED, Progress.CLAIMED):
                response = await self.transport.cancel(stored.attempt.reference)
            if response.response["reply"]["dimensions"]["progress"] in (Progress.QUEUED, Progress.CLAIMED):
                raise Conflict("Delivery is still running; wait before resolving it")
            return response
        except IdentityMismatch:
            return None


def console_payload(command):
    """Translate the bounded console grammar to the Core's typed commands."""
    match = re.fullmatch(r"ai_ScriptCmdEnt ([A-Za-z0-9_-]+) activate", command)
    if match:
        return 1, {"entity": match[1]}
    match = re.fullmatch(r"ai_ScriptCmdEnt player1 givePlayerPerk ([A-Za-z0-9_/-]+)", command)
    if match:
        return 2, {"perk": match[1]}
    match = re.fullmatch(r"(g_damageScaleAllToAI|g_damageScaleAllToSlayer|g_infiniteAmmo) ([0-9]+(?:\.[0-9]+)?)", command)
    if match:
        variables = ("g_damageScaleAllToAI", "g_damageScaleAllToSlayer", "g_infiniteAmmo")
        variable = variables.index(match[1])
        if (variable == 2 and match[2] in ("0", "1")) or (variable != 2 and .01 <= float(match[2]) <= 100):
            return 8, {"variable": variable, "value": match[2]}
    raise ValueError("Unsupported runtime command")


def effect_identity(plan):
    return tuple(map(int, plan.effect_key.split(":")[:2]))


def obligation(row, receipts, sources):
    if row.plan.ammo_operation_id is not None or not row.plan.required:
        return None
    item_id, stage = effect_identity(row.plan)
    key = f"ap:{row.plan.receipt_index}" if row.plan.receipt_index is not None else sources[row.plan.local_source_id].source_key
    return InventoryObligation(item_id, stage, row.state, row.resolution, key)


def restore_confirmed(commands, rows):
    by_id = {row.plan.command_id: row for row in rows}
    children = {row.plan.supersedes_id: row for row in rows if row.plan.supersedes_id is not None}
    restored = []
    for command in commands:
        previous = by_id.get(command.command_id)
        if previous is not None and command.replay is ReplayPolicy.IDEMPOTENT:
            previous = _latest(previous, children)
            if previous.state is CommandState.CONFIRMED:
                identity = str(uuid5(NAMESPACE_URL, f"{previous.plan.command_id}:{previous.revision}:missing"))
                command = replace(command, command_id=identity, realization_key=identity, supersedes_id=previous.plan.command_id)
        restored.append(command)
    return tuple(restored)


class DeliveryPlans:
    def __init__(self, room, context, ownership, definitions, epoch, observation):
        self.room, self.context, self.ownership, self.definitions = room, context, ownership, definitions
        self.epoch = epoch
        self.observation = observation
        self.commands = []
        self.sources = {}
        self.by_item = {}
        for acquisition in ownership.acquisitions:
            self.by_item.setdefault(acquisition.item_id, []).append(acquisition.source_key)

    def _source(self, item_id, stage, source_key=None):
        candidates = self.by_item.get(item_id, ())
        key = source_key or (candidates[min(stage, len(candidates) - 1)] if candidates else f"derived:{item_id}")
        if key.startswith("ap:"):
            return int(key[3:]), None
        identity = str(uuid5(NAMESPACE_URL, f"{self.room}:{key}"))
        kind = SourceKind.DERIVED_FACT if key.startswith("derived:") else SourceKind.STARTING_GRANT
        self.sources[identity] = EffectSource(identity, kind, key, str(item_id).encode("ascii"))
        return None, identity

    def add(self, item_id, stage, ordinal, kind, tag, payload, *, source_key=None, acquisition=False):
        receipt, local = self._source(item_id, stage, source_key)
        effect = f"{item_id}:{stage}:{kind}:{tag}:{json.dumps(payload, sort_keys=True)}"
        realization = "acquisition" if acquisition else (
            f"{self.epoch}:{self.context.native_instance}:{self.context.save_binding_id}:"
            f"{self.context.save_id}:{self.context.load_id}:{self.context.map_name}"
        )
        identity = str(uuid5(NAMESPACE_URL, f"{self.room}:{receipt}:{local}:{effect}:{realization}"))
        command_type = next(i for i, command in codec.COMMANDS.items() if command["name"] == kind)
        self.commands.append(CommandPlan(identity, receipt, None, effect, realization, ordinal, None, None,
                                          kind, 1, codec.encode_variant(command_type, 1, tag, payload),
                                          self.context.context_id, ReplayPolicy.ADDITIVE if acquisition else ReplayPolicy.IDEMPOTENT,
                                          True, 1, local))

    def console(self, item_id, stage, ordinal, command, **options):
        tag, payload = console_payload(command)
        self.add(item_id, stage, ordinal, "command", tag, payload, **options)

    def persistent(self, command, ordinal):
        item_id, stage = command.item_id, command.stage
        if item_id in NORMAL_RUNES:
            self.add(item_id, stage, 0, "runes", 1, {"normal_runes": 1 << NORMAL_RUNES.index(item_id)})
        elif item_id in SUPPORT_RUNE_IDS:
            self.add(item_id, stage, 0, "runes", 2, {"support_runes": 1 << sorted(SUPPORT_RUNE_IDS).index(item_id)})
        elif item_id == 7770083:
            self.add(item_id, stage, 0, "arsenal", 1, {"mods": 1 << 8})
        elif item_id in (7770901, 7770902):
            self.special(item_id, stage)
        else:
            self.console(item_id, stage, ordinal, command.command)

    def special(self, item_id, stage):
        hammer_only = item_id == 7770902
        hammer = int(hammer_only or stage >= 2)
        tier = 2 if stage >= (2 if hammer_only else 3) else hammer
        self.add(item_id, stage, 0, "special", 1, {"crucible": int(not hammer_only), "hammer": hammer, "hammer_tier": tier})

    def compile(self, plan):
        if plan.reconciliation is not None:
            for ordinal, command in enumerate(plan.reconciliation.commands):
                self.persistent(command, ordinal)
        for effect in plan.native_effects:
            if effect.kind is NativeEffectKind.MASTERY:
                self.add(effect.item_id, effect.stage, 0, "arsenal", 3, {"masteries": effect.mask})
            else:
                compiled = compile_item_delivery_plan(effect.item_id, self.definitions, stage=effect.stage)
                for command in compiled.commands:
                    self.console(effect.item_id, effect.stage, command.index, command.command)
        for acquisition in plan.acquisitions:
            for command in acquisition.commands:
                self.console(acquisition.item_id, command.index, command.index, command.command,
                             source_key=acquisition.source_key, acquisition=True)
        self._dependencies(plan.dependencies)
        return tuple({c.command_id: c for c in self.commands}.values()), tuple(self.sources.values())

    def _dependencies(self, dependencies):
        by_effect = {effect_identity(c): c.command_id for c in self.commands}
        commands = []
        for command in self.commands:
            item_id, stage = effect_identity(command)
            dependency = next((d for d in dependencies if (d.item_id, d.stage) == (item_id, stage)), None)
            if dependency is not None:
                predecessor = by_effect.get((dependency.prerequisite_item_id, dependency.prerequisite_stage))
                if predecessor is None and not self.observation.confirms_repair(dependency.prerequisite_item_id, dependency.prerequisite_stage):
                    continue
                command = replace(command, predecessor_id=predecessor)
            commands.append(command)
        self.commands = commands


async def owner_room_exists(config, seed, team, slot, generation):
    path = Path(config.get("delivery_ledger_file") or Path(config["client_state_file"]).with_name("delivery.sqlite3")).resolve()
    if not path.is_file():
        if config.get("delivery_ledger_expected") or path.with_suffix(".expected").exists():
            raise StorageError(text("launcher.items", "missing"))
        return False
    ledger = await Ledger.open(path, read_only=True)
    try:
        room = await ledger.room(RoomKey(seed, team, slot, generation))
        if room is None:
            return False
        if room.authority is not Authority.LEDGER:
            raise Conflict("Room requires ledger delivery")
        return True
    finally:
        await ledger.close()


class RoomDelivery:
    def __init__(self, bridge, config, definitions, policies, emit):
        self.bridge, self.config, self.definitions, self.policies, self.emit = bridge, config, definitions, policies, emit
        self.ledger = None
        self.coordinator = None
        self.transport = None
        self.ammo = None
        self.game_lock = None
        self.key = RoomKey(bridge.room_seed_name, bridge.team, bridge.slot,
                           bridge._connected_slot_data["native_generation_fingerprint"])
        self.path = Path(config.get("delivery_ledger_file") or Path(config["client_state_file"]).with_name("delivery.sqlite3")).resolve()
        self.lock = asyncio.Lock()
        self.can_spend = False
        self.closed = False
        self.planned_ids = ()
        self.balance = {"available": 0, "consumed": 0, "authoritative": False}
        self._acked = set()

    async def _open(self):
        if self.ledger is not None:
            return
        expected = self.path.with_suffix(".expected")
        if self.path.is_file():
            self.ledger = await Ledger.open(self.path)
        elif expected.exists() or self.config.get("delivery_ledger_expected") or self.bridge.session_state.get("delivery_ledger_expected"):
            raise StorageError(text("launcher.items", "missing"))
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.ledger = await Ledger.create(self.path)
        completed = False
        try:
            if not expected.exists():
                with expected.open("xb") as stream:
                    stream.write(b"delivery ledger\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            await self._bind()
            completed = True
        finally:
            if not completed:
                await self.ledger.close()
                self.ledger = None

    async def _bind(self):
        room = await self.ledger.room(self.key)
        self.binding = await self.ledger.bind(self.key, str(uuid4()), compatibility_revision=1, authority=Authority.LEDGER,
                                               expected_revision=None if room is None else room.revision)
        self.bridge.session_state["delivery_ledger_expected"] = True
        self.bridge.persist_session_state()

    async def _ingest_existing_owner(self):
        if self.coordinator is None:
            if not self.path.is_file():
                if self.path.with_suffix(".expected").exists() or self.config.get("delivery_ledger_expected") or self.bridge.session_state.get("delivery_ledger_expected"):
                    raise StorageError(text("launcher.items", "missing"))
                return
            await self._open()
            self.coordinator = DeliveryCoordinator(self.ledger, self.binding, transport=self.transport,
                                                   mode=DeliveryMode.OWNER, is_current=self.current)
        await self.ingest()

    async def ready(self, evidence):
        if self.closed:
            return False
        bridge = self.bridge
        if not bridge.delivery_actions_ready() or not await bridge.saves.checkpoint_recovery_ready() or not await bridge.items.runtime_effects_ready(evidence):
            return False
        link = await bridge.items._native_game_link_async()
        scope = await bridge.items._native_materialization_scope(evidence, link=link)
        if scope is None:
            return False
        identity = dict(pid=link.pid, process_created=int(scope[3]), instance_id=list(UUID(scope[4]).bytes), generation=scope[6])
        if self.transport is None or self.transport.scope != identity:
            await self._transport(link, identity)
        else:
            await self.transport.connect()
        admission = await self.transport.observe(12, b"0" * 64, bytes(16))
        if admission is None or not any(admission["save_binding"]):
            return False
        await self._open()
        await self._context(link, scope, identity, admission)
        return True

    async def _context(self, link, scope, identity, admission):
        epoch = await self.ledger.save_epoch(self.key)
        if epoch is not None and epoch.state is SaveEpochState.PREPARED:
            raise Conflict("New save preparation was interrupted; restore or finish the archived restart")
        save_id = f"{epoch.epoch_id if epoch else 'initial'}:{scope[1]}"
        candidate = Context(str(uuid4()), self.binding, link.pid, scope[3], str(UUID(bytes=bytes(identity["instance_id"]))),
                            self.bridge.runtime_lifecycle.snapshot.campaign, scope[-1], save_id, str(scope[6]), scope[6],
                            str(UUID(bytes=bytes(admission["save_binding"]))))
        if self.coordinator is None or not self.coordinator._same_context(candidate):
            await self.ledger.context(self.binding, candidate, ())
            self.coordinator = DeliveryCoordinator(self.ledger, self.binding, candidate, self.transport, mode=DeliveryMode.OWNER)
            self.coordinator.is_current = self.current
            self.bridge.delivery_coordinator = self.coordinator
            self.ammo = LedgerAmmoRefill(self.coordinator, self.storage, self.bridge.state_key)
            self.coordinator.accounting = self.ammo

    def current(self):
        return (not self.closed and self.bridge.room_seed_name == self.key.seed
                and (self.bridge.team, self.bridge.slot) == (self.key.team, self.key.slot)
                and self.bridge._connected_slot_data["native_generation_fingerprint"] == self.key.generation_fp)

    async def _transport(self, link, identity):
        if self.transport is not None:
            await self.transport.close()
        if self.game_lock is None:
            self.game_lock = _writer_lock(Path(self.bridge.base_directory) / "delivery-game")
        probe = str(link.probe)
        if link.command_prefix:
            from doom_eap.runtime.bridge_config import windows_path
            probe = windows_path(link.probe)
        transport = NativeTransport((*link.command_prefix, probe), identity, env=link.environment)
        completed = False
        try:
            await transport.connect()
            if not all(transport.supports(command) for command in REQUIRED_COMMANDS):
                raise Conflict("Core does not support ledger delivery")
            completed = True
        finally:
            if not completed:
                await transport.close()
                self.transport = None
        self.transport = transport
        self.storage = AmmoStorage(self.bridge.send_msgs)

    async def ingest(self):
        room = await self.ledger.room(self.key)
        history = self.bridge.items_received
        start = 0 if not getattr(self, "history_checked", False) else room.receipt_boundary
        for offset in range(start, len(history), 256):
            receipts = tuple(NetworkReceipt(item.item, item.location, item.player, item.flags) for item in history[offset:offset + 256])
            await self.coordinator.ingest(offset, receipts)
            await asyncio.sleep(0)
        self.history_checked = True
        self.bridge.receipt_session.restore_boundary((await self.ledger.room(self.key)).receipt_boundary)

    async def _rows(self, states=tuple(CommandState), *, unresolved_only=False):
        rows, after = [], None
        while True:
            page = await self.ledger.commands(self.key, states, after, 256, unresolved_only=unresolved_only)
            rows.extend(page)
            if len(page) < 256:
                return rows
            after = page[-1].plan.command_id
            await asyncio.sleep(0)

    async def _ownership(self, epoch):
        receipts, offset = [], 0
        while page := await self.ledger.receipts(self.key, offset, 256):
            receipts.extend(page)
            offset += len(page)
        live = frozenset(index for index in range(len(receipts)) if self.bridge.receipt_session.packet_is_live_tail(index))
        ownership = ownership_from_ledger(receipts, self.bridge.receipt_session.resolved_starting_ownership, live_receipt_indices=live)
        effective = self.bridge.items.effective_delivery_ownership(receipts)
        ownership = replace(ownership, reconciliation_item_ids=tuple(i for i in effective.reconciliation_item_ids if i in self.definitions),
                            blood_punch_upgrades=effective.blood_punch_upgrades,
                            acquisitions=tuple(a for a in ownership.acquisitions if a.item_id in self.definitions))
        return receipts, ownership

    async def _snapshot(self, context, evidence):
        scope = self.coordinator._evidence_scope(self.coordinator.context, self.namespace)
        return await observe_delivery(self.transport, scope, context, self.key.seed, evidence.epoch)

    async def local_command(self, key, command, evidence, *, lease=None, receipt_index=None):
        """Persist a scoped runtime effect before sending; dispatch stays unverified."""
        tag, payload = console_payload(command)
        async with self.lock:
            if not await self.ready(evidence):
                return False
            if lease is not None and lease != self.bridge.runtime_lifecycle.map_epochs.published_materialization_lease:
                return False
            context = self.coordinator.context
            plan, sources = self._runtime_plan(key, command, tag, payload, context, receipt_index)
            command_id = plan.command_id
            row = await self.ledger.command(self.key, command_id)
            if row is None:
                await self.coordinator.plan((plan,), sources=sources)
                row = await self.ledger.command(self.key, command_id)
            if row.state in (CommandState.PLANNED, CommandState.QUEUED):
                await self._execute_command(row)
            elif row.state is CommandState.DISPATCHED:
                await self._finalize(row)
            row = await self.ledger.command(self.key, command_id)
            return row.state not in (CommandState.REJECTED, CommandState.STALE)

    def _runtime_plan(self, key, command, tag, payload, context, receipt_index):
        source_id = str(uuid5(NAMESPACE_URL, f"{self.key}:runtime:{key}"))
        source = EffectSource(source_id, SourceKind.BOOTSTRAP if key.startswith("bootstrap-") else SourceKind.DERIVED_FACT,
                              f"runtime:{key}", command.encode("utf-8"))
        realization = (f"{context.native_instance}:{context.save_binding_id}:{context.save_id}:{context.load_id}:{context.map_name}"
                       if receipt_index is None else "acquisition")
        command_id = str(uuid5(NAMESPACE_URL, f"{source_id}:{realization}"))
        item_id = self.bridge.items_received[receipt_index].item if receipt_index is not None else 0
        plan = CommandPlan(command_id, receipt_index, None, f"{item_id}:0:runtime:{key}", realization, 0, None, None,
                           "command", 1, codec.encode_variant(46, 1, tag, payload), context.context_id,
                           ReplayPolicy.ADDITIVE, receipt_index is not None, 1, source_id if receipt_index is None else None)
        return plan, (source,) if receipt_index is None else ()

    @property
    def namespace(self):
        from doom_eap.runtime.inventory import namespace_id
        return namespace_id(self.key.seed, self.key.team, self.key.slot, self.key.generation_fp).encode("ascii")

    async def reconcile(self, evidence, *, selected=None):
        async with self.lock:
            await self._ingest_existing_owner()
            if not await self.ready(evidence):
                return None, 'Return to DOOM and unpause within 30 seconds. Inventory resync is waiting for safe gameplay.'
            await self.ingest()
            context = self.bridge.items._refresh_runtime_context(self.bridge._connected_slot_data, evidence)
            snapshot = await self._snapshot(context, evidence)
            if snapshot is None:
                return None, 'Repair dispatched; context changed before ownership could be observed'
            await self._recover(snapshot)
            plan = await self._plan(context, snapshot, evidence, selected)
            await self._execute(snapshot)
            after = await self._snapshot(context, evidence)
            if after is not None:
                await self._recover(after)
            if selected is None:
                await self._balance()
            await self._publish_local_obligation()
            if selected is not None and (plan.unresolved_item_ids or await self._has_unconfirmed_repairs(selected)):
                return plan.reconciliation, 'Repair dispatched; native ownership has not been confirmed. Consumables were preserved.'
        if selected is None:
            await self._temporaries(plan)
        return plan.reconciliation, None

    async def _temporaries(self, plan):
        for acquisition in plan.acquisitions:
            if acquisition.transient:
                await self.bridge.transient_effect_manager.apply_receipt(
                    acquisition.item_id, self.bridge.items._observe_transient_runtime(), receipt_index=int(acquisition.source_key[3:]))

    async def _has_unconfirmed_repairs(self, selected):
        for row in await self._rows((*ACTIVE, CommandState.REJECTED), unresolved_only=True):
            if row.plan.required and row.plan.ammo_operation_id is None and effect_identity(row.plan)[0] in selected:
                self.planned_ids = tuple(dict.fromkeys((*self.planned_ids, row.plan.command_id)))
                return True
        return False

    async def _publish_local_obligation(self):
        if await self._publish_ammo_recovery():
            return
        rows = await self._rows((CommandState.EFFECT_UNKNOWN, CommandState.REJECTED), unresolved_only=True)
        row = next((r for r in rows if r.plan.required and r.plan.receipt_index is None), None)
        self.emit("delivery_local_status", room=asdict(self.key),
                  command_id=row.plan.command_id if row else None,
                  expected_revision=row.revision if row else None,
                  item_id=(7770024 if row.plan.ammo_operation_id else effect_identity(row.plan)[0]) if row else None)

    async def _publish_ammo_recovery(self):
        operations = await self.ledger.ammo_operations(self.key, (AmmoState.UNKNOWN,), None, 1)
        operation = next((row for row in operations if row.operation.writer_id == self.ledger.profile_id), None)
        if operation is not None:
            self.emit("delivery_local_status", room=asdict(self.key), item_id=7770024,
                      command_id="ammo-operation:" + operation.operation.operation_id, expected_revision=operation.revision)
            return True
        return False

    async def _plan(self, context, snapshot, evidence, selected):
        epoch = await self.ledger.save_epoch(self.key)
        receipts, ownership = await self._ownership(epoch)
        sources = await self._sources()
        rows = await self._rows()
        replacements = await self._missing_after_core_loss(rows, snapshot.readback.observation)
        planning_rows = _planning_rows(rows, replacements)
        obligations = _obligations(planning_rows, receipts, sources)
        scope = MaterializationScope(self.key.seed, self.key.team, self.key.slot, self.bridge.state_key,
                                     self.bridge.runtime_lifecycle.map_epochs.published_materialization_lease, evidence.epoch, "delivery", self.bridge.state_key)
        comparison = _epoch_ownership(ownership, epoch, self.policies)
        if epoch is not None:
            obligations = await self._epoch_obligations(planning_rows, receipts, sources)
        total_points = sum(a.item_id == 7770903 for a in comparison.acquisitions) * 3
        plan = self.coordinator.compare(comparison, definitions=self.definitions, policies=self.policies,
            observation=snapshot.readback.observation, context=context, scope=scope, obligations=obligations,
            special_mode=self.bridge._connected_slot_data["runtime"].get("special_weapon"),
            selected_item_ids=selected, weapon_points=(total_points, snapshot.weapon_points_gained)
            if selected is None or 7770903 in selected else None)
        compiler = DeliveryPlans(self.key, self.coordinator.context, ownership, self.definitions, epoch.epoch_id if epoch else "initial",
                                 snapshot.readback.observation)
        commands, local_sources = compiler.compile(plan)
        commands = restore_confirmed(commands, rows)
        if selected is None:
            commands += self._notifications(compiler, receipts)
        commands = _link_restoration(commands, replacements)
        self.planned_ids = tuple(c.command_id for c in commands)
        await self._store_plan(commands, local_sources)
        await self._points(compiler, plan, snapshot, comparison)
        await self._satisfied(receipts, snapshot.readback.observation)
        return plan

    def _notifications(self, compiler, receipts):
        start = len(compiler.commands)
        for index, receipt in enumerate(receipts):
            if receipt.item not in self.definitions:
                continue
            for ordinal, command in enumerate(self.bridge.items.delivery_notification_commands(receipt.item, index)):
                compiler.console(receipt.item, 0, ordinal, command, source_key=f"ap:{index}", acquisition=True)
                compiler.commands[-1] = replace(compiler.commands[-1], required=False)
        return tuple(compiler.commands[start:])

    async def _missing_after_core_loss(self, rows, observation):
        contexts, replacements = {}, {}
        for row in rows:
            if not row.plan.required or row.plan.replay is not ReplayPolicy.IDEMPOTENT or row.state is not CommandState.EFFECT_UNKNOWN:
                continue
            if row.plan.context_id not in contexts:
                contexts[row.plan.context_id] = await self.ledger.saved_context(self.key, row.plan.context_id)
            if contexts[row.plan.context_id].native_instance != self.coordinator.context.native_instance:
                item, stage = effect_identity(row.plan)
                if _persistent_missing(observation, item, stage):
                    replacements[row.plan.command_id] = row.plan.effect_key
        return replacements

    async def _store_plan(self, commands, local_sources):
        for start in range(0, len(commands), 256):
            missing = [c for c in commands[start:start + 256] if await self.ledger.command(self.key, c.command_id) is None]
            regular = []
            for command in missing:
                item_id, _ = effect_identity(command)
                if item_id in (7770016, 7770142):
                    await self.coordinator.plan_battery(command, 2 if item_id == 7770142 else 1, self.namespace, sources=local_sources)
                    await self._execute_command(await self.ledger.command(self.key, command.command_id))
                else:
                    regular.append(command)
            await self.coordinator.plan(tuple(regular), sources=local_sources)
            await asyncio.sleep(0)

    async def _epoch_obligations(self, rows, receipts, sources):
        contexts, obligations = {}, []
        for row in rows:
            if row.plan.context_id not in contexts:
                contexts[row.plan.context_id] = await self.ledger.saved_context(self.key, row.plan.context_id)
            if _obligation_in_epoch(row, contexts[row.plan.context_id], self.coordinator.context):
                value = obligation(row, receipts, sources)
                if value is not None:
                    obligations.append(value)
        return tuple(obligations)

    async def _sources(self):
        sources, after = {}, None
        while page := await self.ledger.sources(self.key, after, 256):
            sources.update({source.source_id: source for source in page})
            after = page[-1].source_id
        return sources

    async def _points(self, compiler, plan, snapshot, ownership):
        previous = None
        for baseline in plan.weapon_point_grants:
            acquisitions = [a for a in ownership.acquisitions if a.item_id == 7770903]
            compiler.commands = []
            compiler.add(7770903, baseline // 3, 0, "weapon_points", 1, {"amount": 3, "expected_gained": baseline},
                         source_key=acquisitions[baseline // 3].source_key)
            command, = compiler.commands
            command = replace(command, predecessor_id=previous, replay=ReplayPolicy.ADDITIVE)
            if await self.ledger.command(self.key, command.command_id) is None:
                await self.coordinator.plan((command,), sources=tuple(compiler.sources.values()))
            previous = command.command_id

    async def _satisfied(self, receipts, observation):
        for offset in range(0, len(receipts), 256):
            indices = tuple(offset + i for i, receipt in enumerate(receipts[offset:offset + 256])
                            if receipt.item in self.definitions and
                            (classify_item_definition(self.definitions[receipt.item]) == "no_op" or observation.is_owned(receipt.item)))
            if indices:
                await self.coordinator.satisfy(indices, b"inventory_snapshot")
        await self._satisfy_access(receipts)

    async def _satisfy_access(self, receipts):
        for item_id in sorted({r.item for r in receipts} & ACCESS_IDS.keys()):
            result = await observe_optional(self.transport, 55, self.namespace, UUID(self.coordinator.context.save_binding_id).bytes,
                                            stage_id=item_id)
            scope = self.coordinator._evidence_scope(self.coordinator.context, self.namespace)
            if result is not None and _access_observed(mission_access_observation(result), item_id, scope):
                indices = tuple(index for index, receipt in enumerate(receipts) if receipt.item == item_id)
                for start in range(0, len(indices), 256):
                    await self.coordinator.satisfy(indices[start:start + 256], b"native_mission_access")

    async def _recover(self, snapshot):
        for row in await self._rows((*ACTIVE, CommandState.STALE, CommandState.CONFIRMED, CommandState.REJECTED)):
            if row.state in (CommandState.CONFIRMED, CommandState.REJECTED) or row.resolution is not None:
                await self._ack(row)
                continue
            if row.state is CommandState.STALE:
                await self.coordinator.recover(row.plan.command_id)
                await self._ack(row)
                continue
            if row.attempt_count:
                if row.state in (CommandState.PLANNED, CommandState.QUEUED):
                    await self.coordinator.send(row.plan.command_id)
                    row = await self.ledger.command(self.key, row.plan.command_id)
                await self._recover_one(row, snapshot)
            elif row.plan.context_id != self.coordinator.context.context_id:
                original = await self.ledger.saved_context(self.key, row.plan.context_id)
                if not self.coordinator._same_context(original):
                    await self.coordinator.expire_unsent(row.plan.command_id)

    async def _recover_one(self, row, snapshot):
        proof = {}
        if row.plan.required and row.plan.replay is ReplayPolicy.IDEMPOTENT and row.plan.ammo_operation_id is None:
            attempts = await self.ledger.attempts(self.key, row.plan.command_id, row.attempt_count - 1, 1)
            item_id, stage = effect_identity(row.plan)
            original = await self.ledger.saved_context(self.key, row.plan.context_id)
            if self.coordinator._same_context(original):
                proof = dict(expectation=EffectExpectation(attempts[0].attempt.reference, snapshot.readback.after_scope,
                                                           InventoryEffect(item_id, stage)), evidence=snapshot.readback)
        await self._finalize(row, **proof)

    async def _finalize(self, row, **proof):
        await self.coordinator.recover(row.plan.command_id, **proof)
        current = await self.ledger.command(self.key, row.plan.command_id)
        await self._ack(current)
        if current.revision != row.revision:
            self.emit("delivery_progress", room=asdict(self.key), command_id=row.plan.command_id,
                      state=current.state.value, revision=current.revision, receipt_index=row.plan.receipt_index)

    async def _ack(self, row):
        if (row.plan.command_id in self._acked or not row.attempt_count
                or row.state not in (CommandState.CONFIRMED, CommandState.REJECTED, CommandState.EFFECT_UNKNOWN, CommandState.STALE)):
            return
        try:
            response = await self.coordinator.ack(row.plan.command_id)
        except (TransportError, OSError, TimeoutError):
            return
        if response is not None and response.response["error"] == 0:
            self._acked.add(row.plan.command_id)

    async def _execute(self, snapshot):
        rows = await self._rows((CommandState.PLANNED, CommandState.QUEUED), unresolved_only=True)
        for row in _execution_batch(rows):
            try:
                await self._execute_command(row)
            except Conflict:
                continue
            except (TransportError, TimeoutError):
                if row.plan.required:
                    raise
                return
            await asyncio.sleep(0)

    async def _execute_command(self, row):
        await self.coordinator.prepare(row.plan.command_id, self.namespace)
        await self.coordinator.send(row.plan.command_id)
        await self._finalize(row)

    async def _balance(self):
        self.can_spend = await self.ammo.owner() in (None, self.ledger.profile_id)
        self.coordinator.can_spend = self.can_spend
        if self.can_spend:
            boundary = self.bridge.receipt_session.processed_boundary
            received = sum(item.item == 7770024 for item in self.bridge.items_received[:boundary])
            received += self.bridge.receipt_session.resolved_starting_count(7770024)
            scope = AmmoCommandScope(self.bridge.state_key, self.coordinator.context.map_name, self.coordinator.context.save_id, False)
            totals = await self.ammo.synchronize(received, scope)
            if totals is not None:
                self.balance = dict(available=min(AMMO_REFILL_CAPACITY, max(0, totals.credited - totals.consumed - totals.discarded - totals.reserved)),
                                    consumed=totals.consumed, authoritative=True)
        self.emit("ammo_refill", **self.balance, can_spend=self.can_spend, status="balance",
                  message='Ammo Refill charges available: {count}'.format(count=str(self.balance["available"])))

    async def close(self):
        self.closed = True
        if self.transport is not None:
            await self.transport.close()
        if self.ledger is not None:
            await self.ledger.close()
        if self.game_lock is not None:
            self.game_lock.close()


def control_room(value) -> RoomKey:
    if not isinstance(value, dict) or value.keys() != {"seed", "team", "slot", "generation_fp"}:
        raise ValueError("invalid delivery room")
    if not all(isinstance(value[key], str) and value[key] for key in ("seed", "generation_fp")):
        raise ValueError("invalid delivery room identity")
    if not all(type(value[key]) is int and value[key] >= 0 for key in ("team", "slot")):
        raise ValueError("invalid delivery player")
    return RoomKey(**value)


async def handle_delivery_control(coordinator, control, emit):
    kind = control.get("type")
    if kind not in {"delivery_capabilities", "item_resolution"}:
        return False
    request_id = control.get("request_id")
    room = None
    try:
        room = control_room(control.get("room"))
        if not isinstance(request_id, str):
            raise ValueError("invalid delivery request")
        UUID(request_id)
    except (ValueError, TypeError):
        return True
    try:
        replacement = await _execute(coordinator, room, control)
    except (LedgerError, TransportError, ValueError, TypeError, OSError, TimeoutError):
        ok = False
        replacement = None
    else:
        ok = True
    emit(
        "item_resolution_result" if kind == "item_resolution" else "accounting_capability_changed",
        request_id=request_id,
        room=asdict(room) if room else None,
        ok=ok,
        can_resolve=ok if kind == "delivery_capabilities" else False,
        can_spend=ok and getattr(coordinator, "can_spend", False),
        command_id=control.get("command_id"),
        expected_revision=control.get("expected_revision"),
        replacement_id=replacement,
    )
    return True


def _terminal_state(response, expectation, evidence, inventory_request):
    from doom_eap.runtime.inventory import effect_confirmed

    dimensions = response.response["reply"]["dimensions"]
    if dimensions["progress"] != Progress.TERMINAL:
        return CommandState.EFFECT_UNKNOWN
    if dimensions["execution"] == Execution.NOT_ENTERED and dimensions["effect"] == Effect.NO_EFFECT:
        return CommandState.REJECTED
    if expectation is not None:
        proven = evidence is not None and effect_confirmed(expectation, evidence)
        return CommandState.CONFIRMED if proven else CommandState.EFFECT_UNKNOWN
    if (dimensions["retention"] == Retention.FULL
            and dimensions["effect"] == Effect.OBSERVED
            and _inventory_confirmed(inventory_request, response.result)):
        return CommandState.CONFIRMED
    return CommandState.EFFECT_UNKNOWN


def _refusal_state(response):
    rules = codec.SCHEMA["command_results"]
    if not _matches_result(response, rules["not_executed"]):
        return None
    if any(_matches_result(response, rule) for rule in rules["admission_refusals"]):
        return CommandState.STALE
    if any(_matches_result(response, rule) for rule in rules["definitive_rejections"]):
        return CommandState.REJECTED
    return None


def _matches_result(response, rule):
    value = response.response
    fields = {"error": value["error"], "state": value["execution"]["state"],
              "reason": value["execution"]["reason"],
              "execution": value["reply"]["dimensions"]["execution"],
              "effect": value["reply"]["dimensions"]["effect"]}
    return all(fields[key] == expected for key, expected in rule.items() if key in fields)


def _inventory_confirmed(request, result):
    if request is None or result is None or result.get("native_exception", 0):
        return False
    masks = (request["weapons"], request["equipment"])
    if not any(masks):
        return False
    after = (result.get("weapons_after", 0xffffffff), result.get("equipment_after", 0xffffffff))
    return all(value != 0xffffffff and value & mask == mask
               for value, mask in zip(after, masks, strict=True) if mask)


def _proof_bytes(expectation, evidence):
    return json.dumps({"expectation": asdict(expectation), "evidence_type": type(evidence).__name__,
                       "evidence": asdict(evidence)},
                      default=lambda value: value.hex() if isinstance(value, bytes) else value.value,
                      sort_keys=True).encode("utf-8")


def _weapon_points_confirmed(command, response):
    dimensions = response.response["reply"]["dimensions"]
    if (response.response["error"] or dimensions["progress"] != Progress.TERMINAL
            or dimensions["effect"] != Effect.OBSERVED or dimensions["retention"] != Retention.FULL):
        return False
    result = response.result
    if result is None or result.get("native_exception") or not result.get("flags", 0) & 16:
        return False
    _, grant = codec.decode_variant(COMMAND_IDS["weapon_points"], command.plan.payload_revision, command.plan.payload)
    return (result["gained_before"] == grant["expected_gained"]
            and result["gained_after"] == grant["expected_gained"] + grant["amount"]
            and result["balance_after"] - result["balance_before"] == grant["amount"])


def _native_currency_evidence(kind, reference, result, grant):
    if kind != "currencies" or result is None or result["kind"] != 1:
        return None
    if (result["amount"], result["expected_gained"]) != (grant.amount, grant.expected_gained):
        return None
    return currency_grant_result(result, reference)


def _stored_battery_plan(command, stored, amount):
    if stored.kind != "currencies":
        return command
    _, grant = codec.decode_variant(COMMAND_IDS["currencies"], stored.payload_revision, stored.payload)
    if grant["amount"] != amount:
        raise Conflict("Battery amount differs from the stored command")
    return replace(command, kind=stored.kind, payload_revision=stored.payload_revision, payload=stored.payload)


def _recoverable(command, evidence):
    if command.resolution is not None:
        return False
    if command.state not in TERMINAL:
        return True
    return command.state is CommandState.EFFECT_UNKNOWN and evidence is not None


def _latest(row, children):
    while row.plan.command_id in children:
        row = children[row.plan.command_id]
    return row


def _epoch_ownership(ownership, epoch, policies):
    if epoch is None:
        return ownership
    acquisitions = tuple(a for a in ownership.acquisitions
                         if policies[a.item_id].policy != NEVER_REPLAY
                         or (a.source_key.startswith("ap:") and int(a.source_key[3:]) >= epoch.receipt_boundary))
    return replace(ownership, acquisitions=acquisitions)


def _access_observed(observation, item_id, scope):
    access = MissionAccess(observation.revision, item_id, STAGES[ACCESS_IDS[item_id]]["map"])
    return effect_confirmed(EffectExpectation(b"", scope, access), observation)


def _persistent_missing(observation, item, stage):
    from doom_eap.contracts.inventory_domain import UNKNOWN
    if item == 7770904:
        return item in observation.stage_masks and not observation.stage_masks[item] & (1 << stage)
    return observation.get_state(item) != UNKNOWN and not observation.confirms_repair(item, stage)


def _link_restoration(commands, replacements):
    by_effect = {effect: original for original, effect in replacements.items()}
    return tuple(replace(command, supersedes_id=command.supersedes_id or by_effect.get(command.effect_key)) for command in commands)


def _planning_rows(rows, replacements):
    return [row for row in rows if row.plan.command_id not in replacements]


def _obligations(rows, receipts, sources):
    return tuple(value for row in rows if (value := obligation(row, receipts, sources)) is not None)


def _obligation_in_epoch(row, original, current):
    if original.save_id == current.save_id:
        return True
    if row.plan.ammo_operation_id is not None:
        return False
    return row.plan.replay is ReplayPolicy.ADDITIVE and effect_identity(row.plan)[0] != 7770903


def _execution_batch(rows):
    return [row for row in rows if row.plan.required] + [row for row in rows if not row.plan.required][:64]


async def _execute(coordinator, room, control):
    if coordinator is None or coordinator.binding is None or room != coordinator.binding.room:
        raise Conflict("Delivery room is not active")
    await coordinator.require_owner()
    if control["type"] == "item_resolution":
        return await _resolve(coordinator, control)


async def _resolve(coordinator, control):
    command = control.get("command_id")
    revision = control.get("expected_revision")
    if not isinstance(command, str) or not command or type(revision) is not int or revision < 0:
        raise ValueError("invalid delivery resolution target")
    if command.startswith("ammo-operation:"):
        if ResolutionAction(control.get("action")) is not ResolutionAction.TRY_AGAIN:
            raise ValueError("invalid ammo recovery action")
        if not await coordinator.accounting.retry(command.removeprefix("ammo-operation:"), revision):
            raise Conflict("Ammo recovery is waiting for server confirmation")
        return None
    return await coordinator.resolve(command, revision, ResolutionAction(control.get("action")))


if TYPE_CHECKING:
    from doom_eap.runtime.inventory import InventoryOwnership, InventoryPlan


TERMINAL = frozenset((CommandState.CONFIRMED, CommandState.REJECTED, CommandState.EFFECT_UNKNOWN, CommandState.STALE))
COMMAND_IDS = {command["name"]: command_id for command_id, command in codec.COMMANDS.items()}
REQUIRED_COMMANDS = (12, 18, 25, 29, 33, 37, 46, 53, 54, 55)
ACTIVE = (CommandState.PLANNED, CommandState.QUEUED, CommandState.DISPATCHED, CommandState.EFFECT_UNKNOWN)
