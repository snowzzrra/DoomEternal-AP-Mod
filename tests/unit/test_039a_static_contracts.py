"""Focused static contracts for the non-runtime 0.3.9a pass."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from content_catalog import load_content_catalog
from tools.decls.rune_slot_builder import (
    PATCHED_SLOT_REQUIREMENTS,
    RUNE_SLOT_OWNER,
    SOURCE_SLOT_REQUIREMENTS,
    build_rune_slot_override,
    load_rune_slot_source,
)
from tools.maps.ap_map_generator import (
    extract_target_names,
    find_entity_block_bounds,
    remove_inline_currency_transaction,
)


ROOT = Path(__file__).resolve().parents[2]
CANONICAL_MODEL = "art/pickups/codex.lwo"
QUESTION_MODEL = "art/pickups/question_mark_a.lwo"
EXPECTED_TARGETS = [
    "target_timeline_restore_power",
    "target_relay_power_on",
    "target_relay_engine_light_on",
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entity(content: str, name: str) -> str:
    bounds = find_entity_block_bounds(content, name)
    assert bounds is not None
    return content[bounds[0]:bounds[1]]


def test_every_enabled_map_has_the_same_canonical_ap_visual_bundle() -> None:
    catalog = load_content_catalog()
    enabled = {spec.key: spec for spec in catalog.enabled_maps()}
    visual_assets = {
        asset.map_key: asset
        for asset in catalog.assets
        if asset.key == "archipelago_world_visual"
    }
    assert set(visual_assets) == set(enabled)
    assert len(visual_assets) == len(enabled) == 14

    expected_hashes = {
        CANONICAL_MODEL:
            "fa617a2f3d43a99510a512c6d7ea47fd3f7ed3a1ac32c4e01d36561f20478ac1",
        "art/pickups/codex.tga$streamed$mtlkind=albedo":
            "6716b617263a8b16ab9af28edd69dd976aaf42ec237f81e6bd8763c746564f1a",
        "generated/decls/material2/art/pickups/codex.decl":
            "f2f33d9d52bae5502d621882e67a8fc7254a85c70909011fcd094147f7eef3f7",
    }
    for map_key, asset in visual_assets.items():
        assert asset.strategy == "packaged_bundle"
        assert asset.model == CANONICAL_MODEL
        assert asset.resource_base == enabled[map_key].resource_base
        assert asset.replacement_slot["streamdb_payload"] == (
            "art/pickups/codex_id#13626551837332538432.lwo"
        )
        for relative, expected_hash in expected_hashes.items():
            assert _sha256(ROOT / "packaging/mod_assets" / asset.resource_base / relative) == expected_hash

    streamdb = (
        ROOT / "packaging/mod_assets/streamdb/art/pickups/"
        "codex_id#13626551837332538432.lwo"
    )
    assert _sha256(streamdb) == (
        "ef49160da53774ff7c952ba5f6d939e292f16d16f084558c899821d567459630"
    )
    assert not list((ROOT / "packaging/mod_assets").rglob("question_mark_a.lwo"))
    assert not list((ROOT / "packaging/mod_assets").rglob("question.decl"))
    assert not list((ROOT / "packaging/mod_assets").rglob("question.tga*"))


def test_mandatory_battery_socket_removes_only_its_inline_transaction() -> None:
    config = json.loads((ROOT / "level_configs/hub.json").read_text(encoding="utf-8"))
    [contract] = config["inline_currency_removals"]
    source = (ROOT / "vanillamaps/hub.map").read_text(encoding="utf-8")
    before = _entity(source, contract["entity"])
    updated = remove_inline_currency_transaction(source, contract)
    after = _entity(updated, contract["entity"])

    assert 'class = "idInteractable_GiveItems";' in after
    assert extract_target_names(before) == extract_target_names(after) == EXPECTED_TARGETS
    assert 'currencyList = {' in before
    assert 'currencyType = "CURRENCY_SENTINEL_BATTERY";' in before
    assert "count = -1;" in before
    assert "currencyList" not in after
    assert "CURRENCY_SENTINEL_BATTERY" not in after
    assert updated.count("currencyList = {") == source.count("currencyList = {") - 1

    gate = contract["preserved_requirement"]
    assert gate == {
        "owner": "game/hub/hub.resources",
        "path": (
            "generated/decls/logicentity/maps/game/hub/"
            "hub_game_sentinel_battery_room/sentinel_battery_room_info_logic_1.decl"
        ),
        "variable": "sentinel_battery_available",
        "reader": "idLogicNodeModelPlayerInventoryCheck",
        "currency": "CURRENCY_SENTINEL_BATTERY",
        "minimum_count": 1,
    }


def test_rune_slot_override_changes_only_the_three_threshold_values(tmp_path: Path) -> None:
    audit = build_rune_slot_override(tmp_path)
    source_bytes = load_rune_slot_source()
    assert hashlib.sha256(source_bytes).hexdigest() == RUNE_SLOT_OWNER["sha256"]
    assert b"\r\n" in source_bytes
    source = source_bytes.decode("utf-8").replace("\r\n", "\n")
    target = Path(audit["written_path"])
    patched_bytes = target.read_bytes()
    patched = patched_bytes.decode("utf-8").replace("\r\n", "\n")

    assert SOURCE_SLOT_REQUIREMENTS in source
    assert SOURCE_SLOT_REQUIREMENTS not in patched
    assert PATCHED_SLOT_REQUIREMENTS in patched
    assert patched_bytes == source_bytes.replace(
        SOURCE_SLOT_REQUIREMENTS.replace("\n", "\r\n").encode(),
        PATCHED_SLOT_REQUIREMENTS.replace("\n", "\r\n").encode(), 1
    )
    assert _sha256(target) == "a42074ac147f3cd9924b6e6ab062a4654c42c1601bda9483c5f89ee9a3ce4352"
    assert patched.count("runeSlotReq") == 1
    assert re.findall(r"ptr\[(\d+)\] = (\d+);", PATCHED_SLOT_REQUIREMENTS) == [
        ("0", "0"), ("1", "0"), ("2", "0")
    ]
    assert all(
        token not in PATCHED_SLOT_REQUIREMENTS
        for token in ("STAT_", "perk/", "earned", "acquired", "equipped")
    )
    unchanged_observers = {
        "data/items.json":
            "aaa52687bab8ba4da608feee9c76a7b262351fbdf0473367195df032d58dfc97",
        "challenge_registry.py":
            "1fca98c232d172094812e83a7c8c3f6570cdcc964189b30cce3d263643ff0ca4",
        "observer_lifecycle.py":
            "ec45ed8c59c25eb6d59d1fcada18ddac09764f99b3ed3bf101357c53605fb606",
        "tools/decls/mastery_decl_builder.py":
            "ce6c583b109d8b01a9d249ddd1f032c5ab59006b5363b38f657de7e8d8a8e728",
        "tools/decls/mission_challenge_decl_builder.py":
            "9a5955393fe8f92f7bcb7b63c4663275b3a910fc02bccca3da6b67070f494bca",
    }
    assert {
        relative: _sha256(ROOT / relative)
        for relative in unchanged_observers
    } == unchanged_observers
