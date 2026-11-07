import asyncio
import logging
from types import SimpleNamespace
from typing import ClassVar

import pytest

from doom_eap.runtime import deathlink as domain
from doom_eap.runtime.deathlink import DeathLinkDispatchError, DeathLinkPublication, DeathLinkReceiver, ReceiveState


def setup_session(tmp_path):
    events = []
    owner = domain.DeathLinkSession(DeathLinkReceiver(), ["{player} fell."],
                                   lambda *a, **kw: events.append((a, kw)), logging.getLogger("deathlink"))
    state = {}
    owner.bind("room-A", state)
    owner.configure(True)
    class Native:
        calls: ClassVar[list] = []
        event_id = 0
        event_hash = None
        def deathlink_request(self, kind, **args):
            self.calls.append(kind)
            if kind == 2:
                self.event_id = args["event_id"]
                self.event_hash = args["event_hash"]
            if kind == 5:
                assert args["event_id"] == self.event_id != 0
                assert args["event_hash"] == self.event_hash
            return dict(outcome=0, remote_state=2, remote_event_id=self.event_id)
    native = Native()
    publication = DeathLinkPublication(lambda: native, None, owner.mode)
    return owner, state, publication, events


def _assert_deduplicated_death(data, event_id, owner, persist, persisted):
    assert event_id and len(persisted) == 1
    assert owner.receive(data, persist) is None and len(persisted) == 1


def test_receive_history_safe_gate_and_echo_cancellation_preserve_native_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(domain, "time", SimpleNamespace(monotonic=lambda: 10))
    owner, state, publication, events = setup_session(tmp_path)
    data = {"time": 3, "source": "player", "cause": "test"}
    persisted = []

    def persist():
        state["received_deathlink_event_ids"] = sorted(owner.seen_events)[-64:]
        persisted.append(dict(state))

    event_id = owner.receive(data, persist)
    _assert_deduplicated_death(data, event_id, owner, persist, persisted)
    publication._event = owner.active_event
    owner.advance(False, publication)
    assert not list(tmp_path.glob("*.cmd"))
    owner.advance(True, publication)
    assert publication._link().calls == [1, 2]
    sent = []

    async def send(cause):
        sent.append(cause)

    asyncio.run(owner.report_local_death("Slayer", send, publication))
    assert sent == [] and events == []
    assert publication._link().calls == [1, 2, 5]
    assert owner.mode == "soft"
    snapshot = owner.instrumentation()
    with pytest.raises(TypeError):
        snapshot[-1]["detail"] = "changed"
    restarted, _, _, _ = setup_session(tmp_path)
    restarted.bind("room-A", state)
    assert restarted.receive(data, lambda: pytest.fail("duplicate persisted again")) is None


@pytest.mark.parametrize("failure", [False, True])
def test_outbound_completion_after_room_rebind_has_no_new_room_effects(tmp_path, failure):
    async def run():
        owner, _, publication, events = setup_session(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        calls: ClassVar[list] = []

        async def send(cause):
            calls.append(cause)
            entered.set()
            await release.wait()
            if failure:
                raise OSError("old connection")

        task = asyncio.create_task(owner.report_local_death("Slayer", send, publication))
        await asyncio.wait_for(entered.wait(), timeout=5)
        owner.abandon(11, "disconnect")
        owner.bind("room-B", {})
        release.set()
        await task
        assert calls == ["Slayer fell."] and events == []
        assert owner.seen_events == frozenset()

    asyncio.run(run())


def test_current_outbound_failure_is_not_reported_as_confirmation(tmp_path):
    owner, _, publication, events = setup_session(tmp_path)

    async def failed(_cause):
        raise OSError("current connection")

    with pytest.raises(OSError, match="current connection"):
        asyncio.run(owner.report_local_death("Slayer", failed, publication))
    assert events == []


def test_cancel_failure_does_not_stop_later_receives_or_local_deaths(tmp_path, monkeypatch):
    owner, _, publication, _ = setup_session(tmp_path)
    clock = SimpleNamespace(monotonic=lambda: 10)
    monkeypatch.setattr(domain, "time", clock)
    assert owner.receive({"time": 3, "source": "friend"}, lambda: None)
    publication._event = owner.active_event
    owner.advance(True, publication)
    monkeypatch.setattr(publication, "cancel", lambda _: (_ for _ in ()).throw(RuntimeError("native unavailable")))
    sent = []

    async def send(cause):
        sent.append(cause)

    asyncio.run(owner.report_local_death("Slayer", send, publication))
    clock.monotonic = lambda: 1000
    owner.advance(True, publication)
    asyncio.run(owner.report_local_death("Slayer", send, publication))
    assert sent == ["Slayer fell."]
    assert owner.receive({"time": 4, "source": "friend"}, lambda: None)


def _assert_single_death_delivery(data, event, identities, native, owner):
    assert owner.active_event is None and event.deliveries == 1
    assert native.calls.count(2) == 1
    assert len(set(identities)) == 1
    assert owner.receive(data, lambda: pytest.fail("duplicate persisted again")) is None


@pytest.mark.parametrize("kind", [1, 2])
def test_budget_refusal_then_success_applies_same_event_once(tmp_path, monkeypatch, kind):
    clock = SimpleNamespace(monotonic=lambda: 10)
    monkeypatch.setattr(domain, "time", clock)
    owner, _, publication, _ = setup_session(tmp_path)
    data = {"time": 3, "source": "friend"}
    owner.receive(data, lambda: None)
    event = publication._event = owner.active_event
    native = publication._link()
    request = native.deathlink_request
    calls, identities = [], []
    refused = False

    def invoke(operation, **args):
        nonlocal refused
        calls.append(operation)
        if operation == 2:
            identities.append((args["event_id"], args["event_hash"]))
        if operation == kind and not refused:
            refused = True
            error = RuntimeError("native execution refused")
            error.native_result = {"state": 4, "reason": 25}
            raise error
        result = request(operation, **args)
        if operation == 0:
            result["remote_state"] = 4
        return result

    monkeypatch.setattr(native, "deathlink_request", invoke)
    owner.advance(True, publication)
    assert owner.active_event is event and event.attempts == 0
    clock.monotonic = lambda: 11
    owner.advance(True, publication)
    assert calls == ([1] if kind == 1 else [1, 2])
    clock.monotonic = lambda: 12
    owner.advance(True, publication)
    assert event.state is ReceiveState.COMMAND_IN_FLIGHT and event.attempts == 1
    clock.monotonic = lambda: 13
    owner.advance(True, publication)
    owner.advance(True, publication)
    _assert_single_death_delivery(data, event, identities, native, owner)


@pytest.mark.parametrize("kind", [1, 2])
def test_publication_preserves_structured_native_refusal(tmp_path, monkeypatch, kind):
    owner, _, publication, _ = setup_session(tmp_path)
    owner.receive({"time": 3, "source": "friend"}, lambda: None)
    publication._event = owner.active_event
    native = publication._link()
    request = native.deathlink_request
    original = RuntimeError("refused")
    original.native_result = {"state": 4, "reason": 25, "request_id": 123}

    def invoke(operation, **args):
        if operation == kind:
            raise original
        return request(operation, **args)

    monkeypatch.setattr(native, "deathlink_request", invoke)
    with pytest.raises(DeathLinkDispatchError) as caught:
        publication.dispatch("room-A")
    assert caught.value.request_kind == kind
    assert caught.value.native_result is original.native_result
    assert caught.value.__cause__ is original
