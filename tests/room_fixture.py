import json
from pathlib import Path

from doom_eap.content.slot_data_contract import (
    catalog_digest,
    catalog_tables,
    generation_fingerprint,
    load_slot_catalog,
    placement_digest,
    recipe_digest,
)


def current_room_fixture():
    stages = ("e1m1_intro",)
    catalog = load_slot_catalog()
    tables = catalog_tables(catalog)
    identity = json.loads((Path(__file__).resolve().parents[1] / "data/content_identity.json").read_bytes())
    recipes = {"enabled": False, "limit_sets": [], "profiles": [], "patterns": [], "maps": {}, "timers": []}
    active_maps = {key for key, row in tables["maps"].items() if row["stage_id"] in {*stages, None}}
    recipes["maps"] = {
        key: {
            "rows": [],
            "passthrough_sources": sorted(ref for ref, row in tables["sources"].items() if row["stage_id"] == key),
            "disabled_sources": [],
        }
        for key in active_maps
    }
    recipes["timers"] = [
        [ref, 15000] for ref, row in sorted(tables["timers"].items()) if row["stage_id"] in active_maps
    ]
    data = {
        "identity": {
            "schema": 7,
            "algorithm_revision": 3,
            "catalog_digest": catalog_digest(catalog),
            "recipe_digest": recipe_digest(recipes),
        },
        "compatibility": {
            "apworld_version": identity["apworld_revision"],
            "minimum_client_version": identity["release_version"],
            "bridge_protocol": identity["bridge_protocol_version"],
            "manifest_schema_version": identity["manifest_schema_version"],
            "mod_contract_revision": identity["session_mod_contract_revision"],
            "required_capabilities": ["slot_data_v7"],
        },
        "campaign": {
            "mode": "Vanilla Order",
            "sequence": list(stages),
            "starting_stages": [stages[0]],
            "unprepared_start_stages": [],
            "opening_stage": None,
            "goal_as_item": False,
            "active_masteries_count": 0,
            "master_level_stage_ids": [],
            "access_items": [],
            "fixed_dash_completion_stage": stages[0],
            "goal": {
                "kind": "Complete the Full Saga",
                "stage_id": stages[-1],
                "count": 0,
                "endpoint_event": "Internal Goal Endpoint: Complete the Full Saga",
                "additional_requirements": [],
            },
            "fortress": {
                "active_spend_group_ids": [],
                "phase_thresholds": [],
                "battery_economy": {
                    "cost": 0,
                    "surplus": 0,
                    "total": 0,
                    "surplus_percent": catalog["battery_surplus_percent"],
                    "native_first_battery": False,
                },
            },
        },
        "starting": {"items": [[7770900, 1]], "weapon_item_id": 7770900},
        "runtime": {
            "difficulty": {"engine": 2, "death_policy": "normal"},
            "death_link": {"enabled": False, "mode": "soft"},
            "use_dlc_content": True,
            "randomize_chainsaw": False,
            "randomize_dash": False,
            "randomize_first_battery": True,
            "enhanced_melee_damage": False,
            "easier_secret_encounters": False,
            "reveal_ap_locations_on_automap": False,
            "checks": {
                "include_slayer_gates": True,
                "include_secret_encounters": True,
                "include_weapon_mastery_challenges": True,
                "arena_checks": 4,
            },
            "special_weapon": "Progressive Special Weapon",
            "mastery_requirement_counts": {},
        },
        "recipes": recipes,
        "stage_profiles": {
            stage: {
                "cr_min": 12,
                "cr_max": 12,
                "opening_cr_min": None,
                "opening_cr_max": None,
                "counter_ids": [],
                "opening_counter_ids": [],
                "renewable_ammo": False,
                "native_chainsaw_before_combat": False,
                "skulls": 1,
                "effort_per_check_milli": 0,
            }
            for stage in stages
        },
        "killsanity": {
            "interval": 0,
            "count": 0,
            "finite_supported_count": 0,
            "finite_kills": 0,
            "estimated_kills": 0,
            "sources": [],
        },
        "tracker_options": {"start_inventory": {}, "start_inventory_from_pool": {}},
        "placement_digest": placement_digest([]),
    }
    data["native_generation_fingerprint"] = generation_fingerprint(data)
    return data


def sample_catalog():
    return {
        "stages": {"a": {"access_id": 7771000}},
        "maps": {"a": {"stage_id": "a"}},
        "items": [7770001],
        "spend_groups": {"room": 2},
        "battery_surplus_percent": 25,
        "templates": {"imp": {}, "zombie": {}},
        "sources": {"0" * 16: {"stage_id": "a"}, "1" * 16: {"stage_id": "a"}},
        "roles": ["primary"],
        "spawn_types": ["imp"],
        "species": ["imp"],
        "timers": {"2" * 16: {"stage_id": "a", "entity": "timer"}},
        "finite_sources": {"3" * 16: {"stage_id": "a", "source_id": "kill"}},
        "counters": ["imp"],
        "masteries": {"challenge": {}},
    }


def sample_recipes():
    return {
        "enabled": True,
        "limit_sets": [{"special": [1, 1, 1, 2], "population": None}],
        "profiles": [["imp", 100, 100, 100, 100], ["zombie", 100, 100, 100, 100]],
        "patterns": [[[0, 2], [1, 1], [0, 1]]],
        "maps": {
            "a": {
                "rows": [["0" * 16, "primary", "imp", 0, 4, 4, 4, False, 4, 4, [0, 0, 0, 0]]],
                "passthrough_sources": ["1" * 16],
                "disabled_sources": [],
            }
        },
        "timers": [["2" * 16, 15000]],
    }


def sign_slot(data, catalog):
    data["identity"]["catalog_digest"] = catalog_digest(catalog)
    data["identity"]["recipe_digest"] = recipe_digest(data["recipes"])
    data["native_generation_fingerprint"] = generation_fingerprint(data)
    return data


def sample_slot(catalog=None, recipes=None):
    catalog = sample_catalog() if catalog is None else catalog
    data = {
        "identity": {"schema": 7, "algorithm_revision": 1, "catalog_digest": "", "recipe_digest": ""},
        "compatibility": {
            "apworld_version": "0.8.0",
            "minimum_client_version": "0.8.0",
            "bridge_protocol": 4,
            "manifest_schema_version": 2,
            "mod_contract_revision": 1,
            "required_capabilities": [],
        },
        "campaign": {
            "mode": "full",
            "sequence": ["a"],
            "starting_stages": ["a"],
            "master_level_stage_ids": [],
            "unprepared_start_stages": [],
            "opening_stage": None,
            "goal_as_item": True,
            "active_masteries_count": 0,
            "access_items": [7771000],
            "fixed_dash_completion_stage": None,
            "goal": {
                "kind": "stage",
                "stage_id": "a",
                "count": 0,
                "endpoint_event": "complete",
                "additional_requirements": [],
            },
            "fortress": {
                "active_spend_group_ids": ["room"],
                "phase_thresholds": [0, 1],
                "battery_economy": {
                    "cost": 2,
                    "surplus": 0,
                    "total": 2,
                    "surplus_percent": 25,
                    "native_first_battery": False,
                },
            },
        },
        "starting": {"items": [[7770001, 1]], "weapon_item_id": 7770001},
        "runtime": {
            "difficulty": {"engine": 2, "death_policy": "normal"},
            "death_link": {"enabled": False, "mode": "soft"},
            "use_dlc_content": True,
            "randomize_chainsaw": True,
            "randomize_dash": True,
            "randomize_first_battery": True,
            "enhanced_melee_damage": False,
            "easier_secret_encounters": True,
            "reveal_ap_locations_on_automap": False,
            "checks": {
                "include_slayer_gates": True,
                "include_secret_encounters": True,
                "include_weapon_mastery_challenges": True,
                "arena_checks": 1,
            },
            "special_weapon": "The Crucible",
            "mastery_requirement_counts": {"challenge": 2},
        },
        "recipes": sample_recipes() if recipes is None else recipes,
        "stage_profiles": {
            "a": {
                "cr_min": 10,
                "cr_max": 20,
                "opening_cr_min": None,
                "opening_cr_max": None,
                "counter_ids": ["imp"],
                "opening_counter_ids": [],
                "renewable_ammo": False,
                "native_chainsaw_before_combat": False,
                "skulls": 2,
                "effort_per_check_milli": 1000,
            }
        },
        "killsanity": {
            "interval": 0,
            "count": 0,
            "finite_supported_count": 0,
            "finite_kills": 0,
            "estimated_kills": 0,
            "sources": [],
        },
        "tracker_options": {"start_inventory": {}, "start_inventory_from_pool": {}, "difficulty": 2},
        "placement_digest": placement_digest([[2_200_000_001, 7770001, 1]]),
    }
    return sign_slot(data, catalog)
