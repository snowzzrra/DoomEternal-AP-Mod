#!/usr/bin/env python3
"""Build room-specific DevInvLoadout overrides from packaged canonical data."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path

from map_registry import load_map_registry

SOURCE_OWNER = "gameresources"
SOURCE_PATH = "generated/decls/devinvloadout/devinvloadout/sp/e1m1.decl"
SOURCE_SHA256 = "c68c18750a4267b43d4ffd6e32b67dbed6af1c86099b947ddca9b98f2187a824"
OUTPUT_MAP_KEY = "e1m1_intro"

PAGE_STATS_BLOCK = """\t\tstatsToGive = {
\t\t\tnum = 2;
\t\t\titem[0] = "STAT_SUIT_PAGE_UNLOCKED";
\t\t\titem[1] = "STAT_RUNE_PAGE_UNLOCKED";
\t\t}
"""

# This is the hash-locked retail E1M1 loadout.  Keep source in this module so
# room/package generation never depends on a local vanilla_decls checkout.
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

# Only representations exercised by canonical campaign DECLs are accepted.
# Values are (field, path, flags), where flags are emitted as DECL booleans.
SAFE_REPRESENTATIONS = {
    "Heavy Cannon": (("item", "weapon/player/heavy_cannon", ()),),
    "Plasma Rifle": (("item", "weapon/player/plasma_rifle", ()),),
    "Rocket Launcher": (("item", "weapon/player/rocket_launcher", ()),),
    "Super Shotgun": (("item", "weapon/player/double_barrel", ()),),
    "Ballista": (("item", "weapon/player/gauss_rifle", ()),),
    "Chaingun": (("item", "weapon/player/chaingun", ()),),
    "BFG-9000": (("item", "weapon/player/bfg", ()),),
    "Combat Shotgun": (("item", "weapon/player/shotgun", ()),),
    "The Unmaykr": (("item", "weapon/player/unmaykr", ()),),
    "Sentinel Hammer": (("item", "weapon/player/hammer", ()),),
    "Chainsaw": (("item", "weapon/player/chainsaw", ()),),
    "Dash": (("item", "ability_dash", ()),),
    "Blood Punch": (("perk", "perk/player/blood_punch/base", ("equip",)),),
    "Empyrean Key": (("item", "inventory/key", ()),),
    "Rune": (("item", "inventory/rune", ()),),
    "Frag Grenade": (
        ("item", "equipmentlauncher/equipmentlauncherleft", ()),
        ("item", "throwable/player/frag_grenade", ("forceStat", "equip")),
    ),
    "Flame Belch": (
        ("item", "equipmentlauncher/equipmentlauncherleft", ()),
        ("item", "weapon/player/equipment_flame_belch", ("equip",)),
    ),
    "Ice Bomb": (
        ("item", "equipmentlauncher/equipmentlauncherleft", ()),
        ("item", "throwable/player/ice_bomb", ("forceStat",)),
    ),
}

STARTING_WEAPON_NAMES = frozenset({
    "Heavy Cannon", "Plasma Rifle", "Rocket Launcher", "Super Shotgun", "Ballista", "Chaingun", "Combat Shotgun",
})

# Expected vanilla markers that must exist before patching
REQUIRED_MARKERS = frozenset({
    "clearAllBeforeApply",
    "currencyToGive",
    "startingInventory",
    "CURRENCY_PRAETOR_UPGRADE",
})

# Must NOT appear in the vanilla source (not already patched)
FORBIDDEN_MARKERS = frozenset({
    "STAT_SUIT_PAGE_UNLOCKED",
    "statsToGive",
})

COMBAT_SHOTGUN_PATH = "weapon/player/shotgun"
_STARTING_INVENTORY_RE = re.compile(
    r"\t\tstartingInventory = \{\n(?P<body>.*?)\n\t\t\}\n\t\tcurrencyToGive = \{",
    re.DOTALL,
)
_ITEM_BLOCK_RE = re.compile(
    r"\t\t\titem\[(?P<index>\d+)\] = \{\n(?P<body>.*?)\n\t\t\t\}",
    re.DOTALL,
)
_ITEM_PATH_RE = re.compile(r'\bitem = "(?P<path>[^"]+)";')


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

    return override


def _decl_item(field: str, path: str, flags: tuple[str, ...], index: int, count: int | None = None) -> str:
    lines = [f"\t\t\titem[{index}] = {{", f"\t\t\t\t{field} = \"{path}\";"]
    if count is not None:
        lines.append(f"\t\t\t\tcount = {count};")
    for flag in flags:
        lines.append(f"\t\t\t\t{flag} = true;")
    lines.extend(("\t\t\t}",))
    return "\n".join(lines)


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

    source = canonical_base()
    keep_vanilla_shotgun = starting_weapon in (None, "Combat Shotgun")
    if not keep_vanilla_shotgun:
        source = _remove_base_combat_shotgun(source)

    entries: list[str] = []
    index = 8 if keep_vanilla_shotgun else 7
    for name in sorted(starting_inventory):
        quantity = starting_inventory[name]
        if name not in SAFE_REPRESENTATIONS:
            raise ValueError(f"unsupported starting_inventory item: {name!r}")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ValueError(f"starting_inventory quantity must be positive integer: {name!r}")
        if starting_weapon == name:
            raise ValueError(f"starting weapon is duplicated in starting_inventory: {name!r}")
        for field, path, flags in SAFE_REPRESENTATIONS[name]:
            for _ in range(quantity):
                entries.append(_decl_item(field, path, flags, index))
                index += 1
    if starting_weapon is not None and starting_weapon != "Combat Shotgun":
        field, path, _ = SAFE_REPRESENTATIONS[starting_weapon][0]
        entries.append(_decl_item(field, path, ("equip",), index))
        index += 1

    if entries:
        marker = "\t\t}\n\t\tcurrencyToGive = {"
        if source.count(marker) != 1:
            raise ValueError("canonical DevInv startingInventory block is missing or ambiguous")
        block = "\n".join(entries) + "\n"
        source = source.replace(marker, block + "\t\t}\n\t\tcurrencyToGive = {", 1)
    source = source.replace("\t\t\tnum = 8;", f"\t\t\tnum = {index};", 1)
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

    weapon_paths = {
        SAFE_REPRESENTATIONS[name][0][1]
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
        for name, quantity in starting_inventory.items():
            representation = SAFE_REPRESENTATIONS.get(name)
            if representation is None:
                raise ValueError(f"unsupported starting_inventory item: {name!r}")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                raise ValueError(f"starting_inventory quantity must be positive integer: {name!r}")
            if starting_weapon == name:
                raise ValueError(f"starting weapon is duplicated in starting_inventory: {name!r}")
            for field, path, _ in representation:
                expected = sum(
                    body.count(f'{field} = "{path}";')
                    for body in dynamic_blocks
                )
                if expected != quantity:
                    raise ValueError(f"DevInvLoadout did not materialize {name!r} quantity {quantity}")

    selected_path = (
        SAFE_REPRESENTATIONS[starting_weapon][0][1]
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mod-root", type=Path, required=True,
                        help="Root of the unpacked mod directory")
    parser.add_argument("--audit-output", type=Path, required=True,
                        help="Path to write audit JSON")
    parser.add_argument("--map-registry", type=Path,
                        default=Path(__file__).resolve().parents[2] / "data" / "map_sources.json")
    parser.add_argument("--map-key", default=OUTPUT_MAP_KEY)
    args = parser.parse_args()

    override = build_devinv_loadout()

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
