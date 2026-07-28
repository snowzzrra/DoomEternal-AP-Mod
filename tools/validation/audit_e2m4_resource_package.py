#!/usr/bin/env python3
"""Deprecated compatibility wrapper; use audit_resource_packages.py."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.validation.audit_resource_packages import audit_resource_packages


MAP_KEY = "e2m4_boss"


def audit_e2m4_resource_package(
    map_sources: Path,
    asset_root: Path,
    mod_root: Path,
    packaged_entities: Path | None = None,
) -> dict[str, str]:
    """Compatibility entrypoint backed by the parameterized asset auditor."""
    registry = json.loads(map_sources.read_text(encoding="utf-8"))
    entry = registry["maps"][MAP_KEY]
    resource_path = entry["resource_path"]
    if entry["resource_owner"] != resource_path:
        raise AssertionError("Sentinel Prime resource owner diverges from resource path")
    if resource_path != "game/sp/e2m4_boss/e2m4_boss_patch2.resources":
        raise AssertionError("Sentinel Prime must use the e2m4_boss_patch2 container")

    records = audit_resource_packages(
        asset_root,
        mod_root,
        map_key=MAP_KEY,
        source_map_root=map_sources.parent.parent / "vanillamaps",
    )
    if len(records) != 1 or records[0]["strategy"] != "resident_model":
        raise AssertionError("Sentinel Prime must use one resident_model asset")

    if packaged_entities is not None:
        resource_name = Path(resource_path).stem
        expected_map = mod_root / resource_name / "maps" / entry["relative_entities_path"]
        if packaged_entities != expected_map:
            raise AssertionError("Sentinel Prime entities are not in the patch2 container")
        if not packaged_entities.is_file():
            raise AssertionError(f"Sentinel Prime packaged entities are missing: {packaged_entities}")
        if 'model = "art/pickups/codex.lwo";' not in packaged_entities.read_text(encoding="utf-8"):
            raise AssertionError("Sentinel Prime entities do not reference the resident model")

    return records[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map-sources", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--mod-root", type=Path, required=True)
    parser.add_argument("--packaged-entities", type=Path)
    args = parser.parse_args()
    record = audit_e2m4_resource_package(
        args.map_sources, args.asset_root, args.mod_root, args.packaged_entities
    )
    print(json.dumps(record, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
