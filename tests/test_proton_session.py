from unittest.mock import Mock, patch
import io
import json

import pytest

from doom_eap.launcher.launcher_session import _ProtonSessionOwner, _INSTALL_KEYS


def test_lost_supervisor_requires_positive_guest_exit(tmp_path):
    owner = _ProtonSessionOwner(tmp_path, tmp_path, tmp_path)
    owner.process = Mock()
    owner.process.poll.return_value = 1
    owner.process.stdin.closed = True
    owner._config_identity = (None,) * len(_INSTALL_KEYS)
    owner._game_observed = True
    with patch("doom_eap.runtime.proton.windows_processes", return_value=None):
        assert owner.observe()["state"] == "supervisor_unavailable"
        assert not owner.closable
        with pytest.raises(RuntimeError, match="Confirm DOOM Eternal"):
            owner.retire()
    owner._next_process_query = 0
    with patch("doom_eap.runtime.proton.windows_processes", return_value=()):
        assert owner.observe()["state"] == "game_exited"
        owner.retire()
    assert owner.process is None


def test_failed_preparation_does_not_report_game_exit_or_repeat_helpers(tmp_path):
    owner = _ProtonSessionOwner(tmp_path, tmp_path, tmp_path)
    owner.process = Mock()
    owner.process.poll.return_value = 1
    owner.process.stdin.closed = True
    owner._config_identity = (None,) * len(_INSTALL_KEYS)
    with patch("doom_eap.runtime.proton.windows_processes", return_value=()) as query:
        assert owner.observe()["state"] == "supervisor_unavailable"
        assert owner.closable
        assert owner.observe()["state"] == "supervisor_unavailable"
        assert not owner.closable
        query.assert_called_once()


def test_proton_selection_precedes_campaign_routing(tmp_path):
    from doom_eap.runtime.proton import ProtonSelectionError, runtime
    from doom_eap.launcher.launcher_integration import setup_failure_payload
    prefix = tmp_path / "compatdata/782330"
    (prefix / "pfx").mkdir(parents=True)
    with pytest.raises(ProtonSelectionError) as refused:
        runtime({"game_root": str(tmp_path / "game"), "proton_compat_data_dir": str(prefix)})
    payload = setup_failure_payload(refused.value, phase="campaign_session")
    assert payload["recovery_action"] == "select_proton"
    assert payload["proton_reason"] == "missing_config_info"


def test_guest_identity_query_receives_the_selected_executable(tmp_path):
    from doom_eap.runtime.proton import windows_processes, windows_path
    configuration = {'game_root': str(tmp_path / 'game')}
    response = Mock(returncode=0, stdout=json.dumps({'processes': [dict(
        pid=123, created=456, path='S:\\steamapps\\common\\DOOMEternal\\DOOMEternalx64vk.exe',
        same_executable=True)]}).encode())
    with patch('doom_eap.runtime.proton.runtime', return_value=(['fixture-proton'], {})), \
         patch('doom_eap.runtime.proton.subprocess.run', return_value=response) as run:
        rows = windows_processes(configuration, tmp_path / 'owner.exe')
    assert rows[0]['same_executable'] is True
    command = run.call_args.args[0]
    assert command[command.index('--expected-game-exe') + 1] == windows_path(tmp_path / 'game/DOOMEternalx64vk.exe')
