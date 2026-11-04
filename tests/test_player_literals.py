import pytest

from tools.validation.player_literals import issues, shell_issues


@pytest.mark.parametrize(
    "source",
    [
        'QLabel("Unexpected text")',
        'button.setText("New action")',
        'QMessageBox.warning(parent, "Warning", "Try again")',
        'emit("warning", message="New warning")',
        'event = {"type": "warning", "message": "New warning"}',
        'notify("New notification")',
        'caption = "New action"\nbutton.setText(caption)',
        'button.setText(event.get("message", "New fallback"))',
        'cells = (("key", "New label"),)\nfor key, label in cells:\n    QLabel(label)',
        'QLabel(f"Waiting {seconds}s")',
    ],
)
def test_player_literal_gate_rejects_display_text(source):
    assert issues(source, "fixture.py")


def test_player_literal_gate_accepts_catalog_and_machine_values():
    assert (
        issues(
            """
label.setText(player_text("launcher.ui", "ready"))
event = {"type": "warning", "code": "not_ready", "message": message}
widget.setObjectName("technicalName")
label.setText(str(event["message"]))
label.setText(text("launcher.ui", "ready") if state == "ready" else message)
""",
            "fixture.py",
        )
        == []
    )
    assert shell_issues('echo "Starting AP Client: ${NEW_CMD[@]}" >> "$DIR/bridge_debug.log"', "run_bridge.sh") == []
    assert shell_issues('echo "New player message"', "run_bridge.sh")
