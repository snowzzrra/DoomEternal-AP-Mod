import pytest

from doom_eap.runtime.deathlink import DeathLinkDispatchError, DeathLinkReceiver, ReceiveState


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


def test_duplicate_and_reconnect_identity_do_not_requeue():
    receiver = DeathLinkReceiver(burst_interval=0.5)
    spool = FakeSpool()
    assert receiver.receive("one", 0.0).detail == "queued"
    assert receiver.receive("one", 0.5).detail == "duplicate"
    advance(receiver, spool, 1.0)
    spool.delivered()
    assert advance(receiver, spool, 1.1).state is ReceiveState.APPLIED
    assert advance(receiver, spool, 1.15).state is None
    assert spool.dispatches == 1
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


def _assert_refusal_backoff_and_expiry(calls, event, receiver, tick):
    assert tick(2).detail == "retry_interval"
    assert tick(3).detail == "native_budget_refused"
    assert tick(59).detail == "native_budget_refused"
    assert receiver.active is event and event.total_deadline == 60
    assert tick(60).state is ReceiveState.EXPIRED
    assert tick(61).state is None
    assert len(calls) == 3 and event.attempts == event.deliveries == 0


def test_persistent_budget_refusal_expires_with_backoff_and_no_delivery():
    receiver = DeathLinkReceiver()
    receiver.receive("one", 0)
    event = receiver.active
    calls = []

    def dispatch():
        calls.append(True)
        raise DeathLinkDispatchError(2, {"state": 4, "reason": 25})

    def tick(now):
        return receiver.advance(now=now, safe_gameplay=True, dispatch=dispatch, command_in_flight=lambda: False)

    assert tick(1).detail == "native_budget_refused"
    _assert_refusal_backoff_and_expiry(calls, event, receiver, tick)
    assert receiver.confirm_local_death(61).detail == "not_linked"


@pytest.mark.parametrize("error", [
    OSError("transport interrupted"),
    DeathLinkDispatchError(2, {"state": 6, "reason": 25}),
    DeathLinkDispatchError(2, {"state": 4, "reason": 1}),
    DeathLinkDispatchError(0, {"state": 4, "reason": 25}),
])
def test_uncertain_or_other_refusal_never_replays(error):
    receiver = DeathLinkReceiver()
    receiver.receive("one", 0)
    calls = []

    def dispatch():
        calls.append(True)
        raise error

    def tick(now):
        return receiver.advance(now=now, safe_gameplay=True, dispatch=dispatch, command_in_flight=lambda: False)

    assert tick(1).state is ReceiveState.FAILED
    assert tick(3).state is None
    assert len(calls) == 1
