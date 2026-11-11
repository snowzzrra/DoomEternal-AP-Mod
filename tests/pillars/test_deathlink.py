import asyncio
import json
import time
from collections import deque
from types import SimpleNamespace

from websockets.legacy.client import connect

from doom_eap.runtime.deathlink import DEATHLINK_MESSAGES, DeathLinkReceiver, DeathLinkSession
from doom_eap.runtime.inventory import ReceiptSession
from doom_eap.runtime.native_transport import NativeWorker
from tests.delivery_scenario_support import Scenario


class DeathLinkCore:
    pid = 123
    namespace = 'a' * 64
    _process_identity = 'windows:123:456'

    def __init__(self):
        self.scope = dict(target_pid=123, server_pid=123, process_created='456', instance_id='c' * 32,
                          lifecycle_generation=1, context_generation=1, availability='enabled',
                          lifecycle='active', context_reason='none')
        self.admission = dict(self.scope, namespace_id=self.namespace, state='admitted',
                              accepting_requests=True, route_retained=True, save_binding='d' * 32)
        self.queue = deque()
        self.sequence = 0
        self.enabled = False
        self.mode = 'soft'
        self.remote = 0
        self.applied = []

    def _run(self, args):
        return dict(self.scope)

    def request(self, kind):
        assert kind == 12
        return dict(self.admission)

    def die(self, cause=0, flags=0):
        if self.enabled:
            self.sequence += 1
            self.queue.append(dict(local_death_sequence=self.sequence, local_cause=cause, local_flags=flags))

    def deathlink_request(self, kind, *, enabled=False, mode='soft', event_id=0, event_hash=None, ack_sequence=0):
        if kind == 1:
            self.enabled, self.mode = enabled, mode
        elif kind == 2:
            self.applied.append(event_id)
            self.die(cause=1, flags=512)
            self.remote = event_id
        elif kind == 4:
            self.ack(ack_sequence)
        event = self.queue[0] if self.queue else dict(local_death_sequence=0, local_cause=0, local_flags=0)
        return dict(self.scope, namespace_id=self.namespace, flags=3, outcome=0, enabled=int(self.enabled),
                    mode=int(self.mode == 'hardcore'), remote_event_id=self.remote, remote_state=6 if self.remote else 0,
                    local_state=int(bool(self.queue)), **event)

    def ack(self, sequence):
        assert self.queue[0]['local_death_sequence'] == sequence
        self.queue.popleft()


def deathlink_context(bridge, socket, monkeypatch):
    from doom_eap.runtime import bridge_config

    context = bridge.DoomEternalContext.__new__(bridge.DoomEternalContext)
    context.auth, context.slot, context.team = 'Slayer', 1, 0
    context.player_names = {1: 'Slayer'}
    context.server = SimpleNamespace(socket=socket)
    context.state_key = 'seed:0:1:generation'
    context.session_state = {}
    context.receipt_session = ReceiptSession()
    context.items = bridge.bridge_items.ItemRuntime(context)
    context.checks = bridge.bridge_checks.CheckRuntime(context)
    context.room_delivery = None
    context._slot_validation_task = None
    context.last_death_link = 0
    context.persist_session_state = lambda: None
    monkeypatch.setattr(bridge_config, 'load_config', lambda: dict(death_link_grace=3))
    context.deathlink = DeathLinkSession(DeathLinkReceiver(), DEATHLINK_MESSAGES,
                                        lambda *args, **kwargs: None, bridge_config.logger)
    context.deathlink.configure(True)
    context.deathlink.bind(context.state_key, context.session_state)
    context.deathlink.reconfigure_grace(3, context.persist_session_state)
    return context


def attach_core(context, core, monkeypatch):
    worker = NativeWorker(lambda: context.state_key, lambda: core._process_identity)

    async def link():
        return core

    async def identity():
        return core._process_identity

    monkeypatch.setattr(context.items, '_native_game_link_async', link)
    monkeypatch.setattr(context.items, '_query_process_identity', identity)
    monkeypatch.setattr(context.items, '_native_game_link_for_identity', lambda identity: core)
    monkeypatch.setattr(context.items, '_blocking_native_worker', lambda: worker)
    monkeypatch.setattr(context.items, 'reset_transient_effects', lambda reason: None)
    return worker


async def die_three_times(context, core, sent):
    for deaths in range(1, 4):
        core.die()
        await context.checks.poll_native_deathlink()
        assert context.session_state['death_link_grace']['deaths'] == deaths % 3
        outgoing = [p for p in sent if p['cmd'] == 'Bounce']
        assert len(outgoing) == int(deaths == 3)


def test_deathlink_sends_after_three_deaths_and_applies_remote_once(bridge, tmp_path, monkeypatch):
    from CommonClient import process_server_cmd

    peer = Scenario(tmp_path / 'peers')

    async def play():
        async with connect(f'ws://127.0.0.1:{peer.port}') as socket:
            peer.ap.expect('connected')
            core = DeathLinkCore()
            context = deathlink_context(bridge, socket, monkeypatch)
            sent = []

            async def send(packets):
                sent.extend(packets)
                await socket.send(json.dumps(packets))

            context.send_msgs = send
            worker = attach_core(context, core, monkeypatch)
            try:
                await context.checks.poll_native_deathlink()
                assert core.enabled
                await die_three_times(context, core, sent)
                echo, = json.loads(await asyncio.wait_for(socket.recv(), 30))
                await process_server_cmd(context, echo)
                assert not context.deathlink.receiving

                packet = dict(cmd='Bounced', tags=['DeathLink'],
                              data=dict(time=time.time() + 100, source='Other player', cause='Remote death'))
                peer.ap.send(dict(action='send', packets=[packet, packet]))
                peer.ap.expect('sent')
                for incoming in json.loads(await asyncio.wait_for(socket.recv(), 30)):
                    await process_server_cmd(context, incoming)
                await context.checks.queue_received_deathlink()
                await context.checks.queue_received_deathlink()
                await context.checks.poll_native_deathlink()
                assert len(core.applied) == 1
                assert not context.deathlink.receiving and not core.queue
                peer.ap.send(dict(action='snapshot'))
                assert len([p for p in peer.ap.expect('snapshot')['messages'] if p['cmd'] == 'Bounce']) == 1
            finally:
                worker.close()

    try:
        asyncio.run(play())
    finally:
        peer.close()
