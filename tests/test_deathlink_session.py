import asyncio
import logging
from types import SimpleNamespace

import pytest

from doom_eap.runtime import deathlink_session as domain
from doom_eap.runtime.deathlink_receive import DeathLinkReceiver
from doom_eap.runtime.deathlink_publication import DeathLinkPublication
from doom_eap.runtime.command_spool import CommandSpool, discard_unclaimed_command


def setup_session(tmp_path):
    events = []
    owner = domain.DeathLinkSession(DeathLinkReceiver(), ["{player} fell."],
                                   lambda *a, **kw: events.append((a, kw)), logging.getLogger("deathlink"))
    state = {}
    owner.bind("room-A", state)
    owner.configure(True)
    spool = CommandSpool(tmp_path, arm_rpc=lambda *_: None, log_delivery=lambda *_, **__: None,
                         logger=logging.getLogger("deathlink"))
    publication = DeathLinkPublication(
        lambda command, **kwargs: spool.publish(command, **kwargs).accepted,
        spool.exists,
        lambda key, room: discard_unclaimed_command(tmp_path, spool.scoped_id(key, room)),
    )
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
    owner.advance(False, publication)
    assert not list(tmp_path.glob("*.cmd"))
    owner.advance(True, publication)
    command = next(tmp_path.glob("*.cmd"))
    processing = command.with_suffix(".processing")
    command.rename(processing)
    sent = []

    async def send(cause):
        sent.append(cause)

    asyncio.run(owner.report_local_death("Slayer", send, publication))
    assert sent == [] and events == [] and processing.exists()
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
