import hashlib
import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from doom_eap.content.recipe_codec import recipe_references
from doom_eap.content.slot_data_contract import load_slot_catalog
from doom_eap.contracts.source_bytes import first_party_text_bytes
from tools.content import compile_content_catalog as compiler
from tools.release.prebuilt_room_resources import get_frozen_bundle_dir


@pytest.mark.parametrize("check", [False, True])
def test_cli_reports_relative_changes_and_uses_explicit_source(monkeypatch, capsys, check):
    mod, ap, staging = Path("Mod"), Path("APWorld"), Path("staging")
    output = ap / "worlds/doometernal/generated_content.py"
    changed = (mod / "data/catalog.json", output, staging / "catalog.json")
    monkeypatch.setattr(compiler, "MOD_ROOT", mod)
    monkeypatch.setattr(Path, "is_file", lambda path: path == ap / "worlds/doometernal/items.py")
    monkeypatch.setattr(sys, "argv", ["compile_content_catalog", "--archipelago-source", str(ap),
                                   "--output-root", str(staging)] + (["--check"] if check else []))

    def compile_catalog(path, **kwargs):
        assert path == output
        assert kwargs == dict(output_root=staging, check=check, map_key=None, source_only=False,
                              archipelago_source=ap)
        return () if check else changed

    monkeypatch.setattr(compiler, "_compile_catalog", compile_catalog)
    assert compiler.main() == 0
    assert capsys.readouterr().out == ("up-to-date\n" if check else
        "updated\nMod:\n  data/catalog.json\nAPWorld:\n  worlds/doometernal/generated_content.py\n"
        "Runtime output:\n  catalog.json\n")


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


@pytest.mark.parametrize('dependency', ['native_enemy_catalog.json', 'campaign_stage_ids.json'])
def test_source_only_checks_apworld_copies_without_authoring_maps(tmp_path, monkeypatch, dependency):
    output = tmp_path / "generated_content.py"
    shutil.copytree(compiler.DEFAULT_OUTPUT.parent, tmp_path, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "native_enemy_assets.json"))
    exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda path: False if path == compiler.MOD_ROOT / "vanillamaps" else exists(path))
    staging = tmp_path / "runtime"
    compiler.materialize_catalog(output, source_only=True, output_root=staging)
    compiler.check_catalog(output, source_only=True, output_root=staging)
    manifest = json.loads(output.with_name("projection_manifest.json").read_bytes())
    assert {"slot_data_contract.py", "recipe_codec.py", "slot_catalog.json", "generated_content.py", "native_enemy_catalog.json"} <= set(manifest["files"])
    assert "projection_manifest.json" not in manifest["files"]
    assert "native_enemy_assets.json" not in manifest["files"]
    assert not (tmp_path / "native_enemy_assets.json").exists()
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == digest
    for name in ("recipe_codec.py", "slot_catalog.json", "projection_manifest.json"):
        assert (staging / name).read_bytes() == (tmp_path / name).read_bytes()
    for name in ("slot_data_contract.py", "recipe_codec.py"):
        assert (tmp_path / name).read_text(encoding="utf-8").startswith(f"# GENERATED from doom_eap/content/{name}; regenerate:")
    (tmp_path / '__init__.py').touch()
    spec = importlib.util.spec_from_file_location('projected_' + dependency.partition('.')[0], tmp_path / '__init__.py')
    package = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, package.__name__, package)
    contract = importlib.import_module(package.__name__ + ".slot_data_contract")
    codec = importlib.import_module(package.__name__ + ".recipe_codec")
    assert contract.catalog_digest(contract.load_slot_catalog()) == manifest['catalog_digest']
    assert 'assets' not in contract.catalog_tables(contract.load_slot_catalog())
    assert (staging / 'native_enemy_assets.json').is_file()
    assert 'assets' in load_slot_catalog(path=staging / 'slot_catalog.json').tables
    assert contract.load_slot_catalog(path=staging / 'slot_catalog.json') == contract.load_slot_catalog()
    assert codec.recipe_references() == recipe_references()
    assert contract.canonical_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    assert codec.encode_recipes(codec.ResolvedRecipes(False, {}, ())) == {
        "enabled": False, "limit_sets": [], "profiles": [], "patterns": [], "maps": {}, "timers": []}
    output.with_name(dependency).write_text("{}", encoding="utf-8")
    with pytest.raises(contract.DigestError, match='dependency digest mismatch'):
        contract.load_slot_catalog()
    with pytest.raises(ValueError, match="synchronization value=stale"):
        compiler.check_catalog(output, source_only=True, output_root=staging)


@pytest.mark.parametrize(("source_only", "maps_present"), [(True, True), (False, False)])
def test_catalog_regeneration_requires_authoring_sources(tmp_path, monkeypatch, source_only, maps_present):
    from tools.maps import native_enemy_inventory

    exists = Path.exists
    shutil.copytree(compiler.DEFAULT_OUTPUT.parent, tmp_path, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(Path, "exists", lambda path: False if path == compiler.MOD_ROOT / "vanillamaps" else exists(path))
    compiler.materialize_catalog(tmp_path / "generated_content.py", source_only=True, output_root=tmp_path / "runtime")
    monkeypatch.setattr(Path, "exists", lambda path: maps_present if path == compiler.MOD_ROOT / "vanillamaps" else exists(path))
    def regenerate(root):
        raise ValueError("authoring source validation")
    monkeypatch.setattr(native_enemy_inventory, "build_native_recipe_catalog", regenerate)
    with pytest.raises(ValueError, match="authoring source validation"):
        compiler.check_catalog(tmp_path / "generated_content.py", source_only=source_only)


def test_slot_catalog_is_projected_hashed_and_staged(tmp_path):
    from doom_eap.content.slot_data_contract import CATALOG_DEPENDENCIES, catalog_dependency_path

    root, output, staging = tmp_path / "mod", tmp_path / "ap", tmp_path / "runtime"
    source = root / "doom_eap/content"
    source.mkdir(parents=True)
    for name in CATALOG_DEPENDENCIES:
        shutil.copyfile(catalog_dependency_path(compiler.MOD_ROOT / 'doom_eap/content', name), source / name)
    for name in ("slot_data_contract.py", "recipe_codec.py"):
        (source / name).write_text("VALUE = 1\n", encoding="ascii")
    catalog = b'{"stages":{}}\n'
    (source / "slot_catalog.json").write_bytes(catalog)
    payloads = {source / "slot_catalog.json": catalog}

    def write(path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        payloads[path] = payload

    output.mkdir()
    asset = output / "native_enemy_assets.json"
    asset.write_bytes(b'{}')
    with pytest.raises(ValueError, match="field=projection value=unexpected"):
        compiler.project_contracts(root, output, staging, payloads, write, check=True)
    assert asset.read_bytes() == b'{}'
    compiler.project_contracts(root, output, staging, payloads, write)
    manifest = json.loads((output / "projection_manifest.json").read_bytes())
    assert (output / "slot_catalog.json").read_bytes() == catalog
    assert (staging / "slot_catalog.json").read_bytes() == catalog
    assert manifest["files"]["slot_catalog.json"] == hashlib.sha256(catalog).hexdigest()
    assert not (output / "native_enemy_assets.json").exists()
    assert "native_enemy_assets.json" not in manifest["files"]
    assert (staging / "native_enemy_assets.json").read_bytes() == first_party_text_bytes(
        (source / "native_enemy_assets.json").read_bytes())
