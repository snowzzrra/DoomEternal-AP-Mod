#!/usr/bin/env python3
"""Bypass final-credit nodes through their vanilla completion exits."""

import json
from pathlib import Path

if __package__:
    from .intro_skip import ROUTES as INTRO_ROUTES, patch, sha
else:
    from intro_skip import ROUTES as INTRO_ROUTES, patch, sha

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
    "the_holt_ending": (
        "e4m3_mcity_patch2", DECL + "dlc/e4m3_mcity/e4m3_mcity_art_cinematics_seraphim_death/seraphim_death_info_logic.decl",
        "23b205de918ae01f5432e77cd73f974c80273c9b675f550e2d0e3badbb526a30",
        (3891148376, 76270236, 1, 1037894397, 0, 117100117, 0),
        ('scene = "maps/dlc1/e4m3_mcity/seraphim_death";', 'value = "cinEnd";',
         'soundStateToSet = "music_ghost_states/credits_end";', '"game_target_relay_1";'),
    ),
}


def restore_cinematic_edges(assembled):
    routes = [(owner, member, edges) for owner, member, _, edges, _ in INTRO_ROUTES.values()]
    routes.extend((owner, member, (edge,)) for owner, member, _, edge, _ in ROUTES.values())
    for owner, member, edges in routes:
        path = f"{owner}/{member}"
        if path not in assembled:
            raise ValueError(f"missing cinematic graph: {path}")
        graph = assembled[path].decode("utf-8")
        for edge in edges:
            graph = patch(graph, edge, reverse=True)
        assembled[path] = graph.encode("utf-8")


def _main_member(recipes):
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
        result = after.replace("\r\n", "\n").encode("utf-8")
        output.write_bytes(result)
        recipes.append(dict(map=name, resource=f"{owner}/{member}", source_sha256=expected,
                            result_sha256=sha(result), edge=edge))
    # DLC1 selects credits from its campaign declaration after the map exit.
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
    return before, expected, member, owner


def main():
    recipes = []
    before, expected, member, owner = _main_member(recipes)
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
        json.dumps(recipes, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps(recipes, indent=2))


if __name__ == "__main__":
    main()
