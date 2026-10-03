from unittest.mock import Mock, patch

import pytest

from doom_eap.launcher.launcher_session import _ProtonSessionOwner, _INSTALL_KEYS


def test_lost_supervisor_requires_positive_guest_exit(tmp_path):
    owner = _ProtonSessionOwner(tmp_path, tmp_path, tmp_path)
    owner.process = Mock()
    owner.process.poll.return_value = 1
    owner.process.stdin.closed = True
    owner._config_identity = (None,) * len(_INSTALL_KEYS)
    with patch("doom_eap.runtime.proton.windows_processes", return_value=None):
        assert owner.observe()["state"] == "supervisor_unavailable"
        assert not owner.closable
        with pytest.raises(RuntimeError, match="Confirm DOOM Eternal"):
            owner.retire()
    with patch("doom_eap.runtime.proton.windows_processes", return_value=()):
        assert owner.observe()["state"] == "game_exited"
        owner.retire()
    assert owner.process is None
