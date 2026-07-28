"""Parameterized asset-package audit derived from ``content_catalog``."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Iterable

from content_catalog import AssetSpec, load_content_catalog


def audit_resource_packages(
    asset_root: Path,
    mod_root: Path,
    *,
    map_key: str | None = None,
    assets: Iterable[AssetSpec] | None = None,
    source_map_root: Path | None = None,
    zip_path: Path | None = None,
    generated_maps_root: Path | None = None,
) -> list[dict[str, str]]:
    """Audit bundles/resident assets against the resource base and final ZIP."""
    records: list[dict[str, str]] = []
    catalog = load_content_catalog()
    zip_names: set[str] | None = None
    zip_archive: zipfile.ZipFile | None = None
    if zip_path is not None:
        zip_archive = zipfile.ZipFile(zip_path)
        zip_names = set(zip_archive.namelist())
    try:
        for asset in assets if assets is not None else catalog.assets:
            if map_key is not None and asset.map_key != map_key:
                continue
            if asset.strategy == "resident_model":
                if source_map_root is None:
                    source_map_root = Path(__file__).resolve().parents[2] / "vanillamaps"
                source_map = source_map_root / f"{asset.map_key}.map"
                if not source_map.is_file() or f'model = "{asset.model}";' not in source_map.read_text(
                    encoding="utf-8"
                ):
                    raise AssertionError(
                        f"{asset.map_key}: resident model is not referenced by the vanilla map: "
                        f"{asset.model}"
                    )
                wrong_patch_copy = mod_root / Path(asset.resource_owner).stem / asset.model
                if wrong_patch_copy.exists():
                    raise AssertionError(
                        f"{asset.map_key}: resident model was incorrectly copied to patch owner: "
                        f"{wrong_patch_copy}"
                    )
                map_spec = catalog.maps.get(asset.map_key)
                if map_spec is not None:
                    entities_member = (
                        f"{Path(asset.resource_owner).stem}/maps/"
                        f"{map_spec.relative_entities_path}"
                    )
                    staged_entities = (
                        generated_maps_root / map_spec.data["generated_output"]
                        if generated_maps_root is not None
                        else mod_root / entities_member
                    )
                    if not staged_entities.is_file() or f'model = "{asset.model}";' not in staged_entities.read_text(
                        encoding="utf-8"
                    ):
                        raise AssertionError(
                            f"{asset.map_key}: staged entities do not reference resident model "
                            f"{asset.model}"
                        )
                    if zip_archive is not None:
                        matches = [
                            name for name in zip_names or ()
                            if name == entities_member or name.endswith(f"/{entities_member}")
                        ]
                        if len(matches) != 1:
                            raise AssertionError(
                                f"{asset.map_key}: final ZIP does not reference resident model "
                                f"{asset.model}"
                            )
                records.append({
                    "map_key": asset.map_key,
                    "strategy": asset.strategy,
                    "resource_base": asset.resource_base,
                    "resource_owner": asset.resource_owner,
                    "asset": asset.model,
                    "sha256": "resident",
                })
                continue

            members = (asset.model, *asset.dependencies)
            for member in members:
                source = asset_root / asset.resource_base / member
                packaged = mod_root / asset.resource_base / member
                if not source.is_file():
                    raise AssertionError(
                        f"[ASSET] DEPENDENCY_MISSING bundle={asset.key} dependency={member} source={source}"
                    )
                if not packaged.is_file():
                    raise AssertionError(
                        f"[ASSET] DEPENDENCY_MISSING bundle={asset.key} dependency={member} packaged={packaged}"
                    )
                source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
                if hashlib.sha256(packaged.read_bytes()).hexdigest() != source_hash:
                    raise AssertionError(
                        f"{asset.map_key}: packaged asset hash mismatch: {member}"
                    )
                if zip_names is not None and not any(
                    name.endswith(f"/{asset.resource_base}/{member}")
                    or name == f"{asset.resource_base}/{member}"
                    for name in zip_names
                ):
                    raise AssertionError(
                        f"[ASSET] DEPENDENCY_MISSING bundle={asset.key} dependency={member} zip={zip_path}"
                    )
            source_hash = hashlib.sha256(
                (asset_root / asset.resource_base / asset.model).read_bytes()
            ).hexdigest()
            records.append({
                "map_key": asset.map_key,
                "strategy": asset.strategy,
                "resource_base": asset.resource_base,
                "resource_owner": asset.resource_owner,
                "asset": asset.model,
                "sha256": source_hash,
            })
    finally:
        if zip_archive is not None:
            zip_archive.close()
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--mod-root", type=Path, required=True)
    parser.add_argument("--map")
    parser.add_argument("--source-map-root", type=Path)
    parser.add_argument("--zip", dest="zip_path", type=Path)
    parser.add_argument("--generated-maps", dest="generated_maps_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit_resource_packages(
        args.asset_root,
        args.mod_root,
        map_key=args.map,
        source_map_root=args.source_map_root,
        zip_path=args.zip_path,
        generated_maps_root=args.generated_maps_root,
    ), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
