"""Canonical contextual-name mapping contracts."""

from __future__ import annotations

import json
from pathlib import Path

from content_catalog import load_content_catalog


ROOT = Path(__file__).resolve().parents[1]


def test_contextual_mapping_is_exact_and_complete() -> None:
    mapping = json.loads(
        (ROOT / "data" / "contextual_location_names.json").read_text(
            encoding="utf-8"
        )
    )
    names = json.loads(
        (ROOT / "data" / "location_names.json").read_text(encoding="utf-8")
    )["locations"]
    entries = mapping["locations"]

    assert mapping["schema_version"] == 1
    assert len(entries) == 166
    ids = [entry["location_id"] for entry in entries]
    final_names = [entry["contextual_name"] for entry in entries]
    assert len(ids) == len(set(ids))
    assert len(final_names) == len(set(final_names))
    assert all(entry["current_name"] != entry["contextual_name"] for entry in entries)
    assert all(names[str(entry["location_id"])] == entry["contextual_name"] for entry in entries)

    catalog = load_content_catalog()
    physical_ids = [item.location_id for item in catalog.physical_locations]
    assert len(physical_ids) == len(set(physical_ids))
    assert all(location_id in physical_ids for location_id in ids)
    assert len({item.name for item in catalog.physical_locations}) == len(
        catalog.physical_locations
    )


def test_contextual_mapping_does_not_rewrite_unmapped_locations() -> None:
    mapping = json.loads(
        (ROOT / "data" / "contextual_location_names.json").read_text(
            encoding="utf-8"
        )
    )["locations"]
    names = json.loads(
        (ROOT / "data" / "location_names.json").read_text(encoding="utf-8")
    )["locations"]
    mapped = {str(entry["location_id"]) for entry in mapping}
    expected_unchanged = {
        "7770001": "Hell on Earth - Chainsaw",
        "7770122": "Hell on Earth - Mission Complete",
        "7770414": "Final Sin - Mission Complete",
    }
    assert not (set(expected_unchanged) & mapped)
    assert {key: names[key] for key in expected_unchanged} == expected_unchanged
