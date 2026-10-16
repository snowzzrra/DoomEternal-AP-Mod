import builtins
import io
import json
from copy import deepcopy
from pathlib import Path
from zipfile import ZipFile

import pytest

from doom_eap.content.enemy_options import normalize_enemy_options
from doom_eap.content import enemy_resources
from doom_eap.launcher.launcher_core import ModCompiler, RoomCompiler, SeedManifest
from tools.maps.encounter_materializer import text_sha256
from tools.maps.ap_map_generator import find_entity_block_bounds
from tools.release.room_payloads import map_physical_option_keys
from tools.maps.mission_complete_map_patcher import _patch_hell


ROOT = Path(__file__).resolve().parents[1]
TARGET = "e1m1_intro_patch3/maps/game/sp/e1m1_intro/e1m1_intro.entities"
RESOURCE = "gameresources_patch3/cold-test-stone.asset"


def cold_case(tmp_path, monkeypatch):
    def resources(archive, manifest, species):
        assert set(species) == {"stone_imp"}
        return {RESOURCE: b"qualified enemy resource service"}

    monkeypatch.setattr(enemy_resources, "read_enemy_resource_payloads", resources)
    map_plan = {
        "schema": 1, "map_key": "e1m1_intro",
        "source_sha256": "5d8d1a6c6a377a77e5c8246c5eaf5034a1f4f917e82621645bf70e143b43d4a6",
        "operations": [{
            "manager": {"entity": "subway_encounter_manager_subway_hall",
                        "sha256": "401d25d4ecb3595f93cb79e11984afc7ca7e727145d3294db62ff6c82fec1f86"},
            "script_index": 0, "event_index": 0, "event_def": "spawnSingleAI",
            "event_sha256": "7f6dec569de4369c1dcb6f5de0cb213cc7ee1ee63f0a13f712e1d5d54c5a8f68",
            "from": {"species": "hell_soldier", "spawn_type": "ENCOUNTER_SPAWN_HELL_SOLDIER",
                     "spawner": {"map_key": "e1m1_intro", "entity": "target_spawn_151",
                                 "sha256": "3b135032ef8a2e1e4aa81bd00732c8dfe26ca80ccb2f47375e2bc00de33f2737"}},
            "waves": [{"instances": [{
                "species": "stone_imp", "spawn_type": "ENCOUNTER_SPAWN_STONE_IMP",
                "ai": {"map_key": "e5m1_spear", "entity": "ai_combat_ai_fodder_imp_stone_2",
                       "sha256": "29bfe0aee160c9023158469f3feea966c751c0f9474d59a759683b0d461d56d4"},
                "ai_name": "ap_v070_ai_cold_stone__ap_v070_s00100",
                "spawner_name": "ap_v070_spawn_cold_stone__ap_v070_s00100",
                "attributes": {"health_percent": 100, "damage_percent": 100,
                               "speed_percent": 100, "size_percent": 100},
            }]}],
        }],
    }
    plan = {
        "revision": 1, "seed": "cold-test", "maps": {"e1m1_intro": map_plan},
        "finite_kills": 16,
        "finite_sources": [{"map_key": "e1m1_intro", "location": "Subway fight opportunity",
                            "count": 16, "repeatable": True}],
    }
    snapshots = {}
    archive_path = tmp_path / "payloads.zip"
    with ZipFile(archive_path, "w") as archive:
        for key in ("e1m1_intro", "e5m1_spear"):
            content = (ROOT / "vanillamaps" / f"{key}.map").read_bytes()
            member = f"sources/{key}.entities"
            archive.writestr(member, content)
            snapshots[key] = {"member": member, "sha256": text_sha256(content.decode("utf-8")),
                              "size": len(content)}
    compiler = RoomCompiler.__new__(RoomCompiler)
    contract = json.loads((ROOT / "data/mission_complete_map_contracts.json").read_text())["hell_on_earth"]
    base = tmp_path / "base"
    _patch_hell(contract, ROOT, base)
    compiler.base_resource = tmp_path / "base.zip"
    with ZipFile(compiler.base_resource, "w") as archive:
        archive.writestr(TARGET, b"previous compiled content")
        archive.writestr(contract["override_path"], (base / contract["override_path"]).read_bytes())
    compiler.payload_resource = archive_path
    targets = {"e1m1_intro": TARGET, "e5m1_spear": "e5m1_spear/maps/game/dlc2/e5m1_spear/e5m1_spear.entities"}
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
    compiler._compress_entities_text = lambda text: text.encode("utf-8")
    enemies = normalize_enemy_options({"enemizer_preset": "balanced",
                                       "enemy_speed": {"minimum": 150, "maximum": 150}})
    manifest = SeedManifest.create(seed_name="Cold", team=0, slot=1,
                                   active_location_ids=ModCompiler(ROOT).known_location_ids(),
                                   options={"enemy_plan": plan, "enemies": enemies,
                                            "use_dlc_content": True, "include_dlc_missions": False},
                                   static_precompile=True)
    return compiler, manifest


def base_files(compiler):
    with ZipFile(compiler.base_resource) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_cold_compiler_uses_packaged_sources_and_frozen_clone_without_authoring_reads(tmp_path, monkeypatch):
    compiler, manifest = cold_case(tmp_path, monkeypatch)
    instance = manifest.options["enemy_plan"]["maps"]["e1m1_intro"]["operations"][0]["waves"][0]["instances"][0]
    instance["attributes"].update(health_percent=200, damage_percent=150, size_percent=50)
    original_builtin, original_io = builtins.open, io.open

    def guarded(original):
        def open_file(file, *args, **kwargs):
            if isinstance(file, (str, Path)) and {"vanillamaps", "vanillaentities"} & {part.lower() for part in Path(file).parts}:
                raise AssertionError("Cold room compilation attempted to read authoring sources")
            return original(file, *args, **kwargs)
        return open_file

    monkeypatch.setattr(builtins, "open", guarded(original_builtin))
    monkeypatch.setattr(io, "open", guarded(original_io))
    assembled = base_files(compiler)
    compiler._apply_enemy_plan(assembled, manifest)
    result = assembled[TARGET]
    assert b"entityDef ap_v070_ai_cold_stone__ap_v070_s00100" in result
    assert b"entityDef ap_v070_spawn_cold_stone" in result
    assert b"ENCOUNTER_SPAWN_STONE_IMP" in result
    assert b"inherit = \"ai/fodder/imp_stone\"" in result
    from tools.maps.encounter_catalog import entity_records
    text = result.decode("utf-8")
    begin, end = find_entity_block_bounds(text, instance["ai_name"])
    private = next(entity_records(text[begin:end]))["edit"]
    assert private["aiHealth"]["components"]["components[0]"]["max"] == 400
    assert private["aiHealth"]["components"]["components[0]"]["starting"] == 400
    assert private["aiEditable"]["damageOutModifiers"] == {
        "num": 1, "item[0]": {"damageSource": "DAMAGESRC_ALL", "damageModifier": 1.5},
    }
    assert private["renderModelInfo"]["scale"] == {"x": 0.5, "y": 0.5, "z": 0.5}
    assert private["clipModelInfo"]["size"] == {"x": 0.5, "y": 0.5, "z": 1.60000002}
    from tools.maps.encounter_populations import PRIVATE_POPULATION_ASSETSINFO_PREFIX, PRIVATE_POPULATION_PREFIX
    populations = {name for name in assembled if name.startswith(PRIVATE_POPULATION_PREFIX)}
    metadata = {name for name in assembled if name.startswith(PRIVATE_POPULATION_ASSETSINFO_PREFIX)}
    assert len(populations) == 1
    assert len(metadata) == 1
    population = next(iter(populations))
    reference = "actorpopulation/ap_v070/" + population.removeprefix(PRIVATE_POPULATION_PREFIX).removesuffix(".decl")
    assert f'aiTypeDefAssignments = "{reference}"'.encode() in result
    assert b'ai/fodder/imp_stone' in assembled[population]
    asset = json.loads(assembled[next(iter(metadata))])["assets"][0]
    assert asset["name"] == reference
    assert asset["mapResourceType"] == "actorPopulation"
    assert set(assembled) == {*base_files(compiler), RESOURCE, *populations, *metadata}
    assert assembled[RESOURCE] == b"qualified enemy resource service"
    replay = base_files(compiler)
    compiler._apply_enemy_plan(replay, manifest)
    assert replay == assembled


def test_cold_compiler_rejects_drift_before_replacing_packaged_map(tmp_path, monkeypatch):
    from dataclasses import replace

    compiler, manifest = cold_case(tmp_path, monkeypatch)
    options = deepcopy(manifest.options)
    options["enemy_plan"]["maps"]["e1m1_intro"]["source_sha256"] = "0" * 64
    assembled = base_files(compiler)
    with pytest.raises(ValueError, match="Frozen source hash"):
        compiler._apply_enemy_plan(assembled, replace(manifest, options=options))
    assert assembled == base_files(compiler)


@pytest.mark.parametrize("with_reserve", [False, True], ids=["authored-supply", "private-reserve"])
def test_cold_compiler_materializes_pinned_group_as_two_frozen_waves(tmp_path, monkeypatch, with_reserve):
    compiler, manifest = cold_case(tmp_path, monkeypatch)
    plan = manifest.options["enemy_plan"]["maps"]["e1m1_intro"]
    plan["schema"] = 2
    operation = plan["operations"][0]
    template = operation["waves"][0]["instances"][0]
    operation.update(event_index=2, event_def="spawnAI",
                     event_sha256="7dda56a93509e45b20fd3704667435b0cf6389102f355c59f83432dbbff4e820",
                     barrier={"event_index": 3,
                              "event_sha256": "100a601278bb34b665e02517e0777ae319cb86f777f329d4ffc446812f78be9c"})
    operation["from"] = {
        "species": "zombie_tier_1", "spawn_type": "ENCOUNTER_SPAWN_ZOMBIE_TIER_1",
        "spawner": {"map_key": "e1m1_intro", "entity": "subway_encounter_spawn_subway_hall_zombies",
                    "sha256": "7ac9504c5e8f9e690471d016688043a0534415451cb2760578440816b68529e2"},
    }
    members = (
        (137, "4ff9058a4f7bd83b681fa853e4d86702c56bd7a42bdcd680c82fa3f115a7a2fb"),
        (138, "b894c34ea15f0c9d8bb7a22cbb0caf12e64a7f00aebecdb17273e31d6107d71a"),
        (136, "5fae444fc9934858201a73ae39040ce53f88f238990a3182fc575c9358de2f35"),
        (139, "3e9c8c1060285e47dbf05cb715926d31c10e9808246936ca6f77ccf872aa9575"),
    )
    instances = []
    for index, (number, digest) in enumerate(members):
        instance = deepcopy(template)
        for field, kind in (("ai_name", "ai"), ("spawner_name", "spawn"), ("group_name", "group")):
            instance[field] = f"ap_v070_{kind}_cold_wave{index}__ap_v070_s00100"
        instance["origin_spawner"] = {"map_key": "e1m1_intro", "entity": f"target_spawn_{number}", "sha256": digest}
        instances.append(instance)
    operation["waves"] = [
        {"instances": instances[index:index + 2], "label": f"ap_v070_cold_wave{index // 2}",
         "min_spawn_stagger": 0, "max_spawn_stagger": 0} for index in (0, 2)
    ]
    if with_reserve:
        plan["schema"] = 3
        operation["reserve"] = {
            "commit_template": {"map_key": "e1m1_intro", "entity": "subway_encounter_trigger_commit_2",
                                "sha256": "f4a3fc2cbe2f4aa5558a6111b549e3b025d71994a10393c75242eafa5d8aa716"},
            "finish_template": {"map_key": "e1m1_intro", "entity": "master_level_encounter_trigger_user_flag_1",
                                "sha256": "d64a10755c94f9993dd36e8456151f74209504c26dbf9280a3e19747e6524e05"},
            "manager_name": "ap_v070_reserve_manager_cold", "commit_name": "ap_v070_reserve_commit_cold",
            "finish_name": "ap_v070_reserve_finish_cold", "label": "ap_v070_reserve_supply_cold",
            "completion_flag": "ap_v070_reserve_done_cold", "desired_count": 1,
            "min_spawn_delay": 1, "max_spawn_delay": 2,
        }
    assembled = base_files(compiler)
    compiler._apply_enemy_plan(assembled, manifest)
    result = assembled[TARGET]
    text = result.decode("utf-8")
    begin, end = find_entity_block_bounds(text, "subway_encounter_manager_subway_hall")
    manager = text[begin:end]
    assert manager.count('eventDef = "staggeredAISpawn"') == 4
    assert manager.count('eventDef = "waitStaggeredSpawnComplete"') == 2
    if with_reserve:
        from tools.maps.encounter_catalog import entity_records, list_items
        begin, end = find_entity_block_bounds(text, operation["reserve"]["manager_name"])
        private = next(entity_records(text[begin:end]))["edit"]
        calls = [value["eventCall"] for value in list_items(private["encounterComponent"]["entityEvents"]["item[0]"]["events"])]
        assert [value["eventDef"] for value in calls] == [
            "maintainAICount", "waitForEventFlag", "stopMaintainingAICount", "removeAI",
        ]
        with ZipFile(compiler.payload_resource) as archive:
            source = archive.read("sources/e1m1_intro.entities").decode("utf-8")
        begin, end = find_entity_block_bounds(source, operation["manager"]["entity"])
        original = next(entity_records(source[begin:end]))["edit"]
        assert private["aiTypeDefAssignments"] == original["aiTypeDefAssignments"]
        assert manager.count(operation["reserve"]["commit_name"]) == 1
        assert manager.count(operation["reserve"]["finish_name"]) == 1
    for index in range(4):
        assert f"entityDef ap_v070_ai_cold_wave{index}__ap_v070_s00100".encode() in result
    from tools.maps.encounter_populations import PRIVATE_POPULATION_ASSETSINFO_PREFIX, PRIVATE_POPULATION_PREFIX
    populations = {name for name in assembled if name.startswith(PRIVATE_POPULATION_PREFIX)}
    metadata = {name for name in assembled if name.startswith(PRIVATE_POPULATION_ASSETSINFO_PREFIX)}
    assert len(populations) == 1
    assert len(metadata) == 1
    assert set(assembled) == {*base_files(compiler), RESOURCE, *populations, *metadata}


def test_cold_compiler_requires_resources_before_replacing_any_map(tmp_path, monkeypatch):
    compiler, manifest = cold_case(tmp_path, monkeypatch)

    def missing(*_):
        raise ValueError("Enemy resource profile is not packaged")

    monkeypatch.setattr(enemy_resources, "read_enemy_resource_payloads", missing)
    assembled = base_files(compiler)
    with pytest.raises(ValueError, match="not packaged"):
        compiler._apply_enemy_plan(assembled, manifest)
    assert assembled == base_files(compiler)


def test_cold_compiler_preserves_conflicting_owned_resource_and_maps(tmp_path, monkeypatch):
    compiler, manifest = cold_case(tmp_path, monkeypatch)
    assembled = {**base_files(compiler), RESOURCE: b"existing owned resource"}
    previous = dict(assembled)
    with pytest.raises(ValueError, match="conflict with the room payload"):
        compiler._apply_enemy_plan(assembled, manifest)
    assert assembled == previous
