import json
from dataclasses import replace
from pathlib import Path

from doom_eap.contracts.receipt_delivery import (
    HISTORICAL_OWNERSHIP,
    NEW_RECEIPT,
    PRESENTATION_REPAIR,
    ReceiptFeedbackFacts,
    ReceiptIntent,
)
from doom_eap.runtime.bridge_items import compile_receipt_plan
from doom_eap.runtime.inventory import load_policy_registry


def _assert_silent_ownership_and_receipts(plan, request):
    assert plan(replace(request, intent=HISTORICAL_OWNERSHIP)).commands == ()
    assert not any("ap_notify_item_" in command for command in plan(replace(request, intent=PRESENTATION_REPAIR)).commands)
    assert "removeInventoryItem weapon/player/crucible" not in plan(ReceiptIntent(7770901, 10, NEW_RECEIPT), stage=1).commands
    assert "removeInventoryItem weapon/player/crucible" not in plan(ReceiptIntent(7770901, 10, NEW_RECEIPT), stage=0).commands


def _assert_alternating_item_notifications(first_toast, second_toast):
    assert len(first_toast) == len(second_toast) == 1 and first_toast != second_toast


def test_feedback_is_live_occurrence_only_alternates_per_item_and_preserves_special_ownership(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    definitions = {int(key): value for key, value in json.loads((root / "data/items.json").read_text()).items()}
    policies = load_policy_registry(root / "data/item_replay_policies.json", definitions)
    facts = ReceiptFeedbackFacts(1, 10, None, True, frozenset(), frozenset(), frozenset())
    monkeypatch.setattr(Path, "read_text", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("planner discovery")))

    def plan(request, current=facts, stage=None):
        return compile_receipt_plan(request, current, stage, definitions, policies, {7770001: 1, 7770901: 1})

    request = ReceiptIntent(7770001, 10, NEW_RECEIPT)
    first, second = plan(request), plan(request, replace(facts, owned_count=2))
    first_toast = [command for command in first.commands if "ap_notify_item_" in command]
    second_toast = [command for command in second.commands if "ap_notify_item_" in command]
    _assert_alternating_item_notifications(first_toast, second_toast)
    for current in (replace(facts, owned_count=None),
                    replace(facts, source_location=100, checked=frozenset({100}),
                            local_locations=frozenset({100}), placements=frozenset({100}))):
        assert not any("ap_notify_item_" in command for command in plan(request, current).commands)
    _assert_silent_ownership_and_receipts(plan, request)
