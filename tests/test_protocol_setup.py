import asyncio
import logging
from types import SimpleNamespace

import pytest

from doom_eap.runtime.check_publication import CheckPublication
from doom_eap.runtime.location_setup import LocationSetup
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
