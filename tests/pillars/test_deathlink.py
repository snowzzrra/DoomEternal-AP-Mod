import asyncio
import json
import logging
import time
from collections import deque
from types import SimpleNamespace

import pytest
from websockets.legacy.client import connect

from doom_eap.runtime.deathlink import DEATHLINK_MESSAGES, DeathLinkReceiver, DeathLinkSession, DeathObservation
from doom_eap.runtime.inventory import ReceiptSession
from doom_eap.runtime.native_transport import NativeWorker
from tests.delivery_scenario_support import Scenario


class DeathLinkCore:
    pid = 123
    namespace = 'a' * 64
    _process_identity = 'windows:123:456'

    def __init__(self, path):
        self.path = path
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
        self.applied = set()
        self.fail_ack = False

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
            if event_id not in self.applied:
                self.applied.add(event_id)
                self.die(cause=1, flags=512)
            self.remote = event_id
        elif kind == 4:
            self.ack(ack_sequence)
        event = self.queue[0] if self.queue else dict(local_death_sequence=0, local_cause=0, local_flags=0)
        return dict(self.scope, namespace_id=self.namespace, flags=3, outcome=0, enabled=int(self.enabled),
                    mode=int(self.mode == 'hardcore'), remote_event_id=self.remote, remote_state=6 if self.remote else 0,
                    local_state=int(bool(self.queue)), **event)

    def ack(self, sequence):
        state = json.loads(self.path.read_text())
        decision = state['death_link_native'][f'{self.namespace}:{self.scope["instance_id"]}']
        assert decision['sequence'] == sequence
        assert decision['status'] != 'pending'
        if self.fail_ack:
            self.fail_ack = False
            raise OSError('ACK lost')
        assert self.queue[0]['local_death_sequence'] == sequence
        self.queue.popleft()


def deathlink_context(bridge, path, socket, events, monkeypatch, grace):
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
    context.persist_session_state = lambda: path.write_text(json.dumps(context.session_state), encoding='utf-8')
    monkeypatch.setattr(bridge_config, 'load_config', lambda: dict(death_link_grace=grace))
    def emit(kind, **payload):
        if kind == 'deathlink_progress' and payload['status'] == 'ready':
            persisted = json.loads(path.read_text()).get('death_link_grace', {})
            assert payload['remaining'] == payload['interval'] - persisted.get('deaths', 0)
        events.append((kind, payload))
    monkeypatch.setattr(bridge_config, 'emit_launcher_event', emit)
    context.deathlink = DeathLinkSession(DeathLinkReceiver(), DEATHLINK_MESSAGES, emit, bridge_config.logger)
    context.deathlink.configure(True)
    context.deathlink.bind(context.state_key, context.session_state)
    context.deathlink.reconfigure_grace(grace, context.persist_session_state)
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


async def receive_packet(bridge, context, socket):
    from CommonClient import process_server_cmd

    packet, = json.loads(await asyncio.wait_for(socket.recv(), 5))
    await process_server_cmd(context, packet)
    return packet


async def send_deaths(bridge, context, core, socket, interval):
    for _ in range(interval * 2):
        core.die()
    await context.checks.poll_native_deathlink()
    assert not core.queue
    for _ in range(2):
        await receive_packet(bridge, context, socket)
    assert not context.deathlink.receiving
    await context.checks.poll_native_deathlink()


async def retry_death(bridge, context, core, socket, monkeypatch, interval):
    for _ in range(interval - 1):
        core.die()
        await context.checks.poll_native_deathlink()
    core.die()
    send = context.send_msgs

    async def failed_send(packets):
        raise OSError('AP send failed')

    monkeypatch.setattr(context, 'send_msgs', failed_send)
    await context.checks.poll_native_deathlink()
    persisted = json.loads(core.path.read_text())
    assert next(iter(persisted['death_link_native'].values()))['status'] == 'pending'
    context.session_state = persisted
    context.deathlink.bind(context.state_key, persisted)
    monkeypatch.setattr(context, 'send_msgs', send)
    core.fail_ack = True
    await context.checks.poll_native_deathlink()
    echo = await receive_packet(bridge, context, socket)
    assert echo['data']['time'] == next(iter(persisted['death_link_native'].values()))['time']
    assert core.queue
    context.session_state = json.loads(core.path.read_text())
    context.deathlink.bind(context.state_key, context.session_state)
    await context.checks.poll_native_deathlink()
    assert not core.queue
    assert context.session_state.get('death_link_grace', {}).get('deaths', 0) == 0


async def receive_remote(bridge, context, core, peer, socket, caplog):
    data = dict(time=time.time() + 100, source='Other player', cause='Remote death')
    packet = dict(cmd='Bounced', tags=['DeathLink'], data=data)
    peer.ap.send(dict(action='send', packets=[packet, packet]))
    peer.ap.expect('sent')
    packets = json.loads(await asyncio.wait_for(socket.recv(), 5))
    from CommonClient import process_server_cmd
    for packet in packets:
        await process_server_cmd(context, packet)
    assert not any('Server received and echoed' in row.message for row in caplog.records)
    await wait_for_native_scope(context, core, caplog)
    await context.checks.queue_received_deathlink()
    await context.checks.queue_received_deathlink()
    await context.checks.poll_native_deathlink()
    assert len(core.applied) == 1
    assert not context.deathlink.receiving
    assert not core.queue


async def wait_for_native_scope(context, core, caplog):
    instance = core.scope['instance_id']
    core.scope['instance_id'] = core.admission['instance_id'] = None
    await context.checks.queue_received_deathlink()
    assert not core.applied
    core.scope['instance_id'] = core.admission['instance_id'] = instance
    core.admission['namespace_id'] = 'b' * 64
    await context.checks.queue_received_deathlink()
    assert not core.applied
    core.admission['namespace_id'] = core.namespace
    core.scope['context_generation'] = 2
    await context.checks.queue_received_deathlink()
    assert not core.applied
    core.scope['context_generation'] = 1
    core.admission['save_binding'] = '0' * 32
    await context.checks.queue_received_deathlink()
    assert not core.applied
    assert 'native_namespace_or_save_binding_unavailable' in caplog.text
    core.admission['save_binding'] = 'd' * 32


@pytest.mark.parametrize('grace', [0, 1, 3])
def test_deathlink_native_queue_and_server_once_per_interval(bridge, tmp_path, monkeypatch, caplog, grace):
    caplog.set_level(logging.INFO)
    peer = Scenario(tmp_path / 'peers')

    async def play():
        async with connect(f'ws://127.0.0.1:{peer.port}') as socket:
            peer.ap.expect('connected')
            events = []
            core = DeathLinkCore(tmp_path / 'deathlink-state.json')
            context = deathlink_context(bridge, core.path, socket, events, monkeypatch, grace)

            async def send(packets):
                await socket.send(json.dumps(packets))

            context.send_msgs = send
            worker = attach_core(context, core, monkeypatch)
            try:
                await context.checks.poll_native_deathlink()
                assert core.enabled
                await send_deaths(bridge, context, core, socket, max(1, grace))
                await retry_death(bridge, context, core, socket, monkeypatch, max(1, grace))
                assert caplog.text.count('Server received and echoed the death.') == 3
                await observe_save_copy(context)
                caplog.clear()
                await receive_remote(bridge, context, core, peer, socket, caplog)
                await assert_suppressed_and_reconfigured(bridge, context, core, events, monkeypatch)
                peer.ap.send(dict(action='snapshot'))
                packets = peer.ap.expect('snapshot')['messages']
                assert len([p for p in packets if p['cmd'] == 'Bounce']) == 3
            finally:
                worker.close()

    try:
        asyncio.run(play())
    finally:
        peer.close()


async def assert_suppressed_and_reconfigured(bridge, context, core, events, monkeypatch):
    from doom_eap.runtime import bridge_config

    core.die(cause=1)
    core.die(flags=512)
    await context.checks.poll_native_deathlink()
    assert not core.queue
    monkeypatch.setattr(bridge_config, 'load_config', lambda: dict(death_link_grace=4))
    await bridge._dispatch_launcher_control(context, dict(type='deathlink_grace'))
    assert events[-1][1]['remaining'] == 4
    core.die()
    await context.checks.poll_native_deathlink()
    assert events[-1] == ('deathlink_progress', dict(status='ready', interval=4, remaining=3))
    monkeypatch.setattr(bridge_config, 'load_config', lambda: dict(death_link_grace=2))
    await bridge._dispatch_launcher_control(context, dict(type='deathlink_grace'))
    assert events[-1][1]['remaining'] == 2
    context.deathlink.configure(False)
    await context.checks.poll_native_deathlink()
    assert not core.enabled
    assert events[-1][1]['status'] == 'off'


async def observe_save_copy(context):
    details = dict(diedLastGame='0', _mtime_ns=1, _path='isolated-save')

    async def read_details():
        return dict(details)

    context.death_observer = DeathObservation(logging.getLogger('DeathLink pillar'))
    context.saves = SimpleNamespace(observe_active_game_details=read_details)
    before = json.dumps(context.session_state, sort_keys=True)
    await context.checks.check_game_details_death()
    details.update(diedLastGame='1', _mtime_ns=2)
    await context.checks.check_game_details_death()
    assert json.dumps(context.session_state, sort_keys=True) == before
