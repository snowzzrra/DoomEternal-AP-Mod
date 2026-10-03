from pathlib import Path

import pytest
import yaml

from doom_eap.content.options_foundation import (
    default_option_values, dump_player_yaml, load_options_schema, load_player_yaml,
)


def test_import_edit_export_preserves_common_settings(tmp_path):
    schema = load_options_schema(Path(__file__).parents[1] / "data/options_schema.json")
    values = default_option_values(schema)
    document = yaml.safe_load(dump_player_yaml(schema, "Marine", values))
    document["description"] = "Personal settings"
    document["DOOM Eternal"].update(accessibility="minimal", start_hints=["Ballista"])
    document["DOOM Eternal"]["campaign_difficulty"] = 1
    document["DOOM Eternal"]["start_inventory"] = {"Progressive Blood Punch": 4}
    source = tmp_path / "player.yaml"
    source.write_text(yaml.safe_dump(document), encoding="utf-8")
    name, imported, original = load_player_yaml(source, schema)
    assert imported["campaign_difficulty"] == "hurt_me_plenty"
    imported["campaign_difficulty"] = "nightmare"
    exported = yaml.safe_load(dump_player_yaml(schema, name, imported, imported=original))
    assert exported["description"] == "Personal settings"
    assert exported["DOOM Eternal"]["accessibility"] == "minimal"
    assert exported["DOOM Eternal"]["start_hints"] == ["Ballista"]
    assert exported["DOOM Eternal"]["start_inventory"] == {"Progressive Blood Punch": 4}
    assert exported["DOOM Eternal"]["campaign_difficulty"] == "nightmare"
    source.write_text(yaml.safe_dump(exported), encoding="utf-8")
    assert load_player_yaml(source, schema)[1] == imported
    for invalid in ("name: Marine\nname: Slayer\n", yaml.safe_dump({
        **document, "DOOM Eternal": {**document["DOOM Eternal"], "campaign_difficulty": {"nightmare": 50}}
    }), yaml.safe_dump({**document, "DOOM Eternal": {**document["DOOM Eternal"], "custom_missions": [{}]}})):
        source.write_text(invalid, encoding="utf-8")
        with pytest.raises(ValueError):
            load_player_yaml(source, schema)
