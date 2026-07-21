#!/usr/bin/env python3
"""Build the hash-locked Doom Slayer Toy owner override prototype."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent.parent
OWNER = {
    "container": "e1m1_intro",
    "path": "propitem/propitem/collectible/toys/doom_slayer.decl",
    "source_sha256": "b9dcd55363e92bd918245d2adf57794fc66169505c5c3d1e73342543f4f69933",
    "normalized_snapshot_sha256": "1c221458498ddde1df81ab8cf50a24d5c4d3671fd1227a819bd696cd5881230b",
}
PARENT_OWNERS = (
    {
        "path": "propitem/propitem/collectible/toys/default.decl",
        "source_sha256": "bae6545c08d79a3f67bbc750d4bd38c217d6ce31a1e76fc805a162d16f5e4233",
        "normalized_snapshot_sha256": "e571e82a3059d05d734a78bf5320b4f0ac0360b22ad921d57282536b88743fe5",
        "required": (
            'inherit = "propitem/collectible/default";',
            "inventoryCount = 1;",
            "xp = 20;",
        ),
    },
    {
        "path": "propitem/propitem/collectible/default.decl",
        "source_sha256": "80705f4c65e6df451347196dd60c65adad12201d24434bfa39d5b40dd8cc6b6f",
        "normalized_snapshot_sha256": "3a7b87f0a274863538a142d0708353b796b4f6b18bdc10f904e9990e8f9469fa",
        "required": (
            'componentTypeInfo = "idUseableItemComponent";',
            'use_statIncrease = "STAT_COLLECTIBLE_01_FOUND";',
            "xp = 1;",
        ),
    },
)
EDIT_OPEN = "\tedit = {\n"
XP_OVERRIDE = "\t\txp = 0;\n"


def build_toy_override(mod_root: Path) -> dict:
    owner_root = (
        ROOT / "data" / "automap_native_decl_sources" / OWNER["container"] /
        "generated" / "decls"
    )
    for parent in PARENT_OWNERS:
        parent_payload = (owner_root / parent["path"]).read_bytes()
        parent_hash = hashlib.sha256(parent_payload).hexdigest()
        if parent_hash != parent["normalized_snapshot_sha256"]:
            raise ValueError(
                f"Doom Slayer Toy parent owner hash drift for "
                f"{parent['path']}: {parent_hash}"
            )
        parent_text = parent_payload.decode("utf-8")
        if any(parent_text.count(value) != 1 for value in parent["required"]):
            raise ValueError(
                f"Doom Slayer Toy parent reward chain drift for {parent['path']}"
            )
    source = (
        owner_root / OWNER["path"]
    )
    payload = source.read_bytes()
    actual_hash = hashlib.sha256(payload).hexdigest()
    if actual_hash != OWNER["normalized_snapshot_sha256"]:
        raise ValueError(f"Doom Slayer Toy owner hash drift: {actual_hash}")
    text = payload.decode("utf-8")
    required = (
        'inherit = "propitem/collectible/toys/default";',
        'inventoryDecl = "collectible/toys/doom_slayer";',
        'collectible = "toys/doomguy";',
    )
    if text.count(EDIT_OPEN) != 1 or any(text.count(value) != 1 for value in required):
        raise ValueError("Doom Slayer Toy owner structure drift")
    if "xp" in text or "inventoryCount" in text:
        raise ValueError("Doom Slayer Toy leaf unexpectedly owns inherited reward fields")
    override = text.replace(EDIT_OPEN, EDIT_OPEN + XP_OVERRIDE, 1)
    if override.replace(XP_OVERRIDE, "", 1) != text or override.count("xp = 0;") != 1:
        raise ValueError("Doom Slayer Toy override changed more than inherited XP")
    for value in required[1:]:
        if override.count(value) != 1:
            raise ValueError("Doom Slayer Toy cosmetic/Dossier state was not preserved")

    target = (
        mod_root / OWNER["container"] / "generated" / "decls" / OWNER["path"]
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(override, encoding="utf-8")
    return {
        "owner": OWNER,
        "parent_owners": PARENT_OWNERS,
        "reward_cut": {"field": "xp", "inherited_value": 20, "override_value": 0},
        "preserved_cosmetic_state": [
            "inventoryDecl=collectible/toys/doom_slayer",
            "collectible=toys/doomguy",
        ],
        "written_path": target.as_posix(),
        "runtime_status": "pending",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mod-root", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    args = parser.parse_args()
    audit = build_toy_override(args.mod_root)
    args.audit_output.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
