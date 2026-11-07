"""Render diagnostic state and dispatch Help actions by enum."""

from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..events import ActionId
from ..presenters.diagnostics_presenter import DiagnosticsModel
from .controls import ToggleSwitch, _card, _label
from .doom_style import DoomButton as QPushButton
from .strings import text as player_text


class HelpView(QScrollArea):
    repair_confirmed = Signal()
    download_answered = Signal(bool)
    _label = staticmethod(_label)
    _card = staticmethod(_card)

    def __init__(self, controller, actions, ap_save_button, session_log, session_card, inventory_status, inventory_button, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.actions = actions
        self.ap_save_button = ap_save_button
        self.room_card = session_card
        self.room_card.hide()
        self.inventory_status = inventory_status
        self.inventory_button = inventory_button
        self.symptom_toggles = []
        self.symptom_buttons = {}
        self._build(session_log, session_card)
        self.viewport().installEventFilter(self)

    def render(self, model: DiagnosticsModel, colors):
        self.doctor_status.setText(model.title)
        self.doctor_status.setStyleSheet(f"color:{colors[model.color]};")
        self.doctor_evidence.setText(model.summary)
        self.doctor_action.setText(model.action)
        self.fix_button.setEnabled(model.can_fix)
        self.fix_button.setToolTip("" if model.can_fix else player_text("launcher.help", "check_first"))

    def _button(self, label, action, kind="secondary"):
        button = QPushButton(label)
        button.setObjectName(kind)
        button.clicked.connect(self.actions[action])
        return button

    def _build(self, session_log, session_card):
        body = QWidget()
        self.columns = QGridLayout(body)
        self.columns.setContentsMargins(24, 24, 24, 24)
        self.columns.setHorizontalSpacing(32)
        self.columns.setVerticalSpacing(24)
        self.left = QWidget()
        self.right = QWidget()
        self.columns.addWidget(self.left, 0, 0)
        self.columns.addWidget(self.right, 1, 0)
        self._two_columns = None
        layout = QVBoxLayout(self.left)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._label(player_text("launcher.navigation", "help"), "title"))
        self._heading(layout, "launcher.help", "your_setup", gap=16)
        self._setup(layout)
        self._heading(layout, "launcher.help", "whats_wrong")
        self._symptoms(layout)
        self._heading(layout, "launcher.ui", "report_a_problem")
        layout.addWidget(self._label(player_text("launcher.help", "report_help")))
        layout.addSpacing(12)
        reports = QHBoxLayout()
        reports.setSpacing(12)
        reports.addWidget(self._button(player_text('launcher.ui', 'save_support_report'), ActionId.SAVE_REPORT))
        reports.addWidget(self._button(player_text('launcher.ui', 'open_github_issue'), ActionId.OPEN_ISSUE, 'link'))
        reports.addStretch()
        layout.addLayout(reports)
        for widget in (self.doctor_evidence, self.doctor_room_evidence, self.doctor_action, session_card):
            layout.addWidget(widget)
        log = session_log.findChild(QPlainTextEdit)
        session_log.setVisible(bool(log.toPlainText()))
        log.textChanged.connect(lambda: session_log.setVisible(bool(log.toPlainText())))
        layout.addWidget(session_log)
        layout.addSpacing(16)
        layout.addStretch()
        layout = QVBoxLayout(self.right)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.advanced_toggle = QPushButton(player_text("launcher.help", "advanced"))
        self.advanced_toggle.setObjectName("link")
        self.advanced_toggle.setProperty("compact", True)
        self.advanced_toggle.setCheckable(True)
        layout.addWidget(self.advanced_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        advanced = QWidget()
        self.advanced = advanced
        advanced.hide()
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        self._advanced(advanced)
        layout.addWidget(advanced, 1)
        self.setWidgetResizable(True)
        self.setWidget(body)
        self.setFrameShape(QFrame.Shape.NoFrame)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange_columns(self.viewport().width(), self.viewport().height())

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize:
            self._arrange_columns(event.size().width(), event.size().height())
        return super().eventFilter(watched, event)

    def _arrange_columns(self, width, height):
        two = width - 48 >= 1100
        if two:
            self.right.setFixedHeight(max(0, height - 48))
        else:
            self.right.setMinimumHeight(0)
            self.right.setMaximumHeight(16777215)
        if two == self._two_columns:
            return
        self._two_columns = two
        self.columns.removeWidget(self.left)
        self.columns.removeWidget(self.right)
        self.columns.addWidget(self.left, 0, 0)
        self.columns.addWidget(self.right, 0 if two else 1, 1 if two else 0, Qt.AlignmentFlag.AlignTop)
        self.columns.setColumnStretch(0, 1)
        self.columns.setColumnStretch(1, int(two))

    def _setup(self, layout):
        section = QWidget()
        layout.addWidget(section)
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.doctor_status = self._label(player_text("launcher.support", "not_checked"))
        self.doctor_room_status = self._label(player_text("launcher.support", "not_checked"))
        for key, state in (("game_setup", self.doctor_status), ("room_mod", self.doctor_room_status)):
            row = QHBoxLayout()
            row.setSpacing(12)
            label = self._label(player_text("common", "room_mod") if key == "room_mod" else player_text("launcher.ui", key))
            label.setFixedWidth(260)
            row.addWidget(label)
            state.setProperty("tone", "muted")
            row.addWidget(state)
            row.addStretch()
            layout.addLayout(row)
        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        buttons.addWidget(self._button(player_text('common', 'check_setup'), ActionId.DIAGNOSE, 'primary'))
        self.fix_button = self._button(player_text('launcher.ui', 'fix_setup'), ActionId.FIX_SETUP)
        self.fix_button.setEnabled(False)
        self.fix_button.setToolTip(player_text("launcher.help", "check_first"))
        buttons.addWidget(self.fix_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.repair_preview = QWidget()
        preview = QVBoxLayout(self.repair_preview)
        preview.setContentsMargins(0, 0, 0, 0)
        preview.setSpacing(12)
        self.repair_changes = self._label("")
        self.repair_changes.setTextFormat(Qt.TextFormat.PlainText)
        preview.addWidget(self.repair_changes)
        apply = QPushButton(player_text("launcher.ui", "apply_repair"))
        apply.clicked.connect(self.repair_confirmed.emit)
        cancel = QPushButton(player_text("launcher.items", "cancel"))
        cancel.clicked.connect(self.repair_preview.hide)
        self._action_row(preview, (apply, cancel))
        self.repair_preview.hide()
        layout.addWidget(self.repair_preview)
        self.download_prompt = QWidget()
        download = QVBoxLayout(self.download_prompt)
        download.setContentsMargins(0, 0, 0, 0)
        download.setSpacing(12)
        self.download_details = self._label("")
        self.download_details.setTextFormat(Qt.TextFormat.PlainText)
        download.addWidget(self.download_details)
        accept = QPushButton(player_text("launcher.help", "download"))
        accept.clicked.connect(lambda: self.download_answered.emit(True))
        decline = QPushButton(player_text("launcher.items", "cancel"))
        decline.clicked.connect(lambda: self.download_answered.emit(False))
        self._action_row(download, (accept, decline))
        self.download_prompt.hide()
        layout.addWidget(self.download_prompt)
        self.doctor_evidence = self._label("", "muted")
        self.doctor_room_evidence = self._label("", "muted")
        self.doctor_action = self._label("", "muted")
        self.doctor_proton_button = self._button(player_text('launcher.ui', 'select_proton'), ActionId.SELECT_PROTON)
        self.doctor_proton_button.hide()
        self.uninstall_button = self._button(player_text('launcher.ui', 'uninstall_mod'), ActionId.UNINSTALL, 'danger')
        self.uninstall_button.setEnabled(False)
        self.uninstall_button.setToolTip(player_text("launcher.play", "connect_guidance"))

    def _symptoms(self, layout):
        section = QWidget()
        layout.addWidget(section)
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        for key, button_text, action in (
            ('connection', player_text('launcher.help', 'go_play'), ActionId.SHOW_PLAY),
            ('game', 'Choose game folder', ActionId.CHOOSE_GAME_FOLDER),
            ('item', player_text('common', 'repair_inventory'), ActionId.REPAIR_INVENTORY),
            ('mod', player_text('launcher.setup', 'install_again'), ActionId.REINSTALL),
            ('game_connection', player_text('common', 'check_setup'), ActionId.DIAGNOSE),
            ('other', player_text('launcher.ui', 'save_support_report'), ActionId.SAVE_REPORT),
        ):
            self._symptom(layout, key, button_text, action)

    def _symptom(self, layout, key, button_text, action):
        toggle = QPushButton(player_text("launcher.help", key))
        toggle.setObjectName("symptom")
        toggle.setCheckable(True)
        heading = QHBoxLayout()
        heading.setSpacing(4)
        arrow = self._label('▸ {title}'.format(title='').strip(), "muted")
        heading.addWidget(arrow)
        heading.addWidget(toggle)
        heading.addStretch()
        layout.addLayout(heading)
        expanded = QWidget()
        copy = QVBoxLayout(expanded)
        copy.setContentsMargins(16, 0, 0, 0)
        copy.setSpacing(12)
        description = player_text("launcher.help", key + "_help")
        if key == "game_connection" and sys.platform.startswith("linux"):
            description += " " + player_text("launcher.help", "game_connection_linux")
        copy.addWidget(self._label(description))
        button = self.inventory_button if action is ActionId.REPAIR_INVENTORY else self._button(button_text, action)
        if action is ActionId.REPAIR_INVENTORY:
            copy.addWidget(self.inventory_status)
        self.symptom_buttons[action] = button
        copy.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
        expanded.hide()
        toggle.toggled.connect(lambda checked: self._toggle_symptom(arrow, expanded, checked))
        layout.addWidget(expanded)
        self.symptom_toggles.append(toggle)

    @staticmethod
    def _toggle_symptom(arrow, expanded, checked):
        expanded.setVisible(checked)
        arrow.setText(('▾ {title}'.format(title='') if checked else '▸ {title}'.format(title='')).strip())

    def _toggle_advanced(self, checked):
        self.advanced.setVisible(checked)
        self.advanced_toggle.setText(player_text('launcher.help', 'advanced_open') if checked else player_text("launcher.help", "advanced"))

    def _advanced(self, advanced):
        advanced_layout = QVBoxLayout(advanced)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.setSpacing(0)
        self._heading(advanced_layout, "launcher.help", "connection_section")
        self.core_auto_update = ToggleSwitch(player_text("launcher.help", "automatic_updates"))
        self.core_auto_update.setChecked(bool(self.controller.config.get("core_auto_update", True)))
        self.core_auto_update.toggled.connect(
            lambda checked: self.controller.save_config({"core_auto_update": checked})
        )
        advanced_layout.addWidget(self.core_auto_update)
        advanced_layout.addSpacing(4)
        description = self._label(player_text("launcher.help", "downloaded_during_setup"), "muted")
        description.setContentsMargins(44, 0, 0, 0)
        advanced_layout.addWidget(description)
        advanced_layout.addSpacing(12)
        self._action_row(advanced_layout, (
            self._button(player_text('common', 'repair_game_connection'), ActionId.REPAIR_CORE),
            self._button(player_text('launcher.ui', 'local_build'), ActionId.LOCAL_BUILD),
            self._button(player_text('launcher.help', 'automatic_downloads'), ActionId.AUTOMATIC_CORE),
        ))
        advanced_layout.addSpacing(12)
        self._action_row(advanced_layout, (
            self._button(player_text('launcher.help', 'restore_version'), ActionId.ROLLBACK_CORE, 'danger'),
            self.doctor_proton_button,
        ))
        self.core_status = self._label(
            player_text("launcher.ui", "using_local_game_connection_files")
            if self.controller.config.get("core_runtime_manifest")
            else "",
            "muted",
        )
        self.core_status.setVisible(bool(self.core_status.text()))
        advanced_layout.addWidget(self.core_status)
        self._heading(advanced_layout, "launcher.help", "saves_section")
        self.ap_save_button.setText(player_text("launcher.help", "saves_backups"))
        self._action_row(advanced_layout, (
            self.ap_save_button,
            self._button(player_text('launcher.help', 'repair_saves'), ActionId.REPAIR_SESSION),
        ))
        self._heading(advanced_layout, "launcher.help", "remove_section")
        advanced_layout.addWidget(self.uninstall_button, alignment=Qt.AlignmentFlag.AlignLeft)
        advanced_layout.addSpacing(24)
        details = self._card("plain")
        details_layout = QVBoxLayout(details)
        details_layout.setSpacing(8)
        details_layout.setContentsMargins(0, 0, 0, 0)
        heading = QHBoxLayout()
        title = self._label(player_text("launcher.ui", "technical_details"), "section")
        title.setWordWrap(False)
        heading.addWidget(title, 1)
        copy = QPushButton(player_text("launcher.ui", "copy"))
        copy.setObjectName("link")
        copy.setProperty("compact", True)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.log.toPlainText()))
        heading.addWidget(copy)
        details_layout.addLayout(heading)
        details_layout.addWidget(
            self._label(
                player_text("launcher.ui", "support_privacy_help"),
                "muted",
            )
        )
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(80)
        self.log.setObjectName("technicalDetails")
        self.log.setFont(QFont("Consolas", 9))
        self.log.setPlaceholderText(player_text("launcher.ui", "select_check_setup_to_collect_details"))
        details_layout.addWidget(self.log, 1)
        advanced_layout.addWidget(details, 1)

    @staticmethod
    def _heading(layout, category, key, gap=24):
        layout.addSpacing(gap)
        layout.addWidget(_label(player_text(category, key), "eyebrow"))
        layout.addSpacing(8)

    @staticmethod
    def _action_row(layout, buttons):
        row = QHBoxLayout()
        row.setSpacing(8)
        for button in buttons:
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
