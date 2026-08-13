"""Item and DeathLink contract checks used by runtime surfaces."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ITEM_RUNTIME_CONTRACTS_FILE = Path(__file__).with_name("data") / "item_runtime_contracts.json"
DEFAULT_DEATH_LINK_MODE = "soft"


def load_item_runtime_contracts(
    path: Path = ITEM_RUNTIME_CONTRACTS_FILE,
) -> dict[int, dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1 or document.get("contract_revision") != 3:
        raise ValueError("item runtime contract schema/revision mismatch")
    raw_items = document.get("items")
    if not isinstance(raw_items, dict):
        raise ValueError("item runtime contracts items must be an object")
    contracts: dict[int, dict[str, Any]] = {}
    for raw_id, contract in raw_items.items():
        item_id = int(raw_id)
        if str(item_id) != raw_id or not isinstance(contract, dict):
            raise ValueError(f"invalid item runtime contract {raw_id!r}")
        contracts[item_id] = dict(contract)
    return contracts


def validate_item_contracts(
    definitions: Mapping[int, Any],
    classifications: Mapping[int, Any],
    policies: Mapping[int, Any],
    contracts: Mapping[int, Mapping[str, Any]] | None = None,
) -> None:
    from foundation import compile_item_delivery_plan
    from item_classification import notification_entity_name

    contracts = contracts or load_item_runtime_contracts()
    for item_id, contract in contracts.items():
        if item_id not in definitions or item_id not in classifications or item_id not in policies:
            raise ValueError(f"incomplete item contract for {item_id}")
        if classifications[item_id] != 2:
            raise ValueError(f"item {item_id} must remain useful-classified")
        policy = policies[item_id]
        if getattr(policy, "policy", None) != contract["replay_policy"]:
            raise ValueError(f"item {item_id} replay policy mismatch")
        if getattr(policy, "receipt_feedback", None) != contract["receipt_feedback"]:
            raise ValueError(f"item {item_id} receipt policy mismatch")
        if contract["start_inventory_eligible"] is not False:
            raise ValueError(f"item {item_id} must not be start-inventory eligible")
        if definitions[item_id] != contract["runtime_effect"]:
            raise ValueError(f"item {item_id} runtime effect mismatch")
        plan = compile_item_delivery_plan(
            item_id,
            dict(definitions),
            receipt=True,
            classification=classifications[item_id],
            notification_slot="a",
        )
        if plan.primitive_id != contract["runtime_primitive"]:
            raise ValueError(f"item {item_id} runtime primitive mismatch")
        if plan.commands[-1].entity != notification_entity_name(
            item_id, classifications[item_id], slot="a"
        ):
            raise ValueError(f"item {item_id} notification entity mismatch")


def start_inventory_eligible(item_id: int) -> bool:
    contract = load_item_runtime_contracts().get(item_id)
    return contract is None or bool(contract["start_inventory_eligible"])
