"""Generated checked-location presentation registry and packaged loader."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

SCHEMA_VERSION = 1
REGISTRY_REVISION = "checked-location-visuals-v2"
ENTITY_NAME_RE = re.compile(r"^ap_[A-Za-z0-9_]+$")
FORBIDDEN_VISUAL_TERMS = (
    "targets", "reward", "useable", "inventory", "currency", "functional",
    "objective", "encounter", "door", "elevator", "mission complete",
    "mission_complete",
)
FORBIDDEN_CLEANUP_TERMS = (
    "vanilla", "progression", "relay", "objective", "encounter", "door",
    "elevator", "mission complete", "mission_complete",
)


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resource_identity(spec: Any) -> dict[str, Any]:
    return {
        "resource_base": spec.resource_base,
        "resource_path": spec.resource_path,
        "resource_owner": spec.resource_owner,
        "resource_priority": spec.resource_priority,
        "relative_entities_path": spec.relative_entities_path,
    }


def _packaged_resource_identity(source: dict[str, Any]) -> dict[str, Any]:
    resource_path = source.get("resource_path", "")
    resource_base = re.sub(r"_patch\d+(?=\.resources$)", "", Path(resource_path).name)
    return {
        "resource_base": Path(resource_base).stem,
        "resource_path": resource_path,
        "resource_owner": source.get("resource_owner", ""),
        "resource_priority": source.get("resource_priority", 0),
        "relative_entities_path": source.get("relative_entities_path", ""),
    }


def _manifest_rows(root: Path) -> list[dict[str, Any]]:
    sources = json.loads((root / "data" / "map_sources.json").read_text(encoding="utf-8"))["maps"]
    rows = []
    for map_key, source in sorted(sources.items()):
        if not source.get("enabled", True):
            continue
        manifest = json.loads(
            (root / "manifests" / f"{map_key}.json").read_text(encoding="utf-8")
        )
        for ap_check, location_id in sorted(manifest.items()):
            rows.append({
                "map_key": map_key,
                "runtime_map": source["runtime_map"],
                "ap_check": ap_check,
                "location_id": location_id,
                "source_file": source.get("source_file", ""),
                "source_sha256": source.get("source_sha256", ""),
                "resource_path": source.get("resource_path", ""),
                "resource_owner": source.get("resource_owner", ""),
                "resource_priority": source.get("resource_priority", 0),
                "relative_entities_path": source.get("relative_entities_path", ""),
            })
    return rows


def _catalog_fingerprint(root: Path) -> str:
    return _canonical_hash(_manifest_rows(root))


def _authoritative_fingerprint(root: Path, catalog: Any) -> str:
    maps = []
    for map_key, spec in sorted(catalog.maps.items()):
        if not spec.enabled:
            continue
        descriptor = root / "content" / "maps" / map_key / "descriptor.json"
        maps.append({
            "map_key": map_key,
            "descriptor_sha256": _sha256(descriptor),
            "source_file": spec.source_file,
            "source_sha256": spec.source_sha256,
            "resource": _resource_identity(spec),
        })
    locations = []
    from content_catalog import thaw_content
    for location in sorted(catalog.physical_locations, key=lambda item: (item.map_key, item.location_id)):
        if not catalog.maps[location.map_key].enabled:
            continue
        locations.append({
            "name": location.name,
            "location_id": location.location_id,
            "map_key": location.map_key,
            "ap_check": location.ap_check,
            "region": location.region,
            "strategy": location.strategy,
            "policy": thaw_content(location.policy),
        })
    return _canonical_hash({"maps": maps, "locations": locations})


def _resolved_visual_policy(location_id: int, policy: dict[str, Any]) -> dict[str, Any]:
    from tools.maps.ap_map_generator import resolved_automap_visual_policy

    return resolved_automap_visual_policy(location_id, policy)


def build_authorial_registry(root: Path) -> dict[str, Any]:
    from content_catalog import load_content_catalog, thaw_content

    catalog = load_content_catalog(root)
    identity = json.loads((root / "data" / "content_identity.json").read_text(encoding="utf-8"))
    entries = []
    for location in sorted(catalog.physical_locations, key=lambda item: (item.map_key, item.location_id)):
        spec = catalog.maps[location.map_key]
        if not spec.enabled:
            continue
        source_path = spec.level_config_path
        if location.strategy == "secret_encounter":
            resolved = {"classification": "event_only", "policy": "secret_encounter"}
        else:
            resolved = _resolved_visual_policy(location.location_id, thaw_content(location.policy))
        source_identity = {
            "source_file": str(source_path.relative_to(root)),
            "source_sha256": _sha256(source_path),
            "descriptor_sha256": _sha256(root / "content" / "maps" / location.map_key / "descriptor.json"),
            "resource": _resource_identity(spec),
            "resource_sha256": _canonical_hash(_resource_identity(spec)),
            "source_entity": location.ap_check.removeprefix("AP_CHECK_"),
            "policy_sha256": _canonical_hash(thaw_content(location.policy)),
            "location_descriptor_sha256": _canonical_hash({
                "name": location.name,
                "location_id": location.location_id,
                "map_key": location.map_key,
                "ap_check": location.ap_check,
                "region": location.region,
                "strategy": location.strategy,
                "policy": thaw_content(location.policy),
            }),
            "resolved_policy": resolved["policy"],
        }
        entry = {
            "map_key": location.map_key,
            "runtime_map": spec.runtime_map,
            "location_id": location.location_id,
            "ap_check": location.ap_check,
            "classification": resolved["classification"],
            "source_identity": source_identity,
        }
        if resolved["classification"] == "visible_cleanup":
            entry.update({
                "presentation_entity": resolved["presentation_entity"],
                "cleanup_entity": resolved["cleanup_entity"],
                "direct_target": resolved["presentation_entity"],
            })
        entries.append(entry)
    counts = {
        classification: sum(entry["classification"] == classification for entry in entries)
        for classification in ("visible_cleanup", "no_visual", "event_only")
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "registry_revision": REGISTRY_REVISION,
        "content_revision": identity["content_revision"],
        "compiler_revision": identity["compiler_revision"],
        "catalog_fingerprint": _catalog_fingerprint(root),
        "maps": {
            map_key: {
                "runtime_map": spec.runtime_map,
                "source_file": spec.source_file,
                "source_sha256": spec.source_sha256,
                "descriptor_sha256": _sha256(root / "content" / "maps" / map_key / "descriptor.json"),
                "resource": _resource_identity(spec),
                "resource_sha256": _canonical_hash(_resource_identity(spec)),
            }
            for map_key, spec in sorted(catalog.maps.items()) if spec.enabled
        },
        "authoritative_fingerprint": _authoritative_fingerprint(root, catalog),
        "counts": counts,
        "entries": entries,
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _require_hash(value: Any, label: str) -> None:
    _require(isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value)), f"invalid {label}")


def _validate_common(document: Any) -> dict[str, Any]:
    _require(isinstance(document, dict), "checked-location visuals must be object")
    _require(document.get("schema_version") == SCHEMA_VERSION, "checked-location visuals schema mismatch")
    _require(document.get("registry_revision") == REGISTRY_REVISION, "checked-location visuals revision mismatch")
    entries = document.get("entries")
    _require(isinstance(entries, list), "checked-location visuals entries missing")
    _require(isinstance(document.get("maps"), dict), "checked-location visuals maps missing")
    seen = set()
    for entry in entries:
        _require(isinstance(entry, dict), "checked-location visual entry must be object")
        required = {"map_key", "runtime_map", "location_id", "ap_check", "classification", "source_identity"}
        _require(set(entry) >= required, "checked-location visual entry fields missing")
        identity = (entry["map_key"], entry["location_id"])
        _require(identity not in seen, f"duplicate checked-location visual entry: {identity}")
        seen.add(identity)
        _require(isinstance(entry["location_id"], int) and not isinstance(entry["location_id"], bool), "invalid location ID")
        _require(isinstance(entry["ap_check"], str) and entry["ap_check"].startswith("AP_CHECK_"), "invalid AP check")
        _require(isinstance(entry["source_identity"], dict), "invalid source identity")
        source_identity = entry["source_identity"]
        for field in (
            "source_sha256", "descriptor_sha256", "resource_sha256",
            "policy_sha256", "location_descriptor_sha256",
        ):
            _require_hash(source_identity.get(field), f"source identity {field}")
        _require(isinstance(source_identity.get("source_file"), str), "invalid source identity source_file")
        _require(isinstance(source_identity.get("source_entity"), str), "invalid source identity source_entity")
        _require(isinstance(source_identity.get("resource"), dict), "invalid source identity resource")
        _require(source_identity["resource_sha256"] == _canonical_hash(source_identity["resource"]), "source resource hash drift")
        _require(entry["classification"] in {"visible_cleanup", "no_visual", "event_only"}, "invalid visual classification")
        if entry["classification"] == "visible_cleanup":
            for field in ("presentation_entity", "cleanup_entity", "direct_target"):
                _require(bool(isinstance(entry.get(field), str) and ENTITY_NAME_RE.fullmatch(entry[field])), f"invalid visual target name: {field}")
            _require(entry["direct_target"] == entry["presentation_entity"], "visual direct target mismatch")
        else:
            _require(not any(field in entry for field in ("presentation_entity", "cleanup_entity", "direct_target")), "special visual entry has cleanup fields")
    counts = document.get("counts")
    _require(isinstance(counts, dict), "checked-location visuals counts missing")
    actual = {kind: sum(entry["classification"] == kind for entry in entries) for kind in ("visible_cleanup", "no_visual", "event_only")}
    _require(counts == actual, "checked-location visuals counts stale")
    return document


def load_automap_visual_registry(path: Path | None = None) -> dict[str, Any]:
    if path is None:
        path = Path(__file__).resolve().with_name("data") / "checked_location_visuals.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        _validate_common(document)
        identity = json.loads(path.with_name("content_identity.json").read_text(encoding="utf-8"))
        _require(document["content_revision"] == identity["content_revision"], "visual registry content revision stale")
        _require(document["compiler_revision"] == identity["compiler_revision"], "visual registry compiler revision stale")
        root = path.parent.parent
        expected_rows = _manifest_rows(root)
        expected = {(row["map_key"], row["location_id"], row["ap_check"]): row for row in expected_rows}
        actual = {(entry["map_key"], entry["location_id"], entry["ap_check"]): entry for entry in document["entries"]}
        _require(set(actual) == set(expected), "visual registry diverges from packaged catalog")
        _require(document["catalog_fingerprint"] == _canonical_hash(expected_rows), "visual registry catalog fingerprint stale")
        _require(set(document["maps"]) == {row["map_key"] for row in expected_rows}, "visual registry map catalog stale")
        for key, row in expected.items():
            entry = actual[key]
            _require(entry["runtime_map"] == row["runtime_map"], f"visual registry runtime map stale: {key[0]}")
        expected_maps = {
            map_key: {
                "runtime_map": source["runtime_map"],
                "source_file": source.get("source_file", ""),
                "source_sha256": source.get("source_sha256", ""),
                "resource": _packaged_resource_identity(source),
            }
            for map_key, source in json.loads((root / "data" / "map_sources.json").read_text(encoding="utf-8"))["maps"].items()
            if source.get("enabled", True)
        }
        for map_key, expected_map in expected_maps.items():
            actual_map = document["maps"].get(map_key)
            _require(isinstance(actual_map, dict), f"visual registry map metadata missing: {map_key}")
            _require(actual_map["runtime_map"] == expected_map["runtime_map"], f"visual registry map runtime stale: {map_key}")
            _require(actual_map["source_file"] == expected_map["source_file"], f"visual registry map source stale: {map_key}")
            _require(actual_map["source_sha256"] == expected_map["source_sha256"], f"visual registry map source hash stale: {map_key}")
            _require(actual_map["resource"] == expected_map["resource"], f"visual registry map resource stale: {map_key}")
            _require(actual_map["resource_sha256"] == _canonical_hash(actual_map["resource"]), f"visual registry resource hash invalid: {map_key}")
            _require_hash(actual_map.get("descriptor_sha256"), f"map descriptor hash: {map_key}")
        _require(bool(re.fullmatch(r"[0-9a-f]{64}", document.get("authoritative_fingerprint", ""))), "visual registry authoritative fingerprint invalid")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError(f"checked-location visual registry rejected: {error}") from error
    return document


def index_automap_visual_registry(document: dict[str, Any]) -> dict[str, dict[int, dict[str, Any]]]:
    result: dict[str, dict[int, dict[str, Any]]] = {}
    for entry in document["entries"]:
        result.setdefault(entry["map_key"], {})[entry["location_id"]] = entry
    return result


def validate_generated_visuals(document: dict[str, Any], map_key: str, content: str) -> None:
    from tools.maps.ap_map_generator import extract_target_names, find_entity_block_bounds

    entries = [entry for entry in document["entries"] if entry["map_key"] == map_key]
    expected_visuals = {
        entry["presentation_entity"]
        for entry in entries
        if entry["classification"] == "visible_cleanup"
    }
    expected_cleanups = {
        entry["cleanup_entity"]
        for entry in entries
        if entry["classification"] == "visible_cleanup"
    }
    actual_visuals = set(re.findall(r"entityDef\s+(ap_location_visual_[0-9]+)\s*\{", content))
    actual_cleanups = set(re.findall(r"entityDef\s+(ap_remove_location_visual_[0-9]+)\s*\{", content))
    _require(actual_visuals == expected_visuals, f"generated AP presentation set drift: {map_key}")
    _require(actual_cleanups == expected_cleanups, f"generated AP cleanup set drift: {map_key}")
    forbidden_cleanup = tuple(term.casefold() for term in FORBIDDEN_CLEANUP_TERMS)
    for entry in entries:
        if entry["classification"] != "visible_cleanup":
            default_visual = f"ap_location_visual_{entry['location_id']}"
            default_cleanup = f"ap_remove_location_visual_{entry['location_id']}"
            _require(find_entity_block_bounds(content, default_visual) is None, f"special location has AP presentation: {entry['location_id']}")
            _require(find_entity_block_bounds(content, default_cleanup) is None, f"special location has AP cleanup: {entry['location_id']}")
            continue
        visual_bounds = find_entity_block_bounds(content, entry["presentation_entity"])
        cleanup_bounds = find_entity_block_bounds(content, entry["cleanup_entity"])
        _require(visual_bounds is not None, f"missing AP presentation entity: {entry['presentation_entity']}")
        _require(cleanup_bounds is not None, f"missing AP cleanup entity: {entry['cleanup_entity']}")
        visual_start, visual_end = cast(tuple[int, int], visual_bounds)
        cleanup_start, cleanup_end = cast(tuple[int, int], cleanup_bounds)
        visual = content[visual_start:visual_end]
        cleanup = content[cleanup_start:cleanup_end]
        _require(extract_target_names(cleanup) == [entry["direct_target"]], f"cleanup target graph drift: {entry['location_id']}")
        _require('class = "idTarget_Remove";' in cleanup, f"cleanup class drift: {entry['location_id']}")
        _require(not any(term in visual.casefold() for term in FORBIDDEN_VISUAL_TERMS), f"functional AP presentation entity: {entry['location_id']}")
        _require(not any(term in cleanup.casefold() for term in forbidden_cleanup), f"forbidden cleanup graph: {entry['location_id']}")
