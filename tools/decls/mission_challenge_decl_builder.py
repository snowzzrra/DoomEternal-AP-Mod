#!/usr/bin/env python3
"""Build scoped, reward-free overrides for proven Mission Challenges."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from challenge_registry import load_challenge_registry

ROOT = Path(__file__).resolve().parent.parent.parent
CHILD_SOURCE_OWNER = "gameresources"
CHILD_TARGET_OWNER = "gameresources"
AGGREGATE_SOURCE_OWNER = "gameresources_patch2"
AGGREGATE_TARGET_OWNER = "gameresources_patch2"
AGGREGATE_LIST_PATH = "missionchallengelist/missionchallenge/main.decl"
AGGREGATE_LIST_SHA256 = "e4528a4751e40f1237224989c0357df4bdd8d0f6d86fee8c502eeed5ff393ff4"
AGGREGATE_SUPPRESSION_MISSION_KEYS = frozenset({
    "e1m4",
    "e3m2_hell",
    "e3m2_hell_b",
})
REWARD_FIELD = """\t\tcurrencyToGive = {
\t\t\tnum = 0;
\t\t}
"""


def _source(owner: str, path: str, expected_sha256: str) -> str:
    source = ROOT / "vanilla_decls" / "owners" / owner / "generated" / "decls" / path
    payload = source.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"Mission Challenge vanilla owner hash drift for {path}: {actual}")
    return payload.decode("utf-8")


def _assert_reward_owner(entries: list[dict]) -> None:
    owners = {entry["reward_owner"]["inherited_path"] for entry in entries}
    hashes = {entry["reward_owner"]["sha256"] for entry in entries}
    currencies = {entry["reward_owner"]["currency"] for entry in entries}
    registry_paths = set(entry["completion_owner"]["path"] for entry in entries)
    if len(entries) != len(registry_paths) or len(set(entry["location_id"] for entry in entries)) != len(entries):
        raise ValueError("Mission Challenge entries have non-unique paths or location IDs")
    if owners != {"unlockable/mission_challenge/challenge_base.decl"}:
        raise ValueError("refusing an unscoped Mission Challenge reward override")
    if len(hashes) != 1 or currencies != {"CURRENCY_PRAETOR_UPGRADE"}:
        raise ValueError("Mission Challenge reward owner contract drift")
    base = _source(CHILD_SOURCE_OWNER, next(iter(owners)), next(iter(hashes)))
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


def _block_end(source: str, start: int) -> int:
    opening = source.find("{", start)
    if opening < 0:
        raise ValueError("DECL block has no opening brace")
    depth = 0
    for offset in range(opening, len(source)):
        if source[offset] == "{":
            depth += 1
        elif source[offset] == "}":
            depth -= 1
            if depth == 0:
                return offset + 1
    raise ValueError("DECL block has unbalanced braces")


def _level_blocks(source: str) -> list[tuple[int, int, int, str]]:
    level_list = re.search(r"\blevelList\s*=\s*\{", source)
    if not level_list:
        raise ValueError("Mission Challenge list has no levelList")
    level_end = _block_end(source, level_list.start())
    body_start = source.find("{", level_list.start()) + 1
    blocks: list[tuple[int, int, int, str]] = []
    for match in re.finditer(r"(?m)^(\s*)item\[(\d+)\]\s*=\s*\{", source[body_start:level_end]):
        start = body_start + match.start()
        end = _block_end(source, start)
        if end > level_end:
            raise ValueError("Mission Challenge level item escapes levelList")
        blocks.append((int(match.group(2)), start, end, source[start:end]))
    if not blocks:
        raise ValueError("Mission Challenge list has no level items")
    return blocks


def _challenge_paths(block: str) -> tuple[str, ...]:
    challenge = re.search(r"\bchallenges\s*=\s*\{", block)
    if not challenge:
        return ()
    end = _block_end(block, challenge.start())
    return tuple(re.findall(
        r'(?m)^\s*item\[\d+\]\s*=\s*"([^"]+)";',
        block[challenge.start():end],
    ))


def _aggregate_contracts(registry: dict) -> list[dict]:
    challenges = registry["mission_challenges"]
    contracts = []
    for aggregate in registry["all_mission_challenges"]:
        mission_key = aggregate["mission_key"]
        entries = [
            entry for entry in challenges
            if entry.get("mission_key") == mission_key
            or entry["signal"]["unlockable"].split("/")[1] == mission_key
        ]
        children = {entry["location_id"] for entry in entries}
        if children != set(aggregate["signal"]["children"]):
            raise ValueError(f"{aggregate['name']}: aggregate child discovery drift")
        unlockables = tuple(entry["signal"]["unlockable"] for entry in entries)
        if not unlockables or len(unlockables) != len(set(unlockables)):
            raise ValueError(f"{aggregate['name']}: aggregate unlockable set is invalid")
        contracts.append({
            "name": aggregate["name"],
            "mission_key": mission_key,
            "location_id": aggregate["location_id"],
            "unlockables": unlockables,
        })
    if not contracts:
        raise ValueError("Mission Challenge registry has no aggregate contracts")
    return contracts


def _aggregate_suppression_contracts(registry: dict) -> list[dict]:
    contracts = [
        contract for contract in _aggregate_contracts(registry)
        if contract["mission_key"] in AGGREGATE_SUPPRESSION_MISSION_KEYS
    ]
    found = {contract["mission_key"] for contract in contracts}
    if found != AGGREGATE_SUPPRESSION_MISSION_KEYS:
        raise ValueError(
            "Mission Challenge native aggregate suppression scope drift: "
            f"expected {sorted(AGGREGATE_SUPPRESSION_MISSION_KEYS)}, found {sorted(found)}"
        )
    return contracts


def _suppress_aggregate_reward(block: str) -> str:
    """Remove native aggregate challenges block to suppress default Sentinel Battery reward."""
    challenge_match = re.search(r"\bchallenges\s*=\s*\{", block)
    if not challenge_match:
        return block
    start = challenge_match.start()
    end = _block_end(block, start)
    while end < len(block) and block[end] in " \t\r\n":
        end += 1
    return block[:start] + block[end:]


def _aggregate_reward_free_override(registry: dict) -> tuple[str, list[dict]]:
    source = _source(
        AGGREGATE_SOURCE_OWNER,
        AGGREGATE_LIST_PATH,
        AGGREGATE_LIST_SHA256,
    ).replace("\r\n", "\n")
    blocks = _level_blocks(source)
    contracts = _aggregate_suppression_contracts(registry)
    replacements: list[tuple[int, int, str]] = []
    audit_contracts = []
    used_indexes: set[int] = set()
    for contract in contracts:
        expected = set(contract["unlockables"])
        matches = [
            (index, start, end, block)
            for index, start, end, block in blocks
            if set(_challenge_paths(block)) == expected
            and "_dev_" not in block
        ]
        if len(matches) != 1:
            raise ValueError(
                f"{contract['name']}: expected one vanilla aggregate owner, found {len(matches)}"
            )
        index, start, end, block = matches[0]
        if index in used_indexes:
            raise ValueError(f"{contract['name']}: aggregate owner reused")
        used_indexes.add(index)
        replacement = _suppress_aggregate_reward(block)
        if _challenge_paths(replacement) != ():
            raise ValueError(f"{contract['name']}: completion challenges block not removed")
        for presentation in ("levelName",):
            if len(re.findall(r"\b" + presentation + r"\b", replacement)) != len(re.findall(r"\b" + presentation + r"\b", block)):
                raise ValueError(f"{contract['name']}: presentation changed")
        if re.search(r"\bchallenges\s*=\s*\{", replacement):
            raise ValueError(f"{contract['name']}: aggregate challenges block remains")
        if re.search(r"CURRENCY_|inventoryItemReward|currencyToGive|gainedItems\s*=\s*\{\s*num\s*=\s*[1-9]", replacement):
            raise ValueError(f"{contract['name']}: vanilla aggregate reward remains")
        replacements.append((block, replacement))
        audit_contracts.append({
            **contract,
            "level_index": index,
            "challenges_suppressed": True,
        })
    override = source
    for block, replacement in replacements:
        override = override.replace(block, replacement, 1)
    for _, _, _, block in _level_blocks(override):
        if "_dev_" in block:
            continue
        for contract in contracts:
            if set(_challenge_paths(block)) == set(contract["unlockables"]):
                raise ValueError(f"{contract['name']}: aggregate challenges block remains")
    return override, audit_contracts


def _reward_free_override(entry: dict) -> str:
    owner = entry["completion_owner"]
    source = _source(
        CHILD_SOURCE_OWNER,
        owner["path"],
        owner["sha256"],
    ).replace("\r\n", "\n")
    signal = entry["signal"]
    required = (
        'inherit = "mission_challenge/challenge_base";',
        f'completionStat = "{owner["completion_stat"]}";',
        f'stat = "{signal["rule_0_statname"]}";',
        "count = ",
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
    registry = load_challenge_registry()
    entries = registry["mission_challenges"]
    _assert_reward_owner(entries)
    _assert_proven_observer()
    written_paths = []
    for entry in entries:
        relative = entry["completion_owner"]["path"]
        target = mod_root / CHILD_TARGET_OWNER / "generated" / "decls" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_reward_free_override(entry), encoding="utf-8")
        written_paths.append(target.as_posix())
    if len(written_paths) != len(entries) or len(written_paths) != len(set(written_paths)):
        raise ValueError("Mission Challenge override set is incomplete or has duplicates")
    aggregate_override, aggregate_contracts = _aggregate_reward_free_override(registry)
    aggregate_target = (
        mod_root
        / AGGREGATE_TARGET_OWNER
        / "generated"
        / "decls"
        / AGGREGATE_LIST_PATH
    )
    aggregate_target.parent.mkdir(parents=True, exist_ok=True)
    aggregate_target.write_text(aggregate_override, encoding="utf-8")
    written_paths.append(aggregate_target.as_posix())
    return {
        "child_owner": CHILD_TARGET_OWNER,
        "aggregate_owner": AGGREGATE_TARGET_OWNER,
        "challenge_count": len(entries),
        "location_ids": [entry["location_id"] for entry in entries],
        "aggregate_reward_suppression": {
            "strategy": "suppress_aggregate_challenges_in_main_decl",
            "source_owner": AGGREGATE_SOURCE_OWNER,
            "target_owner": AGGREGATE_TARGET_OWNER,
            "source_path": AGGREGATE_LIST_PATH,
            "aggregate_count": len(aggregate_contracts),
            "contracts": aggregate_contracts,
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
