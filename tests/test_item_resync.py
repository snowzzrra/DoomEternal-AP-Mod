from collections import namedtuple
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from doom_eap.runtime.item_reconciliation import (
    NEVER_REPLAY,
    REPLAY_IDEMPOTENT,
    ReplayPolicy,
    ReceiptSession,
    compile_reconciliation_plan,
    migrate_client_state,
    migrate_legacy_session_key,
    observe_received_items,
)

Receipt = namedtuple("Receipt", "item location player flags")


def _fake_delivery(item_id, definitions, *, stage=None, **_kwargs):
    selected_stage = 0 if stage is None else stage
    command = SimpleNamespace(
        index=selected_stage,
        command=f"activate-{item_id}-{selected_stage}",
    )
    return SimpleNamespace(
        commands=(command,),
        description=f"item {item_id} stage {selected_stage}",
    )


def _registry(*entries):
    return {
        item_id: ReplayPolicy(item_id, f"Item {item_id}", policy)
        for item_id, policy in entries
    }




def test_reconnect_classifies_new_receipts_once_in_order():
    history = [Receipt(item, index, 1, 0) for index, item in enumerate(range(13))]
    first = observe_received_items(history, 10)
    processed_ids = [
        receipt.receipt_id for receipt in first.historical if receipt.receipt_id is not None
    ]

    assert [receipt.item_id for receipt in first.new] == [10, 11, 12]

    processed_ids.extend(
        receipt.receipt_id for receipt in first.new if receipt.receipt_id is not None
    )
    second = observe_received_items(history, 10, processed_ids)
    assert second.new == ()
    assert [receipt.index for receipt in second.duplicates] == [10, 11, 12]


def test_reconciliation_history_stops_at_processed_boundary():
    history = [Receipt(item, index, 1, 0) for index, item in enumerate(range(13))]
    observed = observe_received_items(history, processed_boundary=10)
    definitions = {item: "simple" for item in range(13)}
    registry = _registry(*[(item_id, REPLAY_IDEMPOTENT) for item_id in range(13)])

    with patch("doom_eap.runtime.item_reconciliation.compile_item_delivery_plan", _fake_delivery):
        plan = compile_reconciliation_plan(
            observed.historical_authoritative_item_ids,
            definitions,
            registry,
            "room-0-1",
            4,
        )

    assert [selection.item_id for selection in plan.selections] == list(range(10))
    assert all(command.item_id < 10 for command in plan.commands)




def test_shifted_history_overlap_uses_occurrence_count_not_item_id():
    old_first = Receipt(70, 100, 1, 0)
    old_second = Receipt(71, 101, 1, 0)
    inserted = Receipt(69, -2, 1, 0)
    tail = Receipt(72, 102, 1, 0)
    processed_counts = {
        observe_received_items([old_first], 0).new[0].receipt_id: 1,
        observe_received_items([old_second], 0).new[0].receipt_id: 1,
    }

    observed = observe_received_items(
        [inserted, old_first, old_second, tail],
        2,
        cast(Any, processed_counts),
    )

    assert [receipt.index for receipt in observed.duplicates] == [2]
    assert [receipt.index for receipt in observed.new] == [3]


def test_state_migration_preserves_history_and_safes_malformed_sessions():
    state, migrated = migrate_client_state(
        {
            "version": 1,
            "sessions": {
                "room-a:0:1": {
                    "processed_items": 7,
                    "never_replay_history": [7001],
                    "custom": {"keep": True},
                },
                "room-b:0:1": "malformed",
            },
        }
    )

    assert migrated is True
    assert state["version"] == 2
    assert state["sessions"]["room-a:0:1"]["processed_items"] == 7
    assert state["sessions"]["room-a:0:1"]["never_replay_history"] == [7001]
    assert state["sessions"]["room-a:0:1"]["custom"] == {"keep": True}
    assert state["sessions"]["room-b:0:1"]["processed_items"] == 0
    assert set(state["sessions"]) == {"room-a:0:1", "room-b:0:1"}




def test_generation_identity_never_adopts_another_campaign_session():
    sessions = {
        "room-a:0:1": {
            "processed_items": 31,
            "campaign_navigation": {"generation": "b" * 64},
        },
    }

    state_key, migrated_from = migrate_legacy_session_key(
        sessions, seed_name="room-a", team=0, slot=1, generation="a" * 64,
    )

    assert state_key == "room-a:0:1:" + "a" * 64
    assert migrated_from is None
    assert sessions["room-a:0:1"]["processed_items"] == 31
    assert state_key not in sessions


def test_never_replay_has_zero_commands():
    definitions = {1: "simple"}
    with patch("doom_eap.runtime.item_reconciliation.compile_item_delivery_plan", _fake_delivery):
        plan = compile_reconciliation_plan(
            [1, 1], definitions, _registry((1, NEVER_REPLAY)), "room-0-1", 4
        )

    assert plan.commands == ()
    assert plan.skipped_never_replay == 1










def test_receipt_session_rebind_invalidates_tokens_and_pending_observations():
    session = ReceiptSession()
    key = ("room", 0, "receipt")
    token = session.capture("room")
    session.note_observation(key)
    assert session.was_observed(key)
    assert session.is_current(token, "room")
    assert not session.is_current(token, "other-room")
    session.advance()
    assert session.processed_boundary == 1
    session.begin_rebind()
    assert session.processed_boundary == 1
    assert not session.was_observed(key)
    assert not session.is_current(token, "room")
    assert session.is_current(session.capture("room"), "room")
    session.restore_boundary(7)
    assert session.processed_boundary == 7
    with pytest.raises(FrozenInstanceError):
        token.generation = 10


def test_starting_materialization_tracks_sources_and_subtracts_processed_occurrences():
    session = ReceiptSession()
    receipt = Receipt(8, -2, 1, 0)
    inputs = dict(
        starting_inventory={"Item": 2}, starting_weapon="Item",
        item_identity={8: {"name": "Item"}}, eligible=lambda _item: True,
    )
    session.configure_starting_materialization(**inputs, processed_receipts=[receipt, receipt])
    assert [(fact.item_id, fact.quantity, fact.provenance) for fact in session.starting_materialization] == [
        (8, 2, "starting_inventory"), (8, 1, "starting_weapon"),
    ]
    assert session.consume_starting_materialization(8)
    assert not session.consume_starting_materialization(8)
    session.begin_rebind()
    assert not session.consume_starting_materialization(8)
    session.configure_starting_materialization(**inputs, processed_receipts=[receipt] * 3)
    assert not session.consume_starting_materialization(8)
    with pytest.raises(FrozenInstanceError):
        session.starting_materialization[0].quantity = 9
