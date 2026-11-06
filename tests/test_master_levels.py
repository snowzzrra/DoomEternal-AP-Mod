import json
import struct
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from doom_eap.content.arena_checks import ROOT, arena_check_name, load_arena_checks
from doom_eap.content.content_catalog import load_content_catalog
from doom_eap.content.master_levels import load_master_levels
from doom_eap.contracts.challenge_registry import load_challenge_registry
from doom_eap.contracts.check_observation import CheckObservation
from doom_eap.contracts.publisher_contracts import map_publishers_for_owner
from doom_eap.runtime import sentinel_protocol_generated as codec
from doom_eap.runtime.mission_presentation import MissionPresentations
from doom_eap.runtime.unified_campaign import UnifiedCampaign
from tools.maps.ap_map_generator import (
    _spawn_position_from_block,
    find_entity_block_bounds,
    generate_independent_pickup_trigger,
    generate_map,
)
from tools.maps.mission_complete_map_patcher import _patch_declared_terminal_text


@pytest.mark.parametrize("mode", ["Vanilla Order", "Random Mission Order", "Mission Access as Items"])
@pytest.mark.parametrize("physical,access,completion", [
    ("e2m1_nest", 7776001, 7772100), ("e1m3_cult", 7776004, 7772130),
    ("e2m2_base", 7776005, 7772230), ("e3m1_slayer", 7776006, 7772330),
])
def test_master_navigation_and_completions_are_independent_on_the_same_map(mode, physical, access, completion):
    stage = "ml_" + physical
    runtime = UnifiedCampaign.__new__(UnifiedCampaign)
    runtime.plan = {"mode": mode, "sequence": [physical, "e3m4_boss"],
                    "master_level_stage_ids": [stage], "starting_stages": [physical],
                    "access_items": {str(access): stage}, "goal_stage": None,
                    "fortress_phase_thresholds": [1, 2, 3], "hub": {"map": "game/hub/hub"}}
    runtime.navigation, runtime.persist = {"selected": "hub"}, Mock()
    locked = runtime.snapshot(set(), [])
    assert not locked["rows"][-1]["unlocked"]
    snapshot = runtime.snapshot({completion}, [SimpleNamespace(item=access)])
    normal, master = (next(row for row in snapshot["rows"] if row["stage"] == key)
                      for key in (physical, stage))
    assert normal["map"] == master["map"] and normal["variant"] != master["variant"]
    assert master["completed"] and not normal["completed"] and snapshot["fortress_phase"] == 0
    runtime.select(master["stage"], snapshot)
    assert runtime.navigation["selected"] == stage


def test_master_compiler_keeps_checks_and_terminal_separate_from_normal_mission(tmp_path):
    output, manifest = tmp_path / "map.entities", tmp_path / "manifest.json"
    generate_map(ROOT / "vanillamaps/e3m1_slayer.map", output,
                 ROOT / "content/maps/e3m1_slayer/locations.json", manifest, {}, enable_notifications=False)
    text = output.read_text(encoding="utf-8")
    stage = load_master_levels(ROOT)["ml_e3m1_slayer"]
    _assert_pickup_layers(text, stage, json.loads(manifest.read_text()))
    _assert_arena_layers(text, stage)
    _assert_hidden_pickups(text, stage)
    catalog = load_content_catalog(ROOT)
    publishers = map_publishers_for_owner(catalog.publishers, stage["map_key"], stage["completion_owner"])
    patched, audit = _patch_declared_terminal_text(stage["map_key"], stage["completion_owner"], publishers, text)
    for publisher in publishers:
        relay = audit["publishers"][publisher.key]["relay"]
        trigger, = publisher.triggers_for("map_event_file")
        assert trigger["layer"] in patched[slice(*find_entity_block_bounds(patched, relay))]


def test_pickup_trigger_accepts_default_zero_coordinates():
    source = 'entity { edit = { spawnPosition = { y = 61; z = 1.95; } } }'
    trigger = generate_independent_pickup_trigger("soulsphere", "AP_CHECK_SOULSPHERE", source)
    assert _spawn_position_from_block(trigger) == (0, 61, 1.95)
    with pytest.raises(ValueError, match="requires spawnPosition"):
        generate_independent_pickup_trigger("soulsphere", "AP_CHECK_SOULSPHERE", "entity {}")
    with pytest.raises(ValueError, match="requires spawnPosition"):
        generate_independent_pickup_trigger("soulsphere", "AP_CHECK_SOULSPHERE", source.replace("y = 61", "x = invalid; y = 61"))


def _assert_pickup_layers(text, stage, manifest):
    for pickup in stage["pickups"]:
        code = pickup["location_id"]
        assert manifest[pickup["ap_check"]] == code
        for name in (f"ap_independent_ml_{code}", f"ap_location_visual_{code}"):
            assert stage["layers"][0] in text[slice(*find_entity_block_bounds(text, name))]


def _assert_hidden_pickups(text, stage):
    from tools.maps.ap_map_generator import extract_target_names

    for pickup in stage["pickups"]:
        if "reveal_entity" not in pickup:
            continue
        code, reveal = pickup["location_id"], pickup["reveal_entity"]
        relay = text[slice(*find_entity_block_bounds(text, reveal))]
        assert extract_target_names(relay) == [reveal + "_ap_native_reveal", f"ap_independent_ml_{code}"]
        native = text[slice(*find_entity_block_bounds(text, reveal + "_ap_native_reveal"))]
        assert f'"ap_location_visual_{code}"' in native
        trigger = text[slice(*find_entity_block_bounds(text, f"ap_independent_ml_{code}"))]
        assert "triggerFirst = true;" in trigger


def _assert_arena_layers(text, stage):
    catalog = load_arena_checks()
    for key, layer in ((stage["map_key"], stage["normal_layer"]), ("ml_" + stage["map_key"], stage["layers"][0])):
        for arena in catalog[key]["arenas"]:
            assert layer in text[slice(*find_entity_block_bounds(text, arena_check_name(arena)))]


def test_master_menu_totals_and_challenges_do_not_mix_with_normal_mission():
    registry = load_challenge_registry()
    stages = load_master_levels(ROOT)
    masters = {entry["signal"]["stage_id"]: entry for entry in registry["mission_complete"]
               if "stage_id" in entry["signal"]}
    catalog = load_content_catalog(ROOT)
    assert set(masters) == set(stages)
    assert all(entry["signal"]["runtime_map"] == catalog.maps[stages[key]["map_key"]].runtime_map
               for key, entry in masters.items())
    model = MissionPresentations(ROOT, ())
    all_locations = frozenset(int(code) for code in json.loads((ROOT / "data/location_names.json").read_text())["locations"])
    facts = CheckObservation(frozenset(), frozenset(), all_locations, True, True)
    normal = model.snapshot("room", "generation", "e2m1_nest", revealed=True, facts=facts, pending=set())
    master = model.snapshot("room", "generation", "ml_e2m1_nest", revealed=True, facts=facts, pending=set())
    assert normal.runtime_map == master.runtime_map and master.total == 16
    assert model.pickups["e2m1_nest"].isdisjoint(model.pickups["ml_e2m1_nest"])
    assert model.encounter_snapshot("e5m1_spear", revealed=True, facts=facts, placements={}, pending=set())
    assert not model.encounter_snapshot("ml_e5m1_spear", revealed=True, facts=facts, placements={}, pending=set())


def test_arc_master_shell_terminal_publishes_completion_and_returns_to_fortress():
    from tools.maps.unified_campaign import project_stage_return

    stage = load_master_levels(ROOT)["ml_e2m2_base"]
    catalog = load_content_catalog(ROOT)
    source = (ROOT / "vanillamaps/e2m2_base.map").read_text(encoding="utf-8")
    publishers = map_publishers_for_owner(catalog.publishers, stage["map_key"], stage["completion_owner"])
    publisher, = publishers
    assert publisher.effects[0]["location_id"] == stage["completion_id"]
    patched, audit = _patch_declared_terminal_text(stage["map_key"], stage["completion_owner"], publishers, source)
    projected, changed = project_stage_return(catalog, stage["map_key"], patched)
    native = stage["completion_owner"] + "_ap_native_transition"
    assert native in changed
    assert 'nextMapName = "maps/game/hub/hub.map";' in projected[slice(*find_entity_block_bounds(projected, native))]
    relay = audit["publishers"][publisher.key]["relay"]
    assert stage["layers"][0] in projected[slice(*find_entity_block_bounds(projected, relay))]


def test_campaign_wire_carries_twenty_missions_and_six_master_levels():
    row = codec.decode("sc_campaign_row", struct.pack("<III192s96s", 7776006, 3, 33, b"game/sp/e3m1_slayer/e3m1_slayer", b"MASTER LEVEL"))
    payload = {"revision": 1, "index": 26, "count": 27, "row": row,
               "summary": codec.decode("sc_campaign_summary", bytes(292)), "rewards_known": 1, "rewards": []}
    wire = codec.encode_variant(22, 5, 0, payload)
    assert codec.decode_variant(22, 5, wire)[1] == payload
    with pytest.raises(ValueError):
        codec.encode_variant(22, 4, 0, payload)
    commit = {"revision": 1, "count": 27, "selected_id": 7776006}
    assert codec.decode_variant(23, 5, codec.encode_variant(23, 5, 0, commit))[1] == commit
