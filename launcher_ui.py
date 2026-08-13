"""PySide6 shell for standalone DOOM Eternal Archipelago launcher."""

from __future__ import annotations

import html
import queue
import re
from datetime import datetime
from pathlib import Path
from typing import cast

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QDialog, QFrame, QGridLayout, QInputDialog,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSlider, QSpinBox,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from launcher_controller import LauncherController
from options_foundation import load_start_inventory_catalog, suggested_yaml_filename


class NamedRangeControl(QWidget):
    """Numeric named-range input."""

    def __init__(self, option: dict[str, object]):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.special = QComboBox()
        self.special.addItem("Exact value", None)
        self.special_values: dict[str, object] = {}
        for special in cast(list[dict[str, object]], option["special_values"]):
            key = str(special["key"])
            self.special_values[key] = special.get("value")
            self.special.addItem(str(special["label"]), key)
        layout.addWidget(self.special)
        self.numeric_row = QWidget()
        row = QHBoxLayout(self.numeric_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.spin = QSpinBox()
        self.slider.setRange(cast(int, option["minimum"]), cast(int, option["maximum"]))
        self.spin.setRange(cast(int, option["minimum"]), cast(int, option["maximum"]))
        row.addWidget(self.slider, 1)
        row.addWidget(self.spin)
        layout.addWidget(self.numeric_row)
        self.slider.valueChanged.connect(self.spin.setValue)
        self.spin.valueChanged.connect(self.slider.setValue)
        self.special.currentIndexChanged.connect(self._special_changed)
        self.setValue(option["default"])

    def _special_changed(self, _index: int) -> None:
        key = self.special.currentData()
        if key is None:
            self.numeric_row.show()
            self.slider.setEnabled(True)
            self.spin.setEnabled(True)
            return
        value = self.special_values.get(str(key))
        displayable = isinstance(value, int) and not isinstance(value, bool) and self.slider.minimum() <= value <= self.slider.maximum()
        self.numeric_row.setVisible(displayable)
        if displayable:
            self.spin.setValue(cast(int, value))
        self.slider.setEnabled(False)
        self.spin.setEnabled(False)

    def value(self) -> int | str:
        return self.spin.value() if self.special.currentData() is None else str(self.special.currentData())

    def setValue(self, value: object) -> None:
        if isinstance(value, str):
            index = self.special.findData(value)
            self.special.setCurrentIndex(index if index >= 0 else 0)
        else:
            self.special.setCurrentIndex(0)
            self.spin.setValue(cast(int, value))


class OptionSetControl(QWidget):
    """Schema-driven compact chooser for OptionSet values."""

    def __init__(self, option: dict[str, object]):
        super().__init__()
        self.checks: list[tuple[object, QCheckBox]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        actions = QHBoxLayout()
        select_all = QPushButton("SELECT ALL")
        select_all.clicked.connect(lambda: self._set_all(True))
        clear = QPushButton("CLEAR")
        clear.clicked.connect(lambda: self._set_all(False))
        actions.addWidget(select_all)
        actions.addWidget(clear)
        actions.addStretch(1)
        layout.addLayout(actions)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(5)
        default = option.get("default", [])
        selected = set(default) if isinstance(default, list) else set()
        for index, choice in enumerate(cast(list[object], option.get("choices", []))):
            if not isinstance(choice, dict):
                continue
            key = choice.get("key")
            check = QCheckBox(str(choice.get("label", key)))
            check.setChecked(key in selected)
            grid.addWidget(check, index // 2, index % 2)
            self.checks.append((key, check))
        layout.addLayout(grid)

    def _set_all(self, checked: bool) -> None:
        for _, check in self.checks:
            check.setChecked(checked)

    def value(self) -> list[object]:
        return [key for key, check in self.checks if check.isChecked()]

    def setValue(self, value: object) -> None:
        selected = set(value) if isinstance(value, list) else set()
        for key, check in self.checks:
            check.setChecked(key in selected)


class LauncherUI(QMainWindow):
    """Controller-backed, keyboard-first launcher shell."""

    COLORS = {
        "ink": "#0a1014", "panel": "#111a20", "panel2": "#18252d",
        "line": "#38505b", "text": "#eef4f3", "muted": "#9cacb1",
        "doom": "#e8782f", "doom_hot": "#ff9c4a", "ap": "#42c7c8",
        "good": "#9acd63", "warn": "#f0bd58", "bad": "#ef6262",
    }

    def __init__(self, controller: LauncherController):
        super().__init__()
        self.controller = controller
        self.setWindowTitle("DOOM Eternal Archipelago")
        self.resize(1200, 800)
        self.setMinimumSize(760, 600)
        self.option_controls: dict[str, QWidget] = {}
        self.option_defaults: dict[str, object] = {}
        self.start_inventory_catalog: list[dict[str, str]] = []
        self.room_event: dict[str, object] = {}
        self._room_connected = False
        self._connection_pending = False
        self._configure_style()
        self._build()
        self._load_icon()
        self._install_shortcuts()
        self._discover()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_events)
        self.timer.start(75)

    def _configure_style(self) -> None:
        self.setStyleSheet(f"""
            QWidget {{ background:{self.COLORS['ink']}; color:{self.COLORS['text']}; font-family:'Segoe UI','Aptos','Noto Sans',sans-serif; font-size:10.5pt; }}
            QLabel {{ background:transparent; }} QMainWindow {{ background:{self.COLORS['ink']}; }}
            QFrame#shell {{ background:#0d151a; border-right:1px solid {self.COLORS['line']}; }}
            QFrame#topbar {{ background:#0d151a; border-bottom:1px solid {self.COLORS['line']}; }}
            QFrame#hero {{ background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #16242b,stop:.55 #111b21,stop:1 #0d151a); border:1px solid #45606b; border-left:4px solid {self.COLORS['doom']}; }}
            QFrame#card {{ background:{self.COLORS['panel']}; border:1px solid {self.COLORS['line']}; }}
            QWidget#statusItem {{ background:transparent; border:0; }}
            QLabel#statusIndicator {{ color:#64737a; font-size:12pt; }}
            QWidget#statusItem[statusTone="good"] QLabel#statusIndicator {{ color:{self.COLORS['good']}; }}
            QWidget#statusItem[statusTone="active"] QLabel#statusIndicator {{ color:{self.COLORS['ap']}; }}
            QWidget#statusItem[statusTone="warn"] QLabel#statusIndicator {{ color:{self.COLORS['warn']}; }}
            QWidget#statusItem[statusTone="bad"] QLabel#statusIndicator {{ color:{self.COLORS['bad']}; }}
            QLabel#brand {{ font-family:'Bahnschrift SemiCondensed','Segoe UI',sans-serif; font-size:16pt; font-weight:800; }}
            QLabel#eyebrow {{ color:{self.COLORS['ap']}; font-family:'Bahnschrift SemiCondensed','Segoe UI',sans-serif; font-size:9pt; font-weight:800; letter-spacing:1px; }}
            QLabel#title {{ font-family:'Bahnschrift SemiCondensed','Segoe UI',sans-serif; font-size:23pt; font-weight:800; }}
            QLabel#section {{ font-family:'Bahnschrift SemiCondensed','Segoe UI',sans-serif; font-size:14pt; font-weight:800; }}
            QLabel#muted {{ color:{self.COLORS['muted']}; }} QLabel#state {{ font-weight:800; color:{self.COLORS['good']}; }}
            QLabel#stateDetail {{ color:{self.COLORS['muted']}; font-size:9pt; }}
            QLabel#stateName {{ color:{self.COLORS['muted']}; font-size:9pt; font-weight:800; }}
            QLabel#warning {{ color:{self.COLORS['warn']}; background:#292419; border-left:3px solid {self.COLORS['warn']}; padding:9px; }}
            QLineEdit,QSpinBox {{ background:#0c1419; color:{self.COLORS['text']}; border:1px solid #526871; padding:7px 9px; min-height:20px; }}
            QComboBox {{ background:#0c1419; border:1px solid #526871; padding:7px 9px; min-height:20px; }}
            QComboBox QAbstractItemView {{ background:#101a20; color:{self.COLORS['text']}; selection-background-color:{self.COLORS['doom']}; }}
            QLineEdit:focus,QSpinBox:focus,QComboBox:focus,QPushButton:focus,QCheckBox:focus {{ border:2px solid {self.COLORS['ap']}; }}
            QPushButton {{ background:#17242b; border:1px solid #4a626c; padding:9px 13px; font-family:'Bahnschrift SemiCondensed','Segoe UI',sans-serif; font-weight:800; letter-spacing:.3px; }}
            QPushButton:hover {{ background:#243740; border-color:{self.COLORS['ap']}; }}
            QPushButton#primary {{ background:{self.COLORS['good']}; border-color:{self.COLORS['good']}; color:#102012; }}
            QPushButton#primary:hover {{ background:#b2e77a; }}
            QPushButton#nav {{ text-align:left; background:transparent; border:0; border-left:3px solid transparent; color:{self.COLORS['muted']}; padding:10px 10px; }}
            QPushButton#nav:checked,QPushButton#nav:hover {{ background:#17242b; color:{self.COLORS['text']}; border-left-color:{self.COLORS['doom']}; }}
            QPushButton#sessionNav {{ background:transparent; border:1px solid #405761; padding:6px 10px; color:{self.COLORS['muted']}; }}
            QPushButton#sessionNav:checked,QPushButton#sessionNav:hover {{ color:{self.COLORS['text']}; border-color:{self.COLORS['doom']}; background:#1d2d34; }}
            QPushButton:disabled {{ color:#718087; background:#131e24; border-color:#2d3d44; }}
            QPlainTextEdit,QTableWidget {{ background:#0b1217; color:{self.COLORS['text']}; border:1px solid {self.COLORS['line']}; }}
            QTableWidget {{ gridline-color:#25363e; alternate-background-color:#111c22; }}
            QHeaderView::section {{ background:#16242b; color:{self.COLORS['muted']}; border:0; padding:6px; font-weight:800; }}
            QCheckBox {{ spacing:8px; }} QCheckBox::indicator {{ width:17px; height:17px; border:1px solid #6b7f86; background:#0c1419; }}
            QCheckBox::indicator:checked {{ background:{self.COLORS['good']}; border-color:{self.COLORS['good']}; }}
        """)

    @staticmethod
    def _label(text: str, name: str = "", *, rich: bool = False) -> QLabel:
        label = QLabel(text)
        label.setObjectName(name)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    @staticmethod
    def _scroll(widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(widget)
        return scroll

    @staticmethod
    def _card(name: str = "card") -> QFrame:
        card = QFrame()
        card.setObjectName(name)
        return card

    def _build(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        shell = self._card("shell")
        shell.setMinimumWidth(152)
        shell.setMaximumWidth(196)
        nav = QVBoxLayout(shell)
        nav.setContentsMargins(12, 18, 12, 16)
        nav.setSpacing(5)
        nav.addWidget(self._label("DOOM Eternal\nArchipelago", "brand"))
        nav.addSpacing(22)
        self.nav_buttons: list[QPushButton] = []
        for label, page in (("HOME", 0), ("JOIN ROOM", 1), ("SESSION", 2), ("CREATE PLAYER", 3), ("HELP", 4)):
            button = QPushButton(label)
            button.setObjectName("nav")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, target=page: self._show_page(target))
            nav.addWidget(button)
            self.nav_buttons.append(button)
        nav.addStretch(1)
        layout.addWidget(shell)
        stage = QWidget()
        stage_layout = QVBoxLayout(stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(0)
        topbar = self._card("topbar")
        top = QHBoxLayout(topbar)
        top.setContentsMargins(24, 10, 24, 10)
        self.top_state = self._label("READY TO JOIN", "state")
        self.top_context = self._label("HOME", "eyebrow")
        top.addWidget(self.top_context)
        top.addStretch(1)
        top.addWidget(self.top_state)
        stage_layout.addWidget(topbar)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._home_page())
        self.pages.addWidget(self._join_page())
        self.pages.addWidget(self._session_page())
        self.pages.addWidget(self._create_page())
        self.pages.addWidget(self._doctor_page())
        stage_layout.addWidget(self.pages, 1)
        layout.addWidget(stage, 1)
        self._show_page(0)

    def _home_page(self) -> QScrollArea:
        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(28, 24, 28, 30)
        outer.setSpacing(14)
        hero = self._card("hero")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(24, 20, 24, 20)
        copy = QVBoxLayout()
        self.hero_title = self._label("JOIN A ROOM", "title")
        copy.addWidget(self.hero_title)
        self.hero_detail = self._label("Connect to your Archipelago room and start playing.", "muted")
        self.hero_detail.setWordWrap(True)
        copy.addWidget(self.hero_detail)
        hero_layout.addLayout(copy, 1)
        self.hero_action = QPushButton("JOIN A ROOM")
        self.hero_action.setObjectName("primary")
        self.hero_action.clicked.connect(self._primary_action)
        hero_layout.addWidget(self.hero_action, alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(hero)
        create = self._card()
        create_layout = QHBoxLayout(create)
        create_layout.setContentsMargins(20, 16, 20, 16)
        create_copy = QVBoxLayout()
        create_copy.addWidget(self._label("NEW PLAYER FILE", "section"))
        create_copy.addWidget(self._label("Choose your game settings and save a Player YAML for your next room.", "muted"))
        create_layout.addLayout(create_copy, 1)
        self.join_another_button = QPushButton("JOIN ANOTHER ROOM")
        self.join_another_button.clicked.connect(self._focus_join)
        self.join_another_button.hide()
        create_layout.addWidget(self.join_another_button)
        create_button = QPushButton("CREATE PLAYER YAML")
        create_button.clicked.connect(lambda: self._show_page(3))
        create_layout.addWidget(create_button)
        outer.addWidget(create)
        outer.addStretch(1)
        return self._scroll(body)

    def _status_strip(self) -> QWidget:
        strip = QWidget()
        self.status_layout = QGridLayout(strip)
        self.status_layout.setContentsMargins(0, 0, 0, 0)
        self.status_layout.setHorizontalSpacing(8)
        self.status_layout.setVerticalSpacing(8)
        self.status_strip = strip
        self.status_items: list[QWidget] = []
        self.statuses: dict[str, tuple[QWidget, QLabel, QLabel, QLabel]] = {}
        for index, (key, title) in enumerate((("room", "CONNECTION"), ("mod", "MOD"), ("game", "GAME"), ("rpc", "GAME LINK"))):
            item = QWidget()
            item.setObjectName("statusItem")
            item.setProperty("statusTone", "muted")
            item_layout = QHBoxLayout(item)
            item_layout.setContentsMargins(4, 2, 4, 2)
            item_layout.setSpacing(6)
            indicator = self._label("○", "statusIndicator")
            name = self._label(title, "stateName")
            detail = self._label("WAITING", "stateDetail")
            item_layout.addWidget(indicator)
            item_layout.addWidget(name)
            item_layout.addWidget(self._label("·", "muted"))
            item_layout.addWidget(detail)
            item_layout.addStretch(1)
            self.status_items.append(item)
            self.statuses[key] = (item, indicator, name, detail)
        QTimer.singleShot(0, self._arrange_status_strip)
        return strip

    def _arrange_status_strip(self) -> None:
        columns = 2 if self.status_strip.width() < 700 else 4
        for index, item in enumerate(self.status_items):
            self.status_layout.addWidget(item, index // columns, index % columns)
        for column in range(4):
            self.status_layout.setColumnStretch(column, 1 if column < columns else 0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "status_strip"):
            self._arrange_status_strip()

    def _join_page(self) -> QScrollArea:
        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(28, 24, 28, 30)
        outer.setSpacing(14)
        hero = self._card("hero")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(24, 20, 24, 20)
        hero_layout.addWidget(self._label("JOIN A ROOM", "title"))
        hero_layout.addWidget(self._label("Enter room details. DOOM Eternal location is checked automatically.", "muted"))
        outer.addWidget(hero)
        outer.addWidget(self._join_card())
        outer.addStretch(1)
        return self._scroll(body)

    def _join_card(self) -> QFrame:
        card = self._card()
        layout = QGridLayout(card)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setVerticalSpacing(9)
        layout.setColumnStretch(1, 1)
        layout.addWidget(self._label("ROOM DETAILS", "section"), 0, 0, 1, 3)
        layout.addWidget(self._label("Server, player name, and optional password.", "muted"), 1, 0, 1, 3)
        self.game_root = QLineEdit(str(self.controller.config.get("game_root", "")))
        self.saves_root = QLineEdit(str(self.controller.config.get("save_games_dir", "")))
        self.server = QLineEdit(str(self.controller.config.get("server_address", "")))
        self.slot = QLineEdit(str(self.controller.config.get("slot", "")))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.detected_paths = self._label("Checking for DOOM Eternal…", "muted")
        layout.addWidget(self.detected_paths, 2, 0, 1, 3)
        self.change_paths = QPushButton("CHOOSE GAME FOLDER")
        self.change_paths.clicked.connect(self._toggle_paths)
        layout.addWidget(self.change_paths, 3, 0, 1, 3, Qt.AlignmentFlag.AlignLeft)
        self.game_path_label = self._label("GAME FOLDER", "eyebrow")
        self.save_path_label = self._label("SAVE FOLDER", "eyebrow")
        layout.addWidget(self.game_path_label, 4, 0); layout.addWidget(self.game_root, 4, 1)
        layout.addWidget(self.save_path_label, 5, 0); layout.addWidget(self.saves_root, 5, 1)
        self.game_browse = QPushButton("BROWSE"); self.game_browse.clicked.connect(self._browse_game)
        self.save_browse = QPushButton("BROWSE"); self.save_browse.clicked.connect(self._browse_saves)
        layout.addWidget(self.game_browse, 4, 2); layout.addWidget(self.save_browse, 5, 2)
        self._entry_row(layout, 6, "SERVER", self.server)
        self._entry_row(layout, 7, "PLAYER", self.slot)
        self._entry_row(layout, 8, "PASSWORD", self.password)
        self.join_button = QPushButton("CONNECT")
        self.join_button.setObjectName("primary")
        self.join_button.clicked.connect(self._connect)
        layout.addWidget(self.join_button, 9, 1, 1, 2)
        self._toggle_paths(force=not bool(self.game_root.text() and self.saves_root.text()))
        return card

    def _session_card(self) -> QFrame:
        card = self._card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(9)
        layout.addWidget(self._label("ROOM", "section"))
        self.room_summary = self._label("No room connected.", "muted")
        self.room_summary.setWordWrap(True)
        layout.addWidget(self.room_summary)
        self.room_options = self._label("Room settings appear after connection.", "muted")
        self.room_options.setWordWrap(True)
        layout.addWidget(self.room_options)
        view_options = QPushButton("VIEW ALL SETTINGS")
        view_options.clicked.connect(self._view_room_options)
        layout.addWidget(view_options, alignment=Qt.AlignmentFlag.AlignLeft)
        self.drift = self._label("")
        self.drift.setObjectName("warning")
        self.drift.hide()
        layout.addWidget(self.drift)
        self.launch_option = QLineEdit("Available after setup.")
        self.launch_option.setReadOnly(True)
        layout.addWidget(self._label("STEAM LAUNCH OPTION", "eyebrow"))
        layout.addWidget(self.launch_option)
        copy = QPushButton("COPY OPTION")
        copy.clicked.connect(self._copy_launch_option)
        layout.addWidget(copy, alignment=Qt.AlignmentFlag.AlignLeft)
        controls = QHBoxLayout()
        self.stop_button = QPushButton("DISCONNECT")
        self.stop_button.clicked.connect(self._disconnect)
        self.stop_button.setEnabled(False)
        self.reinstall_button = QPushButton("UPDATE MOD")
        self.reinstall_button.clicked.connect(self._reinstall)
        self.reinstall_button.setEnabled(False)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.reinstall_button)
        controls.addStretch(1)
        layout.addLayout(controls)
        return card

    def _session_page(self) -> QWidget:
        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(28, 24, 28, 30)
        outer.setSpacing(14)
        outer.addWidget(self._status_strip())
        header = QHBoxLayout()
        header.addWidget(self._label("SESSION", "title"))
        header.addStretch(1)
        for text, page in (("ACTIVITY", 0), ("LOG", 1), ("ROOM", 2)):
            button = QPushButton(text)
            button.setObjectName("sessionNav")
            button.setCheckable(True)
            button.setChecked(page == 0)
            button.clicked.connect(lambda checked=False, target=page: self.session_stack.setCurrentIndex(target))
            header.addWidget(button)
        outer.addLayout(header)
        self.session_stack = QStackedWidget()
        self.session_stack.addWidget(self._activity_card())
        self.session_stack.addWidget(self._session_log_page())
        self.session_stack.addWidget(self._session_card())
        outer.addWidget(self.session_stack, 1)
        return body

    def _session_log_page(self) -> QFrame:
        card = self._card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.addWidget(self._label("SESSION LOG", "section"))
        self.session_log = QPlainTextEdit()
        self.session_log.setReadOnly(True)
        self.session_log.setFont(QFont("monospace", 10))
        layout.addWidget(self.session_log, 1)
        return card

    def _activity_card(self) -> QFrame:
        card = self._card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.addWidget(self._label("ACTIVITY", "section"))
        layout.addWidget(self._label("Items, checks, DeathLink, and connection updates. Most recent first.", "muted"))
        self.activity = QTableWidget(0, 3)
        self.activity.setHorizontalHeaderLabels(["TIME", "UPDATE", "DETAILS"])
        self.activity.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.activity.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.activity.setAlternatingRowColors(True)
        self.activity.verticalHeader().hide()
        self.activity.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.activity.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.activity.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.activity.setMinimumHeight(320)
        layout.addWidget(self.activity)
        return card

    def _create_page(self) -> QScrollArea:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(28, 24, 28, 30)
        layout.setSpacing(14)
        header = self._card("hero")
        head = QVBoxLayout(header)
        head.setContentsMargins(24, 20, 24, 20)
        head.addWidget(self._label("CREATE PLAYER YAML", "title"))
        head.addWidget(self._label("Choose your DOOM Eternal settings, Starting Inventory, and save your player file.", "muted"))
        layout.addWidget(header)
        player = self._card()
        player_layout = QVBoxLayout(player)
        player_layout.setContentsMargins(20, 18, 20, 20)
        player_layout.addWidget(self._label("PLAYER", "section"))
        self.player_name = QLineEdit("Player")
        self.player_name.setPlaceholderText("Player name")
        player_layout.addWidget(self.player_name)
        layout.addWidget(player)
        layout.addWidget(self._start_inventory_widget())
        groups: dict[str, list[dict[str, object]]] = {}
        for option in cast(list[dict[str, object]], self.controller.options_schema["options"]):
            groups.setdefault(str(option.get("group") or "OPTIONS"), []).append(option)
        for group, options in groups.items():
            card = self._card()
            group_layout = QVBoxLayout(card)
            group_layout.setContentsMargins(20, 18, 20, 20)
            group_layout.setSpacing(9)
            group_layout.addWidget(self._label(group.upper(), "section"))
            for option in options:
                group_layout.addWidget(self._option_widget(option))
            layout.addWidget(card)
        actions = QHBoxLayout()
        reset = QPushButton("RESET DEFAULTS")
        reset.clicked.connect(self._reset_options)
        save = QPushButton("SAVE PLAYER YAML…")
        save.setObjectName("primary")
        save.clicked.connect(self._save_player_options)
        actions.addWidget(reset)
        actions.addStretch(1)
        actions.addWidget(save)
        layout.addLayout(actions)
        layout.addStretch(1)
        return self._scroll(body)

    def _doctor_page(self) -> QScrollArea:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(28, 24, 28, 30)
        layout.setSpacing(14)
        header = self._card("hero")
        head = QVBoxLayout(header)
        head.setContentsMargins(24, 20, 24, 20)
        head.addWidget(self._label("HELP", "title"))
        head.addWidget(self._label("Check your setup, fix common problems, or create a support report.", "muted"))
        layout.addWidget(header)
        card = self._card()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 18, 20, 20)
        self.doctor_status = self._label("SETUP NOT CHECKED", "state")
        self.doctor_evidence = self._label("Check setup when you need help joining or playing.", "muted")
        self.doctor_evidence.setWordWrap(True)
        self.doctor_action = self._label("", "muted")
        card_layout.addWidget(self.doctor_status)
        card_layout.addWidget(self.doctor_evidence)
        card_layout.addWidget(self.doctor_action)
        buttons = QHBoxLayout()
        for text, callback, primary in (
            ("CHECK SETUP", self._run_doctor, True), ("FIX SETUP", self._preview_repairs, False),
            ("SAVE SUPPORT REPORT", self._save_support_bundle, False),
        ):
            button = QPushButton(text)
            if primary:
                button.setObjectName("primary")
            button.clicked.connect(callback)
            buttons.addWidget(button)
        buttons.addStretch(1)
        card_layout.addLayout(buttons)
        layout.addWidget(card)
        details = self._card()
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(20, 18, 20, 20)
        details_layout.addWidget(self._label("TECHNICAL DETAILS", "section"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("monospace", 10))
        self.log.setPlaceholderText("Setup details appear here when needed.")
        details_layout.addWidget(self.log, 1)
        layout.addWidget(details, 1)
        return self._scroll(body)

    def _install_shortcuts(self) -> None:
        for key, page in (("Alt+H", 0), ("Alt+J", 1), ("Alt+C", 3), ("Alt+D", 4)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(lambda target=page: self._show_page(target))
        primary = QShortcut(QKeySequence("Ctrl+Return"), self)
        primary.activated.connect(self._primary_action)

    def _show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.top_context.setText(("HOME", "JOIN ROOM", "SESSION", "CREATE PLAYER", "HELP")[index])
        for number, button in enumerate(self.nav_buttons):
            button.setChecked(number == index)

    def _set_status(self, key: str, detail: str, color: str) -> None:
        item, indicator, _name, label = self.statuses[key]
        tone = {
            self.COLORS["good"]: "good", self.COLORS["ap"]: "active",
            self.COLORS["warn"]: "warn", self.COLORS["bad"]: "bad",
        }.get(color, "muted")
        item.setProperty("statusTone", tone)
        item.style().unpolish(item)
        item.style().polish(item)
        icon = "●" if color == self.COLORS["good"] else "◆" if color == self.COLORS["ap"] else "!" if color in (self.COLORS["warn"], self.COLORS["bad"]) else "○"
        indicator.setText(icon)
        label.setText(detail.upper())

    def _set_home(self, title: str, detail: str, action: str, state: str, *, enabled: bool = True) -> None:
        self.hero_title.setText(title)
        self.hero_detail.setText(detail)
        self.hero_action.setText(action)
        self.hero_action.setEnabled(enabled)
        self.top_state.setText(state)

    def _set_home_secondary_actions(self, resumable: bool) -> None:
        self.join_another_button.setVisible(resumable)

    def _set_connection_controls(self, editable: bool) -> None:
        for field in (self.game_root, self.saves_root, self.server, self.slot, self.password, self.join_button):
            field.setEnabled(editable)
        self.stop_button.setEnabled(not editable and (self._room_connected or self._connection_pending))

    def _discover(self) -> None:
        found = self.controller.discover()
        if not self.game_root.text() and found.get("game_root"):
            self.game_root.setText(str(found["game_root"]))
        if not self.saves_root.text() and found.get("save_games_dir"):
            self.saves_root.setText(str(found["save_games_dir"]))
        has_session = bool(self.controller.config.get("server_address") and self.controller.config.get("slot"))
        self._set_home_secondary_actions(has_session)
        if self.game_root.text() and self.saves_root.text():
            self.detected_paths.setText("DOOM Eternal found")
            self._set_home(
                "WELCOME BACK" if has_session else "JOIN A ROOM",
                "Reconnect to your saved room and continue playing." if has_session else "Connect to your Archipelago room and start playing.",
                "RESUME" if has_session else "JOIN A ROOM",
                "READY",
            )
        else:
            self.detected_paths.setText("DOOM Eternal needs a game folder")
            self._set_home(
                "WELCOME BACK" if has_session else "JOIN A ROOM",
                "Reconnect to your saved room and continue playing." if has_session else "Connect to your Archipelago room and start playing.",
                "RESUME" if has_session else "JOIN A ROOM",
                "ACTION NEEDED",
            )
        for key in self.statuses:
            self._set_status(key, "waiting", self.COLORS["muted"])
        if not self.start_inventory_catalog:
            self._append_log("Starting Inventory choices unavailable. Open Help and run Check Setup.")

    def _focus_join(self) -> None:
        self._show_page(1)
        self.server.setFocus(Qt.FocusReason.OtherFocusReason)

    def _resume(self) -> None:
        if self._room_connected:
            self._show_page(2)
            return
        endpoint, slot = str(self.controller.config.get("server_address", "")), str(self.controller.config.get("slot", ""))
        if not endpoint or not slot:
            self._focus_join()
            return
        password, accepted = QInputDialog.getText(self, "Resume session", f"Password for {slot} if room requires one:", QLineEdit.EchoMode.Password)
        if not accepted:
            return
        self.server.setText(endpoint); self.slot.setText(slot); self.password.setText(password)
        self._connect()

    def _primary_action(self) -> None:
        text = self.hero_action.text()
        if "JOIN" in text or "RETRY" in text:
            self._focus_join()
        elif "RESUME" in text:
            self._resume()
        elif "UPDATE" in text or "PREPARE" in text or "TRY AGAIN" in text:
            self._prepare(force="UPDATE" in text or "TRY AGAIN" in text)
        elif "PLAY" in text:
            self._launch_game()

    def _connect(self) -> None:
        try:
            self.controller.connect(endpoint=self.server.text(), slot=self.slot.text(), password=self.password.text(), game_root=self.game_root.text(), saves_root=self.saves_root.text())
        except Exception as error:
            self._set_home("CHECK ROOM DETAILS", str(error), "JOIN ROOM", "CONNECTION FAILED")
            self._activity_event({"type": "connection_input_error", "message": str(error)})
            return
        self._connection_pending = True
        self._set_connection_controls(False)
        self._set_status("room", "connecting", self.COLORS["ap"])
        self._show_page(2)
        self._set_home("CONNECTING TO ROOM", "Waiting for authoritative room data.", "RESUME SESSION", "CONNECTING", enabled=False)

    def _toggle_paths(self, _checked: bool = False, *, force: bool | None = None) -> None:
        show = force if force is not None else not self.game_root.isVisible()
        for widget in (self.game_root, self.saves_root, self.game_path_label, self.save_path_label, self.game_browse, self.save_browse):
            widget.setVisible(show)
        self.change_paths.setText("HIDE FOLDERS" if show else "CHOOSE GAME FOLDER")

    def _disconnect(self) -> None:
        try:
            self.controller.disconnect()
            self.stop_button.setEnabled(False)
        except Exception as error:
            self._append_log(f"Stop error: {error}")

    def _prepare(self, *, force: bool = False) -> None:
        if not self._room_connected:
            self._append_log("Connect to a room before preparing its mod.")
            return
        if QMessageBox.question(self, "Confirm room mod", "Prepare and install mod bound to this room?") != QMessageBox.StandardButton.Yes:
            return
        try:
            started = self.controller.reinstall_setup() if force else self.controller.prepare_setup()
            if not started:
                self._append_log("Setup is already active or room is unavailable.")
        except Exception as error:
            self._append_log(f"Setup error: {error}")

    def _reinstall(self) -> None:
        self._prepare(force=True)

    def _confirm_windows(self, succeeded: bool) -> None:
        try:
            self.controller.confirm_windows_installation(succeeded)
        except Exception as error:
            self._append_log(f"Installation confirmation error: {error}")

    def _entry_row(self, layout: QGridLayout, row: int, label: str, field: QLineEdit) -> None:
        layout.addWidget(self._label(label, "eyebrow"), row, 0)
        layout.addWidget(field, row, 1, 1, 2)

    def _path_row(self, layout: QGridLayout, row: int, label: str, field: QLineEdit, callback) -> None:
        layout.addWidget(self._label(label, "eyebrow"), row, 0)
        layout.addWidget(field, row, 1)
        browse = QPushButton("BROWSE")
        browse.clicked.connect(callback)
        layout.addWidget(browse, row, 2)

    def _browse_game(self) -> None:
        value = QFileDialog.getExistingDirectory(self, "Select DOOM Eternal folder")
        if value:
            self.game_root.setText(value)

    def _browse_saves(self) -> None:
        value = QFileDialog.getExistingDirectory(self, "Select DOOM Eternal save folder")
        if value:
            self.saves_root.setText(value)

    def _render_room(self, event: dict[str, object]) -> None:
        self.room_event = dict(event)
        seed = str(event.get("seed_name", "Unknown seed"))
        team, slot = event.get("team", "?"), event.get("slot", "?")
        endpoint = str(event.get("endpoint") or self.controller.config.get("server_address", "Unknown server"))
        player = str(event.get("slot_name") or self.controller.config.get("slot", slot))
        self.room_summary.setText(f"Server: {endpoint}\nPlayer: {player}\nSeed: {seed} | Team {team}, Slot {slot}")
        slot_data = event.get("slot_data")
        if not isinstance(slot_data, dict):
            self.room_options.setText("This room did not send a settings summary.")
            return
        traps = [(key, value) for key, value in sorted(slot_data.items()) if "trap" in str(key).casefold()]
        core = [(key, value) for key, value in sorted(slot_data.items()) if key in {"randomize_dash", "death_link", "death_link_mode", "start_with_automap"}]
        lines = [f"{str(key).replace('_', ' ').title()}: {value}" for key, value in core]
        if traps:
            lines.append("Traps: " + ", ".join(f"{str(key).replace('_', ' ').title()} {value}%" if isinstance(value, int) and "percentage" in str(key) else f"{str(key).replace('_', ' ').title()}: {value}" for key, value in traps))
        else:
            lines.append("Traps: not set for this room")
        self.room_options.setText(" | ".join(lines))

    def _view_room_options(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Room settings")
        dialog.resize(640, 480)
        layout = QVBoxLayout(dialog)
        layout.addWidget(self._label("ROOM SETTINGS", "section"))
        options = QPlainTextEdit()
        options.setReadOnly(True)
        slot_data = self.room_event.get("slot_data", {})
        if isinstance(slot_data, dict):
            options.setPlainText("\n".join(f"{str(key).replace('_', ' ').title()}: {value}" for key, value in sorted(slot_data.items())) or "No settings supplied.")
        else:
            options.setPlainText("No settings supplied.")
        layout.addWidget(options)
        close = QPushButton("CLOSE")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        dialog.exec()

    def _send_command(self) -> None:
        text = self.command_input.text()
        try:
            self.controller.send_command(text)
        except Exception as error:
            self._append_log(f"Command error: {error}")
            return
        self.command_input.clear()
        self._activity_event({"type": "command_sent", "message": text})

    @staticmethod
    def _format_markdown(text: object) -> str:
        escaped = html.escape(str(text), quote=False).replace("\n", "<br>")
        escaped = re.sub(r"(\*\*|__)(.+?)\1", r"<b>\2</b>", escaped)
        return re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", escaped)

    def _option_widget(self, option: dict[str, object]) -> QWidget:
        key, default = str(option["key"]), option.get("default")
        self.option_defaults[key] = default
        row = self._card()
        layout = QGridLayout(row)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setColumnStretch(0, 1)
        title = self._label(str(option["display_name"]))
        title.setStyleSheet("font-weight:800;")
        layout.addWidget(title, 0, 0)
        layout.addWidget(self._label(self._format_markdown(option["description"]), "muted", rich=True), 1, 0)
        kind = str(option["ui_type"])
        if kind == "toggle":
            control: QWidget = QCheckBox("Enabled")
            control.setChecked(bool(default))
        elif kind == "choice":
            control = QComboBox()
            for choice in cast(list[dict[str, object]], option["choices"]):
                control.addItem(str(choice.get("label", choice["key"])), choice["key"])
            control.setCurrentIndex(max(0, control.findData(default)))
        elif kind == "range":
            control = QSpinBox()
            control.setRange(cast(int, option["minimum"]), cast(int, option["maximum"]))
            control.setValue(cast(int, default))
        elif kind == "option_set":
            control = OptionSetControl(option)
        else:
            control = NamedRangeControl(option)
        control.setMinimumWidth(200)
        self.option_controls[key] = control
        layout.addWidget(control, 0, 1, 2, 1, Qt.AlignmentFlag.AlignVCenter)
        return row

    def _start_inventory_widget(self) -> QWidget:
        card = self._card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.addWidget(self._label("STARTING INVENTORY", "section"))
        layout.addWidget(self._label("Choose supported items to begin with. Quantities are saved in your Player YAML.", "muted"))
        try:
            self.start_inventory_catalog = load_start_inventory_catalog(self.controller.client_dir / "data" / "start_inventory_catalog.json")
        except ValueError as error:
            self.start_inventory_catalog = []
            self._append_log(f"Starting Inventory choices could not load: {error}. Check Help for details.")
        controls = QHBoxLayout()
        self.inventory_picker = QComboBox()
        self.inventory_picker.setMinimumContentsLength(24)
        for item in self.start_inventory_catalog:
            self.inventory_picker.addItem(item["label"], item["name"])
        self.inventory_quantity = QSpinBox()
        self.inventory_quantity.setRange(1, 9999)
        add = QPushButton("ADD")
        add.setObjectName("primary")
        add.clicked.connect(self._add_inventory)
        enabled = bool(self.start_inventory_catalog)
        self.inventory_picker.setEnabled(enabled)
        self.inventory_quantity.setEnabled(enabled)
        add.setEnabled(enabled)
        controls.addWidget(self.inventory_picker, 1)
        controls.addWidget(self.inventory_quantity)
        controls.addWidget(add)
        layout.addLayout(controls)
        self.inventory = QTableWidget(0, 2)
        self.inventory.setHorizontalHeaderLabels(["ITEM", "QTY"])
        self.inventory.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.inventory.verticalHeader().hide()
        self.inventory.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.inventory.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.inventory.setMinimumHeight(150)
        layout.addWidget(self.inventory)
        remove = QPushButton("REMOVE SELECTED")
        remove.clicked.connect(lambda: self.inventory.removeRow(self.inventory.currentRow()) if self.inventory.currentRow() >= 0 else None)
        layout.addWidget(remove, alignment=Qt.AlignmentFlag.AlignRight)
        return card

    def _add_inventory(self) -> None:
        name = self.inventory_picker.currentData()
        if not isinstance(name, str):
            return
        for row in range(self.inventory.rowCount()):
            if self.inventory.item(row, 0).data(Qt.ItemDataRole.UserRole) == name:
                amount = int(self.inventory.item(row, 1).text()) + self.inventory_quantity.value()
                self.inventory.setItem(row, 1, QTableWidgetItem(str(amount)))
                return
        row = self.inventory.rowCount()
        self.inventory.insertRow(row)
        item = QTableWidgetItem(name)
        item.setData(Qt.ItemDataRole.UserRole, name)
        self.inventory.setItem(row, 0, item)
        self.inventory.setItem(row, 1, QTableWidgetItem(str(self.inventory_quantity.value())))

    def _option_values(self) -> dict[str, object]:
        values: dict[str, object] = {}
        for key, control in self.option_controls.items():
            if isinstance(control, QCheckBox): values[key] = control.isChecked()
            elif isinstance(control, QComboBox): values[key] = control.currentData()
            elif isinstance(control, QSpinBox): values[key] = control.value()
            elif isinstance(control, (NamedRangeControl, OptionSetControl)): values[key] = control.value()
        inventory: dict[str, int] = {}
        for row in range(self.inventory.rowCount()):
            item, amount = self.inventory.item(row, 0), self.inventory.item(row, 1)
            if item is not None and amount is not None:
                inventory[str(item.data(Qt.ItemDataRole.UserRole))] = int(amount.text())
        values["start_inventory"] = inventory
        return values

    def _reset_options(self) -> None:
        for key, control in self.option_controls.items():
            value = self.option_defaults[key]
            if isinstance(control, QCheckBox): control.setChecked(bool(value))
            elif isinstance(control, QComboBox): control.setCurrentIndex(max(0, control.findData(value)))
            elif isinstance(control, QSpinBox): control.setValue(cast(int, value))
            elif isinstance(control, (NamedRangeControl, OptionSetControl)): control.setValue(value)
        self.inventory.setRowCount(0)

    def _save_player_options(self) -> None:
        try:
            suggested = suggested_yaml_filename(self.player_name.text())
        except ValueError as error:
            QMessageBox.critical(self, "Could not save Player YAML", str(error))
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Save Player YAML", suggested, "YAML files (*.yaml);;All files (*)")
        if not filename:
            return
        try:
            saved = self.controller.save_player_options(Path(filename), self.player_name.text(), self._option_values())
        except Exception as error:
            self._append_log(f"Player YAML save error: {error}")
            QMessageBox.critical(self, "Could not save Player YAML", str(error))
            return
        self._activity_event({"type": "player_yaml_saved", "path": str(saved)})
        QMessageBox.information(self, "Player YAML saved", f"Saved player options to:\n{saved}")

    def _copy_launch_option(self) -> None:
        value = self.launch_option.text()
        if value and not value.startswith("Available"):
            QApplication.clipboard().setText(value)
            self._append_log("Steam launch option copied.")

    def _run_doctor(self) -> None:
        try:
            self._render_doctor_report(self.controller.run_doctor().document())
        except Exception as error:
            self.doctor_status.setText("SETUP CHECK COULD NOT RUN")
            self.doctor_evidence.setText(str(error))

    def _render_doctor_report(self, report: object) -> None:
        if not isinstance(report, dict):
            return
        healthy = bool(report.get("ok"))
        diagnostics = report.get("diagnostics", [])
        lines = []
        if isinstance(diagnostics, list):
            for item in diagnostics:
                if isinstance(item, dict):
                    lines.append(f"{item.get('key', 'check')}: {item.get('status', 'unknown')} - {item.get('message', '')}")
        self.doctor_status.setText("SETUP READY" if healthy else "SETUP NEEDS ATTENTION")
        self.doctor_status.setStyleSheet(f"color:{self.COLORS['good' if healthy else 'warn']};")
        self.doctor_evidence.setText("\n".join(lines) or "No setup details returned.")
        self.doctor_action.setText("You are ready to play." if healthy else "Review failed checks, then use Fix Setup or try again.")

    def _probe_handshake(self) -> None:
        try:
            result = self.controller.probe_handshake()
            status = str(result.get("status", "unavailable")).replace("_", " ").upper()
            self.doctor_status.setText(f"GAME CONNECTION {status}")
            self.doctor_evidence.setText("Game connection check completed. Technical details are available below.")
            self._append_log(", ".join(f"{key}={value}" for key, value in result.items()))
        except Exception as error:
            self._append_log(f"Game connection check error: {error}")

    def _launch_game(self) -> None:
        try:
            self.controller.launch_game()
            self._set_status("game", "launch requested", self.COLORS["good"])
            self._append_log("DOOM Eternal launch requested.")
        except Exception as error:
            self._append_log(f"DOOM Eternal launch error: {error}")

    def _save_support_bundle(self) -> None:
        try:
            path = self.controller.create_support_bundle(Path.home() / "DOOM-Eternal-Archipelago-support.zip", logs=self.log.toPlainText().splitlines())
            self.doctor_action.setText(f"Support report saved: {path}")
        except Exception as error: self._append_log(f"Support bundle error: {error}")

    def _preview_repairs(self) -> None:
        try: actions = self.controller.repair_preview()
        except Exception as error:
            self._append_log(f"Repair preview error: {error}")
            return
        if not actions:
            self.doctor_action.setText("No safe repair is needed.")
            return
        action = actions[0]
        prompt = f"{action.title}\n\nChanges:\n" + "\n".join(f"• {change}" for change in action.changes) + f"\n\nRollback: {action.rollback}"
        if QMessageBox.question(self, "Apply repair", prompt) != QMessageBox.StandardButton.Yes: return
        try: self.doctor_action.setText(str(self.controller.apply_repair(action.key)))
        except Exception as error: self._append_log(f"Repair error: {error}")

    def _poll_events(self) -> None:
        while True:
            try: event = self.controller.events.get_nowait()
            except queue.Empty: break
            self.controller.process_event(event)
            self._activity_event(event)
            self._handle_event(event)

    def _activity_event(self, event: dict[str, object]) -> None:
        kind = str(event.get("type", "event"))
        semantic = {
            "connected": ("CONNECTED", self.COLORS["good"]), "disconnected": ("DISCONNECTED", self.COLORS["muted"]),
            "error": ("NEEDS ATTENTION", self.COLORS["bad"]), "setup_failed": ("NEEDS ATTENTION", self.COLORS["bad"]),
            "warning": ("NOTICE", self.COLORS["warn"]), "room_install_state": ("MOD", self.COLORS["ap"]),
            "setup_ready": ("READY", self.COLORS["good"]), "command_sent": ("MESSAGE SENT", self.COLORS["ap"]),
            "item": ("ITEM RECEIVED", self.COLORS["good"]), "location": ("CHECK COMPLETE", self.COLORS["ap"]),
            "deathlink": ("DEATHLINK", self.COLORS["warn"]),
        }
        segment, color = semantic.get(kind, (kind.replace("_", " ").upper(), self.COLORS["muted"]))
        fields = (("Server", "endpoint"), ("Seed", "seed_name"), ("Status", "state"), ("Reason", "reason"), ("Message", "message"))
        detail = " | ".join(f"{label}: {event[key]}" for label, key in fields if event.get(key) not in (None, ""))
        row = 0
        self.activity.insertRow(row)
        self.activity.setItem(row, 0, QTableWidgetItem(datetime.now().strftime("%H:%M:%S")))
        signal = QTableWidgetItem(segment)
        signal.setForeground(color)
        self.activity.setItem(row, 1, signal)
        self.activity.setItem(row, 2, QTableWidgetItem(detail or "Session update received"))
        while self.activity.rowCount() > 100:
            self.activity.removeRow(self.activity.rowCount() - 1)

    def _handle_event(self, event: dict[str, object]) -> None:
        kind = str(event.get("type", ""))
        if kind == "log":
            self._append_log(str(event.get("message", "")))
            return
        if kind == "connected":
            self._connection_pending = False; self._room_connected = True
            self._set_connection_controls(False); self._render_room(event)
            self._set_status("room", "connected", self.COLORS["good"])
            self._set_status("mod", "checking", self.COLORS["ap"])
            self._set_home("ROOM CONNECTED", "Checking your room before play.", "CHECKING...", "CONNECTED", enabled=False)
        elif kind in {"client_started", "connecting"}:
            self._connection_pending = True; self._set_connection_controls(False)
            self._set_status("room", "connecting", self.COLORS["ap"])
        elif kind == "room_install_state":
            option = str(event.get("steam_launch_option", ""))
            if option:
                self.launch_option.setText(option); self._set_status("game", "ready", self.COLORS["good"])
            state, reason = str(event.get("state", "")), str(event.get("reason", ""))
            if state == "already_installed":
                self.drift.hide(); self.reinstall_button.setEnabled(True)
                self._set_status("mod", "ready", self.COLORS["good"]); self._set_status("game", "ready", self.COLORS["good"]); self._set_status("rpc", "waiting", self.COLORS["ap"])
                self._show_page(2)
                self._set_home("READY TO PLAY", "Your room is ready. Start DOOM Eternal and keep this launcher open.", "PLAY DOOM ETERNAL", "READY")
            else:
                drift = state == "update_required" or "another room" in reason
                self.drift.setText("ROOM UPDATE - " + (reason or "This room needs its matching mod.")); self.drift.setVisible(drift)
                self.reinstall_button.setEnabled(False)
                self._set_status("mod", "update needed", self.COLORS["warn"]); self._set_status("game", "waiting", self.COLORS["warn"])
                self._set_home("UPDATE MOD", reason or "Prepare this room before play.", "UPDATE MOD", "ACTION NEEDED")
        elif kind in {"setup_started", "mod_building", "runtime_config_ready", "mod_staged", "injector_started"}:
            self.reinstall_button.setEnabled(False); self._set_status("mod", "updating", self.COLORS["ap"])
            self._set_home("UPDATING MOD", "Preparing your room for play.", "UPDATING...", "WORKING", enabled=False)
        elif kind == "setup_ready":
            option = str(event.get("steam_launch_option", "")); state = str(event.get("adapter_state", ""))
            if option:
                self.launch_option.setText(option); self._set_status("game", "ready", self.COLORS["good"])
            if state == "applied":
                self.reinstall_button.setEnabled(True); self._set_status("mod", "ready", self.COLORS["good"]); self._set_status("game", "ready", self.COLORS["good"]); self._set_status("rpc", "waiting", self.COLORS["ap"])
                self._show_page(2)
                self._set_home("READY TO PLAY", "Your room is ready. Start DOOM Eternal and keep this launcher open.", "PLAY DOOM ETERNAL", "READY")
            elif state == "manual_action_required":
                self._set_home("FINISH SETUP", str(event.get("message", "Finish the game manager step, then try again.")), "TRY AGAIN", "ACTION NEEDED")
            else:
                self._set_home("SETUP NEEDS ATTENTION", str(event.get("message", "Try setup again.")), "TRY AGAIN", "ACTION NEEDED")
        elif kind == "manual_action_required":
            message = str(event.get("message", "Finish the game manager step."))
            self._set_home("FINISH SETUP", message, "WAITING", "ACTION NEEDED", enabled=False)
            complete = QMessageBox.question(
                self,
                "Confirm mod installation",
                f"{message}\n\nDid EternalModManager finish installing this room mod?",
            ) == QMessageBox.StandardButton.Yes
            self._confirm_windows(complete)
        elif kind == "windows_installation_confirmed":
            if bool(event.get("succeeded")):
                self.reinstall_button.setEnabled(True)
                self._set_status("mod", "ready", self.COLORS["good"])
                self._set_status("game", "ready", self.COLORS["good"]); self._set_status("rpc", "waiting", self.COLORS["ap"])
                self._show_page(2)
                self._set_home("READY TO PLAY", "Your room is ready. Start DOOM Eternal and keep this launcher open.", "PLAY DOOM ETERNAL", "READY")
            else:
                self._set_home("SETUP NEEDS RETRY", "Finish installation, then update this room mod.", "TRY AGAIN", "ACTION NEEDED")
        elif kind == "disconnected":
            self._connection_pending = False; self._room_connected = False; self._set_connection_controls(True)
            self.reinstall_button.setEnabled(False); self.drift.hide(); self.room_summary.setText("No room connected. Join a room to start playing.")
            for key in self.statuses:
                self._set_status(key, "waiting", self.COLORS["muted"])
            self._set_home("SESSION ENDED", "Update room details or reconnect.", "JOIN A ROOM", "OFFLINE")
        elif kind in {"setup_failed", "error"}:
            message = str(event.get("message", "Unknown error"))
            if not self._room_connected:
                self._connection_pending = False; self._set_connection_controls(True); self._set_status("room", "failed", self.COLORS["bad"])
                self._set_home("CONNECTION FAILED", message, "RETRY JOIN", "CONNECTION FAILED")
            else:
                self._set_status("mod", "failed", self.COLORS["bad"]); self._set_home("SETUP FAILED", message, "TRY AGAIN", "ACTION NEEDED")
        elif kind == "warning":
            self._append_log("Warning: " + str(event.get("message", "")))

    def _append_log(self, text: str) -> None:
        if not text:
            return
        self.log.appendPlainText(text)
        if hasattr(self, "session_log"):
            self.session_log.appendPlainText(text)

    def _load_icon(self) -> None:
        icon = self.controller.client_dir / "doom_logo.png"
        if icon.is_file(): self.setWindowIcon(QIcon(str(icon)))

    def closeEvent(self, event) -> None:
        try: self.controller.close()
        finally: event.accept()

    def run(self) -> None:
        self.show()
        app = QApplication.instance()
        if app is None: raise RuntimeError("LauncherUI requires QApplication")
        app.exec()
