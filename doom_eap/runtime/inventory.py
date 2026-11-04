"""Compare AP ownership with observed inventory; persistence and execution belong to adapters."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from doom_eap.contracts.command_publication import stable_command_key
from doom_eap.contracts.foundation import (
    MASTERY_ITEM_BITS,
    DeliveryCommand,
    classify_item_definition,
    compile_item_delivery_plan,
)
from doom_eap.contracts.inventory_domain import (
    MAX_CAPACITY_TIER,
    UNKNOWN,
    Currency,
    CurrencyGrant,
    CurrencyGrantResult,
    CurrencyOutcome,
    EffectExpectation,
    EffectProof,
    EvidenceScope,
    InventoryEffect,
    InventoryObservation,
    InventoryReadback,
    MasteryEffect,
    MeatHookPerk,
    MissionAccess,
    MissionAccessObservation,
    NativeEffectEvidence,
    ProgressionObservation,
    SaveReadiness,
    SlayerKey,
    SuitStage,
)
from doom_eap.contracts.materialization import (
    GATE_KEY_TO_MAP,
    SUPPORT_RUNE_IDS,
    TAG_SPECIAL_CAPABILITY,
    MaterializationScope,
    context_item_ids,
    support_rune_commands,
)
from doom_eap.contracts.ownership import CompletionUpgrade, StartingMaterializationFact
from doom_eap.contracts.runtime_context import RuntimeContext
from doom_eap.runtime import item_reconciliation
from doom_eap.runtime.item_reconciliation import (
    NEVER_REPLAY,
    REPLAY_MANUAL_ONLY,
    SPECIAL_PROGRESSIVE,
    ReconciliationCommand,
    ReconciliationPlan,
    ReconciliationSelection,
    ReplayPolicy,
)
from doom_eap.runtime.ledger_records import CommandState, NetworkReceipt, Resolution
from doom_eap.runtime.materialization import MaterializationPlan, MaterializationPlanError
from doom_eap.runtime.rune_reconciliation import RuneNativeState, RunePlanEntry, RuneReconciliationPlan


@dataclass(frozen=True)
class InventoryOwnership:
    reconciliation_item_ids: tuple[int, ...]
    blood_punch_upgrades: tuple[CompletionUpgrade, ...] = ()
    acquisitions: tuple[InventoryAcquisition, ...] = ()


@dataclass(frozen=True)
class InventoryAcquisition:
    source_key: str
    item_id: int
    live: bool = False


def ownership_from_ledger(
    ingested_prefix: Iterable[NetworkReceipt],
    local_sources: Iterable[StartingMaterializationFact] = (),
    *, blood_punch_upgrades: tuple[CompletionUpgrade, ...] = (), live_receipt_indices=frozenset(),
) -> InventoryOwnership:
    """Project a complete ingested prefix and decoded local facts without synthetic receipts."""
    acquisitions = tuple(InventoryAcquisition(f"ap:{index}", receipt.item, index in live_receipt_indices)
                         for index, receipt in enumerate(ingested_prefix))
    acquisitions += tuple(InventoryAcquisition(f"local:{fact.provenance}:{fact.item_id}:{ordinal}", fact.item_id)
                          for fact in local_sources for ordinal in range(fact.quantity))
    return InventoryOwnership(
        tuple(acquisition.item_id for acquisition in acquisitions), blood_punch_upgrades, acquisitions,
    )


@dataclass(frozen=True)
class InventoryObligation:
    item_id: int
    stage: int
    state: CommandState
    resolution: Resolution | None = None
    source_key: str | None = None


@dataclass(frozen=True)
class AcquisitionEffect:
    source_key: str
    item_id: int
    commands: tuple[DeliveryCommand, ...]
    transient: bool = False


@dataclass(frozen=True)
class InventoryDependency:
    item_id: int
    stage: int
    prerequisite_item_id: int
    prerequisite_stage: int = 0


@dataclass(frozen=True)
class InventoryPlan:
    materialization: MaterializationPlan | None = None
    reconciliation: ReconciliationPlan | None = None
    runes: RuneReconciliationPlan | None = None
    weapon_point_grants: tuple[int, ...] = ()
    unresolved_item_ids: tuple[int, ...] = ()
    weapon_points_error: WeaponPointsBlocked | None = None
    dependencies: tuple[InventoryDependency, ...] = ()
    native_effects: tuple[NativeInventoryEffect, ...] = ()
    acquisitions: tuple[AcquisitionEffect, ...] = ()


class NativeEffectKind(Enum):
    MASTERY = "mastery"
    BLOOD_PUNCH = "blood_punch"


@dataclass(frozen=True)
class NativeInventoryEffect:
    item_id: int
    kind: NativeEffectKind
    stage: int
    mask: int


class WeaponPointsBlocked(RuntimeError):
    pass


def coherent_evidence(before: EvidenceScope, after: EvidenceScope) -> bool:
    return before == after and before.readiness is SaveReadiness.READY


def effect_confirmed(expected: EffectExpectation, evidence: NativeEffectEvidence) -> bool:
    match expected.postcondition, evidence:
        case InventoryEffect(item_id, stage), InventoryReadback(before, after, observation):
            return (before == expected.scope and coherent_evidence(before, after)
                    and observation.confirms_repair(item_id, stage))
        case CurrencyGrant() as grant, CurrencyGrantResult() as result:
            return _currency_confirmed(expected, grant, result)
        case MissionAccess() as access, MissionAccessObservation() as observation:
            return _mission_confirmed(expected.scope, access, observation)
        case _, ProgressionObservation() as observation:
            if not coherent_evidence(observation.before_scope, observation.after_scope):
                return False
            return observation.after_scope == expected.scope and _progression_confirmed(
                expected.postcondition, observation)
    return False


def _currency_confirmed(expected, grant, result):
    if not coherent_evidence(result.before_scope, result.after_scope):
        return False
    if result.reference != expected.reference or result.before_scope != expected.scope:
        return False
    if grant.currency is not Currency.SENTINEL_BATTERY or grant.amount not in (1, 2):
        return False
    if (result.currency, result.writer_entered, result.native_exception, result.outcome) != (
            grant.currency, True, 0, CurrencyOutcome.GRANTED):
        return False
    return result.before.gained == grant.expected_gained and currency_delta(result) == (grant.amount, grant.amount)


def currency_delta(result: CurrencyGrantResult) -> tuple[int, int] | None:
    before, after = result.before, result.after
    if before.balance is None or before.gained is None or after.balance is None or after.gained is None:
        return None
    if any(not 0 <= value <= 0x7fffffff for value in (before.balance, before.gained, after.balance, after.gained)):
        return None
    return after.balance - before.balance, after.gained - before.gained


def _progression_confirmed(postcondition, observation):
    match postcondition:
        case SuitStage(stage):
            return 0 <= stage < 6 and bool(observation.suit.known & observation.suit.owned & (1 << stage))
        case SlayerKey(map_name):
            quantity = observation.slayer_key_quantity
            return observation.after_scope.map_name == map_name and quantity is not None and quantity > 0
        case MeatHookPerk():
            return observation.meat_hook is EffectProof.PRESENT
        case MasteryEffect(item_id, host, mod):
            return _mastery_confirmed(observation, item_id, host, mod)
    return False


def _mastery_confirmed(observation, item_id, host, mod):
    slot = observation.masteries.get(item_id)
    return bool(slot and slot.host == host and slot.mod == mod and _slot_proof(slot) is EffectProof.PRESENT)


def _mission_confirmed(scope, access, observation):
    if scope.readiness is not SaveReadiness.READY or observation.menu_scope != scope:
        return False
    if (observation.revision, observation.stage_id, observation.map_name) != (
            access.revision, access.stage_id, access.map_name):
        return False
    destination = observation.destination_scope
    if not observation.entry_usable or not observation.destination_ready or destination is None:
        return False
    return _destination_matches(scope, access, observation)


def _destination_matches(scope, access, observation):
    destination = observation.destination_scope
    identity = ("pid", "process_created", "core_instance", "namespace", "save_binding")
    if any(getattr(scope, field) != getattr(destination, field) for field in identity):
        return False
    return (destination.readiness is SaveReadiness.READY and destination.generation > scope.generation
            and destination.map_name == access.map_name and observation.destination_revision == access.revision
            and observation.destination_stage_id == access.stage_id)


def compare_inventory(
    ownership: InventoryOwnership,
    *, definitions: Mapping[int, Any] | None = None, policies: Mapping[int, ReplayPolicy] | None = None,
    observation: InventoryObservation | None = None, context: RuntimeContext | None = None,
    scope: MaterializationScope | None = None, special_mode: str = "the_crucible",
    selected_item_ids: frozenset[int] | None = None, replay_identity: tuple[str, int] | None = None,
    include_manual_replay: bool = False, runes: RuneNativeState | None = None,
    rune_mapping: Mapping[int, str] | None = None, expected_rune_item_ids: Iterable[int] | None = None,
    weapon_points: tuple[int, int | None] | None = None, obligations: tuple[InventoryObligation, ...] = (),
    restore_persistent_only: bool = False,
) -> InventoryPlan:
    materialization, reconciliation, unresolved = _compare_persistent(
        ownership, definitions, policies, observation, context, scope, special_mode,
        selected_item_ids, replay_identity, include_manual_replay,
    )
    rune_plan = _compare_runes(ownership, selected_item_ids, runes, rune_mapping, expected_rune_item_ids, obligations)
    persistent_obligations = _persistent_obligations(obligations, restore_persistent_only)
    materialization, reconciliation = _merge_plans(materialization, reconciliation, rune_plan, persistent_obligations)
    points = None if restore_persistent_only else weapon_points
    grants, points_error = _compare_weapon_points(points, obligations)
    native_effects = _pending_native_effects(ownership, observation, selected_item_ids, obligations)
    dependencies = _dependencies(reconciliation, native_effects)
    unresolved = _all_unresolved(ownership, observation, selected_item_ids, rune_plan, unresolved)
    acquisitions = _compare_acquisitions(ownership, context, selected_item_ids, restore_persistent_only,
                                         definitions, policies, obligations)
    return InventoryPlan(materialization, reconciliation, rune_plan, grants, unresolved,
                          points_error, dependencies, native_effects, acquisitions)


def _compare_persistent(ownership, definitions, policies, observation, context, scope,
                        special_mode, selection, replay_identity, include_manual_replay):
    if context is not None:
        materialization = _materialize(ownership, context, scope, definitions, policies, special_mode,
                                       observation=observation, selected_item_ids=selection)
        unresolved = _unresolved_items(ownership, context, scope, policies, special_mode, selection, observation)
        return materialization, materialization.reconciliation, unresolved
    if replay_identity is None:
        return None, None, ()
    reconciliation = _replay(ownership.reconciliation_item_ids, definitions, policies, *replay_identity,
                              include_manual_replay=include_manual_replay)
    return None, reconciliation, ()


def _compare_runes(ownership, selection, native, mapping, expected, obligations):
    if native is None:
        return None
    ids = ownership.reconciliation_item_ids
    if selection is not None:
        ids = tuple(i for i in ids if i in selection)
    plan = _runes(ids, native, mapping, expected_rune_item_ids=expected)
    return _pending_runes(plan, obligations)


def _merge_plans(materialization, reconciliation, rune_plan, obligations):
    if reconciliation is None:
        return materialization, reconciliation
    reconciliation = _pending_effects(reconciliation, obligations)
    if rune_plan is not None:
        rune_ids = {entry.item_id for entry in rune_plan.entries}
        reconciliation = replace(reconciliation, commands=tuple(c for c in reconciliation.commands
                                                                 if c.item_id not in rune_ids))
    if materialization is not None:
        materialization = replace(materialization, reconciliation=reconciliation)
    return materialization, reconciliation


def _pending_native_effects(ownership, observation, selection, obligations):
    blocked = _blocked_effects(obligations)
    effects = _native_effects(ownership, observation, selection) + _acquired_masteries(ownership, selection, obligations)
    return tuple({(effect.item_id, effect.stage): effect for effect in effects
                  if (effect.item_id, effect.stage) not in blocked}.values())


def _acquired_masteries(ownership, selection, obligations):
    recorded = {effect.item_id for effect in obligations}
    ids = {a.item_id for a in ownership.acquisitions} & MASTERY_ITEM_BITS.keys() - recorded
    if selection is not None:
        ids &= selection
    return tuple(NativeInventoryEffect(i, NativeEffectKind.MASTERY, 0, MASTERY_ITEM_BITS[i]) for i in sorted(ids))


def _compare_acquisitions(ownership, context, selection, restore_persistent_only, definitions, policies, obligations):
    if context is None or selection is not None or restore_persistent_only:
        return ()
    return _acquisitions(ownership.acquisitions, definitions, policies, obligations)


def _acquisitions(acquisitions, definitions, policies, obligations):
    blocked = {(effect.source_key, effect.stage) for effect in obligations
               if effect.resolution != Resolution.REPLACED and effect.state != CommandState.STALE}
    effects = []
    for acquisition in acquisitions:
        item_id = acquisition.item_id
        if policies[item_id].policy != NEVER_REPLAY:
            continue
        family = classify_item_definition(definitions[item_id])
        if not _acquisition_allowed(family, acquisition.live):
            continue
        effect = _acquisition_effect(acquisition, family, definitions, blocked)
        if effect is not None:
            effects.append(effect)
    return tuple(effects)


def _acquisition_effect(acquisition, family, definitions, blocked):
    if family == "transient_effect":
        if (acquisition.source_key, 0) in blocked:
            return None
        return AcquisitionEffect(acquisition.source_key, acquisition.item_id, (), transient=True)
    plan = compile_item_delivery_plan(acquisition.item_id, definitions)
    commands = tuple(command for command in plan.commands if (acquisition.source_key, command.index) not in blocked)
    return AcquisitionEffect(acquisition.source_key, acquisition.item_id, commands) if commands else None


def _acquisition_allowed(family, live):
    if family in {"native_weapon_upgrade_points", "no_op"}:
        return False
    return live or family not in {"transient_effect", "trap_spawn"}


def _unresolved_items(ownership, context, scope, policies, special_mode, selection, observation):
    selected = _selected_ids(ownership, context, scope, policies, _special_ids(special_mode), selection)
    return tuple(item_id for item_id in selected if observation.get_state(item_id) == UNKNOWN)


def _dependencies(plan, native_effects):
    commands = () if plan is None else plan.commands
    frag = tuple(InventoryDependency(command.item_id, command.stage, 7770011)
                 for command in commands if command.item_id in {7770111, 7770112, 7770113, 7770114})
    blood_punch = tuple(InventoryDependency(effect.item_id, effect.stage, 7770904)
                        for effect in native_effects if effect.kind == NativeEffectKind.BLOOD_PUNCH and effect.stage > 0)
    return frag + blood_punch


def _native_effects(ownership, observation, selection):
    if observation is None:
        return ()
    ids = set(ownership.reconciliation_item_ids)
    if selection is not None:
        ids &= selection
    effects = [NativeInventoryEffect(i, NativeEffectKind.MASTERY, 0, MASTERY_ITEM_BITS[i])
               for i in sorted(ids & MASTERY_ITEM_BITS.keys()) if _mastery_proof(observation, i) is EffectProof.ABSENT]
    effects.extend(_progressive_blood_punch(ownership, observation, ids))
    return tuple(effects)


def _all_unresolved(ownership, observation, selection, rune_plan, unresolved):
    ids = set(ownership.reconciliation_item_ids)
    if selection is not None:
        ids &= selection
    unknown = set(unresolved)
    if observation is not None:
        unknown.update(i for i in ids & MASTERY_ITEM_BITS.keys()
                       if _mastery_proof(observation, i) is EffectProof.UNPROVEN)
        if 7770904 in ids and 7770904 not in observation.stage_masks:
            unknown.add(7770904)
    if rune_plan is not None:
        unknown.update(entry.item_id for entry in rune_plan.entries if entry.disposition == "deferred")
    return tuple(sorted(unknown))


def _mastery_proof(observation, item_id):
    progression = observation.progression
    if progression is None or not coherent_evidence(progression.before_scope, progression.after_scope):
        return EffectProof.UNPROVEN
    slot = progression.masteries.get(item_id)
    return _slot_proof(slot) if slot else EffectProof.UNPROVEN


def _slot_proof(slot):
    if not slot.selected or slot.component_before != slot.component_after:
        return EffectProof.UNPROVEN
    return slot.proof


def _progressive_blood_punch(ownership, observation, ids):
    observed = observation.stage_masks.get(7770904)
    if 7770904 not in ids or observed is None:
        return ()
    count = min(4, ownership.reconciliation_item_ids.count(7770904))
    return tuple(NativeInventoryEffect(7770904, NativeEffectKind.BLOOD_PUNCH, stage, 1 << stage)
                 for stage in range(count) if not observed & (1 << stage))


def _pending_effects(plan, obligations):
    blocked = _blocked_effects(obligations)
    blocked.update((effect.item_id, effect.stage) for effect in obligations if effect.item_id in GATE_KEY_TO_MAP
                   and effect.state is CommandState.CONFIRMED)
    return replace(plan, commands=tuple(command for command in plan.commands
                                       if (command.item_id, command.stage) not in blocked))


def _persistent_obligations(obligations, restore_persistent_only):
    if not restore_persistent_only:
        return obligations
    return tuple(effect for effect in obligations if not (
        effect.item_id in GATE_KEY_TO_MAP and effect.state is CommandState.CONFIRMED))


def _blocked_effects(obligations):
    return {(effect.item_id, effect.stage) for effect in obligations
               if effect.state in {CommandState.PLANNED, CommandState.DEFERRED,
                                   CommandState.QUEUED, CommandState.DISPATCHED,
                                   CommandState.EFFECT_UNKNOWN, CommandState.REJECTED}
               and effect.resolution != Resolution.REPLACED}


def _pending_runes(plan, obligations):
    blocked = _blocked_effects(obligations)
    entries = tuple(replace(entry, disposition="deferred", reason="existing_obligation")
                    if (entry.item_id, 0) in blocked else entry for entry in plan.entries)
    return replace(plan, entries=entries, status=_rune_status(entries))


def _weapon_point_grants(total, gained):
    if gained is None:
        raise WeaponPointsBlocked("Native earned WUP observation is unknown")
    if gained < 0 or gained > total or gained % 3:
        raise WeaponPointsBlocked("Native earned WUP disagrees with receipt ownership")
    return tuple(range(gained, total, 3))


def _compare_weapon_points(points, obligations):
    if points is None:
        return (), None
    try:
        blocked = _blocked_effects(obligations)
        grants = _weapon_point_grants(*points)
        first_pending = min((stage for item_id, stage in blocked if item_id == 7770903), default=points[0] // 3)
        return tuple(gained for gained in grants if gained // 3 < first_pending), None
    except WeaponPointsBlocked as error:
        return (), error


def _special_ids(mode):
    selected = {
        "progressive_special_weapon": {7770901}, "Progressive Special Weapon": {7770901},
        "progressive_sentinel_hammer": {7770902}, "Progressive Sentinel Hammer": {7770902},
        "the_crucible": {7770007}, "The Crucible": {7770007},
    }.get(mode)
    if selected is None:
        raise MaterializationPlanError(f"unsupported special weapon option: {mode!r}", blocked=False)
    return selected


def _replay_allowed(item_id, policies, lease):
    if item_id in SUPPORT_RUNE_IDS | {7770007, 7770009, 7770901, 7770902}:
        return True
    if item_id == 7770021:
        return True
    if item_id in {7770017, 7770088, 7770092} and lease is not None:
        return True
    policy = policies.get(item_id)
    return policy is not None and policy.policy in {"replay_idempotent", "replay_manual_only"}


def _selected_ids(ownership, context, scope, policies, special_ids, selection):
    received = set(ownership.reconciliation_item_ids)
    candidates = context_item_ids(context, received)
    excluded = MASTERY_ITEM_BITS.keys() | ({7770007, 7770009, 7770901, 7770902} - special_ids)
    if 7770011 not in received:
        excluded |= {7770111, 7770112, 7770113, 7770114}
    candidates = set(candidates) - excluded
    if selection is not None:
        candidates &= selection
    return tuple(sorted(item_id for item_id in candidates
                        if _replay_allowed(item_id, policies, scope.materialization_lease)))


def _command_missing(command, observation, counts):
    item_id = command.item_id
    if item_id == 7770021:
        return _suit_stage_missing(observation.progression, command.stage)
    if item_id in {7770017, 7770088, 7770092}:
        if observation.get_state(item_id) == UNKNOWN:
            return False
        observed = observation.get_observed_stage(item_id) if observation.is_owned(item_id) else 0
        return observed <= command.stage < min(MAX_CAPACITY_TIER, counts[item_id])
    return observation.is_proven_missing(item_id) and (item_id != 7770006 or command.stage == 0)


def _suit_stage_missing(progression, stage):
    if progression is None or not coherent_evidence(progression.before_scope, progression.after_scope):
        return False
    bit = 1 << stage
    return bool(progression.suit.known & bit and not progression.suit.owned & bit)


def _persistent_replay(ownership, selected_ids, special_ids, definitions, policies, context, scope, observation):
    ids = tuple(item_id for item_id in ownership.reconciliation_item_ids
                if item_id in selected_ids and item_id not in SUPPORT_RUNE_IDS | special_ids)
    identity = stable_command_key("context", scope.room_seed_name, scope.team, scope.slot, context.identity)
    plan = _replay(ids, {i: definitions[i] for i in set(ids)}, {i: policies[i] for i in set(ids)},
                   identity, scope.evidence_epoch, include_manual_replay=True)
    counts = Counter(ownership.reconciliation_item_ids)
    return replace(plan, commands=tuple(c for c in plan.commands if _command_missing(c, observation, counts)))


def _special_candidate(item_id, count, definitions):
    if item_id in {7770007, 7770009}:
        return (1 if item_id == 7770007 else 2), item_id, compile_item_delivery_plan(item_id, definitions)
    stage = min(count, len(definitions[item_id].get("perks", []))) - 1
    return stage + 1, item_id, compile_item_delivery_plan(item_id, definitions, stage=stage)


def _hammer_commands(context, item_id, stage):
    has_hammer = item_id == 7770009 or (item_id == 7770901 and stage >= 2) or item_id == 7770902
    if not has_hammer or context.campaign not in {"TAG2", "Dark Lord"}:
        return ()
    if not context.supports(TAG_SPECIAL_CAPABILITY):
        return ()
    return tuple((key, f"ai_ScriptCmdEnt player1 givePlayerPerk perk/player/weapons/hammer/{key}")
                 for key in ("ammo_drops_upgraded", "armor_and_health_drops_upgraded", "debuff_duration_increase"))


def _special_command(item_id, policy, stage, scope, context, index, command, description):
    return ReconciliationCommand(
        item_id, policy.name, policy.policy, stage,
        stable_command_key("reconcile", scope.room_seed_name, scope.team, scope.slot,
                        context.identity, "special", stage, index), command, description,
    )


def _special_commands(ownership, selected_ids, special_ids, definitions, policies, context, scope, observation):
    counts = Counter(ownership.reconciliation_item_ids)
    candidates = [_special_candidate(i, counts[i], definitions) for i in selected_ids if i in special_ids]
    if not candidates:
        return (), 0, None
    stage, item_id, delivery = max(candidates, key=lambda value: (value[0], value[1]))
    policy = policies[item_id]
    commands = [_special_command(item_id, policy, stage, scope, context, c.index, c.command, delivery.description)
                for c in delivery.commands]
    existing = {c.command for c in commands}
    commands.extend(_special_command(item_id, policy, stage, scope, context, f"hammer-upgrade-{key}",
                                     command, f"Sentinel Hammer upgrade: {key}")
                    for key, command in _hammer_commands(context, item_id, stage) if command not in existing)
    if not _special_missing(observation, item_id, stage):
        commands = []
    diagnostic = (item_id, counts[item_id], stage, context.identity, len(commands))
    return tuple(commands), stage, diagnostic


def _special_missing(observation, item_id, stage):
    return observation.is_proven_missing(item_id) or (observation.is_owned(item_id)
                                                      and observation.get_observed_stage(item_id) < stage)


def _support_commands(received, context, scope, definitions, policies, observation):
    commands = []
    for item_id in support_rune_commands(received, context):
        if not observation.is_proven_missing(item_id):
            continue
        plan = compile_item_delivery_plan(item_id, {item_id: definitions[item_id]})
        if len(plan.commands) != 1:
            raise MaterializationPlanError(f"invalid support rune plan {item_id}")
        policy = policies[item_id]
        commands.append(ReconciliationCommand(
            item_id, policy.name, policy.policy, 0,
            stable_command_key("reconcile", scope.room_seed_name, scope.team, scope.slot,
                            context.identity, "support-rune", item_id),
            plan.commands[0].command, plan.description,
        ))
    return tuple(commands)


def _blood_punch_commands(ownership, context, scope, observation, selected_ids):
    if context.campaign == "Base" or 7770014 not in selected_ids:
        return ()
    upgrades = _missing_completion_upgrades(ownership.blood_punch_upgrades, observation)
    return tuple(ReconciliationCommand(
        7770014, "Blood Punch", "replay_idempotent", upgrade.location_id,
        stable_command_key("reconcile", scope.room_seed_name, scope.team, scope.slot,
                        context.identity, "blood-punch-upgrade", upgrade.location_id),
        f"ai_ScriptCmdEnt player1 givePlayerPerk {upgrade.perk_path}",
        f"Blood Punch upgrade from {upgrade.mission_name}",
    ) for upgrade in upgrades)


def _missing_completion_upgrades(upgrades, observation):
    mask = observation.stage_masks.get(7770904)
    if mask is None:
        return upgrades if observation.is_proven_missing(7770014) else ()
    bits = {7770162: 2, 7770290: 4, 7770411: 8}
    return tuple(upgrade for upgrade in upgrades if not mask & bits[upgrade.location_id])


def _materialize(ownership, context, scope, item_definitions, replay_policies, special_mode,
                 *, observation: InventoryObservation | None = None, selected_item_ids=None):
    if observation is None or observation.is_stale_for(
        room_seed_name=scope.room_seed_name, epoch=scope.evidence_epoch,
        context_identity=context.identity, campaign=context.campaign,
    ):
        raise MaterializationPlanError("fresh native observation required; UNKNOWN is not missing")
    special_ids = _special_ids(special_mode)
    selected_ids = _selected_ids(ownership, context, scope, replay_policies, special_ids, selected_item_ids)
    plan = _persistent_replay(ownership, selected_ids, special_ids, item_definitions,
                              replay_policies, context, scope, observation)
    special, stage, diagnostic = _special_commands(ownership, selected_ids, special_ids, item_definitions,
                                                  replay_policies, context, scope, observation)
    support = _support_commands(selected_ids, context, scope, item_definitions, replay_policies, observation)
    blood_punch = _blood_punch_commands(ownership, context, scope, observation, selected_ids)
    raw = plan.commands + special + support + blood_punch
    commands = {}
    for command in raw:
        commands.setdefault((command.item_id, command.stage, command.command), command)
    plan = replace(plan, commands=tuple(commands.values()), special_stages=int(bool(special)))
    return MaterializationPlan(scope, plan, len(raw), len(support), stage, diagnostic)


def _replay_commands(item_id, count, definition_map, policy, identity, epoch):
    if policy.policy == SPECIAL_PROGRESSIVE:
        stages = range(min(count, len(definition_map[item_id]["perks"])))
        deliveries = [(stage, _progressive_delivery(item_id, definition_map, stage)) for stage in stages]
    else:
        deliveries = [(None, item_reconciliation.compile_item_delivery_plan(item_id, definition_map))]
        if not deliveries[0][1].commands:
            raise ValueError(f"replay-safe item {item_id} compiled no commands")
    return tuple(_replay_command(item_id, policy, delivery.index if stage is None else stage,
                                 identity, epoch, delivery.command, plan.description)
                 for stage, plan in deliveries for delivery in plan.commands)


def _replay_command(item_id, policy, stage, identity, epoch, command, description):
    return ReconciliationCommand(item_id, policy.name, policy.policy, stage,
        f"reconcile-{identity}-e{epoch}-item{item_id}-stage{stage}", command, description)


def _progressive_delivery(item_id, definitions, stage):
    plan = item_reconciliation.compile_item_delivery_plan(item_id, definitions, stage=stage)
    if len(plan.commands) != 1:
        raise ValueError(f"progressive item {item_id} stage {stage} must compile one command")
    return plan


def _replay(received_item_ids, definitions, registry, slot_identity, epoch, *, include_manual_replay=False):
    commands, selections = [], []
    replayed = special_stages = skipped_never = skipped_manual = 0
    for item_id, count in sorted(Counter(received_item_ids).items()):
        if item_id == 7770904:
            continue
        policy = registry[item_id]
        selected = ()
        if policy.policy == NEVER_REPLAY:
            skipped_never += 1
        elif policy.policy == REPLAY_MANUAL_ONLY and not include_manual_replay:
            skipped_manual += 1
        else:
            selected = _replay_commands(item_id, count, definitions, policy, slot_identity, epoch)
            replayed += policy.policy != SPECIAL_PROGRESSIVE
            special_stages += len(selected) if policy.policy == SPECIAL_PROGRESSIVE else 0
        commands.extend(selected)
        selections.append(ReconciliationSelection(item_id, policy.name, policy.policy, count,
                                                   tuple(command.command for command in selected)))
    return ReconciliationPlan(tuple(commands), tuple(selections), replayed, special_stages,
                               skipped_never, skipped_manual_replay=skipped_manual)


def _rune_entry(item_id, perk, native):
    registered = perk in native.registered_runes
    available = perk in native.available_perks
    if registered and available:
        reason = "equipped_player_choice_preserved" if perk in native.equipped_slots else "registered_and_available"
        return RunePlanEntry(item_id, perk, "noop", reason)
    if not native.observed:
        return RunePlanEntry(item_id, perk, "deferred", "native_snapshot_unavailable")
    elif not registered and (available or perk in native.active_perks or perk in native.equipped_slots):
        reason = "manager_registration_missing"
    elif registered:
        reason = "perk_availability_missing"
    else:
        reason = "perk_and_manager_registration_missing"
    return RunePlanEntry(item_id, perk, "repair_candidate", reason)


def _runes(ap_owned_item_ids, native, mapping, *, expected_rune_item_ids=None):
    expected = set(mapping) if expected_rune_item_ids is None else set(expected_rune_item_ids)
    owned = set(ap_owned_item_ids) & expected
    entries = tuple(_rune_entry(item_id, mapping[item_id], native) for item_id in sorted(owned))
    payload = {
        "owned": sorted(owned), "available": sorted(native.available_perks),
        "active": sorted(native.active_perks), "registered": sorted(native.registered_runes),
        "slots": list(native.equipped_slots), "page": native.page_unlocked,
        "save_slot": native.save_slot, "evidence_epoch": native.evidence_epoch,
    }
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    status = _rune_status(entries)
    return RuneReconciliationPlan(entries, fingerprint, status)


def _rune_status(entries):
    if any(entry.disposition == "repair_candidate" for entry in entries):
        return "repair_needed"
    return "deferred" if any(entry.disposition == "deferred" for entry in entries) else "noop"
