"""Generate the full-campaign runtime fixture through APWorld generation and fill."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest  # scopes AP's test-world loader


OPTIONS = {
    "mission_pool": "full_saga", "mission_order": "random_mission_order", "mission_count": "all",
    "goal": "kill_the_dark_lord", "campaign_difficulty": "hurt_me_plenty",
    "logic_difficulty": "standard", "mastery_requirements": "half", "resource_profile": "balanced",
    "item_pool_counts": {}, "filler_weights": {}, "trap_weights": {},
    "starting_weapon": "combat_shotgun", "use_dlc_content": True,
    "randomize_chainsaw": False, "randomize_dash": False,
    "include_weapon_mastery_challenges": True, "easier_secret_encounters": False,
    "praetor_suit_upgrades_in_pool": "auto", "trap_percentage": 10, "start_inventory": {},
}


def main():
    if os.environ.get("PYTHONHASHSEED") != "0":
        return subprocess.call([sys.executable, __file__], env={**os.environ, "PYTHONHASHSEED": "0"})
    mod_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(mod_root.parent / "Archipelago"))
    os.environ["AP_TEST_WORLDS"] = "doometernal"
    sys.modules["pyximport"] = None
    from test.general import setup_multiworld
    from Fill import distribute_items_restrictive
    from worlds.doometernal import DoomEternalWorld

    world = setup_multiworld(DoomEternalWorld, seed=700, options=OPTIONS)
    distribute_items_restrictive(world)
    slot_data = world.worlds[1].fill_slot_data()
    target = mod_root / "tests/fixtures/full_campaign_slot_data.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(slot_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(target)
    print(json.dumps({"rng_seed": 700, "python_hash_seed": 0,
                      "fingerprint": slot_data["native_generation_fingerprint"],
                      "active_locations": slot_data["active_location_count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
