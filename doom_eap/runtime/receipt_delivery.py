"""Receipt feedback policy and the existing resumable publication ledgers."""
from doom_eap.contracts.foundation import compile_item_delivery_plan
from doom_eap.contracts.receipt_delivery import (
    HISTORICAL_OWNERSHIP,
    NEW_RECEIPT,
    PRESENTATION_REPAIR,
    RECONCILIATION_REPAIR,
    ReceiptFeedbackFacts,
    ReceiptIntent,
    ReceiptPlan,
)
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
    return ReceiptPlan(tuple(commands), plan.description)


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
