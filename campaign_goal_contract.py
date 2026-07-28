"""Single source of truth for the release campaign-goal map event."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONTRACT_PATH = ROOT / "data" / "campaign_goal_contract.json"
REQUIRED_FIELDS = {
    "schema_version",
    "release",
    "publisher_key",
    "event_filename",
    "marker",
    "event_target",
    "owner",
    "source",
    "runtime_map",
    "destination_map",
}


def load_campaign_goal_contract(path: Path = CONTRACT_PATH) -> dict:
    contract = json.loads(path.read_text(encoding="utf-8"))
    if set(contract) != REQUIRED_FIELDS:
        raise ValueError(
            "campaign goal contract fields drift: "
            f"expected {sorted(REQUIRED_FIELDS)}, got {sorted(contract)}"
        )
    if contract["schema_version"] != 1:
        raise ValueError("unsupported campaign goal contract schema")
    for key in REQUIRED_FIELDS - {"schema_version"}:
        if not isinstance(contract[key], str) or not contract[key].strip():
            raise ValueError(f"campaign goal contract has invalid {key}")
    if not contract["event_filename"].endswith(".txt"):
        raise ValueError("campaign goal event_filename must be a .txt file")
    if not contract["marker"].startswith("AP_GOAL_EVENT_"):
        raise ValueError("campaign goal marker must use AP_GOAL_EVENT_")
    if contract["event_target"] != "ap_campaign_goal_event":
        raise ValueError("campaign goal event_target drift")
    if contract["publisher_key"] != "sentinel_prime_campaign_goal":
        raise ValueError("campaign goal compatibility projection drift")
    if contract["runtime_map"] == contract["destination_map"]:
        raise ValueError("campaign goal transition cannot target its source map")
    return contract


CAMPAIGN_GOAL_CONTRACT = load_campaign_goal_contract()
