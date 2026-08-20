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
            self.assertEqual(ui.session_setup_action.text(), "INSTALL MOD")

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
                self.assertFalse(ui.launch_option.isHidden())
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


if __name__ == "__main__":
    unittest.main()
