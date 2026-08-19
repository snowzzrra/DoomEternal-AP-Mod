from doom_eap.runtime.deathlink_receive import DeathLinkReceiver, ReceiveState


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


def test_two_hit_burst_full_lifecycle():
    receiver = DeathLinkReceiver(burst_interval=0.5)
    spool = FakeSpool()
    receiver.receive("one", 0.0)

    # Hit 1 dispatched
    res1 = advance(receiver, spool, 1.0)
    assert res1.state is ReceiveState.COMMAND_IN_FLIGHT
    assert res1.detail == "dispatched"
    assert spool.dispatches == 1

    # Hit 1 delivered -> enters BURST_IN_FLIGHT waiting 0.5s
    spool.delivered()
    res2 = advance(receiver, spool, 1.1)
    assert res2.state is ReceiveState.BURST_IN_FLIGHT
    assert res2.detail == "burst_wait"

    # Still waiting (only 0.3s elapsed)
    res3 = advance(receiver, spool, 1.4)
    assert res3.state is ReceiveState.BURST_IN_FLIGHT
    assert res3.detail == "burst_wait"
    assert spool.dispatches == 1

    # 0.5s elapsed -> Hit 2 dispatched
    res4 = advance(receiver, spool, 1.6)
    assert res4.state is ReceiveState.COMMAND_IN_FLIGHT
    assert res4.detail == "dispatched"
    assert spool.dispatches == 2

    # Hit 2 delivered -> burst complete
    spool.delivered()
    res5 = advance(receiver, spool, 1.7)
    assert res5.state is ReceiveState.APPLIED
    assert res5.detail == "accepted"
    assert receiver.active is None


def test_first_hit_death_cancels_second_hit():
    receiver = DeathLinkReceiver(burst_interval=0.5)
    spool = FakeSpool()
    receiver.receive("one", 0.0)

    # Hit 1 dispatched and delivered
    advance(receiver, spool, 1.0)
    spool.delivered()
    advance(receiver, spool, 1.1)

    # Player confirmed dead before 500ms elapses
    res = receiver.confirm_local_death(1.3)
    assert res.state is ReceiveState.RESOLVED
    assert res.detail == "second_hit_cancelled_player_dead"
    assert receiver.active is None

    # Subsequent ticks do not send Hit 2
    res2 = advance(receiver, spool, 1.6)
    assert res2.state is None
    assert res2.detail == "idle"
    assert spool.dispatches == 1


def test_unsafe_gameplay_drops_second_hit_failsafe():
    receiver = DeathLinkReceiver(burst_interval=0.5)
    spool = FakeSpool()
    receiver.receive("one", 0.0)

    # Hit 1 dispatched and delivered
    advance(receiver, spool, 1.0)
    spool.delivered()
    advance(receiver, spool, 1.1)

    # At 500ms deadline, environment is unsafe (e.g. paused/loading/menu)
    res = advance(receiver, spool, 1.6, safe=False)
    assert res.state is ReceiveState.APPLIED
    assert res.detail == "second_hit_cancelled_unsafe"
    assert receiver.active is None
    assert spool.dispatches == 1


def test_second_hit_dispatch_failure_failsafe():
    receiver = DeathLinkReceiver(burst_interval=0.5)
    spool = FakeSpool()
    receiver.receive("one", 0.0)

    advance(receiver, spool, 1.0)
    spool.delivered()
    advance(receiver, spool, 1.1)

    res = receiver.advance(
        now=1.6,
        safe_gameplay=True,
        dispatch=lambda: False,
        command_in_flight=lambda: False,
    )
    assert res.state is ReceiveState.APPLIED
    assert res.detail == "second_hit_not_accepted"
    assert receiver.active is None
    assert spool.dispatches == 1


def test_duplicate_and_reconnect_identity_do_not_requeue():
    receiver = DeathLinkReceiver(burst_interval=0.5)
    spool = FakeSpool()
    assert receiver.receive("one", 0.0).detail == "queued"
    assert receiver.receive("one", 0.5).detail == "duplicate"
    advance(receiver, spool, 1.0)
    spool.delivered()
    advance(receiver, spool, 1.1)
    receiver.confirm_local_death(1.2)
    assert receiver.receive("one", 3.0).detail == "duplicate"
    assert receiver.active is None


def test_local_death_after_confirmation_is_not_suppressed():
    receiver = DeathLinkReceiver(burst_interval=0.5)
    spool = FakeSpool()
    receiver.receive("one", 0.0)
    advance(receiver, spool, 1.0)
    spool.delivered()
    advance(receiver, spool, 1.1)
    assert receiver.confirm_local_death(1.2).detail == "second_hit_cancelled_player_dead"
    assert receiver.confirm_local_death(3.0).detail == "not_linked"


def test_late_death_after_burst_is_suppressed_once_within_grace():
    receiver = DeathLinkReceiver(
        burst_interval=0.5,
        total_timeout=4.0,
        late_suppression_grace=2.0,
    )
    spool = FakeSpool()
    receiver.receive("one", 0.0)
    advance(receiver, spool, 0.5)
    spool.delivered()
    advance(receiver, spool, 0.6)
    advance(receiver, spool, 1.1)
    spool.delivered()
    assert advance(receiver, spool, 1.2).state is ReceiveState.APPLIED
    assert receiver.confirm_local_death(2.0).detail == "late_echo_suppressed"
    assert receiver.confirm_local_death(2.1).detail == "not_linked"


def test_only_one_command_is_ever_in_flight():
    receiver = DeathLinkReceiver(burst_interval=0.5)
    spool = FakeSpool()
    receiver.receive("one", 0.0)
    advance(receiver, spool, 1.0)
    for now in (1.1, 1.2, 1.3):
        assert advance(receiver, spool, now).detail == "awaiting_delivery"
    assert spool.dispatches == 1


def test_dispatch_failure_fails_safe_without_retry():
    receiver = DeathLinkReceiver()
    receiver.receive("one", 0.0)
    result = receiver.advance(
        now=1.0,
        safe_gameplay=True,
        dispatch=lambda: False,
        command_in_flight=lambda: False,
    )
    assert result.state is ReceiveState.FAILED
    assert result.detail == "rpc_not_accepted"
    assert receiver.active is None


def test_legacy_mode_string_normalizes_to_single_burst():
    receiver = DeathLinkReceiver(mode="hardcore")
    assert receiver.mode == "soft"
    assert receiver.max_burst_hits == 2
    assert receiver.max_attempts == 2
    receiver.configure_mode("hardcore")
    assert receiver.mode == "soft"
    assert receiver.max_burst_hits == 2
    assert receiver.max_attempts == 2
