from collections import namedtuple
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from item_reconciliation import (
    NEVER_REPLAY,
    REPLAY_IDEMPOTENT,
    SPECIAL_PROGRESSIVE,
    ReplayPolicy,
    compile_reconciliation_plan,
    migrate_client_state,
    migrate_legacy_session_key,
    normalize_session_state,
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


def test_old_history_is_historical_and_has_no_new_receipts():
    history = [Receipt(item, index, 1, 0) for index, item in enumerate((10, 11, 12))]

    observed = observe_received_items(history, processed_boundary=3)

    assert [receipt.item_id for receipt in observed.historical] == [10, 11, 12]
    assert observed.new == ()
    assert observed.highest_observed_index == 2
    assert observed.processed_boundary == 3


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

    with patch("item_reconciliation.compile_item_delivery_plan", _fake_delivery):
        plan = compile_reconciliation_plan(
            observed.historical_authoritative_item_ids,
            definitions,
            registry,
            "room-0-1",
            4,
        )

    assert [selection.item_id for selection in plan.selections] == list(range(10))
    assert all(command.item_id < 10 for command in plan.commands)


def test_duplicate_packet_does_not_create_second_new_receipt():
    first = Receipt(77, 100, 1, 0)
    duplicate = Receipt(77, 100, 1, 0)

    observed = observe_received_items([Receipt(1, 1, 1, 0), first, duplicate], 1)

    assert [receipt.index for receipt in observed.new] == [1]
    assert [receipt.index for receipt in observed.duplicates] == [2]
    assert observed.authoritative_item_ids == (1, 77)


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


def test_legacy_session_migration_is_identity_bound_before_normalization():
    sessions = {
        "None:0:1": {"processed_items": 7},
        "None:0:2": {"processed_items": 11},
    }

    state_key, migrated_from = migrate_legacy_session_key(
        sessions,
        seed_name="room-a",
        team=0,
        slot=1,
    )

    assert state_key == "room-a:0:1"
    assert state_key is not None
    assert migrated_from == "None:0:1"
    assert sessions[state_key]["processed_items"] == 7
    assert "None:0:1" not in sessions
    assert "None:0:2" in sessions

    unchanged = dict(sessions)
    other_key, other_migration = migrate_legacy_session_key(
        sessions,
        seed_name="room-a",
        team=1,
        slot=1,
    )
    assert other_key == "room-a:1:1"
    assert other_migration is None
    assert sessions == unchanged

    malformed = {"None:0:3": "malformed"}
    malformed_key, malformed_from = migrate_legacy_session_key(
        malformed,
        seed_name="room-a",
        team=0,
        slot=3,
    )
    assert malformed_from == "None:0:3"
    assert malformed_key is not None
    assert normalize_session_state(cast(Any, malformed[malformed_key]))["processed_items"] == 0


def test_never_replay_has_zero_commands():
    definitions = {1: "simple"}
    with patch("item_reconciliation.compile_item_delivery_plan", _fake_delivery):
        plan = compile_reconciliation_plan(
            [1, 1], definitions, _registry((1, NEVER_REPLAY)), "room-0-1", 4
        )

    assert plan.commands == ()
    assert plan.skipped_never_replay == 1


def test_replay_safe_plan_is_one_silent_reconciliation_command():
    definitions = {1: "simple"}
    with patch("item_reconciliation.compile_item_delivery_plan", _fake_delivery):
        plan = compile_reconciliation_plan(
            [1], definitions, _registry((1, REPLAY_IDEMPOTENT)), "room-0-1", 4
        )

    assert len(plan.commands) == 1
    assert plan.commands[0].spool_id.startswith("reconcile-")
    assert "notify" not in plan.commands[0].command


def test_progressive_plan_clamps_to_configured_stages():
    definitions = {2: {"type": "progressive_perk", "perks": ["one", "two"]}}
    with patch("item_reconciliation.compile_item_delivery_plan", _fake_delivery):
        plan = compile_reconciliation_plan(
            [2] * 5,
            definitions,
            _registry((2, SPECIAL_PROGRESSIVE)),
            "room-0-1",
            4,
        )

    assert plan.selections[0].received_count == 5
    assert [command.stage for command in plan.commands] == [0, 1]
    assert plan.special_stages == 2


def test_manual_and_automatic_plans_share_deterministic_semantics():
    definitions = {1: "simple", 2: {"type": "progressive_perk", "perks": ["one"]}}
    registry = _registry((1, REPLAY_IDEMPOTENT), (2, SPECIAL_PROGRESSIVE))
    with patch("item_reconciliation.compile_item_delivery_plan", _fake_delivery):
        manual = compile_reconciliation_plan([1, 2, 2], definitions, registry, "room-0-1", 4)
        automatic = compile_reconciliation_plan([1, 2, 2], definitions, registry, "room-0-1", 4)

    assert manual == automatic


def test_identity_state_is_separate_per_room():
    state, _ = migrate_client_state(
        {
            "version": 1,
            "sessions": {
                "room-a:0:1": {"processed_items": 3},
                "room-b:0:1": {"processed_items": 9},
            },
        }
    )

    assert state["sessions"]["room-a:0:1"]["processed_items"] == 3
    assert state["sessions"]["room-b:0:1"]["processed_items"] == 9
