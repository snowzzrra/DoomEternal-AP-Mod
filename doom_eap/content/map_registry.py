"""Single source of truth for map generation, validation and packaging plans."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT = REPO_ROOT
DEFAULT_REGISTRY_PATH = ROOT / "data" / "map_sources.json"  # compiled runtime projection
ROOT_KEYS = {"schema_version", "baseline_map_keys", "maps"}
MAP_KEYS = {
    "display_name", "enabled", "test_only", "onboarding_status",
    "source_file", "source_sha256", "source_owner", "level_config", "manifest",
    "generated_output", "runtime_map", "resource_path", "resource_owner",
    "resource_priority", "relative_entities_path", "supported_game_revision",
    "onboarding_audit",
}


@dataclass(frozen=True)
class MapPlan:
    map_key: str
    display_name: str
    source_file: str
    source_sha256: str
    level_config: str
    manifest: str
    generated_output: str
    runtime_map: str
    resource_path: str
    relative_entities_path: str
    supported_game_revision: str
    release_asset: bool

    @property
    def client_manifest(self) -> str:
        return f"client/{self.manifest}"


def _catalog_registry(root: Path = ROOT) -> dict[str, Any]:
    from doom_eap.content.content_catalog import load_content_catalog

    catalog = load_content_catalog(root)
    maps = {}
    for key, spec in catalog.maps.items():
        raw = dict(spec.data)
        maps[key] = {
            field: raw.get(field)
            for field in MAP_KEYS
        }
        maps[key].update({
            "display_name": spec.display_name,
            "enabled": spec.enabled,
            "test_only": bool(raw.get("test_only", False)),
            "onboarding_status": raw.get("onboarding_status", "implementation_candidate"),
            "source_file": spec.source_file,
            "source_sha256": spec.source_sha256,
            "source_owner": raw.get("source_owner", spec.source_file),
            "level_config": str(spec.level_config_path.relative_to(root)),
            "manifest": str(spec.manifest_path.relative_to(root)),
            "generated_output": raw["generated_output"],
            "runtime_map": spec.runtime_map,
            "resource_path": spec.resource_path,
            "resource_owner": spec.resource_owner,
            "resource_priority": spec.resource_priority,
            "relative_entities_path": spec.relative_entities_path,
            "supported_game_revision": spec.supported_game_revision,
            "onboarding_audit": str(spec.onboarding_path.relative_to(root)),
        })
    return {
        "schema_version": 1,
        "baseline_map_keys": [
            key for key, spec in catalog.maps.items()
            if spec.data.get("onboarding_status") == "frozen_baseline"
        ],
        "maps": maps,
    }


def load_runtime_map_registry(data_dir: Path | None = None) -> dict[str, Any]:
    """Load compiled map_sources.json projection for runtime/client consumption."""
    data_dir = data_dir or (REPO_ROOT / "data")
    path = data_dir / "map_sources.json"
    registry = json.loads(path.read_text(encoding="utf-8"))
    validate_map_registry(registry)
    return registry


def load_authorial_map_registry(root: Path = ROOT) -> dict[str, Any]:
    """Load authorial map registry via content catalog discovery (build/dev only)."""
    return _catalog_registry(root)


def load_map_registry(
    path: Path | None = None,
    *,
    root: Path = ROOT,
    authorial: bool = False,
) -> dict[str, Any]:
    if authorial or root.resolve() != ROOT.resolve():
        return load_authorial_map_registry(root)
    if path is not None and path.resolve() != (root / "data" / "map_sources.json").resolve():
        registry = json.loads(path.read_text(encoding="utf-8"))
        validate_map_registry(registry)
        return registry
    return load_runtime_map_registry(root / "data")


def validate_map_registry(registry: dict[str, Any]) -> None:
    unknown_root = set(registry) - ROOT_KEYS
    if unknown_root:
        raise ValueError(f"Unknown map registry key(s): {sorted(unknown_root)}")
    if registry.get("schema_version") != 1:
        raise ValueError("map registry schema_version must be 1")
    maps = registry.get("maps")
    if not isinstance(maps, dict) or not maps:
        raise ValueError("map registry maps must be a non-empty object")
    baseline = registry.get("baseline_map_keys")
    if not isinstance(baseline, list) or len(baseline) != len(set(baseline)):
        raise ValueError("baseline_map_keys must be a unique list")
    missing_baseline = set(baseline) - set(maps)
    if missing_baseline:
        raise ValueError(f"Unknown baseline map key(s): {sorted(missing_baseline)}")
    seen_outputs: set[str] = set()
    seen_runtime_maps: set[str] = set()
    for map_key, source in maps.items():
        if not isinstance(source, dict):
            raise ValueError(f"Map {map_key} must be an object")
        unknown = set(source) - MAP_KEYS
        missing = MAP_KEYS - set(source)
        if unknown or missing:
            raise ValueError(
                f"Map {map_key} registry fields: missing={sorted(missing)}, "
                f"unknown={sorted(unknown)}"
            )
        if source["onboarding_status"] not in {
            "frozen_baseline", "onboarding", "implementation_candidate",
        }:
            raise ValueError(f"Map {map_key} has invalid onboarding_status")
        if source["test_only"] and source["onboarding_status"] != "onboarding":
            raise ValueError(f"Test-only map {map_key} must use onboarding status")
        for key in MAP_KEYS - {"test_only", "enabled", "onboarding_audit"}:
            if source[key] in (None, ""):
                raise ValueError(f"Map {map_key} is missing {key}")
        if not isinstance(source["resource_priority"], int) or source["resource_priority"] < 0:
            raise ValueError(f"Map {map_key} resource_priority must be a known nonnegative index")
        if source["generated_output"] in seen_outputs:
            raise ValueError(f"Duplicate generated output: {source['generated_output']}")
        if source["runtime_map"] in seen_runtime_maps:
            raise ValueError(f"Duplicate runtime map: {source['runtime_map']}")
        seen_outputs.add(source["generated_output"])
        seen_runtime_maps.add(source["runtime_map"])
        if (
            source["onboarding_status"] in {"onboarding", "implementation_candidate"}
            and not source["onboarding_audit"]
        ):
            raise ValueError(f"Onboarding map {map_key} lacks onboarding_audit")
def _plans(registry: dict[str, Any]) -> tuple[MapPlan, ...]:
    return tuple(
        MapPlan(
            map_key=map_key,
            display_name=source["display_name"],
            source_file=source["source_file"],
            source_sha256=source["source_sha256"],
            level_config=source["level_config"],
            manifest=source["manifest"],
            generated_output=source["generated_output"],
            runtime_map=source["runtime_map"],
            resource_path=source["resource_path"],
            relative_entities_path=source["relative_entities_path"],
            supported_game_revision=source["supported_game_revision"],
            release_asset=not source["test_only"],
        )
        for map_key, source in registry["maps"].items()
        if source["enabled"]
    )


def generation_plan(registry: dict[str, Any]) -> tuple[MapPlan, ...]:
    return _plans(registry)


def validation_plan(registry: dict[str, Any]) -> tuple[MapPlan, ...]:
    return _plans(registry)


def package_plan(registry: dict[str, Any]) -> tuple[MapPlan, ...]:
    return _plans(registry)


def release_plan(registry: dict[str, Any]) -> tuple[MapPlan, ...]:
    return tuple(plan for plan in package_plan(registry) if plan.release_asset)


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("release-rows", "release-manifest-files"))
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--authorial", action="store_true",
                        help="Use content catalog (discovers content/maps packages)")
    args = parser.parse_args()
    plans = release_plan(load_map_registry(args.registry, authorial=args.authorial))
    if args.command == "release-rows":
        for plan in plans:
            print("\t".join((
                plan.map_key, plan.source_file, plan.source_sha256, plan.level_config,
                plan.manifest, plan.generated_output, plan.resource_path,
                plan.relative_entities_path, plan.supported_game_revision,
            )))
    else:
        for plan in plans:
            print(plan.client_manifest)


if __name__ == "__main__":
    _main()
