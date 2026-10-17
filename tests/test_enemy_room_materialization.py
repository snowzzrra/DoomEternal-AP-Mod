import builtins
import io
import json
from copy import deepcopy
from pathlib import Path
from zipfile import ZipFile

import pytest

from doom_eap.content.enemy_options import normalize_enemy_options
from doom_eap.content.enemy_plan import build_enemy_plan, enemy_combat_cost
from doom_eap.content import enemy_resources
from doom_eap.launcher.launcher_core import ModCompiler, RoomCompiler, SeedManifest
from tools.maps.encounter_materializer import text_sha256
from tools.maps.ap_map_generator import find_entity_block_bounds
from tools.release.room_payloads import map_physical_option_keys
from tools.maps.mission_complete_map_patcher import _patch_hell


ROOT = Path(__file__).resolve().parents[1]
TARGET = "e1m1_intro_patch3/maps/game/sp/e1m1_intro/e1m1_intro.entities"
RESOURCE = "gameresources_patch3/cold-test-stone.asset"


def cold_case(tmp_path, monkeypatch, species="stone_imp"):
    def resources(archive, manifest, required_species):
        assert set(required_species) == {species}
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
    profile = json.loads((ROOT / "content/catalog/encounter_composition.json").read_text())["species"][species]
    map_plan["operations"][0]["waves"][0]["instances"][0].update(species=species, **profile)
    plan = {
        "revision": 3, "seed": "cold-test", "maps": {"e1m1_intro": map_plan},
        "finite_kills": 16,
        "finite_sources": [{"map_key": "e1m1_intro", "location": "Subway fight opportunity",
                            "count": 16, "repeatable": True}],
        "combat_costs": {"e1m1_intro": {"original": 0, "target": None, "actual": float(enemy_combat_cost(
            species, map_plan["operations"][0]["waves"][0]["instances"][0]["attributes"]))}},
        "secret_timers": {},
    }
    snapshots = {}
    archive_path = tmp_path / "payloads.zip"
    with ZipFile(archive_path, "w") as archive:
        for key in sorted({"e1m1_intro", "e5m1_spear", profile["ai"]["map_key"]}):
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
    if species == "zombie_maykr":
        targets["e2m2_base"] = "e2m2_base/maps/game/sp/e2m2_base/e2m2_base.entities"
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


@pytest.mark.parametrize("species,health,inherit,clip", [
    ("stone_imp", 400, "imp_stone", {"x": 0.5, "y": 0.5, "z": 1.60000002}),
    ("zombie_maykr", 1600, "zombie_maykr", {"x": 0.400000006, "y": 0.400000006, "z": 1.5}),
])
def test_cold_compiler_uses_packaged_sources_and_frozen_clone_without_authoring_reads(tmp_path, monkeypatch, species, health, inherit, clip):
    compiler, manifest = cold_case(tmp_path, monkeypatch, species)
    instance = manifest.options["enemy_plan"]["maps"]["e1m1_intro"]["operations"][0]["waves"][0]["instances"][0]
    instance["attributes"].update(health_percent=200, damage_percent=150, size_percent=50)
    manifest.options["enemy_plan"]["combat_costs"]["e1m1_intro"]["actual"] = float(enemy_combat_cost(species, instance["attributes"]))
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
    assert instance["spawn_type"].encode() in result
    assert f'inherit = "ai/fodder/{inherit}"'.encode() in result
    from tools.maps.encounter_catalog import entity_records
    text = result.decode("utf-8")
    begin, end = find_entity_block_bounds(text, instance["ai_name"])
    private = next(entity_records(text[begin:end]))["edit"]
    assert private["aiHealth"]["components"]["components[0]"]["max"] == health
    assert private["aiHealth"]["components"]["components[0]"]["starting"] == health
    assert private["aiEditable"]["damageOutModifiers"] == {
        "num": 1, "item[0]": {"damageSource": "DAMAGESRC_ALL", "damageModifier": 1.5},
    }
    assert private["renderModelInfo"]["scale"] == {"x": 0.5, "y": 0.5, "z": 0.5}
    assert private["clipModelInfo"]["size"] == clip
    from tools.maps.encounter_populations import PRIVATE_POPULATION_ASSETSINFO_PREFIX, PRIVATE_POPULATION_PREFIX
    populations = {name for name in assembled if name.startswith(PRIVATE_POPULATION_PREFIX)}
    metadata = {name for name in assembled if name.startswith(PRIVATE_POPULATION_ASSETSINFO_PREFIX)}
    assert len(populations) == len(metadata) == (1 if species == "stone_imp" else 0)
    for population in populations:
        reference = "actorpopulation/ap_v070/" + population.removeprefix(PRIVATE_POPULATION_PREFIX).removesuffix(".decl")
        assert f'aiTypeDefAssignments = "{reference}"'.encode() in result
        assert f'ai/fodder/{inherit}'.encode() in assembled[population]
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


def test_generated_composition_materializes_multiplier_and_attributes_from_options(tmp_path, monkeypatch):
    from dataclasses import replace

    compiler, manifest = cold_case(tmp_path, monkeypatch)
    enemies = normalize_enemy_options({
        "enemy_weights": {"imp": 0, "hell_soldier": 0, "zombie_maykr": 0},
        "enemy_count_mode": "multiplier", "enemy_count_multiplier": 500,
        "enemy_health": {"minimum": 200, "maximum": 200},
        "enemy_damage": {"minimum": 150, "maximum": 150},
        "enemy_size": {"minimum": 50, "maximum": 50},
    })
    campaign = {"sequence": ["e1m1_intro"]}
    plan = build_enemy_plan(enemies, campaign, seed=71401)
    assert plan == build_enemy_plan(enemies, campaign, seed=71401)
    operation = plan["maps"]["e1m1_intro"]["operations"][0]
    assert [len(wave["instances"]) for wave in operation["waves"]] == [4] * 5
    assert {instance["species"] for wave in operation["waves"] for instance in wave["instances"]} == {"stone_imp"}
    options = {**manifest.options, "enemies": enemies, "enemy_plan": plan}
    generated = replace(manifest, options=options)
    assembled = base_files(compiler)
    compiler._apply_enemy_plan(assembled, generated)
    text = assembled[TARGET].decode("utf-8")
    begin, end = find_entity_block_bounds(text, operation["manager"]["entity"])
    from tools.maps.encounter_catalog import entity_records, list_items
    manager = next(entity_records(text[begin:end]))
    calls = [row["eventCall"] for row in list_items(list_items(manager["edit"]["encounterComponent"]["entityEvents"])[0]["events"])]
    expected = {actor["group_name"] for wave in operation["waves"] for actor in wave["instances"]}
    emitted = [list_items(call["args"])[2]["entity"] for call in calls if call["eventDef"] == "staggeredAISpawn"
               and list_items(call["args"])[2]["entity"] in expected]
    assert len(emitted) == len(expected) and set(emitted) == expected
    assert any(call["eventDef"] == "waitStaggeredSpawnComplete" for call in calls)
    for wave in operation["waves"]:
        for instance in wave["instances"]:
            begin, end = find_entity_block_bounds(text, instance["ai_name"])
            private = next(entity_records(text[begin:end]))["edit"]
            assert private["aiHealth"]["components"]["components[0]"]["max"] == 400
            assert private["renderModelInfo"]["scale"] == dict.fromkeys(("x", "y", "z"), 0.5)
    replay = base_files(compiler)
    compiler._apply_enemy_plan(replay, generated)
    assert replay == assembled


def test_disabled_composition_accepts_missions_without_replacement_profiles():
    enemies = normalize_enemy_options({"enemy_randomizer": False})
    assert build_enemy_plan(enemies, {"sequence": ["e5m4_boss"]}, seed=71401) is None


def test_finite_single_source_waves_keep_intervening_spawns_and_final_barrier(tmp_path, monkeypatch):
    from dataclasses import replace
    from tools.maps.encounter_catalog import entity_records, list_items

    compiler, manifest = cold_case(tmp_path, monkeypatch)
    plan = deepcopy(manifest.options["enemy_plan"])
    map_plan = plan["maps"]["e1m1_intro"]
    map_plan["schema"] = 4
    operation = map_plan["operations"][0]
    original = operation["waves"][0]["instances"][0]
    operation["barrier"] = {"event_index": 3, "event_sha256": "100a601278bb34b665e02517e0777ae319cb86f777f329d4ffc446812f78be9c"}
    operation["waves"] = []
    for index in range(3):
        instance = deepcopy(original)
        for field in ("ai_name", "spawner_name"):
            instance[field] = original[field].replace("cold_stone", f"phase_stone_{index}")
        instance["group_name"] = f"ap_v070_group_phase_{index}__ap_v070_s00100"
        operation["waves"].append({"label": f"ap_v070_phase_{index}", "min_spawn_stagger": 0,
                                   "max_spawn_stagger": 0, "instances": [instance]})
    plan["combat_costs"]["e1m1_intro"]["actual"] = float(sum(
        enemy_combat_cost(actor["species"], actor["attributes"])
        for wave in operation["waves"] for actor in wave["instances"]))
    manifest = replace(manifest, options={**manifest.options, "enemy_plan": plan})
    assembled = base_files(compiler)
    compiler._apply_enemy_plan(assembled, manifest)
    before = (ROOT / "vanillamaps/e1m1_intro.map").read_bytes().decode("utf-8")
    after = assembled[TARGET].decode("utf-8")
    def events(text):
        start, end = find_entity_block_bounds(text, operation["manager"]["entity"])
        manager = next(entity_records(text[start:end]))
        return [row["eventCall"] for row in list_items(list_items(manager["edit"]["encounterComponent"]["entityEvents"])[0]["events"])]
    initial, final = events(before), events(after)
    assert [row for row in final if row["eventDef"] in ("spawnSingleAI", "spawnAI")] == initial[1:3]
    assert final[-2:] == initial[-2:]
    assert sum(row["eventDef"] == "staggeredAISpawn" for row in final) == 3
    assert sum(row["eventDef"] == "waitStaggeredSpawnComplete" for row in final) == 3


@pytest.mark.parametrize("map_key", ["e1m2_war", "e1m3_cult"])
def test_finite_waves_preserve_native_highlight_and_label_barriers(map_key):
    from doom_eap.content.enemy_species import load_species_registry, project_species_profiles
    from tools.maps.encounter_catalog import entity_records, list_items
    from tools.maps.encounter_materializer import materialize_frozen_encounters

    enemies = normalize_enemy_options({"enemy_count_mode": "multiplier", "enemy_count_multiplier": 500,
                                      "enemy_weights": {"imp": 0, "hell_soldier": 0}})
    plan = build_enemy_plan(enemies, {"sequence": [map_key]}, seed=71403)["maps"][map_key]
    sources = {key: (ROOT / f"vanillamaps/{key}.map").read_bytes().decode("utf-8")
               for key in {map_key, "e1m1_intro", "e5m1_spear", "e2m2_base"}}
    profiles = project_species_profiles(load_species_registry(ROOT / "content/catalog/enemy_species.json"))
    result = materialize_frozen_encounters(sources[map_key], plan, sources, species_profiles=profiles)
    for operation in plan["operations"]:
        def events(text):
            start, end = find_entity_block_bounds(text, operation["manager"]["entity"])
            manager = next(entity_records(text[start:end]))
            script = list_items(manager["edit"]["encounterComponent"]["entityEvents"])[operation["script_index"]]
            return [row["eventCall"] for row in list_items(script["events"])]
        original = events(sources[map_key])[operation["barrier"]["event_index"]]
        final = events(result)
        assert original in final
        for wave in operation["waves"][1:]:
            expected = deepcopy(original)
            expected["args"]["item[2]"] = {"string": wave["label"]}
            assert final.index(expected) < max(index for index, call in enumerate(final) if call == original)


@pytest.mark.parametrize("multiplier", [50, 500])
def test_generated_phase_spawns_each_reinforcement_group_before_waiting(multiplier):
    from collections import defaultdict
    from doom_eap.content.enemy_species import load_species_registry, project_species_profiles
    from tools.maps.encounter_catalog import entity_records, list_items
    from tools.maps.encounter_materializer import materialize_frozen_encounters

    map_key = "e2m1_nest"
    enemies = normalize_enemy_options({"enemy_count_mode": "multiplier", "enemy_count_multiplier": multiplier})
    plan = build_enemy_plan(enemies, {"sequence": [map_key]}, seed=71405)["maps"][map_key]
    sources = {key: (ROOT / f"vanillamaps/{key}.map").read_bytes().decode("utf-8")
               for key in {map_key, "e1m1_intro", "e5m1_spear", "e2m2_base"}}
    profiles = project_species_profiles(load_species_registry(ROOT / "content/catalog/enemy_species.json"))
    result = materialize_frozen_encounters(sources[map_key], plan, sources, species_profiles=profiles)
    phases = defaultdict(list)
    for operation in plan["operations"]:
        phases[operation["manager"]["entity"], operation["script_index"], operation["barrier"]["event_index"]].append(operation)
    combined = False
    for (manager_name, script_index, barrier_index), operations in phases.items():
        def calls(text):
            start, end = find_entity_block_bounds(text, manager_name)
            manager = next(entity_records(text[start:end]))
            script = list_items(manager["edit"]["encounterComponent"]["entityEvents"])[script_index]
            return [row["eventCall"] for row in list_items(script["events"])]
        initial, final = calls(sources[map_key]), calls(result)
        previous = -1
        for index in range(1, max(len(operation["waves"]) for operation in operations)):
            labels = {operation["waves"][index]["label"] for operation in operations if index < len(operation["waves"])}
            combined |= len(labels) > 1
            spawn_positions, wait_positions, clear_positions = [], [], []
            for position, call in enumerate(final):
                args = list_items(call.get("args"))
                if call["eventDef"] == "staggeredAISpawn" and args[3]["string"] in labels:
                    spawn_positions.append(position)
                elif call["eventDef"] in ("waitStaggeredSpawnComplete", "waitAIRemaining") and args[2]["string"] in labels:
                    (wait_positions if call["eventDef"] == "waitStaggeredSpawnComplete" else clear_positions).append(position)
            assert previous < min(spawn_positions) <= max(spawn_positions) < min(wait_positions)
            assert max(wait_positions) < min(clear_positions)
            previous = max(clear_positions)
        assert max(index for index, call in enumerate(final) if call == initial[barrier_index]) > previous
        for operation in operations:
            if not operation["waves"]:
                assert initial[operation["event_index"]] not in final
            else:
                first_groups = {actor.get("group_name", "owner_" + actor["spawner_name"])
                                for actor in operation["waves"][0]["instances"]}
                label = list_items(initial[operation["event_index"]]["args"])[3 if operation["event_def"] != "spawnSingleAI" else 2]["string"]
                assert {list_items(call["args"])[3]["string"] for call in final
                        if call["eventDef"] == "staggeredAISpawn" and list_items(call["args"])[2]["entity"] in first_groups} == {label}
    assert combined == (multiplier == 500)


def test_secret_completion_follows_generated_waves_and_validates_original_script():
    from doom_eap.content.enemy_species import load_species_registry, project_species_profiles
    from tools.maps.ap_map_generator import inject_secret_encounter_completion
    from tools.maps.encounter_catalog import entity_records, list_items
    from tools.maps.encounter_materializer import materialize_frozen_encounters

    map_key = "e2m1_nest"
    enemies = normalize_enemy_options({"enemy_count_mode": "multiplier", "enemy_count_multiplier": 500,
                                      "enemy_weights": {"imp": 0, "hell_soldier": 0}})
    plan = build_enemy_plan(enemies, {"sequence": [map_key]}, seed=71405)["maps"][map_key]
    sources = {key: (ROOT / f"vanillamaps/{key}.map").read_bytes().decode("utf-8")
               for key in {map_key, "e1m1_intro", "e5m1_spear", "e2m2_base"}}
    profiles = project_species_profiles(load_species_registry(ROOT / "content/catalog/enemy_species.json"))
    expanded = materialize_frozen_encounters(sources[map_key], plan, sources, species_profiles=profiles)
    manager_name = "game_encounter_manager_70_e2m1"
    def events(text):
        start, end = find_entity_block_bounds(text, manager_name)
        manager = next(entity_records(text[start:end]))
        return list_items(list_items(manager["edit"]["encounterComponent"]["entityEvents"])[0]["events"])
    original = events(sources[map_key])
    expanded_events = events(expanded)
    assert len(expanded_events) > len(original)
    with pytest.raises(ValueError, match="before AP hook insertion"):
        inject_secret_encounter_completion(expanded, manager_name, "ap_test_completion", len(original) - 1)
    with pytest.raises(ValueError, match="before AP hook insertion"):
        inject_secret_encounter_completion(expanded, manager_name, "ap_test_completion", len(original), sources[map_key])
    result = inject_secret_encounter_completion(expanded, manager_name, "ap_test_completion", len(original) - 1, sources[map_key])
    final = events(result)
    assert final[:-1] == expanded_events
    assert final[-1]["eventCall"]["eventDef"] == "activateTarget"
    assert list_items(final[-1]["eventCall"]["args"])[0] == {"entity": "ap_test_completion"}
    assert inject_secret_encounter_completion(result, manager_name, "ap_test_completion", len(original) - 1, sources[map_key]) == result


def test_finite_waves_reject_intervening_native_kill_count_gate(tmp_path, monkeypatch):
    from doom_eap.content.enemy_species import load_species_registry, project_species_profiles
    from tools.maps.encounter_catalog import entity_records, list_items
    from tools.maps.encounter_materializer import _value_span, materialize_frozen_encounters

    _, manifest = cold_case(tmp_path, monkeypatch)
    source = (ROOT / "vanillamaps/e2m1_nest.map").read_bytes().decode("utf-8")
    name = "slayergate_e2m1_encounter_manager_1"
    start, end = find_entity_block_bounds(source, name)
    manager_text = source[start:end]
    manager = next(entity_records(manager_text))
    events = list_items(list_items(manager["edit"]["encounterComponent"]["entityEvents"])[0]["events"])
    args = list_items(events[2]["eventCall"]["args"])
    spawner = args[2]["entity"]
    start, end = find_entity_block_bounds(source, spawner)
    def event_hash(index):
        path = ("encounterComponent", "entityEvents", "item[0]", "events", f"item[{index}]", "eventCall")
        start, end = _value_span(manager_text, path)
        return text_sha256(manager_text[start:end].strip())
    operation = deepcopy(manifest.options["enemy_plan"]["maps"]["e1m1_intro"]["operations"][0])
    operation.update(manager={"entity": name, "sha256": text_sha256(manager_text.strip())}, event_index=2,
                     event_def="spawnAI", event_sha256=event_hash(2),
                     barrier={"event_index": 22, "event_sha256": event_hash(22)},
                     **{"from": {"species": "hell_soldier", "spawn_type": args[0]["eEncounterSpawnType_t"],
                         "spawner": {"map_key": "e2m1_nest", "entity": spawner,
                                     "sha256": text_sha256(source[start:end].strip())}}})
    operation["waves"] *= 2
    plan = {"schema": 4, "map_key": "e2m1_nest", "source_sha256": text_sha256(source), "operations": [operation]}
    profiles = project_species_profiles(load_species_registry(ROOT / "content/catalog/enemy_species.json"))
    with pytest.raises(ValueError, match="control or death-count gates"):
        materialize_frozen_encounters(source, plan, {"e2m1_nest": source}, species_profiles=profiles)


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
    manifest.options["enemy_plan"]["combat_costs"]["e1m1_intro"]["actual"] = float(sum(
        enemy_combat_cost(actor["species"], actor["attributes"]) for actor in instances))
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


@pytest.mark.parametrize("multiplier, expected", [(50, 1), (150, 3), (500, 10)])
def test_ambient_sources_emit_the_requested_total_without_adding_death_gates(monkeypatch, multiplier, expected):
    from doom_eap.content import enemy_plan as producer
    from doom_eap.content.enemy_species import load_species_registry, project_species_profiles
    from tools.maps.encounter_catalog import entity_records, list_items
    from tools.maps.encounter_materializer import materialize_frozen_encounters

    candidate = producer._composition_catalog()
    profile = candidate["maps"]["e1m1_intro"]
    profile["encounters"] = [row for row in profile["encounters"]
                             if row["operation"]["manager"]["entity"] == "cathedral_encounter_manager_upper_deck"]
    assert len(profile["encounters"]) == 2
    monkeypatch.setattr(producer, "_composition_catalog", lambda: candidate)
    options = normalize_enemy_options({"enemy_count_mode": "multiplier", "enemy_count_multiplier": multiplier})
    frozen = producer.build_enemy_plan(options, {"sequence": ["e1m1_intro"]}, seed=71414)
    plan = frozen["maps"]["e1m1_intro"]
    sources = {key: (ROOT / f"vanillamaps/{key}.map").read_bytes().decode("utf-8")
               for key in ("e1m1_intro", "e5m1_spear", "e2m2_base")}
    profiles = project_species_profiles(load_species_registry(ROOT / "content/catalog/enemy_species.json"))
    result = materialize_frozen_encounters(sources["e1m1_intro"], plan, sources, species_profiles=profiles)
    start, end = find_entity_block_bounds(result, "cathedral_encounter_manager_upper_deck")
    manager = next(entity_records(result[start:end]))
    calls = [row["eventCall"] for row in list_items(list_items(manager["edit"]["encounterComponent"]["entityEvents"])[0]["events"])]
    assert len([call for call in calls if call["eventDef"] == "staggeredAISpawn"]) == expected
    assert not any(call["eventDef"] in ("waitAIRemaining", "waitKillCount") for call in calls)
    assert calls[0]["eventDef"] == "wait"
    assert calls[-1]["eventDef"] == "waitStaggeredSpawnComplete"
    assert frozen["finite_kills"] == expected


def test_ambient_emission_rejects_a_native_combat_control_script(tmp_path, monkeypatch):
    from tools.maps.encounter_materializer import materialize_frozen_encounters
    from doom_eap.content.enemy_species import load_species_registry, project_species_profiles

    _, manifest = cold_case(tmp_path, monkeypatch)
    snapshots = {key: (ROOT / f"vanillamaps/{key}.map").read_bytes().decode("utf-8")
                 for key in ("e1m1_intro", "e5m1_spear", "e2m2_base")}
    plan = deepcopy(manifest.options["enemy_plan"]["maps"]["e1m1_intro"])
    plan["schema"] = 6
    plan["operations"][0]["barrier"] = None
    profiles = project_species_profiles(load_species_registry(ROOT / "content/catalog/enemy_species.json"))
    with pytest.raises(ValueError, match="Ambient emissions cannot alter"):
        materialize_frozen_encounters(snapshots["e1m1_intro"], plan, snapshots, species_profiles=profiles)


def test_generated_supply_lasts_through_final_reinforcement_and_finishes_without_kills():
    from doom_eap.content.enemy_options import SPECIES_IDS
    from doom_eap.content.enemy_species import load_species_registry, project_species_profiles
    from tools.maps.encounter_catalog import entity_records, list_items
    from tools.maps.encounter_materializer import materialize_frozen_encounters

    options = normalize_enemy_options({"enemy_count_mode": "multiplier", "enemy_count_multiplier": 500,
        "enemy_weights": {species: int(species == "stone_imp") for species in SPECIES_IDS}})
    plan = build_enemy_plan(options, {"sequence": ["e1m1_intro"]}, seed=71415)["maps"]["e1m1_intro"]
    operation = next(op for op in plan["operations"] if "reserve" in op)
    reserve = operation["reserve"]
    sources = {key: (ROOT / f"vanillamaps/{key}.map").read_bytes().decode("utf-8")
               for key in ("e1m1_intro", "e5m1_spear", "e2m2_base")}
    profiles = project_species_profiles(load_species_registry(ROOT / "content/catalog/enemy_species.json"))
    result = materialize_frozen_encounters(sources["e1m1_intro"], plan, sources, species_profiles=profiles)
    def calls(name):
        start, end = find_entity_block_bounds(result, name)
        record = next(entity_records(result[start:end]))
        return [row["eventCall"] for row in list_items(list_items(record["edit"]["encounterComponent"]["entityEvents"])[0]["events"])]
    combat = calls(operation["manager"]["entity"])
    finish = next(index for index, call in enumerate(combat) if call["eventDef"] == "activateTarget"
                  and list_items(call["args"])[0] == {"entity": reserve["finish_name"]})
    assert finish > max(index for index, call in enumerate(combat) if call["eventDef"] == "waitAIRemaining")
    supply = calls(reserve["manager_name"])
    assert [call["eventDef"] for call in supply] == ["staggeredAISpawn", "waitStaggeredSpawnComplete", "activateTarget",
        "maintainAICount", "waitForEventFlag", "stopMaintainingAICount", "removeAI"]
    assert list_items(supply[0]["args"])[1] == {"int": 1}
    assert list_items(supply[3]["args"])[1:3] == [{"int": 1}, {"int": 0}]
    assert list_items(supply[4]["args"])[1] == {"string": reserve["completion_flag"]}
    assert "killAI" not in {call["eventDef"] for call in supply}
