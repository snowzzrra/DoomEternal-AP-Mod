from unittest.mock import Mock, patch
import json
from types import SimpleNamespace

import pytest

from doom_eap.launcher.launcher_session import APSessionOwner, _verify_new_campaign_receipts


def test_admission_identity_and_unknown_exit_preserve_owner(tmp_path):
    owner = APSessionOwner(tmp_path, tmp_path / "data", tmp_path / "state")
    owner._handle = 123
    owner._api = Mock()
    owner._game_exe = tmp_path / "DOOMEternalx64vk.exe"
    owner.namespace = "a" * 64
    owner._build = "b" * 64
    owner._control = tmp_path / "sentinel-prelaunch.txt"
    owner._control_bytes = b"owned test control"
    owner._control.write_bytes(owner._control_bytes)
    owner._probe = Mock(return_value={
        "state": "admitted", "accepting_requests": True, "namespace_id": owner.namespace,
        "build_id": owner._build, "target_pid": 71, "server_pid": 71,
        "process_created": "9007199254740993", "instance_id": 15,
    })
    process = ({"pid": 71, "created": 9007199254740993, "path": str(owner._game_exe)},)
    assert owner.observe(process)["ready"] is True
    assert owner.observe(None)["state"] == "process_unknown"
    with patch("doom_eap.launcher.launcher_session.windows_game_processes", return_value=None):
        assert owner.can_close() is False
        with pytest.raises(RuntimeError):
            owner.retire()
    owner._api.CloseHandle.assert_not_called()
    owner._probe.return_value["namespace_id"] = "c" * 64
    assert owner.observe(process)["state"] == "admission_refused"
    owner._probe.return_value["namespace_id"] = owner.namespace
    owner._probe.return_value["instance_id"] = 16
    assert owner.observe(process)["state"] == "admission_refused"
    changed = ({**process[0], "created": process[0]["created"] + 1},)
    assert owner.observe(changed)["state"] == "process_changed"
    assert owner.observe(())["state"] == "game_exited"
    owner._api.CloseHandle.assert_called_once_with(123)
    assert not owner._control_bytes or not (tmp_path / "sentinel-prelaunch.txt").exists()


def test_create_refuses_receipts_without_native_save(tmp_path):
    snapshot = SimpleNamespace(seed_name="seed", team=0, slot=1)
    state_file = tmp_path / "client_state.json"
    session = {"processed_items": 0, "receipt_history": {"receipt_counts": {"item": 1}}}
    state_file.write_text(json.dumps({"sessions": {"seed:0:1:generation": session}}))
    with pytest.raises(RuntimeError, match="recover the campaign"):
        _verify_new_campaign_receipts(state_file, snapshot)
    session["receipt_history"] = {"processed_boundary": 0, "highest_observed_index": -1}
    state_file.write_text(json.dumps({"sessions": {"seed:0:1:generation": session}}))
    _verify_new_campaign_receipts(state_file, snapshot)
    session["processed_items"] = "0"
    state_file.write_text(json.dumps({"sessions": {"seed:0:1:generation": session}}))
    with pytest.raises(RuntimeError, match="ambiguous"):
        _verify_new_campaign_receipts(state_file, snapshot)
