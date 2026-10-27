import hashlib
import json
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from doom_eap.launcher import launcher_platform as platform


@pytest.fixture
def room_files(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "detect_doom_processes", lambda: ())
    root = tmp_path / "game"
    (root / "Mods").mkdir(parents=True)
    (root / "base").mkdir()
    (root / "DOOMEternalx64vk.exe").write_bytes(b"fixture")
    source = tmp_path / "DoomEternalArchipelago-v0.7.1.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("seed_manifest.json", "{}")
    old = root / "Mods/DoomEternalArchipelago-v0.7.0.zip"
    old.write_bytes(b"previous room")
    legacy = root / "Mods/ap_mod.zip"
    legacy.write_bytes(b"legacy room")
    foreign = root / "Mods/other.zip"
    foreign.write_bytes(b"foreign mod")
    receipt = tmp_path / "ownership.json"
    receipt.write_text(json.dumps(dict(staged_mod=str(old), staged_sha256=hashlib.sha256(old.read_bytes()).hexdigest())))
    return root, source, old, legacy, foreign, receipt


@pytest.mark.parametrize("failure", ["copy", "hash", "backup", "publish", "receipt"])
def test_room_replacement_failure_preserves_previous(room_files, monkeypatch, failure):
    root, source, old, legacy, foreign, receipt = room_files
    before = {path: path.read_bytes() for path in (old, legacy, foreign, receipt)}
    publish = platform.publish_file

    def publish_or_fail(src, dst, *, operation):
        refused = {"backup": "room_mod_backup", "publish": "room_mod_stage", "receipt": "room_receipt_publish"}
        if operation == refused.get(failure) and (failure != "backup" or Path(src) == legacy):
            raise OSError("simulated publication failure")
        return publish(src, dst, operation=operation)

    monkeypatch.setattr(platform, "publish_file", publish_or_fail)
    if failure == "copy":
        monkeypatch.setattr(platform.shutil, "copy2", Mock(side_effect=OSError("simulated copy failure")))
    elif failure == "hash":
        monkeypatch.setattr(platform.shutil, "copy2", lambda src, dst: Path(dst).write_bytes(b"corrupt"))
    removed = []
    with pytest.raises((OSError, RuntimeError)):
        platform.stage_room_mod(source, root, receipt, manifest_hash="room", legacy_removal_sink=removed.append)
    assert {path: path.read_bytes() for path in before} == before
    assert set((root / "Mods").iterdir()) == {old, legacy, foreign}
    assert not removed


@pytest.mark.parametrize("same_name", [False, True])
def test_room_replacement_publishes_before_removal_events(room_files, same_name):
    root, source, old, legacy, foreign, receipt = room_files
    if same_name:
        source = source.rename(source.with_name(old.name))
    removed = []

    def on_remove(path):
        assert (root / "Mods" / source.name).read_bytes() == source.read_bytes()
        assert json.loads(receipt.read_text())["manifest_hash"] == "new-room"
        removed.append(path)

    staged = platform.stage_room_mod(source, root, receipt, manifest_hash="new-room", legacy_removal_sink=on_remove)
    assert staged.read_bytes() == source.read_bytes()
    assert set(removed) == {old, legacy}
    assert foreign.read_bytes() == b"foreign mod"
    assert set(staged.parent.iterdir()) == {staged, foreign}


@pytest.mark.parametrize("same_name", [False, True])
def test_room_replacement_preserves_modified_owned_zip(room_files, same_name):
    root, source, old, legacy, foreign, receipt = room_files
    old.write_bytes(b"user modified")
    if same_name:
        source = source.rename(source.with_name(old.name))
    before = {path: path.read_bytes() for path in (old, legacy, foreign, receipt)}
    with pytest.raises(RuntimeError, match="ownership hash"):
        platform.stage_room_mod(source, root, receipt, manifest_hash="new-room")
    assert {path: path.read_bytes() for path in before} == before
    assert set((root / "Mods").iterdir()) == {old, legacy, foreign}


def test_room_backup_cleanup_failure_keeps_published_mod(room_files, monkeypatch):
    root, source, old, legacy, foreign, receipt = room_files
    unlink = Path.unlink

    def unlink_except_backup(path, *args, **kwargs):
        if path.suffix == ".bak":
            raise PermissionError("backup locked")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_except_backup)
    removed = []
    staged = platform.stage_room_mod(source, root, receipt, manifest_hash="room", legacy_removal_sink=removed.append)
    assert staged.read_bytes() == source.read_bytes()
    assert set(removed) == {old, legacy}
    assert set(staged.parent.glob("*.zip")) == {staged, foreign}
    assert {path.read_bytes() for path in staged.parent.glob("*.bak")} == {b"previous room", b"legacy room"}


@pytest.mark.parametrize(("row", "expected"), [
    (("DOOMEternalx64vk.exe", "101"), "doometernalx64vk.exe"),
    (("102", "DOOMEternalx64v", r"C:\Program Files\DOOMEternalx64vk.exe +com_skipIntroVideo 1"), "doometernalx64vk.exe"),
    (("103", "MainThread", r'"C:\Game Files\DOOMEternalx64vk.exe"'), "doometernalx64vk.exe"),
    (("104", "wine64", 'wine64 "/games/DOOM Eternal/DOOMEternalx64vk.exe"'), "doometernalx64vk.exe"),
    (("105", "python3", 'python3 /opt/proton/proton waitforexitandrun "/games/DOOM Eternal/DOOMEternalx64vk.exe"'), None),
    (("106", "proton", '/opt/proton/proton run /games/DOOMEternalx64vk.exe'), None),
    (("107", "ap_client", '/opt/ap_client --config client.json'), "ap_client"),
    (("108", "python3", 'python3 /opt/proton/proton runinprefix /opt/APSessionOwner.exe --process-identity --expected-game-exe /games/DOOMEternalx64vk.exe'), None),
    (("109", "APSessionOwner", r'"Z:\client\APSessionOwner.exe" --process-identity --expected-game-exe "Z:\games\DOOMEternalx64vk.exe"'), None),
    (("110", "wine64", 'wine64 /opt/APSessionOwner.exe --expected-game-exe /games/DOOMEternalx64vk.exe'), None),
    (("111", "grep", 'grep DOOMEternalx64vk.exe'), None),
    (("112", "python3", 'python3 launcher.py --game /games/DOOMEternalx64vk.exe'), None),
])
def test_process_detection_distinguishes_launch_targets_from_arguments(row, expected):
    processes = platform.detect_doom_processes(process_list=(row,))
    if expected is None:
        assert not processes
    else:
        assert len(processes) == 1
        assert processes[0]["name"] == expected
        assert processes[0]["pid"] == (row[0] if row[0].isdigit() else row[1])
        assert processes[0]["process_name"] == (row[1] if row[0].isdigit() else row[0])
        assert processes[0]["matched_by"] in {"process_name", "launch_target"}


def game_files(root):
    # flock needs a persistent inode so concurrent launchers share the same lock.
    return {path: path.read_bytes() for path in root.rglob("*")
            if path.is_file() and path != root / ".sentinel-game-write.lock"}


@pytest.mark.parametrize("operation", ["core", "stage"])
def test_process_refusal_retains_blocking_identity(room_files, monkeypatch, operation):
    from doom_eap.launcher.launcher_integration import setup_failure_payload

    root, source, old, legacy, foreign, receipt = room_files
    processes = ({"name": "doometernalx64vk.exe", "pid": "123", "process_name": "DOOMEternalx64v",
                  "command_executable": "/games/DOOMEternalx64vk.exe", "matched_by": "process_name"},)
    monkeypatch.setattr(platform, "detect_doom_processes", lambda: processes)
    monkeypatch.setattr(platform, "probe_meathook", Mock(return_value=Mock(ok=False)))
    before = game_files(root)
    with pytest.raises(RuntimeError, match="PID 123") as refused:
        if operation == "core":
            platform.install_meathook(root, None, consent=lambda _: False)
        else:
            platform.stage_room_mod(source, root, receipt, manifest_hash="room")
    payload = setup_failure_payload(refused.value, phase="game_setup")
    assert payload["processes"] == processes
    assert payload["recovery_action"] == "repair_game_integration"
    assert game_files(root) == before


@pytest.mark.parametrize("query_failure", [OSError("tasklist unavailable"), subprocess.TimeoutExpired("tasklist", 3), 1])
@pytest.mark.parametrize("operation", ["core", "replace", "rollback", "stage", "adapter_stage", "windows", "linux", "campaign"])
def test_process_query_failure_refuses_mutation(room_files, monkeypatch, query_failure, operation):
    from doom_eap.launcher.campaign_resources import enable_campaign_resources
    from doom_eap.launcher.launcher_core_install import replace_pair, rollback_core

    root, source, old, legacy, foreign, receipt = room_files
    monkeypatch.undo()
    before = game_files(root)
    calls = {
        "core": lambda: platform.install_meathook(root, None, consent=lambda _: False),
        "replace": lambda: replace_pair(root, None, {}),
        "rollback": lambda: rollback_core(root, None),
        "stage": lambda: platform.stage_room_mod(source, root, receipt, manifest_hash="room"),
        "adapter_stage": lambda: platform._stage_mod(source, root),
        "windows": lambda: platform.WindowsModInjectorAdapter(None).run(root),
        "linux": lambda: platform.LinuxModManagerAdapter(None).run(root),
        "campaign": lambda: enable_campaign_resources(root, source),
    }
    result = subprocess.CompletedProcess(["tasklist"], 1, "", "access denied")
    with patch.object(platform.subprocess, "run", side_effect=query_failure if isinstance(query_failure, Exception) else None,
                      return_value=result) as query:
        with pytest.raises(platform.ProcessInspectionError, match="Could not inspect.*Cannot safely"):
            calls[operation]()
    assert query.call_args.args[0][0] in {"tasklist", "ps"}
    assert game_files(root) == before


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from doom_eap.launcher.launcher_ui import LauncherUI

    app = QApplication.instance() or QApplication([])
    schema = json.loads((Path(__file__).parents[1] / "data/options_schema.json").read_text(encoding="utf-8"))
    controller = Mock(config={}, options_schema=schema, bundle_dir=tmp_path,
                      application_dir=tmp_path, client_dir=tmp_path, last_setup_failure=None)
    controller.workflow.session_owner.status = {}
    with patch.object(LauncherUI, "_discover"):
        window = LauncherUI(controller)
    window.timer.stop()
    window.lifecycle_timer.stop()
    yield window
    window.close()
    app.processEvents()


def test_help_respects_doctor_attention(ui):
    ui._render_doctor_report({"ok": False, "diagnostics": [
        {"key": "ap_session", "status": "attention", "message": "Session refused"}]})
    assert ui.doctor_status.text() == "GAME SETUP NEEDS ATTENTION"
    assert "NEEDS ATTENTION" in ui.doctor_evidence.text()






def test_report_actions_open_copy_and_show_without_submission(ui, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QDialog, QPushButton
    from doom_eap.launcher import launcher_ui
    from doom_eap.launcher.launcher_reporting import report_text

    payload = {"title": "Update failed", "description": "Core update refused", "diagnostics": "Details\n" * 1000}
    bundle = tmp_path / "report.zip"
    bundle.write_bytes(b"local report")
    opened, shown = [], []
    monkeypatch.setattr(launcher_ui.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(launcher_ui.subprocess, "Popen", lambda command: shown.append(command))
    monkeypatch.setattr(launcher_ui.QDesktopServices, "openUrl", lambda url: shown.append(url) or True)

    def review(dialog):
        buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
        for action in ("COPY REPORT", "OPEN GITHUB ISSUE", "SHOW ZIP"):
            assert buttons[action].isEnabled()
            buttons[action].click()
        assert QApplication.clipboard().text() == report_text(payload)
        assert len(opened) == 1 and len(opened[0].encode()) < 2000
        assert len(shown) == 1
        assert "report.zip" in str(shown[0]) or str(tmp_path) in str(shown[0])
        dialog.accept()
        return 0

    monkeypatch.setattr(QDialog, "exec", review)
    ui._present_problem_report({"payload": payload, "message": "Review", "path": str(bundle), "submitted": False})
    ui.controller.request_report_submission.assert_not_called()
    ui.controller.save_problem_report.assert_called_once_with(payload)
