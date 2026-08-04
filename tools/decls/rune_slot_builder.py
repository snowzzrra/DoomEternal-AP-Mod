#!/usr/bin/env python3
"""Build the Rune slot-threshold override."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


RUNE_SLOT_OWNER = {
    "container": "gameresources",
    "path": "entitydef/player.decl",
}

OVERRIDE_CONTENT = """\
{
\tedit = {
\t\truneManager = {
\t\t\truneSlotReq = {
\t\t\t\tptr = {
\t\t\t\t\tptr[0] = 0;
\t\t\t\t\tptr[1] = 0;
\t\t\t\t\tptr[2] = 0;
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}
"""


def build_rune_slot_override(mod_root: Path) -> dict:
    target = (
        mod_root / "gameresources_patch1" / "generated" / "decls"
        / RUNE_SLOT_OWNER["path"]
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(OVERRIDE_CONTENT, encoding="utf-8", newline="\r\n")
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
