"""Focused data selection for the generic donor-model override."""

from pathlib import Path

from content_catalog import load_content_catalog


def test_sentinel_separates_codex_donor_from_imported_replacement_slot() -> None:
    catalog = load_content_catalog()
    override = next(
        asset for asset in catalog.assets
        if asset.map_key == "e2m4_boss"
        and asset.strategy == "donor_model_override"
    )
    assert override.donor["kind"] == "codex"
    assert override.donor["selection"] == "named_entity"
    assert override.replacement_slot_policy == "safe_resident_static_lwo"
    assert override.replacement_slot["model_path"] == "art/pickups/codex.lwo"
    assert override.replacement_slot["resource_archive"] == "e2m4_boss"
    assert override.replacement_slot["material2"] == "art/pickups/codex"
    assert override.replacement_slot["asset_id"] == "13626551837332538432"
    assert override.replacement_slot["import_bundle"] == (
        "codex_id#13626551837332538432"
    )
    assert override.usage_policy == "removed_vanilla_entity_allowlist"
    assert all(
        asset.strategy != "donor_model_override"
        for asset in catalog.assets
        if asset.map_key != "e2m4_boss"
    )


def test_vanilla_codex_entities_remain_original_in_the_source_map() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "vanillamaps" / "e2m4_boss.map"
    ).read_text(encoding="utf-8")
    for index in range(1, 10):
        name = f"game_progress_codex_{index}_e2m4"
        start = source.index(f"entityDef {name} ")
        end = source.index("\nentity {", start)
        block = source[start:end]
        assert 'model = "art/pickups/codex.lwo";' in block
