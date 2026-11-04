"""Help actions and diagnostic display; application work is supplied as callbacks."""

from __future__ import annotations

from PySide6.QtCore import (
    Qt,
)
from PySide6.QtGui import (
    QFont,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..presenters.diagnostics_presenter import DiagnosticsModel
from .controls import _card, _label
from .strings import text as player_text


class HelpView(QScrollArea):
    _label = staticmethod(_label)
    _card = staticmethod(_card)

    def __init__(self, controller, actions, ap_save_button, session_log, session_card, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.actions = actions
        self.ap_save_button = ap_save_button
        self._build(session_log, session_card)

    def render(self, model: DiagnosticsModel, colors):
        self.doctor_status.setText(model.title)
        self.doctor_status.setStyleSheet(f"color:{colors[model.color]};")
        self.doctor_evidence.setText(model.summary)
        self.doctor_action.setText(model.action)

    def _build(self, session_log, session_card) -> QScrollArea:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(28, 24, 28, 30)
        layout.setSpacing(14)
        header = self._card("hero")
        head = QVBoxLayout(header)
        head.setContentsMargins(24, 20, 24, 20)
        head.addWidget(self._label(player_text("launcher.ui", "help"), "title"))
        head.addWidget(
            self._label(
                player_text("launcher.ui", "check_your_setup_fix_common_problems_or_create_a_support_report"), "muted"
            )
        )
        report_button = QPushButton(player_text("launcher.ui", "report_a_problem"))
        report_button.setObjectName("primary")
        report_button.clicked.connect(self.actions["_report_problem"])
        head.addWidget(report_button)
        layout.addWidget(header)
        card = self._card()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 18, 20, 20)
        card_layout.addWidget(self._label(player_text("launcher.ui", "game_setup"), "eyebrow"))
        self.doctor_status = self._label(player_text("launcher.ui", "setup_not_checked"), "state")
        self.doctor_evidence = self._label(
            player_text("launcher.ui", "select_check_setup_to_look_for_problems"), "muted"
        )
        self.doctor_evidence.setWordWrap(True)
        self.doctor_room_status = self._label(player_text("launcher.ui", "room_mod_not_checked"), "state")
        self.doctor_room_evidence = self._label(
            player_text("launcher.ui", "connect_to_a_room_to_check_its_mod"), "muted"
        )
        self.doctor_room_evidence.setWordWrap(True)
        self.doctor_action = self._label("", "muted")
        card_layout.addWidget(self.doctor_status)
        card_layout.addWidget(self.doctor_evidence)
        card_layout.addWidget(self._label(player_text("launcher.ui", "room_mod"), "eyebrow"))
        card_layout.addWidget(self.doctor_room_status)
        card_layout.addWidget(self.doctor_room_evidence)
        card_layout.addWidget(self.doctor_action)
        buttons = QHBoxLayout()
        for text, callback, primary in (
            (player_text("launcher.ui", "check_setup_bd0801"), self.actions["_run_doctor"], True),
            (player_text("launcher.ui", "fix_setup"), self.actions["_preview_repairs"], False),
            (player_text("launcher.ui", "save_support_report"), self.actions["_save_support_bundle"], False),
        ):
            button = QPushButton(text)
            if primary:
                button.setObjectName("primary")
            button.clicked.connect(callback)
            buttons.addWidget(button)
        self.doctor_proton_button = QPushButton(player_text("launcher.ui", "select_proton"))
        self.doctor_proton_button.clicked.connect(self.actions["_select_proton"])
        self.doctor_proton_button.setVisible(False)
        buttons.addWidget(self.doctor_proton_button)
        self.uninstall_button = QPushButton(player_text("launcher.ui", "uninstall_mod"))
        self.uninstall_button.setObjectName("secondary")
        self.uninstall_button.setEnabled(False)
        self.uninstall_button.clicked.connect(self.actions["_uninstall_room_package"])
        buttons.addWidget(self.uninstall_button)
        buttons.addStretch(1)
        card_layout.addLayout(buttons)
        layout.addWidget(card)
        expand = QPushButton(player_text("launcher.player_file", "advanced"))
        expand.setObjectName("helpAdvanced")
        expand.setCheckable(True)
        layout.addWidget(expand)
        advanced = QWidget()
        advanced.hide()
        expand.toggled.connect(advanced.setVisible)
        advanced_layout = QVBoxLayout(advanced)
        card_layout = advanced_layout
        card_layout.addWidget(self.ap_save_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.core_auto_update = QCheckBox(player_text("launcher.ui", "automatically_update_game_connection_files"))
        self.core_auto_update.setChecked(bool(self.controller.config.get("core_auto_update", True)))
        self.core_auto_update.toggled.connect(
            lambda checked: self.controller.save_config({"core_auto_update": checked})
        )
        card_layout.addWidget(self.core_auto_update)
        core_actions = QHBoxLayout()
        for text, action in (
            (player_text("launcher.ui", "repair_game_connection_d53210"), "repair_core"),
            (player_text("launcher.ui", "repair_save_setup"), "repair_session"),
            (player_text("launcher.ui", "restore_previous_version"), "rollback_core"),
        ):
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, key=action: self.actions["_runtime_repair"](key))
            core_actions.addWidget(button)
        local_core = QPushButton(player_text("launcher.ui", "local_build"))
        local_core.setToolTip(player_text("launcher.ui", "choose_local_game_connection_files"))
        local_core.clicked.connect(self.actions["_select_local_core"])
        core_actions.addWidget(local_core)
        clear_core = QPushButton(player_text("launcher.ui", "use_automatic_downloads"))
        clear_core.clicked.connect(self.actions["_clear_local_core"])
        core_actions.addWidget(clear_core)
        card_layout.addLayout(core_actions)
        self.core_status = self._label(
            player_text("launcher.ui", "using_local_game_connection_files")
            if self.controller.config.get("core_runtime_manifest")
            else player_text("launcher.ui", "game_connection_files_download_during_setup"),
            "muted",
        )
        card_layout.addWidget(self.core_status)
        details = self._card()
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(20, 18, 20, 20)
        details_layout.addWidget(self._label(player_text("launcher.ui", "technical_details"), "section"))
        details_layout.addWidget(
            self._label(
                player_text("launcher.ui", "support_reports_include_these_details_with_passwords_removed"),
                "muted",
            )
        )
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("monospace", 10))
        self.log.setPlaceholderText(player_text("launcher.ui", "select_check_setup_to_collect_details"))
        details_layout.addWidget(self.log, 1)
        advanced_layout.addWidget(details, 1)
        advanced_layout.addWidget(session_log)
        advanced_layout.addWidget(session_card)
        layout.addWidget(advanced)
        layout.addStretch()
        self.setWidgetResizable(True)
        self.setWidget(body)
        self.setFrameShape(QFrame.Shape.NoFrame)
