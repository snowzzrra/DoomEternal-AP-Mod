import json
from copy import deepcopy
from pathlib import Path
from zipfile import ZipFile

import pytest

from doom_eap.content.enemy_options import SPECIES_IDS, normalize_enemy_options
from doom_eap.content.native_enemy_plan import build_native_recipes
from doom_eap.launcher.launcher_core import ModCompiler, RoomCompiler, SeedManifest
from tools.maps.encounter_catalog import text_sha256
from tools.maps.ap_map_generator import find_entity_block_bounds
from tools.release.room_payloads import map_physical_option_keys
from tools.maps.mission_complete_map_patcher import _patch_hell

ROOT = Path(__file__).resolve().parents[1]
TARGET = "e1m1_intro_patch3/maps/game/sp/e1m1_intro/e1m1_intro.entities"

def cold_case(tmp_path, monkeypatch):
    enemies = normalize_enemy_options({})
    plan = build_native_recipes(enemies, {"sequence": ["e1m1_intro"]}, seed=73822)
    keys = {"e1m1_intro"} | {body["ai"]["map_key"] for row in plan["maps"].values()
                             for recipe in row["recipes"] for body in recipe["bodies"]}
    compiler = RoomCompiler.__new__(RoomCompiler)
    compiler.base_resource = tmp_path / "base.zip"
    contract = json.loads((ROOT / "data/mission_complete_map_contracts.json").read_text())["hell_on_earth"]
    base = tmp_path / "base"
    _patch_hell(contract, ROOT, base)
    with ZipFile(compiler.base_resource, "w") as archive:
        archive.writestr(TARGET, b"previous compiled content")
        archive.writestr(contract["override_path"], (base / contract["override_path"]).read_bytes())
    compiler.payload_resource = tmp_path / "payloads.zip"
    snapshots = {}
    with ZipFile(compiler.payload_resource, "w") as archive:
        for key in sorted(keys):
            data = (ROOT / "vanillamaps" / f"{key}.map").read_bytes()
            member = f"sources/{key}.entities"
            archive.writestr(member, data)
            snapshots[key] = {"member": member, "sha256": text_sha256(data.decode()), "size": len(data)}
    targets = {key: TARGET if key == "e1m1_intro" else f"{key}/maps/{key}.entities" for key in keys}
    compiler.payload_manifest = {
        "schema_version": 2, "model": "dependent_map_payloads",
        "physical_option_keys": ["randomize_chainsaw", "randomize_dash", "randomize_first_battery"],
        "base_members": list(targets.values()), "context_targets": {"test": TARGET},
        "maps": {key: {"option_keys": list(map_physical_option_keys(key)),
                       "target_member": member, "state_policy": "off_only",
                       "states": [{"options": dict.fromkeys(map_physical_option_keys(key), False),
                                   "source": "base", "member": None, "sha256": "0" * 64}]}
                 for key, member in targets.items()},
        "source_snapshots": snapshots,
    }
    compiler._compress_entities_text = lambda text: text.encode()
    manifest = SeedManifest.create(seed_name="Cold", team=0, slot=1,
        active_location_ids=ModCompiler(ROOT).known_location_ids(),
        options={"enemy_plan": plan, "enemies": enemies, "use_dlc_content": True}, static_precompile=True)
    return compiler, manifest


def base_files(compiler):
    with ZipFile(compiler.base_resource) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_cold_room_compiles_native_recipes_without_reconstructing_encounter_flow(tmp_path, monkeypatch):
    from dataclasses import replace
    from doom_eap.content.native_enemy_plan import build_native_recipes, native_event_identity
    from tools.maps.encounter_catalog import entity_records, list_items

    compiler, manifest = cold_case(tmp_path, monkeypatch)
    enemies = normalize_enemy_options({"enemy_count_mode": "multiplier", "enemy_count_multiplier": 100,
        "enemy_weights": {name: int(name == "stone_imp") for name in SPECIES_IDS}})
    plan = build_native_recipes(enemies, {"sequence": ["e1m1_intro"]}, seed=73822)
    manifest = replace(manifest, options={**manifest.options, "enemy_plan": plan, "enemies": enemies})
    assembled = base_files(compiler)
    compiler._apply_enemy_plan(assembled, manifest)
    text = assembled[TARGET].decode()
    names = ["ap_v070_recipe_plan", "subway_encounter_manager_subway_hall"]
    records = {}
    for name in names:
        start, end = find_entity_block_bounds(text, name)
        records[name] = next(entity_records(text[start:end]))
    assert records["ap_v070_recipe_plan"]["edit"]["spawnEditableShared"]["groupName"].startswith("EAP4|")
    recipe = next(row for row in plan["maps"]["e1m1_intro"]["recipes"]
                  if native_event_identity(row) == "subway_encounter_manager_subway_hall/0/0")
    events = records[recipe["binding"]["entity"]]["edit"]["encounterComponent"]["entityEvents"]["item[0]"]["events"]
    call = list_items(events)[0]["eventCall"]
    assert call["eventDef"] == "spawnSingleAI"
    assert call["args"]["item[1]"]["entity"] == "ap_v070_request_r" + recipe["origin"]
    assert not any(name.startswith("gameresources_patch3/generated/decls/actorpopulation/ap_v070/") for name in assembled)
    replay = base_files(compiler)
    compiler._apply_enemy_plan(replay, manifest)
    assert replay == assembled


def test_cold_compiler_rejects_drift_before_replacing_packaged_map(tmp_path, monkeypatch):
    from dataclasses import replace

    compiler, manifest = cold_case(tmp_path, monkeypatch)
    options = deepcopy(manifest.options)
    options["enemy_plan"]["maps"]["e1m1_intro"]["source_sha256"] = "0" * 64
    assembled = base_files(compiler)
    with pytest.raises(ValueError, match="differ from the seed, options or source catalog"):
        compiler._apply_enemy_plan(assembled, replace(manifest, options=options))
    assert assembled == base_files(compiler)
