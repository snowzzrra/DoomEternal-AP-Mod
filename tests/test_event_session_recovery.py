import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture(autouse=True)
def process_identity(bridge, monkeypatch):
    from doom_eap.runtime import bridge_config

    monkeypatch.setattr(bridge_config, "doom_process_identity", lambda: "fixture-process")


@pytest.mark.parametrize('baseline,stale', [(None, False), (False, False), (True, False), (False, True)])
def test_death_baseline_precedes_gameplay_and_rejects_a_changed_session(bridge, tmp_path, monkeypatch, baseline, stale):
    import logging

    from doom_eap.runtime import bridge_config, bridge_saves
    from doom_eap.runtime.deathlink import DeathObservation
    from doom_eap.runtime.inventory import namespace_id

    namespace = namespace_id('seed-A', 0, 1, 'a' * 64)
    native_root = 'ap-' + 'b' * 40
    remote = tmp_path / 'remote'
    save = remote / native_root / 'GAME-AUTOSAVE0' / 'game_duration.dat'
    save.parent.mkdir(parents=True)
    if baseline is not None:
        save.write_bytes(b'isolated save fixture')
    monkeypatch.setattr(bridge_config, 'STEAM_REMOTE_DIR', remote)
    monkeypatch.setattr(bridge_saves, 'probe_game_duration', lambda path: {'checkpoint_death': baseline})
    owner = DeathObservation(logging.getLogger(__name__))
    owner.bind(namespace)
    context = SimpleNamespace(
        _death_baseline_pending=True,
        room_seed_name='seed-A',
        team=0,
        slot=1,
        _connected_slot_data={'native_generation_fingerprint': 'a' * 64},
        state_key='room-A',
        receipt_session=SimpleNamespace(capture=lambda key: 'token', is_current=lambda *args: not stale),
        death_observer=owner,
        items=None,
        checks=None,
        saves=None
    )
    context.items = bridge.bridge_items.ItemRuntime(context)
    context.checks = bridge.bridge_checks.CheckRuntime(context)
    context.saves = bridge.bridge_saves.SaveRuntime(context)
    context.saves._admitted_campaign_root = AsyncMock(return_value=native_root)
    context.saves.admitted_campaign_save = AsyncMock(side_effect=lambda: bridge_saves.admitted_ap_save(remote, native_root))


    asyncio.run(context.checks.prime_death_baseline())
    assert context._death_baseline_pending == stale
    if not stale:
        from doom_eap.contracts.save_observation import PrimarySaveSelection
        event = owner.observe_checkpoint({'checkpoint_death': True},
            PrimarySaveSelection('GAME-AUTOSAVE0', save, 10), 1, 'map')
        assert bool(event) == (baseline is not True)


@pytest.fixture
def checkpoint_session(bridge, tmp_path, monkeypatch):
    from doom_eap.launcher import sentinel_diagnostics
    from doom_eap.runtime import bridge_config
    from doom_eap.runtime.inventory import SentinelWeaponPoints

    def create():
        context = bridge.DoomEternalContext(None, None)
        context.state_key = 'room-A'
        context.item_state_ready = True
        context._death_baseline_pending = True
        namespace, native_root = 'a' * 64, 'ap-' + 'b' * 40
        link = SentinelWeaponPoints(tmp_path / 'probe.exe', 71, namespace)
        link._process_identity = 'windows:71:123'
        link._run = Mock(return_value=dict(namespace_id=namespace, native_root=native_root,
            state='admitted', accepting_requests=True, route_retained=True,
            process_created='123', instance_id='c' * 32))
        monkeypatch.setattr(context.items, '_native_game_link_async', AsyncMock(return_value=link))

        async def call(function, *args, process=None, **kwargs):
            return function(*args, **kwargs)

        monkeypatch.setattr(context.items, '_blocking_native_worker', lambda: SimpleNamespace(call=call))
        monkeypatch.setattr(sentinel_diagnostics, 'diagnostics_directory', lambda config: tmp_path)
        monkeypatch.setattr(bridge_config, 'STEAM_REMOTE_DIR', tmp_path / 'remote')
        save = bridge_config.STEAM_REMOTE_DIR / native_root / 'GAME-AUTOSAVE0/game_duration.dat'
        save.parent.mkdir(parents=True)
        save.write_bytes(b'interrupted save')
        diagnostic = tmp_path / '71-123.latest.json'

        def publish(status, verified=False, **changes):
            record = dict(schema='sentinel-startup-v1', pid=71, process_created='123',
                admission={'namespace_id': namespace},
                b_diagnostics={'stages': {'checkpoint_recovery': {
                    'sequence': 10 if status == 2 else 11, 'status': status,
                    'predicate': 'checkpoint_recovery_started' if status == 2 else 'previous_checkpoint_restored'}}},
                campaign=dict(resumed=verified, source_verified=verified,
                              parser_completed=verified, parser_result=0))
            diagnostic.write_text(json.dumps({**record, **changes}), encoding='utf-8')

        publish(2)
        return context, link, publish, save, diagnostic

    return create


@pytest.mark.parametrize('status,expected', [(1, 'recovering'), (2, 'recovering'), (4, 'blocked'), (5, 'blocked')])
def test_checkpoint_recovery_defers_bridge_save_readers_and_delivery(
        bridge, checkpoint_session, monkeypatch, caplog, status, expected):
    from doom_eap.runtime import bridge_items, bridge_saves

    async def _assert_deferred_recovery_readers(context, expected):
        assert not await context.items.runtime_effects_ready()
        assert not await context.items.authored_map_runtime_ready()
        assert await context.items._active_materialization_lease() is None
        assert not await context.items.process_pending_item_receipts('recovery')
        assert context._death_baseline_pending and context.save_observer.readiness.frozen
        assert context._checkpoint_recovery.state.value == expected
        assert context.items.context_support_report()['checkpoint_recovery']['state'] == expected
        assert context.death_observer.checkpoints == {}


    async def run():
        context, link, publish, _, _ = checkpoint_session()
        publish(status)
        probe = Mock(side_effect=AssertionError('save read during recovery'))
        monkeypatch.setattr(bridge_saves, 'probe_game_duration', probe)
        monkeypatch.setattr(bridge_saves, 'read_game_details_for_selection', probe)
        monkeypatch.setattr(bridge_saves, 'read_gameplay_save_evidence', lambda: None)
        history = Mock(side_effect=AssertionError('delivery during recovery'))
        monkeypatch.setattr(context.items, 'observe_received_item_history', history)
        for _ in range(2):
            await context.checks.prime_death_baseline()
        assert await context.saves.update_save_slot_lifecycle() is None
        assert await context.saves.observe_active_game_details() is None
        assert await context.checks.check_game_duration_death()
        await context.checks.check_game_details_death()
        await _assert_deferred_recovery_readers(context, expected)
        probe.assert_not_called()
        history.assert_not_called()
        link._run.assert_not_called()
        assert f'state={expected}' in caplog.text
        monkeypatch.setattr(bridge_saves, 'read_gameplay_save_evidence',
                            lambda: SimpleNamespace(state='gameplay', native_safe=True))
        transient = Mock()
        monkeypatch.setattr(bridge_items, 'read_transient_runtime', transient)
        context.save_observer.set_frozen(False)
        context.items._observe_transient_runtime()
        assert transient.call_args.args[2] is False
    asyncio.run(run())


def test_recovered_save_baseline_waits_for_verified_resume_and_is_taken_once(
        bridge, checkpoint_session, monkeypatch):
    from doom_eap.runtime import bridge_saves

    async def run():
        context, _, publish, save, _ = checkpoint_session()
        probe = Mock(side_effect=lambda path: {'checkpoint_death': path.read_bytes() == b'recovered save'})
        monkeypatch.setattr(bridge_saves, 'probe_game_duration', probe)
        await context.checks.prime_death_baseline()
        save.write_bytes(b'recovered save')
        publish(3)
        await context.checks.prime_death_baseline()
        assert context._checkpoint_recovery.state.value == 'resume_pending'
        assert context._death_baseline_pending
        probe.assert_not_called()
        publish(3, verified=True)
        await context.checks.prime_death_baseline()
        await context.checks.prime_death_baseline()
        assert not context._death_baseline_pending
        probe.assert_called_once_with(save.resolve())
        assert context.death_observer.checkpoints == {'GAME-AUTOSAVE0': True}
        selected = await context.saves.admitted_campaign_save()
        assert context.death_observer.observe_checkpoint({'checkpoint_death': True}, selected, 1, 'map') is None
        publish(2)
        assert await context.saves.admitted_campaign_save() is None
        assert context.death_observer.checkpoints == {}
        save.write_bytes(b'recovered clean save')
        publish(3, verified=True)
        await context.checks.prime_death_baseline()
        assert context.death_observer.checkpoints == {'GAME-AUTOSAVE0': False}
    asyncio.run(run())


@pytest.mark.parametrize('invalid', ['missing', 'malformed', 'namespace', 'process', 'oversized'])
def test_unavailable_recovery_diagnostics_block_baseline(bridge, checkpoint_session, monkeypatch, invalid):
    from doom_eap.runtime import bridge_saves

    async def run():
        context, _, publish, _, diagnostic = checkpoint_session()
        publish(3, verified=True)
        if invalid == 'missing':
            diagnostic.unlink()
        elif invalid == 'malformed':
            diagnostic.write_text('{')
        elif invalid == 'namespace':
            publish(3, verified=True, admission={'namespace_id': 'other-room'})
        elif invalid == 'process':
            publish(3, verified=True, process_created='124')
        else:
            diagnostic.write_bytes(b'x' * 131073)
        probe = Mock()
        monkeypatch.setattr(bridge_saves, 'probe_game_duration', probe)
        await context.checks.prime_death_baseline()
        assert context._death_baseline_pending
        assert context._checkpoint_recovery.state.value == 'blocked'
        probe.assert_not_called()
    asyncio.run(run())


def test_recovery_started_during_probe_discards_baseline_and_retries(bridge, checkpoint_session, monkeypatch):
    from doom_eap.runtime import bridge_saves

    async def run():
        context, _, publish, save, _ = checkpoint_session()
        publish(3, verified=True)

        def interrupted(path):
            publish(2)
            return {'checkpoint_death': False}

        monkeypatch.setattr(bridge_saves, 'probe_game_duration', interrupted)
        await context.checks.prime_death_baseline()
        assert context._death_baseline_pending
        assert context.death_observer.checkpoints == {}
        publish(3, verified=True)
        probe = Mock(return_value={'checkpoint_death': True})
        monkeypatch.setattr(bridge_saves, 'probe_game_duration', probe)
        await context.checks.prime_death_baseline()
        assert not context._death_baseline_pending
        probe.assert_called_once_with(save.resolve())
    asyncio.run(run())


@pytest.mark.parametrize('stopped', [False, True])
def test_death_baseline_requires_live_provider(bridge, checkpoint_session, monkeypatch, stopped):
    from doom_eap.runtime import bridge_saves

    async def run():
        context, link, publish, _, _ = checkpoint_session()
        publish(3, verified=True)
        if stopped:
            monkeypatch.setattr(context.items, '_native_game_link_async', AsyncMock(side_effect=RuntimeError('game stopped')))
        else:
            link._run.return_value['state'] = 'refused'
        probe = Mock()
        monkeypatch.setattr(bridge_saves, 'probe_game_duration', probe)
        await context.checks.prime_death_baseline()
        assert context._death_baseline_pending
        probe.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('mismatch', [None, 'pid', 'namespace', 'source_file'])
def test_recovered_gameplay_still_requires_matching_save_origin(
        bridge, checkpoint_session, monkeypatch, mismatch):
    from doom_eap.contracts.save_observation import GameplaySaveEvidence

    async def run():
        context, link, publish, save, _ = checkpoint_session()
        evidence = GameplaySaveEvidence('gameplay', 1, 'GAME-AUTOSAVE0', 'game/hub/hub', False, True,
            link.namespace, link._run.return_value['native_root'], link.pid, '123', 'c' * 32,
            str(save.with_name('game.details')))
        changes = {'pid': 72, 'namespace': 'other-room', 'source_file': str(save.parent.parent / 'game.details')}
        if mismatch:
            evidence = evidence._replace(**{mismatch: changes[mismatch]})
        monkeypatch.setattr(context.saves, 'has_authoritative_save_proof', lambda: True)
        publish(3)
        assert not await context.items.runtime_effects_ready(evidence)
        publish(3, verified=True)
        assert await context.items.runtime_effects_ready(evidence) == (mismatch is None)
    asyncio.run(run())


@pytest.mark.parametrize('location,expected', [(7770122, {'mission_complete'}), (7770100, set())])
def test_server_mission_completion_wakes_inventory_materialization(bridge, monkeypatch, location, expected):
    pending = set()
    context = SimpleNamespace(
        checked_locations=set(),
        _receive_owner_storage=lambda *args: None,
        publisher_dispatch=SimpleNamespace(observe_protocol=lambda observation: None),
        session_tasks=SimpleNamespace(start=lambda task: None),
        items=None,
        checks=None,
        saves=None
    )
    context.items = bridge.bridge_items.ItemRuntime(context)
    context.items._schedule_item_delivery = pending.add
    context.checks = bridge.bridge_checks.CheckRuntime(context)
    context.checks.snapshot_fast_travel_eligibility = lambda **kwargs: None
    context.checks.reconcile_checked_automap_cleanup = lambda trigger: None
    context.checks.reconcile_fast_travel_unlock = lambda trigger: None
    context.checks.evaluate_campaign_goal = lambda reason: None
    context.saves = bridge.bridge_saves.SaveRuntime(context)
    context.saves.check_mission_challenge_locations = lambda: None


    monkeypatch.setattr(bridge.bridge_checks.CheckRuntime, 'check_observation', lambda self: None)
    context._handle_checked_locations = bridge.DoomEternalContext._handle_checked_locations.__get__(context)
    bridge.DoomEternalContext._handle_package(context, 'RoomUpdate', {'checked_locations': [location]})
    assert pending == expected


def _assert_quarantined_publishers(files, tmp_path):
    assert not any(file.exists() for file in files)
    assert len(list((tmp_path / "ap_event_quarantine").rglob("*.txt"))) == len(files)


@pytest.mark.parametrize("old_key", [None, "room-A", "room-B"])
def test_map_publishers_are_bound_to_room_and_pending_same_room_survives(bridge, tmp_path, old_key):
    from doom_eap.runtime import bridge_checks

    context = SimpleNamespace(
        get_ap_state_key=lambda: "room-B",
        items=None,
        checks=None,
        saves=None
    )
    context.items = bridge.bridge_items.ItemRuntime(context)
    context.items._query_process_identity = AsyncMock(return_value="fixture-process")
    context.checks = bridge.bridge_checks.CheckRuntime(context)
    context.saves = bridge.bridge_saves.SaveRuntime(context)


    context.items.quarantine_unbound_physical_events = bridge.bridge_items.ItemRuntime.quarantine_unbound_physical_events.__get__(context.items)
    files = [tmp_path / name for name in bridge_checks.PUBLISHER_MAP_EVENT_FILENAMES]
    for file in files:
        file.write_bytes(b"pending publisher")
    if old_key:
        (tmp_path / "ap_event_session.json").write_text(json.dumps({"ap_state_key": old_key}))
    assert asyncio.run(context.items.check_and_update_event_session())
    if old_key == "room-B":
        assert all(file.read_bytes() == b"pending publisher" for file in files)
    else:
        _assert_quarantined_publishers(files, tmp_path)


def test_failed_quarantine_cannot_bind_old_events_to_new_room(bridge, tmp_path, monkeypatch):
    from doom_eap.runtime import bridge_checks

    file = tmp_path / next(iter(bridge_checks.PUBLISHER_MAP_EVENT_FILENAMES))
    file.write_bytes(b"old room")
    context = SimpleNamespace(
        get_ap_state_key=lambda: "room-B",
        items=None,
        checks=None,
        saves=None
    )
    context.items = bridge.bridge_items.ItemRuntime(context)
    context.items._query_process_identity = AsyncMock(return_value="fixture-process")
    context.checks = bridge.bridge_checks.CheckRuntime(context)
    context.saves = bridge.bridge_saves.SaveRuntime(context)


    context.items.quarantine_unbound_physical_events = bridge.bridge_items.ItemRuntime.quarantine_unbound_physical_events.__get__(context.items)
    monkeypatch.setattr(bridge_checks, "quarantine_event_file", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="could not be quarantined"):
        asyncio.run(context.items.check_and_update_event_session())
    assert not (tmp_path / "ap_event_session.json").exists()
    assert file.read_bytes() == b"old room"


def test_death_monitor_restarts_after_transport_failure_and_honors_shutdown(bridge, monkeypatch):
    from doom_eap.runtime import bridge_client

    async def run():
        events = []
        context = SimpleNamespace(
            exit_event=asyncio.Event(),
            items=None,
            checks=None,
            saves=None
        )
        context.items = bridge.bridge_items.ItemRuntime(context)
        context.checks = bridge.bridge_checks.CheckRuntime(context)
        context.saves = bridge.bridge_saves.SaveRuntime(context)


        async def monitor():
            events.append("poll")
            if len(events) == 1:
                raise OSError("connection interrupted")
            events.extend(("receive", "send"))
            context.exit_event.set()
        async def pause(_):
            return
        context.checks.death_monitor_loop = monitor
        monkeypatch.setattr(bridge_client.asyncio, "sleep", pause)
        await context.checks.death_monitor_supervisor()
        assert events == ["poll", "poll", "receive", "send"]
    asyncio.run(run())
