"""Portable validation for versioned mod content only.

No sibling APWorld, staging directory, vanilla maps, build output, or release
artifact may be read or written by this module.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from doom_eap.content.content_catalog import load_content_catalog

ROOT = Path(__file__).resolve().parents[2]
JSON_ROOTS = ("content", "data", "level_configs", "manifests")
IDENTITY_FIELDS = {
    "apworld_revision": str,
    "bridge_protocol_version": int,
    "compiler_revision": int,
    "content_revision": str,
    "content_schema_version": int,
    "game": str,
    "manifest_schema_version": int,
    "release_version": str,
    "session_mod_contract_revision": int,
    "slot_data_revision": str,
    "slot_data_schema_version": int,
}


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{path.relative_to(ROOT)}: {error}") from error


def check_authorial() -> dict[str, int]:
    json_paths = sorted(path for root_name in JSON_ROOTS for path in (ROOT / root_name).rglob("*.json"))
    for path in json_paths:
        read_json(path)

    identity = read_json(ROOT / "data/content_identity.json")
    if set(identity) != set(IDENTITY_FIELDS):
        raise ValueError("data/content_identity.json fields diverge from the 0.5.1 release contract")
    for field, expected_type in IDENTITY_FIELDS.items():
        value = identity[field]
        if not isinstance(value, expected_type) or isinstance(value, bool):
            raise ValueError(f"data/content_identity.json: invalid {field}")
        if expected_type is int and value <= 0:
            raise ValueError(f"data/content_identity.json: {field} must be positive")
    if identity["game"] != "DOOM Eternal":
        raise ValueError("data/content_identity.json: invalid game identity")
    if identity["apworld_revision"] != "0.5.0":
        raise ValueError("data/content_identity.json: invalid official APWorld revision")
    if identity["content_revision"] != "0.5.0":
        raise ValueError("data/content_identity.json: invalid content revision")
    if identity["release_version"] != "0.5.1":
        raise ValueError("data/content_identity.json: invalid release revision")
    if not identity["slot_data_revision"]:
        raise ValueError("data/content_identity.json: slot_data_revision must not be empty")

    campaign_goal = read_json(ROOT / "data/campaign_goal_contract.json")
    if campaign_goal.get("release") != identity["release_version"]:
        raise ValueError("data/campaign_goal_contract.json: release diverges from content identity")

    catalog = load_content_catalog(ROOT)
    location_ids = [location.location_id for location in (*catalog.physical_locations, *catalog.runtime_locations)]
    if any(location_id < 7_770_000 or location_id >= 7_780_000 for location_id in location_ids):
        raise ValueError("location ID outside reserved DOOM Eternal range")

    commands = read_json(ROOT / "data/items.json")
    item_ids = [int(item_id) for item_id in commands]
    if len(item_ids) != len(set(item_ids)):
        raise ValueError("duplicate item IDs")
    if any(item_id < 7_770_000 or item_id >= 7_780_000 for item_id in item_ids):
        raise ValueError("item ID outside reserved DOOM Eternal range")

    expected_manifests: dict[str, dict[str, int]] = {}
    for map_key, spec in catalog.maps.items():
        config = read_json(spec.level_config_path)
        expected = dict(config.get("entities", {}))
        expected.update(
            {encounter["ap_check"]: encounter["location_id"] for encounter in config.get("secret_encounters", [])}
        )
        expected_manifests[map_key] = expected
        actual = read_json(ROOT / "manifests" / f"{map_key}.json")
        if actual != expected:
            raise ValueError(f"manifests/{map_key}.json is stale")

    projected_names = read_json(ROOT / "data/location_names.json").get("locations", {})
    expected_names = {
        str(location.location_id): location.name
        for location in (*catalog.physical_locations, *catalog.runtime_locations)
    }
    if projected_names != expected_names:
        raise ValueError("data/location_names.json is stale")

    return {
        "json_files": len(json_paths),
        "items": len(item_ids),
        "locations": len(location_ids),
        "maps": len(expected_manifests),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-authorial", action="store_true", required=True)
    parser.parse_args(argv)
    counts = check_authorial()
    print("authorial smoke passed: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
