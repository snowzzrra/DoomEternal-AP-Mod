"""Resolve map resource owners from ordered packagemapspec relations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

DEFAULT_PACKAGEMAPSPEC = Path(
    "/run/media/system/Eris/SteamLibrary/steamapps/common/DOOMEternal/base/packagemapspec.json"
)


def load_packagemapspec(packagemapspec_path: Path | str = DEFAULT_PACKAGEMAPSPEC) -> dict[str, Any]:
    path = Path(packagemapspec_path)
    if not path.is_file():
        raise FileNotFoundError(f"packagemapspec.json not found at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("files", "maps", "mapFileRefs"):
        if not isinstance(data.get(key), list):
            raise ValueError(f"packagemapspec.json {key!r} must be a list")
    return data


def resolve_owner(
    runtime_map: str,
    packagemapspec_path: Path | str = DEFAULT_PACKAGEMAPSPEC,
) -> tuple[str, int]:
    """Return first ordered resource reference and file index for one runtime map."""
    data = load_packagemapspec(packagemapspec_path)
    map_indices = [
        index
        for index, record in enumerate(data["maps"])
        if isinstance(record, dict) and record.get("name") == runtime_map
    ]
    if len(map_indices) != 1:
        raise ValueError(
            f"runtime map {runtime_map!r} must have exactly one packagemapspec map record"
        )
    map_index = map_indices[0]
    for relation in data["mapFileRefs"]:
        if not isinstance(relation, dict) or relation.get("map") != map_index:
            continue
        file_index = relation.get("file")
        if not isinstance(file_index, int) or not 0 <= file_index < len(data["files"]):
            raise ValueError(f"invalid file reference for runtime map {runtime_map!r}")
        file_record = data["files"][file_index]
        if not isinstance(file_record, dict) or not isinstance(file_record.get("name"), str):
            raise ValueError(f"invalid file record {file_index} for runtime map {runtime_map!r}")
        name = file_record["name"]
        if name.endswith(".resources"):
            return name, file_index
    raise ValueError(f"runtime map {runtime_map!r} has no resource reference")


def resolve_owner_evidence(
    runtime_map: str,
    packagemapspec_path: Path | str = DEFAULT_PACKAGEMAPSPEC,
) -> dict[str, Any]:
    path = Path(packagemapspec_path)
    winner, priority = resolve_owner(runtime_map, path)
    return {
        "source": "packagemapspec",
        "runtime_map": runtime_map,
        "winner": winner,
        "priority": priority,
        "packagemapspec_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
