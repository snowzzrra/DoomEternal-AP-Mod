"""Entity-level guard for the only Hub changes admitted after v0.3.0."""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

from tools.maps.ap_map_generator import find_matching_brace, generate_map
from tools.maps.mission_complete_map_patcher import _patch_fortress_goal


ROOT = Path(__file__).resolve().parent.parent.parent
OLD_HUB_LOCATION_IDS = {7770072, 7770073, 7770074, 7770081, 7770086, 7770087, 7770088}
NEW_HUB_LOCATION_IDS = set(range(7770163, 7770172))
EXPECTED_CHANGED_OR_REMOVED = {
    "sentinel_battery_room_progress_praetor_token_1",
    "sentinel_battery_room_progress_praetor_token_2",
    "sentinel_battery_room_progress_mod_bot_3",
    "sentinel_battery_room_progress_mod_bot_4",
    "progress_praetor_token_3",
    "progress_praetor_token_4",
    "progress_cheats_all_mastered_runes_1",
    "pickup_weapon_gauss_rifle_hub_1",
    "progress_cheats_fully_upgraded_progression_wheel_final",
    "target_relay_pickup_ballista",
    "target_give_item_ballista",
    "trigger_transition_to_e2m1",
}


def _blocks(text: str) -> dict[str, str]:
    result = {}
    position = 0
    while True:
        start = text.find("entity {", position)
        if start < 0:
            return result
        end = find_matching_brace(text, text.find("{", start))
        block = text[start:end]
        marker = "entityDef "
        name_start = block.find(marker)
        if name_start >= 0:
            name_start += len(marker)
            name_end = block.find(" ", name_start)
            brace_end = block.find("{", name_start)
            if name_end < 0 or brace_end < name_end:
                name_end = brace_end
            result[block[name_start:name_end].strip()] = block
        position = end


def assert_hub_diff_classified() -> dict:
    config_path = ROOT / "level_configs/hub.json"
    current = json.loads(config_path.read_text(encoding="utf-8"))
    old = copy.deepcopy(current)
    old["entities"] = {
        name: location_id for name, location_id in old["entities"].items()
        if location_id in OLD_HUB_LOCATION_IDS
    }
    old["target_policies"] = {
        name: policy for name, policy in old["target_policies"].items()
        if f"AP_CHECK_{name.upper()}" in old["entities"]
    }
    old.pop("target_removals", None)
    old.pop("remove_entities", None)

    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        old_config = temporary / "hub-v030.json"
        old_config.write_text(json.dumps(old), encoding="utf-8")
        old_map = temporary / "hub-v030.entities"
        new_map = temporary / "hub-v031.entities"
        items = json.loads((ROOT / "data/items.json").read_text(encoding="utf-8"))
        generate_map(
            ROOT / "vanillamaps/hub.map", old_map, old_config,
            temporary / "old-manifest.json", items,
        )
        generate_map(
            ROOT / "vanillamaps/hub.map", new_map, config_path,
            temporary / "new-manifest.json", items,
        )
        contracts = json.loads(
            (ROOT / "data/mission_complete_map_contracts.json").read_text(encoding="utf-8")
        )
        _patch_fortress_goal(
            contracts["fortress_visit_3_goal"], ROOT, new_map
        )
        before = _blocks(old_map.read_text(encoding="utf-8"))
        after = _blocks(new_map.read_text(encoding="utf-8"))

    changed = {name for name in before.keys() & after if before[name] != after[name]}
    removed = set(before) - set(after)
    added = set(after) - set(before)
    if changed | removed != EXPECTED_CHANGED_OR_REMOVED:
        raise ValueError(
            f"Unclassified Hub original-entity diff: changed={sorted(changed)}, "
            f"removed={sorted(removed)}"
        )
    ap_checks = {
        declaration for declaration, location_id in current["entities"].items()
        if location_id in NEW_HUB_LOCATION_IDS
    }
    new_source_entities = {
        declaration.removeprefix("AP_CHECK_").lower()
        for declaration in ap_checks
    }
    named_generated = {
        *(f"ap_independent_{name}" for name in new_source_entities),
        *(f"ap_notify_{declaration}" for declaration in ap_checks),
    }
    unclassified_added = {
        name for name in added
        if name not in ap_checks
        and name not in named_generated
        and name != "ap_goal_fortress_visit_3"
        and not any(str(location_id) in name for location_id in NEW_HUB_LOCATION_IDS)
    }
    if unclassified_added:
        raise ValueError(f"Unclassified added Hub entities: {sorted(unclassified_added)}")
    return {
        "new_fortress_checks": sorted(
            name for name in changed | removed if name != "trigger_transition_to_e2m1"
        ),
        "goal_hook": ["trigger_transition_to_e2m1", "ap_goal_fortress_visit_3"],
        "added_ap_entities": sorted(added - {"ap_goal_fortress_visit_3"}),
        "unrelated": [],
    }


if __name__ == "__main__":
    print(json.dumps(assert_hub_diff_classified(), indent=2, sort_keys=True))
