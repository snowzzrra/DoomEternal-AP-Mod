"""Receipt feedback policy and the existing resumable publication ledgers."""
from typing import Callable, Protocol
from doom_eap.contracts.receipt_delivery import ReceiptIntent, ReceiptFeedbackFacts, ReceiptPlan, MappingRepair, ReceiptPublicationScope
from doom_eap.contracts.foundation import compile_item_delivery_plan
from doom_eap.contracts.receipt_delivery import NEW_RECEIPT, HISTORICAL_OWNERSHIP, RECONCILIATION_REPAIR, PRESENTATION_REPAIR
from doom_eap.runtime.item_reconciliation import AP_RECEIPT_FEEDBACK








def requires_progressive_observation(request, definitions):
    definition = definitions.get(request.item_id)
    return (request.intent in {NEW_RECEIPT, RECONCILIATION_REPAIR, PRESENTATION_REPAIR}
            and isinstance(definition, dict) and definition.get("type") in {"progressive_perk", "progressive_item"}
            and isinstance(request.item_index, int) and not isinstance(request.item_index, bool))


def suppress_local_toast(request, facts):
    if request.intent != NEW_RECEIPT or isinstance(request.item_index, bool) or not isinstance(request.item_index, int):
        return False
    if request.item_index < facts.processed_boundary:
        return False
    location = facts.source_location
    if isinstance(location, bool) or not isinstance(location, int) or location <= 0 or not facts.checked_ready:
        return False
    return location in facts.checked and location in facts.local_locations and location in facts.placements


def compile_receipt_plan(request: ReceiptIntent, facts: ReceiptFeedbackFacts, stage, definitions, policies, classifications) -> ReceiptPlan:
    if request.intent not in {NEW_RECEIPT, HISTORICAL_OWNERSHIP, RECONCILIATION_REPAIR, PRESENTATION_REPAIR}:
        return ReceiptPlan(None, f"unsupported item delivery intent: {request.intent!r}")
    if request.intent == HISTORICAL_OWNERSHIP:
        return ReceiptPlan((), "historical ownership observed")
    receipt = request.intent == NEW_RECEIPT and (True if request.include_notification is None else bool(request.include_notification))
    receipt = receipt and policies[request.item_id].receipt_feedback == AP_RECEIPT_FEEDBACK
    if request.suppress_local_toast and suppress_local_toast(request, facts):
        receipt = False
    if receipt and facts.notification_slot is None:
        receipt = False
    classification = request.classification
    if receipt and classification is None:
        classification = classifications.get(request.item_id)
    try:
        plan = compile_item_delivery_plan(request.item_id, definitions, stage=stage, receipt=receipt,
            classification=classification, notification_slot=facts.notification_slot if receipt else None)
    except ValueError as error:
        return ReceiptPlan(None, str(error))
    commands = [command.command for command in plan.commands]
    if request.item_id == 7770901 and stage is not None and stage >= 1:
        commands.insert(0, "removeInventoryItem weapon/player/crucible")
    return ReceiptPlan(tuple(commands), plan.description)




class ReceiptPublicationPort(Protocol):
    def publish(self, command: str, scope: ReceiptPublicationScope, item_id: int, item_index: int,
                ordinal: int, item_name: str, *, deferred: bool = False) -> tuple[bool, str]: ...


class ReceiptDelivery:
    def __init__(self, logger):
        self._state = {}
        self._logger = logger

    def bind(self, state):
        self._state = state

    def reset(self, mapping_revision):
        self._state["receipt_notifications"] = {}
        self._state["item_mapping_revision"] = mapping_revision
        self._state.pop("mapping_repair_indices", None)
        self._state.pop("item_command_groups", None)

    def next_mapping_repair(self, item_ids, boundary, revision, repaired_items):
        current = int(self._state.get("item_mapping_revision", 0))
        if current >= revision:
            return MappingRepair("done")
        if len(item_ids) < boundary:
            return MappingRepair("deferred")
        repaired = {int(index) for index in self._state.get("mapping_repair_indices", [])}
        repair_ids = {item_id for minimum, ids in repaired_items.items() if current < minimum for item_id in ids}
        for index, item_id in enumerate(item_ids[:boundary]):
            if item_id in repair_ids and index not in repaired:
                return MappingRepair("repair", index, item_id)
        return MappingRepair("complete")

    def record_mapping_repair(self, repair, description, persist):
        repaired = {int(index) for index in self._state.get("mapping_repair_indices", [])}
        repaired.add(repair.item_index)
        self._state["mapping_repair_indices"] = sorted(repaired)
        persist("mapping_repair_progress")
        self._logger.info(f"[State] Recovered item affected by an older mapping {repair.item_id} "
                          f"at receive index {repair.item_index}: {description}")

    def finish_mapping_repair(self, revision, persist):
        self._state["item_mapping_revision"] = revision
        self._state.pop("mapping_repair_indices", None)
        persist("mapping_revision_update")

    def deferred_notification(self, plan: ReceiptPlan, item_id, item_index, item_name, scope: ReceiptPublicationScope,
                              publication: ReceiptPublicationPort, persist: Callable[[], None]):
        if plan.commands is None:
            return False, plan.description
        notification = next((command for command in plan.commands if command.startswith("ai_ScriptCmdEnt ap_notify_item_")), None)
        if notification is None:
            return True, "no local notification generated"
        state = self._state.setdefault("receipt_notifications", {})
        key = str(item_index)
        if state.get(key):
            return True, "local notification already queued"
        accepted, command_id = publication.publish(notification, scope, item_id, item_index, 0, item_name, deferred=True)
        if not accepted:
            return False, f"failed to spool {command_id}"
        state[key] = {"item_id": item_id, "command_id": command_id}
        persist()
        return True, plan.description

    def spool(self, plan: ReceiptPlan, item_id, item_index, item_name, scope: ReceiptPublicationScope,
              publication: ReceiptPublicationPort, persist: Callable[[str], None]):
        if plan.commands is None:
            return False, plan.description
        commands = plan.commands
        groups = self._state.setdefault("item_command_groups", {})
        key = str(item_index)
        group = groups.setdefault(key, {"item_id": item_id, "next_command": 0, "total_commands": len(commands)})
        if group.get("item_id") != item_id:
            return False, "stored command group belongs to a different item"
        next_command = int(group.get("next_command", 0))
        if next_command < 0 or next_command > len(commands):
            return False, "stored command group index is invalid"
        for ordinal in range(next_command, len(commands)):
            accepted, _command_id = publication.publish(commands[ordinal], scope, item_id, item_index, ordinal, item_name)
            if not accepted:
                return False, plan.description
            group["next_command"] = ordinal + 1
            group["total_commands"] = len(commands)
            persist("item_command_group_progress")
        groups.pop(key, None)
        if not groups:
            self._state.pop("item_command_groups", None)
        persist("item_command_group_complete")
        return True, plan.description


def base_special_receipt_allowed(item_id, campaign, special_mode):
    if campaign != "Base":
        return False
    if special_mode in {"the_crucible", "The Crucible"}:
        return item_id == 7770007
    if special_mode in {"progressive_special_weapon", "Progressive Special Weapon"}:
        return item_id == 7770901
    if special_mode in {"progressive_sentinel_hammer", "Progressive Sentinel Hammer"}:
        return item_id == 7770902
    return False
