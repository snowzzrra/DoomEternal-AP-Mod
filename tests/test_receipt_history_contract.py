"""Pre-extraction contracts for the live authoritative history writer."""

from collections import namedtuple
import copy

import pytest

from doom_eap.runtime import bridge_client as bridge


Receipt = namedtuple("Receipt", "item location player flags")


def context(receipts, boundary=0, history=None):
    ctx = object.__new__(bridge.DoomEternalContext)
    ctx.items_received = list(receipts)
    ctx.receipt_session = bridge.ReceiptSession(processed_boundary=boundary)
    ctx.session_state = {"receipt_history": {} if history is None else history}
    ctx.state_key = "room:0:1"
    commits = []
    ctx.persist_session_state = lambda: commits.append(copy.deepcopy(ctx.session_state))
    return ctx, commits


def test_progressive_and_fresh_counts_preserve_exclusions_and_caps(monkeypatch):
    ctx, _commits = context([
        Receipt(8, 1, 1, 0), Receipt(8, 1, 1, 0),
        Receipt(9, 2, 1, 0), Receipt(8, 3, 1, 0),
    ], boundary=1)
    monkeypatch.setattr(bridge, "ITEM_ID_TO_COMMAND", {
        8: {"type": "progressive_perk", "perks": ["first", "second"]},
    })
    assert ctx.progressive_stage(8, 3, excluded_receipt_indices={1}) == 1
    assert ctx.progressive_stage(8, 3, excluded_receipt_indices=set()) == 1
    assert ctx.progressive_stage(8, True, excluded_receipt_indices=set()) == 0
    assert ctx._fresh_receipt_owned_count(8, 3, excluded_receipt_indices={1}) == 1
    assert ctx._fresh_receipt_owned_count(8, 3, excluded_receipt_indices=set()) == 2
    assert ctx._fresh_receipt_owned_count(8, 2) is None
    assert ctx._fresh_receipt_owned_count(8, 0) is None
    monkeypatch.setattr(bridge, "AMMO_REFILL_ITEM_ID", 8)
    from types import SimpleNamespace
    counts = []
    ctx.ammo = SimpleNamespace(observe_receipts=counts.append)
    ctx._observe_ammo_receipts()
    assert counts == [3]
    assert ctx.received_item_ids(processed_only=True) == frozenset({8})
    assert ctx.received_item_ids() == frozenset({8, 9})
    assert isinstance(ctx.received_item_ids(), frozenset)


def test_writer_retains_identical_ordered_occurrences():
    receipt = Receipt(77, -2, 1, 0)
    ctx, _commits = context([receipt, receipt])
    ctx._record_processed_receipt(receipt)
    ctx._record_processed_receipt(receipt)
    identity = bridge.receipt_identity(receipt)
    history = ctx.session_state["receipt_history"]
    assert history["receipt_item_ids"] == [77, 77]
    assert history["receipt_ids"] == [identity, identity]
    assert history["receipt_counts"] == {identity: 2}
    assert ctx.items_processed == 0  # Recording and advancement currently have separate call sites.


def test_observation_separates_visible_ownership_from_processed_prefix():
    first, tail = Receipt(10, 100, 1, 0), Receipt(11, 101, 1, 0)
    ctx, commits = context([first, tail], 1, {"receipt_item_ids": [10]})
    observation = ctx.observe_received_item_history()
    history = ctx.session_state["receipt_history"]
    assert observation.historical_authoritative_item_ids == (10,)
    assert observation.authoritative_item_ids == (10, 11)
    assert history["owned_item_ids"] == [10, 11]
    assert history["receipt_item_ids"] == [10]
    assert history["processed_boundary"] == 1
    assert history["highest_observed_index"] == 1
    assert len(commits) == 1
    assert ctx.observe_received_item_history() == observation
    assert len(commits) == 1


def test_observation_preserves_overlap_counts_not_just_current_prefix():
    first, overlap = Receipt(10, 100, 1, 0), Receipt(11, 101, 1, 0)
    counts = {bridge.receipt_identity(first): 1, bridge.receipt_identity(overlap): 1}
    ctx, _commits = context([first, overlap], 1, {"receipt_item_ids": [10], "receipt_counts": counts.copy()})
    observation = ctx.observe_received_item_history()
    assert [entry.index for entry in observation.duplicates] == [1]
    assert observation.new == ()
    assert ctx.session_state["receipt_history"]["receipt_counts"] == counts
    assert ctx.session_state["receipt_history"]["receipt_ids"] == [bridge.receipt_identity(first)]


def test_incompatible_prefix_is_not_rewritten_or_committed():
    history = {"receipt_item_ids": [10], "custom": {"preserve": True}}
    ctx, commits = context([Receipt(11, 101, 1, 0)], 1, copy.deepcopy(history))
    with pytest.raises(ValueError, match="durable receipt item prefix differs"):
        ctx.observe_received_item_history()
    assert ctx.session_state["receipt_history"] == history
    assert commits == []


def test_count_view_filters_invalid_entries_without_rewriting_valid_dictionary():
    counts = {"valid": 2, "negative": -1, "boolean": True, "text": "2", "": 1}
    ctx, _commits = context([], history={"receipt_counts": counts.copy()})
    assert ctx._processed_receipt_ids() == {"valid": 2}
    assert ctx.session_state["receipt_history"]["receipt_counts"] == counts
