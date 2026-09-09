"""Persistent inventory planning from supplied ownership, context and catalogs."""
from dataclasses import dataclass

from doom_eap.contracts.foundation import compile_item_delivery_plan
from doom_eap.contracts.command_publication import stable_spool_id
from doom_eap.contracts.materialization import (
    MaterializationScope, context_item_ids, support_rune_commands,
    SUPPORT_RUNE_IDS, TAG_SPECIAL_CAPABILITY,
)
from doom_eap.runtime.item_reconciliation import ReconciliationCommand, ReconciliationPlan, compile_reconciliation_plan


@dataclass(frozen=True)
class MaterializationPlan:
    scope: MaterializationScope
    reconciliation: ReconciliationPlan
    raw_operation_count: int
    support_rune_jobs: int
    selected_special_stage: int
    special_diagnostic: tuple | None


class MaterializationPlanError(ValueError):
    def __init__(self, message, *, blocked=True):
        super().__init__(message)
        self.blocked = blocked


def compile_materialization_plan(ownership, context, scope, item_definitions, replay_policies, special_mode):
    received = set(ownership.reconciliation_item_ids)
    materialization_lease = scope.materialization_lease
    received_counts = {}
    for item_id in ownership.reconciliation_item_ids:
        received_counts[item_id] = received_counts.get(item_id, 0) + 1
    materializable_ids = context_item_ids(context, received)
    allowed_replay_policies = {"replay_idempotent", "replay_manual_only"}
    selected_special_ids = {
        "progressive_special_weapon": {7770901},
        "Progressive Special Weapon": {7770901},
        "progressive_sentinel_hammer": {7770902},
        "Progressive Sentinel Hammer": {7770902},
        "the_crucible": {7770007},
        "The Crucible": {7770007},
    }.get(special_mode)
    if selected_special_ids is None:
        raise MaterializationPlanError(f"unsupported special weapon option: {special_mode!r}", blocked=False)
    all_special_ids = {7770007, 7770009, 7770901, 7770902}
    capacity_ids = {7770017, 7770088, 7770092}
    materializable_ids = tuple(
        item_id for item_id in materializable_ids
        if item_id not in all_special_ids or item_id in selected_special_ids
    )
    selected_ids = tuple(
        item_id for item_id in materializable_ids
        if item_id in SUPPORT_RUNE_IDS
        or (
            replay_policies.get(item_id) is not None
            and replay_policies[item_id].policy in allowed_replay_policies
        )
        or item_id in {7770007, 7770009, 7770901, 7770902}
        or (
            item_id in capacity_ids
            and materialization_lease is not None
        )
    )
    selected_ids = tuple(sorted(set(selected_ids)))
    selected_receipts = tuple(
        item_id for item_id in ownership.reconciliation_item_ids if item_id in selected_ids
    )
    special_ids = selected_special_ids
    plan_ids = tuple(
        item_id for item_id in selected_receipts
        if item_id not in SUPPORT_RUNE_IDS and item_id not in special_ids
    )
    definitions = {item_id: item_definitions[item_id] for item_id in set(plan_ids)}
    policies = {item_id: replay_policies[item_id] for item_id in set(plan_ids)}
    reconciliation_slot_identity = stable_spool_id(
        "context", scope.room_seed_name, scope.team, scope.slot, context.identity
    )
    plan = compile_reconciliation_plan(
        plan_ids, definitions, policies,
        reconciliation_slot_identity,
        scope.evidence_epoch,
        include_manual_replay=True,
    )
    # Special ownership is one physical state: materialize highest selected intent.
    special_candidates = []
    special_definitions = {
        item_id: item_definitions[item_id]
        for item_id in selected_ids
        if item_id in special_ids
    }
    special_policies = {
        item_id: replay_policies[item_id]
        for item_id in selected_ids
        if item_id in special_ids
    }
    for item_id in sorted(special_definitions):
        count = received_counts.get(item_id, 0)
        if count < 1:
            continue
        if item_id == 7770007:
            physical_stage = 1
            deliveries = compile_item_delivery_plan(item_id, special_definitions)
        elif item_id == 7770009:
            physical_stage = 2
            deliveries = compile_item_delivery_plan(item_id, special_definitions)
        else:
            perks = special_definitions[item_id].get("perks", [])
            stage = min(count, len(perks)) - 1
            physical_stage = stage + 1
            deliveries = compile_item_delivery_plan(
                item_id, special_definitions, stage=stage
            )
        special_candidates.append((physical_stage, item_id, deliveries))
    special_commands = []
    selected_special_stage = 0
    if special_candidates:
        physical_stage, item_id, deliveries = max(
            special_candidates, key=lambda candidate: (candidate[0], candidate[1])
        )
        selected_special_stage = physical_stage
        policy = special_policies[item_id]
        if item_id == 7770901 and physical_stage >= 2:
            special_commands.append(
                ReconciliationCommand(
                    item_id,
                    policy.name,
                    policy.policy,
                    physical_stage,
                    stable_spool_id(
                        "reconcile", scope.room_seed_name, scope.team, scope.slot,
                        context.identity, "special", physical_stage, "remove-crucible",
                    ),
                    "removeInventoryItem weapon/player/crucible",
                    "replace Crucible with Sentinel Hammer",
                )
            )
        for delivery in deliveries.commands:
            special_commands.append(
                ReconciliationCommand(
                    item_id,
                    policy.name,
                    policy.policy,
                    physical_stage,
                    stable_spool_id(
                        "reconcile", scope.room_seed_name, scope.team, scope.slot,
                        context.identity, "special", physical_stage, delivery.index,
                    ),
                    delivery.command,
                    deliveries.description,
                )
            )
        has_hammer = (item_id == 7770009) or (item_id == 7770901 and physical_stage >= 2) or (item_id == 7770902 and physical_stage >= 1)
        if has_hammer and context.campaign in ("TAG2", "Dark Lord") and context.supports(TAG_SPECIAL_CAPABILITY):
            hammer_upgrades = (
                ("ammo_drops_upgraded", "perk/player/weapons/hammer/ammo_drops_upgraded"),
                ("armor_and_health_drops_upgraded", "perk/player/weapons/hammer/armor_and_health_drops_upgraded"),
            )
            existing_cmd_strings = {cmd.command for cmd in special_commands}
            for upgrade_key, perk_path in hammer_upgrades:
                cmd_str = f"ai_ScriptCmdEnt player1 givePlayerPerk {perk_path}"
                if cmd_str not in existing_cmd_strings:
                    special_commands.append(
                        ReconciliationCommand(
                            item_id,
                            policy.name,
                            policy.policy,
                            physical_stage,
                            stable_spool_id(
                                "reconcile", scope.room_seed_name, scope.team, scope.slot,
                                context.identity, "special", physical_stage, f"hammer-upgrade-{upgrade_key}",
                            ),
                            cmd_str,
                            f"Sentinel Hammer upgrade: {upgrade_key}",
                        )
                    )
    special_diagnostic = (item_id, count, selected_special_stage, context.identity, len(special_commands)) if special_candidates else None
    support_commands = []
    for item_id in support_rune_commands(received, context):
        support_delivery = compile_item_delivery_plan(
            item_id, {item_id: item_definitions[item_id]}
        )
        if len(support_delivery.commands) != 1:
            raise MaterializationPlanError(f"invalid support rune plan {item_id}")
        policy = replay_policies[item_id]
        delivery = support_delivery.commands[0]
        support_commands.append(
            ReconciliationCommand(
                item_id,
                policy.name,
                policy.policy,
                0,
                stable_spool_id(
                    "reconcile", scope.room_seed_name, scope.team, scope.slot,
                    context.identity, "support-rune", item_id,
                ),
                delivery.command,
                support_delivery.description,
            )
        )
    blood_punch_commands = []
    if context.campaign != "Base" and 7770014 in received:
        for upgrade in ownership.blood_punch_upgrades:
            blood_punch_commands.append(
                ReconciliationCommand(
                    7770014,
                    "Blood Punch",
                    "replay_idempotent",
                    upgrade.location_id,
                    stable_spool_id(
                        "reconcile", scope.room_seed_name, scope.team, scope.slot,
                        context.identity, "blood-punch-upgrade", upgrade.location_id,
                    ),
                    f"ai_ScriptCmdEnt player1 givePlayerPerk {upgrade.perk_path}",
                    f"Blood Punch upgrade from {upgrade.mission_name}",
                )
            )

    dash_commands = []
    if (
        context.campaign != "Base"
        and ownership.vanilla_dash
    ):
        dash_commands.append(
            ReconciliationCommand(
                7770015,
                "Dash",
                "replay_idempotent",
                0,
                stable_spool_id(
                    "reconcile", scope.room_seed_name, scope.team, scope.slot,
                    context.identity, "unrandomized-dash",
                ),
                "give ability_dash",
                "Vanilla Dash proven by Exultia mission completion",
            )
        )

    raw_commands = (
        tuple(plan.commands)
        + tuple(special_commands)
        + tuple(support_commands)
        + tuple(blood_punch_commands)
        + tuple(dash_commands)
    )
    commands = []
    semantic_operations = set()
    for command in raw_commands:
        semantic_key = (command.item_id, command.stage, command.command)
        if semantic_key in semantic_operations:
            continue
        semantic_operations.add(semantic_key)
        commands.append(command)
    commands = tuple(commands)
    plan = type(plan)(
        commands=commands,
        selections=plan.selections,
        replayed=plan.replayed,
        special_stages=1 if special_commands else 0,
        skipped_never_replay=plan.skipped_never_replay,
        skipped_unproven=plan.skipped_unproven,
        skipped_manual_replay=plan.skipped_manual_replay,
    )
    return MaterializationPlan(
        scope, plan, len(raw_commands), len(support_commands), selected_special_stage,
        special_diagnostic,
    )


def compile_automatic_plan(authoritative_ids, context, scope, definitions, policies):
    """Historical automatic repair excludes manual presentation and special context effects."""
    excluded = SUPPORT_RUNE_IDS | {7770007, 7770009, 7770901, 7770902}
    allowed = set(context_item_ids(context, authoritative_ids))
    replayable = tuple(item_id for item_id in authoritative_ids if item_id not in excluded and item_id in allowed)
    return compile_reconciliation_plan(
        replayable, definitions, policies,
        f"{scope.room_seed_name}-{scope.team}-{scope.slot}", scope.evidence_epoch,
    )


def authored_effects_allowed(*, evidence, campaign, epoch, lease, process_running,
                             item_ready, room_identity, connected, queue_authoritative):
    return bool(campaign not in {None, "Base"} and evidence is not None
                and evidence.state == "gameplay" and evidence.native_safe
                and isinstance(epoch, str) and lease == epoch and process_running
                and item_ready and room_identity and connected and queue_authoritative)


def reconciliation_session_block(*, item_ready, require_connection, connected, team, slot, seed):
    import re
    if not item_ready:
        return "item state is not ready"
    if require_connection and not connected:
        return "connected AP session required"
    if team is None or slot is None:
        return "current AP team/slot identity is incomplete"
    if not seed or not re.fullmatch(r"[A-Za-z0-9_.-]+", str(seed)):
        return "seed identity is missing or unsafe for deterministic spool IDs"
    return None


def reconciliation_runtime_block(*, evidence, marker, runtime_ready, campaign, active_slot,
                                 active_epoch, active_map, supported_maps, boundary, history_length):
    if evidence is None or evidence.state != "gameplay":
        return "confirmed gameplay epoch required; menus are not eligible"
    if marker is None:
        return "active AP map marker is unavailable"
    if not runtime_ready:
        return "runtime effects are not level-ready"
    if campaign in {None, "Base"}:
        if active_slot != evidence.slot_directory:
            return "gameplay evidence does not match the active save slot"
        if active_epoch != evidence.epoch:
            return "gameplay evidence does not match the active epoch"
    if active_map not in supported_maps:
        return "active map has no ap_rpc_v3 reconciliation entities"
    if boundary > history_length:
        return "authoritative received-item history is incomplete"
    return None
