"""Produce player-editable presets through the public options schema."""
from pathlib import Path
import argparse
import json

from doom_eap.content.options_foundation import default_option_values, load_options_schema, save_player_yaml


def build(destination: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    schema = load_options_schema(root / "data/options_schema.json")
    common = {**default_option_values(schema), "use_dlc_content": True,
              "campaign_difficulty": "hurt_me_plenty", "special_weapon": "progressive_special_weapon",
              "randomize_dash": False, "include_weapon_mastery_challenges": True, "mission_count": "all"}
    rmo = {**common, "mission_pool": "custom", "custom_dark_lord": False,
           "custom_missions": ["Hell on Earth", "Doom Hunter Base", "Mars Core", "UAC Atlantica Facility", "The World Spear", "Final Sin"],
           "mission_order": "random_mission_order", "starting_missions": 1,
           "goal": "kill_the_icon_of_sin", "goal_mission_as_item": False,
           "additional_victory_requirements": ["Complete All Included Missions", "Complete All Slayer Gates", "Complete All Escalation Encounters"]}
    access = {**common, "mission_pool": "full_saga", "mission_order": "mission_access_as_items",
              "starting_missions": 2, "goal": "kill_the_dark_lord", "full_saga_final_boss": "davoth",
              "goal_mission_as_item": False, "additional_victory_requirements": next(
                  [choice["key"] for choice in option["choices"]] for option in schema["options"]
                  if option["key"] == "additional_victory_requirements")}
    save_player_yaml(destination / "RMO6.yaml", schema, "Marine", rmo)
    save_player_yaml(destination / "Full-MAI.yaml", schema, "DoomSlayer", access)
    print(json.dumps({"presets": ["RMO6.yaml", "Full-MAI.yaml"], "difficulty": "Hurt Me Plenty"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    build(parser.parse_args().output)
