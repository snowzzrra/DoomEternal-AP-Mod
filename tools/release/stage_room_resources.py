"""Prepare the existing authorial recipe without installation or a Linux shell."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from doom_eap.content.map_registry import load_map_registry
from tools.release.prebuilt_room_resources import compute_room_resource_input_fingerprint
from tools.release.room_payloads import canonical_json


def prepare_authorial_tree(root: Path, output: Path, compressor: Path) -> Path:
    root, output, compressor = root.resolve(), output.resolve(), compressor.resolve()
    if not output.is_relative_to(root / "build") or output == root / "build":
        raise ValueError("Authorial output must be isolated beneath repository build/")
    if output.exists():
        raise FileExistsError(f"Authorial output already exists: {output}")
    fingerprint, _ = compute_room_resource_input_fingerprint(root)
    output.mkdir(parents=True)
    staged, work = output / "mod", output / "work"
    maps, manifests = work / "maps", work / "manifests"
    maps.mkdir(parents=True)
    manifests.mkdir()
    shutil.copytree(root / "packaging/mod_assets", staged)
    registry = load_map_registry(root / "data/map_sources.json")["maps"]
    sources, generated = {}, {}

    def run(module, *args):
        subprocess.run([sys.executable, "-m", module, *map(str, args)], cwd=root, check=True)

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    for key, spec in registry.items():
        if not spec.get("enabled", True):
            continue
        source = root / "vanillamaps" / spec["source_file"]
        sources[key] = sha(source)
        if sources[key] != spec["source_sha256"]:
            raise ValueError(f"Vanilla source hash mismatch: {key}")
        entities, manifest = maps / spec["generated_output"], manifests / f"{key}.json"
        run("tools.maps.ap_map_generator", "--input", source, "--output", entities,
            "--config", root / spec["level_config"], "--manifest", manifest, "--items", root / "data/items.json")
        if sha(source) != sources[key]:
            raise ValueError(f"Vanilla source modified: {key}")
        expected = json.loads((root / spec["manifest"]).read_text(encoding="utf-8"))
        actual = json.loads(manifest.read_text(encoding="utf-8"))
        if expected != actual:
            raise ValueError(f"Generated manifest differs: {key}; missing={sorted(set(expected) - set(actual))}; "
                             f"extra={sorted(set(actual) - set(expected))}")
        generated[key] = entities

    for language in ("english", "portuguese"):
        destination = staged / f"gameresources_patch1/EternalMod/strings/{language}.json"
        run("tools.release.build_string_table", "--items", root / "data/items.json",
            "--item-replay-policies", root / "data/item_replay_policies.json", "--maps-dir", maps,
            "--location-names", root / "data/location_names.json", "--output", destination)
        table = json.loads(destination.read_text(encoding="utf-8"))
        entries = {entry["name"]: entry for entry in table["strings"]}
        for key in ("#str_code__GHOST31004", "#str_code_mainmenu_campaign_name"):
            entries[key] = {"name": key, "text": "DOOM ETERNAL ARCHIPELAGO"}
        table["strings"] = [entries[key] for key in sorted(entries)]
        destination.write_text(json.dumps(table, indent=4, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")

    from tools.maps.mission_complete_map_patcher import patch_mission_complete_maps
    audit = patch_mission_complete_maps(root / "data/mission_complete_map_contracts.json", generated, staged)
    if audit["unrelated_generated_entity_diff_count"]:
        raise ValueError("Mission-complete patch changed unrelated entities")
    for key, entities in generated.items():
        spec = registry[key]
        target = staged / Path(spec["resource_path"]).stem / "maps" / spec["relative_entities_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([str(compressor), "--compress", str(entities), str(target)], check=True)
    for source_name, relative in (
        ("shell_menu_assetsinfo.json", "shell/EternalMod/assetsinfo/shell.json"),
        ("hub_world_text_assetsinfo.json", "hub_patch2/EternalMod/assetsinfo/hub.json"),
    ):
        target = staged / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / "packaging" / source_name, target)
    shell = work / "shell.entities"
    run("tools.maps.shell_menu_visual", "--source", root / "vanillamaps/shell.map", "--output", shell)
    shell_target = staged / "shell/maps/game/shell/shell.entities"
    shell_target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(compressor), "--compress", str(shell), str(shell_target)], check=True)
    for module in ("tools.maps.automap_native_decl_builder", "tools.decls.rune_decl_builder",
                   "tools.decls.rune_slot_builder", "tools.decls.mastery_decl_builder",
                   "tools.decls.mission_challenge_decl_builder", "tools.decls.weapon_stripping_builder",
                   "tools.decls.devinv_builder"):
        run(module, "--mod-root", staged, "--audit-output", work / f"{module.rsplit('.', 1)[1]}.json")
    run("tools.maps.logic_decl_patcher", "--contracts", root / "data/scripted_location_contracts.json",
        "--location", "7770074", "--output", staged / "hub_patch2/generated/decls/logicentity/maps/game/hub/hub/info_logic_hub_from_e1m2.decl",
        "--snapshot", work / "ice_logic_decl_patch.json")
    expected = json.loads((root / "data/snapshots/ice_logic_decl_patch.json").read_text(encoding="utf-8"))
    actual = json.loads((work / "ice_logic_decl_patch.json").read_text(encoding="utf-8"))
    actual.pop("changed_lines", None)
    if expected != actual:
        raise ValueError("Ice logic DECL structural snapshot drift")
    if compute_room_resource_input_fingerprint(root)[0] != fingerprint:
        raise ValueError("Compiler inputs changed during authorial preparation")
    (output / "AUTHORIAL_BUILD_INPUTS.json").write_bytes(canonical_json({
        "compiler_input_fingerprint": fingerprint, "vanilla_source_hashes": sources,
        "compressor_sha256": sha(compressor),
        "staged_files": {path.relative_to(staged).as_posix(): sha(path) for path in sorted(staged.rglob("*")) if path.is_file()},
    }))
    return staged


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--compressor", type=Path, required=True)
    args = parser.parse_args()
    print(prepare_authorial_tree(args.repo_root, args.output_dir, args.compressor))


if __name__ == "__main__":
    main()
