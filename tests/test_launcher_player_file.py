import pytest
import yaml

from doom_eap.content.options_foundation import default_option_values, dump_player_yaml
from doom_eap.launcher.launcher_controller import LauncherController

pytest_plugins = ("tests.test_launcher_setup",)


def test_translated_editor_exports_identical_yaml(ui, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QLabel, QMessageBox

    from doom_eap.launcher.ui import strings
    from doom_eap.launcher.ui.player_file_view import PlayerFileView
    from doom_eap.strings import load_catalog

    values = ui.player_view._option_values()
    values.update(goal="kill_the_icon_of_sin", mission_count="all", use_dlc_content=False,
                  additional_victory_requirements=["Complete All Included Missions"],
                  enemy_preset="custom", enemy_health={"imp.minimum": 50, "imp.maximum": 100},
                  filler_weights={"Ammo Refill": 0, "Full Heal": 17}, start_inventory={"Air Control": 2})
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, message: pytest.fail(message))
    ui.controller.save_player_options.side_effect = lambda *args, **kwargs: LauncherController.save_player_options(
        ui.controller, *args, **kwargs
    )
    exports = []
    default_names = []
    for locale in ("en", "pt-BR"):
        monkeypatch.setattr(strings, "_locale", locale)
        editor = PlayerFileView(ui.controller, ui.player_view.COLORS)
        default_names.append(editor.player_name.text())
        editor.player_name.setText("Marine")
        editor._set_option_values(values)
        editor.inventory.setValue(values["start_inventory"])
        editor.dependencies._refresh_create_dependencies()
        destination = tmp_path / f"{locale}.yaml"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, path=destination: (str(path), ""))
        editor._save_player_options()
        exports.append(destination.read_bytes())
        goal = editor.option_controls["goal"]
        assert goal.currentData() == values["goal"]
        assert goal.currentText() == load_catalog().text("options", "goal.choice.kill_the_icon_of_sin", locale=locale)
        assert editor.effective_config_values["goal"].text() == goal.currentText()
        assert editor.option_rows["goal"].findChildren(QLabel)[0].text() == load_catalog().text("options", "goal.name", locale=locale)
        assert editor._option_values() == values
        editor.close()
        editor.deleteLater()
    assert exports[0] == exports[1]
    assert default_names[0] == default_names[1]


def test_mounted_player_editor_import_edit_export_preserves_yaml(ui, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QPushButton

    schema = ui.controller.options_schema
    document = yaml.safe_load(dump_player_yaml(schema, "Marine", default_option_values(schema)))
    document["description"] = "Personal settings"
    document["DOOM Eternal"]["start_hints"] = ["Ballista"]
    document["DOOM Eternal"]["filler_weights"] = {"Ammo Refill": 0, "Full Heal": 17}
    document["DOOM Eternal"]["start_inventory"] = {"Air Control": 2}
    source = tmp_path / "source.yaml"
    source.write_text(yaml.safe_dump(document), encoding="utf-8")
    original = source.read_bytes()
    destination = tmp_path / "edited.yaml"
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a: (str(source), ""))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(destination), ""))
    monkeypatch.setattr(QMessageBox, "information", lambda *a: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, message: pytest.fail(message))
    ui.controller.save_player_options.side_effect = lambda *args, **kwargs: LauncherController.save_player_options(
        ui.controller, *args, **kwargs
    )
    ui.nav_buttons[1].click()
    editor = ui.player_view
    ui.show()
    QApplication.processEvents()
    footer_position = editor.save_button.mapTo(ui, editor.save_button.rect().topLeft())
    editor.scroll.verticalScrollBar().setValue(editor.scroll.verticalScrollBar().maximum())
    QApplication.processEvents()
    assert editor.save_button.isVisible()
    assert editor.save_button.mapTo(ui, editor.save_button.rect().topLeft()) == footer_position
    editor.findChild(QPushButton, "playerImport").click()
    assert editor.player_name.text() == "Marine"
    editor.player_name.setText("Edited")
    for width in (900, 1280, 1600):
        ui.resize(width, 720)
        QApplication.processEvents()
    editor.save_button.click()
    exported = yaml.safe_load(destination.read_text(encoding="utf-8"))
    assert exported["name"] == "Edited" and exported["description"] == "Personal settings"
    assert exported["DOOM Eternal"]["start_hints"] == ["Ballista"]
    assert exported["DOOM Eternal"]["filler_weights"] == document["DOOM Eternal"]["filler_weights"]
    assert exported["DOOM Eternal"]["start_inventory"] == {"Air Control": 2}
    assert source.read_bytes() == original
    ui.controller.prepare_setup.assert_not_called()


def test_option_reflow_preserves_explicit_zero_auto_and_goal_dependencies(ui):
    from PySide6.QtWidgets import QApplication

    editor = ui.player_view
    ui.nav_buttons[1].click()
    ui.show()
    filler = editor.option_controls["filler_weights"]
    filler.fields["Ammo Refill"].setValue(0)
    filler.fields["Full Heal"].setValue(17)
    assert filler.value() == {"Ammo Refill": 0, "Full Heal": 17}
    health = editor.option_controls["enemy_health"]
    health.fields["minimum"].setValue(80)
    health.fields["maximum"].setValue(120)
    requirements = editor.option_controls["additional_victory_requirements"]
    editor.option_controls["use_dlc_content"].setChecked(False)
    editor.option_controls["include_weapon_mastery_challenges"].setChecked(False)
    mastery = next(check for key, check in requirements.checks if key == "Complete All Weapon Mastery Challenges")
    assert not mastery.isEnabled()
    expected = editor._option_values()
    assert all(editor.isAncestorOf(row) for row in editor.option_rows.values())
    states = [(check.isChecked(), check.isEnabled()) for _, check in requirements.checks]
    for width in (900, 1280, 1600, 900):
        ui.resize(width, 720)
        QApplication.processEvents()
        assert editor._option_values() == expected
        assert [(check.isChecked(), check.isEnabled()) for _, check in requirements.checks] == states
    filler.fields["Ammo Refill"].setValue(-1)
    assert filler.value() == {"Full Heal": 17}
