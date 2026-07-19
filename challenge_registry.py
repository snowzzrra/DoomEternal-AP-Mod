"""Canonical registry for native mission and Weapon Mastery locations."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = ROOT / "data" / "challenge_location_registry.json"
NATIVE_MASTERY_MANAGER = "UnlockableManager_0_1_2/idUnlockableManager_2"
BASE_MASTERY_LOCATION_IDS = frozenset(range(7770125, 7770138))
MISSION_CHALLENGE_LOCATION_IDS = frozenset(range(7770138, 7770141))
ALL_MISSION_CHALLENGES_LOCATION_ID = 7770141


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
    return [
        *registry["mission_complete"],
        *registry.get("weapon_masteries", []),
        *registry.get("mission_challenges", []),
        registry["all_mission_challenges"],
    ]


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


def validate_challenge_registry(registry: dict) -> None:
    if registry.get("schema_version") != 7:
        raise ValueError("runtime registry schema_version must be 7")
    entries = all_location_entries(registry)
    if len(entries) != 21:
        raise ValueError(
            "expected four Mission Complete, thirteen Weapon Mastery, and "
            "four Cultist Base Mission Challenge locations"
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
        ALL_MISSION_CHALLENGES_LOCATION_ID,
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
    if len(mission_challenges) != 3:
        raise ValueError("expected exactly three Cultist Base Mission Challenges")
    challenge_paths = set()
    completion_stats = set()
    expected_challenge_ids = iter(sorted(MISSION_CHALLENGE_LOCATION_IDS))
    for index, entry in enumerate(mission_challenges, start=1):
        if entry["location_id"] != next(expected_challenge_ids):
            raise ValueError(f"{entry['name']}: Mission Challenge ID order drift")
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
        expected_path = f"mission_challenge/e1m3/challenge_{index}"
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
        if completion_stat != f"STAT_COMPLETED_E1M3_CHALLENGE_{index}":
            raise ValueError(f"{entry['name']}: invalid completion stat owner")
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

    aggregate = registry.get("all_mission_challenges")
    expected_aggregate = {
        "name": "Cultist Base - All Mission Challenges Completed",
        "location_id": ALL_MISSION_CHALLENGES_LOCATION_ID,
        "signal": {
            "kind": "all_mission_challenge_records",
            "unlockables": [
                entry["signal"]["unlockable"] for entry in mission_challenges
            ],
        },
    }
    if aggregate != expected_aggregate:
        raise ValueError(
            "All Mission Challenges must derive only from the three exact "
            "native Mission Challenge records"
        )

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
