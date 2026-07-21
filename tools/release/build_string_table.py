#!/usr/bin/env python3
"""Build the canonical item-notification string table from generated maps."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from tools.maps.notification_formatting import notification_key, notification_text

HEADER_KEY_PATTERN = re.compile(r'header\s*=\s*"(#str_ap_notify_item_\d+(?:_\d+)?)";')


def referenced_notification_keys(maps_dir: Path) -> set[str]:
    map_paths = sorted(maps_dir.rglob("*.entities"))
    if not map_paths:
        raise ValueError(f"no generated maps found in {maps_dir}")
    return {
        key
        for path in map_paths
        for key in HEADER_KEY_PATTERN.findall(path.read_text(encoding="utf-8"))
    }


def build_string_table(
    items_path: Path,
    policies_path: Path,
    maps_dir: Path,
    output_path: Path,
) -> None:
    items = json.loads(items_path.read_text(encoding="utf-8"))
    policies = json.loads(policies_path.read_text(encoding="utf-8"))
    item_names = {
        int(item_id): entry["name"]
        for item_id, entry in policies.get("items", {}).items()
        if "name" in entry
    }

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
            entries.append((key, notification_text(item_id, definition, item_name, stage=stage)))

    key_counts = Counter(key for key, _ in entries)
    duplicates = sorted(key for key, count in key_counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate notification keys: {duplicates}")

    defined_keys = set(key_counts)
    referenced_keys = referenced_notification_keys(maps_dir)
    if referenced_keys != defined_keys:
        missing = sorted(referenced_keys - defined_keys)
        orphaned = sorted(defined_keys - referenced_keys)
        raise ValueError(
            f"notification string keys diverge: missing={missing}, orphaned={orphaned}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps({"strings": dict(entries)}, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", required=True, type=Path)
    parser.add_argument("--item-replay-policies", required=True, type=Path)
    parser.add_argument("--maps-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    build_string_table(
        args.items.resolve(),
        args.item_replay_policies.resolve(),
        args.maps_dir.resolve(),
        args.output.resolve(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
