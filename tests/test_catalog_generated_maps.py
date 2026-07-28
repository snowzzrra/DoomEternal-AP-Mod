"""Integration map assertions consuming the session cache, never per-test maps."""

from __future__ import annotations

import pytest

from content_catalog import discover_maps


@pytest.mark.parametrize("map_spec", discover_maps(), ids=lambda spec: spec.key)
def test_generated_map_has_each_declared_physical_check(map_spec, content_catalog, temporary_generated_maps) -> None:
    generated = temporary_generated_maps[map_spec.key].read_text(encoding="utf-8")
    locations = [item for item in content_catalog.physical_locations if item.map_key == map_spec.key]
    for location in locations:
        assert f"AP_CHECK_EVENT_{location.location_id}" in generated, (
            f"{map_spec.key}: missing generated event for {location.location_id}/{location.name}; "
            f"run pytest --map {map_spec.key} -m integration -q --maxfail=1"
        )


@pytest.mark.parametrize("map_spec", discover_maps(), ids=lambda spec: spec.key)
def test_generated_map_uses_declared_visual_asset_strategy(
    map_spec, content_catalog, temporary_generated_maps
) -> None:
    assets = [asset for asset in content_catalog.assets if asset.map_key == map_spec.key]
    if not assets:
        return
    generated = temporary_generated_maps[map_spec.key].read_text(encoding="utf-8")
    for asset in assets:
        assert f'model = "{asset.model}";' in generated
        if asset.strategy == "resident_model":
            assert not asset.dependencies
