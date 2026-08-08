from deathlink_receive import DeathLinkReceiver, ReceiveState


def test_dispatch_occurs_once_after_rpc_acceptance():
    receiver = DeathLinkReceiver()
    calls = 0

    def dispatch():
        nonlocal calls
        calls += 1
        return True

    receiver.receive("one", 0.0)
    assert receiver.advance(now=1.0, safe_gameplay=True, dispatch=dispatch).state is ReceiveState.DISPATCHED_ONCE
    assert receiver.advance(now=2.0, safe_gameplay=True, dispatch=dispatch).detail == "awaiting_confirmation"
    assert calls == 1


def test_duplicate_packet_is_ignored():
    receiver = DeathLinkReceiver()
    assert receiver.receive("one", 0.0).detail == "queued"
    assert receiver.receive("one", 1.0).detail == "duplicate"
    assert receiver.queued_event_ids == ("one",)


def test_reconnect_does_not_requeue_confirmed_event():
    receiver = DeathLinkReceiver()
    receiver.receive("one", 0.0)
    receiver.advance(now=1.0, safe_gameplay=True, dispatch=lambda: True)
    assert receiver.confirm_local_death().state is ReceiveState.CONFIRMED
    assert receiver.receive("one", 2.0).detail == "duplicate"
    assert receiver.active is None
    assert receiver.queued_event_ids == ()


def test_expiry_clears_active_event_and_suppression():
    receiver = DeathLinkReceiver(confirm_timeout=2.0)
    receiver.receive("one", 0.0)
    receiver.advance(now=1.0, safe_gameplay=True, dispatch=lambda: True)
    assert receiver.suppression_event_id == "one"
    result = receiver.advance(now=3.0, safe_gameplay=True, dispatch=lambda: True)
    assert result.state is ReceiveState.EXPIRED
    assert receiver.active is None
    assert receiver.suppression_event_id is None
    assert receiver.confirm_local_death().detail == "not_linked"


def test_distinct_events_use_bounded_fifo_without_overwrite():
    receiver = DeathLinkReceiver(max_queue=2)
    receiver.receive("one", 0.0)
    receiver.receive("two", 0.1)
    assert receiver.receive("three", 0.2).detail == "queue_full"
    receiver.advance(now=1.0, safe_gameplay=True, dispatch=lambda: True)
    assert receiver.confirm_local_death().state is ReceiveState.CONFIRMED
    assert receiver.active is not None
    assert receiver.active.event_id == "two"
