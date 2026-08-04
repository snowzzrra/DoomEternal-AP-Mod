"""Independent, map-scoped generated-content baselines and compact drift output."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from content_catalog import ContentCatalog, load_content_catalog, thaw_content
from map_registry import load_map_registry, release_plan
from tools.maps.ap_map_generator import generate_map
from tools.maps.mission_complete_map_patcher import patch_mission_complete_maps


ROOT = Path(__file__).resolve().parents[2]
BASELINES_DIR = ROOT / "baselines" / "maps"
DIAGNOSTICS_DIR = ROOT / ".cache" / "ap_pipeline" / "diagnostics"
IDENTITY_PATH = ROOT / "data" / "content_identity.json"


class BaselineDrift(ValueError):
    pass


def baseline_path(map_key: str) -> Path:
    return BASELINES_DIR / f"{map_key}.json"


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _semantic_hash(text: str) -> str:
    output: list[str] = []
    index = 0
    quoted = False
    while index < len(text):
        if not quoted and text.startswith("//", index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end + 1
            continue
        if not quoted and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise ValueError("Unterminated block comment in generated map")
            index = end + 2
            continue
        char = text[index]
        if char == '"' and (index == 0 or text[index - 1] != "\\"):
            quoted = not quoted
            output.append(char)
        elif quoted or not char.isspace():
            output.append(char)
        index += 1
    return hashlib.sha256("".join(output).encode()).hexdigest()


def _field_count(text: str, field: str) -> int:
    return len(re.findall(rf"\b{re.escape(field)}\s*=", text))


def _publisher_payload(catalog: ContentCatalog, map_key: str) -> list[dict]:
    return [
        {
            "key": publisher.key,
            "triggers": [dict(trigger) for trigger in publisher.triggers],
            "effects": [dict(effect) for effect in publisher.effects],
            "dedupe_scope": publisher.dedupe_scope,
            "fallback_policy": publisher.fallback_policy,
        }
        for publisher in catalog.publishers
        if publisher.map_key == map_key
    ]


def _asset_package_payload(catalog: ContentCatalog, map_key: str) -> dict:
    spec = catalog.map(map_key)
    return {
        "assets": [
            {
                "key": asset.key,
                "strategy": asset.strategy,
                "model": asset.model,
                "resource_base": asset.resource_base,
                "resource_owner": asset.resource_owner,
                "dependencies": asset.dependencies,
                "dependency_policy": asset.dependency_policy,
                "donor": dict(asset.donor),
                "replacement_slot_policy": asset.replacement_slot_policy,
                "replacement_slot": thaw_content(asset.replacement_slot),
                "usage_policy": asset.usage_policy,
                "preserve": asset.preserve,
                **(
                    {
                        "visual_presentation_policy": thaw_content(
                            asset.visual_presentation_policy
                        )
                    }
                    if asset.visual_presentation_policy
                    else {}
                ),
            }
            for asset in catalog.assets if asset.map_key == map_key
        ],
        "package": {
            "generated_output": spec.data["generated_output"],
            "resource_base": spec.resource_base,
            "resource_owner": spec.resource_owner,
            "relative_entities_path": spec.relative_entities_path,
        },
    }


def describe_generated_map(
    path: Path,
    manifest_path: Path,
    config_path: Path,
    *,
    map_key: str | None = None,
    catalog: ContentCatalog | None = None,
) -> dict:
    data = path.read_bytes()
    text = data.decode("utf-8")
    names = re.findall(r"\bentityDef\s+([^\s{]+)", text)
    event_ids = sorted({
        int(value) for value in re.findall(r"AP_CHECK_EVENT_(\d+)", text)
    })
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    catalog = catalog or load_content_catalog()
    if map_key is None:
        matches = [
            spec.key for spec in catalog.enabled_maps()
            if spec.level_config_path == config_path
        ]
        if len(matches) != 1:
            raise ValueError(f"cannot infer map key for baseline: {config_path}")
        map_key = matches[0]
    runtime_ids = sorted(
        item.location_id for item in catalog.runtime_locations
        if item.mission_key == map_key
    )
    runtime_set = set(runtime_ids)
    declared_physical = {
        item.location_id for item in catalog.physical_locations
        if item.map_key == map_key
    }
    physical_ids = sorted(value for value in event_ids if value in declared_physical)
    identity = json.loads(IDENTITY_PATH.read_text(encoding="utf-8"))
    return {
        "schema_version": 2,
        "map_key": map_key,
        "content_revision": identity["content_revision"],
        "physical_ap_ids": physical_ids,
        "runtime_location_ids": runtime_ids,
        "generated_entity_metrics": {
            "entity_count": len(names),
            "unique_entity_names": len(set(names)),
            "classes": _field_count(text, "class"),
            "targets": len(re.findall(r'item\[\d+\]\s*=\s*"[^"]+";', text)),
            "bind_parents": _field_count(text, "bindParent"),
            "layers": len(re.findall(r"\blayers\s*\{", text)),
            "transforms": sum(_field_count(text, field) for field in (
                "spawnPosition", "spawnOrientation", "renderModelInfo", "clipModelInfo"
            )),
            "byte_size": len(data),
        },
        "semantic_sha256": _semantic_hash(text),
        "byte_sha256": hashlib.sha256(data).hexdigest(),
        "manifest_sha256": _canonical_hash(manifest),
        "scripted_contract_sha256": _canonical_hash({
            "target_policies": config.get("target_policies", {}),
            "target_removals": config.get("target_removals", {}),
            "remove_entities": config.get("remove_entities", []),
            "neutralize_pickups": config.get("neutralize_pickups", []),
            "inline_currency_removals": config.get(
                "inline_currency_removals", []
            ),
            "secret_encounters": config.get("secret_encounters", []),
            "location_feedback": config.get("location_feedback", {}),
        }),
        "publisher_contract_sha256": _canonical_hash(
            _publisher_payload(catalog, map_key)
        ),
        "asset_package_contract_sha256": _canonical_hash(
            _asset_package_payload(catalog, map_key)
        ),
    }


def _diff(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> dict:
    result: dict[str, Any] = {}
    keys = sorted(set(expected) | set(actual))
    for key in keys:
        # Release identity is validated globally.  Map baselines are scoped to
        # map semantics so a version-only bump cannot force every frozen map
        # to accept an otherwise identical baseline.
        if key in {"acceptance", "content_revision"}:
            continue
        left, right = expected.get(key), actual.get(key)
        if isinstance(left, Mapping) and isinstance(right, Mapping):
            nested = _diff(left, right)
            if nested:
                result[key] = nested
        elif left != right:
            result[key] = {"expected": left, "actual": right}
    # Runtime publisher effects are validated by the normalized publisher
    # contract tests.  A runtime-only effect move must not require accepting a
    # new map baseline when the compiled map bytes and semantics are unchanged.
    if (
        "publisher_contract_sha256" in result
        and "byte_sha256" not in result
        and "semantic_sha256" not in result
    ):
        result.pop("publisher_contract_sha256")
    return result


def _classifications(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    diff: Mapping[str, Any],
) -> list[str]:
    result = []
    removed = set(expected.get("physical_ap_ids", ())) - set(
        actual.get("physical_ap_ids", ())
    )
    if removed and removed <= set(actual.get("runtime_location_ids", ())):
        result.append("runtime_location_moved_out_of_map")
    if "publisher_contract_sha256" in diff:
        result.append("publisher_graph_changed")
    if "asset_package_contract_sha256" in diff:
        result.append("asset_package_changed")
    return result or ["generated_content_changed"]


def format_drift(
    map_key: str,
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    diff: Mapping[str, Any],
) -> str:
    lines = [f"BASELINE DRIFT map={map_key}"]
    physical = diff.get("physical_ap_ids")
    if physical:
        removed = sorted(set(physical["expected"]) - set(physical["actual"]))
        added = sorted(set(physical["actual"]) - set(physical["expected"]))
        lines.append("  physical_ap_ids:")
        if removed:
            lines.append(f"    removed from physical output: {removed}")
        if added:
            lines.append(f"    added to physical output: {added}")
    metrics = diff.get("generated_entity_metrics", {})
    for key in ("entity_count", "targets"):
        if key in metrics:
            item = metrics[key]
            lines.append(f"  {key}: {item['expected']} -> {item['actual']}")
    for key in (
        "semantic_sha256", "byte_sha256", "manifest_sha256",
        "scripted_contract_sha256", "publisher_contract_sha256",
        "asset_package_contract_sha256",
    ):
        if key in diff:
            item = diff[key]
            lines.append(f"  {key}: {item['expected']} -> {item['actual']}")
    lines.extend(["", "Classification:"])
    lines.extend(f"  {item}" for item in _classifications(expected, actual, diff))
    lines.extend([
        "",
        "Reproduce:",
        f"  scripts/pipeline.sh map {map_key} --baseline-diff",
    ])
    return "\n".join(lines)


def assert_map_baseline(
    map_key: str,
    output: Path,
    generated_manifest: Path,
    *,
    diagnostics_dir: Path = DIAGNOSTICS_DIR,
) -> dict:
    catalog = load_content_catalog()
    actual = describe_generated_map(
        output, generated_manifest, catalog.map(map_key).level_config_path,
        map_key=map_key, catalog=catalog,
    )
    path = baseline_path(map_key)
    if not path.exists():
        raise BaselineDrift(
            f"BASELINE MISSING map={map_key}\nReproduce:\n"
            f"  scripts/pipeline.sh map {map_key} --baseline-diff"
        )
    expected = json.loads(path.read_text(encoding="utf-8"))
    diff = _diff(expected, actual)
    if diff:
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        diagnostic = diagnostics_dir / f"baseline-{map_key}.json"
        diagnostic.write_text(
            json.dumps({
                "map_key": map_key,
                "expected": expected,
                "actual": actual,
                "diff": diff,
            }, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        raise BaselineDrift(
            f"{format_drift(map_key, expected, actual, diff)}\n"
            f"\nDetailed diff: {diagnostic}"
        )
    return actual


def accept_map_baseline(
    map_key: str,
    output: Path,
    generated_manifest: Path,
    *,
    reason: str,
) -> Path:
    if not reason.strip():
        raise ValueError("baseline acceptance requires --reason")
    catalog = load_content_catalog()
    actual = describe_generated_map(
        output, generated_manifest, catalog.map(map_key).level_config_path,
        map_key=map_key, catalog=catalog,
    )
    path = baseline_path(map_key)
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    previous_acceptance = previous.get("acceptance", [])
    if isinstance(previous_acceptance, Mapping):
        previous_acceptance = [dict(previous_acceptance)]
    entry = {
        "reason": reason.strip(),
        "content_revision": actual["content_revision"],
    }
    actual["acceptance"] = [
        *[item for item in previous_acceptance if item != entry],
        entry,
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(actual, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def generate_frozen_outputs(
    registry_path: Path | None = None,
) -> tuple[dict, tempfile.TemporaryDirectory]:
    """Compatibility helper for isolated legacy tests; pipeline never calls it."""
    registry = load_map_registry(registry_path or ROOT / "data" / "map_sources.json")
    temporary = tempfile.TemporaryDirectory()
    temp_root = Path(temporary.name)
    generated = temp_root / "generated"
    generated.mkdir()
    mod_root = temp_root / "mod"
    results: dict[str, tuple[Path, Path, Path]] = {}
    items = json.loads((ROOT / "data" / "items.json").read_text(encoding="utf-8"))
    for plan in release_plan(registry):
        output = generated / plan.generated_output
        manifest = generated / f"{plan.map_key}.json"
        config = ROOT / plan.level_config
        generate_map(
            ROOT / "vanillamaps" / plan.source_file, output, config, manifest,
            items,
        )
        results[plan.map_key] = (output, manifest, config)
    patch_mission_complete_maps(
        ROOT / "data" / "mission_complete_map_contracts.json",
        {key: value[0] for key, value in results.items()}, mod_root,
    )
    return results, temporary


def assert_frozen_map_baselines(map_key: str | None = None) -> dict:
    """Legacy API with compact errors; prefer pipeline-provided output paths."""
    outputs, temporary = generate_frozen_outputs()
    try:
        keys = (map_key,) if map_key else tuple(outputs)
        return {
            key: assert_map_baseline(key, outputs[key][0], outputs[key][1])
            for key in keys
        }
    finally:
        temporary.cleanup()
