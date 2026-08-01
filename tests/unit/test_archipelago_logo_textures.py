"""Contracts for the converted Archipelago albedo and its real consumers."""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path

from content_catalog import load_content_catalog


ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = ROOT / "assets" / "source" / "archipelago_logo"
RUNTIME_DIR = ROOT / "assets" / "runtime" / "archipelago_logo"
MOD_ASSETS = ROOT / "packaging" / "mod_assets"
CONTRACT = json.loads(
    (RUNTIME_DIR / "texture_contract.json").read_text(encoding="utf-8")
)
SOURCE_TGA_SHA256 = (
    "6e76b08e7e3e6dfb318b9ea80791a7ea15ed47de848912843cb3967246a6db3f"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_prepared_source_files_hashes() -> None:
    expected_hashes = {
        "ArchipelagoLogo_prepared.obj": (
            "ab6a1f3d8fb833070889c80ef800a8ea9f55a1aa9dc905fb4599df32e5f9c659"
        ),
        "ArchipelagoLogo_prepared.mtl": (
            "803221b0dbc9fff8d1789fbb114a6542b29aaecac000970b2535d68d87b9d76a"
        ),
        "archipelago_logo_atlas.png": (
            "a25bb8b630dafe0dd4a5ad2863fa4ad41296277cf63818c4a7a2b4c1712b1a3a"
        ),
    }
    for filename, expected in expected_hashes.items():
        assert _sha256(SOURCE_DIR / filename) == expected


def test_conventional_source_tga_is_not_a_runtime_texture() -> None:
    source_tga = SOURCE_DIR / "archipelago_logo_atlas_source.tga"
    data = source_tga.read_bytes()
    assert _sha256(source_tga) == SOURCE_TGA_SHA256
    assert data[:3] == b"\x00\x00\x02"
    assert data[-18:] == b"TRUEVISION-XFILE.\x00"

    runtime_members = tuple(MOD_ASSETS.rglob("*.tga*"))
    assert runtime_members
    assert all(_sha256(path) != SOURCE_TGA_SHA256 for path in runtime_members)


def test_autoheckin_output_has_divinity_container_and_verified_bimage_contract() -> None:
    converter = CONTRACT["converter"]
    runtime_tga = ROOT / converter["output"]
    data = runtime_tga.read_bytes()
    assert _sha256(ROOT / converter["source_png"]) == converter["source_png_sha256"]
    assert _sha256(runtime_tga) == converter["output_sha256"]
    assert data[:8] == b"DIVINITY"
    assert data[:3] != b"\x00\x00\x02"
    assert data[-18:] != b"TRUEVISION-XFILE.\x00"
    assert converter["bimage"] == {
        "signature": "BIM",
        "version": 21,
        "material_kind": "albedo",
        "material_kind_id": 1,
        "width": 256,
        "height": 256,
        "mip_count": 9,
        "texture_format": "FMT_BC1_SRGB",
        "texture_format_id": 33,
        "no_mips": False,
    }


def test_runtime_textures_use_only_real_material2_albedo_entries() -> None:
    expected = {
        MOD_ASSETS / slot["resource_base"] / slot["true_filename"]
        for slot in CONTRACT["slots"]
        if slot["packaged"]
    }
    actual = set(MOD_ASSETS.rglob("*.tga*"))
    assert actual == expected

    canonical = RUNTIME_DIR / "archipelago_logo_atlas.tga"
    for slot in CONTRACT["slots"]:
        if not slot["packaged"]:
            assert slot["package_blocker"] == "model_importer_bundle_pending"
            continue
        target = MOD_ASSETS / slot["resource_base"] / slot["true_filename"]
        assert target.read_bytes() == canonical.read_bytes()
        assert slot["true_filename"].startswith(slot["albedo_path"])
        assert slot["higher_patch_copies"] == []
        assert slot["resource_archive"] in slot["resource_priority_indices"]


def test_opaque_material_overrides_preserve_only_non_vfx_texture_maps() -> None:
    catalog = load_content_catalog()
    policies = {
        asset.map_key: asset.visual_presentation_policy
        for asset in catalog.assets
        if asset.visual_presentation_policy
    }
    for slot in CONTRACT["slots"]:
        assert slot["inherit"] == "template/pbr_pickup"
        assert slot["render_layers"] == 1
        assert len(slot["material2_sha256"]) == 64
        assert set(slot["preserved"]) >= {
            "normal", "specular", "smoothness", "heightmap", "emissive"
        }
        policy = policies[slot["map_key"]]
        decl = (
            MOD_ASSETS / slot["resource_base"] / slot["material2"]
        ).read_text(encoding="utf-8")
        for map_kind in (
            "albedo", "normal", "specular", "smoothness", "heightmap",
        ):
            assert (
                f'filePath = "{policy["preserve_maps"][map_kind]}";'
                in decl
            )
        assert slot["preserved"]["emissive"] not in decl
        if "cover" in slot["preserved"]:
            assert "cover =" not in decl
        assert not (
            MOD_ASSETS / slot["resource_base"] / "EternalMod" / "assetsinfo"
        ).exists()


def test_release_zip_contains_only_runtime_visual_assets() -> None:
    configured = os.environ.get("AP_RELEASE_MOD_ZIP")
    zip_path = Path(configured) if configured else (
        ROOT / "build" / "release" / "DoomEternalArchipelagoAlpha.zip"
    )
    assert zip_path.is_file(), f"Expected release artifact is missing: {zip_path}"

    forbidden_suffixes = (".obj", ".mtl", ".blend")
    forbidden_names = {
        "archipelago_logo_atlas.png",
        "archipelago_logo_atlas_direct_kd.png",
        "archipelago_logo_atlas_source.tga",
        "archipelago_logo_prepared_bundle.zip",
    }
    expected_textures = {
        f'{slot["resource_base"]}/{slot["true_filename"]}'
        for slot in CONTRACT["slots"]
        if slot["packaged"]
    }
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        assert expected_textures <= names
        assert {
            name for name in names if ".tga" in name
        } == expected_textures
        assert not any(name.lower().endswith(forbidden_suffixes) for name in names)
        assert not any(Path(name).name in forbidden_names for name in names)
