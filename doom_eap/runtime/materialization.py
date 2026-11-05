"""Persistent inventory planning from supplied ownership, context and catalogs."""
from dataclasses import dataclass
from enum import Enum

from doom_eap.contracts.inventory_domain import (
    InventoryObservation,
)
from doom_eap.contracts.materialization import (
    MaterializationScope,
)
from doom_eap.runtime.item_reconciliation import ReconciliationPlan


class ReconciliationBlock(str, Enum):
    RUNTIME_NOT_READY = 'runtime effects are not level-ready'
    NATIVE_APPLICATION_PENDING = 'materialization queued; awaiting native application'

    def __str__(self):
        return self.value


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










def compile_materialization_plan(
    ownership, context, scope, item_definitions, replay_policies, special_mode,
    *, observation: InventoryObservation | None = None, selected_item_ids=None,
):
    from doom_eap.runtime.inventory import InventoryOwnership, compare_inventory

    result = compare_inventory(
        InventoryOwnership(ownership.reconciliation_item_ids, ownership.blood_punch_upgrades),
        definitions=item_definitions, policies=replay_policies,
        observation=observation, context=context, scope=scope, special_mode=special_mode,
        selected_item_ids=selected_item_ids,
    )
    if not result.reconciliation.commands and result.unresolved_item_ids:
        raise MaterializationPlanError("empty plan has unresolved native observations: "
                                       + ", ".join(map(str, result.unresolved_item_ids)))
    return result.materialization


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
        return "seed identity is missing or unsafe for deterministic command keys"
    return None


def reconciliation_runtime_block(*, evidence, marker, runtime_ready, campaign, active_slot,
                                 active_epoch, active_map, supported_maps, boundary, history_length):
    if evidence is None or evidence.state != "gameplay":
        return "confirmed gameplay epoch required; menus are not eligible"
    if marker is None:
        return "active AP map marker is unavailable"
    if not runtime_ready:
        return ReconciliationBlock.RUNTIME_NOT_READY
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
