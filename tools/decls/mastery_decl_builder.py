#!/usr/bin/env python3
"""Build hash-locked, reward-free overrides for proven vanilla Weapon Masteries."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from challenge_registry import load_challenge_registry


ROOT = Path(__file__).resolve().parent.parent.parent
OWNER = "gameresources"
PERK_TO_GIVE = '\t\tperkToGive = "perk/player/weapons/shotgun/pop_rocket_more_bombs";\n'
ADD_STATS = """\t\taddStats = {
\t\t\tnum = 3;
\t\t\titem[0] = "STAT_CURRENT_MASTERIES_AQUIRED";
\t\t\titem[1] = "STAT_SHOTGUN_STICKY_BOMB_MASTERY_EARNED";
\t\t\titem[2] = "STAT_SHOTGUN_MASTERED";
\t\t}
"""


def _masteries() -> list[dict]:
    return load_challenge_registry()["weapon_masteries"]


def _locked_decl(entry: dict, kind: str) -> dict:
    return entry["decls"][kind]


# Kept as public compatibility names for focused Sticky regression tests.
STICKY_DECLS = {
    "unlockable": _locked_decl(_masteries()[0], "unlockable"),
    "perk": _locked_decl(_masteries()[0], "perk"),
}


def _read_locked_source(entry: dict, kind: str) -> str:
    locked = _locked_decl(entry, kind)
    source = ROOT / "vanilla_decls" / "owners" / OWNER / "generated" / "decls" / locked["path"]
    payload = source.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != locked["sha256"]:
        raise ValueError(f"Mastery vanilla owner hash drift for {locked['path']}: {actual}")
    return payload.decode("utf-8")


def _remove_single_block(source: str, field: str, entry_name: str) -> str:
    marker = f"\t\t{field} = {{"
    start = source.find(marker)
    if start < 0 or source.find(marker, start + 1) >= 0:
        raise ValueError(f"{entry_name}: {field} block is missing or ambiguous")
    cursor = start + len(marker)
    depth = 1
    while cursor < len(source) and depth:
        if source[cursor] == "{":
            depth += 1
        elif source[cursor] == "}":
            depth -= 1
        cursor += 1
    if depth:
        raise ValueError(f"{entry_name}: {field} block is unclosed")
    if cursor < len(source) and source[cursor] == "\n":
        cursor += 1
    return source[:start] + source[cursor:]


def _assert_proven_observer(masteries: list[dict]) -> None:
    if len(masteries) != 13:
        raise ValueError("refusing to strip rewards without the complete base mastery registry")
    bridge = (ROOT / "bridge_client.py").read_text(encoding="utf-8")
    required = (
        "check_weapon_mastery_locations",
        "read_weapon_mastery_records",
        "mastery_save_file",
        "UnlockableManager_0_1_2",
        "unlockableIsUnlocked",
    )
    if not all(token in bridge for token in required):
        raise ValueError("refusing to strip mastery rewards without save reader/send path")


def _reward_free_override(entry: dict) -> tuple[str, str]:
    unlockable = _read_locked_source(entry, "unlockable")
    perk = _read_locked_source(entry, "perk")
    perk_line = f'\t\tperkToGive = "{entry["gameplay_perk"]}";\n'
    if unlockable.count(perk_line) != 1:
        raise ValueError(f"{entry['name']}: native perkToGive edge is missing or ambiguous")
    unlockable = unlockable.replace(perk_line, "", 1)
    perk = _remove_single_block(perk, "addStats", entry["name"])
    if "perkToGive" in unlockable:
        raise ValueError(f"{entry['name']}: natural gameplay reward remains active")
    if "addStats" in perk or "upgrades" not in perk:
        raise ValueError(f"{entry['name']}: AP perk is not gameplay-only")
    return unlockable, perk


def build_mastery_overrides(mod_root: Path) -> dict:
    masteries = _masteries()
    _assert_proven_observer(masteries)
    written_paths = []
    for entry in masteries:
        unlockable, perk = _reward_free_override(entry)
        outputs = {
            _locked_decl(entry, "unlockable")["path"]: unlockable,
            _locked_decl(entry, "perk")["path"]: perk,
        }
        for relative, text in outputs.items():
            target = mod_root / OWNER / "generated" / "decls" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            written_paths.append(target.as_posix())
    if len(written_paths) != 26 or len(written_paths) != len(set(written_paths)):
        raise ValueError("base mastery override set is incomplete or overlapping")
    return {
        "owner": OWNER,
        "mastery_count": len(masteries),
        "location_ids": [entry["location_id"] for entry in masteries],
        "item_ids": [entry["item_id"] for entry in masteries],
        "written_paths": written_paths,
    }


def build_sticky_overrides(mod_root: Path) -> dict:
    """Compatibility wrapper; full catalogue is now one atomic override set."""
    return build_mastery_overrides(mod_root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mod-root", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    args = parser.parse_args()
    audit = build_mastery_overrides(args.mod_root)
    args.audit_output.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
