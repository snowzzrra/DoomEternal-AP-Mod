"""Compatibility facade for the JSON-authored runtime challenge registry."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from types import MappingProxyType
from typing import Any

from doom_eap.content.content_catalog import RUNTIME_STRATEGIES


REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT = REPO_ROOT
REGISTRY_PATH = REPO_ROOT / "data" / "challenge_location_registry.json"
MISSION_CHALLENGE_RUNTIME_MAP_BY_MISSION_KEY = {
    "e1m3": "game/sp/e1m3_cult/e1m3_cult",
    "e1m4": "game/sp/e1m4_boss/e1m4_boss",
    "e2m1": "game/sp/e2m1_nest/e2m1_nest",
    "e2m2": "game/sp/e2m2_base/e2m2_base",
    "e2m3": "game/sp/e2m3_core/e2m3_core",
    "e3m1_slayer": "game/sp/e3m1_slayer/e3m1_slayer",
    "e3m2_hell": "game/sp/e3m2_hell/e3m2_hell",
    "e3m2_hell_b": "game/sp/e3m2_hell_b/e3m2_hell_b",
    "e3m3_maykr": "game/sp/e3m3_maykr/e3m3_maykr",
}


def canonical_map_name(name: str | None) -> str | None:
    if not name:
        return name
    normalized = str(name).strip().replace("\\", "/").rstrip("/")
    return "game/hub/hub" if normalized in {"game/hub/hub", "game/sp/hub/hub"} else normalized


def _thaw(value: Any) -> Any:
    if isinstance(value, (dict, MappingProxyType)):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def challenge_registry_document(catalog=None) -> dict:
    if catalog is None:
        from doom_eap.content.content_catalog import load_content_catalog
        catalog = load_content_catalog()
    registry = {
        "schema_version": 10,
        "mission_complete": [],
        "weapon_masteries": [],
        "mission_challenges": [],
        "all_mission_challenges": [],
    }
    for item in catalog.runtime_locations:
        if not item.category:
            raise ValueError(f"{item.name}: runtime location lacks category")
        data = _thaw(item.data)
        if item.category == "mission_challenges":
            runtime_map = MISSION_CHALLENGE_RUNTIME_MAP_BY_MISSION_KEY.get(item.mission_key)
            if runtime_map is not None:
                data.setdefault("runtime_map", runtime_map)
        registry[item.category].append(data)
    return registry


def load_challenge_registry(path: Path | None = None) -> dict:
    if path is None and (ROOT / "content" / "maps").is_dir():
        registry = challenge_registry_document()
    else:
        registry_path = path or REGISTRY_PATH
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    for entry in registry.get("mission_complete", []):
        signal = entry.get("signal", {})
        for field in ("from", "to", "runtime_map"):
            if field in signal:
                signal[field] = canonical_map_name(signal[field])
    validate_challenge_registry(registry)
    return registry


def all_location_entries(registry: dict) -> list[dict]:
    return [
        *registry.get("mission_complete", []),
        *registry.get("weapon_masteries", []),
        *registry.get("mission_challenges", []),
        *registry.get("all_mission_challenges", []),
    ]


def mastery_entry_by_unlockable(registry: dict) -> dict[str, dict]:
    return {entry["signal"]["unlockable"]: entry for entry in registry.get("weapon_masteries", [])}


def mission_challenge_entry_by_unlockable(registry: dict) -> dict[str, dict]:
    return {entry["signal"]["unlockable"]: entry for entry in registry.get("mission_challenges", [])}


def all_mission_challenge_entries(registry: dict) -> list[dict]:
    return list(registry.get("all_mission_challenges", []))


def aggregate_ready(signal: dict, checked_locations: set[int]) -> bool:
    if signal.get("authority") != "server_checked_locations":
        raise ValueError("aggregate authority must be server_checked_locations")
    children = set(signal.get("children", []))
    required_count = signal.get("required_count")
    if not children or not isinstance(required_count, int):
        raise ValueError("aggregate requires children and required_count")
    return len(children & set(checked_locations)) >= required_count


def _mission_key(entry: dict) -> str | None:
    if entry.get("mission_key"):
        return entry["mission_key"]
    unlockable = entry.get("signal", {}).get("unlockable", "")
    pieces = unlockable.split("/")
    return pieces[1] if len(pieces) > 2 and pieces[0] == "mission_challenge" else None


def validate_challenge_registry(registry: dict) -> None:
    """Validate generic registry invariants; mission count is deliberately data-driven."""
    entries = all_location_entries(registry)
    if not entries:
        raise ValueError("runtime registry has no locations")
    names = [entry.get("name") for entry in entries]
    ids = [entry.get("location_id") for entry in entries]
    if any(not isinstance(name, str) or not name.strip() for name in names) or len(names) != len(set(names)):
        raise ValueError("runtime location names must be unique")
    if any(not isinstance(value, int) or isinstance(value, bool) for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("runtime location IDs must be unique integers")

    mission_challenges = registry.get("mission_challenges", [])
    challenge_by_unlockable: dict[str, dict] = {}
    challenge_missions: dict[str, str] = {}
    completion_stats: set[str] = set()
    for entry in mission_challenges:
        signal = entry.get("signal", {})
        kind = signal.get("kind")
        if kind not in {"unlockable_record", "physical_event_equivalent"}:
            raise ValueError(f"{entry['name']}: unknown challenge strategy")
        unlockable = signal.get("unlockable")
        mission_key = _mission_key(entry)
        if not isinstance(unlockable, str) or not unlockable or not mission_key or unlockable in challenge_by_unlockable:
            raise ValueError(f"{entry['name']}: invalid or duplicate challenge unlockable")
        runtime_map = entry.get("runtime_map")
        expected_runtime_map = MISSION_CHALLENGE_RUNTIME_MAP_BY_MISSION_KEY.get(mission_key)
        if not isinstance(runtime_map, str) or not runtime_map.strip():
            raise ValueError(f"{entry['name']}: mission challenge runtime_map is required")
        if canonical_map_name(runtime_map) != runtime_map:
            raise ValueError(f"{entry['name']}: mission challenge runtime_map is not canonical")
        if runtime_map not in set(MISSION_CHALLENGE_RUNTIME_MAP_BY_MISSION_KEY.values()):
            raise ValueError(f"{entry['name']}: mission challenge runtime_map is unknown")
        if expected_runtime_map is None or runtime_map != expected_runtime_map:
            raise ValueError(f"{entry['name']}: mission challenge runtime_map does not match mission_key")
        challenge_by_unlockable[unlockable] = entry
        challenge_missions[unlockable] = mission_key
        sources = signal.get("physical_location_ids", [])
        if kind == "physical_event_equivalent":
            if not isinstance(sources, list) or not sources or any(not isinstance(item, int) or isinstance(item, bool) for item in sources) or len(sources) != len(set(sources)):
                raise ValueError(f"{entry['name']}: invalid physical_location_ids")
            required = signal.get("required_count", 1)
            if not isinstance(required, int) or isinstance(required, bool) or not 1 <= required <= len(sources):
                raise ValueError(f"{entry['name']}: invalid required_count")
        completion = entry.get("completion_owner", {})
        stat = completion.get("completion_stat")
        if not isinstance(stat, str) or not stat or stat in completion_stats:
            raise ValueError(f"{entry['name']}: invalid or duplicate completion stat")
        completion_stats.add(stat)

    aggregate_children: list[int] = []
    aggregate_missions: list[str] = []
    aggregate_entries = registry.get("all_mission_challenges", [])
    declared_missions = [_mission_key(entry) for entry in aggregate_entries]
    if any(not mission_key for mission_key in declared_missions) or len(declared_missions) != len(set(declared_missions)):
        raise ValueError("duplicate mission_key")
    for entry in aggregate_entries:
        signal = entry.get("signal", {})
        mission_key = _mission_key(entry)
        children = signal.get("children")
        if signal.get("kind") not in {"aggregate", "all_mission_challenge_records"} or not mission_key:
            raise ValueError(f"{entry['name']}: invalid aggregate strategy")
        if not isinstance(children, list) or not children or len(children) != len(set(children)):
            raise ValueError(f"{entry['name']}: aggregate requires unique children")
        challenge_ids_for_mission = {
            child["location_id"]
            for child in registry.get("mission_challenges", [])
            if _mission_key(child) == mission_key
        }
        if set(children) != challenge_ids_for_mission:
            raise ValueError(f"{entry['name']}: aggregate children must match the mission AP locations")
        if signal.get("required_count") != len(children):
            raise ValueError(f"{entry['name']}: aggregate required_count drift")
        if signal.get("authority") != "server_checked_locations":
            raise ValueError(f"{entry['name']}: aggregate authority must be server_checked_locations")
        for child in children:
            if child not in {item["location_id"] for item in registry.get("mission_challenges", [])}:
                raise ValueError(f"{entry['name']}: aggregate child is missing or from another mission")
        aggregate_missions.append(mission_key)
        aggregate_children.extend(children)
    if len(aggregate_children) != len(set(aggregate_children)):
        raise ValueError("aggregate children are duplicated")
    if set(aggregate_children) != {
        entry["location_id"] for entry in registry.get("mission_challenges", [])
    }:
        raise ValueError("aggregates must cover every mission challenge exactly once")

    for entry in registry.get("mission_complete", []):
        signal = entry.get("signal", {})
        if signal.get("kind") not in {"native_transition", "map_terminal"}:
            raise ValueError(f"{entry['name']}: unknown Mission Complete strategy")
        if signal["kind"] == "native_transition" and (not signal.get("from") or not signal.get("to")):
            raise ValueError(f"{entry['name']}: native transition lacks endpoints")
        if signal["kind"] == "map_terminal" and not signal.get("runtime_map"):
            raise ValueError(f"{entry['name']}: terminal lacks runtime map")

    for entry in registry.get("weapon_masteries", []):
        signal = entry.get("signal", {})
        if signal.get("kind") != "unlockable_record" or not isinstance(entry.get("item_id"), int):
            raise ValueError(f"{entry['name']}: invalid mastery record")
        if not signal.get("unlockable") or not signal.get("manager"):
            raise ValueError(f"{entry['name']}: incomplete mastery signal")
    strategies = {entry.get("signal", {}).get("kind") for entry in entries}
    if not strategies <= RUNTIME_STRATEGIES:
        raise ValueError(f"unknown runtime strategies: {sorted(strategies - RUNTIME_STRATEGIES)}")
