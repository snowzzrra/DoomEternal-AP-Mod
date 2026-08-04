"""Create a disabled, valid MapContentPackage without editing Python."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def create_package(
    key: str,
    name: str,
    source: str,
    resource_base: str,
    owner: str,
    *,
    root: Path = ROOT,
) -> Path:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
        raise ValueError("map key must match [a-z][a-z0-9_]*")
    directory = root / "content" / "maps" / key
    if directory.exists():
        raise FileExistsError(f"map package already exists: {directory}")
    directory.mkdir(parents=True)
    runtime_map = resource_base.removesuffix(".resources")
    orders = [
        json.loads(path.read_text(encoding="utf-8")).get("order", -1)
        for path in (root / "content" / "maps").glob("*/descriptor.json")
    ]
    _write(directory / "descriptor.json", {
        "schema_version": 1,
        "key": key,
        "order": max(orders, default=-1) + 1,
        "display_name": name,
        "enabled": False,
        "test_only": True,
        "onboarding_status": "onboarding",
        "source_file": source,
        "source_sha256": "",
        "source_size": 0,
        "source_owner": f"vanillamaps/{source}",
        "generated_output": f"{key}.entities",
        "runtime_map": runtime_map,
        "resource_base": Path(resource_base).stem.removesuffix("_patch1"),
        "resource_path": owner,
        "resource_owner": owner,
        "resource_priority": 0,
        "relative_entities_path": f"{runtime_map}/{key}.entities",
        "supported_game_revision": "UNSET",
        "route": {"regions": [], "connections": [], "virtual_locations": []},
    })
    _write(directory / "locations.json", {
        "schema_version": 1,
        "map_key": key,
        "region": name,
        "entities": {},
        "target_policies": {},
        "assets": [],
    })
    _write(directory / "runtime.json", {"schema_version": 1, "locations": []})
    _write(directory / "publishers.json", {"schema_version": 1, "publishers": []})
    _write(directory / "assets.json", {"schema_version": 1, "assets": []})
    _write(directory / "onboarding.json", {
        "schema_version": 1,
        "map_key": key,
        "status": "onboarding",
        "checks": [],
    })
    return directory


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--resource-base", required=True)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args()
    print(create_package(
        args.key, args.name, args.source, args.resource_base, args.owner
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
