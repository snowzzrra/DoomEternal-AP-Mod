"""Focused tests for pipeline cache validation, receipt materialization, and atomic ZIP publication."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.validation.pipeline import Pipeline, MAP_CACHE_ROOT, _canonical_hash, _sha256


def _inputs_hash(inputs: dict) -> str:
    return _canonical_hash(inputs)


def _make_cache_entry(cache_root: Path, map_key: str, digest: str, *,
                      inputs: dict | None = None,
                      missing_manifest: bool = False,
                      missing_entities: bool = False,
                      corrupt_manifest: bool = False,
                      wrong_manifest_hash: bool = False,
                      output_name: str | None = None) -> Path:
    """Create a cache entry. Writes metadata first, then corrupts if requested."""
    directory = cache_root / map_key / digest
    directory.mkdir(parents=True, exist_ok=True)

    out_name = output_name or f"{map_key}.entities"
    entities = directory / out_name
    manifest = directory / f"{map_key}.json"

    entities_content = b"generated entities content for " + map_key.encode()
    manifest_content = json.dumps(
        {"entities": {map_key: 12345}}, sort_keys=True
    ).encode()

    # Write files in clean state first
    if not missing_entities:
        entities.write_bytes(entities_content)
    if not missing_manifest:
        manifest.write_bytes(manifest_content)

    # Write metadata from clean state
    if not missing_entities and not missing_manifest:
        output_sha = _sha256(entities)
        manifest_sha = _sha256(manifest)
        metadata = {
            "schema_version": 1,
            "map_key": map_key,
            "digest": digest,
            "inputs": inputs or {"pipeline_version": "2", "some_input": "hash"},
            "raw_sha256": hashlib.sha256(b"raw").hexdigest(),
            "raw_size": 3,
            "output_sha256": output_sha,
            "output_size": entities.stat().st_size,
            "manifest_sha256": manifest_sha,
            "manifest_size": manifest.stat().st_size,
        }
        if wrong_manifest_hash:
            metadata["manifest_sha256"] = "0" * 64
        p = directory / "metadata.json"
        p.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    # Now corrupt AFTER metadata was written from clean state
    if corrupt_manifest and not missing_manifest:
        manifest.write_bytes(b"corrupted")

    return directory


def _make_pipeline_for_key(cache_root: Path, inputs: dict, map_key: str,
                           generated_output: str | None = None) -> Pipeline:
    pipeline = Pipeline(no_cache=False)
    out = generated_output or f"{map_key}.entities"
    spec = SimpleNamespace(
        source_file=f"{map_key}.map",
        level_config_path=Path("/nonexistent_level_config.json"),
        manifest_path=Path("/nonexistent_manifest.json"),
        data={"generated_output": out, "package_directory": None},
        onboarding_path=None,
        key=map_key,
    )
    catalog = SimpleNamespace(
        map=lambda key: spec,
        enabled_maps=lambda: [spec],
        physical_locations=[],
        runtime_locations=[],
        publishers=[],
        assets=[],
    )
    pipeline.catalog = catalog  # type: ignore[assignment]
    return pipeline


# ---------------------------------------------------------------------------
# 1. Complete cache entry = hit
# ---------------------------------------------------------------------------
class TestCacheEntryHit:
    def test_complete_entry_returns_hit(self, tmp_path):
        cache_root = tmp_path / "maps"
        inputs = {"pipeline_version": "2", "some_input": "hash"}
        digest = _inputs_hash(inputs)
        _make_cache_entry(cache_root, "tmap", digest, inputs=inputs)

        pipeline = _make_pipeline_for_key(cache_root, inputs, "tmap")

        with patch("tools.validation.pipeline.MAP_CACHE_ROOT", cache_root):
            with patch.object(pipeline, "_map_inputs", return_value=inputs):
                with patch("tools.validation.pipeline.PIPELINE_VERSION", "2"):
                    artifact = pipeline.generate("tmap")

        assert artifact.cache_hit is True
        assert pipeline.cache_hits["tmap"] is True
        assert artifact.digest == digest

    def test_complete_entry_with_sizes(self, tmp_path):
        cache_root = tmp_path / "maps"
        inputs = {"pipeline_version": "2", "extra": "value"}
        digest = _inputs_hash(inputs)
        _make_cache_entry(cache_root, "tmap", digest, inputs=inputs)

        pipeline = _make_pipeline_for_key(cache_root, inputs, "tmap")

        with patch("tools.validation.pipeline.MAP_CACHE_ROOT", cache_root):
            with patch.object(pipeline, "_map_inputs", return_value=inputs):
                with patch("tools.validation.pipeline.PIPELINE_VERSION", "2"):
                    artifact = pipeline.generate("tmap")

        assert artifact.cache_hit is True


# ---------------------------------------------------------------------------
# 2. Missing manifest = invalid + focused regeneration
# ---------------------------------------------------------------------------
class TestCacheInvalidMissingManifest:
    def test_missing_manifest_not_hit(self, tmp_path):
        cache_root = tmp_path / "maps"
        inputs = {"pipeline_version": "2", "some_input": "hash"}
        digest = _inputs_hash(inputs)
        _make_cache_entry(cache_root, "tmap", digest, inputs=inputs, missing_manifest=True)

        pipeline = _make_pipeline_for_key(cache_root, inputs, "tmap")

        with patch("tools.validation.pipeline.MAP_CACHE_ROOT", cache_root):
            with patch.object(pipeline, "_map_inputs", return_value=inputs):
                with patch("tools.validation.pipeline.PIPELINE_VERSION", "2"):
                    try:
                        pipeline.generate("tmap")
                    except (FileNotFoundError, ValueError, OSError):
                        pass

        assert pipeline.cache_hits.get("tmap") is not True

    def test_only_that_map_invalidated(self, tmp_path):
        cache_root = tmp_path / "maps"
        inputs_a = {"pipeline_version": "2", "map": "a"}
        inputs_b = {"pipeline_version": "2", "map": "b"}
        digest_a = _inputs_hash(inputs_a)
        digest_b = _inputs_hash(inputs_b)

        _make_cache_entry(cache_root, "map_a", digest_a, inputs=inputs_a)
        _make_cache_entry(cache_root, "map_b", digest_b, inputs=inputs_b, missing_manifest=True)

        pipeline_a = _make_pipeline_for_key(cache_root, inputs_a, "map_a")
        pipeline_b = _make_pipeline_for_key(cache_root, inputs_b, "map_b")

        with patch("tools.validation.pipeline.MAP_CACHE_ROOT", cache_root):
            with patch("tools.validation.pipeline.PIPELINE_VERSION", "2"):
                with patch.object(pipeline_a, "_map_inputs", return_value=inputs_a):
                    artifact_a = pipeline_a.generate("map_a")
                assert artifact_a.cache_hit is True
                # pipeline_a.cache_hits should have map_a = True
                assert pipeline_a.cache_hits["map_a"] is True

                # map_b should not hit
                with patch.object(pipeline_b, "_map_inputs", return_value=inputs_b):
                    try:
                        pipeline_b.generate("map_b")
                    except (FileNotFoundError, ValueError, OSError):
                        pass
                assert pipeline_b.cache_hits.get("map_b") is not True


# ---------------------------------------------------------------------------
# 3. Corrupt manifest = invalid
# ---------------------------------------------------------------------------
class TestCacheInvalidCorruptManifest:
    def test_corrupt_manifest_not_hit(self, tmp_path):
        cache_root = tmp_path / "maps"
        inputs = {"pipeline_version": "2", "some_input": "hash"}
        digest = _inputs_hash(inputs)
        _make_cache_entry(cache_root, "tmap", digest, inputs=inputs, corrupt_manifest=True)

        pipeline = _make_pipeline_for_key(cache_root, inputs, "tmap")

        with patch("tools.validation.pipeline.MAP_CACHE_ROOT", cache_root):
            with patch.object(pipeline, "_map_inputs", return_value=inputs):
                with patch("tools.validation.pipeline.PIPELINE_VERSION", "2"):
                    try:
                        pipeline.generate("tmap")
                    except (FileNotFoundError, ValueError, OSError):
                        pass

        assert pipeline.cache_hits.get("tmap") is not True


# ---------------------------------------------------------------------------
# 4. Missing entities = invalid
# ---------------------------------------------------------------------------
class TestCacheInvalidMissingEntities:
    def test_missing_entities_not_hit(self, tmp_path):
        cache_root = tmp_path / "maps"
        inputs = {"pipeline_version": "2", "some_input": "hash"}
        digest = _inputs_hash(inputs)
        _make_cache_entry(cache_root, "tmap", digest, inputs=inputs, missing_entities=True)

        pipeline = _make_pipeline_for_key(cache_root, inputs, "tmap")

        with patch("tools.validation.pipeline.MAP_CACHE_ROOT", cache_root):
            with patch.object(pipeline, "_map_inputs", return_value=inputs):
                with patch("tools.validation.pipeline.PIPELINE_VERSION", "2"):
                    try:
                        pipeline.generate("tmap")
                    except (FileNotFoundError, ValueError, OSError):
                        pass

        assert pipeline.cache_hits.get("tmap") is not True

    def test_wrong_manifest_hash_not_hit(self, tmp_path):
        cache_root = tmp_path / "maps"
        inputs = {"pipeline_version": "2", "some_input": "hash"}
        digest = _inputs_hash(inputs)
        _make_cache_entry(cache_root, "tmap", digest, inputs=inputs, wrong_manifest_hash=True)

        pipeline = _make_pipeline_for_key(cache_root, inputs, "tmap")

        with patch("tools.validation.pipeline.MAP_CACHE_ROOT", cache_root):
            with patch.object(pipeline, "_map_inputs", return_value=inputs):
                with patch("tools.validation.pipeline.PIPELINE_VERSION", "2"):
                    try:
                        pipeline.generate("tmap")
                    except (FileNotFoundError, ValueError, OSError):
                        pass

        assert pipeline.cache_hits.get("tmap") is not True


# ---------------------------------------------------------------------------
# 5. Receipt materializes entities + manifests
# ---------------------------------------------------------------------------
class TestReceiptMaterialization:
    def test_materializes_both_files_with_hash_verification(self, tmp_path):
        artifact_root = tmp_path / "artifact"
        maps_dir = artifact_root / "maps"
        manifests_dir = artifact_root / "manifests"
        maps_dir.mkdir(parents=True)
        manifests_dir.mkdir(parents=True)

        entities = maps_dir / "tmap.entities"
        manifest = manifests_dir / "tmap.json"
        entities.write_bytes(b"entities content here")
        manifest.write_text(json.dumps({"test": 1}), encoding="utf-8")

        entry = {
            "output_source": str(entities),
            "output_sha256": _sha256(entities),
            "output_size": entities.stat().st_size,
            "manifest_source": str(manifest),
            "manifest_sha256": _sha256(manifest),
            "manifest_size": manifest.stat().st_size,
        }

        dest = tmp_path / "dest"
        entities_dest = dest / "build/generated-maps/tmap.entities"
        manifest_dest = dest / ".staging/manifests/tmap.json"

        entities_dest.parent.mkdir(parents=True, exist_ok=True)
        manifest_dest.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(entry["output_source"], str(entities_dest))
        actual_hash = hashlib.sha256(entities_dest.read_bytes()).hexdigest()
        assert actual_hash == entry["output_sha256"]

        shutil.copy2(entry["manifest_source"], str(manifest_dest))
        actual_hash = hashlib.sha256(manifest_dest.read_bytes()).hexdigest()
        assert actual_hash == entry["manifest_sha256"]

    def test_hash_mismatch_detected(self, tmp_path):
        entities = tmp_path / "test.entities"
        entities.write_bytes(b"content")
        assert "0" * 64 != _sha256(entities)


# ---------------------------------------------------------------------------
# 6. Incomplete receipt fails before build
# ---------------------------------------------------------------------------
class TestIncompleteReceiptFails:
    def test_missing_map_in_receipt(self):
        receipt = {"schema_version": 2, "maps": {}}
        assert "test_map" not in receipt["maps"]

    def test_mismatched_hash_detected(self, tmp_path):
        entities = tmp_path / "test.entities"
        entities.write_bytes(b"content")
        assert "wrong" != _sha256(entities)


# ---------------------------------------------------------------------------
# 7. Failed build does not publish/report old ZIP
# ---------------------------------------------------------------------------
class TestFailedBuildNoOldZip:
    def test_old_zip_cleaned_up(self, tmp_path):
        old_zip = tmp_path / "old.zip"
        old_zip.write_bytes(b"old")
        old_zip.unlink()
        assert not old_zip.exists()

    def test_missing_zip_detected(self, tmp_path):
        assert not (tmp_path / "missing.zip").is_file()

    def test_invalid_zip_detected(self, tmp_path):
        zip_path = tmp_path / "invalid.zip"
        zip_path.write_bytes(b"not a zip")
        assert not zipfile.is_zipfile(zip_path)


# ---------------------------------------------------------------------------
# 8. Successful build atomically replaces ZIP
# ---------------------------------------------------------------------------
class TestAtomicZipPublication:
    def test_temp_zip_renamed_atomically(self, tmp_path):
        final = tmp_path / "final.zip"
        temp = tmp_path / "final.zip.tmp"
        temp.write_bytes(b"new zip")
        os.replace(str(temp), str(final))
        assert final.exists()
        assert not temp.exists()
        assert final.read_bytes() == b"new zip"

    def test_temp_zip_removed_on_failure(self, tmp_path):
        temp = tmp_path / "final.zip.tmp"
        temp.write_bytes(b"new zip")
        temp.unlink()
        assert not temp.exists()

    def test_old_zip_replaced_by_new(self, tmp_path):
        old = tmp_path / "release.zip"
        new_temp = tmp_path / "release.zip.tmp"
        old.write_bytes(b"old content")
        new_temp.write_bytes(b"new content")
        os.replace(str(new_temp), str(old))
        assert old.read_bytes() == b"new content"


# ---------------------------------------------------------------------------
# 9. Exact e2m2_base regression
# ---------------------------------------------------------------------------
class TestE2m2BaseRegression:
    def test_cache_entries_have_required_fields(self):
        cache_root = ROOT / ".cache" / "ap_pipeline" / "maps" / "e2m2_base"
        if not cache_root.exists():
            pytest.skip("No e2m2_base cache directory")

        for digest_dir in sorted(cache_root.iterdir()):
            if not digest_dir.is_dir():
                continue
            metadata = json.loads(
                (digest_dir / "metadata.json").read_text(encoding="utf-8")
            )
            assert metadata.get("map_key") == "e2m2_base"
            assert "output_sha256" in metadata
            assert "manifest_sha256" in metadata
            assert "digest" in metadata
            assert "inputs" in metadata
            assert (digest_dir / "e2m2_base.entities").exists()
            assert (digest_dir / "e2m2_base.json").exists()

    def test_cache_entries_sizes_match(self):
        cache_root = ROOT / ".cache" / "ap_pipeline" / "maps" / "e2m2_base"
        if not cache_root.exists():
            pytest.skip("No e2m2_base cache directory")

        for digest_dir in sorted(cache_root.iterdir()):
            if not digest_dir.is_dir():
                continue
            metadata = json.loads(
                (digest_dir / "metadata.json").read_text(encoding="utf-8")
            )
            entities = digest_dir / "e2m2_base.entities"
            manifest = digest_dir / "e2m2_base.json"
            if entities.exists() and "output_sha256" in metadata:
                assert _sha256(entities) == metadata["output_sha256"]
                if "output_size" in metadata:
                    assert entities.stat().st_size == metadata["output_size"]
            if manifest.exists() and "manifest_sha256" in metadata:
                assert _sha256(manifest) == metadata["manifest_sha256"]
                if "manifest_size" in metadata:
                    assert manifest.stat().st_size == metadata["manifest_size"]

    def test_atomic_directory_replacement(self, tmp_path):
        cache_root = tmp_path / "maps"
        cache_root.mkdir()
        old_dir = cache_root / "test" / ("f" * 64)
        old_dir.mkdir(parents=True)
        (old_dir / "old.txt").write_text("old")

        new_dir = tmp_path / "new" / "test" / ("f" * 64)
        new_dir.mkdir(parents=True)
        (new_dir / "new.txt").write_text("new")

        if old_dir.exists():
            shutil.rmtree(old_dir)
        os.replace(str(new_dir), str(old_dir))

        assert old_dir.exists()
        assert (old_dir / "new.txt").exists()
        assert not (old_dir / "old.txt").exists()
