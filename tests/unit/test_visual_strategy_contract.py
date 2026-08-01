"""Focused data selection for the generic donor-model override."""

import hashlib
import struct
from pathlib import Path

from content_catalog import load_content_catalog


ROOT = Path(__file__).resolve().parents[2]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        if asset.map_key not in ("e2m4_boss", "e3m4_boss")
    )


def test_final_sin_uses_its_own_model_importer_output() -> None:
    catalog = load_content_catalog()
    override = next(
        asset for asset in catalog.assets
        if asset.map_key == "e3m4_boss"
        and asset.strategy == "donor_model_override"
    )
    assert override.donor["kind"] == "codex"
    assert override.donor["selection"] == "named_entity"
    assert override.replacement_slot_policy == "safe_resident_static_lwo"
    assert override.replacement_slot["model_path"] == "art/pickups/codex.lwo"
    assert override.replacement_slot["resource_archive"] == "e3m4_boss"
    assert override.replacement_slot["material2"] == "art/pickups/codex"
    assert override.dependency_policy == "model_importer_bundle"
    assert override.replacement_slot["asset_id"] == "13626551837332538432"
    assert override.replacement_slot["import_bundle"] == (
        "codex_id#13626551837332538432"
    )
    provenance = override.replacement_slot["provenance"]
    assert provenance["source_obj"].endswith("ArchipelagoLogo_prepared.obj")
    assert provenance["source_obj_sha256"] == (
        "ab6a1f3d8fb833070889c80ef800a8ea9f55a1aa9dc905fb4599df32e5f9c659"
    )
    assert provenance["source_resource"].endswith("e3m4_boss.resources.backup")
    assert provenance["vanilla_lwo_sha256"] == (
        "206df1fb8e0c8b93fdded962647630f9db2e953bd49da3dc63ed03c256301b79"
    )
    assert override.usage_policy == "removed_vanilla_entity_allowlist"


def test_codex_importer_payload_is_coherent_for_both_registered_base_slots() -> None:
    catalog = load_content_catalog()
    overrides = {
        asset.map_key: asset
        for asset in catalog.assets
        if asset.strategy == "donor_model_override"
        and asset.model == "art/pickups/codex.lwo"
    }
    assert set(overrides) == {"e2m4_boss", "e3m4_boss"}

    stream_path = (
        ROOT / "packaging/mod_assets/streamdb/art/pickups"
        / "codex_id#13626551837332538432.lwo"
    )
    stream_bytes = stream_path.read_bytes()
    block_size = struct.unpack_from("<I", stream_bytes, 16)[0]
    assert stream_bytes.startswith(b"STREAMDB")
    assert len(stream_bytes) == block_size + 36

    resource_hashes = set()
    for map_key, override in overrides.items():
        slot = override.replacement_slot
        resource_path = (
            ROOT / "packaging/mod_assets" / map_key / "art/pickups/codex.lwo"
        )
        resource_bytes = resource_path.read_bytes()
        resource_hashes.add(_sha256(resource_path))
        assert _sha256(resource_path) == slot["resource_payload_sha256"]
        assert _sha256(stream_path) == slot["streamdb_payload_sha256"]
        assert resource_bytes.count(struct.pack("<I", block_size)) >= 4
        assert struct.pack("<I", block_size * 2) in resource_bytes
        assert resource_bytes.count(struct.pack("<I", block_size * 3)) >= 2

    assert resource_hashes == {
        "fa617a2f3d43a99510a512c6d7ea47fd3f7ed3a1ac32c4e01d36561f20478ac1"
    }


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
