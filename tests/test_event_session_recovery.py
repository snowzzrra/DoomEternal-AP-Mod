import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "doom_base_dir": str(tmp_path / "base"), "save_games_dir": str(tmp_path),
        "client_state_file": str(tmp_path / "state.json"), "bridge_log_path": str(tmp_path / "bridge.log"),
    }))
    monkeypatch.setenv("DOOM_AP_CONFIG_FILE", str(config))
    monkeypatch.setenv("DOOM_AP_APPLICATION_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(root.parent / "Archipelago"))
    monkeypatch.syspath_prepend(str(root / "packaging/standalone_runtime"))
    module = importlib.import_module("doom_eap.runtime.bridge_client")
    monkeypatch.setattr(module, "INV_DUMP_DIR", str(tmp_path))
    monkeypatch.setattr(module, "doom_process_identity", lambda: "fixture-process")
    return module


@pytest.mark.parametrize('baseline,stale', [(None, False), (False, False), (True, False), (False, True)])
def test_death_baseline_precedes_gameplay_and_rejects_a_changed_session(bridge, tmp_path, monkeypatch, baseline, stale):
    import logging

    from doom_eap.runtime.death_observation import DeathObservation
    from doom_eap.runtime.weapon_points import namespace_id

    namespace = namespace_id('seed-A', 0, 1, 'a' * 64)
    native_root = 'ap-' + 'b' * 40
    remote = tmp_path / 'remote'
    save = remote / native_root / 'GAME-AUTOSAVE0' / 'game_duration.dat'
    save.parent.mkdir(parents=True)
    if baseline is not None:
        save.write_bytes(b'isolated save fixture')
    monkeypatch.setattr(bridge, 'STEAM_REMOTE_DIR', remote)
    monkeypatch.setattr(bridge, 'probe_game_duration', lambda path: {'checkpoint_death': baseline})
    owner = DeathObservation(logging.getLogger(__name__))
    owner.bind(namespace)
    context = SimpleNamespace(_death_baseline_pending=True, room_seed_name='seed-A', team=0, slot=1,
        _connected_slot_data={'native_generation_fingerprint': 'a' * 64}, state_key='room-A',
        receipt_session=SimpleNamespace(capture=lambda key: 'token', is_current=lambda *args: not stale),
        _admitted_campaign_root=AsyncMock(return_value=native_root),
        admitted_campaign_save=AsyncMock(side_effect=lambda: bridge.save_files.admitted_ap_save(remote, native_root)),
        death_observer=owner)
    asyncio.run(bridge.DoomEternalContext.prime_death_baseline(context))
    assert context._death_baseline_pending == stale
    if not stale:
        from doom_eap.contracts.save_observation import PrimarySaveSelection
        event = owner.observe_checkpoint({'checkpoint_death': True},
            PrimarySaveSelection('GAME-AUTOSAVE0', save, 10), 1, 'map')
        assert bool(event) == (baseline is not True)


@pytest.fixture
def checkpoint_session(bridge, tmp_path, monkeypatch):
    from doom_eap.launcher import sentinel_diagnostics
    from doom_eap.runtime.weapon_points import SentinelWeaponPoints

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
        monkeypatch.setattr(context, '_native_game_link_async', AsyncMock(return_value=link))

        async def call(function, *args, process=None, **kwargs):
            return function(*args, **kwargs)

        monkeypatch.setattr(context, '_blocking_native_worker', lambda: SimpleNamespace(call=call))
        monkeypatch.setattr(sentinel_diagnostics, 'diagnostics_directory', lambda config: tmp_path)
        monkeypatch.setattr(bridge, 'STEAM_REMOTE_DIR', tmp_path / 'remote')
        save = bridge.STEAM_REMOTE_DIR / native_root / 'GAME-AUTOSAVE0/game_duration.dat'
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
    async def run():
        context, link, publish, _, _ = checkpoint_session()
        publish(status)
        probe = Mock(side_effect=AssertionError('save read during recovery'))
        monkeypatch.setattr(bridge, 'probe_game_duration', probe)
        monkeypatch.setattr(bridge, 'read_game_details_for_selection', probe)
        monkeypatch.setattr(bridge, 'read_gameplay_save_evidence', lambda: None)
        history = Mock(side_effect=AssertionError('delivery during recovery'))
        monkeypatch.setattr(context, 'observe_received_item_history', history)
        for _ in range(2):
            await context.prime_death_baseline()
        assert await context.update_save_slot_lifecycle() is None
        assert await context.observe_active_game_details() is None
        assert await context.check_game_duration_death()
        await context.check_game_details_death()
        assert not await context.runtime_effects_ready()
        assert not await context.authored_map_runtime_ready()
        assert await context._active_materialization_lease() is None
        assert not await context.process_pending_item_receipts('recovery')
        assert context._death_baseline_pending and context.runtime_observers_frozen
        assert context._checkpoint_recovery.state.value == expected
        assert context.context_support_report()['checkpoint_recovery']['state'] == expected
        assert context.death_observer.checkpoints == {}
        probe.assert_not_called()
        history.assert_not_called()
        link._run.assert_not_called()
        assert f'state={expected}' in caplog.text
        monkeypatch.setattr(bridge, 'read_gameplay_save_evidence',
                            lambda: SimpleNamespace(state='gameplay', native_safe=True))
        transient = Mock()
        monkeypatch.setattr(bridge, 'read_transient_runtime', transient)
        context.save_observer.set_frozen(False)
        context._observe_transient_runtime()
        assert transient.call_args.args[2] is False
    asyncio.run(run())


def test_recovered_save_baseline_waits_for_verified_resume_and_is_taken_once(
        bridge, checkpoint_session, monkeypatch):
    async def run():
        context, _, publish, save, _ = checkpoint_session()
        probe = Mock(side_effect=lambda path: {'checkpoint_death': path.read_bytes() == b'recovered save'})
        monkeypatch.setattr(bridge, 'probe_game_duration', probe)
        await context.prime_death_baseline()
        save.write_bytes(b'recovered save')
        publish(3)
        await context.prime_death_baseline()
        assert context._checkpoint_recovery.state.value == 'resume_pending'
        assert context._death_baseline_pending
        probe.assert_not_called()
        publish(3, verified=True)
        await context.prime_death_baseline()
        await context.prime_death_baseline()
        assert not context._death_baseline_pending
        probe.assert_called_once_with(save.resolve())
        assert context.death_observer.checkpoints == {'GAME-AUTOSAVE0': True}
        selected = await context.admitted_campaign_save()
        assert context.death_observer.observe_checkpoint({'checkpoint_death': True}, selected, 1, 'map') is None
        publish(2)
        assert await context.admitted_campaign_save() is None
        assert context.death_observer.checkpoints == {}
        save.write_bytes(b'recovered clean save')
        publish(3, verified=True)
        await context.prime_death_baseline()
        assert context.death_observer.checkpoints == {'GAME-AUTOSAVE0': False}
    asyncio.run(run())


@pytest.mark.parametrize('invalid', ['missing', 'malformed', 'namespace', 'process', 'oversized'])
def test_unavailable_recovery_diagnostics_block_baseline(bridge, checkpoint_session, monkeypatch, invalid):
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
        monkeypatch.setattr(bridge, 'probe_game_duration', probe)
        await context.prime_death_baseline()
        assert context._death_baseline_pending
        assert context._checkpoint_recovery.state.value == 'blocked'
        probe.assert_not_called()
    asyncio.run(run())


def test_recovery_started_during_probe_discards_baseline_and_retries(bridge, checkpoint_session, monkeypatch):
    async def run():
        context, _, publish, save, _ = checkpoint_session()
        publish(3, verified=True)

        def interrupted(path):
            publish(2)
            return {'checkpoint_death': False}

        monkeypatch.setattr(bridge, 'probe_game_duration', interrupted)
        await context.prime_death_baseline()
        assert context._death_baseline_pending
        assert context.death_observer.checkpoints == {}
        publish(3, verified=True)
        probe = Mock(return_value={'checkpoint_death': True})
        monkeypatch.setattr(bridge, 'probe_game_duration', probe)
        await context.prime_death_baseline()
        assert not context._death_baseline_pending
        probe.assert_called_once_with(save.resolve())
    asyncio.run(run())


@pytest.mark.parametrize('stopped', [False, True])
def test_death_baseline_requires_live_provider(bridge, checkpoint_session, monkeypatch, stopped):
    async def run():
        context, link, publish, _, _ = checkpoint_session()
        publish(3, verified=True)
        if stopped:
            monkeypatch.setattr(context, '_native_game_link_async', AsyncMock(side_effect=RuntimeError('game stopped')))
        else:
            link._run.return_value['state'] = 'refused'
        probe = Mock()
        monkeypatch.setattr(bridge, 'probe_game_duration', probe)
        await context.prime_death_baseline()
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
        monkeypatch.setattr(context, 'has_authoritative_save_proof', lambda: True)
        publish(3)
        assert not await context.runtime_effects_ready(evidence)
        publish(3, verified=True)
        assert await context.runtime_effects_ready(evidence) == (mismatch is None)
    asyncio.run(run())


@pytest.mark.parametrize('location,expected', [(7770122, {'mission_complete'}), (7770100, set())])
def test_server_mission_completion_wakes_inventory_materialization(bridge, monkeypatch, location, expected):
    pending = set()
    context = SimpleNamespace(checked_locations=set(), _schedule_item_delivery=pending.add,
        _receive_owner_storage=lambda *args: None,
        snapshot_fast_travel_eligibility=lambda **kwargs: None,
        publisher_dispatch=SimpleNamespace(observe_protocol=lambda observation: None),
        reconcile_checked_automap_cleanup=lambda trigger: None,
        reconcile_fast_travel_unlock=lambda trigger: None,
        session_tasks=SimpleNamespace(start=lambda task: None),
        check_mission_challenge_locations=lambda: None, evaluate_campaign_goal=lambda reason: None)
    monkeypatch.setattr(bridge.DoomEternalContext, 'check_observation', lambda self: None)
    bridge.DoomEternalContext._handle_package(context, 'RoomUpdate', {'checked_locations': [location]})
    assert pending == expected


@pytest.mark.parametrize("old_key", [None, "room-A", "room-B"])
def test_map_publishers_are_bound_to_room_and_pending_same_room_survives(bridge, tmp_path, old_key):
    context = SimpleNamespace(get_ap_state_key=lambda: "room-B", _query_process_identity=AsyncMock(return_value="fixture-process"))
    context.quarantine_unbound_physical_events = bridge.DoomEternalContext.quarantine_unbound_physical_events.__get__(context)
    files = [tmp_path / name for name in bridge.PUBLISHER_MAP_EVENT_FILENAMES]
    for file in files:
        file.write_bytes(b"pending publisher")
    if old_key:
        (tmp_path / "ap_event_session.json").write_text(json.dumps({"ap_state_key": old_key}))
    assert asyncio.run(bridge.DoomEternalContext.check_and_update_event_session(context))
    if old_key == "room-B":
        assert all(file.read_bytes() == b"pending publisher" for file in files)
    else:
        assert not any(file.exists() for file in files)
        assert len(list((tmp_path / "ap_event_quarantine").rglob("*.txt"))) == len(files)


def test_failed_quarantine_cannot_bind_old_events_to_new_room(bridge, tmp_path, monkeypatch):
    file = tmp_path / next(iter(bridge.PUBLISHER_MAP_EVENT_FILENAMES))
    file.write_bytes(b"old room")
    context = SimpleNamespace(get_ap_state_key=lambda: "room-B", _query_process_identity=AsyncMock(return_value="fixture-process"))
    context.quarantine_unbound_physical_events = bridge.DoomEternalContext.quarantine_unbound_physical_events.__get__(context)
    monkeypatch.setattr(bridge, "quarantine_event_file", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="could not be quarantined"):
        asyncio.run(bridge.DoomEternalContext.check_and_update_event_session(context))
    assert not (tmp_path / "ap_event_session.json").exists()
    assert file.read_bytes() == b"old room"


def test_death_monitor_restarts_after_transport_failure_and_honors_shutdown(bridge, monkeypatch):
    async def run():
        events = []
        context = SimpleNamespace(exit_event=asyncio.Event())
        async def monitor():
            events.append("poll")
            if len(events) == 1:
                raise OSError("connection interrupted")
            events.extend(("receive", "send"))
            context.exit_event.set()
        async def pause(_):
            return
        context.death_monitor_loop = monitor
        monkeypatch.setattr(bridge.asyncio, "sleep", pause)
        await bridge.DoomEternalContext.death_monitor_supervisor(context)
        assert events == ["poll", "poll", "receive", "send"]
    asyncio.run(run())
