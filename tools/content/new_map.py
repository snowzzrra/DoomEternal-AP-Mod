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
    _write(directory / "descriptor.json", {
        "schema_version": 1,
        "key": key,
        "display_name": name,
        "enabled": False,
        "source_file": source,
        "generated_output": f"{key}.entities",
        "runtime_map": runtime_map,
        "resource_base": Path(resource_base).stem,
        "resource_owner": owner,
        "relative_entities_path": f"{runtime_map}/{key}.entities",
        "supported_game_revision": "UNSET",
        "route": [],
    })
    _write(directory / "locations.json", {
        "schema_version": 1,
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
        "status": "scaffolded",
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
