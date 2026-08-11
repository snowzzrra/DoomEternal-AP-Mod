import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from launcher_core import RoomSnapshot, SeedManifest
from options_foundation import (
    default_option_values,
    dump_player_yaml,
    load_options_schema,
    player_yaml_document,
    save_player_yaml,
    validate_option_values,
)
ROOT = Path(__file__).resolve().parents[1]
AP_ROOT = ROOT.parent / "Archipelago"
SCHEMA_PATH = ROOT / "data/options_schema.json"
AP_PYTHON = AP_ROOT / ".venv/bin/python"


@pytest.fixture
def schema():
    return load_options_schema(SCHEMA_PATH)


def test_committed_projection_is_deterministic_and_matches_apworld():
    if not (AP_ROOT / "worlds/doometernal/options.py").is_file() or not AP_PYTHON.is_file():
        pytest.skip("sibling Archipelago checkout unavailable")
    command = [
        str(AP_PYTHON),
        str(ROOT / "tools/content/compile_options_schema.py"),
        "--archipelago-root",
        str(AP_ROOT),
        "--check",
    ]
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "SKIP_REQUIREMENTS_UPDATE": "1",
    }
    first = subprocess.run(
        command, cwd=ROOT, env=environment, check=True, capture_output=True, text=True
    )
    second = subprocess.run(
        command, cwd=ROOT, env=environment, check=True, capture_output=True, text=True
    )

    assert first.stdout == second.stdout == "options schema: up-to-date\n"


def test_supported_current_options_map_to_expected_ui_types(schema):
    types = {option["key"]: option["ui_type"] for option in schema["options"]}

    assert types == {
        "progression_balancing": "range",
        "accessibility": "choice",
        "death_link": "toggle",
        "randomize_chainsaw": "toggle",
        "randomize_dash": "toggle",
        "randomize_first_battery": "toggle",
        "starting_weapon": "choice",
    }
    assert "start_inventory" in {entry["key"] for entry in schema["excluded_options"]}


def test_defaults_and_choice_values_are_canonical(schema):
    defaults = default_option_values(schema)
    options = {option["key"]: option for option in schema["options"]}

    assert defaults["progression_balancing"] == 50
    assert defaults["accessibility"] == "full"
    assert defaults["death_link"] is False
    assert [choice["key"] for choice in options["accessibility"]["choices"]] == [
        "full",
        "minimal",
    ]


def test_range_validation_rejects_out_of_bounds(schema):
    values = default_option_values(schema)
    values["progression_balancing"] = 100

    with pytest.raises(ValueError, match="between 0 and 99"):
        validate_option_values(schema, values)


@pytest.mark.parametrize("dash", [False, True])
def test_randomize_dash_yaml_uses_canonical_boolean(schema, dash):
    values = default_option_values(schema)
    values["randomize_dash"] = dash
    text = dump_player_yaml(schema, "DoomSlayer", values)
    document = yaml.safe_load(text)

    assert document["game"] == "DOOM Eternal"
    assert document["DOOM Eternal"]["randomize_dash"] is dash
    assert "starting_inventory" not in text
    assert document["DOOM Eternal"]["starting_weapon"] == "combat_shotgun"


@pytest.mark.parametrize("dash", [False, True])
def test_generated_yaml_is_accepted_by_current_ap_parser(schema, dash, tmp_path):
    if not (AP_ROOT / "Generate.py").is_file() or not AP_PYTHON.is_file():
        pytest.skip("sibling Archipelago checkout unavailable")
    values = default_option_values(schema)
    values["randomize_dash"] = dash
    yaml_path = tmp_path / "DoomSlayer.yaml"
    yaml_path.write_text(dump_player_yaml(schema, "DoomSlayer", values), encoding="utf-8")
    script = (
        "from Generate import read_weights_yamls, roll_settings; "
        f"parsed=roll_settings(read_weights_yamls({str(yaml_path)!r})[0]); "
        f"assert bool(parsed.randomize_dash.value) is {dash!r}; "
        "assert parsed.accessibility.current_key == 'full'"
    )
    subprocess.run(
        [str(AP_PYTHON), "-c", script],
        cwd=AP_ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(AP_ROOT),
            "SKIP_REQUIREMENTS_UPDATE": "1",
        },
        check=True,
        capture_output=True,
        text=True,
    )


def test_saving_future_yaml_does_not_mutate_connected_room_authority(schema, tmp_path):
    snapshot = RoomSnapshot.from_packets(
        {"seed_name": "current-room"},
        {
            "team": 1,
            "slot": 2,
            "slot_data": {
                "randomize_chainsaw": False,
                "randomize_dash": False,
                "randomize_first_battery": False,
            },
            "missing_locations": [],
            "checked_locations": [],
        },
    )
    manifest = SeedManifest.create(
        seed_name="current-room",
        team=1,
        slot=2,
        options={
            "randomize_chainsaw": False,
            "randomize_dash": False,
            "randomize_first_battery": False,
        },
        active_location_ids=[],
    )
    original_slot_data = dict(snapshot.slot_data)
    original_manifest = manifest.document()
    values = default_option_values(schema)
    values["randomize_dash"] = True

    path = save_player_yaml(tmp_path / "future.yaml", schema, "FuturePlayer", values)

    assert yaml.safe_load(path.read_text(encoding="utf-8"))["DOOM Eternal"]["randomize_dash"] is True
    assert snapshot.slot_data == original_slot_data
    assert manifest.document() == original_manifest


def test_yaml_dump_is_deterministic(schema):
    values = default_option_values(schema)
    assert dump_player_yaml(schema, "Player", values) == dump_player_yaml(
        schema, "Player", values
    )
