"""Unit tests for Archipelago logo source validation, texture replacements, and package contract."""

import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = ROOT / "assets" / "source" / "archipelago_logo"


def test_prepared_source_files_hashes() -> None:
    expected_hashes = {
        "ArchipelagoLogo_prepared.obj": "ab6a1f3d8fb833070889c80ef800a8ea9f55a1aa9dc905fb4599df32e5f9c659",
        "ArchipelagoLogo_prepared.mtl": "803221b0dbc9fff8d1789fbb114a6542b29aaecac000970b2535d68d87b9d76a",
        "archipelago_logo_atlas.png": "a25bb8b630dafe0dd4a5ad2863fa4ad41296277cf63818c4a7a2b4c1712b1a3a",
        "archipelago_logo_atlas_direct_kd.png": "af656a1f1cd34ae8f953f96262526869ae0e3b280ead771504377acf823947b2",
        "archipelago_logo_atlas_source.tga": "6e76b08e7e3e6dfb318b9ea80791a7ea15ed47de848912843cb3967246a6db3f",
    }
    for filename, expected in expected_hashes.items():
        file_path = SOURCE_DIR / filename
        assert file_path.exists(), f"Missing source file: {filename}"
        actual = hashlib.sha256(file_path.read_bytes()).hexdigest()
        assert actual == expected, f"Hash mismatch for {filename}: {actual} != {expected}"


def test_mod_assets_contain_tga_texture_replacements() -> None:
    mod_assets = ROOT / "packaging" / "mod_assets"
    expected_tga_hash = "6e76b08e7e3e6dfb318b9ea80791a7ea15ed47de848912843cb3967246a6db3f"

    # Sentinel Prime and Final Sin must contain codex.tga
    for map_key in ("e2m4_boss", "e3m4_boss"):
        codex_tga = mod_assets / map_key / "art" / "pickups" / "codex.tga"
        assert codex_tga.exists(), f"Missing codex.tga for {map_key}"
        assert hashlib.sha256(codex_tga.read_bytes()).hexdigest() == expected_tga_hash

    # All campaign map directories must contain question_mark_a.tga
    for map_dir in mod_assets.iterdir():
        if not map_dir.is_dir() or map_dir.name == "streamdb":
            continue
        qm_tga = map_dir / "art" / "pickups" / "question_mark_a.tga"
        assert qm_tga.exists(), f"Missing question_mark_a.tga for {map_dir.name}"
        assert hashlib.sha256(qm_tga.read_bytes()).hexdigest() == expected_tga_hash


def test_release_zip_excludes_source_and_preparation_files() -> None:
    release_dir = ROOT / "build" / "release"
    zips = list(release_dir.glob("*.zip"))
    if not zips:
        return  # Skip if release build has not run yet

    forbidden = {
        "ArchipelagoLogo_prepared.obj",
        "ArchipelagoLogo_prepared.mtl",
        "archipelago_logo_atlas.png",
        "archipelago_logo_atlas_direct_kd.png",
        "archipelago_logo_atlas_source.tga",
        "PREPARATION_REPORT.md",
        "archipelago_logo_prepared_bundle.zip",
    }

    for zip_path in zips:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            for forbidden_name in forbidden:
                assert not any(forbidden_name in name for name in names), (
                    f"Forbidden source file {forbidden_name} found in release artifact {zip_path.name}"
                )
