import yaml

from doom_eap.content.options_foundation import default_option_values, dump_player_yaml
from doom_eap.launcher.launcher_controller import LauncherController

pytest_plugins = ("tests.test_launcher_setup",)


def test_mounted_player_editor_import_edit_export_preserves_yaml(ui, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox, QPushButton

    schema = ui.controller.options_schema
    document = yaml.safe_load(dump_player_yaml(schema, "Marine", default_option_values(schema)))
    document["description"] = "Personal settings"
    document["DOOM Eternal"]["start_hints"] = ["Ballista"]
    source = tmp_path / "source.yaml"
    source.write_text(yaml.safe_dump(document), encoding="utf-8")
    original = source.read_bytes()
    destination = tmp_path / "edited.yaml"
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a: (str(source), ""))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(destination), ""))
    monkeypatch.setattr(QMessageBox, "information", lambda *a: None)
    ui.controller.save_player_options.side_effect = lambda *args, **kwargs: LauncherController.save_player_options(
        ui.controller, *args, **kwargs
    )
    ui.nav_buttons[1].click()
    editor = ui.player_view
    editor.findChild(QPushButton, "playerImport").click()
    assert editor.player_name.text() == "Marine"
    editor.player_name.setText("Edited")
    editor.save_button.click()
    exported = yaml.safe_load(destination.read_text(encoding="utf-8"))
    assert exported["name"] == "Edited" and exported["description"] == "Personal settings"
    assert exported["DOOM Eternal"]["start_hints"] == ["Ballista"]
    assert source.read_bytes() == original
    ui.controller.prepare_setup.assert_not_called()
