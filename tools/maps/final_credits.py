#!/usr/bin/env python3
"""Bypass final-credit nodes through their vanilla completion exits."""

import json
from pathlib import Path

from intro_skip import patch, sha


ROOT = Path(__file__).resolve().parents[2]
DECL = "generated/decls/logicentity/maps/game/"
ROUTES = {
    "final_sin_death": (
        "e3m4_boss_patch3", DECL + "sp/e3m4_boss/e3m4_boss_cinematic_icon_of_sin_death/icon_of_sin_death_cinematic_info_logic.decl",
        "755c2fb0227e9c6ed63c963431db3b407facbfe34f6984fe5e583a7fafd11a5c",
        (522942224, 3031889519, 3, 881189391, 0, 96874852, 0),
        ('binkMaterial = "video/credits/credits";', "e3m4_endoflevel_transition"),
    ),
    "final_sin_destroyer": (
        "e3m4_boss_patch3", DECL + "sp/e3m4_boss/e3m4_boss_cinematic_icon_destroyer/icon_destroyer_info_logic.decl",
        "e511f007155cc95391b65c8e9e17adb3253163bd759322316d045b13f05e22b9",
        (1465269036, 1893163630, 3, 2553478532, 0, 1098414917, 0),
        ('binkMaterial = "video/credits/credits";', "e3m4_endoflevel_transition"),
    ),
    "dark_lord": (
        "e5m4_boss_patch1", DECL + "dlc2/e5m4_boss/e5m4_boss_cinematics_death_of_the_darklord/death_of_the_darklord_info_logic.decl",
        "9994dce950ab4eb97f95dacc540581283723e35e50992713501a2a1ae0c24aba",
        (2938577565, 3661270880, 1, 1937119747, 0, 302987093, 0),
        ('className = "idLogicNodeModelRollCredits";', 'credits = "dlc2";',
         "stage_3_target_campaign_complete_1"),
    ),
}


def main():
    recipes = []
    for name, (owner, member, expected, edge, required) in ROUTES.items():
        source = ROOT / "vanilla_decls/owners" / owner / member
        data = source.read_bytes()
        if sha(data) != expected:
            raise ValueError(f"{name}: winning vanilla SHA mismatch")
        before = data.decode("utf-8")
        if any(marker not in before for marker in required):
            raise ValueError(f"{name}: completion path missing")
        after = patch(before, edge)
        if len(before.splitlines()) != len(after.splitlines()):
            raise ValueError(f"{name}: graph line count changed")
        output = ROOT / "packaging/mod_assets" / owner / member
        output.parent.mkdir(parents=True, exist_ok=True)
        result = after.encode("utf-8")
        output.write_bytes(result)
        recipes.append(dict(map=name, resource=f"{owner}/{member}", source_sha256=expected,
                            result_sha256=sha(result), edge=edge))
    # The Holt has no map credit node: campaign completion reads this DLC1 field.
    owner, member = "gameresources_patch2", "generated/decls/campaign/campaign/dlc1.decl"
    expected = "bf1c077fad4c482bfc718cc9bbdda5e6b0dcaa265e50b8c2a06e71f75a5a4d8e"
    source = ROOT / "vanilla_decls/owners" / owner / member
    data = source.read_bytes()
    if sha(data) != expected:
        raise ValueError("the_holt: winning vanilla SHA mismatch")
    before = data.decode("utf-8")
    if before.count('credits = "dlc1";') != 1 or any(marker not in before for marker in (
        'outroTextCrawl = "campaign/dlc1/outro";', 'gameCompleteInfo = {',
        'missionSelectList = "missionlist_dlc1";')):
        raise ValueError("the_holt: campaign completion contract missing")
    after = before.replace('credits = "dlc1";', '', 1)
    if len(before.splitlines()) != len(after.splitlines()):
        raise ValueError("the_holt: campaign line count changed")
    output = ROOT / "packaging/mod_assets" / owner / member
    output.parent.mkdir(parents=True, exist_ok=True)
    result = after.encode("utf-8")
    output.write_bytes(result)
    recipes.append(dict(map="the_holt", resource=f"{owner}/{member}",
                        source_sha256=expected, result_sha256=sha(result), field="credits"))
    (ROOT / "packaging/final-credit-decl-patches.json").write_text(
        json.dumps(recipes, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(recipes, indent=2))


if __name__ == "__main__":
    main()
