"""Canonical registry for native Mission Complete and Weapon Mastery locations."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = ROOT / "data" / "challenge_location_registry.json"
NATIVE_MASTERY_MANAGER = "UnlockableManager_0_1_2/idUnlockableManager_2"
BASE_MASTERY_LOCATION_IDS = frozenset(range(7770125, 7770138))


def canonical_map_name(name: str | None) -> str | None:
    if not name:
        return name
    normalized = str(name).strip().replace("\\", "/").rstrip("/")
    return "game/hub/hub" if normalized in {"game/hub/hub", "game/sp/hub/hub"} else normalized


def load_challenge_registry(path: Path = REGISTRY_PATH) -> dict:
    registry = json.loads(path.read_text(encoding="utf-8"))
    for entry in registry.get("mission_complete", []):
        signal = entry.get("signal", {})
        signal["from"] = canonical_map_name(signal.get("from"))
        signal["to"] = canonical_map_name(signal.get("to"))
    validate_challenge_registry(registry)
    return registry


def all_location_entries(registry: dict) -> list[dict]:
    return [*registry["mission_complete"], *registry.get("weapon_masteries", [])]


def mastery_entry_by_unlockable(registry: dict) -> dict[str, dict]:
    return {
        entry["signal"]["unlockable"]: entry
        for entry in registry["weapon_masteries"]
    }


def validate_challenge_registry(registry: dict) -> None:
    if registry.get("schema_version") != 5:
        raise ValueError("runtime registry schema_version must be 5")
    entries = all_location_entries(registry)
    if len(entries) != 16:
        raise ValueError("expected three Mission Complete and thirteen Weapon Mastery locations")
    names = [entry.get("name") for entry in entries]
    ids = [entry.get("location_id") for entry in entries]
    if None in names or len(names) != len(set(names)):
        raise ValueError("runtime location names must be unique")
    if None in ids or len(ids) != len(set(ids)):
        raise ValueError("runtime location IDs must be unique")
    if set(ids) != {7770122, 7770123, 7770124, *BASE_MASTERY_LOCATION_IDS}:
        raise ValueError("runtime IDs must use the reserved Mission/Mastery range")

    for entry in registry["mission_complete"]:
        signal = entry.get("signal", {})
        if signal.get("kind") != "native_transition" or not signal.get("from") or not signal.get("to"):
            raise ValueError(f"{entry['name']}: invalid native transition signal")
        if signal["to"] == "game/sp/hub/hub":
            raise ValueError(f"{entry['name']}: noncanonical Hub alias")

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
