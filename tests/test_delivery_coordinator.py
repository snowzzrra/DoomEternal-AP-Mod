"""Protect delivery decisions through coordinator entry points and a real ledger."""

import asyncio
from dataclasses import replace
from hashlib import sha256
from uuid import UUID

import pytest
from native_transport_support import command_response

from doom_eap.runtime import sentinel_protocol_generated as codec
from doom_eap.runtime.ammo import LedgerAmmoRefill
from doom_eap.runtime.ammo_adapters import AmmoStorage
from doom_eap.runtime.delivery import DeliveryCoordinator, DeliveryMode
from doom_eap.runtime.inventory import InventoryOwnership
from doom_eap.runtime.ledger import (
    Authority,
    CommandPlan,
    CommandState,
    Conflict,
    Context,
    Ledger,
    NetworkReceipt,
    ReplayPolicy,
    ResolutionAction,
    RevisionConflict,
    RoomKey,
    StaleBinding,
    StorageError,
)
from doom_eap.runtime.native_transport import CommandResponse, PreparedCommand


def response(reference, *, effect=1, progress=2, execution=1, retention=0, error=0, state=0, reason=0):
    ref = codec.decode("command_reference", reference)
    value = command_response(ref, effect=effect, progress=progress, execution=execution,
                             retention=retention, error=error, state=state, reason=reason)
    return CommandResponse(value, {"weapons_after": 1, "equipment_after": 0} if retention == 0 else None,
                           codec.encode("command_response", value))


class Transport:
    def __init__(self):
        self.calls, self.commands, self.effects = [], {}, []
        self.effect = 1

    async def prepare(self, submission, payload):
        envelope = codec.encode("submission", submission) + payload
        immutable = {**submission["reference"], "save_binding": submission["save_binding"],
                     "deadline_ms": submission["deadline_ms"]}
        del immutable["command_id"]
        fingerprint = sha256(codec.encode("immutable_submission", immutable) + payload).digest()
        reference = codec.encode("command_reference", {**submission["reference"], "fingerprint": fingerprint})
        return PreparedCommand(envelope, reference)

    async def query(self, reference):
        self.calls.append("query")
        return self.commands.get(reference, response(reference, progress=3, retention=2, error=4))

    async def submit(self, prepared):
        self.calls.append("submit")
        if prepared.reference not in self.commands:
            self.effects.append(prepared.reference)
            self.commands[prepared.reference] = response(prepared.reference, effect=self.effect)
        return self.commands[prepared.reference]

    async def cancel(self, reference):
        self.calls.append("cancel")
        self.commands[reference] = response(reference, effect=0, execution=0)
        return self.commands[reference]

    async def ack(self, reference, token):
        self.calls.append("ack")
        return self.commands[reference]


def command(context, command_id="grant", *, predecessor=None, required=True):
    return CommandPlan(command_id, 0, None, command_id, "acquisition", 0, predecessor, None,
                       "inventory", 1, codec.encode_variant(25, 1, 1, {"weapons": 1, "equipment": 0}),
                       context.context_id, ReplayPolicy.ADDITIVE, required, 1)


@pytest.fixture
def owner(tmp_path):
    def run(check):
        async def exercise():
            ledger = await Ledger.create(tmp_path / "delivery.sqlite3")
            try:
                key = RoomKey("room", 0, 1, "fixture")
                binding = await ledger.bind(key, "connection", compatibility_revision=1, authority=Authority.LEDGER)
                context = Context("context", binding, 123, "123", str(UUID(bytes=bytes([1]) * 16)),
                                  "base", "map", "save", "load", 1, str(UUID(int=1)))
                await ledger.context(binding, context, ())
                transport = Transport()
                delivery = DeliveryCoordinator(ledger, binding, context, transport, mode=DeliveryMode.OWNER)
                await delivery.ingest(0, (NetworkReceipt(7770025, 1, 1, 0),))
                await check(delivery, transport)
            finally:
                await ledger.close()
        return asyncio.run(exercise())
    return run


@pytest.mark.parametrize("state,reason,error,execution,effect,expected", [
    (4, 18, 0, 0, 0, CommandState.STALE),
    (4, 6, 0, 0, 0, CommandState.STALE),
    (0, 0, 2, 0, 0, CommandState.STALE),
    *[(4, reason, 0, 0, 0, CommandState.REJECTED) for reason in (13, 14, 15, 16, 17)],
    *[(0, 0, error, 0, 0, CommandState.REJECTED) for error in (3, 5)],
    (0, 18, 0, 0, 0, CommandState.REJECTED),
    (0, 6, 0, 0, 0, CommandState.REJECTED),
    *[(4, reason, 0, execution, 4, CommandState.EFFECT_UNKNOWN)
      for reason in (6, 17, 18) for execution in (0, 1, 2)],
    *[(0, 0, error, 2, 4, CommandState.EFFECT_UNKNOWN) for error in (1, 2, 3, 4, 5, 8)],
    (4, 18, 0, 1, 0, CommandState.EFFECT_UNKNOWN),
    (4, 18, 0, 2, 0, CommandState.EFFECT_UNKNOWN),
    (4, 18, 0, 0, 3, CommandState.EFFECT_UNKNOWN),
])
def test_refusals_replace_only_proven_unexecuted_commands(owner, state, reason, error, execution, effect, expected):
    async def check(delivery, transport):
        await delivery.plan((command(delivery.context),))
        prepared = await delivery.prepare("grant", b"a" * 64)
        await delivery.send("grant")
        transport.commands[prepared.reference] = response(
            prepared.reference, effect=effect, execution=execution, retention=1, state=state, reason=reason, error=error)
        await delivery.recover("grant")
        rows = await delivery.ledger.commands(delivery.binding.room, tuple(CommandState), None, 10)
        successors = [row for row in rows if row.plan.supersedes_id == "grant"]
        assert bool(successors) is (expected is CommandState.STALE)
        original = await delivery.ledger.command(delivery.binding.room, "grant")
        assert original.state is expected
        if successors:
            successor, = successors
            assert successor.plan.command_id != "grant"
            await delivery.recover("grant")
            assert len(await delivery.ledger.commands(delivery.binding.room, tuple(CommandState), None, 10)) == 2
            await delivery.prepare(successor.plan.command_id, b"a" * 64)
            await delivery.send(successor.plan.command_id)
        elif expected is CommandState.REJECTED:
            item, = await delivery.ledger.items(delivery.binding.room, -1, 10)
            assert item.actions
            await delivery.resolve("grant", original.revision, ResolutionAction.MARK_AS_RECEIVED)
    owner(check)


@pytest.mark.parametrize("state,reason,error", [(4, 18, 0), (4, 6, 0), (0, 0, 2)])
def test_submit_refusal_is_saved_before_query_can_lose_it(owner, state, reason, error):
    async def check(delivery, transport):
        await delivery.plan((command(delivery.context),))
        prepared = await delivery.prepare("grant", b"a" * 64)

        async def refuse(submission):
            assert submission == prepared
            transport.calls.append("submit")
            return response(prepared.reference, progress=3 if error else 2, execution=0, effect=0,
                            state=state, reason=reason, error=error)

        transport.submit = refuse
        await delivery.send("grant")
        original = await delivery.ledger.command(delivery.binding.room, "grant")
        assert original.state is CommandState.STALE
        assert original.replacement_id and original.replacement_id != "grant"
        assert transport.calls == ["query", "submit"]
        await delivery.recover("grant")
        assert (await delivery.ledger.command(delivery.binding.room, "grant")).replacement_id == original.replacement_id
        stored, = await delivery.ledger.attempts(delivery.binding.room, "grant", 0, 1)
        assert stored.completed_at is not None
        assert codec.decode("command_response", stored.result)["error"] == error
    owner(check)


def test_compare_cannot_send_or_change_obligations(owner):
    async def check(delivery, transport):
        passive = DeliveryCoordinator(delivery.ledger, delivery.binding, delivery.context, transport)
        assert not passive.compare(InventoryOwnership(())).native_effects
        with pytest.raises(Conflict, match="Compare"):
            await passive.plan((command(delivery.context),))
        with pytest.raises(Conflict, match="Compare"):
            await passive.send("grant")
        messages = []

        async def send(packets):
            messages.extend(packets)

        ammo = LedgerAmmoRefill(passive, AmmoStorage(send), "room")
        with pytest.raises(Conflict, match="Compare"):
            await ammo.claim()
        assert messages == []
        assert not transport.calls and not transport.effects
        assert not await delivery.ledger.commands(delivery.binding.room, tuple(CommandState), None, 1)
    owner(check)


@pytest.mark.parametrize("effect,state", [(1, CommandState.CONFIRMED), (2, CommandState.EFFECT_UNKNOWN),
                                         (3, CommandState.EFFECT_UNKNOWN), (4, CommandState.EFFECT_UNKNOWN)])
def test_result_is_committed_before_ack_and_only_readback_confirms(owner, effect, state):
    async def check(delivery, transport):
        transport.effect = effect
        await delivery.plan((command(delivery.context),))
        prepared = await delivery.prepare("grant", b"a" * 64)
        stored, = await delivery.ledger.attempts(delivery.binding.room, "grant", 0, 1)
        assert stored.attempt.reference == prepared.reference
        canonical = bytes(codec.decode("command_reference", prepared.reference)["fingerprint"]).hex()
        assert stored.envelope_fingerprint != canonical
        await delivery.send("grant")
        assert transport.calls == ["query", "submit"]
        with pytest.raises(Conflict, match="Record"):
            await delivery.ack("grant")
        await delivery.recover("grant")
        assert (await delivery.ledger.command(delivery.binding.room, "grant")).state is state
        assert "ack" not in transport.calls
        await delivery.ack("grant")
        assert transport.calls[-1] == "ack" and len(transport.effects) == 1
    owner(check)


@pytest.mark.parametrize("field,value", [("load_id", "reload"), ("save_id", "another"), ("map_name", "another")])
def test_changed_context_cancels_work_before_replacement(owner, field, value):
    async def check(delivery, transport):
        await delivery.plan((command(delivery.context),))
        prepared = await delivery.prepare("grant", b"a" * 64)
        transport.commands[prepared.reference] = response(prepared.reference, progress=0, execution=0, effect=0)
        delivery.context = replace(delivery.context, context_id="next", **{field: value})
        await delivery.ledger.context(delivery.binding, delivery.context, ())
        await delivery.send("grant")
        assert transport.calls == ["query", "cancel"]
        assert (await delivery.ledger.command(delivery.binding.room, "grant")).state is CommandState.REJECTED
        assert not transport.effects
    owner(check)


def test_uncertain_dependency_does_not_block_independent_work_and_manual_retry_is_single(owner):
    async def check(delivery, transport):
        await delivery.plan((command(delivery.context), command(delivery.context, "dependent", predecessor="grant"),
                             command(delivery.context, "independent")))
        transport.effect = 2
        await delivery.prepare("grant", b"a" * 64)
        await delivery.send("grant")
        await delivery.recover("grant")
        with pytest.raises(Conflict, match="dependency"):
            await delivery.prepare("dependent", b"a" * 64)
        transport.effect = 1
        await delivery.prepare("independent", b"a" * 64)
        await delivery.send("independent")
        original = await delivery.ledger.command(delivery.binding.room, "grant")
        replacement = await delivery.resolve("grant", original.revision, ResolutionAction.TRY_AGAIN)
        assert await delivery.resolve("grant", original.revision, ResolutionAction.TRY_AGAIN) == replacement
        with pytest.raises(RevisionConflict):
            await delivery.resolve("grant", original.revision, ResolutionAction.MARK_AS_RECEIVED)
        await delivery.prepare(replacement, b"a" * 64)
        await delivery.send(replacement)
        await delivery.recover(replacement)
        await delivery.prepare("dependent", b"a" * 64)
        assert len(await delivery.ledger.commands(delivery.binding.room, tuple(CommandState), None, 256)) == 4
    owner(check)


def test_failed_result_commit_cannot_be_acknowledged(owner, monkeypatch):
    async def check(delivery, transport):
        await delivery.plan((command(delivery.context),))
        await delivery.prepare("grant", b"a" * 64)
        await delivery.send("grant")
        async def fail(*args, **kwargs):
            raise StorageError("disk full")
        monkeypatch.setattr(delivery.ledger, "apply", fail)
        with pytest.raises(StorageError, match="disk full"):
            await delivery.recover("grant")
        with pytest.raises(Conflict, match="Record"):
            await delivery.ack("grant")
        assert "ack" not in transport.calls
    owner(check)


def test_replaced_writer_cannot_send_prepared_work(owner):
    async def check(delivery, transport):
        await delivery.plan((command(delivery.context),))
        await delivery.prepare("grant", b"a" * 64)
        room = await delivery.ledger.room(delivery.binding.room)
        await delivery.ledger.bind(room.key, "other", compatibility_revision=1, authority=Authority.LEDGER,
                                   expected_revision=room.revision)
        with pytest.raises(StaleBinding):
            await delivery.send("grant")
        assert not transport.calls
    owner(check)


def test_manual_acceptance_cancels_live_work_and_keeps_uncertain_evidence(owner):
    async def check(delivery, transport):
        transport.effect = 2
        await delivery.plan((command(delivery.context),))
        prepared = await delivery.prepare("grant", b"a" * 64)
        await delivery.send("grant")
        await delivery.recover("grant")
        original = await delivery.ledger.command(delivery.binding.room, "grant")
        transport.commands[prepared.reference] = response(prepared.reference, progress=0, execution=0, effect=0)
        transport.calls.clear()
        await delivery.resolve("grant", original.revision, ResolutionAction.MARK_AS_RECEIVED)
        assert transport.calls == ["query", "cancel"]
        accepted = await delivery.ledger.command(delivery.binding.room, "grant")
        assert accepted.state is CommandState.EFFECT_UNKNOWN and accepted.result == original.result
        items = await delivery.ledger.items(delivery.binding.room, -1, 256)
        assert items[0].status.value == "delivered" and not items[0].actions
        assert len(transport.effects) == 1
    owner(check)


def test_unsent_command_is_stale_in_another_load(owner):
    async def check(delivery, transport):
        await delivery.plan((command(delivery.context),))
        await delivery.prepare("grant", b"a" * 64)
        delivery.context = replace(delivery.context, context_id="next", load_id="reload")
        await delivery.ledger.context(delivery.binding, delivery.context, ())
        await delivery.send("grant")
        assert transport.calls == ["query"] and not transport.effects
        assert (await delivery.ledger.command(delivery.binding.room, "grant")).state is CommandState.STALE
    owner(check)
