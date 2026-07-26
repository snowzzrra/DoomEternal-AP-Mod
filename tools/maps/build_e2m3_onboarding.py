#!/usr/bin/env python3
"""Build the hash-locked Mars Core onboarding audit from local source assets."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from campaign_goal_contract import load_campaign_goal_contract
from tools.maps.ap_map_generator import extract_target_names, find_entity_block_bounds

ROOT = Path(__file__).resolve().parents[2]
GOAL_CONTRACT = load_campaign_goal_contract()
SOURCE = ROOT / GOAL_CONTRACT["source"]
CONFIG = ROOT / "level_configs" / "e2m3_core.json"
LOCATION_NAMES = ROOT / "data" / "location_names.json"
OUTPUT = ROOT / "data" / "onboarding" / "e2m3_core.json"

FAMILIES = {
    **dict.fromkeys(range(7770256, 7770261), "praetor_tokens"),
    **dict.fromkeys(range(7770261, 7770264), "sentinel_batteries"),
    **dict.fromkeys(range(7770264, 7770266), "runes"),
    **dict.fromkeys(range(7770266, 7770272), "extra_lives"),
    **dict.fromkeys(range(7770272, 7770275), "toys_collectibles"),
    **dict.fromkeys(range(7770275, 7770277), "albums"),
    7770277: "cheat_codes",
    7770278: "codex",
    7770279: "codex",
    7770280: "slayer_keys",
    7770281: "sentinel_crystals",
    7770283: "secret_encounters",
    7770284: "secret_encounters",
}
SECRET_OWNERS = {
    7770283: "uac_chunk_1_interact_gore_nest_1",
    7770284: "meteors_interact_gore_nest_1",
}
SECRET_MANAGERS = {
    7770283: "uac_chunk_1_encounter_manager_4",
    7770284: "meteors_encounter_manager_secret_enc_01",
}


def _scalar(block: str, field: str) -> str | None:
    match = re.search(rf'\b{re.escape(field)}\s*=\s*"([^"]+)";', block)
    return match.group(1) if match else None


def _layers(block: str) -> list[str]:
    return re.findall(r'item\[\d+\]\s*=\s*"(game/sp/e2m3_core/[^"]+)";', block)


def _transform(block: str) -> dict[str, str]:
    position = re.search(r"spawnPosition\s*=\s*\{([^}]*)\}", block)
    if not position:
        raise ValueError("physical owner lacks spawnPosition")
    result = {}
    for axis in ("x", "y", "z"):
        match = re.search(rf"\b{axis}\s*=\s*([-+0-9.eE]+);", position.group(1))
        if not match:
            raise ValueError(f"physical owner lacks spawnPosition.{axis}")
        result[f"spawn_{axis}"] = f"{axis} = {match.group(1)};"
    return result


def _reward_edge(location_id: int, block: str, inherit: str) -> dict:
    useable = _scalar(block, "useableComponentDecl")
    if location_id == 7770281:
        target = "native WorldCache Sentinel Crystal upgrade transaction"
        classification = "ownership"
    elif location_id in SECRET_OWNERS:
        target = f"completion_manager={SECRET_MANAGERS[location_id]}"
        classification = "progression"
    elif "praetor_token" in inherit:
        target = "currencyList=CURRENCY_PRAETOR_UPGRADE"
        classification = "currency"
    elif "rune" in inherit:
        target = "native Rune selection transaction"
        classification = "ownership"
    else:
        target = f"useableComponentDecl={useable}" if useable else f"inherit={inherit}"
        classification = "ownership"
    return {
        "target": target,
        "classification": classification,
        "disposition": "preserve" if location_id in SECRET_OWNERS else "drop",
    }


def build() -> dict:
    source = SOURCE.read_text(encoding="utf-8")
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    names = json.loads(LOCATION_NAMES.read_text(encoding="utf-8"))["locations"]
    owners = {
        location_id: ap_check.removeprefix("AP_CHECK_").lower()
        for ap_check, location_id in config["entities"].items()
    }
    owners.update(SECRET_OWNERS)
    locations = []
    active_location_ids = [
        *range(7770256, 7770282),
        7770283,
        7770284,
    ]
    for location_id in active_location_ids:
        entity = owners[location_id]
        bounds = find_entity_block_bounds(source, entity)
        if bounds is None or source.count(f"entityDef {entity}") != 1:
            raise ValueError(f"non-unique Mars Core owner: {entity}")
        block = source[bounds[0]:bounds[1]]
        inherit = _scalar(block, "inherit")
        native_class = _scalar(block, "class")
        targets = extract_target_names(block)
        original_targets = [
            {
                "target": target,
                "classification": (
                    "cosmetic"
                    if target.startswith("slayer_gate_destructible_interact")
                    else "progression"
                ),
                "disposition": "preserve",
            }
            for target in targets
        ]
        bind = _scalar(block, "bindParent")
        locations.append({
            "entity": entity,
            "class": native_class,
            "inherit": inherit,
            "ap_id": location_id,
            "ap_name": names[str(location_id)],
            "entity_match_count": 1,
            "original_targets": original_targets,
            "reward_grant_currency_ownership_edges": [
                _reward_edge(location_id, block, inherit)
            ],
            "progression_objective_relays": [
                edge for edge in original_targets
                if edge["classification"] == "progression"
            ],
            "drop_targets": [],
            "bind_parent": bind,
            "local_transform": _transform(block),
            "layers": _layers(block),
            "checkpoints": [],
            "movers": [
                target for target in targets
                if "mover" in target or "destructible" in target
            ],
            "gates": [
                target for target in targets
                if "changelayer" in target or "gate" in target
            ],
            "conditional_pickup_behavior": (
                "native Secret Encounter interaction and used state retained; "
                "manager completion gets one AP target"
                if location_id in SECRET_OWNERS
                else "independent one-shot AP owner; vanilla reward owner removed; "
                "audited functional targets and cleanup retained"
            ),
            "family": FAMILIES[location_id],
            "feedback_policy": "ap_only",
            "reward_removed": location_id not in SECRET_OWNERS,
        })
    return {
        "schema_version": 2,
        "map_key": "e2m3_core",
        "source_sha256": "43d074988387e710c75b2f5368c8c554fbd30e62595bb384fa18dfa34f246dff",
        "resource_owner": "game/sp/e2m3_core/e2m3_core_patch2.resources",
        "resource_priority": 10,
        "mission_complete_transition": {
            "kind": "direct_owner",
            "owner": GOAL_CONTRACT["owner"],
            "target": GOAL_CONTRACT["location_event_target"],
            "classification": "progression",
        },
        "layers": sorted({
            layer for location in locations for layer in location["layers"]
        } | {
            "game/sp/e2m3_core/e2m3_core_gameplay_hell_chunk_2/hell_gameplay",
            "game/sp/e2m3_core/bfg_checkpoint_pickup",
            "game/sp/e2m3_core/master_level_classic_mode",
            "game/sp/e2m3_core/slayergate_01",
        }),
        "checkpoints": ["cp_01", "cp_08_post_bfg_fire"],
        "movers": [
            "slayer_gate_func_mover_key_grate",
            "slayer_gate_destructible_interact_argent_cell_1_1335273921_e2m3",
        ],
        "gates": [
            "slayer_arena_target_changelayer_slayergate_1",
            GOAL_CONTRACT["owner"],
        ],
        "new_decl_resources": [
            {
                "path": "unlockable/mission_challenge/e2m3/challenge_1.decl",
                "replaces_owner": "gameresources",
                "source_sha256": "bc2fdc5f7077aea0fe3a8b9c3ba19fb3b49992e4a30639c83015a479b162069e",
                "container": "gameresources.resources",
            },
            {
                "path": "unlockable/mission_challenge/e2m3/challenge_2.decl",
                "replaces_owner": "gameresources",
                "source_sha256": "fdb2a60ce10d86d06f027f71ea6cddc9f7464bb112a13a58ecff79912b6a9215",
                "container": "gameresources.resources",
            },
            {
                "path": "unlockable/mission_challenge/e2m3/challenge_3.decl",
                "replaces_owner": "gameresources",
                "source_sha256": "c83dc3b5168f296e7cf344e093398be523d0b552fde3f4ab51a70e5343640860",
                "container": "gameresources.resources",
            },
        ],
        "locations": locations,
        "runtime_map": GOAL_CONTRACT["runtime_map"],
        "supported_game_revision": "Steam 6.66 Rev 3.1",
        "resource_priority_metadata_only": True,
        "source_evidence": {
            "file": GOAL_CONTRACT["source"],
            "size_bytes": 15991600,
            "entity_count": 13220,
            "preexisting_ap_prefixes": [],
        },
        "entry_transition": {
            "owner": "target_level_transition_to_e2m3",
            "source_map": "game/hub/hub",
            "destination_map": GOAL_CONTRACT["runtime_map"],
            "checkpoint": "cp_01",
            "layer": "game/sp/hub/from_e2m2",
        },
        "exit_transition": {
            "owner": "hell_chunk_2_target_level_transition_1",
            "relay": "hell_chunk_2_target_relay_175",
            "terminal_owner": GOAL_CONTRACT["owner"],
            "ordered_relay_targets": [
                "hell_chunk_2_target_level_transition_1",
                "sound_sound_soundentity_eol_stinger",
            ],
            "destination_map": GOAL_CONTRACT["destination_map"],
            "checkpoint": "cp_01",
            "kind": "direct relay with six-second terminal delay",
        },
        "campaign_goal_owner": GOAL_CONTRACT["owner"],
        "bfg_ownership_graph": {
            "status": "vanilla_grant_deferred_from_randomization",
            "deferred_location_id": 7770282,
            "vanilla_cutscene_id": 4701,
            "super_shotgun_cutscene_id": 5008,
            "physical_owner": "phobos_pickup_weapon_bfg_2",
            "grant_edge": "inherit=pickup/weapon/bfg",
            "checkpoint_fallback_preserved": "phobos_pickup_weapon_bfg_1",
            "mission_select_fallback_preserved": "_pickup_weapon_bfg_1",
            "functional_timeline_preserved": "phobos_target_timeline_sg_deck_fire",
            "objective_give_preserved": "objective_target_objective_give_1",
            "objective_complete_preserved": "objective_target_objective_complete_1",
            "cinematic_owners_preserved": [
                "cinematic_info_logic_bfg10k_firing",
                "cinematic_target_relay_bfg10k",
            ],
            "tutorial_gate": None,
            "inventory_gate": None,
            "ammo_pickups": [
                "hell_chunk_2_pickup_ammo_bfg_2",
                "phobos_pickup_ammo_bfg_1",
                "meteors_pickup_ammo_bfg_1",
                "meteors_pickup_ammo_bfg_2",
            ],
        },
        "inventory_exclusions": [
            "common ammo/health/armor/fuel and BFG ammo",
            "phobos_interact_automap_1_e2m3 (map reveal only)",
            "Slayer Gate door/nest/chest (native functional reward; no new item type)",
            "slayer_gate_interact_use_panel_kiosk_console_1 (mission console)",
            "master-level keycards, switches and weapon reserves",
            "phobos_pickup_weapon_plasma_rifle_1 and phobos_pickup_weapon_chaingun_1_e2m3",
        ],
    }


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(build(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
