"""Single authoritative validation, generation, cache, receipt and release pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from content_catalog import ContentCatalog, load_content_catalog, thaw_content
from tools.content.compile_content_catalog import compile_catalog
from tools.maps.ap_map_generator import generate_map, load_item_names
from tools.maps.map_semantic_baseline import (
    BaselineDrift,
    assert_map_baseline,
)
from tools.maps.mission_complete_map_patcher import patch_mission_complete_maps
from tools.validation.audit_resource_packages import (
    audit_source_asset_dependencies,
)


ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT.parent
ARCHIPELAGO = WORKSPACE / "Archipelago"
CACHE_ROOT = ROOT / ".cache" / "ap_pipeline"
MAP_CACHE_ROOT = CACHE_ROOT / "maps"
RECEIPTS_ROOT = CACHE_ROOT / "receipts"
WORKSPACES_ROOT = CACHE_ROOT / "workspaces"
IDENTITY_PATH = ROOT / "data" / "content_identity.json"
PIPELINE_VERSION = "2"
CORE_MAP_INPUTS = (
    "data/items.json",
    "data/item_replay_policies.json",
    "data/location_names.json",
    "data/challenge_location_registry.json",
    "data/runtime_locations.json",
    "data/publisher_contracts.json",
    "data/campaign_goal_contract.json",
    "data/mission_complete_map_contracts.json",
    "data/scripted_location_contracts.json",
    "data/content_identity.json",
    "tools/maps/ap_map_generator.py",
    "tools/maps/notification_formatting.py",
    "tools/maps/notification_lab.py",
    "tools/maps/mission_complete_map_patcher.py",
    "tools/validation/audit_resource_packages.py",
    "content_catalog.py",
    "publisher_contracts.py",
)
PREFLIGHT_PYTHON = (
    "bridge_client.py",
    "content_catalog.py",
    "publisher_contracts.py",
    "publisher_runtime.py",
    "tools/content/compile_content_catalog.py",
    "tools/content/new_map.py",
    "tools/content/describe_map.py",
    "tools/maps/ap_map_generator.py",
    "tools/maps/mission_complete_map_patcher.py",
    "tools/maps/map_semantic_baseline.py",
    "tools/validation/pipeline.py",
    "tools/validation/validate_data.py",
    "tools/validation/audit_resource_packages.py",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _run(
    command: Sequence[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> None:
    print("RUN", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


@dataclass(frozen=True)
class MapArtifact:
    map_key: str
    digest: str
    directory: Path
    output: Path
    manifest: Path
    patch_mod: Path
    output_sha256: str
    cache_hit: bool


class Pipeline:
    def __init__(self, *, no_cache: bool = False, baseline_diff: bool = False):
        self.started = time.perf_counter()
        self.no_cache = no_cache
        self.baseline_diff = baseline_diff
        self.catalog: ContentCatalog | None = None
        self.timings: list[tuple[str, float]] = []
        self.generation_counts: dict[str, int] = {}
        self.cache_hits: dict[str, bool] = {}

    def timed(self, name: str):
        pipeline = self

        class Timer:
            def __enter__(self):
                self.started = time.perf_counter()

            def __exit__(self, *_args):
                pipeline.timings.append((name, time.perf_counter() - self.started))

        return Timer()

    def preflight(self) -> ContentCatalog:
        with self.timed("preflight"):
            for relative in PREFLIGHT_PYTHON:
                path = ROOT / relative
                try:
                    compile(path.read_text(encoding="utf-8"), str(path), "exec")
                except SyntaxError as error:
                    raise ValueError(
                        f"component=syntax file={relative} field=line "
                        f"value={error.lineno}\nReproduce:\n"
                        "  scripts/pipeline.sh fast"
                    ) from error
            json_roots = [
                ROOT / "data", ROOT / "level_configs", ROOT / "manifests",
                ROOT / "content" / "maps",
            ]
            for directory in json_roots:
                if not directory.exists():
                    continue
                for path in sorted(directory.rglob("*.json")):
                    try:
                        json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as error:
                        raise ValueError(
                            f"component=json file={path.relative_to(ROOT)} "
                            f"field=parse value={error}\nReproduce:\n"
                            "  scripts/pipeline.sh fast"
                        ) from error
            catalog = load_content_catalog()
            item_document = json.loads(
                (ROOT / "data" / "items.json").read_text(encoding="utf-8")
            )
            for spec in catalog.enabled_maps():
                if not spec.onboarding_path or not spec.data.get("package_directory"):
                    continue
                onboarding = json.loads(
                    spec.onboarding_path.read_text(encoding="utf-8")
                )
                guards = onboarding.get("vanilla_guards", {})
                forbidden = tuple(
                    str(term).casefold()
                    for term in guards.get("forbidden_ap_terms", [])
                )
                public_surface = [
                    item.ap_check
                    for item in catalog.physical_locations
                    if item.map_key == spec.key
                ] + [
                    item.name
                    for item in catalog.runtime_locations
                    if item.mission_key == spec.key
                ] + [
                    json.dumps(item_document, sort_keys=True)
                ]
                for term in forbidden:
                    if any(term in value.casefold() for value in public_surface):
                        raise ValueError(
                            f"component=onboarding map={spec.key} "
                            f"field=forbidden_ap_terms value={term!r}"
                        )
                source = (
                    ROOT / "vanillamaps" / spec.source_file
                ).read_text(encoding="utf-8")
                edge = guards.get("required_source_edge")
                if edge:
                    marker = f"entityDef {edge['from']}"
                    start = source.find(marker)
                    end = source.find("\nentity {", start + len(marker))
                    block = source[start:end if end >= 0 else None]
                    if start < 0 or f'"{edge["to"]}"' not in block:
                        raise ValueError(
                            f"component=onboarding map={spec.key} "
                            "field=required_source_edge value=missing"
                        )
                excluded = set(guards.get("excluded_entities", []))
                declared_entities = {
                    item.ap_check.removeprefix("AP_CHECK_").casefold()
                    for item in catalog.physical_locations
                    if item.map_key == spec.key
                }
                if {name.casefold() for name in excluded} & declared_entities:
                    raise ValueError(
                        f"component=onboarding map={spec.key} "
                        "field=excluded_entities value=declared"
                    )
            audit_source_asset_dependencies(
                ROOT / "packaging" / "mod_assets",
                catalog.assets,
            )
            physical_ids = {item.location_id for item in catalog.physical_locations}
            runtime_ids = {item.location_id for item in catalog.runtime_locations}
            overlap = physical_ids & runtime_ids
            if overlap:
                raise ValueError(
                    f"component=catalog map=* file=data field=location_id "
                    f"value={sorted(overlap)} runtime ID listed as physical\n"
                    "Reproduce:\n  scripts/pipeline.sh fast"
                )
            runtime_by_id = {item.location_id: item for item in catalog.runtime_locations}
            for publisher in catalog.publishers:
                for effect in publisher.effects:
                    if effect["strategy"] != "location_check":
                        continue
                    location_id = effect["location_id"]
                    if location_id not in runtime_by_id:
                        raise ValueError(
                            f"component=publisher map={publisher.map_key} "
                            "file=data/publisher_contracts.json field=effects.location_id "
                            f"value={location_id} is not a runtime location\n"
                            "Reproduce:\n  scripts/pipeline.sh fast"
                        )
            goals = [
                publisher for publisher in catalog.publishers
                if any(effect["strategy"] == "campaign_goal" for effect in publisher.effects)
            ]
            goal = goals[0]
            goal_files = {
                trigger.get("filename") for trigger in goal.triggers
                if trigger["strategy"] == "map_event_file"
            }
            goal_markers = {
                trigger.get("marker") for trigger in goal.triggers
                if trigger["strategy"] == "map_event_file"
            }
            for publisher in catalog.publishers:
                if publisher is goal:
                    continue
                if publisher.dedupe_scope == goal.dedupe_scope:
                    raise ValueError(
                        f"component=publisher map={goal.map_key} "
                        "file=data/publisher_contracts.json field=dedupe_scope "
                        f"value={goal.dedupe_scope!r} shared with campaign goal"
                    )
                files = {
                    trigger.get("filename") for trigger in publisher.triggers
                    if trigger["strategy"] == "map_event_file"
                }
                markers = {
                    trigger.get("marker") for trigger in publisher.triggers
                    if trigger["strategy"] == "map_event_file"
                }
                if files & goal_files or markers & goal_markers:
                    raise ValueError(
                        f"component=publisher map={goal.map_key} "
                        "file=data/publisher_contracts.json "
                        "field=filename/marker value=shared campaign goal authority"
                    )
            compile_catalog(check=True)
            identity = json.loads(IDENTITY_PATH.read_text(encoding="utf-8"))
            generated: dict[str, object] = {}
            exec(
                (ARCHIPELAGO / "worlds" / "doometernal" / "generated_content.py")
                .read_text(encoding="utf-8"),
                generated,
            )
            checks = {
                "CONTENT_SCHEMA_VERSION": identity["content_schema_version"],
                "CONTENT_REVISION": identity["content_revision"],
                "BRIDGE_PROTOCOL_VERSION": identity["bridge_protocol_version"],
            }
            for name, expected in checks.items():
                if generated.get(name) != expected:
                    raise ValueError(
                        f"component=apworld map=* "
                        "file=Archipelago/worlds/doometernal/generated_content.py "
                        f"field={name} value={generated.get(name)!r} expected={expected!r}\n"
                        "Reproduce:\n"
                        "  python -m tools.content.compile_content_catalog --check\n"
                        "Fix:\n"
                        "  python -m tools.content.compile_content_catalog"
                    )
            self.catalog = catalog
            return catalog

    def _map_inputs(self, map_key: str) -> dict[str, str]:
        catalog = self.catalog or load_content_catalog()
        spec = catalog.map(map_key)
        paths = [
            ROOT / "vanillamaps" / spec.source_file,
            spec.level_config_path,
            spec.manifest_path,
            *[ROOT / relative for relative in CORE_MAP_INPUTS],
        ]
        if spec.onboarding_path:
            paths.append(spec.onboarding_path)
        package = spec.data.get("package_directory")
        if package:
            paths.extend(sorted((ROOT / package).glob("*.json")))
        result = {}
        for path in paths:
            if not path.is_file():
                raise ValueError(
                    f"component=map map={map_key} file={path} field=dependency "
                    "value=missing\nReproduce:\n"
                    f"  scripts/pipeline.sh map {map_key}"
                )
            result[str(path.relative_to(ROOT))] = _sha256(path)
        result["pipeline_version"] = PIPELINE_VERSION
        result["publisher_contract"] = _canonical_hash([
            {
                "key": publisher.key,
                "triggers": [dict(item) for item in publisher.triggers],
                "effects": [dict(item) for item in publisher.effects],
                "dedupe_scope": publisher.dedupe_scope,
                "fallback_policy": publisher.fallback_policy,
            }
            for publisher in catalog.publishers if publisher.map_key == map_key
        ])
        result["asset_specs"] = _canonical_hash([
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
            }
            for asset in catalog.assets if asset.map_key == map_key
        ])
        return result

    def map_digest(self, map_key: str) -> tuple[str, dict[str, str]]:
        inputs = self._map_inputs(map_key)
        return _canonical_hash(inputs), inputs

    def generate(self, map_key: str) -> MapArtifact:
        catalog = self.catalog or self.preflight()
        spec = catalog.map(map_key)
        digest, inputs = self.map_digest(map_key)
        directory = MAP_CACHE_ROOT / map_key / digest
        output = directory / spec.data["generated_output"]
        manifest = directory / f"{map_key}.json"
        metadata_path = directory / "metadata.json"
        if not self.no_cache:
            missing = []
            if not output.is_file():
                missing.append("entities")
            if not manifest.is_file():
                missing.append("manifest")
            if not metadata_path.is_file():
                missing.append("metadata")
            if missing:
                print(f"MAP {map_key} cache=invalid digest={digest[:16]} reason=missing_{'_'.join(missing)}")
            elif output.is_file() and manifest.is_file() and metadata_path.is_file():
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if (
                    metadata.get("inputs") == inputs
                    and metadata.get("output_sha256") == _sha256(output)
                    and metadata.get("output_size") == output.stat().st_size
                    and metadata.get("manifest_sha256") == _sha256(manifest)
                    and metadata.get("manifest_size") == manifest.stat().st_size
                ):
                    self.cache_hits[map_key] = True
                    print(f"MAP {map_key} cache=hit digest={digest[:16]}")
                    return MapArtifact(
                        map_key, digest, directory, output, manifest,
                        directory / "patch_mod", metadata["output_sha256"], True,
                    )
                else:
                    reasons = []
                    if metadata.get("inputs") != inputs:
                        reasons.append("inputs")
                    if metadata.get("output_sha256") != _sha256(output):
                        reasons.append("output_sha256")
                    if metadata.get("output_size") != output.stat().st_size:
                        reasons.append("output_size")
                    if metadata.get("manifest_sha256") != _sha256(manifest):
                        reasons.append("manifest_sha256")
                    if metadata.get("manifest_size") != manifest.stat().st_size:
                        reasons.append("manifest_size")
                    print(f"MAP {map_key} cache=invalid digest={digest[:16]} reason={'_'.join(reasons)}")
        with self.timed(f"generate:{map_key}"):
            directory.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=f".{digest}.", dir=directory.parent))
            try:
                raw = temporary / f"{map_key}.raw.entities"
                generated_manifest = temporary / f"{map_key}.json"
                items = json.loads(
                    (ROOT / "data" / "items.json").read_text(encoding="utf-8")
                )
                item_names = load_item_names(
                    ROOT / "data" / "item_replay_policies.json"
                )
                generate_map(
                    ROOT / "vanillamaps" / spec.source_file,
                    raw,
                    spec.level_config_path,
                    generated_manifest,
                    items,
                    item_names=item_names,
                    enable_notifications=True,
                    enable_notification_lab=False,
                )
                final = temporary / spec.data["generated_output"]
                shutil.copyfile(raw, final)
                patch_mod = temporary / "patch_mod"
                patch_mission_complete_maps(
                    ROOT / "data" / "mission_complete_map_contracts.json",
                    {map_key: final},
                    patch_mod,
                )
                metadata = {
                    "schema_version": 1,
                    "map_key": map_key,
                    "digest": digest,
                    "inputs": inputs,
                    "raw_sha256": _sha256(raw),
                    "raw_size": raw.stat().st_size,
                    "output_sha256": _sha256(final),
                    "output_size": final.stat().st_size,
                    "manifest_sha256": _sha256(generated_manifest),
                    "manifest_size": generated_manifest.stat().st_size,
                }
                (temporary / "metadata.json").write_text(
                    json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                if directory.exists():
                    shutil.rmtree(directory)
                os.replace(temporary, directory)
            except BaseException:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        self.generation_counts[map_key] = self.generation_counts.get(map_key, 0) + 1
        self.cache_hits[map_key] = False
        print(f"MAP {map_key} cache=miss digest={digest[:16]}")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        return MapArtifact(
            map_key, digest, directory, output, manifest,
            directory / "patch_mod", metadata["output_sha256"], False,
        )

    def validate_map(
        self,
        map_key: str,
        *,
        baseline: bool = True,
    ) -> MapArtifact:
        if self.catalog is None:
            self.preflight()
        artifact = self.generate(map_key)
        catalog = self.catalog
        assert catalog is not None
        text = artifact.output.read_text(encoding="utf-8")
        declared = {
            item.location_id for item in catalog.physical_locations
            if item.map_key == map_key
        }
        physical_events = {
            int(value)
            for value in __import__("re").findall(r"AP_CHECK_EVENT_(\d+)", text)
        }
        missing = declared - physical_events
        if missing:
            raise ValueError(
                f"component=generated_map map={map_key} file={artifact.output} "
                f"field=physical_ap_ids value=missing {sorted(missing)}\n"
                f"Reproduce:\n  scripts/pipeline.sh map {map_key}"
            )
        expected_manifest = json.loads(
            catalog.map(map_key).manifest_path.read_text(encoding="utf-8")
        )
        actual_manifest = json.loads(artifact.manifest.read_text(encoding="utf-8"))
        if expected_manifest != actual_manifest:
            raise ValueError(
                f"component=manifest map={map_key} file={artifact.manifest} "
                "field=content value=generated manifest differs\n"
                f"Reproduce:\n  scripts/pipeline.sh map {map_key}"
            )
        if baseline:
            assert_map_baseline(map_key, artifact.output, artifact.manifest)
        return artifact

    def _pytest_generated(self, artifacts: Iterable[MapArtifact]) -> None:
        mapping = {
            item.map_key: str(item.output)
            for item in artifacts
        }
        env = os.environ.copy()
        env["AP_PIPELINE_MAPS_JSON"] = json.dumps(mapping, sort_keys=True)
        env["AP_PIPELINE_SELECTED_MAPS"] = json.dumps(sorted(mapping))
        _run([
            sys.executable, "-m", "pytest",
            "tests/test_catalog_generated_maps.py",
            "-q", "--maxfail=1",
        ], env=env)

    def fast(self) -> None:
        self.preflight()
        _run([
            sys.executable, "-m", "pytest",
            "tests/test_content_catalog.py",
            "tests/test_content_architecture.py",
            "tests/test_generated_content.py",
            "tests/test_content_strategies.py",
            "tests/test_publisher_runtime.py",
            "tests/test_aggregate_contract.py",
            "tests/test_observer_lifecycle.py",
            "-q", "--maxfail=1",
        ])

    def map(self, map_key: str) -> MapArtifact:
        artifact = self.validate_map(map_key)
        self._pytest_generated((artifact,))
        return artifact

    def selection(self) -> tuple[list[str], list[str]]:
        catalog = self.catalog or self.preflight()
        paths = []
        for repository in (ROOT, ARCHIPELAGO):
            output = subprocess.run(
                ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                cwd=repository,
                check=True,
                stdout=subprocess.PIPE,
            ).stdout.decode("utf-8", errors="surrogateescape")
            for entry in output.split("\0"):
                if entry:
                    paths.append(f"{repository.name}/{entry[3:]}")
        selected: set[str] = set()
        central = False
        for path in paths:
            matches = [
                spec.key for spec in catalog.enabled_maps()
                if any(token in path for token in (
                    spec.source_file,
                    spec.level_config_path.name,
                    spec.manifest_path.name,
                    f"/{spec.key}/",
                ))
            ]
            selected.update(matches)
            if not matches or any(token in path for token in (
                "ap_map_generator.py", "mission_complete_map_patcher.py",
                "content_catalog.py", "publisher_", "pipeline.py",
                "map_semantic_baseline.py", "items.json", "location_names.json",
                "challenge_location_registry.json", "content_identity.json",
                "generated_content.py",
            )):
                central = True
        if central:
            selected = {spec.key for spec in catalog.enabled_maps()}
        return sorted(selected), sorted(paths)

    def integration(self, map_keys: Sequence[str] | None = None) -> list[MapArtifact]:
        catalog = self.preflight()
        keys = list(map_keys or [spec.key for spec in catalog.enabled_maps()])
        artifacts = []
        for key in keys:
            artifacts.append(self.validate_map(key))
        self._pytest_generated(artifacts)
        return artifacts

    def _workspace_digest(self, artifacts: Sequence[MapArtifact]) -> str:
        identity = json.loads(IDENTITY_PATH.read_text(encoding="utf-8"))
        apworld_files = {
            str(path.relative_to(ARCHIPELAGO)): _sha256(path)
            for path in sorted((ARCHIPELAGO / "worlds" / "doometernal").rglob("*.py"))
        }
        release_roots = (
            ROOT / "data",
            ROOT / "packaging",
            ROOT / "scripts" / "build",
            ROOT / "tools" / "release",
            ROOT / "tools" / "validation",
            ROOT / "native",
        )
        release_inputs = {
            str(path.relative_to(ROOT)): _sha256(path)
            for directory in release_roots
            for path in sorted(directory.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        }
        for relative in (
            "bridge_client.py", "bootstrap_actions.py", "campaign_goal_contract.py",
            "challenge_registry.py", "content_catalog.py", "foundation.py",
            "item_classification.py", "item_reconciliation.py", "map_registry.py",
            "observer_lifecycle.py", "publisher_contracts.py",
            "publisher_runtime.py", "save_decrypt.py",
        ):
            release_inputs[relative] = _sha256(ROOT / relative)
        return _canonical_hash({
            "identity": identity,
            "maps": {item.map_key: item.digest for item in artifacts},
            "apworld": apworld_files,
            "release_inputs": release_inputs,
            "pipeline_version": PIPELINE_VERSION,
        })

    def receipt(self, artifacts: Sequence[MapArtifact], stages: Sequence[str]) -> Path:
        workspace_digest = self._workspace_digest(artifacts)
        artifact_root = WORKSPACES_ROOT / workspace_digest
        receipt_path = RECEIPTS_ROOT / f"{workspace_digest}.json"
        if not artifact_root.exists():
            temporary = Path(tempfile.mkdtemp(
                prefix=f".{workspace_digest}.", dir=WORKSPACES_ROOT.parent
            ))
            try:
                maps_dir = temporary / "maps"
                manifests_dir = temporary / "manifests"
                mod_dir = temporary / "mod"
                maps_dir.mkdir(parents=True)
                manifests_dir.mkdir()
                mod_dir.mkdir()
                for artifact in artifacts:
                    shutil.copy2(artifact.output, maps_dir / artifact.output.name)
                    shutil.copy2(artifact.manifest, manifests_dir / artifact.manifest.name)
                    if artifact.patch_mod.exists():
                        shutil.copytree(
                            artifact.patch_mod, mod_dir,
                            dirs_exist_ok=True,
                        )
                artifact_root.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary, artifact_root)
            except BaseException:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        document = {
            "schema_version": 2,
            "workspace_digest": workspace_digest,
            "content_identity": json.loads(IDENTITY_PATH.read_text(encoding="utf-8")),
            "maps": {
                item.map_key: {
                    "digest": item.digest,
                    "output": item.output.name,
                    "output_sha256": item.output_sha256,
                    "output_size": item.output.stat().st_size,
                    "output_source": str(item.output),
                    "output_destination": f"build/generated-maps/{item.output.name}",
                    "manifest": item.manifest.name,
                    "manifest_sha256": _sha256(item.manifest),
                    "manifest_size": item.manifest.stat().st_size,
                    "manifest_source": str(item.manifest),
                    "manifest_destination": f".staging/manifests/{item.manifest.name}",
                }
                for item in artifacts
            },
            "stages": list(stages),
            "tools": {
                "pipeline": PIPELINE_VERSION,
                "python": sys.version.split()[0],
            },
            "artifact_root": str(artifact_root),
        }
        RECEIPTS_ROOT.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return receipt_path

    def apworld_smoke(self) -> None:
        python = ARCHIPELAGO / ".venv" / "bin" / "python"
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{ARCHIPELAGO}:{ROOT}"
        _run([
            str(python), "-m", "pytest", "-c", str(ARCHIPELAGO / "pytest.ini"),
            str(ARCHIPELAGO / "worlds" / "doometernal" / "test"),
            "-q", "--maxfail=1",
        ], cwd=WORKSPACE, env=env)
        smoke_root = CACHE_ROOT / "seed-smoke"
        smoke_root.mkdir(parents=True, exist_ok=True)
        env["SKIP_REQUIREMENTS_UPDATE"] = "1"
        _run([
            str(python), str(ARCHIPELAGO / "Generate.py"),
            "--player_files_path", str(ROOT / "player_templates"),
            "--outputpath", str(smoke_root),
        ], cwd=ROOT, env=env)

    def release(self, *, build: bool = False) -> tuple[Path, list[MapArtifact]]:
        artifacts = self.integration()
        self.apworld_smoke()
        _run([
            sys.executable, "-m", "pytest",
            "tests/unit/test_check_events.py",
            "tests/unit/test_validate_data.py",
            "-q", "--maxfail=1",
        ])
        receipt = self.receipt(
            artifacts,
            ("preflight", "integration", "apworld", "content_audit", "protocol"),
        )
        if build:
            document = json.loads(receipt.read_text(encoding="utf-8"))
            env = os.environ.copy()
            env["AP_PIPELINE_RECEIPT"] = str(receipt)
            env["AP_PIPELINE_ARTIFACT_ROOT"] = document["artifact_root"]
            _run(["bash", "scripts/build/playable_test.sh"], env=env)
            for key, item in document["maps"].items():
                packaged = ROOT / "build" / "release" / "build" / "generated-maps" / item["output"]
                if not packaged.is_file():
                    continue
                if _sha256(packaged) != item["output_sha256"]:
                    raise ValueError(
                        f"component=build map={key} file={packaged} "
                        "field=sha256 value=does not match validation receipt"
                    )
            identity = json.loads(IDENTITY_PATH.read_text(encoding="utf-8"))
            zip_path = ROOT / "build" / "release" / (
                f"DoomEternalArchipelagoPlayableTest-{identity['release_version']}.zip"
            )
            if not zip_path.is_file():
                print("RELEASE_ARTIFACT_NOT_PUBLISHED reason=zip_missing")
                raise ValueError(
                    f"component=build file={zip_path} field=zip value=missing after build\n"
                    "RELEASE_ARTIFACT_NOT_PUBLISHED"
                )
            if not zipfile.is_zipfile(zip_path):
                print("RELEASE_ARTIFACT_NOT_PUBLISHED reason=zip_invalid")
                raise ValueError(
                    f"component=build file={zip_path} field=zip value=invalid\n"
                    "RELEASE_ARTIFACT_NOT_PUBLISHED"
                )
            with zipfile.ZipFile(zip_path) as outer:
                inner = outer.read("DoomEternalArchipelagoAlpha.zip")
            with tempfile.NamedTemporaryFile(suffix=".zip") as handle:
                handle.write(inner)
                handle.flush()
                if not zipfile.is_zipfile(handle.name):
                    raise ValueError("internal ZIP audit failed")
            print(f"ARTIFACT {zip_path}")
            print(f"SHA256 {_sha256(zip_path)}")
            print(f"CONTENT_REVISION {identity['content_revision']}")
            for spec in (self.catalog or load_content_catalog()).enabled_maps():
                print(
                    f"GENERATIONS map={spec.key} "
                    f"count={self.generation_counts.get(spec.key, 0)}"
                )
        return receipt, artifacts

    def report(self) -> None:
        total = time.perf_counter() - self.started
        print(f"PIPELINE duration={total:.3f}s")
        for name, duration in sorted(
            self.timings, key=lambda item: item[1], reverse=True
        )[:5]:
            print(f"SLOW operation={name} duration={duration:.3f}s")
        for key in sorted(self.cache_hits):
            print(
                f"CACHE map={key} status={'hit' if self.cache_hits[key] else 'miss'} "
                f"generations={self.generation_counts.get(key, 0)}"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=(
        "fast", "map", "changed", "integration", "release", "cache-key",
    ))
    parser.add_argument("map_key", nargs="?")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--baseline-diff", action="store_true")
    parser.add_argument("--show-selection", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pipeline = Pipeline(no_cache=args.no_cache, baseline_diff=args.baseline_diff)
    try:
        if args.phase == "fast":
            pipeline.fast()
        elif args.phase == "map":
            if not args.map_key:
                raise ValueError("map phase requires <map_key>")
            pipeline.map(args.map_key)
        elif args.phase == "changed":
            selected, paths = pipeline.selection()
            if args.show_selection:
                print("CHANGED FILES")
                print("\n".join(f"  {path}" for path in paths) or "  (none)")
                print("SELECTED MAPS")
                print("\n".join(f"  {key}" for key in selected) or "  (none)")
            if selected:
                pipeline.integration(selected)
        elif args.phase == "integration":
            pipeline.integration()
        elif args.phase == "release":
            pipeline.release(build=args.build)
        elif args.phase == "cache-key":
            if not args.map_key:
                raise ValueError("cache-key phase requires <map_key>")
            pipeline.preflight()
            digest, inputs = pipeline.map_digest(args.map_key)
            print(json.dumps({
                "map_key": args.map_key,
                "digest": digest,
                "inputs": inputs,
            }, indent=2, sort_keys=True))
    finally:
        pipeline.report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
