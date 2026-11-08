"""Set ARCHIPELAGO_SOURCE for generation; compilation also needs DOOMEAP_ROOM_RESOURCES
and IDFILE_DECOMPRESSOR. Use isolated TEMP/TMP and pytest --basetemp.
"""

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
from functools import cache, partial
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORPUS = json.loads((ROOT / "tests/fixtures/generation_cases.json").read_text(encoding="utf-8"))
VALID = CORPUS["valid_cases"]
CASES = {case["id"]: case for case in VALID + CORPUS["invalid_options"]}
AUTHORING_GUARD = ContextVar("generation_acceptance_authoring_guard", default=False)


def prepared_path(name):
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"Preparation: set {name}; see this module's docstring.")
    path = Path(value).resolve()
    if not path.exists():
        pytest.fail(f"Preparation: {name} does not exist: {path}")
    return path


@pytest.fixture(scope="session")
def ap_source():
    if not os.environ.get("ARCHIPELAGO_SOURCE"):
        pytest.fail("Set ARCHIPELAGO_SOURCE to run real APWorld generation.")
    source = prepared_path("ARCHIPELAGO_SOURCE")
    if not (source / "worlds/doometernal/__init__.py").is_file():
        pytest.fail("Preparation: ARCHIPELAGO_SOURCE must contain the complete AP checkout, including doometernal.")
    return source


@pytest.fixture(scope="session")
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


@pytest.fixture(scope="session")
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
    settings.skip_autosave = True
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


def runtime_campaign(slot_data, world):
    from doom_eap.runtime.campaign import UnifiedCampaign
    from doom_eap.runtime.observers import validate_slot_contract

    session = {}
    campaign = UnifiedCampaign(validate_slot_contract(slot_data), session, lambda: None)
    assert campaign.plan["sequence"] == world["plan"]["sequence"]
    assert campaign.plan["starting_stages"] == world["plan"]["starting_stages"]
    assert campaign.plan["goal_stage"] == world["plan"]["goal_stage"]
    assert session["campaign_navigation"]["generation"] == slot_data["native_generation_fingerprint"]
    return campaign


def reject_authoring_open(event, args):
    if event != "open" or not AUTHORING_GUARD.get():
        return
    if not isinstance(args[0], str | bytes | os.PathLike):
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


@pytest.fixture(scope="session")
def compiler():
    if not os.environ.get("DOOMEAP_ROOM_RESOURCES"):
        pytest.fail("Set DOOMEAP_ROOM_RESOURCES to packaging/room_resources/v0.8.0.")
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


if __name__ == "__main__":
    result = generate_document(CASES[sys.argv[2]])
    Path(sys.argv[3]).write_text(json.dumps(result), encoding="utf-8")
