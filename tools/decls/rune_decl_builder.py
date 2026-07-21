#!/usr/bin/env python3
"""Build the hash-locked existing Rune menu gate override.

This is intentionally Rune-only. Challenge and Mastery DECL overrides are
not part of the v0.2.2a safe baseline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
RUNE_OWNER = {
    "container": "gameresources_patch1",
    "path": "menuelement/hud/dossier/runes.decl",
    "sha256": "889f165b788c2b06d907f762d3ac58ac35e12defef49d756e4e6401cd0169732",
}
GATE_LINE = '\t\tgatedStat = "STAT_RUNE_PAGE_UNLOCKED";\n'


def build_rune_override(mod_root: Path) -> dict:
    source = (
        ROOT / "vanilla_decls" / "owners" / RUNE_OWNER["container"] /
        "generated" / "decls" / RUNE_OWNER["path"]
    )
    payload = source.read_bytes()
    actual_hash = hashlib.sha256(payload).hexdigest()
    if actual_hash != RUNE_OWNER["sha256"]:
        raise ValueError(f"Rune owner hash drift: {actual_hash}")
    text = payload.decode("utf-8")
    if text.count(GATE_LINE) != 1:
        raise ValueError("Rune owner must contain exactly one existing menu gate")
    target = mod_root / RUNE_OWNER["container"] / "generated" / "decls" / RUNE_OWNER["path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.replace(GATE_LINE, "", 1), encoding="utf-8")
    return {"owner": RUNE_OWNER, "written_path": target.as_posix()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mod-root", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    args = parser.parse_args()
    audit = build_rune_override(args.mod_root)
    args.audit_output.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
