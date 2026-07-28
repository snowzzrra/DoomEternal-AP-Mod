"""Fast APWorld-data checks without importing Archipelago's world loader."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT.parent / "Archipelago" / "worlds" / "doometernal" / "generated_content.py"


@pytest.fixture(scope="session")
def generated_module():
    spec = importlib.util.spec_from_file_location("doometernal_generated_content", GENERATED)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_location_ids_and_regions_are_consistent(generated_module) -> None:
    rows = generated_module.LOCATION_ROWS
    ids = [code for _, code, _ in rows if code is not None]
    assert len(ids) == len(set(ids))
    assert all(region in generated_module.CAMPAIGN_REGIONS or region not in {"Menu"} for _, _, region in rows)


def test_generated_route_uses_declared_regions(generated_module) -> None:
    known = set(generated_module.CAMPAIGN_REGIONS)
    assert all(source in known and destination in known for source, destination, _ in generated_module.CAMPAIGN_CONNECTIONS)


def test_generated_identity_matches_mod_source(generated_module) -> None:
    import json

    identity = json.loads((ROOT / "data" / "content_identity.json").read_text(encoding="utf-8"))
    assert generated_module.CONTENT_SCHEMA_VERSION == identity["content_schema_version"]
    assert generated_module.CONTENT_REVISION == identity["content_revision"]
    assert generated_module.BRIDGE_PROTOCOL_VERSION == identity["bridge_protocol_version"]
