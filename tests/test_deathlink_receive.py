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


def test_native_operation_is_dispatched_once():
    receiver = DeathLinkReceiver(mode="hardcore")
    spool = FakeSpool()
    receiver.receive("one", 0)
    assert advance(receiver, spool, 1).state is ReceiveState.COMMAND_IN_FLIGHT
    spool.delivered()
    assert advance(receiver, spool, 2).state is ReceiveState.APPLIED
    assert advance(receiver, spool, 3).state is None
    assert spool.dispatches == 1 and receiver.mode == "hardcore"


def test_unsafe_gameplay_waits_before_native_dispatch():
    receiver = DeathLinkReceiver()
    spool = FakeSpool()
    receiver.receive("one", 0)
    assert advance(receiver, spool, 1, safe=False).detail == "unsafe_gameplay"
    assert spool.dispatches == 0
    assert advance(receiver, spool, 2).detail == "dispatched"


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
