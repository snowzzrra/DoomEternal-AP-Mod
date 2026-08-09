from deathlink_receive import DeathLinkReceiver, ReceiveState, discard_unclaimed_command


class FakeSpool:
    def __init__(self):
        self.in_flight = False
        self.dispatches = 0

    def dispatch(self):
        assert not self.in_flight
        self.in_flight = True
        self.dispatches += 1
        return True

    def exists(self):
        return self.in_flight

    def delivered(self):
        self.in_flight = False


def advance(receiver, spool, now, *, safe=True):
    return receiver.advance(
        now=now,
        safe_gameplay=safe,
        dispatch=spool.dispatch,
        command_in_flight=spool.exists,
    )


def test_command_delivery_is_not_death_confirmation():
    receiver = DeathLinkReceiver()
    spool = FakeSpool()
    receiver.receive("one", 0.0)
    assert advance(receiver, spool, 1.0).state is ReceiveState.COMMAND_IN_FLIGHT
    spool.delivered()
    result = advance(receiver, spool, 2.0)
    assert result.state is ReceiveState.AWAITING_CONFIRMATION
    assert result.detail == "delivered"
    assert receiver.active is not None


def test_first_attempt_can_fail_then_retry_confirms_exactly_once():
    receiver = DeathLinkReceiver(confirm_timeout=3.0, retry_interval=2.0)
    spool = FakeSpool()
    receiver.receive("one", 0.0)
    advance(receiver, spool, 1.0)
    spool.delivered()
    advance(receiver, spool, 2.0)
    assert advance(receiver, spool, 5.0).detail == "retry_scheduled"
    assert advance(receiver, spool, 6.0).detail == "retry_interval"
    assert advance(receiver, spool, 7.0).detail == "dispatched"
    spool.delivered()
    assert advance(receiver, spool, 8.0).detail == "delivered"
    assert receiver.confirm_local_death(8.5).state is ReceiveState.CONFIRMED
    assert receiver.confirm_local_death(8.6).detail == "not_linked"
    assert spool.dispatches == 2


def test_duplicate_and_reconnect_identity_do_not_requeue():
    receiver = DeathLinkReceiver()
    spool = FakeSpool()
    assert receiver.receive("one", 0.0).detail == "queued"
    assert receiver.receive("one", 0.5).detail == "duplicate"
    advance(receiver, spool, 1.0)
    spool.delivered()
    advance(receiver, spool, 2.0)
    receiver.confirm_local_death(2.5)
    assert receiver.receive("one", 3.0).detail == "duplicate"
    assert receiver.active is None


def test_local_death_after_confirmation_is_not_suppressed():
    receiver = DeathLinkReceiver()
    spool = FakeSpool()
    receiver.receive("one", 0.0)
    advance(receiver, spool, 1.0)
    spool.delivered()
    advance(receiver, spool, 2.0)
    assert receiver.confirm_local_death(2.1).detail == "echo_suppressed"
    assert receiver.confirm_local_death(3.0).detail == "not_linked"


def test_timeout_is_bounded_and_allows_future_event():
    receiver = DeathLinkReceiver(
        confirm_timeout=2.0,
        retry_interval=1.0,
        total_timeout=8.0,
        max_attempts=2,
    )
    spool = FakeSpool()
    receiver.receive("one", 0.0)
    advance(receiver, spool, 1.0)
    spool.delivered()
    advance(receiver, spool, 2.0)
    advance(receiver, spool, 4.0)
    advance(receiver, spool, 5.0)
    spool.delivered()
    advance(receiver, spool, 5.5)
    result = advance(receiver, spool, 7.5)
    assert result.state is ReceiveState.FAILED
    assert result.detail == "attempt_limit"
    assert receiver.active is None
    assert receiver.receive("two", 8.0).detail == "queued"


def test_late_death_after_timeout_is_suppressed_once_within_grace():
    receiver = DeathLinkReceiver(
        confirm_timeout=1.0,
        retry_interval=1.0,
        total_timeout=4.0,
        late_suppression_grace=2.0,
        max_attempts=1,
    )
    spool = FakeSpool()
    receiver.receive("one", 0.0)
    advance(receiver, spool, 0.5)
    spool.delivered()
    advance(receiver, spool, 1.0)
    assert advance(receiver, spool, 2.0).state is ReceiveState.FAILED
    assert receiver.confirm_local_death(3.0).detail == "late_echo_suppressed"
    assert receiver.confirm_local_death(3.1).detail == "not_linked"


def test_only_one_command_is_ever_in_flight():
    receiver = DeathLinkReceiver(confirm_timeout=2.0, retry_interval=1.0)
    spool = FakeSpool()
    receiver.receive("one", 0.0)
    advance(receiver, spool, 1.0)
    for now in (1.1, 1.5, 2.0, 3.0):
        assert advance(receiver, spool, now).detail == "awaiting_delivery"
    assert spool.dispatches == 1


def test_cancel_never_removes_consumer_processing_file(tmp_path):
    queued = tmp_path / "deathlink-kill.cmd"
    processing = tmp_path / "deathlink-kill.processing"
    queued.write_text("kill\n", encoding="utf-8")
    processing.write_text("consumer owns this\n", encoding="utf-8")
    assert discard_unclaimed_command(tmp_path, "deathlink-kill") is True
    assert not queued.exists()
    assert processing.read_text(encoding="utf-8") == "consumer owns this\n"


def test_distinct_events_use_bounded_fifo_without_overwrite():
    receiver = DeathLinkReceiver(max_queue=2)
    spool = FakeSpool()
    receiver.receive("one", 0.0)
    receiver.receive("two", 0.1)
    assert receiver.receive("three", 0.2).detail == "queue_full"
    advance(receiver, spool, 1.0)
    spool.delivered()
    advance(receiver, spool, 2.0)
    assert receiver.confirm_local_death(2.1).state is ReceiveState.CONFIRMED
    assert receiver.active is not None
    assert receiver.active.event_id == "two"
