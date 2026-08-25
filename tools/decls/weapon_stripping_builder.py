#!/usr/bin/env python3
"""Build permanent, hash-locked weapon-ownership DECL overrides.

Only ownership payloads are changed. Cinematic graphs, timelines, objectives,
codex rewards, ammo transactions, and use-weapon hand choreography remain
native. Map pickup entities are neutralized by locations.json through the
canonical map generator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT.parent

OWNERS = (
    {
        "name": "ssg_revenant_cinematic",
        "container": "e1m3_cult_patch3",
        "source": WORKSPACE / "sp_hub_decl_analysis_20260719_140903" / "decls" / "game" / "sp" / "e1m3_cult" / "e1m3_cult_patch3" / "generated" / "decls" / "logicentity" / "maps" / "game" / "sp" / "e1m3_cult" / "e1m3_cult_cinematic" / "cinematic_info_logic_revenant_gives_shotgun.decl",
        "path": "logicentity/maps/game/sp/e1m3_cult/e1m3_cult_cinematic/cinematic_info_logic_revenant_gives_shotgun.decl",
        "sha256": "e5c3a491dfe4b4d6b2b44608afacfa287c036877397c12e7dd95665783f384f0",
        "inventory_item": "weapon/player/double_barrel",
        "desired_count": "1",
    },
    {
        "name": "bfg10k_cinematic",
        "container": "e2m3_core_patch2",
        "source": WORKSPACE / "sp_hub_decl_analysis_20260719_140903" / "decls" / "game" / "sp" / "e2m3_core" / "e2m3_core_patch2" / "generated" / "decls" / "logicentity" / "maps" / "game" / "sp" / "e2m3_core" / "e2m3_core_cinematic" / "cinematic_info_logic_bfg10k_firing.decl",
        "path": "logicentity/maps/game/sp/e2m3_core/e2m3_core_cinematic/cinematic_info_logic_bfg10k_firing.decl",
        "sha256": "55a5ea694653ae6091360b95a099edc76c71d0ec3c61b9fa89f115ca9fadc300",
        "inventory_item": "weapon/player/bfg",
        "desired_count": "1",
    },
    {
        "name": "crucible_cinematic",
        "container": "e3m1_slayer_patch2",
        "source": WORKSPACE / "sp_hub_decl_analysis_20260719_140903" / "decls" / "game" / "sp" / "e3m1_slayer" / "e3m1_slayer_patch2" / "generated" / "decls" / "logicentity" / "maps" / "game" / "sp" / "e3m1_slayer" / "e3m1_slayer_cinematic_crucible" / "cinematic_crucible_info_logic.decl",
        "path": "logicentity/maps/game/sp/e3m1_slayer/e3m1_slayer_cinematic_crucible/cinematic_crucible_info_logic.decl",
        "sha256": "71693874af8425a398615587c82fa253175766315a16c571ae23d04a7a4b51e2",
        "inventory_item": "weapon/player/crucible",
        "desired_count": "1",
    },
)

SSG_WEAPON = {
    "name": "ssg_meat_hook_grant",
    "container": "gameresources",
    "source": WORKSPACE / "Tools" / "gameresources_decl_analysis_20260710_201519" / "files" / "generated" / "decls" / "weapon" / "weapon" / "player" / "double_barrel.decl",
    "path": "weapon/weapon/player/double_barrel.decl",
    "sha256": "5655770c5e74ef49eaf7347775bd368d0702f68c2aeaf05a3f96781bdcedf31d",
    "inventory_item": "perk/player/weapons/double_barrel/meat_hook",
}

PAYLOAD_RE = re.compile(
    r'(className = "idLogicNodeModelPlayerModifyInventory";.*?'
    r'itemsToModify = \{\r?\n'
    r'(?P<indent>\s*)num = )(?P<num>\d+)(;\r?\n'
    r'\s*item\[0\] = \{\r?\n'
    r'\s*inventoryItem = "(?P<inventory_item>[^"]+)";\r?\n'
    r'\s*desiredCount = (?P<desired_count>-?\d+);)',
    re.DOTALL,
)


def _source_payload(owner: dict) -> bytes:
    if not owner["source"].is_file():
        raise ValueError(f"canonical DECL source missing: {owner['source']}")
    payload = owner["source"].read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != owner["sha256"]:
        raise ValueError(
            f"canonical DECL source hash drift for {owner['name']}: "
            f"expected {owner['sha256']}, got {actual}"
        )
    return payload


def _neutralize(owner: dict, payload: bytes) -> tuple[str, dict]:
    text = payload.decode("utf-8")
    matches = []
    for match in PAYLOAD_RE.finditer(text):
        if (
            match.group("inventory_item") == owner["inventory_item"]
            and match.group("desired_count") == owner["desired_count"]
        ):
            matches.append(match)
    if len(matches) != 1:
        raise ValueError(
            f"{owner['name']}: expected one ownership payload, found {len(matches)}"
        )
    match = matches[0]
    if match.group("num") != "1":
        raise ValueError(
            f"{owner['name']}: ownership payload count drifted: {match.group('num')}"
        )
    start, end = match.span("num")
    result = text[:start] + "0" + text[end:]
    before = text.splitlines()
    after = result.splitlines()
    changed = [
        (index + 1, left, right)
        for index, (left, right) in enumerate(zip(before, after))
        if left != right
    ]
    if len(before) != len(after) or len(changed) != 1:
        raise ValueError(f"{owner['name']}: override changed more than num=1 to num=0")
    line_number, old_line, new_line = changed[0]
    if old_line.strip() != "num = 1;" or new_line.strip() != "num = 0;":
        raise ValueError(f"{owner['name']}: unexpected ownership payload diff")
    return result, {
        "name": owner["name"],
        "container": owner["container"],
        "path": owner["path"],
        "source_sha256": owner["sha256"],
        "override_sha256": hashlib.sha256(result.encode("utf-8")).hexdigest(),
        "inventory_item": owner["inventory_item"],
        "changed_line": line_number,
        "before": old_line,
        "after": new_line,
    }


def _strip_ssg_meat_hook(payload: bytes) -> tuple[str, dict]:
    text = payload.decode("utf-8")
    pattern = re.compile(
        r'(givePerksOnReceive\s*=\s*\{\s*num\s*=\s*)1(\s*;\s*item\[0\]\s*=\s*"perk/player/weapons/double_barrel/meat_hook";)',
        re.DOTALL,
    )
    result, count = pattern.subn(r"\g<1>0\g<2>", text)
    if count != 1:
        raise ValueError(f"ssg_meat_hook_grant: expected one givePerksOnReceive payload, found {count}")
    before = text.splitlines()
    after = result.splitlines()
    changed = [(index + 1, left, right) for index, (left, right) in enumerate(zip(before, after)) if left != right]
    if len(before) != len(after) or len(changed) != 1:
        raise ValueError("ssg_meat_hook_grant: override changed more than perk payload count")
    line_number, old_line, new_line = changed[0]
    return result, {
        "name": SSG_WEAPON["name"],
        "container": SSG_WEAPON["container"],
        "path": SSG_WEAPON["path"],
        "source_sha256": SSG_WEAPON["sha256"],
        "override_sha256": hashlib.sha256(result.encode("utf-8")).hexdigest(),
        "inventory_item": SSG_WEAPON["inventory_item"],
        "changed_line": line_number,
        "before": old_line,
        "after": new_line,
    }


def build_weapon_stripping_overrides(mod_root: Path) -> dict:
    audits = []
    for owner in OWNERS:
        result, audit = _neutralize(owner, _source_payload(owner))
        target = mod_root / owner["container"] / "generated" / "decls" / owner["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(result, encoding="utf-8", newline="")
        audit["written_path"] = target.as_posix()
        audits.append(audit)
    result, audit = _strip_ssg_meat_hook(_source_payload(SSG_WEAPON))
    target = mod_root / SSG_WEAPON["container"] / "generated" / "decls" / SSG_WEAPON["path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(result, encoding="utf-8", newline="")
    audit["written_path"] = target.as_posix()
    audits.append(audit)
    if len({entry["written_path"] for entry in audits}) != len(audits):
        raise ValueError("weapon stripping override paths overlap")
    return {"schema_version": 1, "overrides": audits}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mod-root", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    args = parser.parse_args()
    audit = build_weapon_stripping_overrides(args.mod_root)
    args.audit_output.parent.mkdir(parents=True, exist_ok=True)
    args.audit_output.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
