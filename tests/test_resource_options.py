from pathlib import Path

import pytest

from doom_eap.content.options_foundation import (
    default_option_values, dump_player_yaml, load_options_schema, load_player_yaml, validate_option_values,
)


def resource_schema():
    return load_options_schema(Path(__file__).resolve().parents[1] / "data/options_schema.json")


def test_resource_dictionary_overrides_round_trip_player_yaml(tmp_path):
    schema = resource_schema()
    values = default_option_values(schema)
    values.update(item_pool_counts={"Progressive Ammo Upgrade": 3, "Air Control": 1},
                  filler_weights={"Soulsphere": 0},
                  start_inventory={"Progressive Ammo Upgrade": 1})
    trap_option = next(option for option in schema["options"] if option["key"] == "trap_weights")
    values["trap_weights"] = {trap_option["entries"][0]["key"]: 7}
    target = tmp_path / "player.yaml"
    target.write_text(dump_player_yaml(schema, "Slayer", values), encoding="utf-8")
    _, restored, _ = load_player_yaml(target, schema)
    assert restored == validate_option_values(schema, values)


@pytest.mark.parametrize("field", ["item_pool_counts", "filler_weights", "trap_weights"])
def test_resource_dictionary_rejects_unknown_bool_and_out_of_bounds(field):
    schema = resource_schema()
    option = next(option for option in schema["options"] if option["key"] == field)
    entry = option["entries"][0]
    for override in ({"unknown": 1}, {entry["key"]: True}, {entry["key"]: entry["maximum"] + 1}):
        with pytest.raises(ValueError, match="invalid override"):
            validate_option_values(schema, {**default_option_values(schema), field: override})
