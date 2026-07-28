"""Data-driven content catalog contracts; no map key is encoded in this test."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from content_catalog import _map_content_packages, discover_maps
from tools.content.new_map import create_package
from tools.content.compile_content_catalog import render


@pytest.mark.parametrize("map_spec", discover_maps(), ids=lambda item: item.key)
def test_catalog_discovers_resolved_map_content(map_spec) -> None:
    assert map_spec.level_config_path.is_file()
    assert map_spec.manifest_path.is_file()
    assert map_spec.resource_base
    assert map_spec.resource_owner.endswith(".resources")


def test_catalog_locations_and_assets_are_generic(content_catalog, generated_content_snapshot: str) -> None:
    catalog = content_catalog
    physical_ids = [item.location_id for item in catalog.physical_locations]
    assert len(physical_ids) == len(set(physical_ids))
    assert all(item.ap_check.startswith("AP_CHECK_") for item in catalog.physical_locations)
    assert all("_patch" not in item.resource_base for item in catalog.assets)
    assert render(catalog) == generated_content_snapshot


def test_synthetic_fixture_is_data_only(discovered_map_specs) -> None:
    fixture = Path(__file__).parent / "fixtures" / "content" / "minimal_map.json"
    item = json.loads(fixture.read_text(encoding="utf-8"))
    assert item["map_key"] not in {spec.key for spec in discovered_map_specs}
    assert item["generated_output"].endswith(".entities")
    assert item["resource_path"].endswith(".resources")


def test_map_content_package_scaffolder_is_valid_and_python_free(tmp_path: Path) -> None:
    directory = create_package(
        "synthetic_future",
        "Synthetic Future",
        "synthetic_future.map",
        "game/sp/synthetic_future/synthetic_future",
        "game/sp/synthetic_future/synthetic_future_patch1.resources",
        root=tmp_path,
    )
    assert {path.name for path in directory.iterdir()} == {
        "descriptor.json", "locations.json", "runtime.json",
        "publishers.json", "assets.json", "onboarding.json",
    }
    assert len(_map_content_packages(tmp_path)) == 1
