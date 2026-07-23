"""Normalized semantic diagnostics plus mandatory byte baselines for frozen maps."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from pathlib import Path

from map_registry import load_map_registry, release_plan
from tools.maps.ap_map_generator import generate_map
from tools.maps.mission_complete_map_patcher import patch_mission_complete_maps

ROOT = Path(__file__).resolve().parent.parent.parent
BASELINE_PATH = ROOT / "data" / "frozen_map_baselines.json"
def _semantic_hash(text: str) -> str:
    """Hash syntax while skipping comments/irrelevant whitespace without copies."""
    digest = hashlib.sha256()
    quoted = False
    escaped = False
    block_comment = False
    for line in text.splitlines():
        normalized: list[str] = []
        index = 0
        while index < len(line):
            char = line[index]
            following = line[index + 1] if index + 1 < len(line) else ""
            if block_comment:
                if char == "*" and following == "/":
                    block_comment = False
                    index += 2
                    continue
                index += 1
                continue
            if not quoted and char == "/" and following == "/":
                break
            if not quoted and char == "/" and following == "*":
                block_comment = True
                index += 2
                continue
            if quoted or not char.isspace():
                normalized.append(char)
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            index += 1
        if normalized:
            digest.update("".join(normalized).encode("utf-8"))
    if block_comment:
        raise ValueError("Unterminated block comment in generated map")
    return digest.hexdigest()


def _field_count(text: str, field: str) -> int:
    return len(re.findall(rf"\b{re.escape(field)}\s*=", text))


def describe_generated_map(path: Path, manifest_path: Path, config_path: Path) -> dict:
    data = path.read_bytes()
    text = data.decode("utf-8")
    names = re.findall(r"\bentityDef\s+([^\s{]+)", text)
    ap_ids = sorted({int(value) for value in re.findall(r"AP_CHECK_EVENT_(\d+)", text)})
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    return {
        "byte_sha256": hashlib.sha256(data).hexdigest(),
        "byte_size": len(data),
        "semantic_sha256": _semantic_hash(text),
        "entity_count": len(names),
        "unique_entity_names": len(set(names)),
        "classes": _field_count(text, "class"),
        "targets": len(re.findall(r'item\[\d+\]\s*=\s*"[^\"]+";', text)),
        "bind_parents": _field_count(text, "bindParent"),
        "layers": len(re.findall(r"\blayers\s*\{", text)),
        "transforms": sum(_field_count(text, field) for field in (
            "spawnPosition", "spawnOrientation", "renderModelInfo", "clipModelInfo"
        )),
        "ap_ids": ap_ids,
        "manifest_sha256": hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "scripted_contract_sha256": hashlib.sha256(
            json.dumps({
                "target_policies": config.get("target_policies", {}),
                "target_removals": config.get("target_removals", {}),
                "remove_entities": config.get("remove_entities", []),
                "neutralize_pickups": config.get("neutralize_pickups", []),
                "secret_encounters": config.get("secret_encounters", []),
            }, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def generate_frozen_outputs(registry_path: Path | None = None) -> tuple[dict, tempfile.TemporaryDirectory]:
    registry = load_map_registry(registry_path or ROOT / "data" / "map_sources.json")
    temporary = tempfile.TemporaryDirectory()
    temp_root = Path(temporary.name)
    generated = temp_root / "generated"
    generated.mkdir()
    mod_root = temp_root / "mod"
    results: dict[str, tuple[Path, Path, Path]] = {}
    for plan in release_plan(registry):
        output = generated / plan.generated_output
        manifest = generated / f"{plan.map_key}.json"
        config = ROOT / plan.level_config
        generate_map(
            ROOT / "vanillamaps" / plan.source_file, output, config, manifest,
            json.loads((ROOT / "data" / "items.json").read_text(encoding="utf-8")),
            enable_notification_lab=False,
        )
        results[plan.map_key] = (output, manifest, config)
    patch_mission_complete_maps(
        ROOT / "data" / "mission_complete_map_contracts.json",
        {key: value[0] for key, value in results.items()}, mod_root,
    )
    return results, temporary


def current_baseline() -> dict:
    outputs, temporary = generate_frozen_outputs()
    try:
        registry = load_map_registry()
        return {
            "schema_version": 1,
            "normalization": "comments and whitespace outside quoted strings ignored",
            "coverage": [
                "entity names", "classes", "targets/order", "bindParent", "layers",
                "transforms", "AP IDs", "manifests", "scripted contracts",
            ],
            "baseline_map_keys": registry["baseline_map_keys"],
            "maps": {
                key: describe_generated_map(*outputs[key])
                for key in registry["baseline_map_keys"]
            },
        }
    finally:
        temporary.cleanup()


def assert_frozen_map_baselines() -> dict:
    expected = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    actual = current_baseline()
    if actual != expected:
        raise ValueError(f"Frozen map baseline drift: expected={expected!r}, actual={actual!r}")
    return actual


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    rendered = json.dumps(current_baseline(), indent=2, sort_keys=True) + "\n"
    if args.update:
        BASELINE_PATH.write_text(rendered, encoding="utf-8")
        print(f"Updated {BASELINE_PATH}")
    else:
        print(rendered, end="")
