"""Canonical registry for native mission and Weapon Mastery locations."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = ROOT / "data" / "challenge_location_registry.json"
NATIVE_MASTERY_MANAGER = "UnlockableManager_0_1_2/idUnlockableManager_2"
BASE_MASTERY_LOCATION_IDS = frozenset(range(7770125, 7770138))
E1M3_CHALLENGE_LOCATION_IDS = frozenset(range(7770138, 7770141))
E1M4_CHALLENGE_LOCATION_IDS = frozenset(range(7770172, 7770175))
MISSION_CHALLENGE_LOCATION_IDS = E1M3_CHALLENGE_LOCATION_IDS | E1M4_CHALLENGE_LOCATION_IDS
ALL_MISSION_CHALLENGES_LOCATION_IDS = frozenset({7770141, 7770175})


def canonical_map_name(name: str | None) -> str | None:
    if not name:
        return name
    normalized = str(name).strip().replace("\\", "/").rstrip("/")
    if normalized in {"game/hub/hub", "game/sp/hub/hub"}:
        return "game/hub/hub"
    return normalized


def load_challenge_registry(path: Path = REGISTRY_PATH) -> dict:
    registry = json.loads(path.read_text(encoding="utf-8"))
    for entry in registry.get("mission_complete", []):
        signal = entry.get("signal", {})
        if signal.get("kind") == "native_transition":
            signal["from"] = canonical_map_name(signal.get("from"))
            signal["to"] = canonical_map_name(signal.get("to"))
        elif signal.get("kind") == "map_terminal":
            signal["runtime_map"] = canonical_map_name(signal.get("runtime_map"))
    validate_challenge_registry(registry)
    return registry


def all_location_entries(registry: dict) -> list[dict]:
    entries = [
        *registry["mission_complete"],
        *registry.get("weapon_masteries", []),
        *registry.get("mission_challenges", []),
    ]
    for aggregate in registry.get("all_mission_challenges", []):
        entries.append(aggregate)
    return entries


def mastery_entry_by_unlockable(registry: dict) -> dict[str, dict]:
    return {
        entry["signal"]["unlockable"]: entry
        for entry in registry["weapon_masteries"]
    }


def mission_challenge_entry_by_unlockable(registry: dict) -> dict[str, dict]:
    return {
        entry["signal"]["unlockable"]: entry
        for entry in registry["mission_challenges"]
    }


def all_mission_challenge_entries(registry: dict) -> list[dict]:
    """Return all aggregate entries keyed by the unlockable paths they cover."""
    return list(registry.get("all_mission_challenges", []))


def validate_challenge_registry(registry: dict) -> None:
    if registry.get("schema_version") != 9:
        raise ValueError("runtime registry schema_version must be 9")
    entries = all_location_entries(registry)
    expected_count = (
        4   # mission_complete
        + 13  # weapon_masteries
        + 6   # mission_challenges (3 e1m3 + 3 e1m4)
        + 2   # all_mission_challenges (e1m3 aggregate + e1m4 aggregate)
    )
    if len(entries) != expected_count:
        raise ValueError(
            f"expected {expected_count} location entries, got {len(entries)}"
        )
    names = [entry.get("name") for entry in entries]
    ids = [entry.get("location_id") for entry in entries]
    if None in names or len(names) != len(set(names)):
        raise ValueError("runtime location names must be unique")
    if None in ids or len(ids) != len(set(ids)):
        raise ValueError("runtime location IDs must be unique")
    if set(ids) != {
        7770122, 7770123, 7770124, 7770162,
        *BASE_MASTERY_LOCATION_IDS,
        *MISSION_CHALLENGE_LOCATION_IDS,
        *ALL_MISSION_CHALLENGES_LOCATION_IDS,
    }:
        raise ValueError("runtime IDs must use the reserved mission/Mastery range")

    for entry in registry["mission_complete"]:
        signal = entry.get("signal", {})
        if entry["location_id"] in {7770122, 7770123, 7770162}:
            if set(signal) != {"kind", "runtime_map"} or signal["kind"] != "map_terminal":
                raise ValueError(f"{entry['name']}: invalid map terminal signal")
            if signal["runtime_map"] not in {
                "game/sp/e1m1_intro/e1m1_intro",
                "game/sp/e1m2_battle/e1m2_battle",
                "game/sp/e1m4_boss/e1m4_boss",
            }:
                raise ValueError(f"{entry['name']}: invalid runtime map identity")
            continue
        if signal.get("kind") != "native_transition" or not signal.get("from") or not signal.get("to"):
            raise ValueError(f"{entry['name']}: invalid native transition signal")
        if signal["to"] == "game/sp/hub/hub":
            raise ValueError(f"{entry['name']}: noncanonical Hub alias")

    mission_challenges = registry.get("mission_challenges", [])
    if len(mission_challenges) != 6:
        raise ValueError("expected exactly six Mission Challenges (3 e1m3 + 3 e1m4)")
    challenge_paths = set()
    completion_stats = set()
    expected_global_ids = iter(range(7770138, 7770141))
    expected_e1m4_ids = iter(range(7770172, 7770175))

    for index, entry in enumerate(mission_challenges):
        if index < 3:
            if entry["location_id"] != next(expected_global_ids):
                raise ValueError(f"{entry['name']}: E1M3 Mission Challenge ID order drift")
            mission_prefix = "e1m3"
            completion_prefix = "E1M3"
        else:
            if entry["location_id"] != next(expected_e1m4_ids):
                raise ValueError(f"{entry['name']}: E1M4 Mission Challenge ID order drift")
            mission_prefix = "e1m4"
            completion_prefix = "E1M4"

        signal = entry.get("signal", {})
        required = {
            "kind", "manager", "unlockable", "numUnlockableRules",
            "rule_0_statname", "rule_0_statDuration", "rule_0_satisfied",
            "unlockableIsUnlocked",
        }
        if set(signal) != required or signal["kind"] != "unlockable_record":
            raise ValueError(f"{entry.get('name')}: incomplete native challenge signal")
        if signal["manager"] != NATIVE_MASTERY_MANAGER:
            raise ValueError(f"{entry['name']}: unexpected native unlockable manager")
        challenge_num = (index % 3) + 1
        expected_path = f"mission_challenge/{mission_prefix}/challenge_{challenge_num}"
        if signal["unlockable"] != expected_path or signal["unlockable"] in challenge_paths:
            raise ValueError(f"{entry['name']}: unexpected or duplicate challenge path")
        challenge_paths.add(signal["unlockable"])
        if signal["numUnlockableRules"] != 1 or signal["rule_0_statDuration"] != 5:
            raise ValueError(f"{entry['name']}: unexpected native challenge rule shape")
        if signal["rule_0_satisfied"] is not True or signal["unlockableIsUnlocked"] is not True:
            raise ValueError(f"{entry['name']}: durable completion fields must be true")

        completion_owner = entry.get("completion_owner", {})
        if completion_owner.get("path") != f"unlockable/{expected_path}.decl":
            raise ValueError(f"{entry['name']}: invalid completion owner path")
        completion_stat = completion_owner.get("completion_stat")
        expected_stat = f"STAT_COMPLETED_{completion_prefix}_CHALLENGE_{challenge_num}"
        if completion_stat != expected_stat:
            raise ValueError(f"{entry['name']}: invalid completion stat owner: {completion_stat}")
        if completion_stat in completion_stats:
            raise ValueError(f"{entry['name']}: duplicate completion stat")
        completion_stats.add(completion_stat)
        if len(str(completion_owner.get("sha256", ""))) != 64:
            raise ValueError(f"{entry['name']}: completion owner is not hash locked")

        reward_owner = entry.get("reward_owner", {})
        if reward_owner != {
            "inherited_path": "unlockable/mission_challenge/challenge_base.decl",
            "sha256": "2f5905b716eef48dfad260e9f71ab6a0a8c9bd254515f3c97ff1ef01b09fdb34",
            "currency": "CURRENCY_PRAETOR_UPGRADE",
        }:
            raise ValueError(f"{entry['name']}: invalid inherited Suit Point owner")

    aggregates = registry.get("all_mission_challenges", [])
    if len(aggregates) != 2:
        raise ValueError("expected exactly two All Mission Challenges aggregates")
    expected_mission_keys = {"e1m3", "e1m4"}
    mission_keys = [aggregate.get("mission_key") for aggregate in aggregates]
    if any(not isinstance(key, str) or not key.strip() for key in mission_keys):
        raise ValueError("all Mission Challenges aggregates require non-empty mission_key")
    duplicate_mission_keys = [
        key for key, count in Counter(mission_keys).items() if count > 1
    ]
    if duplicate_mission_keys:
        raise ValueError(f"duplicate mission_key: {sorted(duplicate_mission_keys)}")
    if set(mission_keys) != expected_mission_keys:
        raise ValueError("all Mission Challenges must cover exactly e1m3 and e1m4")

    challenge_missions = {
        entry["signal"]["unlockable"]: entry["signal"]["unlockable"].split("/")[1]
        for entry in mission_challenges
    }
    aggregate_unlockables: list[str] = []
    for aggregate in aggregates:
        mission_key = aggregate["mission_key"]
        if aggregate.get("challenges") is not None:
            raise ValueError(f"{aggregate['name']}: deprecated challenges field must be removed")
        unlockables = aggregate.get("signal", {}).get("unlockables", [])
        if not isinstance(unlockables, list) or len(unlockables) != 3:
            raise ValueError(f"{aggregate['name']}: expected exactly 3 unlockable paths")
        location_id = aggregate["location_id"]
        if location_id not in ALL_MISSION_CHALLENGES_LOCATION_IDS:
            raise ValueError(f"{aggregate['name']}: unexpected aggregate location ID")
        if aggregate["signal"]["kind"] != "all_mission_challenge_records":
            raise ValueError(f"{aggregate['name']}: unexpected aggregate signal kind")
        for unlockable in unlockables:
            if unlockable not in challenge_missions:
                raise ValueError(f"{aggregate['name']}: unknown unlockable path: {unlockable}")
            if challenge_missions[unlockable] != mission_key:
                raise ValueError(f"{aggregate['name']}: unlockable belongs to another mission_key")
        aggregate_unlockables.extend(unlockables)

    unlockable_counts = Counter(aggregate_unlockables)
    duplicates = [path for path, count in unlockable_counts.items() if count > 1]
    if duplicates:
        raise ValueError(f"aggregate unlockables are duplicated: {sorted(duplicates)}")
    if set(aggregate_unlockables) != set(challenge_missions):
        raise ValueError("aggregate unlockables must cover every Mission Challenge exactly once")

    masteries = registry.get("weapon_masteries", [])
    if len(masteries) != 13:
        raise ValueError("expected exactly thirteen proven base Weapon Masteries")
    unlockables = set()
    item_ids = set()
    perks = set()
    for entry in masteries:
        signal = entry.get("signal", {})
        required = {
            "kind", "manager", "unlockable", "numUnlockableRules",
            "rule_0_statname", "rule_0_statCount", "rule_0_statDuration",
            "rule_0_satisfied", "unlockableIsUnlocked",
        }
        if set(signal) != required or signal["kind"] != "unlockable_record":
            raise ValueError(f"{entry.get('name')}: incomplete native mastery signal")
        if signal["manager"] != NATIVE_MASTERY_MANAGER:
            raise ValueError(f"{entry['name']}: unexpected native unlockable manager")
        if signal["numUnlockableRules"] != 1 or signal["rule_0_statCount"] <= 0:
            raise ValueError(f"{entry['name']}: unsupported native mastery rule shape")
        if signal["rule_0_statDuration"] != 4:
            raise ValueError(f"{entry['name']}: expected DUR_CUSTOM save enum 4")
        if signal["rule_0_satisfied"] is not True or signal["unlockableIsUnlocked"] is not True:
            raise ValueError(f"{entry['name']}: completion fields must be true")
        if not signal["unlockable"].startswith("weapon_mastery/"):
            raise ValueError(f"{entry['name']}: not a base-game mastery path")
        if signal["unlockable"] in unlockables:
            raise ValueError(f"duplicate native mastery path: {signal['unlockable']}")
        unlockables.add(signal["unlockable"])
        if not isinstance(entry.get("item_id"), int) or entry["item_id"] in item_ids:
            raise ValueError(f"{entry['name']}: invalid or duplicate AP Mastery item ID")
        item_ids.add(entry["item_id"])
        perk = entry.get("gameplay_perk")
        if not entry.get("typed_perk_delivery_valid") or not isinstance(perk, str) or perk in perks:
            raise ValueError(f"{entry['name']}: unsupported typed perk delivery")
        perks.add(perk)
        decls = entry.get("decls", {})
        for kind, prefix in (("unlockable", "unlockable/weapon_mastery/"), ("perk", "perks/perk/player/weapons/")):
            locked = decls.get(kind, {})
            if not isinstance(locked.get("path"), str) or not locked["path"].startswith(prefix):
                raise ValueError(f"{entry['name']}: invalid {kind} override path")
            if len(str(locked.get("sha256", ""))) != 64:
                raise ValueError(f"{entry['name']}: {kind} source is not hash locked")
