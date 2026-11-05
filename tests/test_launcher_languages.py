"""Protect catalog-only language contributions and isolated launcher preferences."""

import json
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest

from doom_eap.launcher.ui import strings
from doom_eap.strings import CATALOG_PATH, read_catalog


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    path = tmp_path / "strings"
    shutil.copytree(CATALOG_PATH, path)
    document = json.loads((path / "languages.json").read_text(encoding="utf-8"))
    document["languages"].append({"code": "fr", "name": "Français"})
    (path / "languages.json").write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    (path / "fr.json").write_text(json.dumps({"launcher.navigation.play": "Jouer", "launcher.save.recovering": "Récupération"}), encoding="utf-8")
    catalog = read_catalog(path)
    monkeypatch.setattr(strings, "load_catalog", lambda: catalog)
    monkeypatch.setattr("doom_eap.launcher.ui.language_button.load_catalog", lambda: catalog)
    monkeypatch.setattr("doom_eap.launcher.launcher_controller.load_catalog", lambda: catalog)
    monkeypatch.setattr(strings, "_locale", "en")
    return catalog


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def controller(tmp_path):
    from doom_eap.launcher.launcher_controller import LauncherController

    controller = LauncherController.__new__(LauncherController)
    controller.config_path = tmp_path / "launcher.json"
    controller.config = {"launcher_language": "en", "server_address": "fixture.invalid", "slot": "Slayer"}
    controller.emit = Mock()
    controller.bundle_dir = Path(__file__).parents[1]
    controller._persist_config()
    return controller


@pytest.mark.parametrize("saved,system,expected", [
    ("pt-BR", "en_US", "pt-BR"), (None, "pt_BR", "pt-BR"), (None, "en_GB", "en"),
    (None, "fr_FR", "fr"), ("unknown", "fr_FR", "fr"), ([], "pt_BR", "pt-BR"),
    (None, "pt_PT", "en"), (None, "ja_JP", "en"),
])
def test_saved_language_or_available_system_language(catalog, saved, system, expected):
    assert strings.configure_language(saved, system) == expected


def test_option_translation_falls_back_to_schema(catalog, monkeypatch):
    monkeypatch.setattr(strings, "_locale", "pt-BR")
    assert strings.option_text("goal", "name", "Goal") == "Objetivo"
    monkeypatch.setattr(strings, "_locale", "fr")
    assert strings.option_text("goal", "name", "New schema goal") == "Goal"
    with pytest.raises(KeyError):
        strings.option_text("new_option", "name", "Schema name")
    with pytest.raises(KeyError):
        strings.option_text("new_option", "choice", "Schema label", choice_key="choice")
    assert strings.option_text("", "choice", "Air Control", choice_key="Air Control") == "Air Control"


@pytest.mark.parametrize("bound,english,portuguese", [
    ("minimum", "Minimum", "Mínimo"), ("maximum", "Maximum", "Máximo"),
    ("early_minimum", "Early Minimum", "Mínimo inicial"), ("early_maximum", "Early Maximum", "Máximo inicial"),
    ("late_minimum", "Late Minimum", "Mínimo final"), ("late_maximum", "Late Maximum", "Máximo final"),
])
def test_enemy_labels_keep_schema_names_and_translate_shared_limits(catalog, monkeypatch, bound, english, portuguese):
    label = "Arch-vile — " + english
    assert strings.option_text("enemy_health", "choice", label, choice_key="archvile." + bound) == label
    monkeypatch.setattr(strings, "_locale", "pt-BR")
    assert strings.option_text("enemy_health", "choice", label, choice_key="archvile." + bound) == "Arch-vile — " + portuguese


@pytest.mark.parametrize("operation", ["check", "install_mod", "install_core", "repair", "connect", "saves", "uninstall", "report", "player_options", "ammo_refill"])
def test_operation_errors_have_player_guidance_without_raw_exception(catalog, monkeypatch, operation):
    monkeypatch.setattr(strings, "_locale", "pt-BR")
    technical = RuntimeError("private driver failure")
    message = strings.failure_text(operation, technical)
    assert message == catalog.text("launcher.failures", operation, locale="pt-BR")
    assert str(technical) not in message


def test_catalog_error_is_localized_at_the_dialog_boundary(catalog, monkeypatch):
    from doom_eap.strings import PlayerError

    error = PlayerError("launcher.ui", "close_doom_eternal_before_closing_this_launcher")
    monkeypatch.setattr(strings, "_locale", "pt-BR")
    assert strings.failure_text("repair", error) == catalog.text(error.category, error.key, locale="pt-BR")


def test_button_menu_saves_language_and_applies_on_next_start(app, catalog, controller, monkeypatch):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QLabel, QMessageBox

    from doom_eap.launcher.ui import language_button

    monkeypatch.setattr(language_button, "load_catalog", lambda: catalog)
    monkeypatch.setattr("doom_eap.launcher.launcher_controller.load_catalog", lambda: catalog)
    notice = Mock()
    monkeypatch.setattr(QMessageBox, "information", notice)
    button = language_button.LanguageButton(controller)
    assert not button.icon().isNull()
    assert not button.icon().pixmap(30, 20).isNull()
    assert [action.data() for action in button.menu().actions()] == ["pt-BR", "fr"]
    opened = Mock()
    button.menu().aboutToShow.connect(opened)
    button.show()
    QTimer.singleShot(0, button.menu().close)
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    opened.assert_called_once()
    button.menu().actions()[1].trigger()
    assert controller._load_config()["launcher_language"] == "fr"
    assert button.text() == "fr" and button.icon().isNull()
    assert "Français" in button.accessibleName()
    assert [action.data() for action in button.menu().actions()] == ["en", "pt-BR"]
    notice.assert_called_once()
    assert notice.call_args.args[2] == catalog.text("launcher.language", "restart", locale="fr")
    assert strings.text("launcher.navigation", "play") == "Play"
    assert strings.configure_language(controller._load_config()["launcher_language"], "en_US") == "fr"
    assert QLabel(strings.text("launcher.navigation", "play")).text() == "Jouer"
    assert strings.text("launcher.navigation", "help") == "Help"
    assert strings.text("launcher.save", "recovering") == "Récupération"
    assert catalog.text("launcher.navigation", "play") == "Play"
    assert controller._load_config()["slot"] == "Slayer"
    button.close()


@pytest.mark.parametrize("language", ["en", "pt-BR"])
def test_bundled_flags_render_and_missing_asset_shows_code(app, controller, language):
    from doom_eap.launcher.ui.language_button import LanguageButton

    controller.config["launcher_language"] = language
    button = LanguageButton(controller)
    assert not button.icon().pixmap(30, 20).isNull()
    controller.bundle_dir = controller.config_path.parent
    missing = LanguageButton(controller)
    assert missing.text() == language and missing.icon().isNull()
    button.close()
    missing.close()


def test_failed_language_save_keeps_previous_choice(app, controller, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from doom_eap.launcher.ui.language_button import LanguageButton

    before = controller.config_path.read_bytes()
    monkeypatch.setattr("doom_eap.launcher.launcher_controller.publish_file", Mock(side_effect=OSError("fixture")))
    warning, information = Mock(), Mock()
    monkeypatch.setattr(QMessageBox, "warning", warning)
    monkeypatch.setattr(QMessageBox, "information", information)
    button = LanguageButton(controller)
    button.menu().actions()[0].trigger()
    assert controller.config["launcher_language"] == "en" and button.language == "en"
    assert controller.config_path.read_bytes() == before
    warning.assert_called_once()
    assert controller.last_operation_failure["technical_message"] == "fixture"
    controller.emit.assert_called_once_with("log", message="fixture")
    information.assert_not_called()
    with pytest.raises(ValueError, match="unsupported launcher language"):
        controller.set_launcher_language("undeclared")
    button.close()


def test_launcher_window_constructs_translated_views(ui, catalog):
    from PySide6.QtWidgets import QLabel

    from doom_eap.launcher.ui.app_window import LauncherUI

    ui.controller.config["launcher_language"] = "fr"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(LauncherUI, "_discover", lambda self: None)
        translated = LauncherUI(ui.controller)
    translated.timer.stop()
    translated.lifecycle_timer.stop()
    assert translated.nav_buttons[0].text() == "Jouer"
    assert translated.settings_view.findChild(QLabel, "title").text() == "Settings"
    assert translated.language_button.text() == "fr"
    translated.close()


pytest_plugins = ("tests.test_launcher_setup",)
