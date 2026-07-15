"""Canonical registry for active native Mission Complete locations."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = ROOT / "data" / "challenge_location_registry.json"


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
    return list(registry["mission_complete"])


def validate_challenge_registry(registry: dict) -> None:
    if registry.get("schema_version") != 2:
        raise ValueError("mission registry schema_version must be 2")
    entries = all_location_entries(registry)
    if len(entries) != 3:
        raise ValueError("expected exactly three active Mission Complete locations")
    if {entry.get("id") for entry in entries} != {7770122, 7770123, 7770124}:
        raise ValueError("Mission Complete IDs must use the reserved location-only range")
    names = [entry.get("name") for entry in entries]
    ids = [entry.get("id") for entry in entries]
    if None in names or len(names) != len(set(names)):
        raise ValueError("Mission Complete names must be unique")
    if None in ids or len(ids) != len(set(ids)):
        raise ValueError("Mission Complete IDs must be unique")
    for entry in entries:
        signal = entry.get("signal", {})
        if signal.get("kind") != "native_transition" or not signal.get("from") or not signal.get("to"):
            raise ValueError(f"{entry['name']}: invalid native transition signal")
        if signal["to"] == "game/sp/hub/hub":
            raise ValueError(f"{entry['name']}: noncanonical Hub alias")
