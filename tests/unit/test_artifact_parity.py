"""Pytest unit tests for artifact parity between catalog compilation staging and release/source."""

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
import pytest

ROOT = Path(__file__).parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools" / "catalog") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools" / "catalog"))

from tools.catalog.compile_catalog import compile_catalog


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_catalog_compilation_produces_all_staging_artifacts():
    with tempfile.TemporaryDirectory() as tmp_dir:
        staging_dir = Path(tmp_dir) / "runtime_catalog"
        apworld_gen = Path(tmp_dir) / "generated_content.py"
        apworld_gen.write_text("", encoding="utf-8")

        compile_catalog(output=apworld_gen, output_root=staging_dir)

        expected_files = [
            "catalog.json",
            "content_identity.json",
            "generated_content.py",
            "items.json",
        ]
        for filename in expected_files:
            file_path = staging_dir / filename
            assert file_path.exists(), f"Missing staging artifact {filename}"
            assert file_path.stat().st_size > 0, f"Empty staging artifact {filename}"


def test_staging_and_source_identity_and_items_parity():
    with tempfile.TemporaryDirectory() as tmp_dir:
        staging_dir = Path(tmp_dir) / "runtime_catalog"
        apworld_gen = Path(tmp_dir) / "generated_content.py"
        apworld_gen.write_text("", encoding="utf-8")

        compile_catalog(output=apworld_gen, output_root=staging_dir)

        # Check content_identity.json parity
        source_identity = ROOT / "data" / "content_identity.json"
        staging_identity = staging_dir / "content_identity.json"
        assert file_sha256(staging_identity) == file_sha256(source_identity)
        assert staging_identity.stat().st_size == source_identity.stat().st_size

        # Check items.json parity
        source_items = ROOT / "data" / "items.json"
        staging_items = staging_dir / "items.json"
        assert file_sha256(staging_items) == file_sha256(source_items)
        assert staging_items.stat().st_size == source_items.stat().st_size


def test_json_artifacts_formatting_and_encoding():
    with tempfile.TemporaryDirectory() as tmp_dir:
        staging_dir = Path(tmp_dir) / "runtime_catalog"
        apworld_gen = Path(tmp_dir) / "generated_content.py"
        apworld_gen.write_text("", encoding="utf-8")

        compile_catalog(output=apworld_gen, output_root=staging_dir)

        for json_name in ["catalog.json", "content_identity.json", "items.json"]:
            path = staging_dir / json_name
            content = path.read_text(encoding="utf-8")
            assert content.endswith("\n")
            
            data = json.loads(content)
            assert isinstance(data, (dict, list))
            if json_name != "items.json":
                re_encoded = json.dumps(data, indent=2, sort_keys=True) + "\n"
                assert content == re_encoded, f"{json_name} is not deterministically formatted with indent=2, sort_keys=True"
