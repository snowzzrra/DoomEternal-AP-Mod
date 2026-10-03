import json
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from doom_eap.launcher.launcher_session import APSessionOwner


@pytest.mark.skipif(os.name != 'nt', reason='Native backup helper requires Windows')
def test_connected_bridge_does_not_mutate_stopped_game_backup(tmp_path, monkeypatch):
    import importlib
    import asyncio

    root = Path(__file__).resolve().parents[1]
    helper_path = root.parent / 'Sentinel-Core/tools/prepare_vanilla_backup.py'
    if not helper_path.is_file() or not (root.parent / 'Archipelago/CommonClient.py').is_file():
        pytest.skip('Source Archipelago and Core backup helper are required')
    steam = tmp_path / 'Steam/userdata/160032537/782330'
    local = tmp_path / 'local'
    (steam / 'remote').mkdir(parents=True)
    local.mkdir()
    (steam / 'remote/GAME-AUTOSAVE0').write_bytes(b'vanilla')
    (local / 'ap_event_session.json').write_text('{"ap_state_key": "old-room"}')
    (local / 'ap_event_7771000.txt').write_text('previous event')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'doom_base_dir': str(tmp_path / 'game/base'),
        'save_games_dir': str(local), 'steam_remote_dir': str(steam / 'remote'),
        'client_state_file': str(tmp_path / 'state/client.json'),
        'bridge_log_dir': str(tmp_path / 'logs')}))
    monkeypatch.setenv('DOOM_AP_CONFIG_FILE', str(config))
    monkeypatch.setenv('DOOM_AP_APPLICATION_DIR', str(tmp_path))
    monkeypatch.syspath_prepend(str(root.parent / 'Archipelago'))
    monkeypatch.syspath_prepend(str(root / 'packaging/standalone_runtime'))
    bridge = importlib.import_module('doom_eap.runtime.bridge_client')
    monkeypatch.setattr(bridge, 'doom_process_identity', lambda: None)
    context = SimpleNamespace(get_ap_state_key=lambda: 'new-room',
        quarantine_unbound_physical_events=Mock(side_effect=AssertionError('quarantine while stopped')))
    context.check_and_update_event_session = lambda: bridge.DoomEternalContext.check_and_update_event_session(context)
    spec = importlib.util.spec_from_file_location('backup_review', helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    monkeypatch.setattr(helper, 'require_stopped', lambda: None)
    originals = {str(path): path.read_bytes() for source in (steam, local) for path in source.rglob('*') if path.is_file()}
    copy_file = helper.copy_file

    def connected_poll(*args, **kwargs):
        copied = copy_file(*args, **kwargs)
        assert context.check_and_update_event_session() is False
        asyncio.run(bridge.DoomEternalContext.flush_check_event_files(context))
        return copied

    monkeypatch.setattr(helper, 'copy_file', connected_poll)
    result = helper.protect(SimpleNamespace(action='prepare', steam_account='160032537',
        steam_app_root=str(steam), local_provider_root=str(local), backup_directory=str(tmp_path / 'backup'),
        ap_root=str(tmp_path / 'campaign'), uninstall_root=[str(tmp_path / 'uninstall')],
        run_backup_parent=None, reference_directory=None))
    assert result['result'] == 'protective_backup_ready'
    assert originals == {str(path): path.read_bytes() for source in (steam, local) for path in source.rglob('*') if path.is_file()}
    for name in ('tracker_loop', 'death_monitor_loop'):
        stopped = SimpleNamespace(exit_event=asyncio.Event(),
            reset_transient_effects=Mock(), invalidate_map_identity=Mock(),
            check_rpc_autopause=Mock(), queue_received_deathlink=Mock())

        async def stop_after_poll(_delay):
            stopped.exit_event.set()

        with monkeypatch.context() as loop_patch:
            loop_patch.setattr(bridge.asyncio, 'sleep', stop_after_poll)
            asyncio.run(getattr(bridge.DoomEternalContext, name)(stopped))
    assert originals == {str(path): path.read_bytes() for source in (steam, local) for path in source.rglob('*') if path.is_file()}
    monkeypatch.setattr(bridge, 'doom_process_identity', lambda: 'windows:42:99')
    context.quarantine_unbound_physical_events = Mock()
    assert context.check_and_update_event_session() is True
    context.quarantine_unbound_physical_events.assert_called_once_with(old_state_key='old-room', new_state_key='new-room', reason='session_changed')
    assert json.loads((local / 'ap_event_session.json').read_text())['ap_state_key'] == 'new-room'


def test_completed_backup_still_requires_matching_native_owner_and_archive(tmp_path):
    owner=APSessionOwner(tmp_path,tmp_path/'data',tmp_path/'state')
    owner.namespace='a'*64
    owner._runtime=tmp_path
    scope={'ready':True,'pid':77,'process_created':99,'admission':{'instance_id':'c'*32}}
    owner.observe=Mock(return_value=scope)
    request={'operation':'native_backup_request','pid':77,'process_created':'99',
             'instance_id':'c'*32,'namespace':owner.namespace}
    result={'operation':'native_backup','state':'complete','basename':'transport-backup-fixture'}
    run=SimpleNamespace(returncode=0,stdout=('\n'.join(json.dumps(value) for value in (request,result))).encode())
    owner._probe=Mock(return_value={'archive_verified':True,'namespace':owner.namespace,'quarantine':False,'owner_bound':False})
    with patch('doom_eap.launcher.launcher_session.os.name','nt'), patch('doom_eap.launcher.launcher_session.subprocess.run',return_value=run):
        with pytest.raises(RuntimeError,match='native owner proof'):
            owner.create_backup()
        owner._probe.return_value['owner_bound']=True
        assert owner.create_backup()['basename']==result['basename']
        request['instance_id']='d'*32
        run.stdout=('\n'.join(json.dumps(value) for value in (request,result))).encode()
        with pytest.raises(RuntimeError,match='not confirmed'):
            owner.create_backup()
