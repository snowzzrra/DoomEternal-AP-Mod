"""Confirm family postconditions through inventory comparison and durable delivery."""

import asyncio
import hashlib
import json
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_delivery_coordinator as delivery_support
import test_inventory as inventory_support
from fixtures.native_effect_evidence import EvidenceCore
from native_transport_support import codec, currency_payloads, evidence_fixtures, evidence_result
from test_delivery_coordinator import command

from doom_eap.contracts.foundation import MASTERY_ITEM_BITS
from doom_eap.contracts.inventory_domain import (
    Currency,
    CurrencyGrant,
    CurrencyObservation,
    CurrencyOutcome,
    CurrencyValue,
    EffectExpectation,
    EffectProof,
    EvidenceScope,
    InventoryObservation,
    ItemObservation,
    MasteryEffect,
    MasterySlot,
    MeatHookPerk,
    MissionAccess,
    MissionAccessObservation,
    PerkStages,
    SaveReadiness,
    SlayerKey,
    SuitPerkStage,
    SuitStage,
)
from doom_eap.contracts.materialization import MaterializationScope
from doom_eap.runtime.context_registry import CONTEXT_BY_IDENTITY
from doom_eap.runtime.inventory import (
    InventoryObligation,
    InventoryOwnership,
    compare_inventory,
    currency_delta,
    effect_confirmed,
    ownership_from_ledger,
)
from doom_eap.runtime.inventory_observation import (
    SentinelInventoryObservation,
    currency_grant_result,
    currency_observation,
    mission_access_observation,
    progression_observation,
)
from doom_eap.runtime.item_reconciliation import load_policy_registry
from doom_eap.runtime.ledger import CommandState, Conflict, NetworkReceipt

owner = delivery_support.owner
inventory = inventory_support.inventory


@pytest.mark.parametrize("kind", ["currencies", "weapon_points"])
def test_manual_currency_retry_observes_current_gained_before_new_attempt(owner, kind):
    async def check(delivery, transport):
        scope = delivery._evidence_scope(delivery.context, b"a" * 64)
        baseline = wire_for_scope(evidence_result("currency_result"), scope)
        baseline.update(gained_after=7)
        if kind == "weapon_points":
            baseline.update(flags=16, native_exception=0, outcome=1)
        async def observe(*args, **kwargs):
            return baseline
        transport.supports = lambda _: True
        transport.observe = observe
        command_type = 53 if kind == "currencies" else 18
        if kind == "currencies":
            await delivery.plan_battery(command(delivery.context), 2, b"a" * 64)
        else:
            await delivery.plan((replace(command(delivery.context), kind=kind,
                payload=codec.encode_variant(18, 1, 1, dict(amount=3, expected_gained=7))),))
        await delivery.prepare("grant", b"a" * 64)
        await delivery.send("grant")
        reference = (await delivery.ledger.attempts(delivery.binding.room, "grant", 0, 1))[0].attempt.reference
        transport.commands[reference] = replace(transport.commands[reference], result=None)
        await delivery.recover("grant")
        original = await delivery.ledger.command(delivery.binding.room, "grant")
        assert original.state is CommandState.EFFECT_UNKNOWN
        baseline["gained_after"] = 8
        from doom_eap.runtime.ledger import ResolutionAction
        replacement = await delivery.resolve("grant", original.revision, ResolutionAction.TRY_AGAIN)
        row = await delivery.ledger.command(delivery.binding.room, replacement)
        assert codec.decode_variant(command_type, 1, row.plan.payload)[1]["expected_gained"] == 8
    owner(check)


@pytest.mark.parametrize("amount", [1, 2])
@pytest.mark.parametrize("balance", [0, 7])
def test_battery_needs_exact_balance_and_gained_inside_the_same_command(amount, balance):
    core = EvidenceCore()
    expected = EffectExpectation(b"command-reference", core.scope, CurrencyGrant(Currency.SENTINEL_BATTERY, amount, 4))
    result = core.grant(expected.reference, amount=amount, balance=balance)
    assert effect_confirmed(expected, result)
    assert currency_delta(result) == (amount, amount)
    assert not effect_confirmed(expected, core.progression())
    mutations = [
        replace(result, reference=b"other-fingerprint"),
        replace(result, before=CurrencyValue(balance, 3)),
        replace(result, after=CurrencyValue(balance + amount + 1, 4 + amount + 1)),
        replace(result, after=CurrencyValue(balance + amount, 4)),
        replace(result, after=CurrencyValue(None, 4 + amount)),
        replace(result, after=CurrencyValue(0x80000000, 4 + amount)),
        replace(result, writer_entered=False),
        replace(result, native_exception=1),
        replace(result, currency=Currency.PRAETOR),
    ]
    assert all(not effect_confirmed(expected, value) for value in mutations)


@pytest.mark.parametrize("field,value", [
    ("pid", 456), ("process_created", 456), ("core_instance", "restarted"),
    ("namespace", "b" * 64), ("save_binding", "other"), ("generation", 2),
    ("map_name", "other"), ("readiness", SaveReadiness.RECOVERING),
    ("readiness", SaveReadiness.UNKNOWN),
])
def test_grant_rejects_reload_map_change_restart_and_recovery(field, value):
    core = EvidenceCore()
    result = core.grant(b"ref")
    expected = EffectExpectation(b"ref", core.scope, CurrencyGrant(Currency.SENTINEL_BATTERY, 1, 4))
    assert not effect_confirmed(expected, replace(result, after_scope=replace(core.scope, **{field: value})))
    changed = replace(core.scope, **{field: value})
    assert not effect_confirmed(expected, replace(result, before_scope=changed, after_scope=changed))


@pytest.mark.parametrize("stage", range(6))
def test_each_suit_stage_requires_its_own_known_owned_bit(stage):
    core = EvidenceCore()
    expected = EffectExpectation(b"ref", core.scope, SuitStage(SuitPerkStage(stage)))
    bit = 1 << stage
    assert effect_confirmed(expected, core.progression(suit=PerkStages(bit, bit)))
    assert not effect_confirmed(expected, core.progression(suit=PerkStages(63, 63 ^ bit)))
    assert not effect_confirmed(expected, core.progression(suit=PerkStages(63 ^ bit, 63)))


def test_suit_plans_holes_including_pickup_radius(inventory):
    _, definitions = inventory
    policies = load_policy_registry(Path("data/item_replay_policies.json"), definitions)
    context = CONTEXT_BY_IDENTITY["base/hub"]
    scope = MaterializationScope("seed", 0, 1, "room", "lease", 1, "reload", "room")
    core = EvidenceCore(replace(EvidenceCore().scope, map_name="game/hub/hub"))
    observation = InventoryObservation("seed", 1, context.identity, context.campaign,
                                       progression=core.progression(suit=PerkStages(63, 0b110101)))
    plan = compare_inventory(InventoryOwnership((7770021,) * 6), definitions=definitions, policies=policies,
                             context=context, scope=scope, observation=observation)
    assert [c.stage for c in plan.reconciliation.commands] == [1, 3]
    assert plan.reconciliation.commands[1].description.endswith("increase_pickup_radius")
    assert not observation.confirms_repair(7770021, 3)
    assert observation.confirms_repair(7770021, 2)
    unknown = replace(observation, progression=core.progression(suit=PerkStages()))
    assert not compare_inventory(InventoryOwnership((7770021,) * 6), definitions=definitions, policies=policies,
                                 context=context, scope=scope, observation=unknown).reconciliation.commands


@pytest.mark.parametrize("quantity", [None, 0, -1, 1, 2])
def test_key_quantity_only_proves_the_matching_map(quantity):
    core = EvidenceCore()
    observation = core.progression(slayer_key_quantity=quantity)
    expected = EffectExpectation(b"ref", core.scope, SlayerKey(core.scope.map_name))
    assert effect_confirmed(expected, observation) is (quantity is not None and quantity > 0)
    assert not effect_confirmed(replace(expected, postcondition=SlayerKey("other")), observation)


def test_consumed_key_is_not_regranted_on_reload_but_explicit_new_save_can_restore_it(inventory):
    _, definitions = inventory
    policies = load_policy_registry(Path("data/item_replay_policies.json"), definitions)
    context = next(c for c in CONTEXT_BY_IDENTITY.values() if "e1m2_war" in c.map_keys)
    scope = MaterializationScope("seed", 0, 1, "room", "lease", 1, "reload", "room")
    observation = InventoryObservation("seed", 1, context.identity, context.campaign,
                                       {7770150: ItemObservation(7770150, "missing")})
    kwargs = dict(definitions=definitions, policies=policies, context=context, scope=scope, observation=observation)
    ownership = InventoryOwnership((7770150,))
    assert len(compare_inventory(ownership, **kwargs).reconciliation.commands) == 1
    kwargs["obligations"] = (InventoryObligation(7770150, 0, CommandState.CONFIRMED),)
    assert not compare_inventory(ownership, **kwargs).reconciliation.commands
    assert len(compare_inventory(ownership, restore_persistent_only=True, **kwargs).reconciliation.commands) == 1


@pytest.mark.parametrize("item_id", MASTERY_ITEM_BITS)
def test_mastery_is_unproven_until_its_selected_host_component_is_observed(item_id):
    core = EvidenceCore()
    expected = EffectExpectation(b"ref", core.scope, MasteryEffect(item_id, "weapon-host", "selected-mod"))
    slot = MasterySlot("weapon-host", "selected-mod", 10, 10, selected=True)
    assert not effect_confirmed(expected, core.progression(masteries={item_id: slot}))
    slot = replace(slot, proof=EffectProof.PRESENT)
    assert effect_confirmed(expected, core.progression(masteries={item_id: slot}))
    for changed in (replace(slot, selected=False), replace(slot, host="other"), replace(slot, mod="other"),
                    replace(slot, component_after=11)):
        assert not effect_confirmed(expected, core.progression(masteries={item_id: changed}))
    assert not effect_confirmed(expected, core.progression())


def test_meat_hook_proves_only_perk_ownership():
    core = EvidenceCore()
    expected = EffectExpectation(b"ref", core.scope, MeatHookPerk())
    assert effect_confirmed(expected, core.progression(meat_hook=EffectProof.PRESENT))
    assert not effect_confirmed(expected, core.progression())


def test_mission_requires_usable_menu_then_matching_destination_ready():
    core = EvidenceCore()
    access = MissionAccess(7, 3, "destination")
    expected = EffectExpectation(b"ref", core.scope, access)
    observation = MissionAccessObservation(core.scope, 7, 3, "destination")
    assert not effect_confirmed(expected, observation)
    observation = replace(observation, entry_usable=True)
    assert not effect_confirmed(expected, observation)
    destination = replace(core.scope, generation=core.scope.generation + 1, map_name="destination")
    observation = replace(observation, destination_scope=destination, destination_revision=7,
                          destination_stage_id=3, destination_ready=True)
    assert effect_confirmed(expected, observation)
    for changed in (replace(observation, entry_usable=False), replace(observation, destination_ready=False),
                    replace(observation, destination_revision=6), replace(observation, destination_stage_id=2),
                    replace(observation, destination_scope=replace(destination, map_name="other")),
                    replace(observation, destination_scope=replace(destination, generation=core.scope.generation)),
                    replace(observation, destination_scope=replace(destination, save_binding="other")),
                    replace(observation, destination_scope=replace(destination, readiness=SaveReadiness.RECOVERING))):
        assert not effect_confirmed(expected, changed)


@pytest.mark.parametrize("recovering", ["initial", "special", "typed", "transition"])
def test_collection_during_save_recovery_or_transition_has_no_baseline(recovering):
    scope = EvidenceScope(123, 123, "core", "a" * 64, "save", 1, "game/hub/hub", SaveReadiness.READY)
    core = EvidenceCore(scope)
    result = dict(weapons_after=0, ice_bomb_after=255,
                  _observation_scope=("123", "core", "build", "1"), _observation_map=scope.map_name,
                  _observation_save_binding="save", _save_recovering=recovering == "initial")
    special = {**result, "native_state_known": 0, "_save_recovering": recovering == "special"}
    link = SimpleNamespace(pid=123, namespace="a" * 64, observe_inventory=lambda: result,
                           observe_special_inventory=lambda: special)
    port = SentinelInventoryObservation(SimpleNamespace(state_key="room"), logging.getLogger(__name__),
                                        lambda *args, **kwargs: None)
    progression = core.progression()
    if recovering == "typed":
        progression = replace(progression, before_scope=replace(scope, readiness=SaveReadiness.RECOVERING),
                              after_scope=replace(scope, readiness=SaveReadiness.RECOVERING))
    if recovering == "transition":
        progression = replace(progression, after_scope=replace(scope, generation=2))
    assert port.observe_inventory(room_seed_name="seed", epoch=1, context_identity="base/hub", campaign="Base",
                                  link=link, progression=progression) is None


def test_zero_currency_observation_does_not_recreate_a_completed_acquisition(inventory):
    compare, _ = inventory
    core = EvidenceCore()
    currencies = CurrencyObservation(core.scope, core.scope, {Currency.SENTINEL_BATTERY: CurrencyValue(0, 4)})
    assert currencies.values[Currency.SENTINEL_BATTERY].balance == 0
    ownership = ownership_from_ledger((NetworkReceipt(7770016, 10, 1, 0),))
    effect = InventoryObligation(7770016, 0, CommandState.CONFIRMED, source_key="ap:0")
    assert not compare((), ownership=ownership, obligations=(effect,)).acquisitions


def test_collection_preserves_known_zero_and_the_six_suit_bits():
    scope = EvidenceScope(123, 123, "core", "a" * 64, "save", 1, "game/hub/hub", SaveReadiness.READY)
    core = EvidenceCore(scope)
    result = dict(weapons_after=0, ice_bomb_after=255,
                  _observation_scope=("123", "core", "build", "1"), _observation_map=scope.map_name,
                  _observation_save_binding="save")
    link = SimpleNamespace(pid=123, namespace="a" * 64, observe_inventory=lambda: result)
    port = SentinelInventoryObservation(SimpleNamespace(state_key="room"), logging.getLogger(__name__),
                                        lambda *args, **kwargs: None)
    currencies = CurrencyObservation(scope, scope, {Currency.SENTINEL_BATTERY: CurrencyValue(0, 9)})
    kwargs = dict(room_seed_name="seed", epoch=1, context_identity="base/hub", campaign="Base", link=link,
                  progression=core.progression(suit=PerkStages(63, 63)), currencies=currencies)
    observation = port.observe_inventory(**kwargs)
    assert observation.currencies.values[Currency.SENTINEL_BATTERY] == CurrencyValue(0, 9)
    assert observation.confirms_repair(7770021, 3)
    changed = replace(scope, save_binding="other")
    kwargs["currencies"] = replace(currencies, before_scope=changed, after_scope=changed)
    assert port.observe_inventory(**kwargs) is None


@pytest.mark.parametrize("applied", [1, 2])
def test_coordinator_persists_partial_or_full_bundle_proof_before_ack(owner, applied):
    async def check(delivery, transport):
        await delivery.plan((command(delivery.context),))
        prepared = await delivery.prepare("grant", b"a" * 64)
        context = delivery.context
        scope = EvidenceScope(context.pid, int(context.process_created), context.native_instance, "a" * 64,
                              context.save_binding_id, context.native_generation, context.map_name, SaveReadiness.READY)
        core = EvidenceCore(scope)
        expected = EffectExpectation(prepared.reference, scope, CurrencyGrant(Currency.SENTINEL_BATTERY, 2, 4))
        evidence = core.grant(prepared.reference, amount=applied)
        await delivery.send("grant")
        await delivery.recover("grant", expectation=expected, evidence=evidence)
        stored = await delivery.ledger.command(delivery.binding.room, "grant")
        assert stored.state is (CommandState.CONFIRMED if applied == 2 else CommandState.EFFECT_UNKNOWN)
        proof = json.loads(stored.result)
        assert proof["evidence"]["after"] == {"balance": applied, "gained": 4 + applied}
        assert proof["expectation"]["reference"] == prepared.reference.hex()
        assert "ack" not in transport.calls
        await delivery.ack("grant")
        assert transport.calls[-1] == "ack"
        await delivery.send("grant")
        assert len(transport.effects) == 1
    owner(check)


def test_coordinator_accepts_later_persistent_observation_and_refuses_another_attempt(owner):
    async def check(delivery, transport):
        transport.effect = 2
        await delivery.plan((command(delivery.context),))
        prepared = await delivery.prepare("grant", b"a" * 64)
        ctx = delivery.context
        scope = EvidenceScope(ctx.pid, int(ctx.process_created), ctx.native_instance, "a" * 64,
                              ctx.save_binding_id, ctx.native_generation, ctx.map_name, SaveReadiness.READY)
        expected = EffectExpectation(prepared.reference, scope, MeatHookPerk())
        await delivery.send("grant")
        await delivery.recover("grant")
        assert (await delivery.ledger.command(delivery.binding.room, "grant")).state is CommandState.EFFECT_UNKNOWN
        proof = EvidenceCore(scope).progression(meat_hook=EffectProof.PRESENT)
        with pytest.raises(Conflict, match="prepared"):
            await delivery.recover("grant", expectation=replace(expected, reference=b"other"), evidence=proof)
        await delivery.recover("grant", expectation=expected, evidence=proof)
        assert (await delivery.ledger.command(delivery.binding.room, "grant")).state is CommandState.CONFIRMED
    owner(check)


def test_observed_without_family_readback_stays_uncertain(owner):
    async def check(delivery, transport):
        await delivery.plan((command(delivery.context),))
        prepared = await delivery.prepare("grant", b"a" * 64)
        await delivery.send("grant")
        transport.commands[prepared.reference] = replace(transport.commands[prepared.reference], result={"readback": True})
        await delivery.recover("grant")
        assert (await delivery.ledger.command(delivery.binding.room, "grant")).state is CommandState.EFFECT_UNKNOWN
        assert len(transport.effects) == 1
    owner(check)


def test_inventory_confirmation_does_not_require_unrequested_equipment(owner):
    async def check(delivery, transport):
        await delivery.plan((command(delivery.context),))
        prepared = await delivery.prepare("grant", b"a" * 64)
        await delivery.send("grant")
        transport.commands[prepared.reference] = replace(transport.commands[prepared.reference],
            result={"weapons_after": 1, "equipment_after": 0xffffffff})
        await delivery.recover("grant")
        assert (await delivery.ledger.command(delivery.binding.room, "grant")).state is CommandState.CONFIRMED
    owner(check)


def test_retained_result_proves_original_key_after_map_change(owner):
    async def check(delivery, transport):
        await delivery.plan((command(delivery.context),))
        prepared = await delivery.prepare("grant", b"a" * 64)
        ctx = delivery.context
        scope = EvidenceScope(ctx.pid, int(ctx.process_created), ctx.native_instance, "a" * 64,
                              ctx.save_binding_id, ctx.native_generation, ctx.map_name, SaveReadiness.READY)
        expected = EffectExpectation(prepared.reference, scope, SlayerKey(ctx.map_name))
        await delivery.send("grant")
        delivery.context = replace(ctx, context_id="new-map", map_name="other", native_generation=2)
        await delivery.ledger.context(delivery.binding, delivery.context, ())
        evidence = EvidenceCore(scope).progression(slayer_key_quantity=1)
        await delivery.recover("grant", expectation=expected, evidence=evidence)
        assert (await delivery.ledger.command(delivery.binding.room, "grant")).state is CommandState.CONFIRMED
        assert len(transport.effects) == 1
    owner(check)


def test_shared_wire_results_round_trip_and_keep_proof_limits():
    for item in currency_payloads():
        data = bytes.fromhex(item["hex"])
        tag, value = codec.decode_variant(53, 1, data)
        assert codec.encode_variant(53, 1, tag, value) == data
    bundle = next(item for item in currency_payloads() if item["name"] == "battery_bundle")
    assert codec.encode_variant(53, 1, 1, dict(currency=4, amount=2, expected_gained=7)) == bytes.fromhex(bundle["hex"])
    for item in evidence_fixtures().values():
        if item["type"] in codec.TYPES:
            data = bytes.fromhex(item["hex"])
            assert len(data) == item["size"]
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
            assert codec.encode(item["type"], codec.decode(item["type"], data)) == data
    progression = progression_observation(evidence_result("progression_result"))
    assert progression.suit == PerkStages(63, 45)
    assert progression.slayer_key_quantity == 2
    assert progression.meat_hook is EffectProof.PRESENT
    assert not progression.masteries
    mission = mission_access_observation(evidence_result("mission_access_result"))
    assert effect_confirmed(EffectExpectation(b"ref", mission.menu_scope,
                            MissionAccess(mission.revision, mission.stage_id, mission.map_name)), mission)


@pytest.mark.parametrize("flags", [0, 1, 2, 3, 5, 6, 7])
def test_currency_validity_is_not_a_zero_balance(flags):
    result = evidence_result("currency_result")
    result.update(flags=flags, balance_before=0, gained_before=0, balance_after=2, gained_after=2)
    proof = currency_grant_result(result, b"ref")
    assert proof.before.balance == (0 if flags & 1 else None)
    assert proof.after.balance == (2 if flags & 2 else None)
    assert currency_observation(result).values[Currency.SENTINEL_BATTERY] == proof.after
    expected = EffectExpectation(b"ref", proof.before_scope, CurrencyGrant(Currency.SENTINEL_BATTERY, 2, 0))
    assert effect_confirmed(expected, proof) == (flags == 7)
    assert not effect_confirmed(expected, replace(proof, outcome=CurrencyOutcome.UNKNOWN))


def wire_for_scope(result, scope):
    from uuid import UUID

    for field in ("before_scope", "after_scope", "menu_scope"):
        if field in result:
            result[field] = dict(process=dict(pid=scope.pid, process_created=scope.process_created,
                                             instance_id=UUID(scope.core_instance).bytes,
                                             lifecycle_generation=scope.generation),
                                 namespace_id=scope.namespace.encode() + b"\0",
                                 save_binding=UUID(scope.save_binding).bytes,
                                 map_name=scope.map_name.encode().ljust(192, b"\0"), readiness=1)
    return result


@pytest.mark.parametrize("amount", [1, 2])
@pytest.mark.parametrize("capability", [False, True])
def test_battery_selects_one_route_and_persists_wire_proof_before_ack(owner, amount, capability):
    async def check(delivery, transport):
        scope = delivery._evidence_scope(delivery.context, b"a" * 64)
        baseline = wire_for_scope(evidence_result("currency_result"), scope)
        baseline.update(balance_after=0, gained_after=7)
        reads = []

        async def observe(*args, **kwargs):
            reads.append((args, kwargs))
            return baseline

        transport.supports = lambda command: capability and command == 53
        transport.observe = observe
        fallback = replace(command(delivery.context), kind="command", payload=codec.encode_variant(46, 1, 1,
                           {"entity": "ap_rpc_v3_7770016"}))
        await delivery.plan_battery(fallback, amount, b"a" * 64)
        plan = (await delivery.ledger.command(delivery.binding.room, "grant")).plan
        assert plan.kind == ("currencies" if capability else "command")
        if capability:
            assert codec.decode_variant(53, 1, plan.payload) == (1, dict(currency=4, amount=amount, expected_gained=7))
        prepared = await delivery.prepare("grant", b"a" * 64)
        transport.supports = lambda _: False
        await delivery.plan_battery(fallback, amount, b"a" * 64)
        assert len(reads) == int(capability)
        if capability:
            with pytest.raises(Conflict, match="amount"):
                await delivery.plan_battery(fallback, 3 - amount, b"a" * 64)
        await delivery.send("grant")
        result = wire_for_scope(evidence_result("currency_result"), scope)
        result.update(amount=amount, expected_gained=7, balance_before=0, gained_before=7,
                      balance_after=amount, gained_after=7 + amount)
        transport.commands[prepared.reference] = replace(transport.commands[prepared.reference], result=result)
        await delivery.recover("grant")
        row = await delivery.ledger.command(delivery.binding.room, "grant")
        assert row.state is (CommandState.CONFIRMED if capability else CommandState.EFFECT_UNKNOWN)
        if capability:
            assert json.loads(row.result)["expectation"]["reference"] == prepared.reference.hex()
        assert "ack" not in transport.calls
        await delivery.ack("grant")
        assert len(transport.effects) == 1
    owner(check)


@pytest.mark.parametrize("family", ["suit", "key", "meat_hook", "mastery", "mission"])
def test_coordinator_collects_shared_progression_and_mission_evidence(owner, family):
    async def check(delivery, transport):
        await delivery.plan((command(delivery.context),))
        prepared = await delivery.prepare("grant", b"a" * 64)
        scope = delivery._evidence_scope(delivery.context, b"a" * 64)
        mission = wire_for_scope(evidence_result("mission_access_result"), scope)
        mission["destination_scope"] = {**mission["menu_scope"], "process": {
            **mission["menu_scope"]["process"], "lifecycle_generation": scope.generation + 1},
            "map_name": mission["map_name"]}
        posts = dict(suit=SuitStage(0), key=SlayerKey(scope.map_name), meat_hook=MeatHookPerk(),
                     mastery=MasteryEffect(7770084, "ssg", "meat_hook"),
                     mission=MissionAccess(mission["revision"], mission["stage_id"],
                                           bytes(mission["map_name"]).split(b"\0")[0].decode()))

        async def observe(command_type, namespace, binding, **payload):
            assert namespace == b"a" * 64
            if family == "mission":
                assert command_type == 55 and payload == dict(stage_id=mission["stage_id"])
                return mission
            assert command_type == 54
            return wire_for_scope(evidence_result("progression_result"), scope)

        transport.supports = lambda _: True
        transport.observe = observe
        await delivery.send("grant")
        transport.effect = 2
        transport.commands[prepared.reference] = replace(transport.commands[prepared.reference], result=None)
        await delivery.recover("grant", expectation=EffectExpectation(prepared.reference, scope, posts[family]))
        row = await delivery.ledger.command(delivery.binding.room, "grant")
        assert row.state is (CommandState.EFFECT_UNKNOWN if family == "mastery" else CommandState.CONFIRMED)
        assert "ack" not in transport.calls
    owner(check)


@pytest.mark.parametrize("changed", [None, "generation", "room", "invalid", "legacy"])
def test_async_collection_keeps_wire_values_and_rejects_crossed_snapshots(changed):
    async def check():
        progression = evidence_result("progression_result")
        scope = progression_observation(progression).after_scope
        context = SimpleNamespace(state_key="room")
        result = dict(weapons_after=0, ice_bomb_after=255,
                      _observation_scope=(str(scope.process_created), scope.core_instance, "build", str(scope.generation)),
                      _observation_map=scope.map_name, _observation_save_binding=scope.save_binding)
        link = SimpleNamespace(pid=scope.pid, namespace=scope.namespace, observe_inventory=lambda: result)
        reads = []

        async def observe(command_type, namespace, binding, **payload):
            reads.append(command_type)
            if changed == "legacy":
                return None
            if command_type == 54:
                return progression
            currency = evidence_result("currency_result")
            currency.update(currency=payload["currency"], balance_after=0)
            if changed == "generation":
                currency["after_scope"]["process"]["lifecycle_generation"] += 1
            if changed == "room":
                context.state_key = "another-room"
            if changed == "invalid":
                currency["flags"] = 0
            return currency

        port = SentinelInventoryObservation(context, logging.getLogger(__name__), lambda *args, **kwargs: None)
        observation = await port.observe_native(SimpleNamespace(observe=observe), scope.namespace.encode(), bytes(16),
                                               room_seed_name="seed", epoch=1, context_identity="base/hub",
                                               campaign="Base", link=link)
        if changed in ("generation", "room"):
            assert observation is None
            return
        if changed == "legacy":
            assert observation.progression is None and observation.currencies is None
            return
        assert observation.progression.suit == PerkStages(63, 45)
        assert observation.get_state(7770083) == "owned"
        assert set(observation.currencies.values) == set(Currency)
        assert observation.currencies.values[Currency.SENTINEL_BATTERY].balance == (None if changed == "invalid" else 0)
        assert reads == [54, 53, 53, 53, 53]
    asyncio.run(check())
