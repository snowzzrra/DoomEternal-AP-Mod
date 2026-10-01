"""Deterministic room-resource compilation from a prepared authorial mod tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from doom_eap.content.content_catalog import load_content_catalog
from doom_eap.content.physical_options import PHYSICAL_OPTION_KEYS, map_physical_option_keys
from doom_eap.launcher.launcher_core import ModCompiler, SeedManifest
from doom_eap.runtime.context_registry import dlc_contexts
from tools.maps.ap_map_generator import generate_context_marker_overlay
from tools.maps.mission_complete_map_patcher import patch_mission_complete_maps
from tools.release.room_payloads import (
    BASE_RESOURCE_NAME,
    ROOM_PAYLOAD_MANIFEST_NAME,
    ROOM_PAYLOAD_RESOURCE_NAME,
    canonical_json,
    plan_map_local_states,
    validate_room_payload_manifest,
    write_deterministic_zip,
    zip_directory,
)
from tools.release.prebuilt_room_resources import compute_room_resource_input_fingerprint


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha_files(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(set(paths), key=lambda item: item.as_posix()):
        if not path.is_file():
            raise ValueError(f"missing room resource input: {path}")
        digest.update(path.as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class StateCache:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def paths(self, material: dict[str, object]) -> tuple[str, Path, Path, Path]:
        key = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        directory = self.root / key[:2]
        return key, directory / f"{key}.json", directory / f"{key}.entities", directory / f"{key}.packed"

    def read(self, material: dict[str, object]) -> tuple[bytes, bytes] | None:
        key, metadata_path, entities_path, packed_path = self.paths(material)
        if not all(path.is_file() for path in (metadata_path, entities_path, packed_path)):
            return None
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            entities, packed = entities_path.read_bytes(), packed_path.read_bytes()
        except (OSError, ValueError):
            return None
        if (metadata.get("key") != key or metadata.get("material") != material
                or metadata.get("entities_sha256") != hashlib.sha256(entities).hexdigest()
                or metadata.get("packed_sha256") != hashlib.sha256(packed).hexdigest()):
            return None
        return entities, packed

    def write(self, material: dict[str, object], entities: bytes, packed: bytes) -> None:
        key, metadata_path, entities_path, packed_path = self.paths(material)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        entities_path.write_bytes(entities)
        packed_path.write_bytes(packed)
        metadata_path.write_text(json.dumps({
            "key": key, "material": material,
            "entities_sha256": hashlib.sha256(entities).hexdigest(),
            "packed_sha256": hashlib.sha256(packed).hexdigest(),
        }, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def build_room_resources(root: Path, staged: Path, work: Path, compressor: Path,
                         map_sources_path: Path, output: Path, cache_root: Path) -> None:
    root, staged, work, compressor, map_sources_path, output = (
        path.resolve() for path in (root, staged, work, compressor, map_sources_path, output)
    )
    if os.name == "nt" and compressor.read_bytes()[:2] != b"MZ":
        raise ValueError(f"Windows room build requires the PE compressor: {compressor}")
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    compiler = ModCompiler(root)
    catalog = load_content_catalog(root)
    raw_sources = json.loads(map_sources_path.read_text(encoding="utf-8"))["maps"]
    release_keys = tuple(spec.key for spec in catalog.enabled_maps())
    maps = {
        key: (entry["resource_path"], entry["relative_entities_path"], root / "vanillamaps" / entry["source_file"])
        for key, entry in raw_sources.items() if key in release_keys and entry["enabled"]
    }
    if set(maps) != set(release_keys):
        raise ValueError("room payload map set does not match the content catalog")

    compiler_identity, _ = compute_room_resource_input_fingerprint(root)
    # A caller may supply a registry outside the default source tree.
    compiler_identity = hashlib.sha256((compiler_identity + _sha(map_sources_path)).encode("ascii")).hexdigest()
    compressor_identity = _sha(compressor)
    cache = StateCache(cache_root.resolve())
    canonical_options = {key: False for key in PHYSICAL_OPTION_KEYS}
    plans = {plan.map_key: plan for plan in plan_map_local_states(tuple(maps))}

    local_identity: dict[str, str] = {}
    for key, entry in raw_sources.items():
        paths = [root / value for value in entry.values() if isinstance(value, str) and (root / value).is_file()]
        local_identity[key] = _sha_files(paths)

    def compile_state(key: str, vanilla: Path, options: dict[str, bool], entities: Path, packed: Path) -> None:
        material = {
            "schema": 1, "map_key": key, "source_identity": _sha(vanilla),
            "compiler_identity": compiler_identity, "compressor_identity": compressor_identity,
            "local_identity": local_identity[key], "state": dict(sorted(options.items())),
        }
        cached = cache.read(material)
        if cached:
            entities.write_bytes(cached[0])
            packed.parent.mkdir(parents=True, exist_ok=True)
            packed.write_bytes(cached[1])
            return
        compile_options = dict(canonical_options)
        compile_options.update(options)
        manifest = SeedManifest.create(
            seed_name=f"room-payload-{key}-state", team=0, slot=1, options=options,
            active_location_ids=compiler.active_location_ids(compile_options), static_precompile=True,
        )
        compiler.compile_map(manifest, vanilla, entities, key)
        with tempfile.TemporaryDirectory(prefix=f"room-{key}-patch-") as patch_root:
            audit = patch_mission_complete_maps(
                root / "data/mission_complete_map_contracts.json", {key: entities}, Path(patch_root)
            )
        if audit["unrelated_generated_entity_diff_count"]:
            raise RuntimeError(f"mission-complete patch changed unrelated entities: {key}")
        packed.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([str(compressor), "--compress", str(entities), str(packed)], check=True)
        cache.write(material, entities.read_bytes(), packed.read_bytes())

    for key, (resource_path, relative, vanilla) in maps.items():
        target = staged / f"{Path(resource_path).stem}/maps/{relative}"
        plan = plans[key]
        if plan.compile_base:
            target.parent.mkdir(parents=True, exist_ok=True)
            compile_state(key, vanilla, plan.base_options, work / f"{key}-base.entities", target)
        elif not target.is_file():
            raise ValueError(f"prepared authorial staging is missing {target}")

    for context in dlc_contexts():
        key = context.map_keys[0]
        spec = catalog.maps[key]
        target = staged / f"{Path(spec.resource_path).stem}/maps/{context.runtime_maps[0]}.entities"
        if not target.is_file():
            text = (root / "vanillamaps" / spec.source_file).read_text(encoding="utf-8").rstrip()
            entities = work / f"{key}-context.entities"
            entities.write_text(text + "\n" + generate_context_marker_overlay(key, context.runtime_maps[0]).lstrip(),
                                encoding="utf-8", newline="")
            target.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run([str(compressor), "--compress", str(entities), str(target)], check=True)

    base_members = zip_directory(staged, output / BASE_RESOURCE_NAME)
    payload_files: dict[str, bytes] = {}
    records: dict[str, object] = {}
    for key, (resource_path, relative, vanilla) in maps.items():
        plan = plans[key]
        target_member = f"{Path(resource_path).stem}/maps/{relative}"
        states = []
        for options in plan.states:
            if not any(options.values()):
                states.append({"options": options, "source": "base", "member": None, "sha256": base_members[target_member]})
                continue
            labels = [name.removeprefix("randomize_") for name in map_physical_option_keys(key) if options[name]]
            state_name = "-".join(labels)
            entities, packed = work / f"{key}-{state_name}.entities", work / f"{key}-{state_name}.packed"
            compile_state(key, vanilla, options, entities, packed)
            member = f"replacements/{key}/{state_name}.entities"
            payload_files[member] = packed.read_bytes()
            states.append({"options": options, "source": "replacement", "member": member, "sha256": _sha(packed)})
        records[key] = {"option_keys": list(plan.option_keys), "target_member": target_member,
                        "states": states, "state_policy": plan.state_policy}
    manifest = {
        "schema_version": 1, "model": "dependent_map_payloads",
        "physical_option_keys": list(PHYSICAL_OPTION_KEYS), "base_members": sorted(base_members), "maps": records,
        "context_targets": {context.identity: f"{Path(catalog.maps[context.map_keys[0]].resource_path).stem}/maps/{context.runtime_maps[0]}.entities"
                            for context in dlc_contexts()},
    }
    validate_room_payload_manifest(manifest, known_maps={key: value["target_member"] for key, value in records.items()})
    write_deterministic_zip(payload_files, output / ROOM_PAYLOAD_RESOURCE_NAME)
    (output / ROOM_PAYLOAD_MANIFEST_NAME).write_bytes(canonical_json(manifest))
    print(f"ROOM_RESOURCES output={output} maps={len(records)} states={len(payload_files)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--staged-mod", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--compressor", type=Path, required=True)
    parser.add_argument("--map-sources", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    args = parser.parse_args()
    build_room_resources(args.repo_root, args.staged_mod, args.work_dir, args.compressor,
                         args.map_sources, args.output_dir, args.cache_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
