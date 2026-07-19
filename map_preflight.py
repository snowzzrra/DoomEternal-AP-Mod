"""Fail-closed onboarding audit for maps not yet admitted to the frozen baseline."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ap_map_generator import extract_target_names, find_entity_block_bounds
from map_registry import validation_plan


AUDIT_KEYS = {
    "schema_version", "map_key", "source_sha256", "resource_owner",
    "resource_priority", "mission_complete_transition", "layers", "checkpoints",
    "movers", "gates", "new_decl_resources", "locations",
}
LOCATION_KEYS = {
    "entity", "class", "inherit", "ap_id", "ap_name", "entity_match_count", "original_targets",
    "reward_grant_currency_ownership_edges", "progression_objective_relays",
    "drop_targets", "bind_parent", "local_transform", "layers", "checkpoints",
    "movers", "gates", "conditional_pickup_behavior",
}
EDGE_KEYS = {"target", "classification", "disposition"}
TRANSITION_KEYS = {"kind", "owner", "target", "classification"}
EDGE_CLASSIFICATIONS = {
    "reward", "grant", "currency", "ownership", "progression", "objective",
    "cosmetic", "removal", "other_proven",
}


def _exact_keys(value: dict, expected: set[str], label: str) -> None:
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing or unknown:
        raise ValueError(f"{label} fields: missing={sorted(missing)}, unknown={sorted(unknown)}")


def validate_onboarding_audit(
    root: Path,
    map_key: str,
    source: dict[str, Any],
    canonical_locations: dict[str, int],
    canonical_item_ids: set[int],
    container_catalog: set[str],
) -> None:
    for field in ("source_file", "level_config", "manifest"):
        prefix = "vanillamaps/" if field == "source_file" else ""
        path = root / prefix / source[field]
        if not path.exists():
            raise ValueError(f"{map_key}: missing {field}: {path}")
    if source["resource_path"] not in container_catalog:
        raise ValueError(f"{map_key}: resource container is absent from catalog")
    source_path = root / "vanillamaps" / source["source_file"]
    actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if actual_hash != source["source_sha256"]:
        raise ValueError(f"{map_key}: source hash mismatch")
    audit_path = root / source["onboarding_audit"]
    if not audit_path.exists():
        raise ValueError(f"{map_key}: onboarding audit does not exist")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    _exact_keys(audit, AUDIT_KEYS, f"{map_key} audit")
    if audit["schema_version"] != 1 or audit["map_key"] != map_key:
        raise ValueError(f"{map_key}: onboarding audit identity mismatch")
    for field in ("source_sha256", "resource_owner", "resource_priority"):
        if audit[field] != source[field]:
            raise ValueError(f"{map_key}: audit {field} drift")
    transition = audit["mission_complete_transition"]
    _exact_keys(transition, TRANSITION_KEYS, f"{map_key} Mission Complete")
    if any(not transition[key] for key in TRANSITION_KEYS):
        raise ValueError(f"{map_key}: Mission Complete transition is incomplete")
    if audit["new_decl_resources"]:
        for decl in audit["new_decl_resources"]:
            if set(decl) != {"path", "replaces_owner", "source_sha256", "container"}:
                raise ValueError(f"{map_key}: new DECL proof fields are incomplete")
            if not all(decl.values()) or decl["container"] not in container_catalog:
                raise ValueError(f"{map_key}: new DECL lacks proven replacement owner")
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    source_text = source_path.read_text(encoding="utf-8")
    config = json.loads((root / source["level_config"]).read_text(encoding="utf-8"))
    for index, location in enumerate(audit["locations"]):
        label = f"{map_key} location[{index}]"
        _exact_keys(location, LOCATION_KEYS, label)
        bounds = find_entity_block_bounds(source_text, location["entity"])
        if source_text.count(f"entityDef {location['entity']}") != 1 or location["entity_match_count"] != 1 or bounds is None:
            raise ValueError(f"{label}: entity must exist uniquely")
        source_block = source_text[bounds[0]:bounds[1]]
        class_match = re.search(r'class\s*=\s*"([^"]+)";', source_block)
        inherit_match = re.search(r'inherit\s*=\s*"([^"]+)";', source_block)
        if not class_match or location["class"] != class_match.group(1):
            raise ValueError(f"{label}: native class is missing or drifted")
        if not inherit_match or location["inherit"] != inherit_match.group(1):
            raise ValueError(f"{label}: native inherit is missing or drifted")
        if canonical_locations.get(location["ap_name"]) != location["ap_id"]:
            raise ValueError(f"{label}: AP name/ID is not synchronized with APWorld")
        if location["ap_id"] in canonical_item_ids:
            raise ValueError(f"{label}: AP location ID collides with an item ID")
        if location["ap_id"] in seen_ids or location["ap_name"] in seen_names:
            raise ValueError(f"{label}: duplicate AP name/ID")
        seen_ids.add(location["ap_id"])
        seen_names.add(location["ap_name"])
        classified_targets = set()
        for edge_field in ("original_targets", "reward_grant_currency_ownership_edges", "progression_objective_relays"):
            for edge in location[edge_field]:
                _exact_keys(edge, EDGE_KEYS, f"{label} {edge_field}")
                if not all(edge.values()):
                    raise ValueError(f"{label}: unclassified edge")
                if edge["classification"] not in EDGE_CLASSIFICATIONS:
                    raise ValueError(f"{label}: unknown edge classification")
                if edge["disposition"] not in {"preserve", "drop"}:
                    raise ValueError(f"{label}: edge disposition must be preserve/drop")
                classified_targets.add(edge["target"])
        original_edges = location["original_targets"]
        original_names = [edge["target"] for edge in original_edges]
        if original_names != extract_target_names(source_block):
            raise ValueError(f"{label}: original targets are incomplete, reordered or drifted")
        audited_drops = [
            edge["target"] for edge in original_edges if edge["disposition"] == "drop"
        ]
        if location["drop_targets"] != audited_drops:
            raise ValueError(f"{label}: drop_targets does not match individual classifications")
        policy = config.get("target_policies", {}).get(location["entity"], {})
        if policy.get("drop_targets", []) != audited_drops:
            raise ValueError(f"{label}: generator drop_targets is not synchronized with audit")
        if not isinstance(location["local_transform"], dict) or not location["local_transform"]:
            raise ValueError(f"{label}: local transform is not recorded")
        for snippet in location["local_transform"].values():
            if not isinstance(snippet, str) or snippet not in source_block:
                raise ValueError(f"{label}: local transform does not match source")
        bind_match = re.search(r'bindParent\s*=\s*"([^"]+)";', source_block)
        actual_bind = bind_match.group(1) if bind_match else None
        if location["bind_parent"] != actual_bind:
            raise ValueError(f"{label}: bindParent is not preserved from source")
        if not location["conditional_pickup_behavior"]:
            raise ValueError(f"{label}: conditional pickup behavior is not recorded")


def validate_registry_preflight(
    root: Path,
    registry: dict[str, Any],
    canonical_locations: dict[str, int],
    canonical_item_ids: set[int],
    container_catalog: set[str],
) -> None:
    for plan in validation_plan(registry):
        source = registry["maps"][plan.map_key]
        if source["onboarding_status"] == "onboarding":
            validate_onboarding_audit(
                root, plan.map_key, source, canonical_locations,
                canonical_item_ids, container_catalog,
            )
