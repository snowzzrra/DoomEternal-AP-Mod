#!/usr/bin/env python3
"""Route three opening scenes through their existing completion paths."""

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DECL = "generated/decls/logicentity/maps/game/"
ROUTES = {
    "e1m1_intro": (
        "e1m1_intro_patch3", DECL + "sp/e1m1_intro/e1m1_intro_cinematic_intro_game/intro_game_info_logic.decl",
        "aa6305d1c847c97c621857a5e76112383b11e0824c69327a3142aa0de8443499",
        ((2392606599, 3947516743, 3, 2788213351, 0, 4110483463, 0),
         (3484244384, 4110483463, 1, 1622116226, 0, 1701438474, 0)),
        ('name = "bPlayedOnce";', 'value = "cin_complete";', 'idLogicNodeModelPlayerInhibitLoadCheckpoint'),
    ),
    "e4m1_rig": (
        "e4m1_rig_patch2", DECL + "dlc/e4m1_rig/e4m1_rig_cinematic_intro/intro_info_logic.decl",
        "636ff9d8c103d61c6e1080de0ee272761450acf9e2b8b2f4cb534b025c5fba7b",
        ((1134950910, 1104602475, 3, 3112197059, 0, 1467365544, 0),
         (1919791452, 1467365544, 1, 2814965625, 0, 1708779698, 0)),
        ('name = "bPlayedOnce";', 'value = "cinEnd";', 'checkpoints_target_change_layer_34'),
    ),
    "e5m1_spear": (
        "e5m1_spear_patch2", DECL + "dlc2/e5m1_spear/e5m1_spear_cinematics_intro/cinematic_intro_info_logic.decl",
        "0faba3acdda2e11f3dd48a5abd996175c5ae23f00055b3e9f8f6c7af26adf497",
        ((215632548, 1227821557, 3, 1792378336, 0, 2877089172, 0),
         (3250851675, 2877089172, 1, 272046422, 117545624, 3435759729, 0)),
        ('name = "hasScenePlayed";', 'value = "cinEnd";', 'checkpoints_target_relay_48',
         'cinematiccomplete e5m1_intro'),
    ),
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def patch(text, edge):
    edge_id, source, source_pin, old_target, old_pin, target, target_pin = edge
    pattern = re.compile(
        rf'(id = {edge_id};\s*fromNodeId = {source};\s*fromPinId = {source_pin};\s*'
        rf'toNodeId = ){old_target}(;\s*toPinId = ){old_pin}(;)' 
    )
    result, count = pattern.subn(lambda m: f"{m[1]}{target}{m[2]}{target_pin}{m[3]}", text)
    if count != 1:
        raise ValueError(f"expected one opening-scene edge {edge_id}, found {count}")
    return result


def main():
    recipes = []
    for map_id, (owner, member, expected, edges, required) in ROUTES.items():
        source = ROOT / "vanilla_decls" / member
        data = source.read_bytes()
        if sha(data) != expected:
            raise ValueError(f"{map_id}: source SHA mismatch")
        before = data.decode("utf-8")
        if any(value not in before for value in required):
            raise ValueError(f"{map_id}: completion contract missing")
        after = before
        for edge in edges:
            after = patch(after, edge)
        if len(before.splitlines()) != len(after.splitlines()):
            raise ValueError(f"{map_id}: graph line count changed")
        output = ROOT / "packaging/mod_assets" / owner / member
        output.parent.mkdir(parents=True, exist_ok=True)
        result = after.encode("utf-8")
        output.write_bytes(result)
        recipes.append(dict(map=map_id, resource=f"{owner}/{member}", source_sha256=expected,
                            result_sha256=sha(result), edges=edges))
    recipe = ROOT / "packaging/intro-decl-patches.json"
    recipe.write_text(json.dumps(recipes, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(recipes, indent=2))


if __name__ == "__main__":
    main()
