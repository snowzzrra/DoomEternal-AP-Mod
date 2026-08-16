"""Canonical base and dependent-map room payload resources."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from collections.abc import Mapping
from itertools import product
from pathlib import Path, PurePosixPath
from typing import Any

BASE_RESOURCE_NAME = "base_mod.zip"
ROOM_PAYLOAD_RESOURCE_NAME = "room_payloads.zip"
ROOM_PAYLOAD_MANIFEST_NAME = "room_payload_manifest.json"
ROOM_PAYLOAD_SCHEMA_VERSION = 1
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_path(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _safe_member(name: str) -> str:
    normalized = PurePosixPath(name.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts or not normalized.parts:
        raise ValueError(f"unsafe room resource member: {name}")
    return normalized.as_posix()


def write_deterministic_zip(files: Mapping[str, bytes], destination: Path) -> None:
    """Write sorted, timestamp-free ZIP bytes and replace destination atomically."""
    normalized = {_safe_member(name): bytes(value) for name, value in files.items()}
    if len(normalized) != len(files):
        raise ValueError("room resource contains duplicate members")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.incoming")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(normalized):
            info = zipfile.ZipInfo(name, ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            archive.writestr(info, normalized[name])
    os.replace(temporary, destination)


def zip_directory(source: Path, destination: Path) -> dict[str, str]:
    files = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in sorted(source.rglob("*"))
        if path.is_file()
    }
    write_deterministic_zip(files, destination)
    return {name: sha256_bytes(value) for name, value in sorted(files.items())}


def read_zip(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        members: dict[str, bytes] = {}
        for info in archive.infolist():
            name = _safe_member(info.filename)
            if info.is_dir() or name in members:
                raise ValueError(f"invalid room resource archive member: {info.filename}")
            members[name] = archive.read(info)
    return members


def _state_key(options: Mapping[str, Any]) -> tuple[tuple[str, bool], ...]:
    return tuple(sorted((str(key), value) for key, value in options.items()))


def validate_room_payload_manifest(
    document: Mapping[str, Any],
    *,
    known_maps: Mapping[str, str] | None = None,
) -> None:
    if document.get("schema_version") != ROOM_PAYLOAD_SCHEMA_VERSION:
        raise ValueError("room payload manifest schema version is unsupported")
    if document.get("model") != "dependent_map_payloads":
        raise ValueError("room payload manifest model is unsupported")
    keys = document.get("physical_option_keys")
    if keys != ["randomize_chainsaw", "randomize_dash", "randomize_first_battery"]:
        raise ValueError("room payload physical option contract drifted")
    physical_option_keys = {
        "randomize_chainsaw", "randomize_dash", "randomize_first_battery",
    }
    maps = document.get("maps")
    if not isinstance(maps, Mapping) or not maps:
        raise ValueError("room payload manifest maps are missing")
    seen_members: set[str] = set()
    base_members = document.get("base_members")
    if not isinstance(base_members, list) or any(not isinstance(name, str) for name in base_members):
        raise ValueError("room payload base member list is invalid")
    normalized_base_members = [_safe_member(name) for name in base_members]
    if len(set(normalized_base_members)) != len(normalized_base_members):
        raise ValueError("room payload base member list contains duplicates")
    seen_targets: set[str] = set()
    for map_key, record in maps.items():
        if not isinstance(map_key, str) or not isinstance(record, Mapping):
            raise ValueError("room payload map record is invalid")
        option_keys = record.get("option_keys")
        states = record.get("states")
        target_member = record.get("target_member")
        if not isinstance(target_member, str) or _safe_member(target_member) != target_member:
            raise ValueError(f"room payload target member is missing: {map_key}")
        if target_member not in normalized_base_members or target_member in seen_targets:
            raise ValueError(f"room payload target member is not unique/base-backed: {map_key}")
        seen_targets.add(target_member)
        if not isinstance(option_keys, list) or not all(isinstance(key, str) for key in option_keys):
            raise ValueError(f"room payload option keys are invalid: {map_key}")
        if option_keys != sorted(set(option_keys)) or not set(option_keys) <= physical_option_keys:
            raise ValueError(f"room payload option key coverage is invalid: {map_key}")
        if not isinstance(states, list) or not states:
            raise ValueError(f"room payload states are missing: {map_key}")
        state_keys: set[tuple[tuple[str, bool], ...]] = set()
        for state in states:
            if not isinstance(state, Mapping) or set(state) != {"options", "source", "member", "sha256"}:
                raise ValueError(f"room payload state fields are invalid: {map_key}")
            options = state["options"]
            if not isinstance(options, Mapping) or set(options) != set(option_keys):
                raise ValueError(f"room payload state options are invalid: {map_key}")
            if any(not isinstance(value, bool) for value in options.values()):
                raise ValueError(f"room payload state option is not boolean: {map_key}")
            state_key = _state_key(options)
            if state_key in state_keys:
                raise ValueError(f"duplicate room payload state: {map_key}")
            state_keys.add(state_key)
            source = state["source"]
            member = state["member"]
            if not isinstance(state["sha256"], str) or len(state["sha256"]) != 64 or any(
                char not in "0123456789abcdef" for char in state["sha256"]
            ):
                raise ValueError(f"room payload state hash is invalid: {map_key}")
            if source not in {"base", "replacement"}:
                raise ValueError(f"room payload state source is invalid: {map_key}")
            if member is not None:
                member = _safe_member(str(member))
                if source != "replacement" or member in seen_members:
                    raise ValueError(f"room payload replacement member is invalid: {map_key}")
                seen_members.add(member)
            elif source != "base":
                raise ValueError(f"room payload replacement member is missing: {map_key}")
        expected_states = {
            tuple(sorted(zip(option_keys, values)))
            for values in product((False, True), repeat=len(option_keys))
        }
        if state_keys != expected_states:
            raise ValueError(f"room payload state coverage is incomplete: {map_key}")
    if known_maps is not None:
        if set(maps) != set(known_maps):
            raise ValueError("room payload map set disagrees with known release maps")
        for map_key, expected_target in known_maps.items():
            if maps[map_key]["target_member"] != expected_target:
                raise ValueError(f"room payload resource mapping drifted: {map_key}")


def load_room_payload_manifest(
    path: Path,
    *,
    known_maps: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("room payload manifest must be an object")
    validate_room_payload_manifest(document, known_maps=known_maps)
    return document


def select_room_payloads(document: Mapping[str, Any], options: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for map_key, record in document["maps"].items():
        relevant = {key: options.get(key, False) for key in record["option_keys"]}
        matches = [state for state in record["states"] if state["options"] == relevant]
        if len(matches) != 1:
            raise ValueError(f"room payload state is not deterministic: {map_key}")
        selected[map_key] = dict(matches[0])
    return selected


def assemble_room_files(
    base_resource: Path,
    payload_resource: Path,
    payload_manifest: Mapping[str, Any],
    options: Mapping[str, Any],
) -> tuple[dict[str, bytes], dict[str, dict[str, Any]]]:
    base = read_zip(base_resource)
    payloads = read_zip(payload_resource)
    if set(base) != set(payload_manifest["base_members"]):
        raise ValueError("base room payload member set drifted")
    selected = select_room_payloads(payload_manifest, options)
    expected_payload_members = {
        state["member"]
        for record in payload_manifest["maps"].values()
        for state in record["states"]
        if state["source"] == "replacement"
    }
    if set(payloads) != expected_payload_members:
        raise ValueError("room payload archive member set drifted")
    assembled = dict(base)
    for map_key, state in selected.items():
        target = str(payload_manifest["maps"][map_key]["target_member"])
        if target not in assembled:
            raise ValueError(f"base room payload target is missing: {map_key}/{target}")
        if state["source"] == "base":
            if sha256_bytes(assembled[target]) != state["sha256"]:
                raise ValueError(f"base room payload identity drifted: {map_key}")
            continue
        member = str(state["member"])
        if member not in payloads:
            raise ValueError(f"room payload member is missing: {map_key}/{member}")
        assembled[target] = payloads[member]
        if sha256_bytes(assembled[target]) != state["sha256"]:
            raise ValueError(f"replacement room payload identity drifted: {map_key}")
    if any(name not in assembled for name in payload_manifest.get("base_members", [])):
        raise ValueError("base room payload member set is incomplete")
    return assembled, selected


def resource_metadata(path: Path) -> dict[str, Any]:
    return {"path": path.name, "sha256": sha256_path(path), "size": path.stat().st_size}
