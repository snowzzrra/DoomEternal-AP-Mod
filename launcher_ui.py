"""PySide6 UI for standalone DOOM Eternal Archipelago launcher."""

from __future__ import annotations

import queue
import html
import re
from pathlib import Path
from typing import cast

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from launcher_controller import LauncherController
from options_foundation import load_start_inventory_catalog, suggested_yaml_filename


class LauncherUI(QMainWindow):
    """Guided Qt shell; controller events are consumed on Qt main thread."""

    COLORS = {
        "background": "#0b1016",
        "surface": "#121b25",
        "surface_alt": "#192735",
        "border": "#2c4253",
        "text": "#eef5fa",
        "muted": "#9cabb9",
        "accent": "#e86b35",
        "accent_active": "#ff8750",
        "success": "#78df8a",
        "info": "#68d5e9",
        "warning": "#f0b24a",
        "danger": "#ed6d6d",
    }

    def __init__(self, controller: LauncherController):
        super().__init__()
        self.controller = controller
        self.setWindowTitle("DOOM Eternal Archipelago")
        self.resize(1180, 820)
        self.setMinimumSize(900, 680)
        self._step_widgets: list[tuple[QFrame, QLabel, QLabel]] = []
        self._configure_style()
        self._load_icon()

        self.server = QLineEdit(str(controller.config.get("server_address", "")))
        self.slot = QLineEdit(str(controller.config.get("slot", "")))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.game_root = QLineEdit(str(controller.config.get("game_root", "")))
        self.saves_root = QLineEdit(str(controller.config.get("save_games_dir", "")))
        self.headline = QLabel("Configure your game and connect to your room.")
        self.detail = QLabel("We will prepare the mod and never launch DOOM Eternal directly.")
        self.next_action = "Connect to Archipelago"
        self.overall_state = QLabel("READY TO CONFIGURE")
        self._room_connected = False
        self._connection_pending = False
        self.install_guidance = QLabel()
        self.option_controls: dict[str, QWidget] = {}
        self.option_defaults: dict[str, object] = {}
        self.start_inventory_catalog: list[dict[str, str]] = []
        self._build()
        self._discover()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_events)
        self._timer.start(75)

    def _configure_style(self) -> None:
        self.setStyleSheet(f"""
            QWidget {{ background: {self.COLORS['background']}; color: {self.COLORS['text']};
                font-family: system-ui, 'Segoe UI', sans-serif; font-size: 11pt; }}
            QLabel {{ background: transparent; }}
            QMainWindow {{ background: {self.COLORS['background']}; }}
            QFrame#surface, QFrame#card {{ background: {self.COLORS['surface']};
                border: 1px solid {self.COLORS['border']}; border-radius: 5px; }}
            QFrame#surface {{ border-left: 3px solid #35566a; }}
            QFrame#card {{ background: {self.COLORS['surface_alt']}; border-color: #263b4b; }}
            QFrame#optionCard {{ background: #101923; border: 1px solid #263f50; border-left: 3px solid #365c6d; border-radius: 4px; }}
            QLabel#title {{ font-size: 24pt; font-weight: 700; letter-spacing: 1px; }}
            QLabel#subtitle, QLabel#muted {{ color: {self.COLORS['muted']}; }}
            QLabel#section {{ font-size: 15pt; font-weight: 700; }}
            QLabel#headline {{ font-size: 19pt; font-weight: 700; }}
            QLabel#state {{ color: {self.COLORS['info']}; font-size: 11pt; font-weight: 700; }}
            QLabel#stepTitle {{ font-size: 11pt; font-weight: 700; }}
            QLabel#stepState {{ color: {self.COLORS['muted']}; font-size: 9pt; }}
            QLabel#stepNumber {{ color: {self.COLORS['muted']}; font-size: 15pt; font-weight: 700; }}
            QLineEdit {{ background: #f2f5f8; color: #17212b; border: 1px solid #91a0ae;
                border-radius: 4px; padding: 6px 9px; min-height: 22px; }}
            QSpinBox {{ background: #f2f5f8; color: #17212b; border: 1px solid #91a0ae;
                border-radius: 4px; padding: 6px 9px; min-height: 22px; }}
            QComboBox {{ background: #101923; color: {self.COLORS['text']}; border: 1px solid #4d697d;
                border-radius: 4px; padding: 6px 9px; min-height: 22px; }}
            QComboBox::drop-down {{ width: 30px; border: 0; border-left: 1px solid #4d697d; }}
            QComboBox:hover {{ background: #192735; border-color: {self.COLORS['info']}; }}
            QComboBox:disabled {{ background: #18232d; color: #8796a3; border-color: #3c4b58; }}
            QComboBox QAbstractItemView {{ background: #101923; color: {self.COLORS['text']};
                border: 1px solid #4d697d; outline: 0; selection-background-color: {self.COLORS['accent']};
                selection-color: #ffffff; }}
            QComboBox QAbstractItemView::item {{ min-height: 28px; padding: 4px 9px; color: {self.COLORS['text']};
                background: #101923; }}
            QComboBox QAbstractItemView::item:hover {{ background: #244052; color: #ffffff; }}
            QComboBox QAbstractItemView::item:selected {{ background: {self.COLORS['accent']}; color: #ffffff; }}
            QComboBox QAbstractItemView::item:disabled {{ background: #18232d; color: #7f8d99; }}
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 2px solid {self.COLORS['info']}; }}
            QCheckBox {{ spacing: 9px; font-weight: 600; }}
            QCheckBox::indicator {{ width: 21px; height: 21px; border: 1px solid #6d8391; background: #0d141c; border-radius: 3px; }}
            QCheckBox::indicator:checked {{ background: {self.COLORS['success']}; border-color: {self.COLORS['success']}; }}
            QTabWidget::pane {{ border: 0; }}
            QTabBar::tab {{ background: {self.COLORS['surface_alt']}; color: {self.COLORS['muted']};
                border: 1px solid {self.COLORS['border']}; border-bottom: 0; padding: 9px 18px;
                font-weight: 600; }}
            QTabBar::tab:selected {{ background: {self.COLORS['surface']}; color: {self.COLORS['text']}; }}
            QPlainTextEdit {{ background: #0d1219; color: #d8e4ef; border: 1px solid {self.COLORS['border']};
                border-radius: 4px; padding: 8px; }}
            QTableWidget {{ background: #0d141c; alternate-background-color: #121d28; border: 1px solid #263f50;
                border-radius: 4px; gridline-color: #263f50; }}
            QHeaderView::section {{ background: {self.COLORS['surface_alt']}; color: {self.COLORS['muted']};
                border: 0; border-bottom: 1px solid {self.COLORS['border']}; padding: 7px; font-weight: 700; }}
            QPushButton {{ background: {self.COLORS['surface_alt']}; color: {self.COLORS['text']};
                border: 1px solid {self.COLORS['border']}; border-radius: 4px; padding: 9px 13px;
                font-weight: 600; }}
            QPushButton:hover {{ background: {self.COLORS['border']}; }}
            QPushButton#primary {{ background: {self.COLORS['accent']}; border-color: {self.COLORS['accent']}; color: white; }}
            QPushButton#primary:hover {{ background: {self.COLORS['accent_active']}; }}
            QPushButton:disabled {{ color: #8190a0; background: #293544; }}
            QProgressBar {{ border: 0; background: {self.COLORS['surface_alt']}; border-radius: 4px; height: 8px; }}
            QProgressBar::chunk {{ background: {self.COLORS['accent']}; border-radius: 4px; }}
        """)

    def _load_icon(self) -> None:
        icon = self.controller.client_dir / "doom_logo.png"
        if icon.is_file():
            self.setWindowIcon(QIcon(str(icon)))

    @staticmethod
    def _label(text: str, object_name: str = "", *, rich_text: bool = False) -> QLabel:
        label = QLabel(text)
        label.setObjectName(object_name)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText if rich_text else Qt.TextFormat.PlainText)
        label.setOpenExternalLinks(False)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    @staticmethod
    def _format_markdown(text: object) -> str:
        """Small safe subset for APWorld option descriptions."""
        escaped = html.escape(str(text), quote=False)
        paragraphs = re.split(r"\n\s*\n", escaped)
        formatted: list[str] = []
        for paragraph in paragraphs:
            paragraph = paragraph.replace("\n", "<br>")
            paragraph = re.sub(r"(\*\*|__)(.+?)\1", r"<b>\2</b>", paragraph)
            paragraph = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", paragraph)
            paragraph = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<i>\1</i>", paragraph)
            formatted.append(f"<p>{paragraph}</p>")
        return "".join(formatted)

    @staticmethod
    def _surface() -> QFrame:
        frame = QFrame()
        frame.setObjectName("surface")
        return frame

    def _build(self) -> None:
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setMinimumWidth(0)
        outer = QVBoxLayout(body)
        outer.setContentsMargins(28, 24, 28, 28)
        outer.setSpacing(14)
        scroll.setWidget(body)
        self.tabs.addTab(scroll, "Setup")

        header = self._surface()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(24, 20, 24, 20)
        header_text = QVBoxLayout()
        header_text.addWidget(self._label("DOOM Eternal", "title"))
        header_text.addWidget(self._label("Archipelago Launcher", "subtitle"))
        header_layout.addLayout(header_text, 1)
        self.overall_state.setObjectName("state")
        self.overall_state.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self.overall_state.setMinimumWidth(0)
        header_layout.addWidget(self.overall_state, 1, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        outer.addWidget(header)

        steps = QGridLayout()
        steps.setHorizontalSpacing(7)
        steps.setVerticalSpacing(7)
        for index, (title, state) in enumerate((("Configure", "Game paths"), ("Connect", "Room details"),
                                                  ("Prepare", "Build mod"), ("Install", "Apply mod"),
                                                  ("Play", "Start via Steam")), start=1):
            card = QFrame()
            card.setObjectName("card")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(10, 8, 10, 8)
            layout.setSpacing(1)
            number = self._label(str(index), "stepNumber")
            title_label = self._label(title, "stepTitle")
            state_label = self._label(state, "stepState")
            layout.addWidget(number)
            layout.addWidget(title_label)
            layout.addWidget(state_label)
            steps.addWidget(card, 0, index - 1)
            steps.setColumnStretch(index - 1, 1)
            self._step_widgets.append((card, number, state_label))
        outer.addLayout(steps)

        content = QHBoxLayout()
        content.setSpacing(14)
        outer.addLayout(content)
        configure = self._surface()
        configure_layout = QGridLayout(configure)
        configure_layout.setContentsMargins(22, 20, 22, 22)
        configure_layout.setHorizontalSpacing(12)
        configure_layout.setVerticalSpacing(10)
        configure_layout.setColumnMinimumWidth(0, 132)
        configure_layout.setColumnStretch(1, 1)
        self.game_root.setMinimumWidth(180)
        self.saves_root.setMinimumWidth(180)
        configure_layout.addWidget(self._label("1. Configure game", "section"), 0, 0, 1, 3)
        configure_layout.addWidget(self._label("Confirm detected folders, then enter room details.", "muted"), 1, 0, 1, 3)
        self._path_row(configure_layout, 2, "DOOM Eternal folder", self.game_root, self._browse_game)
        self._path_row(configure_layout, 3, "Save folder", self.saves_root, self._browse_saves)
        configure_layout.addWidget(self._label("2. Connect to Archipelago", "section"), 5, 0, 1, 3)
        configure_layout.addWidget(self._label("Use address and slot from your room.", "muted"), 6, 0, 1, 3)
        self._entry_row(configure_layout, 7, "Server address", self.server)
        self._entry_row(configure_layout, 8, "Slot name", self.slot)
        self._entry_row(configure_layout, 9, "Password", self.password)
        content.addWidget(configure, 3)

        status = self._surface()
        status_layout = QVBoxLayout(status)
        status_layout.setContentsMargins(22, 20, 22, 22)
        status_layout.setSpacing(10)
        self.headline.setObjectName("headline")
        status_layout.addWidget(self.headline)
        self.detail.setObjectName("muted")
        status_layout.addWidget(self.detail)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        status_layout.addWidget(self.progress)
        self.primary_button = QPushButton(self.next_action)
        self.primary_button.setObjectName("primary")
        self.primary_button.clicked.connect(self._primary_action)
        status_layout.addWidget(self.primary_button)
        self.stop_button = QPushButton("Stop connection")
        self.stop_button.clicked.connect(self._disconnect)
        self.stop_button.setVisible(False)
        status_layout.addWidget(self.stop_button)
        self.reinstall_button = QPushButton("Reinstall mod")
        self.reinstall_button.clicked.connect(self._reinstall)
        self.reinstall_button.setVisible(False)
        status_layout.addWidget(self.reinstall_button)
        self.guidance = QFrame()
        self.guidance.setObjectName("card")
        guidance_layout = QVBoxLayout(self.guidance)
        guidance_layout.addWidget(self._label("Finish installation", "section"))
        self.install_guidance.setObjectName("muted")
        guidance_layout.addWidget(self.install_guidance)
        actions = QHBoxLayout()
        yes = QPushButton("Yes, finish")
        yes.setObjectName("primary")
        yes.clicked.connect(lambda: self._confirm_windows(True))
        no = QPushButton("No, there was a problem")
        no.clicked.connect(lambda: self._confirm_windows(False))
        actions.addWidget(yes)
        actions.addWidget(no)
        guidance_layout.addLayout(actions)
        self.guidance.setVisible(False)
        status_layout.addWidget(self.guidance)
        status_layout.addWidget(self._label("Launcher log", "section"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("monospace", 11))
        self.log.setMinimumHeight(120)
        status_layout.addWidget(self.log, 1)
        command = QHBoxLayout()
        self.command = QLineEdit()
        self.command.setPlaceholderText("Send chat command")
        self.command.returnPressed.connect(self._send_command)
        command.addWidget(self.command, 1)
        send = QPushButton("Send chat")
        send.clicked.connect(self._send_command)
        command.addWidget(send)
        status_layout.addLayout(command)
        content.addWidget(status, 3)
        configure.setMinimumWidth(470)
        status.setMinimumWidth(0)

        launch = self._surface()
        launch_layout = QGridLayout(launch)
        launch_layout.setContentsMargins(18, 10, 18, 10)
        launch_layout.setColumnStretch(0, 1)
        launch_layout.addWidget(self._label("Steam launch option", "section"), 0, 0, 1, 2)
        launch_layout.addWidget(self._label("Copy this on Linux; launcher never edits Steam settings.", "muted"), 1, 0, 1, 2)
        self.launch_option = QLineEdit("Unavailable until setup completes.")
        self.launch_option.setReadOnly(True)
        launch_layout.addWidget(self.launch_option, 2, 0)
        copy_button = QPushButton("Copy option")
        copy_button.clicked.connect(self._copy_launch_option)
        launch_layout.addWidget(copy_button, 2, 1)
        outer.addWidget(launch)

        self._set_step(1)
        self._build_options_tab()

    def _build_options_tab(self) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setMinimumWidth(0)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(14)
        scroll.setWidget(body)
        self.tabs.addTab(scroll, "Options")

        header = self._surface()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(24, 20, 24, 20)
        header_layout.addWidget(self._label("Room Options", "title"))
        header_layout.addWidget(self._label("Choose options for a player YAML file used when creating a future room.", "muted"))
        self.connected_options_notice = self._label(
            "Connected room settings are server-authoritative. Local edits affect only a YAML file for future room generation.", "muted"
        )
        self.connected_options_notice.setVisible(False)
        header_layout.addWidget(self.connected_options_notice)
        layout.addWidget(header)

        player_card = self._surface()
        player_layout = QVBoxLayout(player_card)
        player_layout.setContentsMargins(22, 18, 22, 20)
        player_layout.addWidget(self._label("Player Name", "section"))
        player_layout.addWidget(self._label("Used for the suggested YAML filename.", "muted"))
        self.player_name = QLineEdit("Player")
        player_layout.addWidget(self.player_name)
        layout.addWidget(player_card)

        layout.addWidget(self._start_inventory_widget())

        options = cast(list[dict[str, object]], self.controller.options_schema["options"])
        groups: dict[str, list[dict[str, object]]] = {}
        for option in options:
            groups.setdefault(str(option.get("group") or "Options"), []).append(option)
        for group, options in groups.items():
            card = self._surface()
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(22, 18, 22, 20)
            card_layout.setSpacing(10)
            card_layout.addWidget(self._label(group, "section"))
            for option in options:
                card_layout.addWidget(self._option_widget(option))
            layout.addWidget(card)

        actions = QHBoxLayout()
        reset = QPushButton("Reset to Defaults")
        reset.clicked.connect(self._reset_options)
        actions.addWidget(reset)
        actions.addStretch(1)
        save = QPushButton("Save Player YAML…")
        save.setObjectName("primary")
        save.clicked.connect(self._save_player_options)
        actions.addWidget(save)
        layout.addLayout(actions)
        layout.addStretch(1)

    def _start_inventory_widget(self) -> QWidget:
        card = self._surface()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(22, 18, 22, 20)
        card_layout.setSpacing(10)
        card_layout.addWidget(self._label("Starting Inventory", "section"))
        card_layout.addWidget(self._label(
            "Add persistent progression or useful items. Quantities are written as the common Archipelago start_inventory map.",
            "muted",
        ))
        try:
            self.start_inventory_catalog = load_start_inventory_catalog(
                self.controller.client_dir / "data" / "item_classifications.json"
            )
        except ValueError as error:
            self._append_log(f"Starting inventory catalog unavailable: {error}")
            self.start_inventory_catalog = []

        controls = QHBoxLayout()
        self.start_inventory_selector = QComboBox()
        for item in self.start_inventory_catalog:
            self.start_inventory_selector.addItem(item["label"], item["name"])
        self.start_inventory_selector.setMinimumWidth(250)
        self.start_inventory_quantity = QSpinBox()
        self.start_inventory_quantity.setRange(1, 9999)
        self.start_inventory_quantity.setValue(1)
        self.start_inventory_quantity.setPrefix("Qty: ")
        add = QPushButton("Add item")
        add.setObjectName("primary")
        add.clicked.connect(self._add_start_inventory_item)
        enabled = bool(self.start_inventory_catalog)
        self.start_inventory_selector.setEnabled(enabled)
        self.start_inventory_quantity.setEnabled(enabled)
        add.setEnabled(enabled)
        controls.addWidget(self.start_inventory_selector, 1)
        controls.addWidget(self.start_inventory_quantity)
        controls.addWidget(add)
        card_layout.addLayout(controls)

        if not enabled:
            card_layout.addWidget(self._label(
                "No safe item catalog is available. Starting inventory stays empty rather than offering unverified items.",
                "muted",
            ))

        self.start_inventory_table = QTableWidget(0, 2)
        self.start_inventory_table.setHorizontalHeaderLabels(["Item", "Quantity"])
        self.start_inventory_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.start_inventory_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.start_inventory_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.start_inventory_table.setAlternatingRowColors(True)
        self.start_inventory_table.verticalHeader().setVisible(False)
        self.start_inventory_table.horizontalHeader().setStretchLastSection(False)
        self.start_inventory_table.horizontalHeader().setStretchLastSection(False)
        self.start_inventory_table.setColumnWidth(1, 105)
        self.start_inventory_table.setMinimumHeight(126)
        card_layout.addWidget(self.start_inventory_table)
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self._remove_start_inventory_item)
        card_layout.addWidget(remove, alignment=Qt.AlignmentFlag.AlignRight)
        return card

    def _add_start_inventory_item(self) -> None:
        name = self.start_inventory_selector.currentData()
        if not isinstance(name, str) or not name:
            return
        quantity = self.start_inventory_quantity.value()
        for row in range(self.start_inventory_table.rowCount()):
            item = self.start_inventory_table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == name:
                existing = self.start_inventory_table.item(row, 1)
                current = int(existing.text()) if existing is not None else 0
                self.start_inventory_table.setItem(row, 1, QTableWidgetItem(str(current + quantity)))
                self.start_inventory_table.selectRow(row)
                return
        row = self.start_inventory_table.rowCount()
        self.start_inventory_table.insertRow(row)
        item = QTableWidgetItem(name)
        item.setData(Qt.ItemDataRole.UserRole, name)
        self.start_inventory_table.setItem(row, 0, item)
        self.start_inventory_table.setItem(row, 1, QTableWidgetItem(str(quantity)))
        self.start_inventory_table.selectRow(row)

    def _remove_start_inventory_item(self) -> None:
        row = self.start_inventory_table.currentRow()
        if row >= 0:
            self.start_inventory_table.removeRow(row)

    def _start_inventory_values(self) -> dict[str, int]:
        values: dict[str, int] = {}
        for row in range(self.start_inventory_table.rowCount()):
            item = self.start_inventory_table.item(row, 0)
            quantity = self.start_inventory_table.item(row, 1)
            if item is not None and quantity is not None:
                values[str(item.data(Qt.ItemDataRole.UserRole))] = int(quantity.text())
        return values

    def _option_widget(self, option: dict[str, object]) -> QWidget:
        key = str(option["key"])
        default = option.get("default")
        self.option_defaults[key] = default
        row = QFrame()
        row.setObjectName("optionCard")
        layout = QGridLayout(row)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setHorizontalSpacing(18)
        layout.setVerticalSpacing(5)
        layout.setColumnStretch(0, 1)
        name = self._label(str(option["display_name"]))
        name.setStyleSheet("font-weight: 700;")
        layout.addWidget(name, 0, 0)
        layout.addWidget(
            self._label(self._format_markdown(option["description"]), "muted", rich_text=True), 1, 0
        )
        ui_type = str(option["ui_type"])
        if ui_type == "toggle":
            control: QWidget = QCheckBox("Enabled")
            control.setChecked(bool(default))
        elif ui_type == "choice":
            control = QComboBox()
            for choice in cast(list[object], option["choices"]):
                if isinstance(choice, dict):
                    choice_key = choice.get("key")
                    choice_name = choice.get("label", choice_key)
                else:
                    choice_key = choice
                    choice_name = choice
                control.addItem(str(choice_name), choice_key)
            index = control.findData(default)
            control.setCurrentIndex(index if index >= 0 else 0)
        else:
            control = QSpinBox()
            control.setRange(cast(int, option["minimum"]), cast(int, option["maximum"]))
            control.setValue(cast(int, default))
        self.option_controls[key] = control
        control.setMinimumWidth(170)
        layout.addWidget(control, 0, 1, 2, 1, Qt.AlignmentFlag.AlignVCenter)
        default_text = "Enabled" if default is True else "Disabled" if default is False else str(default)
        default_label = self._label(f"Default: {default_text}", "muted")
        default_label.setStyleSheet("font-size: 9pt;")
        layout.addWidget(default_label, 2, 0, 1, 2)
        return row

    def _reset_options(self) -> None:
        for key, control in self.option_controls.items():
            default = self.option_defaults[key]
            if isinstance(control, QCheckBox):
                control.setChecked(bool(default))
            elif isinstance(control, QComboBox):
                control.setCurrentIndex(max(0, control.findData(default)))
            elif isinstance(control, QSpinBox):
                control.setValue(cast(int, default))
        self.start_inventory_table.setRowCount(0)

    def _save_player_options(self) -> None:
        player_name = self.player_name.text()
        try:
            suggested = suggested_yaml_filename(player_name)
        except ValueError as error:
            self._append_log(f"Player YAML save error: {error}")
            QMessageBox.critical(self, "Could not save Player YAML", str(error))
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "Save Player YAML", suggested, "YAML files (*.yaml);;All files (*)"
        )
        if not filename:
            return
        path = Path(filename)
        if path.suffix.lower() not in {".yaml", ".yml"}:
            path = path.with_suffix(".yaml")
        values: dict[str, object] = {}
        for key, control in self.option_controls.items():
            if isinstance(control, QCheckBox):
                values[key] = control.isChecked()
            elif isinstance(control, QComboBox):
                values[key] = control.currentData()
            elif isinstance(control, QSpinBox):
                values[key] = control.value()
        values["start_inventory"] = self._start_inventory_values()
        try:
            saved = self.controller.save_player_options(path, player_name, values)
        except Exception as error:
            self._append_log(f"Player YAML save error: {error}")
            QMessageBox.critical(self, "Could not save Player YAML", str(error))
            return
        self._append_log(f"Player YAML saved: {saved}")
        QMessageBox.information(self, "Player YAML saved", f"Saved player options to:\n{saved}")

    def _entry_row(self, layout: QGridLayout, row: int, label: str, field: QLineEdit) -> None:
        layout.addWidget(self._label(label), row, 0)
        layout.addWidget(field, row, 1, 1, 2)

    def _path_row(self, layout: QGridLayout, row: int, label: str, field: QLineEdit, callback) -> None:
        layout.addWidget(self._label(label), row, 0)
        layout.addWidget(field, row, 1)
        browse = QPushButton("Browse")
        browse.clicked.connect(callback)
        layout.addWidget(browse, row, 2)

    def _set_step(self, active: int, *, complete_through: int = 0) -> None:
        for index, (_card, number, state) in enumerate(self._step_widgets, start=1):
            if index == 5 and complete_through >= 5:
                color, text = self.COLORS["success"], "Ready"
            elif index <= complete_through:
                color, text = self.COLORS["success"], "Complete"
            elif index == active:
                color, text = self.COLORS["accent_active"], "In progress"
            else:
                color, text = self.COLORS["muted"], ("Next" if index == active + 1 else "Waiting")
            number.setStyleSheet(f"color: {color};")
            state.setStyleSheet(f"color: {color};")
            state.setText(text)

    def _set_state(self, headline: str, detail: str, *, action: str, step: int, complete: int = 0,
                   busy: bool = False, state: str = "IN PROGRESS") -> None:
        self.headline.setText(headline)
        self.detail.setText(detail)
        self.next_action = action
        self.primary_button.setText(action)
        self.overall_state.setText(state)
        self._set_step(step, complete_through=complete)
        self.progress.setVisible(busy)
        self.primary_button.setEnabled(not busy)

    def _set_connection_controls(self, editable: bool, stop_visible: bool, *, stop_enabled: bool = True) -> None:
        """Keep room lifecycle visible without changing controller contracts."""
        for field in (self.server, self.slot, self.password, self.game_root, self.saves_root):
            field.setEnabled(editable)
        self.stop_button.setVisible(stop_visible)
        self.stop_button.setEnabled(stop_enabled)

    def _discover(self) -> None:
        found = self.controller.discover()
        if not self.game_root.text() and found.get("game_root"):
            self.game_root.setText(str(found["game_root"]))
        if not self.saves_root.text() and found.get("save_games_dir"):
            self.saves_root.setText(str(found["save_games_dir"]))
        if found.get("ambiguous_game_roots"):
            self._set_state("Choose your DOOM Eternal folder.", "More than one installation was found.", action="Review game folder", step=1, state="ACTION NEEDED")
        elif self.game_root.text() and self.saves_root.text():
            self._set_state("Game folders detected.", "Enter your room details, then connect.", action="Connect to Archipelago", step=1, state="READY")
        else:
            self._set_state("Choose your game folders.", "Select DOOM Eternal and its save folder before connecting.", action="Review game folders", step=1, state="ACTION NEEDED")

    def _browse_game(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Select the DOOM Eternal folder")
        if selected:
            self.game_root.setText(selected)

    def _browse_saves(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Select the DOOM Eternal save folder")
        if selected:
            self.saves_root.setText(selected)

    def _primary_action(self) -> None:
        if "Retry connection" in self.next_action:
            self._connect()
        elif "Try again" in self.next_action:
            self._retry()
        elif "Prepare" in self.next_action:
            try:
                if not self.controller.prepare_setup():
                    self._append_log("No connected room is available for setup.")
            except Exception as error:
                self._append_log(f"Setup error: {error}")
        elif "Steam" in self.next_action:
            self._append_log("Start DOOM Eternal through Steam when ready. The launcher will not start the game.")
        elif "Connect" in self.next_action or "Review" in self.next_action:
            self._connect()

    def _connect(self) -> None:
        try:
            self.controller.connect(endpoint=self.server.text(), slot=self.slot.text(), password=self.password.text(), game_root=self.game_root.text(), saves_root=self.saves_root.text())
            self._connection_pending = True
            self._set_connection_controls(False, True)
            self._set_state("Connecting to your Archipelago room…", "The launcher is waiting for room information.", action="Connecting…", step=2, complete=1, busy=True, state="CONNECTING")
        except Exception as error:
            self._connection_pending = False
            self._set_connection_controls(True, False)
            self._set_state("Check your connection details.", str(error), action="Retry connection", step=2, complete=1, state="CONNECTION FAILED")
            self._append_log(f"Connection error: {error}")

    def _disconnect(self) -> None:
        try:
            self._set_connection_controls(False, True, stop_enabled=False)
            self._set_state("Stopping connection…", "Waiting for the bridge worker to close.", action="Stopping…", step=2, complete=1, busy=True, state="DISCONNECTING")
            self.controller.disconnect()
        except Exception as error:
            self._append_log(f"Stop error: {error}")

    def _retry(self) -> None:
        if not self.controller.retry_setup():
            self._append_log("Connect to a room before retrying setup.")

    def _reinstall(self) -> None:
        try:
            if not self.controller.reinstall_setup():
                self._append_log("Connect to a room before reinstalling the mod.")
        except Exception as error:
            self._append_log(f"Reinstall error: {error}")

    def _confirm_windows(self, succeeded: bool) -> None:
        try:
            self.controller.confirm_windows_installation(succeeded)
        except Exception as error:
            self._append_log(f"Confirmation error: {error}")

    def _copy_launch_option(self) -> None:
        value = self.launch_option.text()
        if value and not value.startswith("Unavailable"):
            QApplication.clipboard().setText(value)
            self._append_log("Steam launch option copied. Paste it in Steam manually.")

    def _send_command(self) -> None:
        text = self.command.text().strip()
        if not text:
            return
        try:
            self.controller.send_command(text)
            self.command.clear()
        except Exception as error:
            self._append_log(f"Chat error: {error}")

    def _poll_events(self) -> None:
        while True:
            try:
                event = self.controller.events.get_nowait()
            except queue.Empty:
                break
            self.controller.process_event(event)
            self._handle_event(event)

    def _handle_event(self, event: dict[str, object]) -> None:
        kind = str(event.get("type", ""))
        if kind == "log":
            self._append_log(str(event.get("message", "")))
        elif kind == "warning":
            self._append_log(f"Warning: {event.get('message', '')}")
            if event.get("field") == "steam_launch_options":
                self.launch_option.setText("Unavailable — see log.")
        elif kind in {"client_started", "connecting"}:
            self._connection_pending = True
            self._set_connection_controls(False, True)
            self._set_state("Connecting to your Archipelago room…", "The launcher is waiting for room information.", action="Connecting…", step=2, complete=1, busy=True, state="CONNECTING")
        elif kind == "connected":
            self._connection_pending = False
            self._room_connected = True
            self._set_connection_controls(False, True)
            self.connected_options_notice.setVisible(True)
            self._set_state("Connected. Checking installed mod.", "Verifying room identity and installed package before any setup.", action="Checking…", step=3, complete=2, busy=True, state="CONNECTED")
        elif kind == "room_install_state":
            self.progress.setVisible(False)
            if event.get("steam_launch_option"):
                self.launch_option.setText(str(event["steam_launch_option"]))
            if event.get("state") == "already_installed":
                self.reinstall_button.setVisible(True)
                self._set_state("Mod for this room is already installed.", "Room identity and installed package hash match. No reinjection is needed.", action="Ready — start via Steam", step=5, complete=5, state="READY TO PLAY")
                self._append_log("Current room mod is already installed; automatic reinstall skipped.")
            else:
                self.reinstall_button.setVisible(False)
                reason = str(event.get("reason") or "no matching verified install was found")
                self._set_state("Room connected. Mod needs installation.", f"Prepare the room-specific mod before starting DOOM Eternal. ({reason})", action="Prepare and install mod", step=3, complete=2, state="CONNECTED")
                self._append_log("Connected room requires explicit Prepare and install.")
        elif kind == "disconnected":
            self._connection_pending = False
            self._room_connected = False
            self._set_connection_controls(True, False)
            self.connected_options_notice.setVisible(False)
            self.reinstall_button.setVisible(False)
            self._set_state("Connection stopped.", "You can update details and reconnect when ready.", action="Connect to Archipelago", step=2, complete=1, state="DISCONNECTED")
        elif kind == "player_yaml_saved":
            pass
        elif kind in {"setup_started", "mod_building"}:
            self.reinstall_button.setVisible(False)
            self._set_state("Preparing your mod…", "Creating the room-specific mod package.", action="Preparing…", step=3, complete=2, busy=True)
        elif kind == "room_validated":
            self._append_log(f"Room validated. Dash randomization: {bool(event.get('randomize_dash'))}.")
        elif kind == "mod_staged":
            self._set_state("Mod ready. Installing next…", "The room-specific ZIP is staged safely in DOOM Eternal/Mods.", action="Installing…", step=4, complete=3, busy=True)
            self._append_log(f"Staged mod: {event.get('path', '')}")
        elif kind == "dependency_consent_required":
            accepted = QMessageBox.question(self, "Download required tool", f"Download {event.get('name')} {event.get('version')}?\n\nThe download is verified before use.") == QMessageBox.StandardButton.Yes
            self.controller.resolve_consent(str(event.get("request_id", "")), accepted)
            self._append_log("Tool download approved." if accepted else "Tool download declined.")
        elif kind == "dependency_ready":
            self._append_log(f"Tool ready: {event.get('name')} {event.get('version')}")
        elif kind == "injector_started":
            self._set_state("Installing the mod automatically…", "EternalModInjectorShell is working. This can take a few minutes.", action="Installing…", step=4, complete=3, busy=True)
            command = event.get("command", [])
            if isinstance(command, (list, tuple)):
                self._append_log("Running: " + " ".join(map(str, command)))
        elif kind == "injector_finished":
            for key in ("stdout", "stderr"):
                if event.get(key):
                    self._append_log(str(event[key]).rstrip())
            self._append_log(f"Injector exit code: {event.get('returncode', 'timeout')}")
        elif kind == "manual_action_required":
            self.install_guidance.setText("EternalModManager is open.\n\n1. Select the DOOM Eternal Archipelago mod.\n2. Choose Run Injector.\n3. Return here and confirm the result.")
            self.guidance.setVisible(True)
            self._set_state("Finish installation in EternalModManager.", "The manager was opened for you. Confirm when its injector is done.", action="Waiting for confirmation", step=4, complete=3, state="ACTION NEEDED")
            self.primary_button.setEnabled(False)
        elif kind == "windows_installation_confirmed":
            if bool(event.get("succeeded")):
                self.guidance.setVisible(False)
                self.reinstall_button.setVisible(True)
                self._set_state("Mod installed successfully.", "Now start DOOM Eternal through Steam. Keep this launcher open while you play.", action="Ready — start via Steam", step=5, complete=5, state="READY TO PLAY")
            else:
                self._set_state("Installation needs another try.", "Review details, then retry setup after fixing the manager issue.", action="Try again", step=4, complete=3, state="ACTION NEEDED")
                self._append_log("User reported EternalModManager installation failure.")
        elif kind == "setup_ready":
            state = str(event.get("adapter_state", ""))
            option = str(event.get("steam_launch_option", ""))
            if option:
                self.launch_option.setText(option)
            if state == "applied":
                self.reinstall_button.setVisible(True)
                self._set_state("Mod installed successfully.", "Now start DOOM Eternal through Steam. Keep this launcher open while you play.", action="Ready — start via Steam", step=5, complete=5, state="READY TO PLAY")
            elif state in {"failed", "timed_out"}:
                self._set_state("Mod installation did not finish.", "Review details, then retry setup. DOOM Eternal was not started.", action="Try again", step=4, complete=3, state="ACTION NEEDED")
            elif state != "manual_action_required":
                self._set_state("Setup needs your attention.", str(event.get("message", "")), action="Try again", step=4, complete=3, state="ACTION NEEDED")
        elif kind in {"setup_failed", "error"}:
            message = str(event.get("message", "Unknown error"))
            connection_failed = kind == "error" and event.get("code") == "connection_failed"
            if connection_failed:
                self._room_connected = False
                self._connection_pending = False
                self._set_connection_controls(True, False)
                self.connected_options_notice.setVisible(False)
                self.reinstall_button.setVisible(False)
                self.guidance.setVisible(False)
                self._set_state("Connection failed.", message, action="Retry connection", step=2, complete=1, state="CONNECTION FAILED")
            elif not self._room_connected:
                self._connection_pending = False
                self._set_connection_controls(True, False)
                self._set_state("Connection failed.", message, action="Retry connection", step=2, complete=1, state="CONNECTION FAILED")
            else:
                self._set_state("Setup could not finish.", "Review details and try again after resolving the issue.", action="Try again", step=4, complete=3, state="ACTION NEEDED")
            self._append_log(f"{kind}: {message}")
        elif kind == "client_stopping":
            self._set_connection_controls(False, True, stop_enabled=False)
            self._set_state("Stopping connection…", "Waiting for the bridge worker to close.", action="Stopping…", step=2, complete=1, busy=True, state="DISCONNECTING")

    def _append_log(self, text: str) -> None:
        if text:
            self.log.appendPlainText(text)

    def closeEvent(self, event) -> None:
        try:
            self.controller.close()
        finally:
            event.accept()

    def run(self) -> None:
        self.show()
        app = QApplication.instance()
        if app is None:
            raise RuntimeError("LauncherUI requires QApplication")
        app.exec()
