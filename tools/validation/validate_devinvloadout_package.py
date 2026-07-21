#!/usr/bin/env python3
"""Validate the E1M1 DevInvLoadout archive placement in an unpacked mod."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from map_registry import load_map_registry


DECL_PREFIX = Path("generated/decls/devinvloadout")


def expected_decl_path(mod_root: Path, registry_path: Path, generated_map: Path) -> Path:
    registry = load_map_registry(registry_path)
    resource_path = registry["maps"]["e1m1_intro"]["resource_path"]
    world = generated_map.read_text(encoding="utf-8")[:4096]
    match = re.search(r'devmapInvLoadout\s*=\s*"([^"]+)";', world)
    if match is None:
        raise ValueError("E1M1 game world has no devmapInvLoadout")
    logical_decl = match.group(1)
    return mod_root / Path(resource_path).stem / DECL_PREFIX / f"{logical_decl}.decl"


def validate(mod_root: Path, registry_path: Path, generated_map: Path) -> None:
    expected = expected_decl_path(mod_root, registry_path, generated_map)
    logical_suffix = expected.relative_to(mod_root).parts[1:]
    candidates = [
        path for path in mod_root.rglob("e1m1.decl")
        if path.relative_to(mod_root).parts[1:] == logical_suffix
    ]
    if candidates != [expected]:
        raise AssertionError(f"expected exactly one E1M1 DevInvLoadout at {expected}, got {candidates}")
    legacy = mod_root / "gameresources" / Path(*logical_suffix)
    if legacy.exists():
        raise AssertionError(f"DevInvLoadout must not remain in gameresources: {legacy}")

    text = expected.read_text(encoding="utf-8")
    for marker in (
        'STAT_SUIT_PAGE_UNLOCKED',
        'STAT_RUNE_PAGE_UNLOCKED',
        'startingInventory',
        'currencyType = "CURRENCY_PRAETOR_UPGRADE"',
        'count = 0;',
        'clearAllBeforeApply = true;',
    ):
        if marker not in text:
            raise AssertionError(f"DevInvLoadout lost required marker: {marker}")
    if 'STAT_CHALLENGE_PAGE_UNLOCKED' in text:
        raise AssertionError("DevInvLoadout unexpectedly unlocks Challenge Page")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mod-root", required=True, type=Path)
    parser.add_argument("--map-registry", required=True, type=Path)
    parser.add_argument("--generated-map", required=True, type=Path)
    args = parser.parse_args()
    validate(args.mod_root, args.map_registry, args.generated_map)


if __name__ == "__main__":
    main()
