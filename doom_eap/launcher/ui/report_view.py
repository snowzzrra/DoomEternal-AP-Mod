"""Editable support preview and local export actions; no report upload."""

from __future__ import annotations

from doom_eap.launcher.launcher_support import sanitize_support_value
from ..launcher_reporting import github_issue_url, report_text

import os
import subprocess
import webbrowser
from pathlib import Path

from PySide6.QtCore import (
    Qt,
    QUrl,
)
from PySide6.QtGui import (
    QDesktopServices,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
)

from .doom_style import DoomButton as QPushButton
from .strings import record_failure
from .strings import text as player_text


class ReportDialog(QDialog):
    def __init__(self, controller, event, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.presenter = ReportPresenter(event)
        self._build(event)

    def _build(self, event) -> None:
        payload = self.presenter.payload
        dialog = self
        dialog.setWindowTitle(player_text("launcher.ui", "review_problem_report"))
        dialog.resize(660, 560)
        layout = QVBoxLayout(dialog)
        message = QLabel(event["message"])
        message.setWordWrap(True)
        layout.addWidget(message)
        title = QLineEdit(payload["title"])
        title.setMaxLength(120)
        layout.addWidget(QLabel(player_text("launcher.ui", "title")))
        layout.addWidget(title)
        description = QPlainTextEdit()
        description.setPlaceholderText(player_text("launcher.ui", "what_happened_and_what_did_you_expect"))
        description.setPlainText(payload["description"])
        title.setReadOnly(bool(event.get("submitted")))
        description.setReadOnly(bool(event.get("submitted")))
        description.setMaximumHeight(120)
        layout.addWidget(description)
        preview = QPlainTextEdit()
        preview.setReadOnly(True)
        preview.setPlainText(payload["diagnostics"])
        layout.addWidget(QLabel(player_text("launcher.ui", "sanitized_details")))
        layout.addWidget(preview)
        path = QLabel(
            player_text("launcher.ui", "support_report_file")
            + str(event.get("path") or player_text("launcher.report_view", "not_created"))
        )
        path.setWordWrap(True)
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(path)
        instruction = QLabel(
            player_text("launcher.ui", "attach_report_help")
        )
        instruction.setWordWrap(True)
        layout.addWidget(instruction)
        actions = QHBoxLayout()

        def document():
            return self.presenter.document(title.text(), description.toPlainText())

        open_issue = QPushButton(player_text("launcher.ui", "open_github_issue"))
        open_issue.setObjectName("reportOpenIssue")

        def open_github():
            url, clipboard = self.presenter.issue(document())
            if clipboard is not None:
                QApplication.clipboard().setText(clipboard)
                message.setText(
                    player_text("launcher.ui", "report_copied_help")
                )
            if not webbrowser.open(url):
                message.setText(
                    player_text("launcher.ui", "browser_failed")
                )

        open_issue.clicked.connect(open_github)
        copy_report = QPushButton(player_text("launcher.ui", "copy_report"))
        copy_report.setObjectName("reportCopy")
        copy_report.clicked.connect(lambda: QApplication.clipboard().setText(self.presenter.copy(document())))
        show_zip = QPushButton(player_text("launcher.ui", "show_zip"))
        show_zip.setObjectName("reportShowZip")
        show_zip.setEnabled(bool(event.get("path")))

        def reveal_zip():
            filename = Path(event["path"]).resolve()
            if os.name == "nt":
                try:
                    subprocess.Popen(["explorer.exe", f"/select,{filename}"])
                except OSError as error:
                    self.controller.emit("log", message=record_failure(self.controller, "report", error))
                    message.setText(player_text("launcher.ui", "could_not_open_the_zip_folder"))
            elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(filename.parent))):
                message.setText(player_text("launcher.ui", "could_not_open_the_zip_folder"))

        show_zip.clicked.connect(reveal_zip)
        close = QPushButton(player_text("launcher.ui", "close"))
        close.clicked.connect(dialog.accept)
        for button in (open_issue, copy_report, show_zip, close):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.document = document

    def save_draft(self):
        self.controller.save_problem_report(self.document())


class ReportPresenter:
    def __init__(self, event):
        self.payload = sanitize_support_value(dict(event["payload"]))

    def document(self, title, description):
        return sanitize_support_value({**self.payload, "title": title[:120], "description": description[:8000]})

    issue = staticmethod(github_issue_url)
    copy = staticmethod(report_text)
