#!/usr/bin/env python3
"""Build the canonical item-notification string table from generated maps."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from tools.maps.notification_formatting import (
    ITEM_NOTIFICATION_HEADER_KEY,
    ITEM_NOTIFICATION_TITLE,
    LOCATION_NOTIFICATION_HEADER_KEY,
    LOCATION_NOTIFICATION_TITLE,
    PLACEMENT_SENT_KEY_PREFIX,
    item_receipt_text,
    location_notification_text,
    location_sent_text,
    major_notification_key,
    major_notification_text,
    notification_key,
    notification_text,
)

ITEM_KEY_PATTERN = re.compile(
    r'(?:header|subtext)\s*=\s*"(#str_ap_(?:item_received|notify_item(?:_received)?_\d+(?:_\d+)?))";'
)
LOCATION_KEY_PATTERN = re.compile(
    r'(?:header|subtext)\s*=\s*"(#str_ap_location_(?:sent(?:_\d+)?|\d+))";'
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
            ITEM_KEY_PATTERN.findall(path.read_text(encoding="utf-8"))
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
    placement_metadata_path: Path | None = None,
) -> None:
    items = json.loads(items_path.read_text(encoding="utf-8"))
    policies = json.loads(policies_path.read_text(encoding="utf-8"))
    item_names = {
        int(item_id): entry["name"]
        for item_id, entry in policies.get("items", {}).items()
        if "name" in entry
    }

    referenced_keys = referenced_notification_keys(maps_dir)
    placement_names = {}
    placement_records = {}
    placement_by_item = {}
    if placement_metadata_path is not None:
        metadata = json.loads(placement_metadata_path.read_text(encoding="utf-8"))
        if isinstance(metadata, dict):
            metadata = metadata.get("placement_metadata")
        if not isinstance(metadata, list):
            raise ValueError("placement metadata must contain a list")
        required = {
            "location_id", "location_name", "item_id", "item_name",
            "recipient_slot", "recipient_name", "classification", "trap", "local",
        }
        for record in metadata:
            if not isinstance(record, dict) or set(record) != required:
                raise ValueError("placement metadata has invalid fields")
            location_id = record["location_id"]
            location_name = record["location_name"]
            if (
                not isinstance(location_id, int)
                or isinstance(location_id, bool)
                or not isinstance(location_name, str)
                or not location_name.strip()
            ):
                raise ValueError("placement metadata has invalid location identity")
            if location_id in placement_names:
                raise ValueError(f"placement metadata duplicates location {location_id}")
            placement_names[location_id] = location_name.strip()
            placement_records[location_id] = record
            item_id = record["item_id"]
            placement_by_item.setdefault(item_id, []).append(record)
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
            isinstance(definition, dict)
            and definition.get("type") in {"progressive_perk", "progressive_item"}
        ) else (None,)
        for stage in stages:
            key = notification_key(item_id, definition, stage=stage)
            if key in referenced_keys:
                records = placement_by_item.get(item_id, [])
                text = None
                if stage is None and len(records) == 1:
                    record = records[0]
                    text = item_receipt_text(
                        item_name,
                        local=bool(record["local"]),
                        trap=bool(record["trap"]),
                        recipient_name=str(record["recipient_name"]),
                    )
                if text is None:
                    text = notification_text(
                        item_id, definition, item_name, stage=stage
                    )
                entries.append((key, text))
            major_key = major_notification_key(item_id, definition, stage=stage)
            if major_key in referenced_keys:
                entries.append((
                    major_key,
                    major_notification_text(
                        item_id,
                        definition,
                        item_name,
                        stage=stage,
                        locale=output_path.stem,
                    ),
                ))

    location_keys = {
        key for key in referenced_keys if key.startswith("#str_ap_location_")
    }
    if ITEM_NOTIFICATION_HEADER_KEY in referenced_keys:
        try:
            entries.append((
                ITEM_NOTIFICATION_HEADER_KEY,
                ITEM_NOTIFICATION_TITLE[output_path.stem],
            ))
        except KeyError as error:
            raise ValueError(
                f"unsupported notification locale: {output_path.stem}"
            ) from error
    if location_keys:
        default_location_names = (
            Path(__file__).resolve().parents[2]
            / "data"
            / "location_names.json"
        )
        if location_names_path is None:
            location_names_path = default_location_names
        if location_names_path.resolve() == default_location_names.resolve():
            from doom_eap.content.content_catalog import load_content_catalog
            location_identity = {
                "schema_version": 1,
                "locations": {
                    str(location_id): name
                    for location_id, name
                    in load_content_catalog().location_names.items()
                },
            }
        else:
            location_identity = json.loads(
                location_names_path.read_text(encoding="utf-8")
            )
        if location_identity.get("schema_version") != 1:
            raise ValueError("unsupported location-name schema")
        location_names = location_identity.get("locations", {})
        if LOCATION_NOTIFICATION_HEADER_KEY in referenced_keys:
            try:
                entries.append((
                    LOCATION_NOTIFICATION_HEADER_KEY,
                    LOCATION_NOTIFICATION_TITLE[output_path.stem],
                ))
            except KeyError as error:
                raise ValueError(
                    f"unsupported notification locale: {output_path.stem}"
                ) from error
        for key in sorted(location_keys):
            if key == LOCATION_NOTIFICATION_HEADER_KEY:
                continue
            if key.startswith(PLACEMENT_SENT_KEY_PREFIX):
                location_id = int(key.removeprefix(PLACEMENT_SENT_KEY_PREFIX))
                record = placement_records.get(location_id)
                if record is None:
                    raise ValueError(
                        f"location {location_id} has no placement metadata for "
                        "its sent presentation"
                    )
                entries.append((key, location_sent_text(record)))
                continue
            location_id = key.removeprefix("#str_ap_location_")
            if location_id in placement_names:
                location_name = placement_names[location_id]
            else:
                try:
                    location_name = location_names[location_id]
                except KeyError as error:
                    raise ValueError(
                        f"location {location_id} has no canonical notification name"
                    ) from error
            entries.append((key, location_notification_text(location_name)))

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
    parser.add_argument("--placement-metadata", type=Path)
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
        (
            args.placement_metadata.resolve()
            if args.placement_metadata is not None
            else None
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
