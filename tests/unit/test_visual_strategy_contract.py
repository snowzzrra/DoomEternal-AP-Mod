"""Focused data selection for the generic donor-model override."""

from pathlib import Path

from content_catalog import load_content_catalog


def test_sentinel_uses_codex_donor_and_shared_ap_model_from_data() -> None:
    catalog = load_content_catalog()
    override = next(
        asset for asset in catalog.assets
        if asset.map_key == "e2m4_boss"
        and asset.strategy == "donor_model_override"
    )
    assert override.donor["kind"] == "codex"
    assert override.donor["selection"] == "named_entity"
    assert override.model == "art/pickups/question_mark_a.lwo"
    assert override.scope == "injected_entity_only"
    assert all(
        asset.strategy != "donor_model_override"
        for asset in catalog.assets
        if asset.map_key != "e2m4_boss"
    )


def test_generator_has_no_sentinel_specific_visual_branch() -> None:
    generator = (
        Path(__file__).resolve().parents[2]
        / "tools" / "maps" / "ap_map_generator.py"
    ).read_text(encoding="utf-8")
    assert 'if map_key == "e2m4_boss"' not in generator
    assert "if map_key == 'e2m4_boss'" not in generator
