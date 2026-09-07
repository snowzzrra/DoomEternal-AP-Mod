import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from doom_eap.launcher.launcher_ui import LauncherUI
    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False
    LauncherUI = None

from doom_eap.launcher.launcher_controller import LauncherController, bundle_directory


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is required for launcher UI tests")
class TestLauncherUIConstruction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HAS_PYSIDE6:
            return
        cls.app = QApplication.instance()
        if cls.app is None:
            cls.app = QApplication(["test_launcher_ui", "-platform", "offscreen"])

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="doomeap_ui_test_")
        self.tmp_root = Path(self.tmp_dir.name)

        fake_app = self.tmp_root / "application"
        fake_client_data = fake_app / "client" / "data"
        fake_client_data.mkdir(parents=True)
        schema_src = bundle_directory() / "data" / "options_schema.json"
        if schema_src.is_file():
            shutil.copy2(schema_src, fake_client_data / "options_schema.json")

        self.fake_user_state = self.tmp_root / "user_state"
        self.fake_user_config = self.tmp_root / "user_config"
        self.fake_user_data = self.tmp_root / "user_data"
        self.fake_user_state.mkdir(parents=True)
        self.fake_user_config.mkdir(parents=True)
        self.fake_user_data.mkdir(parents=True)

        self.env_override = {
            "XDG_CONFIG_HOME": str(self.fake_user_config),
            "XDG_STATE_HOME": str(self.fake_user_state),
            "XDG_DATA_HOME": str(self.fake_user_data),
            "APPDATA": str(self.fake_user_config),
            "LOCALAPPDATA": str(self.fake_user_state),
        }
        self.old_env = {k: os.environ.get(k) for k in self.env_override}
        for k, v in self.env_override.items():
            os.environ[k] = v

        self.controller = LauncherController(application_dir=fake_app)
        self.controller.discover = MagicMock(return_value={})

    def tearDown(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp_dir.cleanup()

    def test_launcher_ui_constructs_all_pages_without_errors(self):
        ui = LauncherUI(self.controller)
        try:
            self.assertEqual(ui.pages.count(), 5)
            self.assertEqual(ui.windowTitle(), "DOOM Eternal Archipelago")

            # Verify navigation across all 5 primary pages
            for page_index in range(5):
                ui._show_page(page_index)
                self.assertEqual(ui.pages.currentIndex(), page_index)
                self.app.processEvents()

            # Specifically verify session page layouts and actions
            ui._show_page(2)
            self.assertTrue(hasattr(ui, "session_setup"))
            self.assertTrue(hasattr(ui, "session_setup_action"))
            self.assertTrue(hasattr(ui, "session_manual_complete_action"))
            self.assertTrue(hasattr(ui, "session_manual_retry_action"))
            self.assertTrue(hasattr(ui, "command_input"))
            self.assertFalse(ui.command_input.isEnabled())

            # Exercise session setup state transitions
            ui._set_setup_state("manual_install_required", "Manual setup required.")
            self.assertFalse(ui.session_manual_complete_action.isHidden())
            self.assertFalse(ui.session_manual_retry_action.isHidden())
            self.assertEqual(ui.session_setup_action.text(), "OPEN MANUAL INSTALL GUIDE")

            ui._set_setup_state("install_needed", "Install needed.")
            self.assertTrue(ui.session_manual_complete_action.isHidden())
            self.assertTrue(ui.session_manual_retry_action.isHidden())
            self.assertEqual(ui.session_setup_action.text(), "INSTALL ROOM PACKAGE")

            ui._set_setup_state("ready")
            self.assertTrue(ui.session_setup_action.isHidden())

            # Switch session sub-tabs
            for tab_index in (0, 1, 2, 3):
                ui._show_session_tab(tab_index)
                self.assertEqual(ui.session_stack.currentIndex(), tab_index)
                self.app.processEvents()

            ui._handle_event({"type": "connected", "team": 0, "slot": 1, "slot_data": {}})
            self.assertTrue(ui.command_input.isEnabled())
            self.assertEqual(ui._hints_state, "loading")
            self.assertEqual(ui.hints_empty.text(), "Loading hints…")
            ui._render_hints({"hints": [
                {
                    "receiving_player": 1, "finding_player": 2, "location": 100,
                    "item": 200, "entrance": "", "status_name": "HINT_PRIORITY",
                    "item_name": "Super Shotgun", "location_name": "Cultist Base",
                    "receiving_player_name": "Doom Slayer", "finding_player_name": "Other",
                },
                {
                    "receiving_player": 1, "finding_player": 2, "location": 100,
                    "item": 200, "entrance": "", "status_name": "HINT_FOUND",
                    "item_name": "Super Shotgun", "location_name": "Cultist Base",
                    "receiving_player_name": "Doom Slayer", "finding_player_name": "Other",
                },
            ]})
            self.assertEqual(ui.hints.rowCount(), 1)
            self.assertEqual(ui.hints.item(0, 0).text(), "FOUND")
            ui._render_hints({"hints": []})
            self.assertEqual(ui._hints_state, "loaded")
            self.assertEqual(ui.hints_empty.text(), "No hints yet.")
            ui._handle_event({"type": "disconnected"})
            self.assertEqual(ui._hints_state, "disconnected")
            self.assertEqual(ui.hints_empty.text(), "Hints unavailable while disconnected.")

            # Exercise log appending and event formatting
            ui._append_log("Test diagnostic log line")
            ui._append_session_event({"type": "room_info", "seed_name": "test_seed", "slot": "DoomSlayer"})
        finally:
            ui.timer.stop()
            ui.close()
            ui.deleteLater()
            self.app.processEvents()

    def test_launcher_ui_windows_hides_steam_launch_option(self):
        from unittest.mock import patch
        with patch("os.name", "nt"):
            ui = LauncherUI(self.controller)
            try:
                self.assertTrue(ui.launch_option.isHidden())
                self.assertTrue(ui.launch_option_label.isHidden())
                self.assertTrue(ui.copy_launch_option_button.isHidden())
                ui._set_setup_state("ready")
                self.assertIn("Start DOOM Eternal normally", ui.session_setup_detail.text())
            finally:
                ui.timer.stop()
                ui.close()
                ui.deleteLater()
                self.app.processEvents()

    def test_launcher_ui_linux_shows_steam_launch_option(self):
        from unittest.mock import patch
        with patch("os.name", "posix"):
            ui = LauncherUI(self.controller)
            try:
                # Full launch option stays hidden; Room card exposes status + COPY only.
                self.assertTrue(ui.launch_option.isHidden())
                self.assertFalse(ui.launch_option_label.isHidden())
                self.assertFalse(ui.copy_launch_option_button.isHidden())
                ui._set_setup_state("ready")
                self.assertIn("Copy the Steam launch option", ui.session_setup_detail.text())
            finally:
                ui.timer.stop()
                ui.close()
                ui.deleteLater()
                self.app.processEvents()

    def test_chat_sends_exact_text_and_clears_after_bridge_confirmation(self):
        ui = LauncherUI(self.controller)
        try:
            self.controller.send_chat = MagicMock()
            ui._room_connected = True
            ui._set_chat_enabled(True)
            ui.command_input.setText("  !hint Super Shotgun  ")
            ui._send_command()
            self.controller.send_chat.assert_called_once_with("  !hint Super Shotgun  ")
            self.assertEqual(ui.command_input.text(), "  !hint Super Shotgun  ")
            self.assertFalse(ui.command_input.isEnabled())
            ui._handle_event({"type": "chat_sent", "text": "  !hint Super Shotgun  "})
            self.assertEqual(ui.command_input.text(), "")
            self.assertTrue(ui.command_input.isEnabled())
            ui.command_input.setText("   ")
            ui._send_command()
            self.controller.send_chat.assert_called_once()
        finally:
            ui.timer.stop()
            ui.close()
            ui.deleteLater()
            self.app.processEvents()

    def test_activity_table_pruning_and_selection_retention(self):
        ui = LauncherUI(self.controller)
        try:
            for i in range(100):
                ui._activity_event({"type": "chat_sent", "text": f"message {i}"})
            self.assertEqual(ui.activity.rowCount(), 100)

            # Test 1: Selecting row 50 and inserting should shift selection to row 51
            ui.activity.setCurrentCell(50, 0)
            self.assertEqual(ui.activity.currentRow(), 50)
            ui._activity_event({"type": "chat_sent", "text": "shift message"})
            self.assertEqual(ui.activity.rowCount(), 100)
            self.assertEqual(ui.activity.currentRow(), 51)

            # Test 2: Selecting row 99 and inserting prunes row 99 (shifted to 100) and clears selection cleanly
            ui.activity.setCurrentCell(99, 0)
            self.assertEqual(ui.activity.currentRow(), 99)
            ui._activity_event({"type": "chat_sent", "text": "prune message"})
            self.assertEqual(ui.activity.rowCount(), 100)
            self.assertEqual(ui.activity.currentRow(), -1)
        finally:
            ui.timer.stop()
            ui.close()
            ui.deleteLater()
            self.app.processEvents()

    def test_starting_weapon_refreshes_effective_config(self):
        ui = LauncherUI(self.controller)
        try:
            ui._show_page(3)
            control = ui.option_controls.get("starting_weapon")
            self.assertIsNotNone(control)
            from PySide6.QtWidgets import QComboBox
            self.assertIsInstance(control, QComboBox)
            combo = control
            if combo.count() > 1:
                new_index = 1 if combo.currentIndex() == 0 else 0
                expected_text = combo.itemText(new_index)
                combo.setCurrentIndex(new_index)
                self.assertEqual(ui.effective_config_values["starting"].text(), expected_text)
        finally:
            ui.timer.stop()
            ui.close()
            ui.deleteLater()
            self.app.processEvents()

    def test_ui_repair_result_updates_doctor_action(self):
        ui = LauncherUI(self.controller)
        try:
            ui._handle_event({
                "type": "ui_repair_result",
                "message": "Repair applied successfully",
                "success": True,
            })
            self.assertEqual(ui.doctor_action.text(), "Repair applied successfully")
        finally:
            ui.timer.stop()
            ui.close()
            ui.deleteLater()
            self.app.processEvents()


class TestGamePathValidationWithoutClassicwads(unittest.TestCase):
    def test_normalize_and_validate_game_without_classicwads(self):
        import sys
        repo_root = Path(__file__).resolve().parents[1]
        archipelago_root = (repo_root.parent / "Archipelago").resolve()
        if str(archipelago_root) not in sys.path:
            sys.path.insert(0, str(archipelago_root))
        from doom_eap.runtime.bridge_client import normalize_doom_base_dir
        from doom_eap.launcher.launcher_core import validate_game
        from doom_eap.runtime.context_registry import evaluate_dlc_availability
        with tempfile.TemporaryDirectory() as tmp_str:
            game_root = Path(tmp_str)
            (game_root / "DOOMEternalx64vk.exe").write_bytes(b"")
            base = game_root / "base"
            base.mkdir()
            (base / "game").mkdir()
            normalized = normalize_doom_base_dir(game_root)
            self.assertEqual(Path(normalized).resolve(), base.resolve())

            meathook = game_root / "XINPUT1_3.dll"
            meathook.write_bytes(b"")
            client_dir = game_root / "client"
            client_dir.mkdir()
            (client_dir / "bridge_client.py").write_bytes(b"")
            saves_dir = game_root / "saves"
            saves_dir.mkdir()

            validate_game(game_root, meathook, client_dir, saves_dir)

            dlc_evidence = evaluate_dlc_availability(game_root)
            self.assertNotEqual(dlc_evidence.status, "unknown")


if __name__ == "__main__":
    unittest.main()
