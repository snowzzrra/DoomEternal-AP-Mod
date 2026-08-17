"""Canonical release manifest contract.

Build writers and package readers use this module instead of maintaining
independent RELEASE_MANIFEST interpretations.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from doom_eap.content.automap_visual_registry import load_automap_visual_registry
from doom_eap.content.content_catalog import ContentCatalog, load_content_catalog
from tools.validation.release_layout import validate_public_file_members

MANIFEST_SCHEMA_VERSION = 2
MANIFEST_FILENAME = "RELEASE_MANIFEST.json"
CHECKED_LOCATION_VISUAL_FIELDS = frozenset({
    "path", "sha256", "authoritative_fingerprint", "generated_map_sha256",
})
ROOM_COMPILER_FIELDS = frozenset({"schema_version", "model", "revision", "maps", "resources"})
BASE_RESOURCE_FIELDS = frozenset({"schema_version", "maps"})
STALE_PACKAGE_MARKERS = (
    "mod_templates",
    "map-content-",
    "physical_signature",
    "map_content_signature",
    "templates_sha256",
    "1110",
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _identity(root: Path) -> dict[str, Any]:
    return json.loads((root / "data" / "content_identity.json").read_text(encoding="utf-8"))


def _map_record(spec: Any, generated_maps: Path | None) -> dict[str, Any]:
    generated = generated_maps / spec.data["generated_output"] if generated_maps else None
    record: dict[str, Any] = {
        "runtime_map": spec.runtime_map,
        "generated_output": spec.data["generated_output"],
        "resource_base": spec.resource_base,
        "resource_path": spec.resource_path,
        "resource_owner": spec.resource_owner,
        "resource_priority": spec.resource_priority,
        "relative_entities_path": spec.relative_entities_path,
    }
    if generated is not None and generated.is_file():
        record["generated_map_sha256"] = _sha256(generated)
        record["generated_map_size"] = generated.stat().st_size
    return record


def build_release_manifest(
    root: Path,
    *,
    generated_maps: Path | None = None,
    public_files: list[str] | None = None,
    release_version: str | None = None,
    apworld: Mapping[str, Any] | None = None,
    room_resources: Path | None = None,
) -> dict[str, Any]:
    """Build canonical manifest from current source and optional build outputs."""
    root = root.resolve()
    from tools.release.build_cache import validate_source_contract

    validate_source_contract(root)
    identity = _identity(root)
    visual_path = root / "data" / "checked_location_visuals.json"
    visual = load_automap_visual_registry(visual_path)
    catalog = load_content_catalog(root)
    maps = {
        spec.key: _map_record(spec, generated_maps)
        for spec in catalog.enabled_maps()
    }
    generated_hashes = {
        key: value["generated_map_sha256"]
        for key, value in maps.items()
        if "generated_map_sha256" in value
    }
    resources_root = room_resources or root / "build" / "release" / "client" / "resources"
    resources = {}
    for filename in ("base_mod.zip", "room_payloads.zip", "room_payload_manifest.json"):
        path = resources_root / filename
        if path.is_file():
            resources[filename] = {
                "path": f"client/resources/{filename}",
                "sha256": _sha256(path),
                "size": path.stat().st_size,
            }
    result: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "version": release_version or identity["release_version"],
        "content_identity": identity,
        "checked_location_visuals": {
            "path": "client/data/checked_location_visuals.json",
            "sha256": _sha256(visual_path),
            "authoritative_fingerprint": visual["authoritative_fingerprint"],
            "generated_map_sha256": generated_hashes,
        },
        "room_compiler": {
            "schema_version": 1,
            "model": "direct_room_compile",
            "revision": identity["compiler_revision"],
            "maps": maps,
            "resources": resources,
        },
        "base_resources": {
            "schema_version": 1,
            "maps": {
                key: {
                    field: value[field]
                    for field in (
                        "resource_base", "resource_path", "resource_owner",
                        "resource_priority", "relative_entities_path",
                    )
                }
                for key, value in maps.items()
            },
        },
    }
    if public_files is not None:
        result["public_files"] = sorted(public_files)
    if apworld is not None:
        result["apworld"] = dict(apworld)
    return result


def write_release_manifest(path: Path, document: Mapping[str, Any]) -> None:
    """Validate then write canonical manifest JSON."""
    validate_release_manifest(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def load_release_manifest(
    path: Path,
    *,
    package_root: Path | None = None,
    generated_maps: Path | None = None,
) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    validate_release_manifest(
        document,
        package_root=package_root or path.parent,
        generated_maps=generated_maps,
    )
    return document


def _require_hash(value: Any, label: str) -> None:
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise ValueError(f"release manifest {label} must be SHA-256")


def _validate_map_metadata(map_key: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"release manifest room compiler map is invalid: {map_key}")
    required = {
        "runtime_map", "generated_output", "resource_base", "resource_path",
        "resource_owner", "resource_priority", "relative_entities_path",
    }
    if not required <= set(value):
        raise ValueError(f"release manifest room compiler map fields missing: {map_key}")
    for field in ("runtime_map", "generated_output", "resource_base", "resource_path", "resource_owner", "relative_entities_path"):
        if not isinstance(value[field], str) or not value[field]:
            raise ValueError(f"release manifest map field invalid: {map_key}/{field}")
    if not isinstance(value["resource_priority"], int) or isinstance(value["resource_priority"], bool):
        raise ValueError(f"release manifest map priority invalid: {map_key}")
    if "generated_map_sha256" in value:
        _require_hash(value["generated_map_sha256"], f"generated map {map_key}")
    if "generated_map_size" in value and not isinstance(value["generated_map_size"], int):
        raise ValueError(f"release manifest map size invalid: {map_key}")


def validate_release_manifest(
    document: Mapping[str, Any],
    *,
    package_root: Path | None = None,
    generated_maps: Path | None = None,
) -> None:
    """Validate schema and, when paths are supplied, validate packaged bytes."""
    if not isinstance(document, Mapping):
        raise ValueError("release manifest must be an object")
    if document.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("release manifest schema version is unsupported")
    if not isinstance(document.get("version"), str) or not document["version"]:
        raise ValueError("release manifest version is missing")
    if any(marker in json.dumps(document, sort_keys=True).casefold() for marker in (
        "mod_templates", "physical_signature", "map_content_signature", "templates_sha256", "map-content-",
    )):
        raise ValueError("release manifest contains obsolete room variant metadata")
    identity = document.get("content_identity")
    if not isinstance(identity, Mapping):
        raise ValueError("release manifest content identity is missing")
    for field in ("content_revision", "compiler_revision", "bridge_protocol_version"):
        if field not in identity:
            raise ValueError(f"release manifest content identity field missing: {field}")

    visual = document.get("checked_location_visuals")
    if not isinstance(visual, Mapping) or set(visual) != CHECKED_LOCATION_VISUAL_FIELDS:
        raise ValueError("release manifest checked-location visual fields drifted")
    if visual["path"] != "client/data/checked_location_visuals.json":
        raise ValueError("release manifest checked-location visual path drifted")
    _require_hash(visual["sha256"], "checked-location visual registry")
    _require_hash(visual["authoritative_fingerprint"], "checked-location visual fingerprint")
    generated_hashes = visual["generated_map_sha256"]
    if not isinstance(generated_hashes, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str) or not HEX64.fullmatch(value)
        for key, value in generated_hashes.items()
    ):
        raise ValueError("release manifest generated map hashes are invalid")

    compiler = document.get("room_compiler")
    if not isinstance(compiler, Mapping) or not ROOM_COMPILER_FIELDS <= set(compiler):
        raise ValueError("release manifest room compiler metadata is missing")
    if compiler["schema_version"] != 1 or compiler["model"] != "direct_room_compile":
        raise ValueError("release manifest room compiler model is unsupported")
    if compiler["revision"] != identity["compiler_revision"] or not isinstance(compiler["maps"], Mapping):
        raise ValueError("release manifest room compiler revision drifted")
    if not isinstance(compiler["resources"], Mapping):
        raise ValueError("release manifest room compiler resources are invalid")
    for name, resource in compiler["resources"].items():
        if name not in {"base_mod.zip", "room_payloads.zip", "room_payload_manifest.json"}:
            raise ValueError(f"release manifest room compiler resource is unknown: {name}")
        if not isinstance(resource, Mapping) or set(resource) != {"path", "sha256", "size"}:
            raise ValueError(f"release manifest room compiler resource is invalid: {name}")
        if resource["path"] != f"client/resources/{name}":
            raise ValueError(f"release manifest room compiler resource path drifted: {name}")
        _require_hash(resource["sha256"], f"room compiler resource {name}")
        if not isinstance(resource["size"], int) or resource["size"] < 1:
            raise ValueError(f"release manifest room compiler resource size is invalid: {name}")
    if package_root is not None and set(compiler["resources"]) != {
        "base_mod.zip", "room_payloads.zip", "room_payload_manifest.json"
    }:
        raise ValueError("release manifest room compiler resource set is incomplete")
    for key, value in compiler["maps"].items():
        _validate_map_metadata(str(key), value)

    resources = document.get("base_resources")
    if not isinstance(resources, Mapping) or not BASE_RESOURCE_FIELDS <= set(resources):
        raise ValueError("release manifest base-resource metadata is missing")
    if resources["schema_version"] != 1 or not isinstance(resources["maps"], Mapping):
        raise ValueError("release manifest base-resource metadata is invalid")
    if set(resources["maps"]) != set(compiler["maps"]):
        raise ValueError("release manifest base-resource map set disagrees with room compiler")
    for key, resource in resources["maps"].items():
        compiler_map = compiler["maps"][key]
        if any(resource.get(field) != compiler_map[field] for field in (
            "resource_base", "resource_path", "resource_owner", "resource_priority", "relative_entities_path",
        )):
            raise ValueError(f"release manifest base-resource disagreement: {key}")

    if package_root is not None:
        registry = package_root / visual["path"]
        if not registry.is_file() and package_root.name == "client":
            registry = package_root / Path(visual["path"]).relative_to("client")
        if registry.is_file() and _sha256(registry) != visual["sha256"]:
            raise ValueError("release manifest checked-location visual registry hash drifted")
        for resource in compiler["resources"].values():
            path = package_root / resource["path"]
            if not path.is_file() or _sha256(path) != resource["sha256"] or path.stat().st_size != resource["size"]:
                raise ValueError(f"release manifest room compiler resource hash drifted: {resource['path']}")
    if generated_maps is not None:
        for key, expected in generated_hashes.items():
            map_record = compiler["maps"].get(key)
            generated = generated_maps / map_record["generated_output"] if map_record else None
            if generated is None or not generated.is_file() or _sha256(generated) != expected:
                raise ValueError(f"release manifest generated map hash drifted: {key}")
    public_files = document.get("public_files")
    if not isinstance(public_files, list) or not all(isinstance(path, str) for path in public_files):
        raise ValueError("release manifest public_files is invalid")
    if package_root is not None and not (package_root / "build").is_dir():
        validate_public_file_members(package_root, public_files)


def validate_source_layout(root: Path, catalog: ContentCatalog | None = None) -> None:
    """Reject source/package paths that cannot represent direct room compilation."""
    catalog = catalog or load_content_catalog(root)
    for spec in catalog.enabled_maps():
        paths = (
            spec.source_file, spec.data["generated_output"], spec.relative_entities_path,
            spec.resource_base, spec.resource_path, spec.resource_owner,
        )
        if any(Path(value).is_absolute() or ".." in Path(value).parts for value in paths):
            raise ValueError(f"impossible source layout: {spec.key}")
        source = root / "vanillamaps" / spec.source_file
        if not source.is_file():
            raise ValueError(f"impossible source layout: missing source {spec.key}")
        if spec.manifest_path.parent != root / "manifests":
            raise ValueError(f"impossible source layout: manifest {spec.key}")


def stale_package_paths(root: Path) -> list[str]:
    """Return stale room markers from source contract inputs, not build output."""
    source_root = Path(__file__).resolve().parents[2]
    from tools.release.build_cache import validate_source_contract

    validate_source_contract(source_root)
    source_files = (
        "doom_eap/launcher/launcher_core.py",
        "doom_eap/launcher/launcher_integration.py",
        "doom_eap/launcher/launcher_platform.py",
        "doom_eap/content/physical_options.py",
        "scripts/build/playable_test.sh",
        "tools/release/room_payloads.py",
        "tools/validation/audit_item_notification_release.py",
        "tools/validation/validate_item_notification_package.py",
        "tools/validation/audit_packaged_transition_bridge.py",
        "tools/validation/release_layout.py",
    )
    matches = []
    for relative_name in source_files:
        path = source_root / relative_name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if any(marker.casefold() in text.casefold() for marker in STALE_PACKAGE_MARKERS):
            matches.append(relative_name)
    return sorted(matches)
