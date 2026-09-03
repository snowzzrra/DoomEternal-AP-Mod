#!/usr/bin/env python3
"""Build room-specific DevInvLoadout overrides from packaged canonical data."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from doom_eap.content.map_registry import load_map_registry

SOURCE_OWNER = "gameresources"
SOURCE_PATH = "generated/decls/devinvloadout/devinvloadout/sp/e1m1.decl"
SOURCE_SHA256 = "c68c18750a4267b43d4ffd6e32b67dbed6af1c86099b947ddca9b98f2187a824"
OUTPUT_MAP_KEY = "e1m1_intro"
TAG_DEVINV_MANIFEST = Path(__file__).resolve().parents[2] / "data" / "devinv_sources" / "tag_dev_inv_chain.json"
TAG_DEVINV_DECL_PATH = "generated/decls/devinvloadout/devinvloadout/dlc/{map_key}.decl"

PAGE_STATS_BLOCK = """\t\tstatsToGive = {
\t\t\tnum = 2;
\t\t\titem[0] = "STAT_SUIT_PAGE_UNLOCKED";
\t\t\titem[1] = "STAT_RUNE_PAGE_UNLOCKED";
\t\t}
"""

CODEX_DOSSIER_CATEGORIES = {
    "earth": (
        "codex/earth/remaining_population_1",
        "codex/earth/remaining_population_2",
        "codex/earth/formation_of_arc",
        "codex/earth/cultist_base",
        "codex/earth/doom_hunter",
        "codex/earth/hellgrowth_1",
        "codex/earth/hellgrowth_2",
        "codex/earth/super_gore_nest",
        "codex/earth/return_of_hayden_1",
        "codex/earth/return_of_hayden_2",
        "codex/earth/the_arc_1",
        "codex/earth/the_arc_2",
        "codex/earth/samuel_hayden",
        "codex/earth/bfg_10k",
        "codex/earth/icon_of_sin",
        "codex/earth/arc_broadcast_1",
        "codex/earth/arc_broadcast_2",
        "codex/earth/arc_broadcast_3",
        "codex/earth/arc_broadcast_4",
        "codex/earth/arc_broadcast_5",
        "codex/earth/arc_broadcast_6",
        "codex/earth/elena_richardson_1",
        "codex/earth/elena_richardson_2",
        "codex/earth/elena_richardson_3",
        "codex/earth/elena_richardson_4",
    ),
    "sentinel": (
        "codex/sentinel/exultia",
        "codex/sentinel/wolf",
        "codex/sentinel/king_novik",
        "codex/sentinel/betrayer",
        "codex/sentinel/fortress_of_doom",
        "codex/sentinel/lost_city_hebeth",
        "codex/sentinel/sentinel_prime",
        "codex/sentinel/taras_nabad",
        "codex/sentinel/divinity_machine",
        "codex/sentinelhistory/sentinel_history_01",
        "codex/sentinelhistory/sentinel_history_02",
        "codex/sentinelhistory/sentinel_history_03",
        "codex/sentinelhistory/sentinel_history_04",
        "codex/sentinelhistory/sentinel_history_05",
        "codex/sentinelhistory/sentinel_history_06",
        "codex/sentinelhistory/sentinel_history_07",
        "codex/sentinelhistory/sentinel_history_08",
        "codex/sentinelhistory/sentinel_history_09",
        "codex/sentinelhistory/sentinel_history_10",
        "codex/sentinelhistory/sentinel_history_11",
        "codex/sentinelhistory/sentinel_history_12",
        "codex/sentinelhistory/sentinel_history_13",
        "codex/sentinelhistory/sentinel_history_14",
    ),
    "hell": (
        "codex/hell/hell_barges",
        "codex/hell/hell_priests",
        "codex/hell/deag_nilox",
        "codex/hell/deag_ranak",
        "codex/hell/doom_hunter",
        "codex/hell/marauder",
        "codex/hell/deag_grav",
        "codex/hell/gladiator",
        "codex/hell/nekravol_1",
        "codex/hell/fuel_eternal_flame_1",
        "codex/hell/nekravol_2",
        "codex/hell/fuel_eternal_flame_2",
        "codex/hell/fuel_eternal_flame_3",
        "codex/hell/icon_of_sin",
    ),
    "maykr": (
        "codex/maykr/urdak_1",
        "codex/maykr/khan_maykr",
        "codex/maykr/maykr_angels",
    ),
    "demons": (
        "codex/hell/demon_zombie_earth",
        "codex/hell/demon_imp",
        "codex/hell/demon_soldier_blaster",
        "codex/hell/demon_gargoyle",
        "codex/hell/demon_lostsoul",
        "codex/maykr/maykr_drones",
        "codex/hell/demon_arachnotron",
        "codex/hell/demon_cacodemon",
        "codex/hell/demon_carcass",
        "codex/hell/demon_mancubus_cyber",
        "codex/hell/demon_dreadknight",
        "codex/hell/demon_hellknight",
        "codex/hell/demon_mancubus_fire",
        "codex/hell/demon_painelemental",
        "codex/hell/demon_pinky",
        "codex/hell/demon_prowler",
        "codex/hell/demon_revenant",
        "codex/hell/demon_pinky_spectre",
        "codex/hell/demon_whiplash",
        "codex/hell/demon_archvile",
        "codex/hell/demon_baronofhell",
        "codex/hell/demon_doom_hunter",
        "codex/hell/demon_marauder",
        "codex/hell/demon_tyrant",
        "codex/hell/demon_buffpod",
        "codex/hell/demon_cueball",
        "codex/hell/demon_tentacle",
    ),
    "slayer": (
        "codex/slayer/arsenal_doomblade",
        "codex/slayer/arsenal_ballista",
        "codex/slayer/arsenal_bfg",
        "codex/slayer/arsenal_chaingun",
        "codex/slayer/arsenal_chainsaw",
        "codex/slayer/arsenal_combat_shotgun",
        "codex/slayer/arsenal_crucible",
        "codex/slayer/arsenal_equipment_launcher",
        "codex/slayer/arsenal_heavy_cannon",
        "codex/slayer/arsenal_plasmarifle",
        "codex/slayer/arsenal_rocketlauncher",
        "codex/slayer/arsenal_super_shotgun",
        "codex/slayer/arsenal_unmaykr",
    ),
    "tutorials": (
        "codex/tutorials/custom_skins",
        "codex/tutorials/empowered_demon",
        "codex/tutorials/secret",
        "codex/tutorials/mission_select",
        "codex/tutorials/cheat_codes",
        "codex/tutorials/sentinel_battery",
        "codex/tutorials/demon_prison",
        "codex/tutorials/empyrean_shard",
        "codex/tutorials/unmaykr",
        "codex/tutorials/glory_kill",
        "codex/tutorials/double_jump",
        "codex/tutorials/chainsaw",
        "codex/tutorials/objective_marker",
        "codex/tutorials/mod_station",
        "codex/tutorials/mod_swap",
        "codex/tutorials/weapon_wheel",
        "codex/tutorials/weak_point_arachnotron",
        "codex/tutorials/weak_point_cacodemon",
        "codex/tutorials/weak_point_revenant",
        "codex/tutorials/weak_point_mancubus",
        "codex/tutorials/plasma_vs_shields",
        "codex/tutorials/cueball",
        "codex/tutorials/weak_point_pinky",
        "codex/tutorials/weak_point_doomhunter",
        "codex/tutorials/weak_point_cyber_mancubus",
        "codex/tutorials/weak_point_marauder",
        "codex/tutorials/weak_point_maykr_zombie",
        "codex/tutorials/boss_gladiator_a",
        "codex/tutorials/boss_gladiator_b",
        "codex/tutorials/boss_khan_maykr",
        "codex/tutorials/boss_icon_of_sin_a",
        "codex/tutorials/boss_icon_of_sin_b",
        "codex/tutorials/automap_station",
        "codex/tutorials/extra_lives",
        "codex/tutorials/powerups",
        "codex/tutorials/wall_climb",
        "codex/tutorials/fast_travel",
        "codex/tutorials/equipment_frag",
        "codex/tutorials/equipment_flame",
        "codex/tutorials/equipment_ice",
        "codex/tutorials/argent_cell",
        "codex/tutorials/blood_punch",
        "codex/tutorials/blood_punch_upgrade_aoe",
        "codex/tutorials/blood_punch_upgrade_charge",
        "codex/tutorials/blood_punch_upgrade_maxcharges",
        "codex/tutorials/rune",
        "codex/tutorials/demonic_corruption",
        "codex/tutorials/secret_encounters",
        "codex/tutorials/slayer_key",
        "codex/tutorials/slayer_gate",
        "codex/tutorials/slayer_gate_retry",
        "codex/tutorials/weapon_points",
        "codex/tutorials/mastery_token_weapon",
        "codex/tutorials/dash",
        "codex/tutorials/dash_refill",
        "codex/tutorials/praetor_suit_perks",
        "codex/tutorials/play_as_revenant",
        "codex/tutorials/super_shotgun",
        "codex/tutorials/buffpod",
        "codex/tutorials/rad_suit",
        "codex/tutorials/bfg",
        "codex/tutorials/crucible",
    ),
}
CODEX_DOSSIER_ENTRIES = tuple(
    entry for category in CODEX_DOSSIER_CATEGORIES.values() for entry in category
)
CODEX_DOSSIER_BLOCK = "\t\tcodexEntriesToGive = {\n" + f"\t\t\tnum = {len(CODEX_DOSSIER_ENTRIES)};\n" + "\n".join(
    f'\t\t\titem[{index}] = "{entry}";' for index, entry in enumerate(CODEX_DOSSIER_ENTRIES)
) + "\n\t\t}\n"

# Hash-locked retail E1M1 loadout used by room and package generation.
CANONICAL_BASE = """{
\tedit = {
\t\tstartingInventory = {
\t\t\tnum = 8;
\t\t\titem[0] = {
\t\t\t\titem = \"jumpboots/base\";
\t\t\t\tequip = true;
\t\t\t}
\t\t\titem[1] = {
\t\t\t\titem = \"abilities/environmentsuit\";
\t\t\t}
\t\t\titem[2] = {
\t\t\t\titem = \"abilities/grapplegloves\";
\t\t\t}
\t\t\titem[3] = {
\t\t\t\titem = \"weapon/player/fists\";
\t\t\t}
\t\t\titem[4] = {
\t\t\t\titem = \"ammo/sharedammopool/bullets\";
\t\t\t\tcount = 999;
\t\t\t}
\t\t\titem[5] = {
\t\t\t\titem = \"weapon/player/shotgun\";
\t\t\t\tequip = true;
\t\t\t}
\t\t\titem[6] = {
\t\t\t\titem = \"ammo/sharedammopool/shells\";
\t\t\t\tcount = 999;
\t\t\t\tapplyAfterLoadout = true;
\t\t\t}
\t\t\titem[7] = {
\t\t\t\tperk = \"perk/player/suit/powerup/powerup_duration\";
\t\t\t\tequip = true;
\t\t\t}
\t\t}
\t\tcurrencyToGive = {
\t\t\tnum = 2;
\t\t\titem[0] = {
\t\t\t\tcount = 0;
\t\t\t}
\t\t\titem[1] = {
\t\t\t\tcurrencyType = \"CURRENCY_PRAETOR_UPGRADE\";
\t\t\t\tcount = 0;
\t\t\t}
\t\t}
\t\tclearAllBeforeApply = true;
\t}
}
""".rstrip("\n")

DEVINV_MAPPING_PATH = Path(__file__).resolve().parents[2] / "data" / "devinv_start_mapping.json"
DEVINV_MAPPING_SCHEMA_VERSION = 1
DEVINV_ALLOWED_KINDS = frozenset({
    "ability", "currency", "equipment", "key", "mastery", "progressive",
    "rune", "special_weapon", "stored_charge", "suit_perk", "weapon", "weapon_mod",
})
DEVINV_ALLOWED_FIELDS = frozenset({"item", "perk"})
DEVINV_ALLOWED_FLAGS = frozenset({"applyAfterLoadout", "equip", "forceStat", "isRune"})

STARTING_WEAPON_NAMES = frozenset({
    "Heavy Cannon", "Plasma Rifle", "Rocket Launcher", "Super Shotgun", "Ballista", "Chaingun", "Combat Shotgun",
})

# Retail source markers required by the patcher.
REQUIRED_MARKERS = frozenset({
    "clearAllBeforeApply",
    "currencyToGive",
    "startingInventory",
    "CURRENCY_PRAETOR_UPGRADE",
})

# Markers introduced by the generated room loadout.
FORBIDDEN_MARKERS = frozenset({
    "STAT_SUIT_PAGE_UNLOCKED",
    "statsToGive",
})

COMBAT_SHOTGUN_PATH = "weapon/player/shotgun"
_STARTING_INVENTORY_RE = re.compile(
    r"\t\tstartingInventory = \{\n(?P<body>.*?)\n\t\t\}",
    re.DOTALL,
)
_STARTING_INVENTORY_ANY_RE = _STARTING_INVENTORY_RE
_ITEM_BLOCK_RE = re.compile(
    r"\t\t\titem\[(?P<index>\d+)\] = \{\n(?P<body>.*?)\n\t\t\t\}",
    re.DOTALL,
)
_ITEM_PATH_RE = re.compile(r'\bitem = "(?P<path>[^"]+)";')
_PERK_PATH_RE = re.compile(r'\bperk = "(?P<path>[^"]+)";')
_CURRENCY_BLOCK_RE = re.compile(
    r"\t\tcurrencyToGive = \{\n(?P<body>.*?)\n\t\t\}\n\t\tclearAllBeforeApply",
    re.DOTALL,
)
_INHERIT_DECL_RE = re.compile(r"^[ \t]*inherit\s*=\s*[^;]+;\s*\n", re.MULTILINE)
_CODEX_ENTRIES_RE = re.compile(
    r"\t\tcodexEntriesToGive\s*=\s*\{.*?\n\t\t\}", re.DOTALL
)
_CODEX_ITEM_RE = re.compile(r'\t\t\titem\[(?P<index>\d+)\] = "(?P<path>[^"]+)";')


def load_devinv_mapping(path: Path = DEVINV_MAPPING_PATH) -> dict[int, dict[str, Any]]:
    """Load declarative public-ID -> DevInv representations."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not load DevInv start mapping: {error}") from error
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != DEVINV_MAPPING_SCHEMA_VERSION
        or document.get("mapping_revision") != 1
        or not isinstance(document.get("source"), str)
    ):
        raise ValueError("unsupported DevInv start mapping schema")
    raw_items = document.get("items")
    if not isinstance(raw_items, dict) or not raw_items:
        raise ValueError("DevInv start mapping lacks items")
    result: dict[int, dict[str, Any]] = {}
    names: set[str] = set()
    for raw_id, raw_entry in raw_items.items():
        try:
            item_id = int(raw_id)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid DevInv mapping item ID: {raw_id!r}") from error
        if str(item_id) != raw_id or item_id in result:
            raise ValueError(f"non-canonical or duplicate DevInv mapping ID: {raw_id!r}")
        if not isinstance(raw_entry, dict):
            raise ValueError(f"DevInv mapping item {item_id} is not an object")
        entry = dict(raw_entry)
        name = entry.get("name")
        kind = entry.get("kind")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError(f"DevInv mapping item {item_id} has duplicate or invalid name")
        if kind not in DEVINV_ALLOWED_KINDS:
            raise ValueError(f"DevInv mapping item {item_id} has invalid kind: {kind!r}")
        names.add(name)
        if kind == "currency":
            if set(entry) != {"name", "kind", "currency", "countPerItem"}:
                raise ValueError(f"DevInv currency mapping {item_id} has invalid fields")
            if not isinstance(entry["currency"], str) or not entry["currency"].startswith("CURRENCY_"):
                raise ValueError(f"DevInv currency mapping {item_id} has invalid currency")
            if isinstance(entry["countPerItem"], bool) or not isinstance(entry["countPerItem"], int) or entry["countPerItem"] < 1:
                raise ValueError(f"DevInv currency mapping {item_id} has invalid countPerItem")
        elif kind == "progressive":
            tiers = entry.get("tiers")
            if set(entry) != {"name", "kind", "field", "tiers"} or entry.get("field") not in DEVINV_ALLOWED_FIELDS:
                raise ValueError(f"DevInv progressive mapping {item_id} has invalid fields")
            if (
                not isinstance(tiers, list)
                or not tiers
                or any(not isinstance(path, str) or not path for path in tiers)
                or len(tiers) != len(set(tiers))
            ):
                raise ValueError(f"DevInv progressive mapping {item_id} has invalid ordered tiers")
        elif kind == "stored_charge":
            if set(entry) != {"name", "kind", "storage", "start_inventory_eligible"}:
                raise ValueError(f"DevInv stored-charge mapping {item_id} has invalid fields")
            if entry["storage"] != "ap_stored_charge" or entry["start_inventory_eligible"] is not True:
                raise ValueError(f"DevInv stored-charge mapping {item_id} has invalid storage contract")
        elif kind == "special_weapon":
            states = entry.get("states")
            if set(entry) != {"name", "kind", "state_family", "max_quantity", "replacement", "states"}:
                raise ValueError(f"DevInv special-weapon mapping {item_id} has invalid fields")
            if (
                not isinstance(entry["state_family"], str)
                or entry["replacement"] != "derived_highest_state_with_targeted_remove"
                or isinstance(entry["max_quantity"], bool)
                or not isinstance(entry["max_quantity"], int)
                or entry["max_quantity"] < 1
                or not isinstance(states, list)
                or len(states) != entry["max_quantity"]
            ):
                raise ValueError(f"DevInv special-weapon mapping {item_id} has invalid states")
            quantities = []
            for state in states:
                if not isinstance(state, dict) or set(state) != {"quantity", "representations"}:
                    raise ValueError(f"DevInv special-weapon mapping {item_id} has malformed state")
                quantity = state["quantity"]
                representations = state["representations"]
                if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                    raise ValueError(f"DevInv special-weapon mapping {item_id} has invalid state quantity")
                quantities.append(quantity)
                if not isinstance(representations, list) or not representations:
                    raise ValueError(f"DevInv special-weapon mapping {item_id} has empty state")
                for representation in representations:
                    if not isinstance(representation, dict):
                        raise ValueError(f"DevInv special-weapon mapping {item_id} has malformed representation")
                    allowed = {"field", "path", "flags", "dedupe"}
                    if set(representation) - allowed or representation.get("field") not in DEVINV_ALLOWED_FIELDS:
                        raise ValueError(f"DevInv special-weapon mapping {item_id} has invalid representation fields")
                    if not isinstance(representation.get("path"), str) or not representation["path"]:
                        raise ValueError(f"DevInv special-weapon mapping {item_id} has invalid representation path")
                    flags = representation.get("flags", [])
                    if not isinstance(flags, list) or any(flag not in DEVINV_ALLOWED_FLAGS for flag in flags):
                        raise ValueError(f"DevInv special-weapon mapping {item_id} has invalid representation flags")
            if quantities != list(range(1, entry["max_quantity"] + 1)):
                raise ValueError(f"DevInv special-weapon mapping {item_id} states must be contiguous")
        elif kind == "key":
            representations = entry.get("representations")
            if set(entry) != {"name", "kind", "map_key", "representations"} or not isinstance(representations, list) or not representations:
                raise ValueError(f"DevInv key mapping {item_id} has invalid fields")
            if not isinstance(entry.get("map_key"), str) or not entry["map_key"]:
                raise ValueError(f"DevInv key mapping {item_id} has invalid map_key")
            for representation in representations:
                if not isinstance(representation, dict):
                    raise ValueError(f"DevInv key mapping {item_id} has malformed representation")
                allowed = {"field", "path", "flags", "dedupe"}
                if set(representation) - allowed or representation.get("field") not in DEVINV_ALLOWED_FIELDS:
                    raise ValueError(f"DevInv key mapping {item_id} has invalid representation fields")
                if not isinstance(representation.get("path"), str) or not representation["path"]:
                    raise ValueError(f"DevInv key mapping {item_id} has invalid representation path")
                flags = representation.get("flags", [])
                if (
                    not isinstance(flags, list)
                    or any(not isinstance(flag, str) or flag not in DEVINV_ALLOWED_FLAGS for flag in flags)
                    or len(flags) != len(set(flags))
                ):
                    raise ValueError(f"DevInv key mapping {item_id} has invalid representation flags")
                if "dedupe" in representation and (not isinstance(representation["dedupe"], str) or not representation["dedupe"]):
                    raise ValueError(f"DevInv key mapping {item_id} has invalid dedupe key")
        else:
            representations = entry.get("representations")
            if set(entry) != {"name", "kind", "representations"} or not isinstance(representations, list) or not representations:
                raise ValueError(f"DevInv mapping item {item_id} has invalid representations")
            for representation in representations:
                if not isinstance(representation, dict):
                    raise ValueError(f"DevInv mapping item {item_id} has malformed representation")
                allowed = {"field", "path", "flags", "dedupe"}
                if set(representation) - allowed or representation.get("field") not in DEVINV_ALLOWED_FIELDS:
                    raise ValueError(f"DevInv mapping item {item_id} has invalid representation fields")
                if not isinstance(representation.get("path"), str) or not representation["path"]:
                    raise ValueError(f"DevInv mapping item {item_id} has invalid representation path")
                flags = representation.get("flags", [])
                if (
                    not isinstance(flags, list)
                    or any(not isinstance(flag, str) or flag not in DEVINV_ALLOWED_FLAGS for flag in flags)
                    or len(flags) != len(set(flags))
                ):
                    raise ValueError(f"DevInv mapping item {item_id} has invalid representation flags")
                if "dedupe" in representation and (not isinstance(representation["dedupe"], str) or not representation["dedupe"]):
                    raise ValueError(f"DevInv mapping item {item_id} has invalid dedupe key")
        result[item_id] = entry
    return result


def _mapping_by_name(mapping: Mapping[int, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in mapping.values()}


def _entry_for_name(name: str, mapping: Mapping[int, dict[str, Any]]) -> dict[str, Any]:
    try:
        return _mapping_by_name(mapping)[name]
    except KeyError as error:
        raise ValueError(f"unsupported starting_inventory item: {name!r}") from error


def _materialized_representations(
    name: str, quantity: int, mapping: Mapping[int, dict[str, Any]], seen_dedupe: set[str],
) -> list[tuple[str, str, tuple[str, ...]]]:
    entry = _entry_for_name(name, mapping)
    if entry["kind"] == "currency":
        return []
    if entry["kind"] == "stored_charge":
        # Stored charges are AP ownership state, not native DevInv inventory.
        return []
    if entry["kind"] == "key":
        return []
    if entry["kind"] == "special_weapon":
        if quantity > entry["max_quantity"]:
            raise ValueError(
                f"starting_inventory quantity for {name!r} exceeds {entry['max_quantity']} special-weapon states"
            )
        state = entry["states"][quantity - 1]
        return [
            (raw["field"], raw["path"], tuple(sorted(raw.get("flags", ()))))
            for raw in state["representations"]
        ]
    if entry["kind"] == "progressive":
        tiers = entry["tiers"]
        if quantity > len(tiers):
            raise ValueError(f"starting_inventory quantity for {name!r} exceeds {len(tiers)} progressive tiers")
        return [(entry["field"], path, ()) for path in tiers[:quantity]]
    materialized = []
    for raw in entry["representations"]:
        dedupe = raw.get("dedupe")
        if dedupe is not None and dedupe in seen_dedupe:
            continue
        if dedupe is not None:
            seen_dedupe.add(dedupe)
        materialized.append((raw["field"], raw["path"], tuple(sorted(raw.get("flags", ())))) )
    return materialized * quantity if not any(raw.get("dedupe") for raw in entry["representations"]) else materialized + [
        (raw["field"], raw["path"], tuple(sorted(raw.get("flags", ()))))
        for raw in entry["representations"] if raw.get("dedupe") is None
    ] * (quantity - 1)


def _remove_base_combat_shotgun(source: str) -> str:
    """Remove vanilla shotgun ownership and compact fixed inventory indices."""
    old = (
        '\t\t\titem[5] = {\n'
        f'\t\t\t\titem = "{COMBAT_SHOTGUN_PATH}";\n'
        '\t\t\t\tequip = true;\n'
        '\t\t\t}'
    )
    if source.count(old) != 1:
        raise ValueError("canonical DevInv base Combat Shotgun entry is missing or ambiguous")
    source = source.replace(old + "\n", "", 1)
    for old_index, new_index in ((6, 5), (7, 6)):
        marker = f"\t\t\titem[{old_index}] = {{"
        if source.count(marker) != 1:
            raise ValueError("canonical DevInv fixed inventory entry is missing or ambiguous")
        source = source.replace(marker, f"\t\t\titem[{new_index}] = {{", 1)
    return source


def canonical_base() -> str:
    """Return packaged vanilla base plus proven Suit/Rune page unlock stats."""
    source = CANONICAL_BASE.replace("\r\n", "\n")
    actual = hashlib.sha256(source.replace("\n", "\r\n").encode("utf-8")).hexdigest()
    if actual != SOURCE_SHA256:
        raise ValueError(
            f"packaged DevInvLoadout base hash drift: expected {SOURCE_SHA256}, got {actual}"
        )
    _assert_source_integrity(source)
    return _patch(source)


def _assert_source_integrity(source: str) -> None:
    for marker in REQUIRED_MARKERS:
        if marker not in source:
            raise ValueError(f"DevInvLoadout vanilla source missing required marker: {marker!r}")
    for marker in FORBIDDEN_MARKERS:
        if marker in source:
            raise ValueError(f"DevInvLoadout source already contains forbidden marker: {marker!r}")


def _patch(source: str) -> str:
    edit_marker = "\tedit = {\n"
    if source.count(edit_marker) != 1:
        raise ValueError("DevInvLoadout edit block is missing or ambiguous")

    override = source.replace(edit_marker, edit_marker + PAGE_STATS_BLOCK, 1)
    override = override.replace(
        "\t\tclearAllBeforeApply = true;",
        "\t\tclearAllBeforeApply = true;\n" + CODEX_DOSSIER_BLOCK.rstrip("\n"),
        1,
    )

    # Verify patch succeeded
    if "STAT_SUIT_PAGE_UNLOCKED" not in override:
        raise ValueError("DevInvLoadout patch: STAT_SUIT_PAGE_UNLOCKED not injected")
    if "STAT_RUNE_PAGE_UNLOCKED" not in override:
        raise ValueError("DevInvLoadout patch: STAT_RUNE_PAGE_UNLOCKED not injected")
    if override.count("statsToGive") != 1:
        raise ValueError("DevInvLoadout patch: statsToGive count mismatch")
    if override.count("currencyToGive") != 1:
        raise ValueError("DevInvLoadout patch: existing currencyToGive was corrupted")
    if override.count("clearAllBeforeApply") != 1:
        raise ValueError("DevInvLoadout patch: existing clearAllBeforeApply was corrupted")
    _assert_codex_dossier(override)

    return override


def _assert_codex_dossier(source: str) -> None:
    match = _CODEX_ENTRIES_RE.search(source)
    if match is None:
        raise ValueError("DevInvLoadout codexEntriesToGive block is missing or ambiguous")
    items = [
        (int(item.group("index")), item.group("path"))
        for item in _CODEX_ITEM_RE.finditer(match.group(0))
    ]
    expected = list(enumerate(CODEX_DOSSIER_ENTRIES))
    if items != expected:
        raise ValueError("DevInvLoadout codexEntriesToGive does not match stable dossier baseline")


def _decl_item(field: str, path: str, flags: tuple[str, ...], index: int, count: int | None = None) -> str:
    lines = [f"\t\t\titem[{index}] = {{", f"\t\t\t\t{field} = \"{path}\";"]
    if count is not None:
        lines.append(f"\t\t\t\tcount = {count};")
    for flag in flags:
        lines.append(f"\t\t\t\t{flag} = true;")
    lines.extend(("\t\t\t}",))
    return "\n".join(lines)


def _add_currency_totals(source: str, totals: Mapping[str, int]) -> str:
    match = _CURRENCY_BLOCK_RE.search(source)
    if match is None:
        raise ValueError("canonical DevInv currencyToGive block is missing or ambiguous")
    body = match.group("body")
    blocks = list(_ITEM_BLOCK_RE.finditer(body))
    existing: dict[str, int] = {}
    for item_match in blocks:
        item_body = item_match.group("body")
        currency_match = re.search(r'currencyType = "([^"]+)";', item_body)
        if currency_match:
            count_match = re.search(r"count = (\d+);", item_body)
            if count_match is None:
                raise ValueError("DevInv currency entry has no count")
            existing[currency_match.group(1)] = existing.get(currency_match.group(1), 0) + int(count_match.group(1))
    for currency, amount in totals.items():
        if currency in existing:
            pattern = re.compile(
                rf'(currencyType = "{re.escape(currency)}";(?:(?!\n\t\t\t\}}).)*?\n\t\t\t\tcount = )\d+(;)',
                re.DOTALL,
            )
            if not pattern.search(body):
                raise ValueError(f"DevInv currency entry {currency} cannot be updated")
            body = pattern.sub(
                lambda m, currency=currency, amount=amount: f"{m.group(1)}{existing[currency] + amount}{m.group(2)}",
                body,
                count=1,
            )
            existing[currency] += amount
            continue
        next_index = max((int(item.group("index")) for item in blocks), default=-1) + 1
        body += "\n" + _decl_currency(currency, amount, next_index)
        blocks = list(_ITEM_BLOCK_RE.finditer(body))
    num = len(list(_ITEM_BLOCK_RE.finditer(body)))
    replacement = f"\t\tcurrencyToGive = {{\n\t\t\tnum = {num};\n{body}\n\t\t}}\n\t\tclearAllBeforeApply"
    return source[:match.start()] + replacement + source[match.end():]


def _decl_currency(currency: str, count: int, index: int) -> str:
    return "\n".join((
        f"\t\t\titem[{index}] = {{",
        f'\t\t\t\tcurrencyType = "{currency}";',
        f"\t\t\t\tcount = {count};",
        "\t\t\t}",
    ))


def _currency_totals(source: str) -> dict[str, int]:
    match = _CURRENCY_BLOCK_RE.search(source)
    if match is None:
        raise ValueError("DevInvLoadout currencyToGive block is missing or ambiguous")
    totals: dict[str, int] = {}
    for item_match in _ITEM_BLOCK_RE.finditer(match.group("body")):
        body = item_match.group("body")
        currency_match = re.search(r'currencyType = "([^"]+)";', body)
        if currency_match is None:
            continue
        count_match = re.search(r"count = (\d+);", body)
        if count_match is None:
            raise ValueError("DevInvLoadout currency entry has no count")
        currency = currency_match.group(1)
        totals[currency] = totals.get(currency, 0) + int(count_match.group(1))
    return totals


def build_devinv_loadout(
    starting_inventory: Mapping[str, int] | None = None,
    starting_weapon: str | None = None,
) -> str:
    """Build safe E1M1 DevInv from resolved AP item names.

    ``starting_inventory`` and ``starting_weapon`` are already resolved seed
    values. Unknown names, malformed quantities, duplicate starting weapon
    ownership, and random/unresolved weapon values fail closed.
    """
    if starting_inventory is None:
        starting_inventory = {}
    if not isinstance(starting_inventory, Mapping):
        raise ValueError("starting_inventory must be an object of canonical item names")
    if any(not isinstance(name, str) for name in starting_inventory):
        raise ValueError("starting_inventory names must be strings")
    if starting_weapon is not None and (
        not isinstance(starting_weapon, str) or starting_weapon not in STARTING_WEAPON_NAMES
    ):
        raise ValueError(f"unsupported or unresolved starting_weapon: {starting_weapon!r}")

    mapping = load_devinv_mapping()
    by_name = _mapping_by_name(mapping)
    for name, quantity in starting_inventory.items():
        _entry_for_name(name, mapping)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ValueError(f"starting_inventory quantity must be positive integer: {name!r}")
        if starting_weapon == name:
            raise ValueError(f"starting weapon is duplicated in starting_inventory: {name!r}")
    source = canonical_base()
    keep_vanilla_shotgun = starting_weapon in (None, "Combat Shotgun")
    if not keep_vanilla_shotgun:
        source = _remove_base_combat_shotgun(source)

    entries: list[str] = []
    index = 8 if keep_vanilla_shotgun else 7
    seen_dedupe: set[str] = set()
    currency_totals: dict[str, int] = {}
    for name in sorted(starting_inventory):
        quantity = starting_inventory[name]
        entry = by_name[name]
        if entry["kind"] == "currency":
            currency_totals[entry["currency"]] = currency_totals.get(entry["currency"], 0) + quantity * entry["countPerItem"]
            continue
        for field, path, flags in _materialized_representations(name, quantity, mapping, seen_dedupe):
            entries.append(_decl_item(field, path, flags, index))
            index += 1
    if starting_weapon is not None and starting_weapon != "Combat Shotgun":
        weapon_entry = by_name.get(starting_weapon)
        if weapon_entry is None or weapon_entry["kind"] != "weapon":
            raise ValueError(f"unsupported or unresolved starting_weapon: {starting_weapon!r}")
        representation = weapon_entry["representations"][0]
        field, path = representation["field"], representation["path"]
        entries.append(_decl_item(field, path, ("equip",), index))
        index += 1

    if entries:
        marker = "\t\t}\n\t\tcurrencyToGive = {"
        if source.count(marker) != 1:
            raise ValueError("canonical DevInv startingInventory block is missing or ambiguous")
        block = "\n".join(entries) + "\n"
        source = source.replace(marker, block + "\t\t}\n\t\tcurrencyToGive = {", 1)
    source = source.replace("\t\t\tnum = 8;", f"\t\t\tnum = {index};", 1)
    if currency_totals:
        source = _add_currency_totals(source, currency_totals)
    validate_devinv_source(source, starting_inventory, starting_weapon)
    return source


def build_devinv(*, starting_inventory=None, starting_weapon=None) -> str:
    """Keyword-friendly alias for package/compiler callers."""
    return build_devinv_loadout(starting_inventory, starting_weapon)


def validate_devinv_source(
    source: str,
    starting_inventory: Mapping[str, int] | None = None,
    starting_weapon: str | None = None,
) -> None:
    """Validate canonical base, preserved stats, and optional materialization."""
    for marker in (
        "startingInventory", "currencyToGive", "CURRENCY_PRAETOR_UPGRADE",
        'STAT_SUIT_PAGE_UNLOCKED', 'STAT_RUNE_PAGE_UNLOCKED',
        "clearAllBeforeApply = true;",
    ):
        if marker not in source:
            raise ValueError(f"DevInvLoadout missing required marker: {marker}")
    if "STAT_CHALLENGE_PAGE_UNLOCKED" in source or "strip" in source.lower():
        raise ValueError("DevInvLoadout contains unsupported page/stripping capability")
    _assert_codex_dossier(source)
    if starting_inventory is not None and not isinstance(starting_inventory, Mapping):
        raise ValueError("starting_inventory must be an object")
    if starting_weapon is not None and starting_weapon not in STARTING_WEAPON_NAMES:
        raise ValueError(f"unsupported or unresolved starting_weapon: {starting_weapon!r}")
    match = _STARTING_INVENTORY_RE.search(source)
    if match is None:
        raise ValueError("DevInvLoadout startingInventory block is missing or ambiguous")
    num_match = re.search(r"\t\t\tnum = (?P<num>\d+);", match.group("body"))
    item_matches = list(_ITEM_BLOCK_RE.finditer(match.group("body")))
    if num_match is None:
        raise ValueError("DevInvLoadout startingInventory num is missing")
    declared_num = int(num_match.group("num"))
    indices = [int(item_match.group("index")) for item_match in item_matches]
    if indices != list(range(declared_num)):
        raise ValueError("DevInvLoadout startingInventory indices are not contiguous or num is untruthful")
    item_blocks = {
        int(item_match.group("index")): item_match.group("body")
        for item_match in item_matches
    }

    mapping = load_devinv_mapping()
    by_name = _mapping_by_name(mapping)
    if starting_inventory is not None:
        baseline_currency = _currency_totals(canonical_base())
        actual_currency = _currency_totals(source)
        expected_currency: dict[str, int] = {}
        for name, quantity in starting_inventory.items():
            entry = _entry_for_name(name, mapping)
            if entry["kind"] == "currency":
                currency = entry["currency"]
                expected_currency[currency] = expected_currency.get(currency, 0) + quantity * entry["countPerItem"]
        for currency, amount in expected_currency.items():
            if actual_currency.get(currency, 0) - baseline_currency.get(currency, 0) != amount:
                raise ValueError(f"DevInvLoadout did not aggregate currency {currency} by {amount}")
    weapon_paths = {
        by_name[name]["representations"][0]["path"]
        for name in STARTING_WEAPON_NAMES
    }
    weapon_entries = []
    for index, body in item_blocks.items():
        path_match = _ITEM_PATH_RE.search(body)
        if path_match is not None and path_match.group("path") in weapon_paths:
            weapon_entries.append((index, body, path_match.group("path")))

    shotgun_entries = [entry for entry in weapon_entries if entry[2] == COMBAT_SHOTGUN_PATH]
    if starting_weapon in (None, "Combat Shotgun"):
        vanilla_shotguns = [entry for entry in shotgun_entries if entry[0] < 8]
        if (
            len(vanilla_shotguns) != 1
            or vanilla_shotguns[0][0] != 5
            or "equip = true;" not in vanilla_shotguns[0][1]
        ):
            raise ValueError("DevInvLoadout vanilla Combat Shotgun must be exactly one equipped entry")
        if starting_weapon == "Combat Shotgun" and len(shotgun_entries) != 1:
            raise ValueError("DevInvLoadout Combat Shotgun must use vanilla entry without dynamic duplicate")
    elif any(index < 7 for index, _, _ in shotgun_entries):
        raise ValueError("DevInvLoadout non-shotgun starting weapon retained vanilla Combat Shotgun")

    dynamic_start = 8 if starting_weapon in (None, "Combat Shotgun") else 7
    dynamic_blocks = [body for index, body in item_blocks.items() if index >= dynamic_start]

    if starting_inventory is not None:
        expected: dict[tuple[str, str, tuple[str, ...]], int] = {}
        seen_dedupe: set[str] = set()
        for name, quantity in starting_inventory.items():
            entry = _entry_for_name(name, mapping)
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                raise ValueError(f"starting_inventory quantity must be positive integer: {name!r}")
            if starting_weapon == name:
                raise ValueError(f"starting weapon is duplicated in starting_inventory: {name!r}")
            for field, path, flags in _materialized_representations(name, quantity, mapping, seen_dedupe):
                expected[(field, path, flags)] = expected.get((field, path, flags), 0) + 1
        actual: dict[tuple[str, str, tuple[str, ...]], int] = {}
        for body in dynamic_blocks:
            field_match = re.search(r"\b(item|perk) = \"([^\"]+)\";", body)
            if field_match is None:
                raise ValueError("DevInvLoadout dynamic entry has no item or perk")
            flags = tuple(sorted(flag for flag in DEVINV_ALLOWED_FLAGS if f"{flag} = true;" in body))
            key = (field_match.group(1), field_match.group(2), flags)
            actual[key] = actual.get(key, 0) + 1
        for key, count in expected.items():
            if actual.get(key, 0) != count:
                raise ValueError(f"DevInvLoadout did not materialize representation {key!r} count {count}")
        if starting_weapon is not None and starting_weapon != "Combat Shotgun":
            weapon_representation = by_name[starting_weapon]["representations"][0]
            key = (weapon_representation["field"], weapon_representation["path"], ("equip",))
            expected[key] = expected.get(key, 0) + 1
            if actual.get(key, 0) != expected[key]:
                raise ValueError("DevInvLoadout resolved starting weapon was not materialized")
        unexpected = {key: count for key, count in actual.items() if count != expected.get(key, 0)}
        if unexpected:
            raise ValueError(f"DevInvLoadout contains unexpected dynamic representations: {unexpected!r}")

    selected_path = (
        by_name[starting_weapon]["representations"][0]["path"]
        if starting_weapon is not None
        else None
    )
    for body in dynamic_blocks:
        path_match = _ITEM_PATH_RE.search(body)
        if path_match is None:
            continue
        path = path_match.group("path")
        if path in weapon_paths and "equip = true;" in body and path != selected_path:
            raise ValueError("DevInvLoadout starting_inventory weapon must not be equipped")
    equipped_weapons = [body for _, body, _ in weapon_entries if "equip = true;" in body]
    if len(equipped_weapons) != 1:
        raise ValueError("DevInvLoadout must contain exactly one equipped weapon")
    if starting_weapon is not None:
        selected_entries = [entry for entry in weapon_entries if entry[2] == selected_path]
        equipped_selected = sum("equip = true;" in entry[1] for entry in selected_entries)
        if len(selected_entries) != 1 or equipped_selected != 1:
            raise ValueError("DevInvLoadout resolved starting_weapon must have exactly one equipped ownership entry")


def output_path_for_map(mod_root: Path, registry_path: Path, map_key: str) -> Path:
    """Place the DECL in the resource archive that owns the selected map."""
    registry = load_map_registry(registry_path)
    try:
        resource_path = registry["maps"][map_key]["resource_path"]
    except KeyError as error:
        raise ValueError(f"DevInvLoadout map is absent from registry: {map_key}") from error
    return mod_root / Path(resource_path).stem / SOURCE_PATH


TAG_FORBIDDEN_MASTERIES = frozenset({
    "perk/player/weapons/shotgun/pop_rocket_more_bombs",
    "perk/player/weapons/shotgun/secondary_full_auto_ammo_giveback",
    "perk/player/weapons/heavy_cannon/bolt_action_mastery_upgrades",
    "perk/player/weapons/heavy_cannon/burst_detonate_mastery",
    "perk/player/weapons/plasma_rifle/secondary_aoe_mastery",
    "perk/player/weapons/plasma_rifle/secondary_microwave_mastery",
    "perk/player/weapons/rocket_launcher/detonate_explosive_array_horizontal",
    "perk/player/weapons/rocket_launcher/detonate_mastery",
    "perk/player/weapons/rocket_launcher/lockon_mastery",
    "perk/player/weapons/double_barrel/meat_hook_mastery",
    "perk/player/weapons/gauss_cannon/ballista_mastery",
    "perk/player/weapons/gauss_cannon/destroyer_charge_levels",
    "perk/player/weapons/chaingun/turret_mastery",
    "perk/player/weapons/chaingun/energy_shell_mastery",
})

TAG_REQUIRED_NORMAL_MOD_UPGRADES = frozenset({
    "perk/player/weapons/shotgun/pop_rocket_weakpoint_hit",
    "perk/player/weapons/shotgun/pop_rocket_faster_recharge",
    "perk/player/weapons/shotgun/pop_rocket_larger_explosion",
    "perk/player/weapons/shotgun/secondary_full_auto_faster_recovery",
    "perk/player/weapons/shotgun/secondary_full_auto_faster_charge",
    "perk/player/weapons/shotgun/secondary_full_auto_increased_movement_speed",
    "perk/player/weapons/heavy_cannon/bolt_action_faster_movement",
    "perk/player/weapons/heavy_cannon/bolt_action_faster_reload",
    "perk/player/weapons/heavy_cannon/burst_detonate_faster_charge",
    "perk/player/weapons/heavy_cannon/burst_detonate_primary_charge",
    "perk/player/weapons/heavy_cannon/burst_detonate_faster_recharge",
    "perk/player/weapons/plasma_rifle/secondary_aoe_no_primary_delay",
    "perk/player/weapons/plasma_rifle/secondary_aoe_faster_charge",
    "perk/player/weapons/plasma_rifle/secondary_microwave_faster_charge",
    "perk/player/weapons/plasma_rifle/secondary_microwave_max_range",
    "perk/player/weapons/rocket_launcher/detonate_proximity_flare",
    "perk/player/weapons/rocket_launcher/detonate_concussive",
    "perk/player/weapons/rocket_launcher/lockon_faster_recovery",
    "perk/player/weapons/rocket_launcher/lockon_decrease_lock_time",
    "perk/player/weapons/double_barrel/meat_hook_faster_reload",
    "perk/player/weapons/double_barrel/default_faster_reload",
    "perk/player/weapons/gauss_cannon/ballista_movement",
    "perk/player/weapons/gauss_cannon/ballista_larger_explosion",
    "perk/player/weapons/gauss_cannon/destroyer_charge_levels_aoe",
    "perk/player/weapons/gauss_cannon/destroyer_faster_charge_and_recovery",
    "perk/player/weapons/chaingun/turret_faster_equip",
    "perk/player/weapons/chaingun/turret_faster_movement",
    "perk/player/weapons/chaingun/energy_shell_faster_recharge",
    "perk/player/weapons/chaingun/energy_shell_dash_smash",
})


TAG_FORBIDDEN_AP_ITEMS = frozenset({
    "ability_dash",
    "weapon/player/shotgun",
    "weapon/player/heavy_cannon",
    "weapon/player/plasma_rifle",
    "weapon/player/rocket_launcher",
    "weapon/player/double_barrel",
    "weapon/player/gauss_rifle",
    "weapon/player/chaingun",
    "weapon/player/bfg",
    "weapon/player/unmaykr",
    "weapon/player/chainsaw",
    "weapon/player/equipment_flame_belch",
    "weapon/player/equipment_flame_belch_right",
    "equipmentlauncher/equipmentlauncherleft",
    "throwable/player/frag_grenade",
    "throwable/player/ice_bomb",
    "ammo/sharedammopool/bfg",
})

TAG_FORBIDDEN_AP_PERKS = frozenset({
    "perk/player/blood_punch/base",
    "perk/player/blood_punch/area_of_effect",
    "perk/player/blood_punch/ai_charge_rate",
    "perk/player/blood_punch/max_charges",
    "perk/player/weapons/shotgun/pop_rocket",
    "perk/player/weapons/shotgun/secondary_full_auto",
    "perk/player/weapons/heavy_cannon/bolt_action",
    "perk/player/weapons/heavy_cannon/burst_detonate",
    "perk/player/weapons/plasma_rifle/secondary_aoe",
    "perk/player/weapons/plasma_rifle/secondary_microwave",
    "perk/player/weapons/rocket_launcher/detonate",
    "perk/player/weapons/rocket_launcher/lock_on",
    "perk/player/weapons/gauss_cannon/ballista",
    "perk/player/weapons/gauss_cannon/destroyer",
    "perk/player/weapons/chaingun/turret",
    "perk/player/weapons/chaingun/energy_shell",
})


def validate_tag_devinv_source(source: str) -> None:
    """Validate that TAG DevInv loadout contains normal mod upgrades and NO masteries."""
    for marker in (
        "startingInventory", "currencyToGive", "CURRENCY_PRAETOR_UPGRADE",
        "STAT_SUIT_PAGE_UNLOCKED", "STAT_RUNE_PAGE_UNLOCKED",
        "clearAllBeforeApply = true;",
    ):
        if marker not in source:
            raise ValueError(f"TAG DevInvLoadout missing required marker: {marker}")
    if re.search(r"^[ \t]*inherit\s*=", source, re.MULTILINE):
        raise ValueError("TAG DevInv output retained inheritance")
    _assert_codex_dossier(source)

    match = _STARTING_INVENTORY_RE.search(source)
    if match is None:
        raise ValueError("TAG DevInvLoadout startingInventory block is missing")
    num_match = re.search(r"\t\t\tnum = (?P<num>\d+);", match.group("body"))
    item_matches = list(_ITEM_BLOCK_RE.finditer(match.group("body")))
    if num_match is None:
        raise ValueError("TAG DevInvLoadout startingInventory num is missing")
    declared_num = int(num_match.group("num"))
    indices = [int(m.group("index")) for m in item_matches]
    if indices != list(range(declared_num)):
        raise ValueError("TAG DevInvLoadout startingInventory indices are not contiguous")

    perk_paths = set()
    item_paths = set()
    for m in item_matches:
        body = m.group("body")
        perk_m = _PERK_PATH_RE.search(body)
        if perk_m:
            perk_paths.add(perk_m.group("path"))
        item_m = _ITEM_PATH_RE.search(body)
        if item_m:
            item_paths.add(item_m.group("path"))

    forbidden_items = item_paths & TAG_FORBIDDEN_AP_ITEMS
    if forbidden_items:
        raise ValueError(f"TAG DevInvLoadout contains forbidden AP items: {forbidden_items}")

    forbidden_perks = perk_paths & (TAG_FORBIDDEN_MASTERIES | TAG_FORBIDDEN_AP_PERKS)
    if forbidden_perks:
        raise ValueError(f"TAG DevInvLoadout contains forbidden AP perks: {forbidden_perks}")

    for p in perk_paths:
        if p.startswith("perk/player/argent/") or p.startswith("perk/player/runes/") or p.startswith("perk/player/suit/"):
            raise ValueError(f"TAG DevInvLoadout contains forbidden AP perk: {p}")

    missing_upgrades = TAG_REQUIRED_NORMAL_MOD_UPGRADES - perk_paths
    if missing_upgrades:
        raise ValueError(f"TAG DevInvLoadout missing required mod upgrades: {missing_upgrades}")


def build_tag_devinv_overrides(
    starting_inventory: Mapping[str, int] | None = None,
    starting_weapon: str | None = None,
) -> dict[str, str]:
    """Build exact TAG archive overrides from project-owned declaration inputs."""
    manifest = json.loads(TAG_DEVINV_MANIFEST.read_text(encoding="utf-8"))
    result: dict[str, str] = {}
    tag_root_record = manifest["declarations"]["e4m1_rig"]
    tag_root_path = Path(__file__).resolve().parents[2] / tag_root_record["source"]
    tag_root_source = tag_root_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    baseline_inventory = _STARTING_INVENTORY_ANY_RE.search(tag_root_source)
    baseline_currency = _CURRENCY_BLOCK_RE.search(tag_root_source)
    if baseline_inventory is None or baseline_currency is None:
        raise ValueError("TAG root DevInv lacks replaceable loadout blocks")
    baseline_inventory_block = (
        "\t\tstartingInventory = {\n"
        + baseline_inventory.group("body")
        + "\n\t\t}"
    )
    baseline_currency_block = (
        "\t\tcurrencyToGive = {\n"
        + baseline_currency.group("body")
        + "\n\t\t}"
    )
    for declaration_key, record in manifest["declarations"].items():
        source_path = Path(__file__).resolve().parents[2] / record["source"]
        source_bytes = source_path.read_bytes()
        if hashlib.sha256(source_bytes).hexdigest() != record["sha256"]:
            raise ValueError(f"TAG DevInv source hash drifted: {declaration_key}")
        source = source_bytes.decode("utf-8").replace("\r\n", "\n")
        source_inventory = _STARTING_INVENTORY_ANY_RE.search(source)
        source_currency = _CURRENCY_BLOCK_RE.search(source)
        # Preserve declaration framing and unrelated fields; replace only loadout data.
        if source_inventory is None:
            edit_start = source.find("\tedit = {\n")
            edit_end = source.find("\n\t}", edit_start + 1)
            if edit_start < 0 or edit_end < 0:
                raise ValueError(f"TAG DevInv source lacks editable loadout block: {declaration_key}")
            override = (
                source[: edit_start + len("\tedit = {\n")]
                + baseline_inventory_block
                + "\n"
                + source[edit_start + len("\tedit = {\n") :]
            )
        else:
            override = (
                source[: source_inventory.start()]
                + baseline_inventory_block
                + source[source_inventory.end() :]
            )
        source_currency = _CURRENCY_BLOCK_RE.search(override)
        if source_currency is not None:
            override = (
                override[: source_currency.start("body")]
                + baseline_currency.group("body")
                + override[source_currency.end("body") :]
            )
        inherited_fields = []
        if "currencyToGive = {" not in override:
            inherited_fields.append(baseline_currency_block)
        if "STAT_SUIT_PAGE_UNLOCKED" not in override:
            inherited_fields.append(PAGE_STATS_BLOCK.rstrip("\n"))
        if "clearAllBeforeApply = true;" not in override:
            inherited_fields.append("\t\tclearAllBeforeApply = true;")
        if inherited_fields:
            edit_end = override.rfind("\n\t}")
            if edit_end < 0:
                raise ValueError(f"TAG DevInv source has no editable declaration: {declaration_key}")
            override = (
                override[:edit_end]
                + "\n"
                + "\n".join(inherited_fields)
                + override[edit_end:]
            )
        codex_match = _CODEX_ENTRIES_RE.search(override)
        if codex_match is None:
            override = override.replace(
                "\t\tclearAllBeforeApply = true;",
                "\t\tclearAllBeforeApply = true;\n" + CODEX_DOSSIER_BLOCK.rstrip("\n"),
                1,
            )
        else:
            override = (
                override[:codex_match.start()]
                + CODEX_DOSSIER_BLOCK.rstrip("\n")
                + override[codex_match.end():]
            )
        # Runtime loads one self-contained declaration per archive.  The
        # extracted inheritance chain is provenance, not a package dependency.
        override = _INHERIT_DECL_RE.sub("", override)
        if re.search(r"^[ \t]*inherit\s*=", override, re.MULTILINE):
            raise ValueError(f"TAG DevInv output retained inheritance: {declaration_key}")
        validate_tag_devinv_source(override)
        archive = Path(record["archive"])
        map_key = record["map_key"]
        result[(archive.stem + "/" + TAG_DEVINV_DECL_PATH.format(map_key=map_key))] = override
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mod-root", type=Path, required=True,
                        help="Root of the unpacked mod directory")
    parser.add_argument("--audit-output", type=Path, required=True,
                        help="Path to write audit JSON")
    parser.add_argument("--map-registry", type=Path,
                        default=Path(__file__).resolve().parents[2] / "data" / "map_sources.json")
    parser.add_argument("--map-key", default=OUTPUT_MAP_KEY)
    parser.add_argument("--slot-data", type=Path,
                        help="Resolved slot_data JSON containing starting_inventory and starting_weapon")
    args = parser.parse_args()
    slot_data = json.loads(args.slot_data.read_text(encoding="utf-8")) if args.slot_data else {}
    override = build_devinv_loadout(
        starting_inventory=slot_data.get("starting_inventory", {}),
        starting_weapon=slot_data.get("starting_weapon"),
    )

    output_path = output_path_for_map(args.mod_root, args.map_registry, args.map_key)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(override, encoding="utf-8")

    audit = {
        "source_path": SOURCE_PATH,
        "source_sha256": SOURCE_SHA256,
        "map_key": args.map_key,
        "resource_container": output_path.relative_to(args.mod_root).parts[0],
        "logical_decl": "devinvloadout/sp/e1m1",
        "output_path": output_path.as_posix(),
        "output_sha256": hashlib.sha256(override.encode("utf-8")).hexdigest(),
        "stats_to_give": ["STAT_SUIT_PAGE_UNLOCKED", "STAT_RUNE_PAGE_UNLOCKED"],
        "clearAllBeforeApply_preserved": True,
        "currencyToGive_preserved": True,
    }
    args.audit_output.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    print(f"DevInvLoadout patched: {output_path}")


if __name__ == "__main__":
    main()
