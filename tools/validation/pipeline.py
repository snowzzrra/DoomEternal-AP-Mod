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
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from doom_eap.content.content_catalog import (
    ContentCatalog,
    load_content_catalog,
    thaw_content,
)
from tools.content.compile_content_catalog import compile_catalog
from doom_eap.content.automap_visual_registry import (
    load_automap_visual_registry,
    validate_generated_visuals,
)
from tools.maps.ap_map_generator import generate_map, load_item_notification_policies
from tools.maps.map_semantic_baseline import (
    assert_map_baseline,
)
from tools.maps.mission_complete_map_patcher import patch_mission_complete_maps
from tools.validation.audit_resource_packages import (
    audit_source_asset_dependencies,
)
from tools.release.apworld_cache import apworld_fingerprint
from tools.release.release_manifest import (
    build_release_manifest,
    load_release_manifest,
    stale_package_paths,
    validate_automap_option_keys,
    validate_release_manifest,
    validate_source_layout,
)

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT.parent
ARCHIPELAGO = WORKSPACE / "Archipelago"
CACHE_ROOT = ROOT / ".cache" / "ap_pipeline"
MAP_CACHE_ROOT = CACHE_ROOT / "maps"
RECEIPTS_ROOT = CACHE_ROOT / "receipts"
WORKSPACES_ROOT = CACHE_ROOT / "workspaces"
IDENTITY_PATH = ROOT / "data" / "content_identity.json"
PIPELINE_VERSION = "4"
CORE_MAP_INPUTS = (
    "data/items.json",
    "data/item_replay_policies.json",
    "data/mission_complete_map_contracts.json",
    "data/content_identity.json",
    "data/ap_visual_bundle.json",
    "data/checked_location_visuals.json",
    "doom_eap/contracts/ap_visual_contract.py",
    "tools/maps/ap_map_generator.py",
    "tools/maps/start_with_automap.py",
    "tools/maps/notification_formatting.py",
    "tools/maps/mission_complete_map_patcher.py",
    "tools/validation/audit_resource_packages.py",
    "doom_eap/content/content_catalog.py",
    "doom_eap/content/automap_visual_registry.py",
    "doom_eap/contracts/publisher_contracts.py",
)
PREFLIGHT_PYTHON = (
    "doom_eap/contracts/ap_visual_contract.py",
    "doom_eap/runtime/bridge_client.py",
    "doom_eap/content/physical_options.py",
    "doom_eap/content/content_catalog.py",
    "doom_eap/contracts/publisher_contracts.py",
    "doom_eap/launcher/launcher_app.py",
    "doom_eap/launcher/launcher_cli.py",
    "doom_eap/launcher/launcher_controller.py",
    "doom_eap/launcher/launcher_core.py",
    "doom_eap/launcher/launcher_doctor.py",
    "doom_eap/launcher/launcher_integration.py",
    "doom_eap/launcher/launcher_native_health.py",
    "doom_eap/launcher/launcher_platform.py",
    "doom_eap/launcher/launcher_supervisor.py",
    "doom_eap/launcher/launcher_ui.py",
    "doom_eap/runtime/publisher_runtime.py",
    "doom_eap/__init__.py",
    "doom_eap/launcher/__init__.py",
    "doom_eap/runtime/__init__.py",
    "tools/content/compile_content_catalog.py",
    "tools/content/new_map.py",
    "tools/content/describe_map.py",
    "tools/maps/ap_map_generator.py",
    "tools/maps/start_with_automap.py",
    "tools/maps/mission_complete_map_patcher.py",
    "tools/maps/map_semantic_baseline.py",
    "tools/decls/rune_slot_builder.py",
    "tools/validation/pipeline.py",
    "tools/validation/physical_option_room_contract.py",
    "tools/validation/validate_data.py",
    "tools/validation/audit_resource_packages.py",
    "doom_eap/content/automap_visual_registry.py",
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


def _member_hashes(root: Path) -> dict[str, dict[str, int | str]] | None:
    """Return complete regular-file inventory, rejecting unsafe trees."""
    if root.is_symlink() or not root.is_dir():
        return None
    members: dict[str, dict[str, int | str]] = {}
    try:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                return None
            if not path.is_file():
                continue
            members[path.relative_to(root).as_posix()] = {
                "sha256": _sha256(path),
                "size": path.stat().st_size,
            }
    except OSError:
        return None
    return members


def _merge_members(
    target: dict[str, dict[str, int | str]],
    prefix: str,
    source: Path,
) -> bool:
    if not source.exists() and not source.is_symlink():
        return True
    members = _member_hashes(source)
    if members is None:
        return False
    for relative, metadata in members.items():
        destination = f"{prefix}/{relative}" if prefix else relative
        previous = target.get(destination)
        if previous is not None and previous != metadata:
            return False
        target[destination] = metadata
    return True


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


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
        self.integration_receipt: Path | None = None
        self.workspace_cache_hit: bool | None = None
        self.receipt_cache_hit: bool | None = None

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
            json_roots = [ROOT / "data", ROOT / "content", ROOT / "manifests"]
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
            load_automap_visual_registry(ROOT / "data" / "checked_location_visuals.json")
            from doom_eap.content.options_foundation import load_options_schema

            load_options_schema(ROOT / "data" / "options_schema.json")
            validate_automap_option_keys(
                json.loads((ROOT / "data" / "options_schema.json").read_text(encoding="utf-8"))
            )
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
            validate_source_layout(ROOT, catalog)
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
        ])
        return result

    def map_digest(self, map_key: str) -> tuple[str, dict[str, str]]:
        inputs = self._map_inputs(map_key)
        return _canonical_hash(inputs), inputs

    def _cached_map_artifact(
        self,
        *,
        map_key: str,
        digest: str,
        inputs: dict[str, str],
        directory: Path,
        output: Path,
        manifest: Path,
        metadata_path: Path,
    ) -> MapArtifact | None:
        patch_mod = directory / "patch_mod"
        raw = directory / f"{map_key}.raw.entities"
        try:
            if not directory.is_dir() or any(
                child.name not in {
                    output.name, manifest.name, metadata_path.name, raw.name, "patch_mod",
                }
                for child in directory.iterdir()
            ):
                return None
            if any(path.is_symlink() for path in (
                output, manifest, metadata_path, raw, patch_mod,
            )):
                return None
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            patch_members = {} if not patch_mod.exists() else _member_hashes(patch_mod)
            if not isinstance(metadata, dict) or patch_members is None:
                return None
            if (
                metadata.get("schema_version") != 2
                or metadata.get("map_key") != map_key
                or metadata.get("digest") != digest
                or metadata.get("inputs") != inputs
                or not output.is_file()
                or not manifest.is_file()
                or metadata.get("output_sha256") != _sha256(output)
                or metadata.get("output_size") != output.stat().st_size
                or metadata.get("manifest_sha256") != _sha256(manifest)
                or metadata.get("manifest_size") != manifest.stat().st_size
                or not raw.is_file()
                or metadata.get("raw_sha256") != _sha256(raw)
                or metadata.get("raw_size") != raw.stat().st_size
                or metadata.get("patch_mod_members") != patch_members
            ):
                return None
            return MapArtifact(
                map_key,
                digest,
                directory,
                output,
                manifest,
                patch_mod,
                metadata["output_sha256"],
                True,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def generate(self, map_key: str) -> MapArtifact:
        catalog = self.catalog or self.preflight()
        spec = catalog.map(map_key)
        digest, inputs = self.map_digest(map_key)
        directory = MAP_CACHE_ROOT / map_key / digest
        output = directory / spec.data["generated_output"]
        manifest = directory / f"{map_key}.json"
        metadata_path = directory / "metadata.json"
        if not self.no_cache:
            cached = self._cached_map_artifact(
                map_key=map_key,
                digest=digest,
                inputs=inputs,
                directory=directory,
                output=output,
                manifest=manifest,
                metadata_path=metadata_path,
            )
            if cached is not None:
                self.cache_hits[map_key] = True
                print(f"MAP {map_key} cache=hit digest={digest[:16]}")
                return cached
            print(f"MAP {map_key} cache=invalid digest={digest[:16]} reason=integrity")
        with self.timed(f"generate:{map_key}"):
            directory.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=f".{digest}.", dir=directory.parent))
            try:
                raw = temporary / f"{map_key}.raw.entities"
                generated_manifest = temporary / f"{map_key}.json"
                items = json.loads(
                    (ROOT / "data" / "items.json").read_text(encoding="utf-8")
                )
                item_names, receipt_feedback = load_item_notification_policies(
                    ROOT / "data" / "item_replay_policies.json"
                )
                generate_map(
                    ROOT / "vanillamaps" / spec.source_file,
                    raw,
                    spec.level_config_path,
                    generated_manifest,
                    items,
                    item_names=item_names,
                    receipt_feedback=receipt_feedback,
                    enable_notifications=True,
                )
                final = temporary / spec.data["generated_output"]
                shutil.copyfile(raw, final)
                patch_mod = temporary / "patch_mod"
                patch_mission_complete_maps(
                    ROOT / "data" / "mission_complete_map_contracts.json",
                    {map_key: final},
                    patch_mod,
                )
                patch_members = {} if not patch_mod.exists() else _member_hashes(patch_mod)
                if patch_members is None:
                    raise ValueError(f"generated patch_mod is not a regular file tree: {patch_mod}")
                metadata = {
                    "schema_version": 2,
                    "map_key": map_key,
                    "digest": digest,
                    "inputs": inputs,
                    "raw_sha256": _sha256(raw),
                    "raw_size": raw.stat().st_size,
                    "output_sha256": _sha256(final),
                    "output_size": final.stat().st_size,
                    "manifest_sha256": _sha256(generated_manifest),
                    "manifest_size": generated_manifest.stat().st_size,
                    "patch_mod_members": patch_members,
                }
                (temporary / "metadata.json").write_text(
                    json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                if directory.exists() or directory.is_symlink():
                    _remove_path(directory)
                os.replace(temporary, directory)
            except BaseException:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        self.generation_counts[map_key] = self.generation_counts.get(map_key, 0) + 1
        self.cache_hits[map_key] = False
        print(f"MAP {map_key} cache=miss digest={digest[:16]}")
        artifact = self._cached_map_artifact(
            map_key=map_key,
            digest=digest,
            inputs=inputs,
            directory=directory,
            output=output,
            manifest=manifest,
            metadata_path=metadata_path,
        )
        if artifact is None:
            raise ValueError(f"generated map cache failed integrity check: {directory}")
        return MapArtifact(
            artifact.map_key,
            artifact.digest,
            artifact.directory,
            artifact.output,
            artifact.manifest,
            artifact.patch_mod,
            artifact.output_sha256,
            False,
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
        validate_generated_visuals(
            load_automap_visual_registry(ROOT / "data" / "checked_location_visuals.json"),
            map_key,
            text,
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

    def fast(self) -> None:
        self.preflight()

    def map(self, map_key: str) -> MapArtifact:
        return self.validate_map(map_key)

    def selection(self, *, preflight: bool = True) -> tuple[list[str], list[str]]:
        catalog = self.catalog or (
            self.preflight() if preflight else load_content_catalog()
        )
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

    def _run_focused_tests(self, tests: Sequence[str]) -> None:
        if not tests:
            return
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{ARCHIPELAGO}:{ROOT}:{env.get('PYTHONPATH', '')}"
        env["SKIP_REQUIREMENTS_UPDATE"] = "1"
        command = [sys.executable, "-m", "pytest"]
        command.extend(tests)
        command.extend(("-q", "--maxfail=1"))
        _run(command, env=env)

    def integration(
        self,
        map_keys: Sequence[str] | None = None,
        *,
        tests: Sequence[str] = (),
        full: bool = False,
    ) -> list[MapArtifact]:
        catalog = self.catalog or self.preflight()
        keys = list(map_keys or [spec.key for spec in catalog.enabled_maps()])
        artifacts = []
        with self.timed("integration"):
            for key in keys:
                artifacts.append(self.validate_map(key))
            if full:
                self.apworld_contract_smoke(tests)
                self.integration_receipt = self.receipt(
                    artifacts,
                    ("preflight", "integration", "apworld-contract", "content_audit", "protocol"),
                )
                print(f"INTEGRATION_RECEIPT {self.integration_receipt}")
            else:
                self._run_focused_tests(tests)
        return artifacts

    def affected_cache(self) -> list[MapArtifact]:
        """Refresh affected map cache, then expose complete assembler inputs."""
        catalog = self.catalog or self.preflight()
        selected, paths = self.selection()
        print(
            "AFFECTED maps="
            f"{','.join(selected) if selected else '(none)'} files={len(paths)}"
        )
        with self.timed("affected-cache"):
            for key in selected:
                self.validate_map(key)
            return [
                self.validate_map(spec.key)
                for spec in catalog.enabled_maps()
            ]

    def _package_root(self, explicit: Path | None = None) -> Path | None:
        return explicit

    def package_preflight(self, package_root: Path | None = None) -> None:
        """Cheap source/package contract audit; never generates maps or seeds."""
        with self.timed("package-preflight"):
            catalog = self.catalog or self.preflight()
            from tools.validation.physical_option_room_contract import audit_physical_option_rooms

            audit_physical_option_rooms(ROOT)
            option_path = ROOT / "data" / "options_schema.json"
            validate_automap_option_keys(json.loads(option_path.read_text(encoding="utf-8")))
            root = self._package_root(package_root)
            if root is None:
                synthetic = build_release_manifest(
                    ROOT,
                    generated_maps=None,
                    room_resources=ROOT / ".cache" / "ap_pipeline" / "source-contract-resources",
                    public_files=[],
                )
                validate_release_manifest(synthetic)
                print("PACKAGE_PREFLIGHT package=source-synthetic checked")
                return
            if not root.exists():
                raise ValueError(f"component=package root={root} value=missing")
            stale = stale_package_paths(root)
            if stale:
                raise ValueError(f"component=package field=stale_layout value={stale}")
            manifest_path = root / "RELEASE_MANIFEST.json"
            if not manifest_path.is_file():
                raise ValueError(f"component=package file={manifest_path} field=manifest value=missing")
            generated_maps = ROOT / "build" / "generated-maps"
            generated_root = generated_maps if generated_maps.is_dir() else None
            manifest = load_release_manifest(
                manifest_path,
                package_root=root,
                generated_maps=generated_root,
            )
            expected = build_release_manifest(
                ROOT,
                generated_maps=generated_root,
                release_version=manifest["version"],
            )
            for field in ("checked_location_visuals", "room_compiler", "base_resources"):
                actual_value = manifest[field]
                expected_value = expected[field]
                if field == "checked_location_visuals" and not expected_value["generated_map_sha256"]:
                    actual_value = dict(actual_value)
                    actual_value["generated_map_sha256"] = {}
                if actual_value != expected_value:
                    raise ValueError(f"component=package field=manifest_disagreement value={field}")
            packaged_options = root / "client" / "data" / "options_schema.json"
            if packaged_options.is_file():
                validate_automap_option_keys(json.loads(packaged_options.read_text(encoding="utf-8")))
            print(f"PACKAGE_PREFLIGHT package=checked root={root}")

    def playtest(self) -> None:
        """Validate, create receipt, then delegate packaging to package builder."""
        self.preflight()
        artifacts = self.affected_cache()
        self.package_preflight()
        receipt = self.receipt(artifacts, ("preflight", "affected", "package-preflight"))
        document = json.loads(receipt.read_text(encoding="utf-8"))
        env = os.environ.copy()
        env["AP_PIPELINE_RECEIPT"] = str(receipt)
        env["AP_PIPELINE_ARTIFACT_ROOT"] = document["artifact_root"]
        with self.timed("assembler"):
            _run(["bash", "scripts/build/playable_test.sh"], env=env)

    def full(self, tests: Sequence[str] = ()) -> None:
        self.integration(tests=tests, full=True)

    def seed_smoke(self) -> None:
        with self.timed("seed-smoke"):
            self.preflight()
            self.apworld_contract_smoke()
            with self.timed("seed-smoke-generate"):
                python = ARCHIPELAGO / ".venv" / "bin" / "python"
                env = os.environ.copy()
                env["PYTHONPATH"] = f"{ARCHIPELAGO}:{ROOT}"
                env["SKIP_REQUIREMENTS_UPDATE"] = "1"
                smoke_root = CACHE_ROOT / "seed-smoke"
                smoke_root.mkdir(parents=True, exist_ok=True)
                print(
                    "APWORLD seed-smoke fingerprint="
                    f"{apworld_fingerprint(ARCHIPELAGO / 'worlds' / 'doometernal')}"
                )
                _run([
                    str(python), str(ARCHIPELAGO / "Generate.py"),
                    "--player_files_path", str(ROOT / "player_templates"),
                    "--outputpath", str(smoke_root),
                ], cwd=ROOT, env=env)

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
            "doom_eap/runtime/bridge_client.py",
            "doom_eap/runtime/bootstrap_actions.py",
            "doom_eap/contracts/campaign_goal_contract.py",
            "doom_eap/contracts/challenge_registry.py",
            "doom_eap/content/content_catalog.py",
            "doom_eap/contracts/foundation.py",
            "doom_eap/content/item_classification.py",
            "doom_eap/runtime/item_reconciliation.py",
            "doom_eap/content/map_registry.py",
            "doom_eap/runtime/observer_lifecycle.py",
            "doom_eap/contracts/publisher_contracts.py",
            "doom_eap/launcher/launcher_app.py",
            "doom_eap/launcher/launcher_cli.py",
            "doom_eap/launcher/launcher_controller.py",
            "doom_eap/launcher/launcher_core.py",
            "doom_eap/launcher/launcher_doctor.py",
            "doom_eap/launcher/launcher_integration.py",
            "doom_eap/launcher/launcher_native_health.py",
            "doom_eap/launcher/launcher_platform.py",
            "doom_eap/launcher/launcher_supervisor.py",
            "doom_eap/launcher/launcher_ui.py",
            "doom_eap/runtime/publisher_runtime.py",
            "doom_eap/__init__.py", "doom_eap/launcher/__init__.py",
            "doom_eap/runtime/__init__.py", "doom_eap/runtime/save_decrypt.py",
            "tools/maps/start_with_automap.py",
        ):
            release_inputs[relative] = _sha256(ROOT / relative)
        return _canonical_hash({
            "identity": identity,
            "maps": {item.map_key: item.digest for item in artifacts},
            "apworld": apworld_files,
            "release_inputs": release_inputs,
            "pipeline_version": PIPELINE_VERSION,
        })

    def _workspace_members(
        self,
        artifacts: Sequence[MapArtifact],
    ) -> dict[str, dict[str, int | str]] | None:
        members: dict[str, dict[str, int | str]] = {}
        for artifact in artifacts:
            for prefix, source in (
                ("maps", artifact.output),
                ("manifests", artifact.manifest),
            ):
                if source.is_symlink() or not source.is_file():
                    return None
                metadata = {
                    "sha256": _sha256(source),
                    "size": source.stat().st_size,
                }
                destination = f"{prefix}/{source.name}"
                if destination in members and members[destination] != metadata:
                    return None
                members[destination] = metadata
            if not _merge_members(members, "mod", artifact.patch_mod):
                return None
        return members

    def _receipt_matches(
        self,
        receipt_path: Path,
        *,
        workspace_digest: str,
        artifact_root: Path,
        members: dict[str, dict[str, int | str]],
        artifacts: Sequence[MapArtifact],
    ) -> bool:
        try:
            document = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                return False
            if (
                document.get("schema_version") != 3
                or document.get("workspace_digest") != workspace_digest
                or document.get("artifact_root") != str(artifact_root)
                or document.get("workspace_members") != members
                or document.get("content_identity")
                != json.loads(IDENTITY_PATH.read_text(encoding="utf-8"))
            ):
                return False
            expected_maps = self._receipt_maps(artifacts)
            if document.get("maps") != expected_maps:
                return False
            return _member_hashes(artifact_root) == members
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def _receipt_maps(self, artifacts: Sequence[MapArtifact]) -> dict[str, dict[str, object]]:
        return {
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
        }

    def receipt(self, artifacts: Sequence[MapArtifact], stages: Sequence[str]) -> Path:
        workspace_digest = self._workspace_digest(artifacts)
        artifact_root = WORKSPACES_ROOT / workspace_digest
        receipt_path = RECEIPTS_ROOT / f"{workspace_digest}.json"
        members = self._workspace_members(artifacts)
        if members is None:
            raise ValueError("workspace sources are not a complete regular-file cache")
        self.workspace_cache_hit = False
        if not self.no_cache and artifact_root.exists():
            self.workspace_cache_hit = _member_hashes(artifact_root) == members
            if not self.workspace_cache_hit:
                print(f"WORKSPACE cache=invalid digest={workspace_digest[:16]} reason=integrity")
                if artifact_root.exists() or artifact_root.is_symlink():
                    _remove_path(artifact_root)
        if not self.workspace_cache_hit:
            CACHE_ROOT.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(
                prefix=f".{workspace_digest}.", dir=CACHE_ROOT
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
        if _member_hashes(artifact_root) != members:
            raise ValueError(f"workspace cache failed integrity check: {artifact_root}")
        self.receipt_cache_hit = (
            not self.no_cache
            and self.workspace_cache_hit is True
            and receipt_path.is_file()
            and not receipt_path.is_symlink()
            and self._receipt_matches(
                receipt_path,
                workspace_digest=workspace_digest,
                artifact_root=artifact_root,
                members=members,
                artifacts=artifacts,
            )
        )
        if self.receipt_cache_hit:
            return receipt_path
        if receipt_path.exists():
            print(f"RECEIPT cache=invalid digest={workspace_digest[:16]} reason=integrity")
        document = {
            "schema_version": 3,
            "workspace_digest": workspace_digest,
            "content_identity": json.loads(IDENTITY_PATH.read_text(encoding="utf-8")),
            "maps": self._receipt_maps(artifacts),
            "workspace_members": members,
            "stages": list(stages),
            "tools": {
                "pipeline": PIPELINE_VERSION,
                "python": sys.version.split()[0],
            },
            "artifact_root": str(artifact_root),
        }
        RECEIPTS_ROOT.mkdir(parents=True, exist_ok=True)
        receipt_fd, receipt_name = tempfile.mkstemp(
            prefix=f".{workspace_digest}.", suffix=".json", dir=RECEIPTS_ROOT
        )
        os.close(receipt_fd)
        temporary_receipt = Path(receipt_name)
        try:
            temporary_receipt.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            os.replace(temporary_receipt, receipt_path)
        except BaseException:
            temporary_receipt.unlink(missing_ok=True)
            raise
        return receipt_path

    def apworld_contract_smoke(self, tests: Sequence[str] = ()) -> None:
        with self.timed("apworld-contract"):
            python = ARCHIPELAGO / ".venv" / "bin" / "python"
            env = os.environ.copy()
            env["PYTHONPATH"] = f"{ARCHIPELAGO}:{ROOT}"
            env["AP_TEST_WORLDS"] = "doometernal"
            contract = (
                "from worlds.doometernal import DoomEternalWorld; "
                "assert DoomEternalWorld.game; "
                "assert DoomEternalWorld.item_name_to_id; "
                "assert DoomEternalWorld.location_name_to_id"
            )
            _run([str(python), "-c", contract], cwd=WORKSPACE, env=env)
            print(
                "APWORLD contract fingerprint="
                f"{apworld_fingerprint(ARCHIPELAGO / 'worlds' / 'doometernal')}"
            )
            if tests:
                self._run_focused_tests(tests)

    def release(
        self,
        *,
        build: bool = False,
        tests: Sequence[str] = (),
    ) -> tuple[Path, list[MapArtifact]]:
        catalog = self.preflight()
        pending_imports = [
            asset.key for asset in catalog.assets
            if asset.dependency_policy == "model_importer_bundle_pending"
        ]
        if pending_imports:
            raise ValueError(
                "component=assets map=* field=model_importer value=pending "
                f"bundles={pending_imports}"
            )
        artifacts = self.integration(tests=tests, full=True)
        assert self.integration_receipt is not None
        receipt = self.integration_receipt
        if build:
            document = json.loads(receipt.read_text(encoding="utf-8"))
            env = os.environ.copy()
            env["AP_PIPELINE_RECEIPT"] = str(receipt)
            env["AP_PIPELINE_ARTIFACT_ROOT"] = document["artifact_root"]
            _run(["bash", "scripts/build/playable_test.sh"], env=env)
            self.package_preflight()
        return receipt, artifacts

    def report(self) -> None:
        total = time.perf_counter() - self.started
        print(f"PIPELINE duration={total:.3f}s")
        for name, duration in self.timings:
            print(f"STAGE name={name} duration_ms={duration * 1000:.3f}")
        hits = sum(value for value in self.cache_hits.values())
        misses = len(self.cache_hits) - hits
        workspace_hits = int(self.workspace_cache_hit is True)
        workspace_misses = int(self.workspace_cache_hit is False)
        receipt_hits = int(self.receipt_cache_hit is True)
        receipt_misses = int(self.receipt_cache_hit is False)
        print(
            "CACHE summary=maps "
            f"hits={hits} misses={misses} "
            f"workspace_hits={workspace_hits} workspace_misses={workspace_misses} "
            f"receipt_hits={receipt_hits} receipt_misses={receipt_misses}"
        )
        for key in sorted(self.cache_hits):
            print(
                f"CACHE map={key} status={'hit' if self.cache_hits[key] else 'miss'} "
                f"generations={self.generation_counts.get(key, 0)}"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=(
        "fast", "map", "affected", "changed", "integration",
        "package-preflight", "package", "playtest", "full", "release",
        "seed-smoke", "cache-key",
    ))
    parser.add_argument("map_key", nargs="?")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--baseline-diff", action="store_true")
    parser.add_argument("--show-selection", action="store_true")
    parser.add_argument("--map", dest="maps", action="append", default=[])
    parser.add_argument("--test", dest="tests", action="append", default=[])
    parser.add_argument("--package-root", type=Path)
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
        elif args.phase in {"changed", "affected"}:
            selected, paths = pipeline.selection(preflight=not args.show_selection)
            if args.show_selection:
                print("CHANGED FILES")
                print("\n".join(f"  {path}" for path in paths) or "  (none)")
                print("SELECTED MAPS")
                print("\n".join(f"  {key}" for key in selected) or "  (none)")
            if selected and not args.show_selection:
                pipeline.integration(selected, tests=args.tests)
        elif args.phase == "integration":
            selected = args.maps or ([args.map_key] if args.map_key else None)
            pipeline.integration(selected, tests=args.tests)
        elif args.phase == "package-preflight":
            pipeline.package_preflight(args.package_root)
        elif args.phase in {"package", "playtest"}:
            pipeline.playtest()
        elif args.phase == "full":
            pipeline.full(args.tests)
        elif args.phase == "release":
            pipeline.release(build=args.build, tests=args.tests)
        elif args.phase == "seed-smoke":
            pipeline.seed_smoke()
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
