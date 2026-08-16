"""Authoritative projection for room-selected physical pickup options."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

PHYSICAL_OPTION_KEYS = (
    "randomize_chainsaw",
    "randomize_dash",
    "randomize_first_battery",
)

ROOM_OPTION_KEYS = (
    "death_link",
    "death_link_mode",
    "start_with_automap",
)
DEATH_LINK_MODES = frozenset({"soft", "hardcore"})

PHYSICAL_OPTIONS = {
    "randomize_chainsaw": {
        "map_key": "e1m1_intro",
        "location_id": 7770001,
        "entity": "AP_CHECK_BARGE_PICKUP_WEAPON_CHAINSAW_1",
        "vanilla_entity": "barge_pickup_weapon_chainsaw_1",
    },
    "randomize_dash": {
        "map_key": "e1m2_war",
        "location_id": 7770083,
        "entity": "AP_CHECK_CAPITOL_PROGRESS_DASH_1",
        "vanilla_entity": "capitol_progress_dash_1",
    },
    "randomize_first_battery": {
        "map_key": "e1m2_war",
        "location_id": 7770084,
        "entity": "AP_CHECK_CAPITOL_PROGRESS_SENTINEL_BATTERY_1_E1M2",
        "vanilla_entity": "capitol_progress_sentinel_battery_1_e1m2",
    },
}


def map_physical_option_keys(map_key: str) -> tuple[str, ...]:
    return tuple(sorted(
        key for key, spec in PHYSICAL_OPTIONS.items()
        if spec["map_key"] == map_key
    ))


def normalize_physical_options(options: Mapping[str, Any], *, require_all: bool = False) -> dict[str, bool]:
    """Return physical flags without inventing values for real room data."""
    result: dict[str, bool] = {}
    for key in PHYSICAL_OPTION_KEYS:
        if key not in options:
            if require_all:
                raise ValueError(f"missing physical option: {key}")
            result[key] = False
            continue
        value = options[key]
        if not isinstance(value, bool):
            raise ValueError(f"physical option {key} must be boolean")
        result[key] = value
    return result


def project_room_config(options: Mapping[str, Any]) -> dict[str, Any]:
    """Project room-wide options."""
    death_link = options.get("death_link", False)
    if not isinstance(death_link, bool):
        raise ValueError("room option death_link must be boolean")

    death_link_mode = options.get("death_link_mode", "soft")
    if not isinstance(death_link_mode, str) or death_link_mode not in DEATH_LINK_MODES:
        raise ValueError(
            "room option death_link_mode must be one of "
            + ", ".join(sorted(DEATH_LINK_MODES))
        )

    start_with_automap = options.get("start_with_automap", False)
    if not isinstance(start_with_automap, bool):
        raise ValueError("room option start_with_automap must be boolean")
    return {
        "schema_version": 1,
        "death_link": death_link,
        "death_link_mode": death_link_mode,
        "start_with_automap": start_with_automap,
    }


def physical_location_ids(options: Mapping[str, Any]) -> set[int]:
    values = normalize_physical_options(options)
    return {
        int(spec["location_id"])
        for key, spec in PHYSICAL_OPTIONS.items()
        if values[key]
    }


def project_map_config(config: Mapping[str, Any], options: Mapping[str, Any]) -> dict[str, Any]:
    """Project room options onto one map configuration."""
    result = deepcopy(dict(config))
    map_key = result.get("map_key")
    values = normalize_physical_options(options)
    start_with_automap = options.get("start_with_automap", False)
    if not isinstance(start_with_automap, bool):
        raise ValueError("room option start_with_automap must be boolean")
    result["start_with_automap"] = start_with_automap
    for key, spec in PHYSICAL_OPTIONS.items():
        if values[key] or spec["map_key"] != map_key:
            continue
        entity = str(spec["entity"])
        result.get("entities", {}).pop(entity, None)
        result.get("names", {}).pop(str(spec["location_id"]), None)
        result.get("location_feedback", {}).pop(entity, None)
        result.get("target_policies", {}).pop(entity.removeprefix("AP_CHECK_").lower(), None)
    return result
