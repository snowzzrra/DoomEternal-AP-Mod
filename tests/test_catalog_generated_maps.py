"""Integration map assertions consuming the session cache, never per-test maps."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from content_catalog import discover_maps
from tools.maps.ap_map_generator import find_entity_block_bounds
from tools.validation.audit_resource_packages import _model_references


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
        if asset.strategy in {
            "resident_model", "donor_model_override", "packaged_bundle",
        }:
            assert f'model = "{asset.model}";' in generated
        if asset.strategy == "packaged_bundle":
            visual_blocks = [
                block for block in generated.split("entity {")
                if "entityDef ap_location_visual_" in block
            ]
            assert visual_blocks
            assert all(
                f'model = "{asset.model}";' in block
                for block in visual_blocks
            )
            assert "art/pickups/question_mark_a.lwo" not in "".join(
                visual_blocks
            )
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


@pytest.mark.parametrize("map_spec", discover_maps(), ids=lambda spec: spec.key)
def test_ap_visual_presentation_preserves_motion_and_functional_contracts(
    map_spec, content_catalog, temporary_generated_maps
) -> None:
    assets = [
        asset for asset in content_catalog.assets
        if asset.map_key == map_spec.key and asset.visual_presentation_policy
    ]
    if not assets:
        return
    assert len(assets) == 1
    asset = assets[0]
    policy = asset.visual_presentation_policy
    generated = temporary_generated_maps[map_spec.key].read_text(encoding="utf-8")
    source = (ROOT / "vanillamaps" / map_spec.source_file).read_text(
        encoding="utf-8"
    )
    map_config = json.loads(map_spec.level_config_path.read_text(encoding="utf-8"))
    references = _model_references(generated, asset.model)
    locations = [
        item for item in content_catalog.physical_locations
        if item.map_key == map_spec.key
    ]
    visual_ids = {
        int(name.removeprefix("ap_location_visual_")) for name in references
    }
    locations = [
        location for location in locations if location.location_id in visual_ids
    ]
    assert references == {
        f"ap_location_visual_{location.location_id}" for location in locations
    }

    for location in locations:
        source_entity = location.ap_check.removeprefix("AP_CHECK_").lower()
        location_policy = map_config.get("target_policies", {}).get(
            source_entity, {}
        )
        source_bounds = find_entity_block_bounds(source, source_entity)
        visual_name = f"ap_location_visual_{location.location_id}"
        trigger_name = location_policy.get(
            "independent_entity_name", f"ap_independent_{source_entity}"
        )
        cleanup_name = f"ap_remove_location_visual_{location.location_id}"
        check_name = location.ap_check
        assert source_bounds is not None
        source_block = source[source_bounds[0]:source_bounds[1]]
        blocks = {}
        for name in (visual_name, trigger_name, cleanup_name, check_name):
            bounds = find_entity_block_bounds(generated, name)
            assert bounds is not None, f"{map_spec.key}: missing {name}"
            blocks[name] = generated[bounds[0]:bounds[1]]

        visual = blocks[visual_name]
        trigger = blocks[trigger_name]
        cleanup = blocks[cleanup_name]
        check = blocks[check_name]
        configured_visual = location_policy.get("independent_visual")
        expected_class = (
            configured_visual.get("class", "idDynamicEntity")
            if configured_visual else "idProp2"
        )
        assert f'class = "{expected_class}";' in visual
        assert f'model = "{asset.model}";' in visual
        assert (
            f'thinkComponentDecl = "{policy["think_component"]}";'
            in visual
        )
        assert 'type = "CLIPMODEL_NONE";' in visual
        assert all(
                token not in visual
                for token in (
                    "fxDecl", "updateFX", "particle", "renderLight",
                    "targets =", "renderParms", "shaderParm", "materialOverride",
                )
            )
        source_bind = re.search(r'bindParent\s*=\s*"([^"]+)";', source_block)
        if source_bind:
            assert f'bindParent = "{source_bind.group(1)}";' in visual
        else:
            assert "bindInfo" not in visual
        assert "triggerOnce = true;" in trigger
        assert 'type = "CLIPMODEL_BOX";' in trigger
        assert f'"{check_name}";' in trigger
        assert f'"{cleanup_name}";' in trigger
        assert f'item[0] = "{visual_name}";' in cleanup
        assert f'"ap_event_{location.location_id}";' in check
        feedback = map_config.get("location_feedback", {}).get(check_name, {})
        notification = f'"ap_notify_location_{location.location_id}";'
        assert (notification in check) is not (
            feedback.get("policy") == "vanilla_only"
        )
        assert f"entityDef {source_entity} {{" in source_block


def test_onboarded_physical_candidates_remove_vanilla_reward_owners(
    temporary_generated_maps,
) -> None:
    physical_classes = {
        "idInteractable_GiveItems",
        "idInteractable_WorldCache",
        "idProp2",
    }
    for map_key, generated_path in temporary_generated_maps.items():
        onboarding_path = ROOT / "content" / "maps" / map_key / "onboarding.json"
        if not onboarding_path.is_file():
            continue
        inventory = json.loads(onboarding_path.read_text(encoding="utf-8")).get(
            "candidate_inventory", ()
        )
        generated = generated_path.read_text(encoding="utf-8")
        for candidate in inventory:
            if (
                candidate.get("decision") != "include"
                or candidate.get("class") not in physical_classes
            ):
                continue
            entity_name = candidate["entity"]
            assert f"entityDef {entity_name} {{" not in generated
            assert generated.count(
                f"entityDef ap_independent_{entity_name} {{"
            ) == 1


def test_taras_mastery_tokens_replace_vanilla_rewards_once(temporary_generated_maps) -> None:
    if "e3m1_slayer" not in temporary_generated_maps:
        pytest.skip("Taras Nabad was not selected for this focused map run")
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
        assert 'model = "art/pickups/codex.lwo";' in visual_block

    assert 'useableComponentDecl = "propitem/mastery_token/weapon";' not in generated
