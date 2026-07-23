#!/usr/bin/env python3
"""Build deterministic item classifications and location names from local APWorld."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load APWorld source: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_identities(
    archipelago_root: Path,
    items_output: Path,
    locations_output: Path,
    *,
    item_mapping_revision: int,
) -> None:
    world_root = archipelago_root / "worlds" / "doometernal"
    import_names = ("BaseClasses", "NetUtils", "Options", "Utils", "settings")
    previous_modules = {
        name: sys.modules.pop(name) for name in import_names if name in sys.modules
    }
    sys.path.insert(0, str(archipelago_root))
    try:
        items_module = _load_module(
            world_root / "items.py", "doometernal_identity_items"
        )
        locations_module = _load_module(
            world_root / "locations.py", "doometernal_identity_locations"
        )
    finally:
        sys.path.pop(0)
        for name in import_names:
            sys.modules.pop(name, None)
        sys.modules.update(previous_modules)

    items: dict[int, dict[str, object]] = {}
    for name, definition in items_module.item_data_table.items():
        if definition.code is None:
            continue
        item_id = int(definition.code)
        if item_id in items:
            raise ValueError(f"duplicate APWorld item ID: {item_id}")
        classification = int(definition.classification)
        if classification < 0:
            raise ValueError(f"item {item_id} has invalid classification")
        items[item_id] = {"name": name, "classification": classification}

    locations: dict[int, str] = {}
    for name, definition in locations_module.location_data_table.items():
        if definition.code is None:
            continue
        location_id = int(definition.code)
        if location_id in locations:
            raise ValueError(f"duplicate APWorld location ID: {location_id}")
        locations[location_id] = name

    item_payload = {
        "schema_version": 1,
        "item_mapping_revision": item_mapping_revision,
        "source": "Archipelago/worlds/doometernal/items.py",
        "source_sha256": hashlib.sha256((world_root / "items.py").read_bytes()).hexdigest(),
        "items": {str(key): items[key] for key in sorted(items)},
    }
    location_payload = {
        "schema_version": 1,
        "source": "Archipelago/worlds/doometernal/locations.py",
        "source_sha256": hashlib.sha256((world_root / "locations.py").read_bytes()).hexdigest(),
        "locations": {str(key): locations[key] for key in sorted(locations)},
    }
    for path, payload in (
        (items_output, item_payload),
        (locations_output, location_payload),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=4, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archipelago-root", required=True, type=Path)
    parser.add_argument("--items-output", required=True, type=Path)
    parser.add_argument("--locations-output", required=True, type=Path)
    parser.add_argument("--item-mapping-revision", required=True, type=int)
    args = parser.parse_args()
    build_identities(
        args.archipelago_root.resolve(),
        args.items_output.resolve(),
        args.locations_output.resolve(),
        item_mapping_revision=args.item_mapping_revision,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
