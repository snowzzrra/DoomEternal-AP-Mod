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
    effective_ownership,
    migrate_client_state,
    migrate_legacy_session_key,
    normalize_session_state,
    observe_received_items,
    receipt_history_fingerprint,
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


def test_byte_identical_receipts_at_distinct_indices_are_both_new():
    first = Receipt(77, -2, 1, 0)
    second = Receipt(77, -2, 1, 0)

    observed = observe_received_items([first, second], 0)

    assert [receipt.index for receipt in observed.new] == [0, 1]
    assert observed.duplicates == ()
    assert observed.authoritative_item_ids == (77, 77)


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
    with patch("doom_eap.runtime.item_reconciliation.compile_item_delivery_plan", _fake_delivery):
        plan = compile_reconciliation_plan(
            [1, 1], definitions, _registry((1, NEVER_REPLAY)), "room-0-1", 4
        )

    assert plan.commands == ()
    assert plan.skipped_never_replay == 1


def test_replay_safe_plan_is_one_silent_reconciliation_command():
    definitions = {1: "simple"}
    with patch("doom_eap.runtime.item_reconciliation.compile_item_delivery_plan", _fake_delivery):
        plan = compile_reconciliation_plan(
            [1], definitions, _registry((1, REPLAY_IDEMPOTENT)), "room-0-1", 4
        )

    assert len(plan.commands) == 1
    assert plan.commands[0].spool_id.startswith("reconcile-")
    assert "notify" not in plan.commands[0].command


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


def test_effective_ownership_keeps_ap_receipts_and_legacy_fingerprint_separate():
    receipts = [Receipt(7770014, 100, 1, 0), Receipt(77, -2, 1, 0), Receipt(77, -2, 1, 0)]
    ownership = effective_ownership(
        receipts, randomize_chainsaw=False, randomize_dash=False,
        checked_locations=frozenset({7770162, 55}), local_checked_locations=frozenset({"7770002"}),
        server_checked_ready=True, hell_on_earth_locations=frozenset({7770002}),
        exultia_complete_location=55, slot=1,
    )
    assert ownership.ap_item_ids == (7770014, 77, 77)
    assert ownership.reconciliation_item_ids == (7770014, 77, 77, 7770010)
    assert [fact.item_id for fact in ownership.derived_facts] == [7770010, 7770015]
    assert ownership.vanilla_dash
    assert [upgrade.location_id for upgrade in ownership.blood_punch_upgrades] == [7770162]
    # This is the pre-P-1 cache-key oracle, not a receipt emitted by the new domain.
    legacy_key = receipt_history_fingerprint([
        *receipts, SimpleNamespace(item=7770010, location=0, player=1),
    ])
    assert ownership.materialization_fingerprint == legacy_key
    assert len(receipts) == 3
    receipts.clear()
    assert ownership.ap_item_ids == (7770014, 77, 77)
    with pytest.raises(FrozenInstanceError):
        ownership.vanilla_dash = False


@pytest.mark.parametrize("randomized,ap_chainsaw", [(False, False), (True, False), (False, True), (True, True)])
def test_effective_ownership_does_not_promote_local_checks_to_dash_or_blood_punch(randomized, ap_chainsaw):
    receipts = [Receipt(7770010, 100, 1, 0)] if ap_chainsaw else []
    ownership = effective_ownership(
        receipts, randomize_chainsaw=randomized, randomize_dash=False,
        checked_locations=frozenset(), local_checked_locations=frozenset({7770002, 55, 7770162}),
        server_checked_ready=True, hell_on_earth_locations=frozenset({7770002}),
        exultia_complete_location=55, slot=1,
    )
    assert not ownership.vanilla_dash
    assert ownership.blood_punch_upgrades == ()
    derived_chainsaw = not randomized and not ap_chainsaw
    assert len(ownership.derived_facts) == int(derived_chainsaw)
    if not derived_chainsaw:
        assert ownership.materialization_fingerprint == receipt_history_fingerprint(receipts)


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


def test_starting_materialization_preserves_existing_quantity_and_eligibility_rules():
    session = ReceiptSession()
    session.configure_starting_materialization(
        starting_inventory={"A": True, "B": -1, "C": "2", "Excluded": 4, "Unknown": 1},
        starting_weapon="Excluded",
        item_identity={1: {"name": "A"}, 2: {"name": "B"}, 3: {"name": "C"}, 4: {"name": "Excluded"}},
        eligible=lambda item: item != 4, processed_receipts=(),
    )
    assert session.consume_starting_materialization(1)  # bool was accepted as int by the original parser.
    assert not session.consume_starting_materialization(1)
    assert not any(session.consume_starting_materialization(item) for item in (2, 3, 4))


def test_receipt_packet_ranges_are_bounded_and_pruned_without_rebind_reset():
    session = ReceiptSession()
    for index in range(257):
        assert session.observe_packet(index, 1, index + 1, 100 + index) == (index, True)
    assert session.packet_timestamp(0) is None
    assert session.packet_timestamp(1) == 101
    assert session.observe_packet(True, 1, 300, 999) == (299, False)
    assert session.packet_timestamp(256) == 356
    session.restore_boundary(128)
    session.prune_processed_packets()
    assert session.packet_timestamp(127) is None
    assert session.packet_timestamp(128) == 228
    session.begin_rebind()
    assert session.packet_timestamp(128) == 228
    session.clear_packet_ranges()
    assert session.packet_timestamp(128) is None
