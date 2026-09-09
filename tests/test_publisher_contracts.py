"""Stable publisher document and source-selection contracts."""

import copy
import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from doom_eap.content.content_catalog import PublisherSpec
from doom_eap.content import publisher_loader as loader
from doom_eap.contracts import publisher_contracts as contracts


@pytest.fixture
def document():
    return {
        "schema_version": 1,
        "publishers": [{
            "key": "finish",
            "map_key": "intro",
            "triggers": [{
                "strategy": "native_transition",
                "from_map": " game\\sp\\hub\\hub/ ",
                "to_map": "game/sp/intro/",
            }],
            "effects": [{"strategy": "location_check", "location_id": 7770001}],
            "dedupe_scope": "room",
            "fallback_policy": "first_success_wins",
        }],
    }


def test_normalization_round_trip_and_model_identity(document):
    original = copy.deepcopy(document)
    publishers = contracts.publisher_contracts_from_document(document)
    assert PublisherSpec is contracts.PublisherContract
    assert document == original
    projected = contracts.publisher_contracts_document(publishers)
    expected = copy.deepcopy(document)
    expected["publishers"][0]["triggers"][0].update(
        from_map="game/hub/hub", to_map="game/sp/intro"
    )
    assert projected == expected
    assert contracts.publisher_contracts_from_document(projected) == publishers
    with pytest.raises(FrozenInstanceError):
        publishers[0].key = "changed"
    with pytest.raises(TypeError):
        publishers[0].triggers[0]["from_map"] = "changed"
    projected["publishers"][0]["effects"][0]["location_id"] = 0
    assert publishers[0].effects[0]["location_id"] == 7770001


def test_empty_and_invalid_documents(document):
    assert contracts.publisher_contracts_from_document(
        {"schema_version": 1, "publishers": []}, allow_empty=True
    ) == ()
    with pytest.raises(ValueError, match="non-empty publishers list"):
        contracts.publisher_contracts_from_document({"schema_version": 1, "publishers": []})
    with pytest.raises(ValueError, match="schema_version must be 1"):
        contracts.publisher_contracts_from_document(
            {"schema_version": 2, "publishers": []}, allow_empty=True
        )
    document["publishers"][0]["effects"][0]["location_id"] = True
    with pytest.raises(ValueError, match="requires integer location_id"):
        contracts.publisher_contracts_from_document(document)


def test_trigger_index_and_owner_order(document):
    second = copy.deepcopy(document["publishers"][0])
    second["key"] = "alpha"
    document["publishers"].append(second)
    for publisher in document["publishers"]:
        publisher["triggers"].append({"strategy": "terminal_owner", "owner": "exit"})
    publishers = contracts.publisher_contracts_from_document(document)
    assert [p.key for p in publishers] == ["finish", "alpha"]
    assert [p.key for p in contracts.publishers_for_transition(
        publishers, "game/sp/hub/hub", "game/sp/intro/"
    )] == ["alpha", "finish"]
    assert [p.key for p in contracts.map_publishers_for_owner(
        publishers, "intro", "exit"
    )] == ["alpha", "finish"]
    assert contracts.map_publishers_for_owner(publishers, "other", "exit") == ()
    with pytest.raises(TypeError):
        contracts.publishers_by_trigger(publishers)[("terminal_owner", "exit")] = ()


def test_explicit_and_default_source_selection(document, tmp_path):
    path = tmp_path / "publishers.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    expected = contracts.publisher_contracts_from_document(document)
    with patch.object(loader, "ROOT", tmp_path), patch.object(loader, "CONTRACT_PATH", path):
        assert loader.load_publisher_contracts() == expected
        (tmp_path / "content" / "maps").mkdir(parents=True)
        with patch.object(loader, "load_content_catalog") as load:
            load.return_value = SimpleNamespace(publishers=expected)
            assert loader.load_publisher_contracts(path) == expected
            load.assert_not_called()
            assert loader.load_publisher_contracts() is expected
            load.assert_called_once_with()
