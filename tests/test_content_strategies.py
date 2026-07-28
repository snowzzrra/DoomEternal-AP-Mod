"""Small synthetic contracts for every catalog strategy family."""

from __future__ import annotations

import hashlib
import zipfile
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
        "model": "art/pickups/question_mark_a.lwo",
        "donor": {"kind": kind, "selection": "per_location_source"},
        "replacement_slot_policy": "safe_resident_static_lwo",
        "replacement_slot": {
            "model_path": "art/pickups/question_mark_a.lwo",
        },
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
    assert set(_override_asset(kind)) >= {"donor", "replacement_slot"}
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


def _importer_override(
    *,
    resource_hash: str = "0" * 64,
    streamdb_hash: str = "0" * 64,
    streamdb_payload: str = "art/ap_id#123.lwo",
    provenance: dict | None = None,
    usage_policy: str = "no_vanilla_entity_references",
    allowlist: tuple[str, ...] = (),
) -> AssetSpec:
    return AssetSpec(
        key="override",
        map_key="synthetic",
        strategy="donor_model_override",
        model="art/ap.lwo",
        resource_base="synthetic",
        resource_owner="synthetic_patch.resources",
        dependencies=(),
        dependency_policy="model_importer_bundle",
        donor={"kind": "codex", "selection": "named_entity", "entity": "donor"},
        replacement_slot_policy="safe_resident_static_lwo",
        replacement_slot={
            "model_path": "art/ap.lwo",
            "resource_archive": "synthetic",
            "material2": "art/ap",
            "import_bundle": "ap_id#123",
            "asset_id": "123",
            "streamdb_payload": streamdb_payload,
            "resource_payload_sha256": resource_hash,
            "streamdb_payload_sha256": streamdb_hash,
            "vanilla_reference_allowlist": allowlist,
            "provenance": provenance or {
                "producer": "Doom Eternal Model Importer v1.2",
                "source_obj": "Archipelago.obj",
                "source_obj_sha256": "1" * 64,
            },
        },
        usage_policy=usage_policy,
        preserve=(
            "trigger", "collision", "transform", "layers", "interaction",
        ),
    )


def _write_importer_bundle(root: Path) -> tuple[str, str]:
    resource = root / "synthetic" / "art" / "ap.lwo"
    streamdb = root / "streamdb" / "art" / "ap_id#123.lwo"
    resource.parent.mkdir(parents=True)
    streamdb.parent.mkdir(parents=True)
    resource.write_bytes(
        b"\0" * 48 + b"art/ap" + b"\0" * 128
    )
    streamdb.write_bytes(b"STREAMDB" + b"\0" * 1024)
    return (
        hashlib.sha256(resource.read_bytes()).hexdigest(),
        hashlib.sha256(streamdb.read_bytes()).hexdigest(),
    )


def test_missing_resource_archive_fails_source_preflight(tmp_path: Path) -> None:
    override = _importer_override()
    with pytest.raises(AssertionError, match="IMPORTER_BUNDLE_INCOMPLETE"):
        audit_source_asset_dependencies(tmp_path, (override,))


def test_missing_streamdb_fails_source_preflight(tmp_path: Path) -> None:
    resource = tmp_path / "synthetic" / "art" / "ap.lwo"
    resource.parent.mkdir(parents=True)
    resource.write_bytes(b"\0" * 48 + b"art/ap" + b"\0" * 128)
    override = _importer_override()
    with pytest.raises(AssertionError, match="IMPORTER_BUNDLE_INCOMPLETE"):
        audit_source_asset_dependencies(tmp_path, (override,))


def test_renamed_payload_is_not_a_valid_import_bundle(tmp_path: Path) -> None:
    resource_hash, streamdb_hash = _write_importer_bundle(tmp_path)
    override = _importer_override(
        resource_hash=resource_hash,
        streamdb_hash=streamdb_hash,
        streamdb_payload="art/renamed_id#123.lwo",
    )
    with pytest.raises(AssertionError, match="IMPORTER_IDENTITY_INVALID"):
        audit_source_asset_dependencies(tmp_path, (override,))


def test_stub_without_importer_provenance_fails(tmp_path: Path) -> None:
    resource_hash, streamdb_hash = _write_importer_bundle(tmp_path)
    override = _importer_override(
        resource_hash=resource_hash,
        streamdb_hash=streamdb_hash,
        provenance={"producer": "manual stub"},
    )
    with pytest.raises(AssertionError, match="IMPORTER_IDENTITY_INVALID"):
        audit_source_asset_dependencies(tmp_path, (override,))


def _write_synthetic_source(root: Path, *, vanilla_uses_slot: bool) -> None:
    root.mkdir()
    source = _donor_block("codex")
    if vanilla_uses_slot:
        source += "\n" + _injected_visual("art/ap.lwo").replace(
            "ap_location_visual_1", "vanilla_visible_prop"
        )
    (root / "synthetic.map").write_text(source, encoding="utf-8")


def test_slot_used_by_vanilla_entity_fails(tmp_path: Path) -> None:
    resource_hash, streamdb_hash = _write_importer_bundle(
        tmp_path / "assets"
    )
    _write_importer_bundle(tmp_path / "mod")
    _write_synthetic_source(tmp_path / "maps", vanilla_uses_slot=True)
    override = _importer_override(
        resource_hash=resource_hash,
        streamdb_hash=streamdb_hash,
    )
    with pytest.raises(AssertionError, match="vanilla references"):
        audit_resource_packages(
            tmp_path / "assets",
            tmp_path / "mod",
            assets=(override,),
            source_map_root=tmp_path / "maps",
        )


def test_zero_vanilla_references_passes(tmp_path: Path) -> None:
    resource_hash, streamdb_hash = _write_importer_bundle(
        tmp_path / "assets"
    )
    _write_importer_bundle(tmp_path / "mod")
    _write_synthetic_source(tmp_path / "maps", vanilla_uses_slot=False)
    override = _importer_override(
        resource_hash=resource_hash,
        streamdb_hash=streamdb_hash,
    )
    records = audit_resource_packages(
        tmp_path / "assets",
        tmp_path / "mod",
        assets=(override,),
        source_map_root=tmp_path / "maps",
    )
    assert records[0]["asset_id"] == "123"


def test_final_zip_contains_both_importer_trees(tmp_path: Path) -> None:
    resource_hash, streamdb_hash = _write_importer_bundle(
        tmp_path / "assets"
    )
    _write_importer_bundle(tmp_path / "mod")
    _write_synthetic_source(tmp_path / "maps", vanilla_uses_slot=False)
    zip_path = tmp_path / "mod.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(
            tmp_path / "mod" / "synthetic" / "art" / "ap.lwo",
            "synthetic/art/ap.lwo",
        )
        archive.write(
            tmp_path / "mod" / "streamdb" / "art" / "ap_id#123.lwo",
            "streamdb/art/ap_id#123.lwo",
        )
    override = _importer_override(
        resource_hash=resource_hash,
        streamdb_hash=streamdb_hash,
    )
    audit_resource_packages(
        tmp_path / "assets",
        tmp_path / "mod",
        assets=(override,),
        source_map_root=tmp_path / "maps",
        zip_path=zip_path,
    )
