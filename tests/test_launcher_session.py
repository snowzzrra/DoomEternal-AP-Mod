from unittest.mock import Mock, patch
import json
from types import SimpleNamespace

import pytest

from doom_eap.launcher.launcher_session import APSessionOwner


def test_preparation_failure_is_diagnosable_campaign_error(tmp_path):
    from doom_eap.launcher.launcher_integration import classify_setup_failure, setup_failure_payload
    from doom_eap.launcher.launcher_doctor import _preparation_failure_summary
    owner = APSessionOwner(tmp_path, tmp_path, tmp_path)
    failure = RuntimeError("campaign contract fixture refusal")
    failure.stage, failure.operation = "copy_and_readback", "readback"
    failure.source_path = r"C:\Users\fixture-user\Saved Games\DOOM\game.details"
    failure.destination_path = r"C:\Users\fixture-user\backup\game.details"
    missing = FileNotFoundError(2, "fixture missing source", failure.source_path)
    missing.winerror, missing.filename2 = 3, failure.destination_path
    failure.cause_errno, failure.cause_winerror = missing.errno, missing.winerror
    failure.cause_filename, failure.cause_filename2 = missing.filename, missing.filename2
    def refuse(*args, **kwargs):
        try:
            raise missing
        except FileNotFoundError as cause:
            raise failure from cause
    with patch.object(owner, "_prepare", side_effect=refuse):
        with pytest.raises(RuntimeError) as refused:
            owner.prepare(None, {})
    assert refused.value is failure and classify_setup_failure(failure) == "campaign_session"
    assert setup_failure_payload(failure)["stage"] == "copy_and_readback"
    diagnostic = json.loads((tmp_path / "session_prepare_failure.json").read_text())
    expected = dict(stage=failure.stage, operation=failure.operation, source_path=failure.source_path,
        destination_path=failure.destination_path, errno=2, winerror=3,
        filename=missing.filename, filename2=missing.filename2)
    for key, value in expected.items():
        assert diagnostic[key] == value
    from datetime import datetime, timezone
    assert datetime.fromisoformat(diagnostic["at_utc"]).tzinfo == timezone.utc
    assert "FileNotFoundError" in diagnostic["traceback"] and "RuntimeError" in diagnostic["traceback"]
    assert repr(missing.filename) in diagnostic["traceback"]
    support = _preparation_failure_summary(tmp_path)
    assert support["source"] == "last_preparation_failure"
    assert "fixture-user" not in json.dumps(support)
    assert "game.details" in json.dumps(support)


def test_proton_supervisor_preserves_structured_preparation_failure(tmp_path):
    import io
    from doom_eap.launcher.launcher_session import _ProtonSessionOwner
    owner = _ProtonSessionOwner(tmp_path, tmp_path, tmp_path)
    failure = dict(failure_domain="campaign_session", stage="copy_and_readback", operation="readback",
        source_path=r"D:\fixture\remote\game.details", destination_path=r"E:\fixture\backup\game.details",
        errno=2, winerror=3, filename=r"D:\fixture\remote\game.details", filename2=None)
    owner.process = Mock(stdin=io.StringIO(), stdout=io.StringIO(json.dumps(dict(
        error="fixture missing source", can_close=False, failure=failure)) + "\n"))
    owner.process.poll.return_value = None
    with patch("doom_eap.launcher.launcher_session.selectors.DefaultSelector") as selector:
        selector.return_value.__enter__.return_value.select.return_value = [(None, None)]
        with pytest.raises(RuntimeError) as caught:
            owner.request("prepare")
    for key, value in failure.items():
        assert getattr(caught.value, key) == value


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


@pytest.mark.parametrize("receipt_origin", ("empty", "starting", "multiworld"))
def test_core_prepares_launcher_session(monkeypatch, receipt_origin):
    import ctypes
    import os
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path
    from doom_eap.launcher.launcher_platform import DependencyManager, detect_doom_processes, install_meathook

    selected = os.environ.get("SENTINEL_CORE_RUNTIME")
    if os.name != "nt" or not selected:
        pytest.skip("requires an explicitly selected Windows Core distribution")
    if detect_doom_processes():
        pytest.skip("requires DOOM closed for the distribution's vanilla protection helper")
    monkeypatch.setattr("doom_eap.launcher.launcher_session.windows_game_processes", lambda: ())
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
            "starting_inventory": {"Frag Grenade": 1}, "starting_weapon": "Combat Shotgun",
        })
        owner = APSessionOwner(root, data, state)
        config = {"game_root": str(game), "save_games_dir": str(local),
                  "client_state_file": str(data / "client_state.json"),
                  "steam_remote_dir": str(remote), "core_runtime_manifest": str(runtime / "distribution.json")}
        from doom_eap.runtime.item_reconciliation import (
            default_session_state, observe_received_items, project_receipt_history, record_processed_receipt,
        )
        receipts = [] if receipt_origin == "empty" else [
            SimpleNamespace(item=item, location=-2, player=0, flags=0) for item in (7770900, 7770011)
        ]
        if receipt_origin == "multiworld":
            receipts += [SimpleNamespace(item=item, location=7770300 + i, player=2, flags=0)
                         for i, item in enumerate((7770904, 7770025, 7770903))]
        session = default_session_state()
        boundary = len(receipts) - 1 if receipt_origin == "multiworld" else len(receipts)
        for receipt in receipts[:boundary]:
            record_processed_receipt(session, receipt)
        session["processed_items"] = boundary
        project_receipt_history(session, receipts, boundary, observe_received_items(receipts, boundary))
        client_state = Path(config["client_state_file"])
        client_state.write_text(json.dumps({"version": 2, "sessions": {
            "sala-á:0:1:" + "a" * 64: session, "other-room:0:1": default_session_state(),
        }}), encoding="utf-8")
        receipt_bytes = client_state.read_bytes()
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
            assert client_state.read_bytes() == receipt_bytes
            assert (game / "sentinel-prelaunch.txt").read_bytes() == lease
            descriptor = (state / "ap-session-descriptor.txt").read_text(encoding="utf-8")
            assert "seed_hex=" + snapshot.seed_name.encode("utf-8").hex() + "\n" in descriptor
            for path, content in originals.items():
                assert path.read_bytes() == content
            from doom_eap.launcher.launcher_session import _campaign_native_root
            first = _campaign_native_root(data / "campaigns", owner.namespace)
            namespace = owner.namespace
            assert first != "ap-" + namespace[:40]
            owner.retire()
            # a menu-only start can leave just the marker before campaign creation
            native = remote / first
            native.mkdir()
            (native / f"sentinel-owner-{namespace}.txt").write_text(
                f"sentinel-native-session-v1\nnamespace_id={namespace}\nseed_hex={snapshot.seed_name.encode('utf-8').hex()}\n"
                f"team=0\nslot=1\ngeneration_fingerprint={'a' * 64}\nprovenance=synthetic-fixture\n", encoding="utf-8", newline="\n")
            assert owner.prepare(snapshot, config)["intent"] == "create"
            assert client_state.read_bytes() == receipt_bytes
            assert _campaign_native_root(data / "campaigns", namespace) == first
            owner.retire()
            contract = data / "campaigns" / namespace / "campaign.contract"
            checkpoint = contract.with_name("campaign.checkpoint")
            for records in ((contract,), (checkpoint,), (contract, checkpoint)):
                for record in records:
                    record.write_bytes(b"existing campaign metadata")
                refusal = "identity or immutable options differ" if len(records) == 2 else "metadata and native saves disagree"
                with pytest.raises(RuntimeError, match=refusal):
                    owner.prepare(snapshot, config)
                assert client_state.read_bytes() == receipt_bytes
                for record in records:
                    assert record.read_bytes() == b"existing campaign metadata"
                    record.unlink()
            native_save = native / "GAME-AUTOSAVE0"
            native_save.mkdir()
            with pytest.raises(RuntimeError, match="metadata and native saves disagree"):
                owner.prepare(snapshot, config)
            assert native_save.is_dir() and client_state.read_bytes() == receipt_bytes
            native_save.rmdir()
            orphan = native / "save.fixture"
            orphan.write_bytes(b"retained native payload")
            contract.write_text(
                f"sentinel-campaign-v2\nnamespace={namespace}\ngeneration={'a' * 64}\n"
                "provenance=synthetic-fixture\ncampaign=unified\nstarting_stage=hub\ndifficulty=1\nslot=AUTOSAVE0\n",
                encoding="utf-8", newline="\n",
            )
            checkpoint.write_bytes(b"retained checkpoint")
            with pytest.raises(RuntimeError, match="native campaign is missing"):
                owner.prepare(snapshot, config)
            assert orphan.read_bytes() == b"retained native payload"
            assert checkpoint.read_bytes() == b"retained checkpoint"
            assert client_state.read_bytes() == receipt_bytes
            for retained in (orphan, contract, checkpoint):
                retained.unlink()
            config["doom_base_dir"] = str(game / "base")
            archive = owner.restart_campaign(snapshot, config)
            assert archive.is_file() and steam.poll() is None
            assert owner.prepare(snapshot, config)["intent"] == "create"
            assert owner.namespace == namespace and _campaign_native_root(data / "campaigns", namespace) != first
            assert steam.poll() is None
        finally:
            owner.retire()
            steam.communicate(b"", timeout=5)
        assert not (game / "sentinel-prelaunch.txt").exists()
