import json
from dataclasses import replace


def load_master_levels(root):
    document = json.loads((root / "content/catalog/master_levels.json").read_text(encoding="utf-8"))
    if document["schema_version"] != 1:
        raise ValueError("Unsupported Master Level catalog")
    stages = document["stages"]
    for key, stage in stages.items():
        if key != "ml_" + stage["map_key"] or stage["variant"] != "master" or stage["qualification"] != "static":
            raise ValueError(f"Invalid Master Level identity: {key}")
        if not stage["entry"].startswith("masterl_level/") or not stage["layers"]:
            raise ValueError(f"Missing Master Level entry: {key}")
    return stages


def assign_master_level_regions(root, topology):
    for key, stage in load_master_levels(root).items():
        region = stage["entry_region"]
        topology["regions"].append({"name": region, "mission_key": key, "terminal": True,
                                    "order": len(topology["regions"])})
        topology["assignments"][region] = [stage["completion_id"], *[p["location_id"] for p in stage["pickups"]]]
        topology["connections"].append(["Menu", region, "Enter " + stage["name"]])


def master_level_profiles(root, native):
    from .vanilla_enemy_profiles import catalog_profile

    return {key: catalog_profile(native, stage["map_key"], master=True)
            for key, stage in load_master_levels(root).items()}


def combat_profiles(root):
    from .enemy_plan import BASE_COMBAT_COST

    native = json.loads((root / "doom_eap/content/native_enemy_catalog.json").read_bytes())
    result = {}
    for key, profile in master_level_profiles(root, native).items():
        waves = [{"id": wave["id"], "bodies": wave["total"],
                  "cost": float(sum(slot["count"] * max(BASE_COMBAT_COST[s] for s in slot["species"])
                                    for slot in wave["slots"]))} for wave in profile["waves"]]
        species = sorted({s for wave in profile["waves"] for slot in wave["slots"] for s in slot["species"]})
        result[key] = {"waves": waves, "bodies": sum(wave["bodies"] for wave in waves), "species": species,
                       "estimated_kills": profile["finite_total"] + 2 * profile["renewable_targets"],
                       "effort": profile["finite_total"] + 2 * profile["renewable_targets"] + 4 * len(waves),
                       "renewable_ammo": _renewable_fodder(profile)}
    return result


def append_master_publishers(root, publishers):
    from doom_eap.contracts.publisher_contracts import publisher_contracts_from_document

    for key, stage in load_master_levels(root).items():
        for index, publisher in enumerate(publishers):
            if publisher.map_key == stage["map_key"]:
                publishers[index] = replace(publisher, triggers=tuple(
                    {**trigger, "layer": stage["normal_layer"]} for trigger in publisher.triggers))
        owner = stage["completion_owner"]
        document = {"schema_version": 1, "publishers": [{"key": key + "_complete", "map_key": stage["map_key"],
            "triggers": [{"strategy": "map_event_file", "owner": owner, "filename": f"ap_stage_{key}_complete.txt",
                          "marker": "AP_STAGE_COMPLETE_" + key.upper(), "layer": stage["layers"][0]},
                         {"strategy": "terminal_owner", "owner": owner}],
            "effects": [{"strategy": "location_check", "location_id": stage["completion_id"]}],
            "dedupe_scope": "ap_session_location", "fallback_policy": "first_success_wins"}]}
        publishers.extend(publisher_contracts_from_document(document))


def _renewable_fodder(profile):
    from .vanilla_enemy_profiles import ROLES

    return any(slot["renewable"] and any(ROLES[species] == "fodder" for species in slot["species"])
               for wave in profile["waves"] for slot in wave["slots"])
