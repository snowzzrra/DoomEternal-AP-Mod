"""Authored combat spaces and their native completion points."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_arena_checks(root=ROOT):
    document = json.loads((root / "content/catalog/arena_checks.json").read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise ValueError("Invalid arena catalog schema")
    identities, ids, managers = set(), set(), set()
    for map_key, entry in document["maps"].items():
        if entry["kind"] not in {"base", "dlc", "master"}:
            raise ValueError(f"{map_key}: invalid arena mission kind")
        for arena in entry["arenas"]:
            identity = (map_key, arena["key"])
            location_id = arena["location_id"]
            if identity in identities or location_id in ids or type(location_id) is not int:
                raise ValueError(f"{identity}: duplicate or invalid arena identity")
            if not document["id_block"][0] <= location_id <= document["id_block"][1]:
                raise ValueError(f"{identity}: arena address outside reserved block")
            identities.add(identity)
            ids.add(location_id)
            validate_arena(arena, map_key, managers)
    return document["maps"]


def validate_arena(arena, map_key, managers):
    if not arena["managers"] or not arena["completion"] or not arena["region"]:
        raise ValueError(f"{map_key}/{arena['key']}: incomplete arena")
    for manager in arena["managers"]:
        identity = (map_key, manager)
        if identity in managers:
            raise ValueError(f"{identity}: manager belongs to multiple arenas")
        managers.add(identity)
    for source in arena["completion"]:
        if "manager" in source and source["manager"] not in arena["managers"]:
            raise ValueError(f"{map_key}/{arena['key']}: completion outside arena")


def arena_set_enabled(choice, kind):
    if type(choice) is not int or choice not in range(5):
        raise ValueError("Arena Checks must be an integer from 0 to 4")
    return kind in ({"base", "dlc", "master"}, {"dlc", "master"}, {"dlc"}, {"master"}, set())[choice]


def arena_check_name(arena):
    return f"AP_CHECK_ARENA_{arena['location_id']}"


def arena_location_kinds(root):
    return {arena["name"]: entry["kind"] for entry in load_arena_checks(root).values()
            for arena in entry["arenas"]}


def arena_location_rules(root):
    return {arena["name"]: arena["logic_reference"] for entry in load_arena_checks(root).values()
            for arena in entry["arenas"] if "logic_reference" in arena}


def inactive_arena_ids(root, choice):
    return {arena["location_id"] for entry in load_arena_checks(root).values()
            if not arena_set_enabled(choice, entry["kind"]) for arena in entry["arenas"]}


def arena_manifest(catalog, map_key):
    return {item.ap_check: item.location_id for item in catalog.physical_locations
            if item.map_key == map_key and item.strategy == "arena"}


def arena_automap_classification(root):
    return {arena["location_id"]: "arena_checks" for entry in load_arena_checks(root).values()
            for arena in entry["arenas"]}


def assign_arena_regions(root, topology):
    for entry in load_arena_checks(root).values():
        for arena in entry["arenas"]:
            topology["assignments"].setdefault(arena["region"], []).append(arena["location_id"])


def map_arena_config(config, descriptor_path):
    if "arena_checks" not in config and descriptor_path.is_file():
        from .master_levels import load_master_levels

        masters = load_master_levels(ROOT)
        layers = {stage["map_key"]: stage["normal_layer"] for stage in masters.values()}
        config["arena_checks"] = [{**arena, "layer": masters[key]["layers"][0] if key in masters else layers.get(key)}
                                  for key, entry in load_arena_checks().items()
                                  if entry.get("map_key", key) == config["map_key"] for arena in entry["arenas"]]
