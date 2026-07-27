#!/usr/bin/env python3
"""Verify Sentinel Prime's AP visual model is carried by its patch2 container."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


MAP_KEY = "e2m4_boss"
MODEL_PATH = Path("art/pickups/question_mark_a.lwo")
MODEL_SHA256 = "9bc94a7d92fa10d31298883700dc0131db6149c6f8acfc3873671a9e3c9e94d2"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit_e2m4_resource_package(
    map_sources: Path,
    asset_root: Path,
    mod_root: Path,
    packaged_entities: Path | None = None,
) -> dict[str, str]:
    """Prove the visual is present under the exact final resource container.

    The build copies ``asset_root`` into the mod payload before compressing the
    map.  This audit checks the source dependency and the resulting package
    tree separately, so an entities-only model reference cannot pass.
    """
    registry = json.loads(map_sources.read_text(encoding="utf-8"))
    entry = registry["maps"][MAP_KEY]
    resource_path = entry["resource_path"]
    if entry["resource_owner"] != resource_path:
        raise AssertionError("Sentinel Prime resource owner diverges from resource path")
    if resource_path != "game/sp/e2m4_boss/e2m4_boss_patch2.resources":
        raise AssertionError("Sentinel Prime must use the e2m4_boss_patch2 container")

    resource_name = Path(resource_path).stem
    dependency = asset_root / resource_name / MODEL_PATH
    packaged_model = mod_root / resource_name / MODEL_PATH
    for label, path in (("source dependency", dependency), ("packaged model", packaged_model)):
        if not path.is_file():
            raise AssertionError(f"Sentinel Prime {label} is missing: {path}")
        if _sha256(path) != MODEL_SHA256:
            raise AssertionError(f"Sentinel Prime {label} hash mismatch: {path}")

    if packaged_entities is not None:
        expected_map = mod_root / resource_name / "maps" / entry["relative_entities_path"]
        if packaged_entities != expected_map:
            raise AssertionError("Sentinel Prime entities are not in the patch2 container")
        if not packaged_entities.is_file():
            raise AssertionError(f"Sentinel Prime packaged entities are missing: {packaged_entities}")
        if 'model = "art/pickups/question_mark_a.lwo";' not in packaged_entities.read_text(encoding="utf-8"):
            raise AssertionError("Sentinel Prime packaged entities do not reference the AP model")

    return {
        "resource_owner": resource_path,
        "resource_name": resource_name,
        "model": str(MODEL_PATH),
        "model_sha256": MODEL_SHA256,
        "packaged_model": str(packaged_model),
    }


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
