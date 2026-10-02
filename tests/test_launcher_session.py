from unittest.mock import Mock, patch
import json
from types import SimpleNamespace

import pytest

from doom_eap.launcher.launcher_session import APSessionOwner, _verify_new_campaign_receipts


def test_support_condump_uses_workflow_session_owner(tmp_path):
    import threading
    from doom_eap.launcher.launcher_controller import LauncherController

    controller = LauncherController.__new__(LauncherController)
    controller.config = {"save_games_dir": str(tmp_path)}
    controller.user_paths = SimpleNamespace(data_dir=tmp_path / "data")
    controller._condump_lock = threading.Lock()
    controller._condump_pending = None
    owner = Mock(observe=Mock(return_value={"ready": True, "pid": 7, "process_created": 22}))
    controller.workflow = SimpleNamespace(session_owner=owner)
    controller.read_native_health = Mock(return_value={"ready": True})
    source = tmp_path / "AP_SUPPORT_FILE.txt"
    controller.supervisor = SimpleNamespace(running=True, request_support_condump=lambda: source.write_bytes(b"fixture condump"))
    result = controller._request_support_condump()
    assert result["status"] == "available" and result["owned_capture"]
    from pathlib import Path
    assert Path(result["path"]).read_bytes() == b"fixture condump"
    assert source.read_bytes() == b"fixture condump"
    assert owner.observe.call_count >= 2 and controller._condump_pending is None


def test_same_world_restart_backs_up_and_preserves_other_rooms(tmp_path, monkeypatch):
    from pathlib import Path
    import zipfile
    from doom_eap.runtime.client_state_store import ClientStateStore
    from doom_eap.contracts.command_publication import queue_session_namespace
    from doom_eap.runtime.weapon_points import namespace_id

    snapshot = SimpleNamespace(seed_name="same-world", team=0, slot=1,
                               slot_data={"native_generation_fingerprint": "a" * 64})
    namespace = namespace_id(snapshot.seed_name, 0, 1, "a" * 64)
    key = "same-world:0:1:" + "a" * 64
    owner = APSessionOwner(tmp_path, tmp_path / "data", tmp_path / "state")
    config = {"doom_base_dir": str(tmp_path / "game/base"),
              "steam_remote_dir": str(tmp_path / "Steam/userdata/123/782330/remote"),
              "save_games_dir": str(tmp_path / "saved/base"),
              "client_state_file": str(tmp_path / "client_state.json")}
    sources = [Path(config["steam_remote_dir"]) / ("ap-" + namespace[:40]),
               Path(config["save_games_dir"]) / ("ap-" + namespace[:40]),
               owner.data_dir / "campaigns" / namespace]
    for source in sources:
        source.mkdir(parents=True)
        (source / "fixture").write_bytes(b"old campaign")
    old_backup = owner.data_dir / "campaigns" / "transport-backup-old"
    old_backup.mkdir()
    (old_backup / "transport.manifest").write_text(f"namespace={namespace}\n")
    queue = Path(config["doom_base_dir"]) / "ap_queue"
    queue.mkdir(parents=True)
    pending = queue / f"recv-{queue_session_namespace(key)}-0-item-1.processing"
    pending.write_bytes(b"old item must not reach the new save")
    goal = Path(config["doom_base_dir"]) / "ap_transition_goal.evt"
    goal.write_bytes(b"old goal")
    marker = Path(config["save_games_dir"]) / "ap_event_session.json"
    marker.write_text(json.dumps({"ap_state_key": key}))
    foreign = queue / "recv-ffffffffffffffff-1.cmd"
    foreign.write_bytes(b"other room")
    original = {"version": 2, "sessions": {key: {"processed_items": 13}, "other-world:0:1": {"processed_items": 7}}}
    state_file = Path(config["client_state_file"])
    state_file.write_text(json.dumps(original))
    monkeypatch.setattr(owner, "game_processes", lambda config: None)
    with pytest.raises(RuntimeError, match="confirmed closed"):
        owner.restart_campaign(snapshot, config)
    assert all(source.exists() for source in sources) and pending.exists()
    monkeypatch.setattr(owner, "game_processes", lambda config: ())
    commit = ClientStateStore.commit

    def fail_after_publish(store, state, **options):
        commit(store, state, **options)
        if options.get("reason") == "campaign_restart":
            raise OSError("publication fixture")

    with monkeypatch.context() as patching:
        patching.setattr(ClientStateStore, "commit", fail_after_publish)
        with pytest.raises(OSError, match="publication fixture"):
            owner.restart_campaign(snapshot, config)
    assert json.loads(state_file.read_text()) == original
    assert all((source / "fixture").read_bytes() == b"old campaign" for source in sources) and pending.exists()
    archive = owner.restart_campaign(snapshot, config)
    with zipfile.ZipFile(archive) as backup:
        assert backup.testzip() is None and json.loads(backup.read("client_state.json")) == original
        assert json.loads(backup.read("room.json"))["seed"] == "same-world"
        assert sum(backup.read(name) == b"old campaign" for name in backup.namelist()) == 3
    saved = json.loads(state_file.read_text())
    assert saved["sessions"][key]["processed_items"] == 0
    assert saved["sessions"]["other-world:0:1"] == original["sessions"]["other-world:0:1"]
    assert all(not source.exists() for source in sources) and not pending.exists()
    assert foreign.read_bytes() == b"other room"
    assert not goal.exists() and not marker.exists()
    assert not old_backup.exists() and owner.list_backups(snapshot) == []
    _verify_new_campaign_receipts(state_file, snapshot)


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
    snapshot = SimpleNamespace(seed_name="seed", team=0, slot=1, slot_data={
        "native_generation_fingerprint": "a" * 64,
        "starting_inventory": {}, "starting_weapon": "Combat Shotgun",
    })
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


def test_create_preserves_initial_receipts_and_pending_ownership(tmp_path):
    from doom_eap.runtime.item_reconciliation import (
        default_session_state, observe_received_items, project_receipt_history,
        record_processed_receipt,
    )
    snapshot = SimpleNamespace(seed_name="seed", team=0, slot=1, slot_data={
        "native_generation_fingerprint": "a" * 64,
        "starting_inventory": {"Frag Grenade": 1}, "starting_weapon": "Combat Shotgun",
    })
    state_file = tmp_path / "client_state.json"
    session = default_session_state()
    receipts = [SimpleNamespace(item=item, location=-2, player=0, flags=0)
                for item in (7770900, 7770011)]

    def write(boundary):
        session["processed_items"] = boundary
        project_receipt_history(session, receipts, boundary,
                                observe_received_items(receipts, boundary))
        state_file.write_text(json.dumps({"sessions": {"seed:0:1:" + "a" * 64: session}}))
        return state_file.read_bytes()

    for receipt in receipts:
        record_processed_receipt(session, receipt)
    before = write(2)
    _verify_new_campaign_receipts(state_file, snapshot)
    assert state_file.read_bytes() == before
    receipts.append(SimpleNamespace(item=7770903, location=7770300, player=1, flags=0))
    write(2)
    _verify_new_campaign_receipts(state_file, snapshot)
    record_processed_receipt(session, receipts[-1])
    write(3)
    with pytest.raises(RuntimeError, match="recover the campaign"):
        _verify_new_campaign_receipts(state_file, snapshot)
    receipts[:] = [receipts[0], receipts[0]]
    session["receipt_history"]["receipt_counts"] = {}
    for receipt in receipts:
        record_processed_receipt(session, receipt)
    write(2)
    with pytest.raises(RuntimeError, match="recover the campaign"):
        _verify_new_campaign_receipts(state_file, snapshot)
    receipts[:] = [SimpleNamespace(item=7770900, location=7770300, player=1, flags=0)]
    session["receipt_history"]["receipt_counts"] = {}
    record_processed_receipt(session, receipts[0])
    write(1)
    with pytest.raises(RuntimeError, match="recover the campaign"):
        _verify_new_campaign_receipts(state_file, snapshot)


def test_core_prepares_launcher_session():
    import ctypes
    import os
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path
    from doom_eap.launcher.launcher_platform import DependencyManager, install_meathook

    selected = os.environ.get("SENTINEL_CORE_RUNTIME")
    if os.name != "nt" or not selected:
        pytest.skip("requires an explicitly selected Windows Core distribution")
    runtime = Path(selected).resolve()
    with tempfile.TemporaryDirectory(prefix="ap-") as temporary:
        expanded = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetLongPathNameW(temporary, expanded, len(expanded))
        assert 0 < length < len(expanded)
        root = Path(expanded.value)
        game, data, state = root / "game", root / "data", root / "state"
        local = root / "Saved Games/id Software/DOOMEternal/base"
        remote = root / "Steam/userdata/1/782330/remote"
        for folder in (game / "base", data, state, local, remote):
            folder.mkdir(parents=True, exist_ok=True)
        (game / "DOOMEternalx64vk.exe").write_bytes(b"MZ-private-fixture")
        originals = {local / "GAME-AUTOSAVE0": b"local-original",
                     remote / "GAME-AUTOSAVE0": b"steam-original"}
        for path, content in originals.items():
            path.write_bytes(content)
        installed = install_meathook(game, DependencyManager(state / "deps"), state_dir=state,
                                    consent=lambda _: True, local_artifact=runtime / "distribution.json")
        assert installed.state == "installed"
        snapshot = SimpleNamespace(seed_name="sala-á", team=0, slot=1, slot_data={
            "native_generation_fingerprint": "a" * 64, "campaign_plan": {"difficulty": 1},
        })
        owner = APSessionOwner(root, data, state)
        config = {"game_root": str(game), "save_games_dir": str(local),
                  "client_state_file": str(data / "client_state.json"),
                  "steam_remote_dir": str(remote), "core_runtime_manifest": str(runtime / "distribution.json")}
        system = Path(os.environ["SystemRoot"]) / "System32"
        shutil.copyfile(system / "cmd.exe", root / "steam.exe")
        steam = subprocess.Popen([str(root / "steam.exe"), "/d", "/q", "/c", str(system / "more.com")],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            assert steam.poll() is None
            prepared = owner.prepare(snapshot, config)
            assert prepared["state"] == "prelaunch_ready"
            lease = (game / "sentinel-prelaunch.txt").read_bytes()
            assert owner.prepare(snapshot, config) == prepared
            assert (game / "sentinel-prelaunch.txt").read_bytes() == lease
            descriptor = (state / "ap-session-descriptor.txt").read_text(encoding="utf-8")
            assert "seed_hex=" + snapshot.seed_name.encode("utf-8").hex() + "\n" in descriptor
            for path, content in originals.items():
                assert path.read_bytes() == content
        finally:
            owner.retire()
            steam.communicate(b"", timeout=5)
        assert not (game / "sentinel-prelaunch.txt").exists()
