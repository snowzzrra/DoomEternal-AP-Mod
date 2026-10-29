"""Real APWorld fill, launcher packets, frozen compiler resources and runtime policy.

Set ARCHIPELAGO_SOURCE for generation. Set DOOMEAP_ROOM_RESOURCES and
IDFILE_DECOMPRESSOR for compilation. DOOMEAP_GENERATION_PROFILE=fast/full makes
missing preparation an error; select fast with ``-m 'not integration'``.
Use isolated TEMP/TMP and pytest --basetemp, and disable pytest's cache provider.
Generation runs in a child so AP's worlds and settings cannot replace test peers.
"""

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import replace
from functools import cache, partial
from pathlib import Path
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORPUS = json.loads((ROOT / "tests/fixtures/generation_cases.json").read_text(encoding="utf-8"))
VALID = CORPUS["valid_cases"]
CASES = {case["id"]: case for case in VALID + CORPUS["invalid_options"]}
COMPACT = VALID[0]["id"]
REMOTE = next(case["id"] for case in VALID if case["expected"]["remote_placements"])
ENEMIES = next(case["id"] for case in VALID if case["expected"]["enemy_descriptor"])
AUTHORING_GUARD = ContextVar("generation_acceptance_authoring_guard", default=False)


def prepared_path(name):
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"Preparation: set {name}; see this module's docstring.")
    path = Path(value).resolve()
    if not path.exists():
        pytest.fail(f"Preparation: {name} does not exist: {path}")
    return path


@pytest.fixture(scope="module", autouse=True)
def requested_profile_is_prepared():
    profile = os.environ.get("DOOMEAP_GENERATION_PROFILE", "")
    if profile not in {"", "fast", "full"}:
        pytest.fail("Preparation: DOOMEAP_GENERATION_PROFILE must be fast or full.")
    if profile or os.environ.get("DOOMEAP_ROOM_RESOURCES"):
        prepared_path("ARCHIPELAGO_SOURCE")
    if profile == "full":
        prepared_path("DOOMEAP_ROOM_RESOURCES")
        prepared_path("IDFILE_DECOMPRESSOR")


@pytest.fixture(scope="module")
def ap_source():
    if not os.environ.get("ARCHIPELAGO_SOURCE"):
        pytest.skip("Set ARCHIPELAGO_SOURCE to run real APWorld generation.")
    source = prepared_path("ARCHIPELAGO_SOURCE")
    if not (source / "worlds/doometernal/__init__.py").is_file():
        pytest.fail("Preparation: ARCHIPELAGO_SOURCE must contain the complete AP checkout, including doometernal.")
    return source


@pytest.fixture(scope="module")
def scratch(request, tmp_path_factory):
    if not request.config.getoption("basetemp"):
        pytest.fail("Preparation: supply an isolated pytest --basetemp and set TEMP/TMP to isolated scratch.")
    temporary = prepared_path("TEMP")
    if prepared_path("TMP") != temporary:
        pytest.fail("Preparation: TEMP and TMP must name the same isolated directory.")
    with tempfile.TemporaryFile(dir=temporary):
        pass
    if Path(tempfile.gettempdir()).resolve() != temporary:
        pytest.fail("Preparation: TEMP, TMP and Python's temporary directory must agree; start a fresh Python process.")
    return tmp_path_factory.mktemp("generation-acceptance")


@pytest.fixture(scope="module")
def generated(ap_source, scratch):
    return partial(generate_once, ap_source, scratch)


@cache
def generate_once(source, scratch, case_id):
    output = scratch / f"{case_id}.json"
    child_root = scratch / case_id
    child_root.mkdir()
    env = generation_environment(source, child_root)
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve()), "--generate", case_id, str(output)],
        cwd=child_root, env=env, capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    document = json.loads(output.read_text(encoding="utf-8"))
    document["generation_seconds"] = time.perf_counter() - started
    return document


def generation_environment(source, root):
    env = dict(os.environ)
    for name in ("TEMP", "APPDATA", "LOCALAPPDATA", "AP_TEST_USER"):
        path = root / name.lower()
        path.mkdir()
        env[name] = str(path)
    env["TMP"] = env["TEMP"]
    env.update({
        "PYTHONPATH": os.pathsep.join(map(str, (source, ROOT, ROOT / "packaging/standalone_runtime"))),
        "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1", "CI": "1",
        "AP_TEST_WORLDS": "doometernal", "SKIP_REQUIREMENTS_UPDATE": "1",
        "DOOM_AP_APPLICATION_DIR": str(root), "DOOM_AP_CONFIG_FILE": str(root / "unused-config.json"),
    })
    return env


def placement_mapping(location, multiworld, slot):
    item = location.item
    return {
        "location_id": location.address, "location_name": location.name,
        "item_id": item.code, "item_name": item.name,
        "recipient_slot": item.player, "recipient_name": multiworld.player_name[item.player],
        "classification": int(item.classification), "trap": item.trap, "local": item.player == slot,
    }


def generated_slot(multiworld, slot):
    world = multiworld.worlds[slot]
    locations = sorted(
        (location for location in multiworld.get_locations(slot) if location.address is not None),
        key=lambda location: location.address,
    )
    ids = [location.address for location in locations]
    return {
        "connected": {
            "team": 0, "slot": slot, "slot_data": world.fill_slot_data(),
            "missing_locations": ids[1:], "checked_locations": ids[:1],
            "placements": [placement_mapping(location, multiworld, slot) for location in locations],
        },
        "world": {
            "plan": deepcopy(world.campaign_plan), "active_ids": ids,
            "known_ids": sorted(world.location_name_to_id.values()),
            "starting_weapon": world.starting_weapon_name,
            "placements": [
                [location.address, location.name, location.item.code, location.item.name,
                 location.item.player, multiworld.player_name[location.item.player],
                 int(location.item.classification), location.item.trap, location.item.player == slot]
                for location in locations
            ],
            "battery_count": sum(
                location.item.name == "Sentinel Battery" and location.item.player == slot
                for location in multiworld.get_filled_locations()
            ),
        },
    }


def generate_document(case):
    import settings
    import Utils

    Utils.user_path.cached_path = os.environ["AP_TEST_USER"]
    settings.get_settings._cache = settings.Settings(None)
    from Fill import distribute_items_restrictive
    from NetUtils import convert_to_base_types
    from Options import OptionError
    from test.general import setup_multiworld
    from worlds.AutoWorld import call_all
    from worlds.doometernal import DoomEternalWorld

    if "error_match" in case:
        with pytest.raises(OptionError, match=case["error_match"]):
            setup_multiworld(DoomEternalWorld, seed=case["seed"], options=deepcopy(case["options"]))
        return {"rejected": True}
    multiworld = setup_multiworld(
        [DoomEternalWorld] * case["players"], seed=case["seed"], options=deepcopy(case["options"]),
    )
    before = Counter((item.player, item.name, item.code) for item in multiworld.get_items())
    distribute_items_restrictive(multiworld)
    call_all(multiworld, "post_fill")
    call_all(multiworld, "finalize_multiworld")
    assert not multiworld.get_unfilled_locations()
    after = Counter(
        (location.item.player, location.item.name, location.item.code)
        for location in multiworld.get_filled_locations()
    )
    assert before == after
    assert multiworld.can_beat_game()
    return convert_to_base_types({
        "room_info": {"seed_name": str(case["seed"])},
        "slots": [generated_slot(multiworld, slot) for slot in multiworld.player_ids],
    })


def room_snapshot(document, slot):
    from doom_eap.launcher.launcher_core import RoomSnapshot

    return RoomSnapshot.from_packets(document["room_info"], document["slots"][slot - 1]["connected"])


def manifest_from_world(snapshot, world, digest=""):
    from doom_eap.launcher.launcher_core import SeedManifest

    return SeedManifest.from_room(snapshot, set(world["known_ids"]), static_content_digest=digest)


def assert_placements(records, expected):
    actual = [
        [record.location_id, record.location_name, record.item_id, record.item_name,
         record.recipient_slot, record.recipient_name, record.classification, record.trap, record.local]
        for record in records
    ]
    assert actual == expected


def assert_generated_properties(case, slot):
    expected = case["expected"]
    data = slot["connected"]["slot_data"]
    assert data["identity"]["schema"] == 7
    assert slot["world"]["plan"]["effective_mission_count"] == expected["mission_count"]
    assert data["campaign"]["sequence"] == slot["world"]["plan"]["sequence"]
    assert data["campaign"]["mode"] == expected["mode"]
    assert data["campaign"]["goal"]["stage_id"] == expected["goal_stage"]
    assert data["recipes"]["enabled"] == expected["enemy_descriptor"]
    assert any(not row[-1] for row in slot["world"]["placements"]) == expected["remote_placements"]


def assert_starting_weapon(data, name):
    items = json.loads((ROOT / "data/item_replay_policies.json").read_text())["items"]
    assert items[str(data["starting"]["weapon_item_id"])]["name"] == name


def runtime_campaign(slot_data, world):
    from doom_eap.runtime.context_registry import validate_slot_contract
    from doom_eap.runtime.unified_campaign import UnifiedCampaign

    session = {}
    campaign = UnifiedCampaign(validate_slot_contract(slot_data), session, lambda: None)
    assert campaign.plan["sequence"] == world["plan"]["sequence"]
    assert campaign.plan["starting_stages"] == world["plan"]["starting_stages"]
    assert campaign.plan["goal_stage"] == world["plan"]["goal_stage"]
    assert session["campaign_navigation"]["generation"] == slot_data["native_generation_fingerprint"]
    return campaign


def assert_runtime_progress(campaign, world):
    plan = world["plan"]
    initial = campaign.snapshot([], [])
    assert {row["stage"] for row in initial["rows"] if row["unlocked"]} == set(plan["starting_stages"])
    completions = [stage["completion_id"] for stage in plan["stages"]]
    completed = campaign.snapshot(completions, [])
    assert {row["stage"] for row in completed["rows"] if row["completed"]} == set(plan["sequence"])
    assert campaign.goal_admitted(completions, [])


@pytest.mark.apworld
@pytest.mark.parametrize("case", VALID, ids=lambda case: case["id"])
def test_generated_packets_and_runtime_preserve_the_filled_campaign(case, generated, record_property):
    document = generated(case["id"])
    record_property("generation_seconds", document["generation_seconds"])
    for slot in document["slots"]:
        assert_generated_properties(case, slot)
        snapshot = room_snapshot(document, slot["connected"]["slot"])
        manifest = manifest_from_world(snapshot, slot["world"])
        assert list(snapshot.active_location_ids) == slot["world"]["active_ids"]
        assert_placements(manifest.placements, slot["world"]["placements"])
        assert_starting_weapon(manifest.options, slot["world"]["starting_weapon"])
        campaign = runtime_campaign(snapshot.slot_data, slot["world"])
        assert_runtime_progress(campaign, slot["world"])


@pytest.mark.apworld
def test_compact_campaign_battery_surplus_survives_packet_and_runtime_consumers(generated):
    document = generated(COMPACT)
    slot = document["slots"][0]
    expected = CASES[COMPACT]["expected"]["battery_economy"]
    assert slot["world"]["plan"]["battery_economy"] == expected
    assert slot["world"]["battery_count"] == expected["total"]
    snapshot = room_snapshot(document, 1)
    manifest = manifest_from_world(snapshot, slot["world"])
    assert manifest.options["campaign"]["fortress"]["battery_economy"] == expected
    assert snapshot.slot_data["campaign"]["fortress"]["battery_economy"] == expected


@pytest.mark.apworld
@pytest.mark.parametrize("case", CORPUS["invalid_options"], ids=lambda case: case["id"])
def test_invalid_generation_options_are_rejected_separately(case, generated):
    assert generated(case["id"])["rejected"]


def reject_authoring_open(event, args):
    if event != "open" or not AUTHORING_GUARD.get():
        return
    if not isinstance(args[0], (str, bytes, os.PathLike)):
        return
    path = Path(os.fsdecode(args[0])).resolve()
    for name in ("vanillamaps", "vanilla_decls", "gameresources"):
        if path.is_relative_to((ROOT / name).resolve()):
            raise AssertionError(f"Frozen compilation read authoring input: {path}")


@contextmanager
def without_authoring_maps():
    token = AUTHORING_GUARD.set(True)
    try:
        yield
    finally:
        AUTHORING_GUARD.reset(token)


@pytest.fixture(scope="module")
def compiler():
    if not os.environ.get("DOOMEAP_ROOM_RESOURCES"):
        pytest.skip("Set DOOMEAP_ROOM_RESOURCES to the pinned v0.7.1 bundle for real compilation.")
    resources = prepared_path("DOOMEAP_ROOM_RESOURCES")
    decompressor = prepared_path("IDFILE_DECOMPRESSOR")
    for name, expected in CORPUS["resource_sha256"].items():
        path = resources / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            pytest.fail(f"Preparation: restore the pinned {CORPUS['resource_version']} resource {name}.")
    from doom_eap.launcher.launcher_core import RoomCompiler

    sys.addaudithook(reject_authoring_open)
    return RoomCompiler(
        resources / "base_mod.zip", resources / "room_payloads.zip", resources / "room_payload_manifest.json",
        decompressor=decompressor,
    )


@pytest.fixture(scope="module")
def compiled(compiler, generated, scratch):
    return partial(compile_once, compiler, generated, scratch)


@cache
def compile_once(compiler, generated, scratch, case_id):
    document = generated(case_id)
    slot = document["slots"][0]
    with without_authoring_maps():
        snapshot = room_snapshot(document, 1)
        manifest = manifest_from_world(snapshot, slot["world"], compiler.static_content_digest)
        started = time.perf_counter()
        package = compiler.build(manifest, scratch / f"compiled-{case_id}")
    return manifest, package, time.perf_counter() - started


def assert_package_matches_world(package, slot):
    from doom_eap.launcher.launcher_core import PlacementRecord

    world = slot["world"]
    with ZipFile(package) as archive:
        manifest = json.loads(archive.read("seed_manifest.json"))
        receipt = json.loads(archive.read("seed_receipt.json"))
        assert not any(name.startswith("sources/") for name in archive.namelist())
    assert manifest["active_location_ids"] == world["active_ids"]
    assert manifest["options"]["campaign"]["sequence"] == world["plan"]["sequence"]
    assert_starting_weapon(manifest["options"], world["starting_weapon"])
    assert receipt["starting_weapon"] == world["starting_weapon"]
    assert_placements([PlacementRecord.from_mapping(row) for row in manifest["placements"]], world["placements"])
    campaign = runtime_campaign(slot["connected"]["slot_data"], world)
    assert_runtime_progress(campaign, world)
    return manifest


@pytest.mark.apworld
@pytest.mark.integration
@pytest.mark.parametrize("case", VALID, ids=lambda case: case["id"])
def test_generated_room_compiles_and_runtime_accepts_campaign(case, compiled, generated, compiler, record_property):
    manifest, package, seconds = compiled(case["id"])
    record_property("compile_seconds", seconds)
    slot = generated(case["id"])["slots"][0]
    assert_package_matches_world(package, slot)
    assert compiler.validate_cached_package(package, manifest)


@pytest.mark.apworld
@pytest.mark.integration
def test_compact_campaign_battery_surplus_survives_all_consumers(compiled, generated):
    _, package, _ = compiled(COMPACT)
    slot = generated(COMPACT)["slots"][0]
    emitted = assert_package_matches_world(package, slot)
    assert emitted["options"]["campaign"]["fortress"]["battery_economy"] == CASES[COMPACT]["expected"]["battery_economy"]


@pytest.mark.apworld
@pytest.mark.integration
def test_remote_placements_preserve_recipient(compiled, generated):
    from doom_eap.launcher.launcher_core import PlacementRecord

    _, package, _ = compiled(REMOTE)
    with ZipFile(package) as archive:
        emitted = json.loads(archive.read("seed_manifest.json"))
    expected = generated(REMOTE)["slots"][0]["world"]["placements"]
    assert any(not row[-1] for row in expected)
    assert_placements([PlacementRecord.from_mapping(row) for row in emitted["placements"]], expected)


@pytest.mark.apworld
@pytest.mark.integration
def test_generated_enemy_descriptor_compiles_without_authoring_maps(compiled, generated):
    document = generated(ENEMIES)
    descriptor = document["slots"][0]["connected"]["slot_data"]["recipes"]
    assert descriptor["enabled"]
    manifest, package, _ = compiled(ENEMIES)
    assert manifest.options["recipes"] == descriptor
    with ZipFile(package) as archive:
        receipt = json.loads(archive.read("seed_receipt.json"))
        assert receipt["native_resource_roots"]


def output_bytes(directory):
    return {path.relative_to(directory).as_posix(): path.read_bytes() for path in directory.rglob("*") if path.is_file()}


@pytest.mark.apworld
@pytest.mark.integration
@pytest.mark.parametrize("invalid", CORPUS["invalid_rooms"], ids=lambda case: case["id"])
def test_incompatible_room_is_rejected_before_output_replacement(invalid, compiled, generated, compiler):
    _, package, _ = compiled(COMPACT)
    before = output_bytes(package.parent)
    document = deepcopy(generated(COMPACT))
    slot = document["slots"][0]
    from doom_eap.content.slot_data_contract import generation_fingerprint
    for block, values in invalid.get("slot_data", {}).items():
        slot["connected"]["slot_data"][block].update(values)
    data = slot["connected"]["slot_data"]
    data["native_generation_fingerprint"] = generation_fingerprint(data)
    slot["connected"].update(invalid.get("connected", {}))
    with pytest.raises(ValueError, match=invalid["error_match"]):
        snapshot = room_snapshot(document, 1)
        manifest = manifest_from_world(snapshot, slot["world"], compiler.static_content_digest)
        compiler.build(manifest, package.parent, force=True)
    assert output_bytes(package.parent) == before


@pytest.mark.apworld
@pytest.mark.integration
def test_changed_resource_identity_cannot_replace_a_compiled_room(compiled, compiler):
    manifest, package, _ = compiled(COMPACT)
    before = output_bytes(package.parent)
    incompatible = replace(manifest, static_content_digest="incompatible")
    with pytest.raises(ValueError, match="static content identity drifted"):
        compiler.build(incompatible, package.parent, force=True)
    assert output_bytes(package.parent) == before


def test_authoring_read_guard_rejects_paths_before_the_file_is_opened():
    sys.addaudithook(reject_authoring_open)
    with without_authoring_maps(), pytest.raises(AssertionError, match="authoring input"):
        (ROOT / "vanillamaps/a3-probe.entities").read_bytes()


def test_acceptance_functions_stay_within_the_complexity_limits():
    from tools.validation.function_complexity import Functions, failures

    visitor = Functions(Path(__file__).name)
    visitor.visit(ast.parse(Path(__file__).read_text(encoding="utf-8")))
    assert not failures(visitor.functions, {})


if __name__ == "__main__":
    result = generate_document(CASES[sys.argv[2]])
    Path(sys.argv[3]).write_text(json.dumps(result), encoding="utf-8")
