#!/usr/bin/env python3
"""Build the canonical item-notification string table from generated maps."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from tools.maps.notification_formatting import notification_key, notification_text
from tools.maps.notification_lab import (
    notification_lab_enabled,
    notification_lab_string_entries,
)

HEADER_KEY_PATTERN = re.compile(r'header\s*=\s*"(#str_ap_notify_item_\d+(?:_\d+)?)";')
LAB_HEADER_KEY_PATTERN = re.compile(
    r'header\s*=\s*"(#str_ap_notification_lab_[a-z_]+)";'
)
LOCATION_KEY_PATTERN = re.compile(
    r'(?:header|subtext)\s*=\s*"(#str_ap_location_(?:sent|\d+))";'
)
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def referenced_notification_keys(maps_dir: Path) -> set[str]:
    map_paths = sorted(maps_dir.rglob("*.entities"))
    if not map_paths:
        raise ValueError(f"no generated maps found in {maps_dir}")
    return {
        key
        for path in map_paths
        for key in (
            HEADER_KEY_PATTERN.findall(path.read_text(encoding="utf-8"))
            + LAB_HEADER_KEY_PATTERN.findall(path.read_text(encoding="utf-8"))
            + LOCATION_KEY_PATTERN.findall(path.read_text(encoding="utf-8"))
        )
    }


def string_entries(entries: list[tuple[str, str]]) -> list[dict[str, str]]:
    """Validate and serialize the strict BLang list schema deterministically."""
    names = set()
    result = []
    for name, text in sorted(entries, key=lambda entry: entry[0]):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("notification string name cannot be empty")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"notification string text cannot be empty: {name}")
        if CONTROL_CHARACTERS.search(name) or CONTROL_CHARACTERS.search(text):
            raise ValueError(f"notification string contains a control character: {name}")
        if name in names:
            raise ValueError(f"duplicate notification keys: {[name]}")
        names.add(name)
        result.append({"name": name, "text": text})
    return result


def build_string_table(
    items_path: Path,
    policies_path: Path,
    maps_dir: Path,
    output_path: Path,
    location_names_path: Path | None = None,
) -> None:
    items = json.loads(items_path.read_text(encoding="utf-8"))
    policies = json.loads(policies_path.read_text(encoding="utf-8"))
    item_names = {
        int(item_id): entry["name"]
        for item_id, entry in policies.get("items", {}).items()
        if "name" in entry
    }

    referenced_keys = referenced_notification_keys(maps_dir)
    entries: list[tuple[str, str]] = []
    for raw_item_id, definition in sorted(items.items(), key=lambda entry: int(entry[0])):
        if isinstance(definition, dict) and definition.get("type") == "no_op":
            continue
        item_id = int(raw_item_id)
        try:
            item_name = item_names[item_id]
        except KeyError as error:
            raise ValueError(f"item {item_id} has no notification name") from error
        stages = range(len(definition["perks"])) if (
            isinstance(definition, dict) and definition.get("type") == "progressive_perk"
        ) else (None,)
        for stage in stages:
            key = notification_key(item_id, definition, stage=stage)
            if key in referenced_keys:
                entries.append((
                    key,
                    notification_text(
                        item_id, definition, item_name, stage=stage
                    ),
                ))

    if notification_lab_enabled():
        entries.extend(notification_lab_string_entries(output_path.stem))

    location_keys = {
        key for key in referenced_keys if key.startswith("#str_ap_location_")
    }
    if location_keys:
        if location_names_path is None:
            location_names_path = (
                Path(__file__).resolve().parents[2]
                / "data"
                / "location_names.json"
            )
        location_identity = json.loads(
            location_names_path.read_text(encoding="utf-8")
        )
        if location_identity.get("schema_version") != 1:
            raise ValueError("unsupported location-name schema")
        location_names = location_identity.get("locations", {})
        sent_text = {
            "english": "AP Location Sent",
            "portuguese": "Localização AP enviada",
        }
        try:
            entries.append(("#str_ap_location_sent", sent_text[output_path.stem]))
        except KeyError as error:
            raise ValueError(
                f"unsupported notification locale: {output_path.stem}"
            ) from error
        for key in sorted(location_keys - {"#str_ap_location_sent"}):
            location_id = key.removeprefix("#str_ap_location_")
            try:
                location_name = location_names[location_id]
            except KeyError as error:
                raise ValueError(
                    f"location {location_id} has no canonical notification name"
                ) from error
            entries.append((key, f"AP: {location_name}"))

    serialized_entries = string_entries(entries)
    defined_keys = {entry["name"] for entry in serialized_entries}
    if referenced_keys != defined_keys:
        missing = sorted(referenced_keys - defined_keys)
        orphaned = sorted(defined_keys - referenced_keys)
        raise ValueError(
            f"notification string keys diverge: missing={missing}, orphaned={orphaned}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps({"strings": serialized_entries}, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", required=True, type=Path)
    parser.add_argument("--item-replay-policies", required=True, type=Path)
    parser.add_argument("--maps-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--location-names", type=Path)
    args = parser.parse_args()
    build_string_table(
        args.items.resolve(),
        args.item_replay_policies.resolve(),
        args.maps_dir.resolve(),
        args.output.resolve(),
        (
            args.location_names.resolve()
            if args.location_names is not None
            else None
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
