import asyncio
import logging
from dataclasses import replace

import pytest

from doom_eap.contracts.check_observation import CheckObservation
from doom_eap.runtime.check_publication import CheckPublication
from doom_eap.runtime.physical_checks import PhysicalChecks
from test_cross_campaign_save_authority import _create_test_context, _active_bridge_client


def test_physical_policy_distinguishes_confirmed_submitted_and_unknown_slot():
    facts = CheckObservation(frozenset({1}), frozenset({2}), frozenset({1, 2, 3}), False, True)
    assert PhysicalChecks.disposition(1, facts, False) == "acknowledged"
    assert PhysicalChecks.disposition(2, facts, True) == "submitted"
    assert PhysicalChecks.disposition(3, facts, True) == "submit"
    assert PhysicalChecks.disposition(4, facts, True) == "quarantine"
    assert PhysicalChecks.disposition(4, facts, False) == "outside_slot"
    assert PhysicalChecks.disposition(4, replace(facts, server_locations=frozenset()), True) == "outside_slot"


@pytest.mark.parametrize("late,failure", [(False, False), (False, True), (True, False), (True, True)])
def test_physical_batch_submission_cannot_cross_connection(late, failure):
    owner = PhysicalChecks(logging.getLogger("physical-checks"))
    packets, submitted = [], []

    async def send(value):
        packets.extend(value)
        if late:
            owner.invalidate()
        if failure:
            raise OSError("transport failure")

    asyncio.run(owner.publish([20, 10], CheckPublication(send, submitted.append, None)))
    assert packets == [{"cmd": "LocationChecks", "locations": [20, 10]}]
    assert submitted == ([] if late or failure else [20, 10])
    assert owner.last_event == 10


def test_real_file_flush_retains_submitted_files_until_server_ack(tmp_path, monkeypatch):
    bridge = _active_bridge_client()
    context = _create_test_context()
    context.get_ap_state_key = lambda: "room-A"
    context.check_and_update_event_session = lambda: True
    context.item_state_ready = True
    context.checked_locations = set()
    context.locations_checked = set()
    context.server_locations = {10}
    context.server_checked_locations_ready = True
    context.record_local_automap_cleanup_ownership = lambda *_: None
    path = tmp_path / "ap_event_10.txt"
    path.write_text("physical event")
    monkeypatch.setattr(bridge, "check_event_files", lambda: list(tmp_path.glob("ap_event_*.txt")))
    monkeypatch.setattr(bridge, "extract_location_id_from_event", lambda _: 10)
    packets = []

    async def send(value):
        packets.extend(value)

    context.send_msgs = send
    asyncio.run(context.flush_check_event_files())
    assert path.exists() and context.locations_checked == {10}
    asyncio.run(context.flush_check_event_files())
    assert len(packets) == 1 and path.exists()
    context.checked_locations.add(10)
    asyncio.run(context.flush_check_event_files())
    assert not path.exists() and len(packets) == 1
