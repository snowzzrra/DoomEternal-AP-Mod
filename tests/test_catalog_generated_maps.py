"""Integration map assertions consuming the session cache, never per-test maps."""

from __future__ import annotations

from pathlib import Path

import pytest

from content_catalog import discover_maps
from tools.maps.ap_map_generator import find_entity_block_bounds


ROOT = Path(__file__).resolve().parents[1]


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
        if asset.strategy in {"resident_model", "donor_model_override"}:
            assert f'model = "{asset.model}";' in generated
        if asset.strategy == "resident_model":
            assert not asset.dependencies
        if asset.strategy == "donor_model_override":
            visual_blocks = [
                block for block in generated.split("entity {")
                if "entityDef ap_location_visual_" in block
            ]
            assert visual_blocks
            assert all(
                f'model = "{asset.model}";' in block
                for block in visual_blocks
            )
            assert "modelDecl" not in generated


def test_taras_mastery_tokens_replace_vanilla_rewards_once(temporary_generated_maps) -> None:
    source = (ROOT / "vanillamaps" / "e3m1_slayer.map").read_text(encoding="utf-8")
    generated = temporary_generated_maps["e3m1_slayer"].read_text(encoding="utf-8")
    tokens = (
        (
            "pickups_progress_mastery_token_weapon_1_e3m1",
            "AP_CHECK_PICKUPS_PROGRESS_MASTERY_TOKEN_WEAPON_1_E3M1",
            7770338,
            ("x = -66.1289673;", "y = 310.241547;", "z = -132.311172;"),
        ),
        (
            "pickups_progress_mastery_token_weapon_3_e3m1",
            "AP_CHECK_PICKUPS_PROGRESS_MASTERY_TOKEN_WEAPON_3_E3M1",
            7770339,
            ("x = -171.009979;", "y = 140.030029;", "z = -42.9099998;"),
        ),
    )

    for entity_name, ap_check, location_id, position in tokens:
        assert source.count(f"entityDef {entity_name} {{") == 1
        bounds = find_entity_block_bounds(source, entity_name)
        assert bounds is not None
        source_block = source[bounds[0]:bounds[1]]
        assert 'class = "idProp2";' in source_block
        assert 'useableComponentDecl = "propitem/mastery_token/weapon";' in source_block
        assert "targets = {" not in source_block

        independent_name = f"ap_independent_{entity_name}"
        assert generated.count(f"entityDef {entity_name} {{") == 0
        assert generated.count(f"entityDef {independent_name} {{") == 1
        assert generated.count(f"entityDef {ap_check} {{") == 1
        assert generated.count(f"AP_CHECK_EVENT_{location_id}") == 1
        assert generated.count(f"entityDef ap_location_visual_{location_id} {{") == 1

        trigger_block = next(
            block for block in generated.split("entity {")
            if f"entityDef {independent_name} {{" in block
        )
        visual_block = next(
            block for block in generated.split("entity {")
            if f"entityDef ap_location_visual_{location_id} {{" in block
        )
        assert all(coordinate in trigger_block for coordinate in position)
        assert f'item[0] = "{ap_check}";' in trigger_block
        assert trigger_block.count(ap_check) == 1
        assert 'useableComponentDecl = "propitem/mastery_token/weapon";' not in trigger_block
        assert 'model = "art/pickups/question_mark_a.lwo";' in visual_block

    assert 'useableComponentDecl = "propitem/mastery_token/weapon";' not in generated
