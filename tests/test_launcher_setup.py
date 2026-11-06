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
    pytest.importorskip("PySide6.QtWidgets", reason="Qt tests require requirements-launcher.txt")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    from PySide6.QtWidgets import QApplication

    from doom_eap.launcher.launcher_ui import LauncherUI

    app = QApplication.instance() or QApplication([])
    data = tmp_path / "data"
    data.mkdir()
    (data / "start_inventory_catalog.json").write_bytes(
        (Path(__file__).parents[1] / "data/start_inventory_catalog.json").read_bytes()
    )
    schema = json.loads((Path(__file__).parents[1] / "data/options_schema.json").read_text(encoding="utf-8"))
    controller = Mock(config={"launcher_language": "en"}, options_schema=schema, bundle_dir=tmp_path,
                      application_dir=tmp_path, client_dir=tmp_path, last_setup_failure=None, items_room=None)
    controller.workflow.session_owner.status = {}
    with patch.object(LauncherUI, "_discover"):
        window = LauncherUI(controller)
    window.timer.stop()
    window.lifecycle_timer.stop()
    yield window
    window.close()
    app.processEvents()
    window.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_window_has_no_visible_children_outside_central_widget(ui):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QWidget

    ui.show()
    QApplication.processEvents()
    children = ui.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly)
    assert [widget for widget in children if widget.isVisible()] == [ui.centralWidget()]
    ui.pages.setCurrentIndex(2)
    assert [button.isChecked() for button in ui.nav_buttons] == [False, False, True, False]


def test_game_folder_controls_collapse_when_found_and_change_opens_them(ui):
    ui.game_root.setText("fixture/game")
    ui.saves_root.setText("fixture/save")
    ui._toggle_paths(force=False)
    assert ui.game_paths.isHidden()
    assert ui.game_root.isReadOnly() and ui.saves_root.isReadOnly()
    ui.change_paths.click()
    assert not ui.game_paths.isHidden()
    ui._select_recent_room(0)
    assert not ui.server.text()


def test_key_capture_warns_without_saving_conflicts_and_escape_cancels(ui):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    ui.ammo_refill_keybind.set_value("Y")
    ui.special_toggle_keybind.editor.click()
    QTest.keyClick(ui.special_toggle_keybind.editor, Qt.Key.Key_Y)
    assert not ui.settings_view.conflict_warning.isHidden()
    assert ui.special_toggle_keybind.editor.property("conflict")
    ui.controller.set_ap_keybinds.assert_not_called()
    ui.special_toggle_keybind.editor.click()
    QTest.keyClick(ui.special_toggle_keybind.editor, Qt.Key.Key_Escape)
    assert ui.special_toggle_keybind.value() == "Y"
    ui.special_toggle_keybind.editor.click()
    QTest.keyClick(ui.special_toggle_keybind.editor, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)
    assert ui.special_toggle_keybind.value() == "Y"
    QTest.keyClick(ui.special_toggle_keybind.editor, Qt.Key.Key_B)
    assert ui.settings_view.conflict_warning.isHidden()
    ui.controller.set_ap_keybinds.assert_called_once_with("Y", "B")


def test_help_respects_doctor_attention(ui):
    dispatch_window(ui, {"type": "doctor_report", "report": {"ok": False, "diagnostics": [
        {"key": "ap_session", "status": "attention", "message": "Session refused"}]}})
    assert "attention" in ui.help_view.doctor_status.text().casefold()
    assert "needs attention" in ui.help_view.doctor_evidence.text().casefold()
    assert ui.help_view.fix_button.isEnabled()


def test_help_game_action_opens_play_folder_controls(ui):
    from doom_eap.launcher.events import ActionId

    ui._show_page(4)
    assert not ui.help_view.fix_button.isEnabled()
    assert ui.help_view.fix_button.toolTip()
    ui.help_view.symptom_buttons[ActionId.CHOOSE_GAME_FOLDER].click()
    assert ui.pages.currentWidget() is ui.play_view
    assert not ui.game_paths.isHidden()
    assert ui.nav_buttons[0].isChecked()


def dispatch_window(ui, *events):
    import queue

    from PySide6.QtWidgets import QApplication
    ui.controller.events = queue.Queue()
    ui.controller.operation_generation = 3
    ui.controller.connection_attempt_id = 2
    ui.controller.item_history_status = None
    ui.controller.process_event.return_value = True
    for event in events:
        ui.controller.events.put(event)
    ui.timer.start(0)
    for _ in range(20):
        QApplication.processEvents()
        if ui.controller.events.empty():
            break
    ui.timer.stop()
    assert ui.controller.events.empty()


def test_window_keeps_installer_progress_inventory_result_and_session_navigation(ui):
    from PySide6.QtWidgets import QApplication

    from tests.room_fixture import current_room_fixture

    slot_data = current_room_fixture()
    dispatch_window(ui, {"type": "connected", "seed_name": "room", "team": 0, "slot": 1, "slot_data": slot_data},
                    {"type": "mod_staged"}, {"type": "injector_started"})
    assert "EternalModInjector" in ui.session_setup_detail.text()
    assert not ui.session_setup_detail.isHidden()
    now = [ui._setup_progress.clock()]
    ui._setup_progress.clock = lambda: now[0]
    dispatch_window(ui, {"type": "injector_finished", "state": "applied"})
    now[0] += 300
    ui.lifecycle_timer.start(0)
    QApplication.processEvents()
    ui.lifecycle_timer.stop()
    assert not ui.session_setup_help.isHidden()
    dispatch_window(ui, {"type": "connection_lost"})
    ui.hero_action.click()
    assert ui.pages.currentWidget() is ui.play_view
    ui.show()
    dispatch_window(ui, {"type": "inventory_resync", "status": "waiting"})
    ui._show_page(4)
    ui.help_view.symptom_toggles[2].click()
    waiting = ui.player_inventory.text()
    assert ui.player_inventory.isVisible() and not ui.resync_inventory_button.isEnabled()
    dispatch_window(ui, {"type": "inventory_resync", "status": "error"})
    assert ui.player_inventory.isVisible() and ui.player_inventory.text() != waiting
    assert ui.resync_inventory_button.isEnabled()


def test_window_requires_retry_instead_of_confirmation_for_unknown_injection(ui):
    dispatch_window(ui, {'type': 'manual_install_required', 'recovery_required': True,
                         'injector_state': 'unknown', 'message': 'Re-run installation.'})
    assert ui.session_manual_complete_action.isHidden()
    assert not ui.session_manual_retry_action.isHidden()
    dispatch_window(ui, {'type': 'manual_install_required', 'message': 'Confirm installation.'})
    assert not ui.session_manual_complete_action.isHidden()


def test_window_offers_preservation_for_interrupted_save(ui, monkeypatch):
    from PySide6.QtWidgets import QComboBox, QInputDialog

    shown = []
    def inspect_dialog(dialog):
        combo = dialog.findChild(QComboBox)
        shown.append((dialog.labelText(), combo.model().item(1).isEnabled()))
        return 0
    monkeypatch.setattr(QInputDialog, "exec", inspect_dialog)
    dispatch_window(ui, {"type": "ap_save_inspection", "inspection": {
        "seed": "fixture", "slot": 1, "status": "save_interrupted", "native_backup_available": False}})
    assert len(shown) == 1 and "interrupted" in shown[0][0] and shown[0][1]


def test_room_summary_displays_tracker_options(ui):
    ui.controller.item_history_status = None
    from tests.room_fixture import current_room_fixture

    slot_data = current_room_fixture()
    ui._render_room({"seed_name": "room", "team": 0, "slot": 1, "slot_data": slot_data})
    summary = ui.room_summary.text()
    assert "Complete the Full Saga" in summary and "Standard" in summary
    assert "<b>CAMPAIGN</b> &nbsp; Full Saga" in summary


def test_injector_failure_displays_exit_code_and_output(ui):
    with patch.object(ui, "_append_log") as log:
        ui._handle_event({"type": "injector_finished", "state": "failed", "returncode": 23,
                          "stdout": "injection output", "stderr": "injection error"})
    messages = [call.args[0] for call in log.call_args_list]
    assert any("exit code 23" in message for message in messages)
    assert "Injector stdout: injection output" in messages and "Injector stderr: injection error" in messages






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
        for action in ("reportCopy", "reportOpenIssue", "reportShowZip"):
            button = dialog.findChild(QPushButton, action)
            assert button.isEnabled()
            button.click()
        assert QApplication.clipboard().text() == report_text(payload)
        assert len(opened) == 1 and len(opened[0].encode()) < 2000
        assert len(shown) == 1
        assert "report.zip" in str(shown[0]) or str(tmp_path) in str(shown[0])
        dialog.accept()
        return 0

    monkeypatch.setattr(QDialog, "exec", review)
    dispatch_window(ui, {"type": "problem_report_ready", "payload": payload, "message": "Review", "path": str(bundle), "submitted": False})
    ui.controller.request_report_submission.assert_not_called()
    ui.controller.save_problem_report.assert_called_once_with(payload)


@pytest.mark.parametrize("confirmed", (False, True))
def test_manual_installation_requires_explicit_inline_confirmation_once(ui, confirmed):
    ui.controller.operation_generation = 10
    ui._confirm_manual_installation()
    ui.controller.confirm_manual_installation.assert_not_called()
    assert not ui.install_yes.isHidden() and not ui.install_no.isHidden()
    ui._answer_installation(confirmed)
    ui._answer_installation(True)
    if confirmed:
        ui.controller.confirm_manual_installation.assert_called_once_with(generation=10)
    else:
        ui.controller.confirm_manual_installation.assert_not_called()


def test_manual_installation_confirmation_cannot_cross_operations(ui):
    ui.controller.operation_generation = 10
    ui._confirm_manual_installation()
    ui.controller.operation_generation = 11
    ui._answer_installation(True)
    ui.controller.confirm_manual_installation.assert_not_called()


@pytest.mark.parametrize("confirmed", (False, True))
def test_download_consent_is_explicit_and_resolved_once(ui, confirmed):
    event = {"request_id": "download", "name": "fixture", "source": "https://fixture.invalid/tool"}
    ui._request_dependency_consent(event)
    ui._request_dependency_consent(event)
    ui.controller.resolve_consent.assert_not_called()
    assert not ui.help_view.download_prompt.isHidden()
    assert event["source"] in ui.help_view.download_details.text()
    ui.help_view.download_answered.emit(confirmed)
    ui.help_view.download_answered.emit(True)
    ui.controller.resolve_consent.assert_called_once_with("download", confirmed)


def test_pending_download_is_declined_when_room_controls_reset(ui):
    ui._request_dependency_consent({"request_id": "download", "name": "fixture"})
    ui._clear_pending_confirmations()
    ui.help_view.download_answered.emit(True)
    ui.controller.resolve_consent.assert_called_once_with("download", False)


def test_repair_preview_requires_a_visible_current_choice_and_applies_once(ui):
    actions = [{"action_id": "fixture", "title": "Fixture repair", "changes": ["fixture"], "rollback": "fixture"}]
    ui._present_repair_preview(actions, 10)
    ui.controller.request_repair.assert_not_called()
    ui.help_view.repair_preview.hide()
    ui.help_view.repair_confirmed.emit()
    ui.controller.request_repair.assert_not_called()
    ui._present_repair_preview(actions, 10)
    ui._present_repair_preview([], 10)
    ui.help_view.repair_confirmed.emit()
    ui.controller.request_repair.assert_not_called()
    ui._present_repair_preview(actions, 11)
    ui.help_view.repair_confirmed.emit()
    ui.help_view.repair_confirmed.emit()
    ui.controller.request_repair.assert_called_once_with("fixture", generation=11)


def test_installation_confirmation_is_inline_and_sent_once(ui):
    dispatch_window(ui, {"type": "installation_confirmation_required", "request_id": "install"})
    ui.show()
    assert ui.install_yes.isVisible() and ui.install_no.isVisible()
    ui.install_no.click()
    ui.install_yes.click()
    ui.controller.resolve_installation_confirmation.assert_called_once_with("install", False)


def test_window_waits_for_safe_close(ui, monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    scheduled = []
    monkeypatch.setattr(QTimer, "singleShot", lambda delay, callback: scheduled.append(callback))
    ui.show()
    QApplication.processEvents()
    ui.controller.close.return_value = False
    ui.controller.workers.active = {"install"}
    ui.close()
    assert ui.isVisible() and ui._close_requested
    assert not ui.centralWidget().isEnabled()
    assert "Stop requested" in ui.hero_detail.text()
    ui._finish_close()
    assert ui.isVisible()
    ui.controller.workers.active = frozenset()
    ui.controller.close.return_value = True
    ui._finish_close()
    assert not ui.isVisible()


def test_uncertain_installation_offers_install_again(ui):
    ui._prepare = Mock()
    ui._handle_event({"type": "manual_install_required", "message": "recovery guidance", "recovery_required": True})
    assert not ui.session_manual_retry_action.isHidden()
    assert ui.session_manual_complete_action.isHidden()
    assert ui._manual_retry
    ui.hero_action.click()
    ui._prepare.assert_called_once_with(force=True)
