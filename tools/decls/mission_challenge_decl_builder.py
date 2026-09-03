#!/usr/bin/env python3
"""Build scoped, reward-free overrides for proven Mission Challenges."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

from doom_eap.contracts.challenge_registry import load_challenge_registry

ROOT = Path(__file__).resolve().parent.parent.parent
CHILD_SOURCE_OWNER = "gameresources"
CHILD_TARGET_OWNER = "gameresources"
AGGREGATE_SOURCE_OWNER = "gameresources_patch2"
AGGREGATE_TARGET_OWNER = "gameresources_patch2"
AGGREGATE_LIST_PATH = "missionchallengelist/missionchallenge/main.decl"
AGGREGATE_LIST_SHA256 = "e4528a4751e40f1237224989c0357df4bdd8d0f6d86fee8c502eeed5ff393ff4"
DHB_MISSION_KEY = "e1m4"
DHB_DUMMY_SOURCE_OWNER = "gameresources_patch2"
DHB_DUMMY_PATH = "mission_challenge/e6m3/challenge_3"
DHB_DUMMY_DECL_PATH = "unlockable/mission_challenge/e6m3/challenge_3.decl"
DHB_DUMMY_SHA256 = "7528ea87150ce66df800f420ea775f02f452b485b8f18a942438ea6c41a735d2"
DHB_DUMMY_STAT = "STAT_PAIN_ELEMENTAL_GLORYKILL_STYLES"
NEKRAVOL_DUMMY_PATH = "mission_challenge/e6m3/challenge_2"
NEKRAVOL_DUMMY_DECL_PATH = "unlockable/mission_challenge/e6m3/challenge_2.decl"
NEKRAVOL_DUMMY_SHA256 = "cae29c0689cae500f839825114eeb8c1927fca0aae2655a1ee409b8518c8599b"
NEKRAVOL_DUMMY_STAT = "STAT_CHARGEBALL_MATCH_WON"
PLAN_B_REGISTRATIONS = (
    {
        "mission_key": DHB_MISSION_KEY,
        "label": "DHB",
        "dummy_path": DHB_DUMMY_PATH,
        "decl_path": DHB_DUMMY_DECL_PATH,
        "sha256": DHB_DUMMY_SHA256,
        "stat": DHB_DUMMY_STAT,
        "count": 3,
        "violence_event": None,
        "required_monster": "AI_MONSTER_PAIN_ELEMENTAL",
        "impossibility": "Pain Elementals are absent from vanilla Doom Hunter Base",
    },
    {
        "mission_key": "e3m2_hell",
        "label": "Nekravol",
        "dummy_path": NEKRAVOL_DUMMY_PATH,
        "decl_path": NEKRAVOL_DUMMY_DECL_PATH,
        "sha256": NEKRAVOL_DUMMY_SHA256,
        "stat": NEKRAVOL_DUMMY_STAT,
        "count": 5,
        "violence_event": "violenceevent/mission_challenge/angel_of_death",
        "violence_event_decl_path": "violenceevent/violenceevent/mission_challenge/angel_of_death.decl",
        "violence_event_sha256": "1354aaae8c0406097d703e8861dbb45480b64c46eeef8f807eb5209bf6bee2eb",
        "required_monster": "AI_MONSTER_ZOMBIE_MAYKR",
        "impossibility": "Maykr Drones are absent from vanilla Nekravol",
    },
    {
        "mission_key": "e3m2_hell_b",
        "label": "Nekravol II",
        "dummy_path": NEKRAVOL_DUMMY_PATH,
        "decl_path": NEKRAVOL_DUMMY_DECL_PATH,
        "sha256": NEKRAVOL_DUMMY_SHA256,
        "stat": NEKRAVOL_DUMMY_STAT,
        "count": 5,
        "violence_event": "violenceevent/mission_challenge/angel_of_death",
        "violence_event_decl_path": "violenceevent/violenceevent/mission_challenge/angel_of_death.decl",
        "violence_event_sha256": "1354aaae8c0406097d703e8861dbb45480b64c46eeef8f807eb5209bf6bee2eb",
        "required_monster": "AI_MONSTER_ZOMBIE_MAYKR",
        "impossibility": "Maykr Drones are absent from vanilla Nekravol Part II",
    },
)
PATCH2_CORPUS_ARCHIVE = "gameresources_patch2_decl_analysis_20260710_210928.zip"
PATCH2_CORPUS_PREFIX = (
    "gameresources_patch2_decl_analysis_20260710_210928/files/generated/decls/"
)
BASE_CORPUS_ARCHIVE = "gameresources_decl_analysis_20260710_201519.zip"
BASE_CORPUS_PREFIX = (
    "gameresources_decl_analysis_20260710_201519/files/generated/decls/"
)
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


def _patch2_corpus_source(path: str, expected_sha256: str) -> str:
    archive = ROOT.parent / "Tools" / PATCH2_CORPUS_ARCHIVE
    if not archive.is_file():
        raise ValueError(f"Mission Challenge patch2 corpus missing: {archive}")
    member = PATCH2_CORPUS_PREFIX + path
    with zipfile.ZipFile(archive) as corpus:
        try:
            payload = corpus.read(member)
        except KeyError as error:
            raise ValueError(
                f"Mission Challenge patch2 corpus path missing: {path}"
            ) from error
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_sha256:
        raise ValueError(
            f"Mission Challenge patch2 corpus hash drift for {path}: {actual}"
        )
    return payload.decode("utf-8")


def _base_corpus_source(path: str, expected_sha256: str) -> str:
    archive = ROOT.parent / "Tools" / BASE_CORPUS_ARCHIVE
    if not archive.is_file():
        raise ValueError(f"Mission Challenge base corpus missing: {archive}")
    member = BASE_CORPUS_PREFIX + path
    with zipfile.ZipFile(archive) as corpus:
        try:
            payload = corpus.read(member)
        except KeyError as error:
            raise ValueError(
                f"Mission Challenge base corpus path missing: {path}"
            ) from error
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_sha256:
        raise ValueError(
            f"Mission Challenge base corpus hash drift for {path}: {actual}"
        )
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
    bridge = (ROOT / "doom_eap" / "runtime" / "bridge_client.py").read_text(encoding="utf-8")
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


def _plan_b_registration_contracts(registry: dict) -> tuple[dict, ...]:
    contracts_by_mission = {
        contract["mission_key"]: contract for contract in _aggregate_contracts(registry)
    }
    contracts = []
    for plan in PLAN_B_REGISTRATIONS:
        contract = contracts_by_mission.get(plan["mission_key"])
        if contract is None:
            raise ValueError(f"{plan['label']} Mission Challenge registration contract is missing")
        if len(contract["unlockables"]) != 3:
            raise ValueError(
                f"{plan['label']} must retain exactly three real AP challenge children"
            )
        contracts.append({**contract, "plan": plan})
    return tuple(contracts)


def _assert_dummy_candidate(plan: dict) -> None:
    candidate = _patch2_corpus_source(
        plan["decl_path"],
        plan["sha256"],
    ).replace("\r\n", "\n")
    required = (
        'inherit = "mission_challenge/dlc1_challenge_base";',
        'unlockableFlags = "UNLOCKABLE_FLAG_HORDE_CHALLENGE";',
        f'stat = "{plan["stat"]}";',
        f'count = {plan["count"]};',
        'scoringItem = "horde/challenge_complete";',
    )
    for snippet in required:
        if candidate.count(snippet) != 1:
            raise ValueError(f"{plan['label']} dummy candidate drift for {snippet!r}")
    violence_event = plan.get("violence_event")
    if violence_event and candidate.count(f'violenceEvent = "{violence_event}";') != 1:
        raise ValueError(f"{plan['label']} dummy candidate violence event drift")
    if violence_event:
        event = _base_corpus_source(
            plan["violence_event_decl_path"],
            plan["violence_event_sha256"],
        ).replace("\r\n", "\n")
        event_required = (
            f'requiredMonsterType = "{plan["required_monster"]}";',
            f'stat = "{plan["stat"]}";',
            'item[0] = "damage/firearm/heavy_cannon_bolt_action_cylindrical";',
            "headShot = true;",
        )
        for snippet in event_required:
            if event.count(snippet) != 1:
                raise ValueError(
                    f"{plan['label']} dummy violence event drift for {snippet!r}"
                )
    forbidden = (
        "completionStat",
        "currencyToGive",
        "currencyList",
        "CURRENCY_",
        "inventoryItemReward",
    )
    if any(token in candidate for token in forbidden):
        raise ValueError(
            f"{plan['label']} dummy candidate unexpectedly owns persistence or reward"
        )
    base = _patch2_corpus_source(
        "unlockable/mission_challenge/dlc1_challenge_base.decl",
        "c1a96d4848f4a7b7a6e6e265e99a778a5ee82b636d006eed76e4ff6bd2149e99",
    ).replace("\r\n", "\n")
    if 'statDuration = "DUR_CUSTOM_LEVEL";' not in base:
        raise ValueError(f"{plan['label']} dummy candidate base lost custom-level duration")
    if any(token in base for token in forbidden):
        raise ValueError(
            f"{plan['label']} dummy candidate base unexpectedly owns persistence or reward"
        )


def _append_dummy_to_registration(
    block: str,
    expected: tuple[str, ...],
    dummy_path: str,
    label: str,
) -> str:
    challenge_match = re.search(r"\bchallenges\s*=\s*\{", block)
    if challenge_match is None:
        raise ValueError(f"{label} canonical registration has no challenges block")
    challenge_end = _block_end(block, challenge_match.start())
    challenge_block = block[challenge_match.start():challenge_end]
    if len(re.findall(r"\bnum\s*=\s*3\s*;", challenge_block)) != 1:
        raise ValueError(f"{label} canonical challenge count is not exactly three")
    item_matches = list(re.finditer(
        r'(?m)^(\s*)item\[(\d+)\]\s*=\s*"([^"]+)";',
        challenge_block,
    ))
    if [int(match.group(2)) for match in item_matches] != [0, 1, 2]:
        raise ValueError(f"{label} canonical challenge indexes drifted")
    if tuple(match.group(3) for match in item_matches) != expected:
        raise ValueError(f"{label} canonical real challenge order drifted")
    item_indent = item_matches[-1].group(1)
    close_indent = re.search(r"(?m)^(\s*)\}\s*$", challenge_block)
    if close_indent is None:
        raise ValueError(f"{label} canonical challenges block has no closing indentation")
    patched_challenges = re.sub(
        r"(\bnum\s*=\s*)3(\s*;)", r"\g<1>4\g<2>", challenge_block, count=1
    )
    close = patched_challenges.rfind("}")
    patched_challenges = (
        patched_challenges[:close].rstrip()
        + f'\n{item_indent}item[3] = "{dummy_path}";\n'
        + close_indent.group(1)
        + patched_challenges[close:]
    )
    replacement = block.replace(challenge_block, patched_challenges, 1)
    if _challenge_paths(replacement) != (*expected, dummy_path):
        raise ValueError(f"{label} dummy registration patch failed")
    return replacement


def _plan_b_registration_override(registry: dict) -> tuple[str, tuple[dict, ...]]:
    source = _source(
        AGGREGATE_SOURCE_OWNER,
        AGGREGATE_LIST_PATH,
        AGGREGATE_LIST_SHA256,
    ).replace("\r\n", "\n")
    contracts = _plan_b_registration_contracts(registry)
    audited_contracts = []
    for contract in contracts:
        plan = contract["plan"]
        _assert_dummy_candidate(plan)
        expected = tuple(contract["unlockables"])
        matches = [
            (index, block)
            for index, _, _, block in _level_blocks(source)
            if _challenge_paths(block) == expected and "_dev_" not in block
        ]
        if len(matches) != 1:
            raise ValueError(
                f"{contract['name']}: canonical registration count is {len(matches)}"
            )
        level_index, block = matches[0]
        replacement = _append_dummy_to_registration(
            block, expected, plan["dummy_path"], plan["label"]
        )
        source = source.replace(block, replacement, 1)
        audited_contracts.append({
            **{key: value for key, value in contract.items() if key != "plan"},
            "level_index": level_index,
            "real_challenges": expected,
            "dummy": {
                "path": plan["dummy_path"],
                "source_owner": DHB_DUMMY_SOURCE_OWNER,
                "decl_path": plan["decl_path"],
                "sha256": plan["sha256"],
                "inherit": "mission_challenge/dlc1_challenge_base",
                "stat": plan["stat"],
                "count": plan["count"],
                "duration": "DUR_CUSTOM_LEVEL",
                "unlockable_flags": "UNLOCKABLE_FLAG_HORDE_CHALLENGE",
                "violence_event": plan.get("violence_event"),
                "violence_event_decl_path": plan.get("violence_event_decl_path"),
                "violence_event_sha256": plan.get("violence_event_sha256"),
                "required_monster": plan["required_monster"],
                "impossibility": plan["impossibility"],
                "existing_vanilla_reference": "Horde e6m3 registration",
                "reward": None,
                "ap_location": None,
            },
        })
    return source, tuple(audited_contracts)


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
    source_equivalent = override.replace(REWARD_FIELD, "", 1)
    if source_equivalent != source:
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
    aggregate_override, plan_b_contracts = _plan_b_registration_override(registry)
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
        "registration_experiment": {
            "strategy": "append_existing_mission_safe_impossible_horde_challenges",
            "source_owner": AGGREGATE_SOURCE_OWNER,
            "target_owner": AGGREGATE_TARGET_OWNER,
            "source_path": AGGREGATE_LIST_PATH,
            "mission_count": len(plan_b_contracts),
            "contracts": plan_b_contracts,
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
