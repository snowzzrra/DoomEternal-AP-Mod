"""Coordinate durable delivery and recovery; inventory plans and native execution have separate owners."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, replace
from enum import Enum, IntEnum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

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

from . import sentinel_protocol_generated as codec
from .inventory_observation import (
    currency_grant_result,
    currency_observation,
    mission_access_observation,
    progression_observation,
)
from .ledger import (
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
    EffectSource,
    ManualResolution,
    NetworkReceipt,
    ReceiptDecision,
    Resolution,
    ResolutionAction,
    RevisionConflict,
    StaleBinding,
)
from .native_transport import CommandResponse, IdentityMismatch, PreparedCommand, TransportError

if TYPE_CHECKING:
    from .inventory import InventoryOwnership, InventoryPlan


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


TERMINAL = frozenset((CommandState.CONFIRMED, CommandState.REJECTED, CommandState.EFFECT_UNKNOWN, CommandState.STALE))
COMMAND_IDS = {command["name"]: command_id for command_id, command in codec.COMMANDS.items()}


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


def _terminal_state(response, expectation, evidence, inventory_request):
    from .inventory import effect_confirmed

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


class DeliveryCoordinator:
    def __init__(self, ledger=None, binding=None, context=None, transport=None, *, mode=DeliveryMode.COMPARE, is_current=None):
        self.ledger, self.binding, self.context, self.transport = ledger, binding, context, transport
        self.mode = mode
        self.is_current = is_current
        self._lock = asyncio.Lock()

    def compare(self, ownership: InventoryOwnership, **snapshot) -> InventoryPlan:
        from .inventory import compare_inventory

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
        original = await self.ledger.saved_context(self.binding.room, command.plan.context_id)
        if not self._same_context(original):
            return False
        if not await self._ammo_paid(command):
            return False
        if command.plan.predecessor_id is None:
            return True
        predecessor = await self._command(command.plan.predecessor_id)
        while predecessor.resolution is Resolution.REPLACED:
            assert predecessor.replacement_id is not None
            predecessor = await self._command(predecessor.replacement_id)
        return predecessor.state is CommandState.CONFIRMED or predecessor.resolution is Resolution.ACCEPTED_BY_PLAYER

    async def _ammo_paid(self, command):
        operation_id = command.plan.ammo_operation_id
        if operation_id is None:
            return True
        row = await self.ledger.ammo_operation(self.binding.room, operation_id)
        return row is not None and row.state is AmmoState.CONFIRMED

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
                    await self._record(command, stored, CommandState.STALE, response)
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
        if command.resolution is not None:
            return
        stored = await self._attempt(command)
        if stored is not None:
            if not stored.result:
                return
            result, _ = codec._decode_type("command_response", stored.result, 0)
            response = CommandResponse(result, None, stored.result)
            if classify(response) is not CommandState.STALE:
                return
        replacement = await self._replacement_plan(command, stored)
        await self.ledger.resolve(self.binding, ManualResolution(
            command.plan.command_id, command.revision, ResolutionAction.TRY_AGAIN, replacement,
            stored.result if stored else b"unsent"))

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
        terminal = state in TERMINAL
        evidence = response.body if response is not None else b""
        reason = state.value if state in (CommandState.EFFECT_UNKNOWN, CommandState.REJECTED) else None
        if response is None:
            reason = "core_identity_changed"
        completion = AttemptChange(command.plan.command_id, stored.attempt.number, stored.revision,
                                   stored.acceptance if terminal else evidence,
                                   evidence if terminal else b"", time.time_ns() // 1000 if terminal else None)
        change = CommandChange(command.plan.command_id, command.revision, state, reason, proof or evidence, None)
        await self.ledger.apply(self.binding, (change,), (), (), attempts=(completion,))

    async def ack(self, command_id: str) -> CommandResponse | None:
        async with self._lock:
            await self.require_owner()
            command = await self._command(command_id)
            stored = await self._attempt(command)
            if stored is None or stored.completed_at is None:
                raise Conflict("Record the delivery result before acknowledging it")
            if not stored.result:
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
        from .weapon_points import namespace_id
        namespace = (bytes(codec.decode("command_reference", stored.attempt.reference)["namespace_id"]) if stored else
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
        if stored is None:
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
