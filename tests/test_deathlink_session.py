import asyncio
import logging
from types import SimpleNamespace

import pytest

from doom_eap.runtime import deathlink_session as domain
from doom_eap.runtime.deathlink_receive import DeathLinkReceiver
from doom_eap.runtime.deathlink_publication import DeathLinkPublication


def setup_session(tmp_path):
    events = []
    owner = domain.DeathLinkSession(DeathLinkReceiver(), ["{player} fell."],
                                   lambda *a, **kw: events.append((a, kw)), logging.getLogger("deathlink"))
    state = {}
    owner.bind("room-A", state)
    owner.configure(True)
    class Native:
        calls = []
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


def test_receive_history_safe_gate_and_echo_cancellation_preserve_native_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(domain, "time", SimpleNamespace(monotonic=lambda: 10))
    owner, state, publication, events = setup_session(tmp_path)
    data = {"time": 3, "source": "player", "cause": "test"}
    persisted = []

    def persist():
        state["received_deathlink_event_ids"] = sorted(owner.seen_events)[-64:]
        persisted.append(dict(state))

    event_id = owner.receive(data, persist)
    assert event_id and len(persisted) == 1
    assert owner.receive(data, persist) is None and len(persisted) == 1
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
        calls = []

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
@pytest.mark.parametrize('interval', [0, 1, 3])
def test_grace_sends_every_selected_local_death_and_survives_reconnect(tmp_path, interval):
    owner, state, publication, events = setup_session(tmp_path)
    sent, persisted = [], []
    async def send(cause):
        sent.append(cause)
    for death in range(1, 8):
        if death == 3:
            owner, _, publication, _ = setup_session(tmp_path)
            owner.bind('room-A', state)
        asyncio.run(owner.report_local_death('Slayer', send, publication, grace=interval,
                    persist=lambda: persisted.append(dict(state))))
        assert len(sent) == death // max(1, interval)
    if interval > 1:
        assert persisted and state['death_link_grace']['deaths'] == 7 % interval
        other = {}
        owner.bind('room-B', other)
        asyncio.run(owner.report_local_death('Slayer', send, publication, grace=interval))
        assert other['death_link_grace']['deaths'] == 1
        assert state['death_link_grace']['deaths'] == 7 % interval


def test_received_death_applies_and_does_not_consume_outgoing_grace(tmp_path, monkeypatch):
    monkeypatch.setattr(domain, 'time', SimpleNamespace(monotonic=lambda: 10))
    owner, state, publication, _ = setup_session(tmp_path)
    async def send(cause):
        pytest.fail('incoming link or grace emitted an outgoing link')
    for _ in range(2):
        asyncio.run(owner.report_local_death('Slayer', send, publication, grace=3))
    owner.receive({'time': 3, 'source': 'player'}, lambda: None)
    publication._event = owner.active_event
    owner.advance(True, publication)
    asyncio.run(owner.report_local_death('Slayer', send, publication, grace=3))
    assert publication._link().calls == [1, 2, 5]
    assert state['death_link_grace']['deaths'] == 2


def test_grace_counts_deaths_when_threshold_send_fails(tmp_path):
    owner, state, publication, _ = setup_session(tmp_path)
    async def send(cause):
        raise OSError('connection lost')
    asyncio.run(owner.report_local_death('Slayer', send, publication, grace=2))
    with pytest.raises(OSError):
        asyncio.run(owner.report_local_death('Slayer', send, publication, grace=2))
    assert state['death_link_grace']['deaths'] == 0
    asyncio.run(owner.report_local_death('Slayer', send, publication, grace=2))
    assert state['death_link_grace']['deaths'] == 1


def test_launcher_publishes_grace_as_local_runtime_configuration(tmp_path):
    import json
    from doom_eap.launcher.launcher_core import LaunchWorkflow
    path = LaunchWorkflow.write_client_config(tmp_path, runtime_config={'death_link_grace': 3})
    assert json.loads(path.read_bytes())['death_link_grace'] == 3
