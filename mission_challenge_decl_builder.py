#!/usr/bin/env python3
"""Build scoped, reward-free overrides for proven Cultist Base challenges."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from challenge_registry import load_challenge_registry


ROOT = Path(__file__).resolve().parent
OWNER = "gameresources"
REWARD_FIELD = """\t\tcurrencyToGive = {
\t\t\tnum = 0;
\t\t}
"""


def _source(path: str, expected_sha256: str) -> str:
    source = ROOT / "vanilla_decls" / "owners" / OWNER / "generated" / "decls" / path
    payload = source.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"Mission Challenge vanilla owner hash drift for {path}: {actual}")
    return payload.decode("utf-8")


def _assert_reward_owner(entries: list[dict]) -> None:
    owners = {entry["reward_owner"]["inherited_path"] for entry in entries}
    hashes = {entry["reward_owner"]["sha256"] for entry in entries}
    currencies = {entry["reward_owner"]["currency"] for entry in entries}
    if len(entries) != 3 or owners != {"unlockable/mission_challenge/challenge_base.decl"}:
        raise ValueError("refusing an unscoped Mission Challenge reward override")
    if len(hashes) != 1 or currencies != {"CURRENCY_PRAETOR_UPGRADE"}:
        raise ValueError("Mission Challenge reward owner contract drift")
    base = _source(next(iter(owners)), next(iter(hashes)))
    if base.count("CURRENCY_PRAETOR_UPGRADE") != 1 or base.count("currencyToGive") != 1:
        raise ValueError("inherited Mission Challenge Suit Point reward is ambiguous")


def _assert_proven_observer() -> None:
    bridge = (ROOT / "bridge_client.py").read_text(encoding="utf-8")
    required = (
        "read_mission_challenge_records",
        "observe_mission_challenges",
        "check_mission_challenge_locations",
        "mission_challenge_records",
    )
    if not all(token in bridge for token in required):
        raise ValueError("refusing to strip challenge rewards without the save reader/send path")


def _reward_free_override(entry: dict) -> str:
    owner = entry["completion_owner"]
    source = _source(owner["path"], owner["sha256"])
    signal = entry["signal"]
    required = (
        'inherit = "mission_challenge/challenge_base";',
        f'completionStat = "{owner["completion_stat"]}";',
        f'stat = "{signal["rule_0_statname"]}";',
        "count = 1;",
    )
    for snippet in required:
        if source.count(snippet) != 1:
            raise ValueError(f"{entry['name']}: native owner drift for {snippet!r}")
    if "currencyToGive" in source or "CURRENCY_PRAETOR_UPGRADE" in source:
        raise ValueError(f"{entry['name']}: child owner unexpectedly defines a reward")
    edit = "\tedit = {\n"
    if source.count(edit) != 1:
        raise ValueError(f"{entry['name']}: edit block is missing or ambiguous")
    override = source.replace(edit, edit + REWARD_FIELD, 1)
    if "CURRENCY_PRAETOR_UPGRADE" in override or override.count("currencyToGive") != 1:
        raise ValueError(f"{entry['name']}: scoped reward suppression failed")
    if override.replace(REWARD_FIELD, "", 1) != source:
        raise ValueError(f"{entry['name']}: fields other than the inherited reward changed")
    return override


def build_mission_challenge_overrides(mod_root: Path) -> dict:
    entries = load_challenge_registry()["mission_challenges"]
    _assert_reward_owner(entries)
    _assert_proven_observer()
    written_paths = []
    for entry in entries:
        relative = entry["completion_owner"]["path"]
        target = mod_root / OWNER / "generated" / "decls" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_reward_free_override(entry), encoding="utf-8")
        written_paths.append(target.as_posix())
    if len(written_paths) != 3 or len(written_paths) != len(set(written_paths)):
        raise ValueError("Cultist Base Mission Challenge override set is incomplete")
    return {
        "owner": OWNER,
        "challenge_count": len(entries),
        "location_ids": [entry["location_id"] for entry in entries],
        "aggregate_reward_suppression": {
            "strategy": "child_currencyToGive_num_zero",
            "field": "currencyToGive.num",
            "value": 0,
            "suppressed_native_rewards": [
                "CURRENCY_PRAETOR_UPGRADE",
                "CURRENCY_SENTINEL_BATTERY",
            ],
            "runtime_evidence": "v0.3.0c.1",
        },
        "written_paths": written_paths,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mod-root", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    args = parser.parse_args()
    audit = build_mission_challenge_overrides(args.mod_root)
    args.audit_output.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
