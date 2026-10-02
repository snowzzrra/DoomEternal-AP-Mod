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

    # hit 1 dispatched
    res1 = advance(receiver, spool, 1.0)
    assert res1.state is ReceiveState.COMMAND_IN_FLIGHT
    assert res1.detail == "dispatched"
    assert spool.dispatches == 1

    # Hit 1 delivered -> enters BURST_IN_FLIGHT waiting 0.5s
    spool.delivered()
    res2 = advance(receiver, spool, 1.1)
    assert res2.state is ReceiveState.BURST_IN_FLIGHT
    assert res2.detail == "burst_wait"

    # still waiting (only 0.3s elapsed)
    res3 = advance(receiver, spool, 1.4)
    assert res3.state is ReceiveState.BURST_IN_FLIGHT
    assert res3.detail == "burst_wait"
    assert spool.dispatches == 1

    # 0.5s elapsed -> hit 2 dispatched
    res4 = advance(receiver, spool, 1.6)
    assert res4.state is ReceiveState.COMMAND_IN_FLIGHT
    assert res4.detail == "dispatched"
    assert spool.dispatches == 2

    # hit 2 delivered -> burst complete
    spool.delivered()
    res5 = advance(receiver, spool, 1.7)
    assert res5.state is ReceiveState.APPLIED
    assert res5.detail == "accepted"
    assert receiver.active is None




def test_unsafe_gameplay_drops_second_hit_failsafe():
    receiver = DeathLinkReceiver(burst_interval=0.5)
    spool = FakeSpool()
    receiver.receive("one", 0.0)

    # hit 1 dispatched and delivered
    advance(receiver, spool, 1.0)
    spool.delivered()
    advance(receiver, spool, 1.1)

    # At 500ms deadline, environment is unsafe (e.g. paused/loading/menu)
    res = advance(receiver, spool, 1.6, safe=False)
    assert res.state is ReceiveState.APPLIED
    assert res.detail == "second_hit_cancelled_unsafe"
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
