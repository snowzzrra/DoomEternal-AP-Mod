import json
import subprocess
import time
from zipfile import ZipFile

import pytest

from doom_eap.content.arena_checks import arena_check_name, load_arena_checks
from doom_eap.content.content_catalog import load_content_catalog
from doom_eap.content.master_levels import load_master_levels
from doom_eap.runtime.observers import validate_slot_contract
from tests.generation_support import (
    ROOT,
    VALID,
)
from tools.maps.ap_map_generator import entity_definition_index, extract_target_names, find_entity_block_bounds


def entity(text, index, name):
    bounds = find_entity_block_bounds(text, name, index=index)
    assert bounds is not None, name
    return text[bounds[0]:bounds[1] + 1]


def samples(manifest):
    active = set(manifest.active_location_ids)
    pickup = next(row for row in load_content_catalog().physical_locations
                  if row.location_id in active and not row.ap_check.startswith("AP_CHECK_ML_"))
    checks = [(pickup.map_key, pickup.location_id, pickup.ap_check,
               "ap_independent_" + pickup.ap_check.removeprefix("AP_CHECK_").lower(), None)]
    arenas = [(key, row) for key, spec in load_arena_checks().items() for row in spec["arenas"]
              if row["location_id"] in active]
    if arenas:
        key, arena = arenas[0]
        checks.append((key.removeprefix("ml_"), arena["location_id"], arena_check_name(arena), None, None))
    masters = load_master_levels(ROOT)
    if manifest.options["campaign"]["master_level_stage_ids"]:
        stage = masters["ml_e2m1_nest"]
        master = stage["pickups"][0]
        checks.append((stage["map_key"], master["location_id"], master["ap_check"],
                       f"ap_independent_ml_{master['location_id']}", stage["layers"][0]))
        master_arena = load_arena_checks()["ml_e2m1_nest"]["arenas"][0]
        assert master_arena["location_id"] in active
        checks.append((stage["map_key"], master_arena["location_id"], arena_check_name(master_arena),
                       None, stage["layers"][0]))
    return checks, arenas


def assert_chain(text, index, code, relay_name, trigger_name, layer):
    relay = entity(text, index, relay_name)
    assert {f"ap_notify_location_{code}", f"ap_event_{code}"} <= set(extract_target_names(relay))
    notification = entity(text, index, f"ap_notify_location_{code}")
    assert f"#str_ap_location_{code}" in notification
    event = entity(text, index, f"ap_event_{code}")
    assert f"echo AP_CHECK_EVENT_{code}; condump ap_event_{code}.txt;" in event
    if trigger_name is not None:
        trigger = entity(text, index, trigger_name)
        visual = entity(text, index, f"ap_location_visual_{code}")
        assert relay_name in extract_target_names(trigger)
        assert ('class = "idTrigger";' in trigger, 'model = "art/pickups/codex.lwo";' in visual) == (True, True)
        if layer is not None:
            assert (layer in trigger, layer in visual) == (True, True)


def assert_arena_source(text, index, arena, layer):
    for completion in arena["completion"]:
        owner = completion.get("manager", completion.get("target"))
        assert arena_check_name(arena) in entity(text, index, owner)
    if layer is not None:
        assert layer in entity(text, index, arena_check_name(arena))


@pytest.mark.parametrize("case", VALID, ids=lambda case: case["id"])
def test_room_generation_and_compile(case, generated, compiled_room, compiler, tmp_path, record_property):
    started = time.perf_counter()
    document = generated(case["id"])
    slot = document["slots"][0]
    validate_slot_contract(slot["connected"]["slot_data"])
    manifest, package, _ = compiled_room(case["id"])
    checks, arenas = samples(manifest)
    arenas_by_id = {row["location_id"]: row for _, row in arenas}

    with ZipFile(package) as archive:
        emitted = json.loads(archive.read("seed_manifest.json"))
        assert emitted["active_location_ids"] == slot["world"]["active_ids"]
        assert emitted["options"]["campaign"] == slot["connected"]["slot_data"]["campaign"]
        for map_key, code, relay_name, trigger_name, layer in checks:
            member = compiler.payload_manifest["maps"][map_key]["target_member"]
            compressed = tmp_path / f"{code}.entities"
            decoded = tmp_path / f"{code}.txt"
            compressed.write_bytes(archive.read(member))
            result = subprocess.run([str(compiler.decompressor), "--decompress", str(compressed), str(decoded)],
                                    capture_output=True, timeout=60)
            assert result.returncode == 0, result.stderr.decode(errors="replace")
            text = decoded.read_text(encoding="utf-8")
            index = entity_definition_index(text)
            assert_chain(text, index, code, relay_name, trigger_name, layer)
            if trigger_name is None:
                assert_arena_source(text, index, arenas_by_id[code], layer)
    assert compiler.validate_cached_package(package, manifest)
    record_property("pipeline_seconds", time.perf_counter() - started)
