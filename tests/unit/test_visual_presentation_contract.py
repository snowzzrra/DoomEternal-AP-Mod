"""Opaque, resource-scoped presentation contract for Archipelago visuals."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from content_catalog import load_content_catalog


ROOT = Path(__file__).resolve().parents[2]
MOD_ASSETS = ROOT / "packaging" / "mod_assets"
TEXTURE_CONTRACT = json.loads(
    (
        ROOT / "assets/runtime/archipelago_logo/texture_contract.json"
    ).read_text(encoding="utf-8")
)
EXPECTED_ASSET_HASHES = {
    "assets/runtime/archipelago_logo/archipelago_logo_atlas.tga": (
        "6716b617263a8b16ab9af28edd69dd976aaf42ec237f81e6bd8763c746564f1a"
    ),
    "packaging/mod_assets/e2m4_boss/art/pickups/codex.lwo": (
        "fa617a2f3d43a99510a512c6d7ea47fd3f7ed3a1ac32c4e01d36561f20478ac1"
    ),
    "packaging/mod_assets/e3m4_boss/art/pickups/codex.lwo": (
        "fa617a2f3d43a99510a512c6d7ea47fd3f7ed3a1ac32c4e01d36561f20478ac1"
    ),
    "packaging/mod_assets/e3m3_maykr/art/pickups/codex.lwo": (
        "fa617a2f3d43a99510a512c6d7ea47fd3f7ed3a1ac32c4e01d36561f20478ac1"
    ),
    (
        "packaging/mod_assets/streamdb/art/pickups/"
        "codex_id#13626551837332538432.lwo"
    ): "ef49160da53774ff7c952ba5f6d939e292f16d16f084558c899821d567459630",
}
VANILLA_MAP_HASHES = {
    "e3m3_maykr": (
        "29cdee76e07ecdce1cc3b38d47d311c50d06b3b5e8f14ea9e74512a95e577ffb"
    ),
    "e2m4_boss": (
        "a2ad83c240a8bc929b40f059f7f11aff5337c36999e773d0e045c132823ad390"
    ),
    "e3m4_boss": (
        "ec8a59ec4a240eb5f4535a01a98896d618f1724a1e669a946c8f0202c9f4d398"
    ),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assignment(text: str, name: str) -> str:
    match = re.search(rf'\b{re.escape(name)}\s*=\s*"([^"]+)";', text)
    assert match is not None, f"missing assignment: {name}"
    return match.group(1)


def _entity_block(content: str, name: str) -> str:
    start = content.index(f"entityDef {name} {{")
    end = content.index("\nentity {", start)
    return content[start:end]


def test_visual_binary_and_autoheckin_payload_hashes_are_unchanged() -> None:
    for relative, expected in EXPECTED_ASSET_HASHES.items():
        assert _sha256(ROOT / relative) == expected
    assert TEXTURE_CONTRACT["converter"]["output_sha256"] == (
        EXPECTED_ASSET_HASHES[
            "assets/runtime/archipelago_logo/archipelago_logo_atlas.tga"
        ]
    )


def test_all_enabled_maps_declare_one_opaque_ap_only_material_policy() -> None:
    catalog = load_content_catalog()
    policies = {
        asset.map_key: asset
        for asset in catalog.assets
        if asset.visual_presentation_policy
    }
    assert set(policies) == {spec.key for spec in catalog.enabled_maps()}
    for asset in policies.values():
        policy = asset.visual_presentation_policy
        assert policy["scope"] == "ap_generated_entities_only"
        assert policy["preserve_motion"] is True
        assert policy["preserve_bobbing"] is True
        assert policy["preserve_rotation"] is True
        assert policy["think_component"] == "bob_rotate_fast"
        assert policy["material_mode"] == "resource_scoped_opaque_override"


def test_opaque_materials_preserve_maps_and_strip_pickup_shader_contract() -> None:
    catalog = load_content_catalog()
    for asset in catalog.assets:
        policy = asset.visual_presentation_policy
        if not policy:
            continue
        decl_path = MOD_ASSETS / asset.resource_base / policy["material_decl"]
        decl = decl_path.read_text(encoding="utf-8")
        assert _sha256(decl_path) == policy["material_sha256"]
        assert _assignment(decl, "inherit") == "template/pbr"
        assert re.search(r"\bsurfaceemissivescale\s*=\s*0;", decl)
        file_paths = set(re.findall(r'\bfilePath\s*=\s*"([^"]+)";', decl))
        assert file_paths == set(policy["preserve_maps"].values())
        assert all(
            token not in decl
            for token in (
                "template/pbr_pickup", 'prog = "pickup"',
                "surfaceemissivetiledmask", "pickup_panning", "bloommaskmap",
                "surfacesheencolor", "alphatestthreshold", "prezdrawalpha",
                "cover =",
            )
        )


def test_vanilla_material_contracts_are_not_packaged_outside_ap_resources() -> None:
    catalog = load_content_catalog()
    expected = {
        (
            spec.resource_base,
            "generated/decls/material2/art/pickups/codex.decl",
        )
        for spec in catalog.enabled_maps()
    }
    actual = {
        (path.relative_to(MOD_ASSETS).parts[0], path.relative_to(MOD_ASSETS / path.relative_to(MOD_ASSETS).parts[0]).as_posix())
        for path in MOD_ASSETS.rglob("*.decl")
        if path.name in {"question.decl", "codex.decl"}
    }
    assert actual == expected
    vanilla_hashes = {
        slot["map_key"]: slot["material2_sha256"]
        for slot in TEXTURE_CONTRACT["slots"]
    }
    assert set(vanilla_hashes) == {spec.key for spec in catalog.enabled_maps()}
    assert set(vanilla_hashes.values()) == {
        "90f91e8e63d65338515f04a294f2b10d097f848ad3d7a02393e0e6d797bcc562"
    }


def test_vanilla_donors_keep_original_motion_and_fx_in_immutable_source_maps(
) -> None:
    for map_key, expected_hash in VANILLA_MAP_HASHES.items():
        assert _sha256(ROOT / "vanillamaps" / f"{map_key}.map") == expected_hash

    urdak_source = (ROOT / "vanillamaps/e3m3_maykr.map").read_text(
        encoding="utf-8"
    )
    urdak_donors = {
        "blue_control_tower_progress_cheats_powerup_infinite_berserk_1",
        "temp_all_scripting_pickup_collectible_toys_khan_maykr_1",
        "temp_all_scripting_pickup_collectible_toys_zombie_maykr_1",
        "temp_all_scripting_pickup_collectible_albums_album_15_1",
        "temp_all_scripting_pickup_collectible_albums_album_16_1",
        "temp_all_scripting_pickup_collectible_toys_pinky_spectre_1",
    }
    entity_matches = list(re.finditer(r"entityDef ([^ ]+) \{", urdak_source))
    discovered = set()
    for index, match in enumerate(entity_matches):
        end = (
            entity_matches[index + 1].start()
            if index + 1 < len(entity_matches)
            else len(urdak_source)
        )
        if "art/pickups/question_mark_a.lwo" in urdak_source[match.start():end]:
            discovered.add(match.group(1))
    assert discovered == urdak_donors
    for donor in urdak_donors:
        block = _entity_block(urdak_source, donor)
        assert 'fxDecl = "pickups/secret_item";' in block
        assert 'thinkComponentDecl = "bob_rotate_fast";' in block

    donors = {
        "e2m4_boss": "game_progress_codex_1_e2m4",
        "e3m4_boss": "pickups_progress_codex_2_e3m4",
    }
    for map_key, donor in donors.items():
        source = (ROOT / "vanillamaps" / f"{map_key}.map").read_text(
            encoding="utf-8"
        )
        block = _entity_block(source, donor)
        assert 'fxDecl = "gameplay/codex_activate";' in block
        assert "updateFX = true;" in block
        assert 'thinkComponentDecl = "bob_rotate_slow";' in block
