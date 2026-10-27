import json
from pathlib import Path
import shutil

import pytest

from tools.content import compile_content_catalog as compiler
from tools.release.prebuilt_room_resources import get_frozen_bundle_dir


def test_prebuilt_enemy_catalogs_reject_content_and_source_drift(tmp_path):
    root = compiler.MOD_ROOT
    bundle = get_frozen_bundle_dir(root).relative_to(root)
    paths = [bundle / "ROOM_RESOURCES_PROVENANCE.json",
             Path("doom_eap/content/native_enemy_catalog.json"),
             Path("doom_eap/content/vanilla_enemy_profiles.json")]
    paths.extend(path.relative_to(root) for path in (root / "content/maps").glob("*/descriptor.json"))
    for path in paths:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / path, tmp_path / path)
    compiler._prebuilt_enemy_catalogs(tmp_path)
    for path in paths[1:3]:
        original = (tmp_path / path).read_bytes()
        document = json.loads(original)
        document["maps"]["e1m1_intro"]["source_sha256"] = "0" * 64
        (tmp_path / path).write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(ValueError, match="differs from room resource provenance"):
            compiler._prebuilt_enemy_catalogs(tmp_path)
        (tmp_path / path).write_bytes(original)
    descriptor = tmp_path / "content/maps/e1m1_intro/descriptor.json"
    document = json.loads(descriptor.read_bytes())
    document["source_sha256"] = "0" * 64
    descriptor.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="source differs from descriptor"):
        compiler._prebuilt_enemy_catalogs(tmp_path)


def test_source_only_checks_apworld_copies_without_authoring_maps(tmp_path, monkeypatch):
    output = tmp_path / "generated_content.py"
    shutil.copytree(compiler.DEFAULT_OUTPUT.parent, tmp_path, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda path: False if path == compiler.MOD_ROOT / "vanillamaps" else exists(path))
    compiler.check_catalog(output, source_only=True)
    output.with_name("native_enemy_catalog.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="synchronization value=stale"):
        compiler.check_catalog(output, source_only=True)


@pytest.mark.parametrize(("source_only", "maps_present"), [(True, True), (False, False)])
def test_catalog_regeneration_requires_authoring_sources(tmp_path, monkeypatch, source_only, maps_present):
    from tools.maps import native_enemy_inventory

    exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda path: maps_present if path == compiler.MOD_ROOT / "vanillamaps" else exists(path))
    def regenerate(root):
        raise ValueError("authoring source validation")
    monkeypatch.setattr(native_enemy_inventory, "build_native_recipe_catalog", regenerate)
    shutil.copytree(compiler.DEFAULT_OUTPUT.parent, tmp_path, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    with pytest.raises(ValueError, match="authoring source validation"):
        compiler.check_catalog(tmp_path / "generated_content.py", source_only=source_only)
