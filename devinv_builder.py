#!/usr/bin/env python3
"""Patch DevInvLoadout to pre-unlock Suit and Rune pages on new saves.

Adds statsToGive to the e1m1_intro DevInvLoadout, matching the proven pattern
from retail dlc/e4m1_rig.decl: page-unlocked with zero corresponding currency.
Only fires at new-save creation per the DevInvLoadout lifecycle; does not
affect existing saves, checkpoint reload, or reconnect.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

SOURCE_OWNER = "gameresources"
SOURCE_PATH = "generated/decls/devinvloadout/devinvloadout/sp/e1m1.decl"
SOURCE_SHA256 = "c68c18750a4267b43d4ffd6e32b67dbed6af1c86099b947ddca9b98f2187a824"

PAGE_STATS_BLOCK = """\t\tstatsToGive = {
\t\t\tnum = 2;
\t\t\titem[0] = "STAT_SUIT_PAGE_UNLOCKED";
\t\t\titem[1] = "STAT_RUNE_PAGE_UNLOCKED";
\t\t}
"""

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


def _load_vanilla(vanilla_root: Path) -> str:
    source = vanilla_root / SOURCE_OWNER / SOURCE_PATH
    payload = source.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != SOURCE_SHA256:
        raise ValueError(
            f"DevInvLoadout vanilla source hash drift: expected {SOURCE_SHA256}, "
            f"got {actual}"
        )
    text = payload.decode("utf-8")
    return text.replace("\r\n", "\n")


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mod-root", type=Path, required=True,
                        help="Root of the unpacked mod directory")
    parser.add_argument("--audit-output", type=Path, required=True,
                        help="Path to write audit JSON")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    vanilla_root = script_dir / "vanilla_decls" / "owners"

    source = _load_vanilla(vanilla_root)
    _assert_source_integrity(source)
    override = _patch(source)

    output_path = args.mod_root / SOURCE_OWNER / SOURCE_PATH
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(override, encoding="utf-8")

    audit = {
        "source_path": SOURCE_PATH,
        "source_sha256": SOURCE_SHA256,
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
