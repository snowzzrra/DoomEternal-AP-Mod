import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from doom_eap.content.content_catalog import load_content_catalog
from doom_eap.content.enemy_options import normalize_enemy_options
from doom_eap.content.native_enemy_plan import build_native_recipes
from doom_eap.content.killsanity_plan import build_killsanity_plan, milestone_ids
from doom_eap.launcher.launcher_core import ModCompiler, RoomCompiler, RoomSnapshot, SeedManifest, _normalize_slot_data
from doom_eap.runtime.context_registry import validate_slot_contract
from tools.release.room_payloads import select_room_payloads


def slot_data():
    slot = json.loads((Path(__file__).parent / "fixtures/full_campaign_slot_data.json").read_text(encoding="utf-8"))
    slot["enemy_randomizer"] = True
    slot["enemies"] = normalize_enemy_options(slot)
    slot["enemy_plan"] = frozen_plan(slot["enemies"])
    return slot


def frozen_plan(enemies):
    return build_native_recipes(enemies, {"sequence": ["e1m1_intro"]}, seed="room")


@pytest.mark.parametrize("validate", [validate_slot_contract, _normalize_slot_data])
@pytest.mark.parametrize("change", ["missing", "different", "boolean_number"])
def test_room_rejects_inconsistent_frozen_enemy_configuration(validate, change):
    slot = slot_data()
    if change == "missing":
        del slot["enemies"]
    elif change == "different":
        slot["enemy_density"] = slot["enemies"]["density"] + 1
    else:
        slot["enemies"]["special_limits"]["marauder"] = True
    with pytest.raises(ValueError, match="enemies"):
        validate(slot)


def test_manifest_keeps_effective_enemy_options_for_room_compilation():
    slot = slot_data()
    slot["enemy_plan"] = frozen_plan(slot["enemies"])
    manifest = SeedManifest.from_room(RoomSnapshot("room", 0, 1, slot, (), (), ()), set())
    document = json.loads(json.dumps(manifest.document()))
    assert document["options"]["enemies"] == slot["enemies"]
    assert document["options"]["enemy_plan"] == slot["enemy_plan"]
    assert "enemy_preset" not in document["options"]


@pytest.mark.parametrize("validate", [validate_slot_contract, _normalize_slot_data])
@pytest.mark.parametrize("enabled", ["off", "enemies", "killsanity"])
@pytest.mark.parametrize("plan_state", ["absent", "null", "present"])
def test_room_requires_frozen_plan_for_requested_enemy_semantics(validate, enabled, plan_state):
    slot = slot_data()
    slot["enemy_randomizer"] = enabled == "enemies"
    slot["enemies"] = normalize_enemy_options(slot)
    slot["killsanity_options"]["enabled"] = enabled == "killsanity"
    slot["killsanity_options"]["maximum_milestones"] = 13
    slot["killsanity"] = build_killsanity_plan(slot["killsanity_options"], [{"source_id": "intro/finite",
        "map_key": "e1m1_intro", "count": 1000, "repeatable": True}])
    slot.pop("enemy_plan", None)
    if plan_state == "null":
        slot["enemy_plan"] = None
    elif plan_state == "present":
        slot["enemy_plan"] = frozen_plan(slot["enemies"])
    if enabled == "enemies" and plan_state != "present":
        with pytest.raises(ValueError, match="requires"):
            validate(slot)
    else:
        validate(slot)


@pytest.mark.parametrize("enabled", ["off", "enemies", "killsanity"])
def test_compiler_rejects_missing_enemy_plan_before_ignoring_payload(enabled):
    manifest = SimpleNamespace(options={"enemies": {"enabled": enabled == "enemies"},
                                        "killsanity": {"enabled": enabled == "killsanity"}})
    compiler = object.__new__(RoomCompiler)
    assembled = {"retained": b"unchanged"}
    if enabled != "enemies":
        compiler._apply_enemy_plan(assembled, manifest)
    else:
        with pytest.raises(ValueError, match="requires"):
            compiler._apply_enemy_plan(assembled, manifest)
    assert assembled == {"retained": b"unchanged"}


@pytest.mark.parametrize("validate", [validate_slot_contract, _normalize_slot_data])
def test_kill_rewards_are_independent_of_enemy_totals_but_validate_enemy_sources(validate):
    slot = slot_data()
    slot["enemy_plan"] = frozen_plan(slot["enemies"])
    slot["killsanity_options"]["enabled"] = True
    slot["killsanity_options"]["maximum_milestones"] = 13
    slot["killsanity"] = build_killsanity_plan(slot["killsanity_options"], [{"source_id": "intro/finite",
        "map_key": "e1m1_intro", "count": 1000, "repeatable": True}])
    validate(slot)
    slot["killsanity_options"]["maximum_milestones"] = 14
    slot["killsanity"] = build_killsanity_plan(slot["killsanity_options"], [{"source_id": "intro/finite",
        "map_key": "e1m1_intro", "count": 1000, "repeatable": True}])
    validate(slot)
    slot["enemy_plan"]["maps"]["e1m1_intro"]["source_sha256"] = "drift"
    with pytest.raises(ValueError, match="source catalog"):
        validate(slot)


@pytest.mark.parametrize("validate", [validate_slot_contract, _normalize_slot_data])
@pytest.mark.parametrize("species, qualified", [("arachnotron", True), ("gargoyle", False)])
def test_room_accepts_speed_only_for_qualified_species(validate, species, qualified):
    slot = slot_data()
    slot["enemy_randomizer"] = True
    slot["enemies"] = normalize_enemy_options(slot)
    slot["enemy_plan"] = frozen_plan(slot["enemies"])
    slot["enemy_weights"] = {name: int(name == species) for name in slot["enemies"]["weights"]}
    slot["enemy_speed"] = {"minimum": 150, "maximum": 150}
    slot["enemies"] = normalize_enemy_options(slot)
    slot["enemy_plan"] = frozen_plan(slot["enemies"])
    if qualified:
        validate(slot)
    else:
        body = next(body for row in slot["enemy_plan"]["maps"].values() for recipe in row["recipes"]
                    for body in recipe["bodies"] if body["species"] == species)
        body["attributes"]["speed_percent"] = 150
        with pytest.raises(ValueError):
            validate(slot)


@pytest.mark.parametrize("validate", [validate_slot_contract, _normalize_slot_data])
def test_room_rejects_changed_kill_reward_addresses(validate):
    slot = slot_data()
    slot["enemy_plan"] = frozen_plan(slot["enemies"])
    slot["killsanity_options"]["enabled"] = True
    slot["killsanity_options"]["maximum_milestones"] = 13
    slot["killsanity"] = build_killsanity_plan(slot["killsanity_options"], [{"source_id": "intro/finite",
        "map_key": "e1m1_intro", "count": 1000, "repeatable": True}])
    validate(slot)
    slot["killsanity"]["milestones"][0]["threshold"] = 101
    with pytest.raises(ValueError, match="Killsanity"):
        validate(slot)


def test_milestones_are_active_checks_without_placement_scouting():
    from doom_eap.runtime.location_setup import LocationSetup
    from types import SimpleNamespace

    slot = slot_data()
    slot["enemy_plan"] = frozen_plan(slot["enemies"])
    slot["killsanity_options"]["enabled"] = True
    slot["killsanity_options"]["maximum_milestones"] = 13
    slot["killsanity"] = build_killsanity_plan(slot["killsanity_options"], [{"source_id": "intro/finite",
        "map_key": "e1m1_intro", "count": 1000, "repeatable": True}])
    milestones = milestone_ids(slot["killsanity"])
    ordinary = 7770001
    connected = {"slot": 1, "team": 0, "slot_data": slot,
                 "missing_locations": [ordinary, *milestones], "checked_locations": [],
                 "placements": [{"location_id": ordinary, "location_name": "ordinary",
                                 "item_id": 7770900, "item_name": "test item", "recipient_slot": 1,
                                 "recipient_name": "Player", "classification": 1, "trap": False, "local": True}]}
    setup = LocationSetup(SimpleNamespace(error=lambda *args: None))
    assert setup.begin(connected, {ordinary}, milestones) == {ordinary}
    assert "unknown" in setup.consume({"locations": [[7770900, min(milestones), 1, 1]]}, set(milestones))
    assert setup.consume({"locations": [[7770900, ordinary, 1, 1]]}, {ordinary}) is None
    snapshot = RoomSnapshot.from_packets({"seed_name": "room"}, connected)
    manifest = SeedManifest.from_room(snapshot, {ordinary})
    assert set(manifest.active_location_ids) == {ordinary, *milestones}
    assert len(manifest.require_complete_placements()) == 1
    assert manifest.options["killsanity"] == slot["killsanity"]
    connected["missing_locations"].append(123456)
    with pytest.raises(ValueError, match="unknown"):
        setup.begin(connected, {ordinary}, milestones)


@pytest.mark.parametrize("include_missions", [False, True])
def test_dlc_mission_payloads_are_independent_of_dlc_bestiary(tmp_path, include_missions):
    slot = slot_data()
    slot.update(use_dlc_content=False, include_dlc_missions=include_missions)
    slot["enemies"] = normalize_enemy_options(slot)
    slot["enemy_plan"] = frozen_plan(slot["enemies"])
    assert _normalize_slot_data(slot)["include_dlc_missions"] is include_missions
    compiler = ModCompiler()
    manifest = SeedManifest.create(seed_name="room", team=0, slot=1, options=slot,
                                   active_location_ids=[], static_precompile=True)
    dlc_maps = {spec.key for spec in load_content_catalog().compiler_maps() if spec.requires_dlc_content}
    assert dlc_maps
    for map_key in dlc_maps:
        if include_missions:
            assert compiler.project_map_config(manifest, map_key)["map_key"] == map_key
        else:
            with pytest.raises(ValueError, match="unsupported"):
                compiler.project_map_config(manifest, map_key)
    payload = {"maps": {
        key: {"option_keys": [], "states": [{"options": {}, "source": "base"}]}
        for key in {"e1m1_intro", *dlc_maps}
    }}
    selected = select_room_payloads(payload, slot)
    assert "e1m1_intro" in selected
    assert bool(dlc_maps & selected.keys()) is include_missions
