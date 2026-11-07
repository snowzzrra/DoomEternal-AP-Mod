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
from tools.release import prebuilt_room_resources


@pytest.fixture(autouse=True)
def enemy_catalog_provenance(tmp_path, monkeypatch):
    bundle = tmp_path / "enemy-catalog-provenance"
    bundle.mkdir()
    hashes = {"doom_eap/content/" + name: hashlib.sha256(first_party_text_bytes(
        (compiler.MOD_ROOT / "doom_eap/content" / name).read_bytes())).hexdigest()
        for name in ("native_enemy_catalog.json", "vanilla_enemy_profiles.json")}
    # isolated provenance keeps catalog drift tests independent of the pinned release bundle.
    (bundle / "ROOM_RESOURCES_PROVENANCE.json").write_text(json.dumps({"source_hashes": hashes}), encoding="utf-8")
    monkeypatch.setattr(prebuilt_room_resources, "get_frozen_bundle_dir", lambda root: bundle)


@pytest.fixture
def projected_package(tmp_path, monkeypatch):
    (tmp_path / "__init__.py").touch()
    spec = importlib.util.spec_from_file_location("projected_content", tmp_path / "__init__.py")
    package = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, package.__name__, package)
    yield package
    for name in tuple(sys.modules):
        if name.startswith(package.__name__ + "."):
            del sys.modules[name]


def test_prebuilt_enemy_catalogs_reject_content_and_source_drift(tmp_path):
    root = compiler.MOD_ROOT
    paths = [Path("doom_eap/content/native_enemy_catalog.json"),
             Path("doom_eap/content/vanilla_enemy_profiles.json")]
    paths.extend(path.relative_to(root) for path in (root / "content/maps").glob("*/descriptor.json"))
    for path in paths:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / path, tmp_path / path)
    compiler._prebuilt_enemy_catalogs(tmp_path)
    for path in paths[:2]:
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


def test_source_only_checks_apworld_copies_without_authoring_maps(tmp_path, monkeypatch, projected_package):
    output = tmp_path / "generated_content.py"
    shutil.copytree(
        compiler.DEFAULT_OUTPUT.parent,
        tmp_path,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "native_enemy_assets.json"),
    )
    exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda path: False if path == compiler.MOD_ROOT / "vanillamaps" else exists(path)
    )
    staging = tmp_path / "runtime"
    compiler.materialize_catalog(output, source_only=True, output_root=staging)
    compiler.check_catalog(output, source_only=True, output_root=staging)
    contract = importlib.import_module(projected_package.__name__ + ".slot_data_contract")
    codec = importlib.import_module(projected_package.__name__ + ".recipe_codec")
    assert "assets" not in contract.catalog_tables(contract.load_slot_catalog())
    assert "assets" in load_slot_catalog(path=staging / "slot_catalog.json").tables
    assert contract.load_slot_catalog(path=staging / "slot_catalog.json") == contract.load_slot_catalog()
    assert codec.recipe_references() == recipe_references()
    output.with_name("native_enemy_catalog.json").write_text("{}", encoding="utf-8")
    with pytest.raises(contract.DigestError, match="dependency digest mismatch"):
        contract.load_slot_catalog()
    with pytest.raises(ValueError, match="synchronization value=stale"):
        compiler.check_catalog(output, source_only=True, output_root=staging)
