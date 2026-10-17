import copy
import hashlib
import json
from pathlib import Path
import re
import struct
import importlib
import sys
from unittest.mock import patch
from zipfile import ZipFile

import pytest

from doom_eap.content.enemy_species import (
    enemy_speed_capabilities, load_species_registry, project_species_profiles, project_speed_capabilities,
)
from tools.maps.encounter_catalog import entity_records, list_items, parse_object, walk_values


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def registry():
    return load_species_registry(ROOT / "content/catalog/enemy_species.json")


@pytest.fixture(scope="module")
def authored(registry):
    wanted = {}
    for entry in registry["species"].values():
        for template in entry.get("templates", []) + entry.get("candidates", []):
            names = wanted.setdefault(template["map_key"], set())
            names.add(template["entity"])
            if "proof" in template:
                names.add(template["proof"]["manager"])
                if template["proof"]["kind"] == "spawnSingleAI":
                    names.add(template["proof"]["spawner"])
    result = {}
    for map_key, names in wanted.items():
        source = registry["sources"][map_key]
        data = (ROOT / source["path"]).read_bytes()
        descriptor = json.loads((ROOT / "content/maps" / map_key / "descriptor.json").read_bytes().decode("utf-8"))
        assert hashlib.sha256(data).hexdigest() == source["sha256"] == descriptor["source_sha256"]
        assert source["path"] == "vanillamaps/" + descriptor["source_file"]
        records = {record["name"]: record for record in entity_records(data.decode("utf-8"), include_names=names)}
        assert records.keys() == names
        result[map_key] = records
    return result


@pytest.fixture(scope="module")
def populations(registry):
    result = {}
    for name, source in registry["actorpopulation_sources"].items():
        variants = []
        for variant in source["variants"]:
            data = (ROOT / variant["path"]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == variant["sha256"]
            assert variant["resource_name"] == "generated/decls/actorpopulation/" + name + ".decl"
            value = parse_object(data.decode("utf-8").strip()[1:-1])
            variants.append(value["edit"]["aiTypeDefAssignments"])
        assert variants
        result[name] = variants
    return result


def test_every_identity_matches_its_enum_binding_and_template(registry, authored, populations):
    for entry in registry["species"].values():
        for template in entry["templates"]:
            records = authored[template["map_key"]]
            actor = records[template["entity"]]
            assert actor["class"] == "idAI2"
            assert actor["inherit"] == template["inherit"]
            assert actor["sha256"] == template["sha256"]
            proof = template["proof"]
            manager = records[proof["manager"]]
            assert manager["class"] == "idEncounterManager"
            assert manager["sha256"] == proof["manager_sha256"]
            if proof["kind"] == "actorpopulation_default":
                assert dict(walk_values(manager["edit"]))[proof["assignment_field"]] == proof["assignment"]
                conversion = registry["population_encounter_conversion"]["values"][proof["population_type"]]
                assert conversion["encounter_spawn_type"] == proof["spawn_type"] == entry["spawn_type"]
                assert conversion["population_value"] == proof["population_value"]
                assert conversion["encounter_value"] == proof["spawn_value"]
                assert struct.unpack_from("<Q", bytes.fromhex(conversion["population_record_bytes"]), 8)[0] == proof["population_value"]
                assert struct.unpack_from("<Q", bytes.fromhex(conversion["encounter_record_bytes"]), 8)[0] == proof["spawn_value"]
                for assignments in populations[proof["assignment"]]:
                    selected = [value for key, value in assignments.items()
                                if re.fullmatch(r"item\[\d+\]", key) and value.get("aiType") == proof["population_type"]]
                    assert len(selected) == 1 and selected[0]["entityDef"] == actor["inherit"]
                continue
            scripts = list_items(manager["edit"]["encounterComponent"]["entityEvents"])
            event = list_items(scripts[proof["script_index"]]["events"])[proof["event_index"]]["eventCall"]
            assert event["eventDef"] == proof["kind"] == "spawnSingleAI"
            args = list_items(event["args"])
            assert args[0]["eEncounterSpawnType_t"] == proof["spawn_type"]
            assert proof["spawn_type"] in entry["source_spawn_types"]
            assert args[1]["entity"] == proof["spawner"]
            spawner = records[proof["spawner"]]
            assert spawner["class"] == "idTarget_Spawn"
            assert spawner["sha256"] == proof["spawner_sha256"]
            assert list_items(spawner["edit"]["entityDefs"]) == [{"name": template["entity"]}]


def test_gaps_remain_unavailable_and_candidates_keep_source_identity(registry, authored):
    profiles = project_species_profiles(registry)
    for species, entry in registry["species"].items():
        if entry["status"] == "gap":
            assert species not in profiles and entry["gaps"]
            for candidate in entry["candidates"]:
                actor = authored[candidate["map_key"]][candidate["entity"]]
                assert actor["sha256"] == candidate["sha256"]
                assert actor["inherit"] == candidate["inherit"] and actor["class"] == "idAI2"


def test_projection_does_not_grant_runtime_eligibility_or_share_mutable_data(registry):
    modified = copy.deepcopy(registry)
    modified["species"]["imp"]["resources"] = {"qualified": True}
    profiles = project_species_profiles(modified)
    assert set(profiles["imp"]) == {"spawn_type", "spawn_types", "inherits"}
    assert profiles["imp"]["inherits"] == tuple(registry["species"]["imp"]["inherits"])
    profiles["imp"]["spawn_type"] = "unrelated"
    assert modified["species"]["imp"]["spawn_type"] == registry["species"]["imp"]["spawn_type"]


def test_apworld_speed_projection_matches_sampling_and_rejects_unqualified_plans(tmp_path, monkeypatch):
    from doom_eap.content.enemy_options import SPECIES_IDS, normalize_enemy_options
    from doom_eap.content.enemy_plan import sample_instance_attributes

    projected_root = ROOT.parent / "Archipelago/worlds/doometernal"
    archive = tmp_path / "speed_projection.zip"
    with ZipFile(archive, "w") as bundle:
        bundle.writestr("speed_projection/__init__.py", "")
        for name in ("enemy_options.py", "enemy_species.py", "enemy_plan.py", "enemy_species.json"):
            bundle.write(projected_root / name, f"speed_projection/{name}")
    monkeypatch.syspath_prepend(str(archive))
    with patch.dict(sys.modules):
        modules = {name: importlib.import_module(f"speed_projection.{name}")
                   for name in ("enemy_options", "enemy_species", "enemy_plan")}
        assert modules["enemy_species"].enemy_speed_capabilities() == enemy_speed_capabilities()
        enemies = normalize_enemy_options({"enemy_speed": {"minimum": 150, "maximum": 150}})
        for species in SPECIES_IDS:
            kwargs = dict(seed=42, instance_key="actor", stage_index=0, stage_count=1)
            assert modules["enemy_plan"].sample_instance_attributes(enemies, species, **kwargs) == (
                sample_instance_attributes(enemies, species, **kwargs))
            if enemy_speed_capabilities()[species] == "qualified":
                modules["enemy_species"].validate_enemy_speed_percent(species, 150)
            else:
                with pytest.raises(ValueError, match="Speed changes are not qualified"):
                    modules["enemy_species"].validate_enemy_speed_percent(species, 150)


def test_speed_qualification_needs_separate_evidence_from_identity(registry):
    modified = copy.deepcopy(registry)
    entry = modified["species"]["mancubus"]
    assert project_speed_capabilities(modified)["mancubus"] == "unqualified"
    entry["speed"] = {"status": "qualified"}
    with pytest.raises(ValueError, match="needs evidence"):
        project_speed_capabilities(modified)
    entry["speed"]["evidence"] = {"fixture": "synthetic qualified Speed consumer"}
    assert project_speed_capabilities(modified)["mancubus"] == "qualified"
    assert project_species_profiles(modified) == project_species_profiles(registry)


@pytest.mark.parametrize("fault", ["species", "inherit", "mask", "preferred"])
def test_unproved_identity_expansion_is_refused(registry, fault):
    modified = copy.deepcopy(registry)
    entry = modified["species"]["imp"]
    if fault == "species":
        modified["species"].pop("stone_imp")
    elif fault == "inherit":
        entry["inherits"].append("ai/unqualified")
    elif fault == "mask":
        entry["source_spawn_types"].append("ENCOUNTER_SPAWN_UNQUALIFIED")
    else:
        entry["templates"][0]["role"] = "alias"
    with pytest.raises(ValueError):
        project_species_profiles(modified)


def test_native_masks_preserve_aliases_and_tracking_requires_selected_slots():
    from doom_eap.content.enemy_species import enemy_spawn_mask, enemy_tracking_suffix
    assert enemy_tracking_suffix("ENCOUNTER_SPAWN_IMP", "ENCOUNTER_SPAWN_STONE_IMP") == "_t00000000000000100000000000000020"
    assert enemy_spawn_mask("ENCOUNTER_SPAWN_IMP | ENCOUNTER_SPAWN_STONE_IMP") == 48
    with pytest.raises(ValueError):
        enemy_tracking_suffix("ENCOUNTER_SPAWN_IMP | ENCOUNTER_SPAWN_STONE_IMP", "ENCOUNTER_SPAWN_STONE_IMP")
    aliases = {"ENCOUNTER_DO_NOT_USE_MAX_COMMON": 1 << 13,
               "ENCOUNTER_DO_NOT_USE_MAX_HEAVY": 1 << 25,
               "ENCOUNTER_DO_NOT_USE_MAX_SUPER": 1 << 31,
               "ENCOUNTER_DO_NOT_USE_AMBIENT": 1 << 35}
    for alias, value in aliases.items():
        assert enemy_spawn_mask("ENCOUNTER_SPAWN_IMP " + alias) == 16 | value
        assert enemy_tracking_suffix(alias, "ENCOUNTER_SPAWN_STONE_IMP") == f"_t{value:016x}{32:016x}"
    assert enemy_tracking_suffix("ENCOUNTER_SPAWN_WHIPLASH ENCOUNTER_DO_NOT_USE_MAX_HEAVY", "ENCOUNTER_SPAWN_IMP") == "_t00000000020000000000000000000010"
    for invalid in (None, "", "ENCOUNTER_SPAWN_ANY", "unknown"):
        with pytest.raises(ValueError):
            enemy_tracking_suffix(invalid, "ENCOUNTER_SPAWN_IMP")
    with pytest.raises(ValueError):
        enemy_tracking_suffix("ENCOUNTER_SPAWN_IMP", "ENCOUNTER_SPAWN_IMP | ENCOUNTER_SPAWN_STONE_IMP")
