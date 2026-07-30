"""Data-driven content catalog contracts; no map key is encoded in this test."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from challenge_registry import challenge_registry_document, validate_challenge_registry
from content_catalog import _map_content_packages, discover_maps, load_content_catalog
from map_registry import generation_plan, load_map_registry, release_plan
from publisher_contracts import publishers_by_trigger
from tools.content.new_map import create_package
from tools.content.compile_content_catalog import render


ROOT = Path(__file__).parents[1]


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


def test_enabled_catalog_matches_release_plan_and_authorial_surfaces(content_catalog) -> None:
    specs = tuple(content_catalog.enabled_maps())
    catalog_keys = [spec.key for spec in specs]
    assert [
        plan.map_key
        for plan in release_plan(load_map_registry(authorial=True))
    ] == catalog_keys
    for spec in specs:
        assert spec.manifest_path.is_file()
        assert (ROOT / "baselines" / "maps" / f"{spec.key}.json").is_file()
        package = spec.data.get("package_directory")
        if package:
            assert {
                path.name for path in (ROOT / package).glob("*.json")
            } == {
                "assets.json",
                "descriptor.json",
                "locations.json",
                "onboarding.json",
                "publishers.json",
                "runtime.json",
            }


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


def test_synthetic_package_feeds_every_normalized_consumer(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    for name in ("data", "level_configs", "manifests"):
        shutil.copytree(root / name, tmp_path / name)
    directory = create_package(
        "synthetic_future",
        "Synthetic Future",
        "synthetic_future.map",
        "game/sp/synthetic_future/synthetic_future.resources",
        "game/sp/synthetic_future/synthetic_future_patch1.resources",
        root=tmp_path,
    )
    legacy_destination = next(iter(
        json.loads((tmp_path / "data" / "map_sources.json").read_text())["maps"].values()
    ))["runtime_map"]

    def write(name: str, document: dict) -> None:
        (directory / name).write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )

    descriptor = json.loads((directory / "descriptor.json").read_text())
    descriptor.update({
        "enabled": True,
        "source_sha256": "0" * 64,
        "source_size": 1,
        "resource_path": descriptor["resource_owner"],
        "resource_priority": 1,
        "supported_game_revision": "synthetic",
        "route": {
            "regions": ["Synthetic Future"],
            "connections": [["Sentinel Prime", "Synthetic Future", "Synthetic route"]],
        },
    })
    write("descriptor.json", descriptor)
    write("locations.json", {
        "schema_version": 1,
        "region": "Synthetic Future",
        "entities": {"AP_CHECK_INTERACT_SYNTHETIC_PANEL": 8880001},
        "names": {"8880001": "Synthetic Future - Panel"},
        "target_policies": {},
        "assets": [],
    })
    write("runtime.json", {
        "schema_version": 1,
        "locations": [
            {
                "category": "mission_challenges",
                "name": "Synthetic Future - Mission Challenge - Panel",
                "location_id": 8880002,
                "strategy": "physical_event_equivalent",
                "mission_key": "synthetic_future",
                "signal": {
                    "kind": "physical_event_equivalent",
                    "physical_location_ids": [8880001],
                    "required_count": 1,
                    "unlockable": "mission_challenge/synthetic_future/challenge_1",
                },
                "completion_owner": {"completion_stat": "STAT_SYNTHETIC_COMPLETE"},
            },
            {
                "category": "all_mission_challenges",
                "name": "Synthetic Future - All Mission Challenges Completed",
                "location_id": 8880003,
                "strategy": "aggregate",
                "mission_key": "synthetic_future",
                "signal": {
                    "kind": "aggregate",
                    "children": [8880002],
                    "required_count": 1,
                    "authority": "server_checked_locations",
                },
            },
            {
                "category": "mission_complete",
                "name": "Synthetic Future - Mission Complete",
                "location_id": 8880004,
                "strategy": "map_terminal",
                "mission_key": "synthetic_future",
                "signal": {
                    "kind": "map_terminal",
                    "runtime_map": "game/sp/synthetic_future/synthetic_future",
                },
            },
        ],
    })
    write("publishers.json", {
        "schema_version": 1,
        "publishers": [{
            "key": "synthetic_campaign_goal",
            "map_key": "synthetic_future",
            "triggers": [{
                "strategy": "native_transition",
                "from_map": "game/sp/synthetic_future/synthetic_future",
                "to_map": legacy_destination,
            }],
            "effects": [{"strategy": "campaign_goal"}],
            "dedupe_scope": "synthetic_goal",
            "fallback_policy": "first_success_wins",
        }],
    })
    catalog = load_content_catalog(tmp_path)
    spec = catalog.map("synthetic_future")
    assert next(item for item in catalog.physical_locations if item.map_key == spec.key).strategy == "interactable"
    assert catalog.location_names[8880001] == "Synthetic Future - Panel"
    assert {item.location_id for item in catalog.runtime_locations if item.mission_key == spec.key} == {
        8880002, 8880003, 8880004,
    }
    registry = challenge_registry_document(catalog)
    validate_challenge_registry(registry)
    assert any(item["location_id"] == 8880002 for item in registry["mission_challenges"])
    assert any(key[0] == "native_transition" for key in publishers_by_trigger(catalog.publishers))
    assert ("Sentinel Prime", "Synthetic Future", "Synthetic route") in catalog.route["connections"]
    assert any(plan.map_key == spec.key for plan in generation_plan(load_map_registry(root=tmp_path)))
