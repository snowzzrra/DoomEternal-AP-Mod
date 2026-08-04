#!/usr/bin/env python3
"""Build the hash-locked native Rune slot-threshold override."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent.parent
RUNE_SLOT_OWNER = {
    "container": "gameresources",
    "path": "entitydef/player.decl",
    "sha256": "4f06051e78e1a4b4cac3ed2f7c34aade613627c35537e3f5fe10cc83e2949c95",
}
SOURCE_SLOT_REQUIREMENTS = """\t\t\t\truneSlotReq = {
\t\t\t\t\tptr = {
\t\t\t\t\t\tptr[0] = 1;
\t\t\t\t\t\tptr[1] = 2;
\t\t\t\t\t\tptr[2] = 3;
\t\t\t\t\t}
\t\t\t\t}
"""
PATCHED_SLOT_REQUIREMENTS = """\t\t\t\truneSlotReq = {
\t\t\t\t\tptr = {
\t\t\t\t\t\tptr[0] = 0;
\t\t\t\t\t\tptr[1] = 0;
\t\t\t\t\t\tptr[2] = 0;
\t\t\t\t\t}
\t\t\t\t}
"""


def build_rune_slot_override(mod_root: Path) -> dict:
    source = (
        ROOT / "assets" / "native_decl_sources" / "base"
        / "generated" / "decls" / RUNE_SLOT_OWNER["path"]
    )
    payload = source.read_bytes()
    actual_hash = hashlib.sha256(payload).hexdigest()
    if actual_hash != RUNE_SLOT_OWNER["sha256"]:
        raise ValueError(f"Rune slot owner hash drift: {actual_hash}")
    newline = "\r\n" if b"\r\n" in payload else "\n"
    source_thresholds = SOURCE_SLOT_REQUIREMENTS.replace("\n", newline).encode()
    patched_thresholds = PATCHED_SLOT_REQUIREMENTS.replace("\n", newline).encode()
    if payload.count(source_thresholds) != 1:
        raise ValueError("Rune slot owner must contain exactly one [1,2,3] threshold")
    patched = payload.replace(source_thresholds, patched_thresholds, 1)
    target = (
        mod_root / "gameresources_patch1" / "generated" / "decls"
        / RUNE_SLOT_OWNER["path"]
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(patched)
    return {
        "owner": RUNE_SLOT_OWNER,
        "source_requirements": [1, 2, 3],
        "patched_requirements": [0, 0, 0],
        "written_path": target.as_posix(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mod-root", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    args = parser.parse_args()
    audit = build_rune_slot_override(args.mod_root)
    args.audit_output.write_text(
        json.dumps(audit, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
