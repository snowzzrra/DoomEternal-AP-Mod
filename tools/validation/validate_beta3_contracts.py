#!/usr/bin/env python3
"""Validate beta.3 item contracts and report intentionally blocked surfaces."""

from __future__ import annotations

import json
from pathlib import Path

from item_classification import load_item_classification_identity
from item_contracts import (
    UNSUPPORTED_EXTRA_LIVES_BLOCKER,
    UNSUPPORTED_POWERUP_BLOCKER,
    load_item_runtime_contracts,
    validate_beta3_item_contracts,
    validate_death_link_mode,
)
from item_reconciliation import load_policy_registry

ROOT = Path(__file__).resolve().parents[2]


def validate() -> list[str]:
    definitions = {
        int(item_id): definition
        for item_id, definition in json.loads(
            (ROOT / "data/items.json").read_text(encoding="utf-8")
        ).items()
    }
    classifications = load_item_classification_identity(
        ROOT / "data/item_classifications.json"
    )
    classification_values = {
        item_id: entry["classification"] for item_id, entry in classifications.items()
    }
    policies = load_policy_registry(
        ROOT / "data/item_replay_policies.json", definitions
    )
    validate_beta3_item_contracts(
        definitions,
        classification_values,
        policies,
        load_item_runtime_contracts(),
    )
    supported, blocker = validate_death_link_mode("hardcore")
    if not supported or blocker:
        raise ValueError("hardcore DeathLink mode must be supported")
    extra_supported, extra_blocker = validate_death_link_mode("extra_lives")
    if extra_supported or extra_blocker != UNSUPPORTED_EXTRA_LIVES_BLOCKER:
        raise ValueError("extra_lives mode must fail closed with exact blocker")
    return [UNSUPPORTED_EXTRA_LIVES_BLOCKER, UNSUPPORTED_POWERUP_BLOCKER]


def main() -> int:
    blockers = validate()
    print("beta.3 item contracts: passed")
    for blocker in blockers:
        print(f"BLOCKER: {blocker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
