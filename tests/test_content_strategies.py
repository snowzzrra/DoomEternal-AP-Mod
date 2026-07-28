"""Small synthetic contracts for every catalog strategy family."""

from __future__ import annotations

from pathlib import Path

import pytest

from content_catalog import (
    ASSET_STRATEGIES,
    PHYSICAL_STRATEGIES,
    PUBLISHER_STRATEGIES,
    RUNTIME_STRATEGIES,
    AssetSpec,
    PhysicalLocationSpec,
    PublisherSpec,
    RuntimeLocationSpec,
)
from tools.maps.mission_complete_map_patcher import compile_publishers
from tools.maps.ap_map_generator import (
    apply_injected_entity_model_override,
    resolve_donor_model_override,
)
from tools.validation.audit_resource_packages import (
    audit_resource_packages,
    audit_source_asset_dependencies,
)


@pytest.mark.parametrize("strategy", sorted(PHYSICAL_STRATEGIES))
def test_physical_strategy_is_catalog_supported(strategy: str) -> None:
    item = PhysicalLocationSpec("Synthetic", 1, "synthetic", "AP_CHECK_SYNTHETIC", "Synthetic", strategy, {})
    assert item.strategy in PHYSICAL_STRATEGIES


@pytest.mark.parametrize("strategy", sorted(RUNTIME_STRATEGIES))
def test_runtime_strategy_is_catalog_supported(strategy: str) -> None:
    item = RuntimeLocationSpec("Synthetic runtime", 2, strategy, None, {}, {})
    assert item.strategy in RUNTIME_STRATEGIES


@pytest.mark.parametrize("strategy", sorted(PUBLISHER_STRATEGIES))
def test_publisher_strategy_is_catalog_supported(strategy: str) -> None:
    assert strategy in PUBLISHER_STRATEGIES


def test_location_and_goal_publishers_are_independent() -> None:
    publishers = (
        PublisherSpec(
            "mission",
            "synthetic",
            ({"strategy": "map_event_file", "owner": "owner", "filename": "mission.txt", "marker": "MISSION"},),
            ({"strategy": "location_check", "location_id": 3},),
            "ap_session_location",
            "first_success_wins",
        ),
        PublisherSpec(
            "goal",
            "synthetic",
            ({"strategy": "map_event_file", "owner": "owner", "filename": "goal.txt", "marker": "GOAL"},),
            ({"strategy": "campaign_goal"},),
            "ap_session_goal",
            "first_success_wins",
        ),
    )
    compiled = compile_publishers(publishers)
    mission = compiled["publishers"]["mission"]
    goal = compiled["publishers"]["goal"]
    assert mission["relay"] != goal["relay"]
    assert mission["marker_entity"] != goal["marker_entity"]
    assert mission["dump_entity"] != goal["dump_entity"]
    assert "echo MISSION" in compiled["entities"]
    assert "condump mission.txt" in compiled["entities"]
    assert "echo GOAL" in compiled["entities"]
    assert "condump goal.txt" in compiled["entities"]
    assert "echo MISSION; condump" not in compiled["entities"]


@pytest.mark.parametrize("strategy", sorted(ASSET_STRATEGIES))
def test_asset_strategy_is_catalog_supported(strategy: str) -> None:
    item = AssetSpec(
        "asset",
        "synthetic",
        strategy,
        "art/model.lwo",
        "synthetic",
        "synthetic_patch2.resources",
        ("textures/model.tga",) if strategy == "packaged_bundle" else (),
        "required",
    )
    assert item.strategy in ASSET_STRATEGIES


def test_packaged_bundle_audit_uses_resource_base_not_patch_owner(tmp_path: Path) -> None:
    source = tmp_path / "assets" / "synthetic" / "art"
    packaged = tmp_path / "mod" / "synthetic" / "art"
    source.mkdir(parents=True)
    packaged.mkdir(parents=True)
    (source / "model.lwo").write_bytes(b"model")
    (packaged / "model.lwo").write_bytes(b"model")
    (source.parent / "textures").mkdir()
    (packaged.parent / "textures").mkdir()
    (source.parent / "textures" / "model.tga").write_bytes(b"texture")
    (packaged.parent / "textures" / "model.tga").write_bytes(b"texture")
    asset = AssetSpec(
        "bundle",
        "synthetic",
        "packaged_bundle",
        "art/model.lwo",
        "synthetic",
        "synthetic_patch2.resources",
        ("textures/model.tga",),
        "required",
    )
    records = audit_resource_packages(tmp_path / "assets", tmp_path / "mod", assets=(asset,))
    assert records[0]["resource_base"] == "synthetic"
    assert records[0]["resource_owner"].endswith("_patch2.resources")


def test_incomplete_packaged_bundle_fails_audit(tmp_path: Path) -> None:
    source = tmp_path / "assets" / "synthetic" / "art"
    packaged = tmp_path / "mod" / "synthetic" / "art"
    source.mkdir(parents=True)
    packaged.mkdir(parents=True)
    (source / "model.lwo").write_bytes(b"model")
    (packaged / "model.lwo").write_bytes(b"model")
    asset = AssetSpec(
        "bundle",
        "synthetic",
        "packaged_bundle",
        "art/model.lwo",
        "synthetic",
        "synthetic_patch2.resources",
        ("textures/missing.tga",),
        "required",
    )
    with pytest.raises(AssertionError, match="DEPENDENCY_MISSING"):
        audit_resource_packages(
            tmp_path / "assets", tmp_path / "mod", assets=(asset,)
        )


def test_resident_model_requires_no_dependency_bundle(tmp_path: Path) -> None:
    maps = tmp_path / "maps"
    maps.mkdir()
    (maps / "synthetic.map").write_text(
        'renderModelInfo = { model = "art/resident.lwo"; }',
        encoding="utf-8",
    )
    asset = AssetSpec(
        "resident",
        "synthetic",
        "resident_model",
        "art/resident.lwo",
        "synthetic",
        "synthetic_patch2.resources",
        (),
        "resident",
    )
    records = audit_resource_packages(
        tmp_path / "assets",
        tmp_path / "mod",
        assets=(asset,),
        source_map_root=maps,
    )
    assert records[0]["sha256"] == "resident"


def _donor_block(kind: str) -> str:
    inherit, model = {
        "question_mark": (
            "pickup/collectible/question_mark",
            "art/pickups/question_mark_a.lwo",
        ),
        "codex": ("progress/codex", "art/pickups/codex.lwo"),
    }[kind]
    return f'''entity {{
    layers {{ "gameplay" }}
    entityDef donor {{
        inherit = "{inherit}";
        class = "idProp2";
        edit = {{
            triggerDef = "trigger/props/pickup_large";
            spawnPosition = {{ x = 1; y = 2; z = 3; }}
            renderModelInfo = {{ model = "{model}"; }}
            clipModelInfo = {{ type = "CLIPMODEL_BOX"; }}
            interaction = {{ initalState = "idle"; }}
        }}
    }}
}}'''


def _override_asset(kind: str = "codex") -> dict:
    return {
        "strategy": "donor_model_override",
        "scope": "injected_entity_only",
        "donor": {"kind": kind, "selection": "per_location_source"},
    }


def _injected_visual(model: str) -> str:
    return f'''entity {{
    layers {{ "gameplay" }}
    entityDef ap_location_visual_1 {{
        class = "idProp2";
        edit = {{
            triggerDef = "trigger/props/pickup_large";
            spawnPosition = {{ x = 1; y = 2; z = 3; }}
            renderModelInfo = {{ model = "{model}"; }}
            clipModelInfo = {{ type = "CLIPMODEL_BOX"; }}
            interaction = {{ initalState = "idle"; }}
        }}
    }}
}}'''


@pytest.mark.parametrize("kind", ["question_mark", "codex"])
def test_donor_model_override_supports_generic_donors(kind: str) -> None:
    donor = _donor_block(kind)
    donor_model = resolve_donor_model_override(
        donor, donor, _override_asset(kind)
    )
    visual = _injected_visual(donor_model)
    overridden = apply_injected_entity_model_override(
        visual, "art/pickups/question_mark_a.lwo"
    )
    assert 'model = "art/pickups/question_mark_a.lwo";' in overridden


def test_model_override_preserves_injected_structure_and_source_donor() -> None:
    donor = _donor_block("codex")
    original_donor = donor
    visual = _injected_visual("art/pickups/codex.lwo")
    overridden = apply_injected_entity_model_override(
        visual, "art/pickups/question_mark_a.lwo"
    )
    assert donor == original_donor
    assert overridden.replace(
        "art/pickups/question_mark_a.lwo", "art/pickups/codex.lwo"
    ) == visual
    for field in (
        "layers", "triggerDef", "spawnPosition", "clipModelInfo", "interaction",
    ):
        assert field in overridden


def test_model_override_rejects_non_injected_entity() -> None:
    with pytest.raises(ValueError, match="limited to injected"):
        apply_injected_entity_model_override(
            _injected_visual("art/pickups/codex.lwo").replace(
                "ap_location_visual_1", "game_progress_codex_1"
            ),
            "art/pickups/question_mark_a.lwo",
        )


def test_missing_override_bundle_fails_source_preflight(tmp_path: Path) -> None:
    model = tmp_path / "synthetic" / "art"
    model.mkdir(parents=True)
    (model / "ap.lwo").write_bytes(b"model")
    override = AssetSpec(
        "override", "synthetic", "donor_model_override", "art/ap.lwo",
        "synthetic", "synthetic_patch.resources", (), "required",
        {"kind": "codex", "selection": "per_location_source"},
        "missing", "injected_entity_only",
        ("trigger", "collision", "transform", "layers", "interaction"),
    )
    with pytest.raises(AssertionError, match="BUNDLE_MISSING"):
        audit_source_asset_dependencies(tmp_path, (override,))


def test_missing_model_payload_fails_with_short_dependency_error(
    tmp_path: Path,
) -> None:
    model = tmp_path / "synthetic" / "art"
    model.mkdir(parents=True)
    (model / "ap.lwo").write_bytes(b"model")
    bundle = AssetSpec(
        "payload", "synthetic", "streamdb", "art/missing_payload.lwo",
        "streamdb", "synthetic_patch.resources", (), "embedded",
    )
    override = AssetSpec(
        "override", "synthetic", "donor_model_override", "art/ap.lwo",
        "synthetic", "synthetic_patch.resources", (), "required",
        {"kind": "codex", "selection": "per_location_source"},
        "payload", "injected_entity_only",
        ("trigger", "collision", "transform", "layers", "interaction"),
    )
    with pytest.raises(AssertionError, match=r"DEPENDENCY_MISSING.*payload"):
        audit_source_asset_dependencies(tmp_path, (override, bundle))
