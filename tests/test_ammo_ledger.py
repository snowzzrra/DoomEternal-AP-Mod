"""Exercise paid refill recovery through isolated AP/Core peers and durable profiles."""

import json
import sys

import pytest
from delivery_scenario_support import FIXTURES, Child, Scenario, prepare


@pytest.fixture
def peer(tmp_path):
    scenario = Scenario(tmp_path, backend="v2")
    try:
        yield scenario
    finally:
        scenario.close()


def fault(peer, name):
    peer.ap.send({"action": "ammo_fault", "fault": name})
    peer.ap.expect("configured")


def storage(peer):
    return json.loads((peer.root / "ap-storage.json").read_text())


@pytest.mark.parametrize("failure", ["refused", "lost", "foreign", "duplicate"])
def test_refill_requires_correlated_debit_and_recovers_the_same_operation(peer, failure):
    peer.start()
    fault(peer, failure)
    status = peer.action("ammo")
    operation, = status["ammo_operations"]
    identifier = operation["id"]
    assert peer.effects() == []
    if failure != "duplicate":
        assert operation["state"] == "unknown"
        assert status["ammo_reserved"] == 1 and status["ammo_consumed"] == 0
        assert peer.action("ammo_deliver")["blocked"]
        assert peer.effects() == []
    fault(peer, "")
    status = peer.action("ammo_recover")
    if failure == "refused":
        assert status["ammo_operations"][0]["state"] == "unknown"
        status = peer.action("ammo_retry", operation_id=identifier,
                             revision=status["ammo_operations"][0]["revision"])
    assert status["ammo_operations"][0]["id"] == identifier
    assert status["ammo_consumed"] == 1 and status["ammo_reserved"] == 0
    peer.action("ammo_deliver")
    peer.action("ammo_recover")
    peer.action("ammo_deliver")
    assert peer.effects() == [f"{identifier}:0"]
    values = storage(peer)
    assert values["doom_eap:DeliveryRoomA:ammo_refill_consumed"] == 1
    assert sum("ammo/ops/" in key for key in values) == 1


@pytest.mark.parametrize("point", ["ammo_reservation:after", "ammo_total:before",
                                   "ammo_confirmation:before", "ammo_confirmation:after"])
def test_restart_between_reservation_operation_total_and_commit_never_debits_twice(peer, point):
    peer.start(point)
    peer.bridge.send({"action": "ammo"})
    peer.bridge.expect(point)
    peer.kill_bridge()
    status = peer.start()
    operation, = status["ammo_operations"]
    assert peer.effects() == []
    status = peer.action("ammo_recover")
    if status["ammo_operations"][0]["state"] == "unknown":
        assert peer.action("ammo_deliver")["blocked"]
        status = peer.action("ammo_retry", operation_id=operation["id"],
                             revision=status["ammo_operations"][0]["revision"])
    assert status["ammo_consumed"] == 1 and status["ammo_reserved"] == 0
    assert status["ammo_operations"][0]["id"] == operation["id"]
    peer.action("ammo_deliver")
    peer.action("ammo_recover")
    peer.action("ammo_deliver")
    assert peer.effects() == [f"{operation['id']}:0"]
    values = storage(peer)
    assert values["doom_eap:DeliveryRoomA:ammo_refill_consumed"] == 1
    assert sum("ammo/ops/" in key for key in values) == 1


def test_first_profile_keeps_spending_and_overflow_ownership(peer):
    peer.start()
    first = peer.action("ammo")
    root = peer.root / "second-profile"
    prepare(root)
    second = Child([sys.executable, "-B", str(FIXTURES / "delivery_v2_worker.py"),
                    f"ws://127.0.0.1:{peer.port}", "", f"ws://127.0.0.1:{peer.core_port}"], root)
    try:
        second.expect("ready")
        peer.ap.expect("connected")
        second.send({"action": "collect"})
        second.expect("awaiting_packet")
        peer.ap.send({"action": "send", "packets": [
            {"cmd": "ReceivedItems", "index": 0, "items": [[7770024, 1, 1, 0]]},
        ]})
        peer.ap.expect("sent")
        assert second.expect("done")["received"] == 1
        second.send({"action": "ammo", "received": 8})
        status = second.expect("done")
        assert status["ammo_operations"] == []
        assert status["ammo_consumed"] == status["ammo_discarded"] == 0
        assert peer.action("ammo")["ammo_consumed"] == first["ammo_consumed"] + 1
        assert sum("ammo/ops/" in key for key in storage(peer)) == 2
    finally:
        second.close()


def test_overflow_is_absolute_and_restart_does_not_restore_spent_charges(peer):
    peer.start()
    status = peer.action("ammo", received=6, crucible=True)
    assert status["ammo_consumed"] == 1 and status["ammo_discarded"] == 3
    peer.action("ammo_deliver")
    assert len(peer.effects()) == 2
    peer.kill_bridge()
    peer.start()
    status = peer.action("ammo_recover")
    assert status["ammo_consumed"] == 1 and status["ammo_discarded"] == 3
    peer.action("ammo_deliver")
    assert len(peer.effects()) == 2
    assert storage(peer)["doom_eap:DeliveryRoomA:ammo_refill_discarded"] == 3


def test_uncertain_effect_requires_explicit_retry_and_reuses_the_paid_operation(peer):
    peer.start()
    status = peer.action("ammo")
    operation, = status["ammo_operations"]
    original, = status["commands"]
    peer.action("ammo_deliver", recover=False)
    peer.action("dispatch_only", command_id=original)
    uncertain = peer.action("result", command_id=original)["ammo_commands"][0]
    assert uncertain["state"] == "effect_unknown"
    peer.action("ammo_recover")
    peer.action("ammo_deliver")
    assert peer.effects() == [original]
    assert storage(peer)["doom_eap:DeliveryRoomA:ammo_refill_consumed"] == 1

    peer.action("resolve", command_id=original, revision=uncertain["revision"], resolution="try_again")
    status = peer.action("resolve", command_id=original, revision=uncertain["revision"], resolution="try_again")
    assert len(status["commands"]) == 2
    assert status["ammo_operations"] == [operation]
    assert status["ammo_consumed"] == 1
    peer.action("ammo_deliver")
    assert len(peer.effects()) == 2
    assert sum("ammo/ops/" in key for key in storage(peer)) == 1
    assert storage(peer)["doom_eap:DeliveryRoomA:ammo_refill_consumed"] == 1


def test_server_totals_cannot_be_reused_as_a_new_debit(peer):
    peer.start()
    assert peer.action("ammo")["ammo_consumed"] == 1
    peer.ap.send({"action": "store", "values": {"doom_eap:DeliveryRoomA:ammo_refill_consumed": 2}})
    peer.ap.expect("stored")
    status = peer.action("ammo")
    assert status["ammo_consumed"] == 3
    assert len(status["ammo_operations"]) == 2
    assert peer.action("ammo")["ammo_operations"] == status["ammo_operations"]
    assert peer.effects() == []
