import json
import os
from unittest.mock import Mock, patch

import pytest

from doom_eap.launcher.launcher_session import APSessionOwner


@pytest.fixture
def owner(tmp_path):
    value = APSessionOwner(tmp_path, tmp_path / 'data', tmp_path / 'state')
    value._handle = 1
    value._game_exe = tmp_path / 'game/DOOMEternalx64vk.exe'
    value._game_exe.parent.mkdir()
    value._game_exe.write_bytes(b'isolated executable fixture')
    value._runtime = tmp_path / 'runtime'
    value.namespace, value._build = 'room-fixture', 'build-fixture'
    value._probe = Mock(return_value=dict(state='admitted', accepting_requests=True,
        namespace_id=value.namespace, build_id=value._build, target_pid=123, server_pid=123,
        process_created='456', instance_id='instance-fixture'))
    return value


def process(path):
    return dict(pid=123, created=456, path=str(path))


def test_matching_file_alias_keeps_full_admission_checks(owner, tmp_path):
    alias = tmp_path / 'alias.exe'
    os.link(owner._game_exe, alias)
    assert owner.observe((process(alias),))['state'] == 'admitted'
    owner._probe.assert_called_once()
    assert owner._process == (123, 456) and owner._instance == 'instance-fixture'


@pytest.mark.parametrize('case', ['copy', 'missing', 'multiple', 'unknown'])
def test_unqualified_process_retains_diagnostic_without_querying_core(owner, tmp_path, case):
    path = tmp_path / 'other.exe'
    path.write_bytes(owner._game_exe.read_bytes())
    rows = (process(path),)
    if case == 'missing':
        path.unlink()
    elif case == 'multiple':
        rows = (process(owner._game_exe), dict(pid=789, created=987, path=str(owner._game_exe)))
    elif case == 'unknown':
        rows = None
    with patch('doom_eap.launcher.launcher_session.subprocess.run') as run:
        assert not owner.observe(rows)['ready']
        owner._probe.assert_not_called()
        run.assert_not_called()
    diagnostic = json.loads((owner.state_dir / 'session_admission_failure.json').read_text())
    assert diagnostic['expected_game_path'] == str(owner._game_exe)
    assert diagnostic['processes'] == (list(rows) if rows is not None else None)
    assert diagnostic['queries'] == {}


@pytest.mark.parametrize(('field', 'value'), [
    ('state', 'refused'), ('accepting_requests', False), ('namespace_id', 'other-room'),
    ('build_id', 'other-build'), ('target_pid', 999), ('server_pid', 999),
    ('process_created', '999'), ('instance_id', None), ('instance_id', 'other-instance'),
])
def test_alias_cannot_bypass_core_identity(owner, tmp_path, field, value):
    alias = tmp_path / 'alias.exe'
    os.link(owner._game_exe, alias)
    owner._instance = 'instance-fixture'
    owner._probe.return_value[field] = value
    with patch.object(owner, '_capture_admission_failure'):
        assert owner.observe((process(alias),))['state'] == 'admission_refused'
        assert not owner.status['ready']


@pytest.mark.parametrize('identity', [(999, 456), (123, 999)])
def test_process_replacement_requires_new_session(owner, identity):
    owner._process = identity
    assert owner.observe((process(owner._game_exe),))['state'] == 'process_changed'
    owner._probe.assert_not_called()
    diagnostic = json.loads((owner.state_dir / 'session_admission_failure.json').read_text())
    assert diagnostic['expected_process'] == list(identity)
    assert diagnostic['state'] == 'process_changed'


def test_exact_path_preserves_existing_windows_behavior(owner):
    assert owner.observe((process(owner._game_exe),))['state'] == 'admitted'


def test_process_diagnostics_are_bounded(owner):
    rows = (process(owner._game_exe), process(owner._game_exe))
    with patch('doom_eap.launcher.launcher_session.time.monotonic', return_value=1):
        for _ in range(20):
            owner.observe(rows)
    assert owner._admission_capture_attempts == 1
