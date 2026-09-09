import asyncio
import logging
from types import SimpleNamespace

import pytest

from doom_eap.runtime.check_publication import CheckPublication
from doom_eap.runtime.location_setup import LocationSetup
from doom_eap.runtime.location_names import resolve_placement_records
from doom_eap.runtime.protocol_feed import ProtocolFeed, hints_key


def test_placement_collection_waits_for_exact_history_and_preserves_scout_packet():
    setup = LocationSetup(logging.getLogger("setup-contract"))
    packets = []

    async def send(value):
        packets.extend(value)

    assert setup.begin({"missing_locations": [20], "checked_locations": [10]}, {10, 20}) == {10, 20}
    assert asyncio.run(setup.scout(CheckPublication(send, None, None))).current
    assert packets == [{"cmd": "LocationScouts", "locations": [10, 20], "create_as_hint": 0}]
    assert setup.consume({"locations": [(1, 20, 2, 0)]}, {20}) is None
    assert not setup.ready_to_resolve
    assert setup.consume({"locations": [(2, 10, 1, 1)]}, {10, 20}) is None
    assert setup.ready_to_resolve and not setup.complete
    assert setup.consume({"locations": [(2, 10, 1, 1)]}, {10, 20}) is None
    setup.resolved()
    assert setup.complete and not setup.ready_to_resolve
    assert setup.received_ids == frozenset({10, 20})


@pytest.mark.parametrize("missing,checked,error", [
    (None, [], "must be a location list"), ([True], [], "invalid location ID"),
    ([1], [1], "overlap"), ([99], [], "unknown DOOM location IDs"),
])
def test_connected_rejection_retains_existing_contract(missing, checked, error):
    setup = LocationSetup(logging.getLogger("setup-contract"))
    with pytest.raises(ValueError, match=error):
        setup.begin({"missing_locations": missing, "checked_locations": checked}, {1})


@pytest.mark.parametrize("rows,materialized,error", [
    (None, {1}, "not a list"), ([(2, 1, 1)], {1}, "malformed placement"),
    ([(2, True, 1, 0)], {1}, "invalid location ID"),
    ([(2, 9, 1, 0)], {9}, "unknown location ID"),
    ([(2, 1, 1, 0)], set(), "did not materialize"),
    ([(2, 1, 1, 0), (2, 1, 1, 0)], {1}, "duplicate location IDs"),
])
def test_location_info_failure_blocks_resolution(rows, materialized, error):
    setup = LocationSetup(logging.getLogger("setup-contract"))
    setup.begin({"missing_locations": [1], "checked_locations": []}, {1})
    reason = setup.consume({"locations": rows}, materialized)
    assert error in reason
    assert setup.fail(reason)
    assert not setup.fail(reason)
    assert not setup.ready_to_resolve


@pytest.mark.parametrize("failure", [False, True])
def test_old_scout_cannot_complete_or_fail_new_connection(failure):
    async def run():
        setup = LocationSetup(logging.getLogger("setup-contract"))
        setup.begin({"missing_locations": [1], "checked_locations": []}, {1})

        async def send(_):
            setup.begin({"missing_locations": [2], "checked_locations": []}, {2})
            setup.consume({"locations": [(4, 2, 1, 0)]}, {2})
            if failure:
                raise OSError("old connection")

        result = await setup.scout(CheckPublication(send, None, None))
        assert not result.current and result.error is None
        assert setup.ready_to_resolve and setup.received_ids == {2}

    asyncio.run(run())


def test_placement_names_preserve_sorted_order_and_unknown_classification_rejection():
    names = SimpleNamespace(
        item_names=SimpleNamespace(lookup_in_slot=lambda item, slot: f"item-{item}"),
        location_names=SimpleNamespace(lookup_in_slot=lambda loc, slot: f"location-{loc}"),
        player_names={1: "local", 2: "remote"},
    )
    network = {2: SimpleNamespace(item=20, player=2, flags=4), 1: SimpleNamespace(item=10, player=1, flags=0)}
    rows = resolve_placement_records({2, 1}, network, {1: object(), 2: object()}, names, 1)
    assert [row["location_id"] for row in rows] == [1, 2]
    assert rows[0]["local"] and rows[1]["trap"] and rows[1]["recipient_name"] == "remote"
    network[1].flags = True
    with pytest.raises(ValueError, match="invalid item classification"):
        resolve_placement_records({1}, network, {1: object()}, names, 1)


def test_chat_echo_is_bounded_local_once_and_cannot_cross_connection(monkeypatch):
    from doom_eap.runtime import protocol_feed
    now = [10.0]
    monkeypatch.setattr(protocol_feed, "time", SimpleNamespace(monotonic=lambda: now[0]))
    feed = ProtocolFeed()
    event = {"plain": "local: hello", "segments": [{"type": "player", "self": True}]}
    feed.record_echo("hello")
    assert not feed.consume_echo({**event, "segments": [{"type": "player", "self": False}]})
    assert feed.consume_echo(event) and not feed.consume_echo(event)
    feed.record_echo("hello")
    now[0] = 15.0
    assert feed.consume_echo(event)
    feed.record_echo("hello")
    now[0] = 20.01
    assert not feed.consume_echo(event)
    feed.record_echo("hello")
    feed.invalidate()
    assert not feed.consume_echo(event)
    assert hints_key(1, 2) == "_read_hints_1_2"
    assert hints_key(True, 2) is None
