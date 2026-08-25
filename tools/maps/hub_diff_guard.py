"""Entity-level guard for the only Hub changes admitted after v0.3.0."""

from __future__ import annotations

import copy
import json
import re
import tempfile
from pathlib import Path

from tools.maps.ap_map_generator import (
    find_entity_block_bounds,
    find_matching_brace,
    generate_map,
    remove_property_blocks,
)

ROOT = Path(__file__).resolve().parent.parent.parent
OLD_HUB_LOCATION_IDS = {7770072, 7770073, 7770074, 7770081, 7770086, 7770087, 7770088}
NEW_HUB_LOCATION_IDS = set(range(7770163, 7770172)) | {7770253, 7770254, 7770255}
EXPECTED_CHANGED = {
    "target_relay_pickup_ballista",
}
EXPECTED_STATE_STATS_REMOVED = {
    "interact_hub_2_battery_station_1",
    "interact_hub_2_battery_station_2",
    "interact_hub_2_battery_station_3",
}
EXPECTED_REMOVED = {
    "sentinel_battery_room_progress_praetor_token_1",
    "sentinel_battery_room_progress_praetor_token_2",
    "sentinel_battery_room_progress_mod_bot_3",
    "sentinel_battery_room_progress_mod_bot_4",
    "progress_praetor_token_3",
    "progress_praetor_token_4",
    "progress_cheats_all_mastered_runes_1",
    "pickup_weapon_gauss_rifle_hub_1",
    "progress_cheats_fully_upgraded_progression_wheel_final",
    "target_give_item_ballista",
    "func_animated_1",
    "func_animated_2",
    "func_animated_3",
}
EXPECTED_VISUAL_CHILD_REMOVALS = {
    "func_animated_1": {
        "model": "md6def/objects/doomslayer_armor/doomslayer_armor_set11.md6",
        "bind_parent": "doom_sentinel_armor_anchor",
        "preserve_entity": "doom_sentinel_armor_anchor",
    },
    "func_animated_2": {
        "model": "md6def/customization/characters/humans/male/set19/base/doom_marine_3p_set19.md6",
        "bind_parent": "doom_4_armor_anchor",
        "preserve_entity": "doom_4_armor_anchor",
    },
    "func_animated_3": {
        "model": "md6def/objects/doomslayer_armor/doomslayer_armor_set3.md6",
        "bind_parent": "doom_1_armor_anchor",
        "preserve_entity": "doom_1_armor_anchor",
    },
}
PRESERVED_HUB_ANCHORS = {
    "doom_sentinel_armor_anchor",
    "doom_4_armor_anchor",
    "doom_1_armor_anchor",
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


def _assert_visual_child_removals(source_text: str, generated_text: str) -> None:
    for child_name, expected in EXPECTED_VISUAL_CHILD_REMOVALS.items():
        source_bounds = find_entity_block_bounds(source_text, child_name)
        if source_bounds is None:
            raise ValueError(f"Hub visual child source entity is missing: {child_name}")
        source_block = source_text[source_bounds[0]:source_bounds[1]]
        models = re.findall(r'\bmodel\s*=\s*"([^"]+)";', source_block)
        bind_parents = re.findall(r'\bbindParent\s*=\s*"([^"]+)";', source_block)
        if models != [expected["model"]] or bind_parents != [expected["bind_parent"]]:
            raise ValueError(
                f"Hub visual child source signature drifted: {child_name}; "
                f"models={models}, bind_parents={bind_parents}"
            )
        if generated_text.count(f"entityDef {child_name} {{") != 0:
            raise ValueError(f"Hub visual child remains in generated map: {child_name}")

    for anchor_name in PRESERVED_HUB_ANCHORS:
        source_bounds = find_entity_block_bounds(source_text, anchor_name)
        generated_bounds = find_entity_block_bounds(generated_text, anchor_name)
        if source_bounds is None or generated_bounds is None:
            raise ValueError(f"Hub armor anchor missing after visual removal: {anchor_name}")
        if (
            source_text[source_bounds[0]:source_bounds[1]]
            != generated_text[generated_bounds[0]:generated_bounds[1]]
        ):
            raise ValueError(f"Hub armor anchor changed during visual removal: {anchor_name}")


def assert_hub_diff_classified() -> dict:
    config_path = ROOT / "content/maps/hub/locations.json"
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
    old.pop("visual_child_removals", None)

    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        old_config = temporary / "locations.json"
        old_config.write_text(json.dumps(old), encoding="utf-8")
        for companion_name in ("assets.json", "descriptor.json"):
            companion = config_path.with_name(companion_name)
            if companion.is_file():
                (temporary / companion_name).write_text(
                    companion.read_text(encoding="utf-8"), encoding="utf-8"
                )
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
        generated_text = new_map.read_text(encoding="utf-8")
        source_text = (ROOT / "vanillamaps/hub.map").read_text(encoding="utf-8")
        configured_visual_removals = {
            entry["entity"]: entry
            for entry in current.get("visual_child_removals", [])
        }
        if set(configured_visual_removals) != set(EXPECTED_VISUAL_CHILD_REMOVALS):
            raise ValueError("Hub visual child removal configuration is not exact")
        for child_name, expected in EXPECTED_VISUAL_CHILD_REMOVALS.items():
            configured = configured_visual_removals[child_name]
            if (
                configured.get("model") != expected["model"]
                or configured.get("bind_parent") != expected["bind_parent"]
                or configured.get("preserve_entity") != expected["preserve_entity"]
            ):
                raise ValueError(f"Hub visual child removal configuration drifted: {child_name}")
        _assert_visual_child_removals(source_text, generated_text)
        goal_owner = "trigger_transition_to_e2m3"
        generated_bounds = find_entity_block_bounds(generated_text, goal_owner)
        source_bounds = find_entity_block_bounds(source_text, goal_owner)
        if generated_bounds is None or source_bounds is None:
            raise ValueError("Hub Mars Core transition owner is missing")
        if (
            generated_text[generated_bounds[0]:generated_bounds[1]]
            != source_text[source_bounds[0]:source_bounds[1]]
        ):
            raise ValueError("Hub Mars Core transition drifted while removing old goal")
        if "ap_campaign_goal_event" in generated_text or "AP_GOAL_EVENT_" in generated_text:
            raise ValueError("Hub still contains a campaign goal hook")
        before = _blocks(old_map.read_text(encoding="utf-8"))
        after = _blocks(new_map.read_text(encoding="utf-8"))

    changed = {name for name in before.keys() & after if before[name] != after[name]}
    removed = set(before) - set(after)
    added = set(after) - set(before)
    unexpected_changed = changed - EXPECTED_CHANGED - EXPECTED_STATE_STATS_REMOVED
    missing_state_stats_changes = EXPECTED_STATE_STATS_REMOVED - changed
    if unexpected_changed or missing_state_stats_changes or removed != EXPECTED_REMOVED:
        raise ValueError(
            f"Unclassified Hub original-entity diff: changed={sorted(changed)}, "
            f"unexpected_changed={sorted(unexpected_changed)}, "
            f"missing_state_stats_changes={sorted(missing_state_stats_changes)}, "
            f"removed={sorted(removed)}"
        )
    for name in EXPECTED_STATE_STATS_REMOVED:
        if remove_property_blocks(before[name], "stateStats") != after[name]:
            raise ValueError(
                f"Unclassified Hub station drift: {name} differs beyond stateStats removal"
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
        and not any(str(location_id) in name for location_id in NEW_HUB_LOCATION_IDS)
    }
    if unclassified_added:
        raise ValueError(f"Unclassified added Hub entities: {sorted(unclassified_added)}")
    return {
        "new_fortress_checks": sorted(
            name for name in changed | removed if name not in ("trigger_transition_to_e2m2", "trigger_transition_to_e2m3")
        ),
        "removed_goal_hook": ["trigger_transition_to_e2m3"],
        "goal_hook": [],
        "added_ap_entities": sorted(added),
        "unrelated": [],
    }


if __name__ == "__main__":
    print(json.dumps(assert_hub_diff_classified(), indent=2, sort_keys=True))
