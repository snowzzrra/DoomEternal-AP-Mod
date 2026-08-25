"""PySide6 shell for standalone DOOM Eternal Archipelago launcher."""

from __future__ import annotations

import html
import os
import queue
import re
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import cast

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from doom_eap.content.options_foundation import load_start_inventory_catalog, suggested_yaml_filename

from .launcher_controller import LauncherController
from .launcher_platform import (
    probe_meathook,
    redact_secrets,
)


class AmmoRefillKeyControl(QWidget):
    """Single keyboard chord capture with explicit reset controls."""

    changed = Signal(str)

    def __init__(self, value: str = "F9"):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        self.editor = QKeySequenceEdit(QKeySequence(value))
        self.editor.setMaximumSequenceLength(1)
        self.editor.setClearButtonEnabled(True)
        self.editor.setToolTip("Press one key or supported modifier chord. Modifier-only shortcuts are rejected.")
        self.editor.editingFinished.connect(self._emit_value)
        clear = QPushButton("CLEAR")
        clear.clicked.connect(self._clear)
        reset = QPushButton("RESET")
        reset.clicked.connect(lambda: self.set_value("F9", emit=True))
        layout.addWidget(self.editor, 1)
        layout.addWidget(clear)
        layout.addWidget(reset)

    def value(self) -> str:
        return self.editor.keySequence().toString(QKeySequence.SequenceFormat.PortableText)

    def set_value(self, value: str, *, emit: bool = False) -> None:
        self.editor.setKeySequence(QKeySequence(value))
        if emit:
            self._emit_value()

    def _emit_value(self) -> None:
        self.changed.emit(self.value())

    def _clear(self) -> None:
        self.editor.clear()
        self._emit_value()


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
        tooltips = {
            "Acquire the Unmaykr": "Physically claim the Unmaykr in the Fortress of Doom.",
            "Complete All Enabled Missions": "Finish every mission included in this room: 13 Base Campaign missions or all 19 Full Saga missions.",
            "Complete All Slayer Gates": "Complete every Slayer Gate included in the room.",
            "Complete All Escalation Encounters": "Complete both waves of every TAG2 Escalation Encounter.",
            "Complete All Secret Encounters": "Complete every Secret Encounter included in the room.",
            "Complete All Mission Challenges": "Complete every Mission Challenge included in the room.",
            "Complete All Weapon Mastery Challenges": "Complete every enabled Weapon Mastery Challenge.",
        }
        for index, choice in enumerate(cast(list[object], option.get("choices", []))):
            if not isinstance(choice, dict):
                continue
            key = choice.get("key")
            check = QCheckBox(str(choice.get("label", key)))
            check.setObjectName("requirementOption")
            check.setToolTip(tooltips.get(str(key), ""))
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
        "ink": "#080b0d", "panel": "#111619", "panel2": "#171e22",
        "line": "#3b4549", "text": "#f1f3ef", "muted": "#9ba3a3",
        "doom": "#e86f1c", "doom_hot": "#ff8a2b", "ap": "#43bfc7",
        "good": "#a8d52a", "warn": "#f2c230", "bad": "#df3f3f",
    }

    def __init__(self, controller: LauncherController):
        super().__init__()
        self.controller = controller
        self.setWindowTitle("DOOM Eternal Archipelago")
        self.resize(1200, 800)
        self.setMinimumSize(760, 600)
        self.option_controls: dict[str, QWidget] = {}
        self.option_defaults: dict[str, object] = {}
        self.option_rows: dict[str, QWidget] = {}
        self.start_inventory_catalog: list[dict[str, str]] = []
        self._syncing_create_dependencies = False
        self.room_event: dict[str, object] = {}
        self._room_connected = False
        self._connection_pending = False
        self._setup_state = "disconnected"
        self._resolved_consent_requests: set[str] = set()
        self._native_health_presentation: tuple[str, str] | None = None
        self._session_log_limit = 400
        self._chat_pending_text: str | None = None
        self._hints_state = "disconnected"
        self._warning_state: dict[str, bool] = {}
        self._configure_style()
        self._build()
        self._set_hints_state("disconnected")
        self._set_setup_state("disconnected")
        self._load_icon()
        self._install_shortcuts()
        self._discover()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_events)
        self.timer.start(75)

    def _configure_style(self) -> None:
        self.setStyleSheet(f"""
            QWidget {{ background:{self.COLORS['ink']}; color:{self.COLORS['text']}; font-family:'Noto Sans','Segoe UI','Aptos',sans-serif; font-size:10.5pt; }}
            QLabel {{ background:transparent; }} QMainWindow {{ background:{self.COLORS['ink']}; }}
            QFrame#shell {{ background:#0d151a; border-right:1px solid {self.COLORS['line']}; }}
            QFrame#topbar {{ background:#0d151a; border-bottom:1px solid {self.COLORS['line']}; }}
            QFrame#hero {{ background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #242017,stop:.46 #15191b,stop:1 #0b0e10); border:1px solid #5a4b38; border-left:4px solid {self.COLORS['doom']}; }}
            QFrame#card {{ background:{self.COLORS['panel']}; border:1px solid {self.COLORS['line']}; border-radius:2px; }}
            QWidget#statusItem {{ background:#0c151a; border:1px solid #30444d; border-radius:11px; }}
            QLabel#statusIndicator {{ color:#64737a; font-size:12pt; }}
            QWidget#statusItem[statusTone="good"] QLabel#statusIndicator {{ color:{self.COLORS['good']}; }}
            QWidget#statusItem[statusTone="active"] QLabel#statusIndicator {{ color:{self.COLORS['ap']}; }}
            QWidget#statusItem[statusTone="warn"] QLabel#statusIndicator {{ color:{self.COLORS['warn']}; }}
            QWidget#statusItem[statusTone="bad"] QLabel#statusIndicator {{ color:{self.COLORS['bad']}; }}
            QFrame#actionStrip {{ background:#252015; border:1px solid #6e5427; border-left:3px solid {self.COLORS['warn']}; }}
            QFrame#actionStrip[actionTone="ready"] {{ background:#14231b; border-color:#3d6245; border-left-color:{self.COLORS['good']}; }}
            QFrame#actionStrip[actionTone="working"] {{ background:#10252a; border-color:#32636a; border-left-color:{self.COLORS['ap']}; }}
            QFrame#effectiveConfig {{ background:#121614; border:1px solid #566044; border-left:4px solid {self.COLORS['good']}; }}
            QLabel#brand {{ font-family:'Bahnschrift SemiCondensed','Segoe UI',sans-serif; font-size:16pt; font-weight:800; }}
            QLabel#eyebrow {{ color:{self.COLORS['doom_hot']}; font-family:'Bahnschrift SemiCondensed','Segoe UI',sans-serif; font-size:9pt; font-weight:800; letter-spacing:1px; }}
            QLabel#title {{ font-family:'Bahnschrift SemiCondensed','Segoe UI',sans-serif; font-size:23pt; font-weight:800; }}
            QLabel#section {{ font-family:'Bahnschrift SemiCondensed','Segoe UI',sans-serif; font-size:14pt; font-weight:800; letter-spacing:.4px; }}
            QLabel#muted {{ color:{self.COLORS['muted']}; }} QLabel#state {{ font-weight:800; color:{self.COLORS['good']}; }}
            QLabel#stateDetail {{ color:{self.COLORS['muted']}; font-size:9pt; }}
            QLabel#stateName {{ color:{self.COLORS['muted']}; font-size:9pt; font-weight:800; }}
            QLabel#warning {{ color:{self.COLORS['warn']}; background:#292419; border-left:3px solid {self.COLORS['warn']}; padding:9px; }}
            QLineEdit,QSpinBox,QKeySequenceEdit {{ background:#0c1113; color:{self.COLORS['text']}; border:1px solid #596164; padding:7px 9px; min-height:20px; selection-background-color:{self.COLORS['doom']}; selection-color:#fff; }}
            QComboBox {{ background:#0c1419; border:1px solid #526871; padding:7px 9px; min-height:20px; }}
            QComboBox QAbstractItemView {{ background:#101a20; color:{self.COLORS['text']}; selection-background-color:{self.COLORS['doom']}; }}
            QLineEdit:focus,QSpinBox:focus,QKeySequenceEdit:focus,QComboBox:focus,QPushButton:focus,QCheckBox:focus {{ border:2px solid {self.COLORS['doom_hot']}; outline:0; }}
            QPushButton {{ background:#17242b; border:1px solid #4a626c; padding:9px 13px; font-family:'Bahnschrift SemiCondensed','Segoe UI',sans-serif; font-weight:800; letter-spacing:.3px; }}
            QPushButton:hover {{ background:#2d302b; border-color:{self.COLORS['doom_hot']}; }}
            QPushButton:pressed {{ background:#0d151a; border-color:{self.COLORS['doom_hot']}; padding:10px 12px 8px 14px; }}
            QPushButton#primary {{ background:{self.COLORS['good']}; border-color:{self.COLORS['good']}; color:#102012; }}
            QPushButton#primary:hover {{ background:#b2e77a; }}
            QPushButton#primary:pressed {{ background:#7eaf50; color:#071008; }}
            QPushButton#danger {{ background:#522226; border-color:#b9494e; color:#fff1f1; }}
            QPushButton#danger:hover {{ background:#743037; border-color:#ff7676; }}
            QPushButton#nav {{ text-align:left; background:transparent; border:0; border-left:3px solid transparent; color:{self.COLORS['muted']}; padding:10px 10px; }}
            QPushButton#nav:checked,QPushButton#nav:hover {{ background:#17242b; color:{self.COLORS['text']}; border-left-color:{self.COLORS['doom']}; }}
            QPushButton#sessionNav {{ background:transparent; border:1px solid #405761; padding:10px 16px; color:{self.COLORS['muted']}; font-size:11pt; }}
            QPushButton#sessionNav:checked {{ color:#19110b; border-color:{self.COLORS['doom_hot']}; background:{self.COLORS['doom_hot']}; }}
            QPushButton#sessionNav:hover {{ color:{self.COLORS['text']}; border-color:{self.COLORS['doom']}; background:#1d2d34; }}
            QPushButton:disabled {{ color:#718087; background:#131e24; border-color:#2d3d44; }}
            QPlainTextEdit,QTableWidget {{ background:#0b1217; color:{self.COLORS['text']}; border:1px solid {self.COLORS['line']}; }}
            QTableWidget {{ gridline-color:#25363e; alternate-background-color:#111c22; }}
            QHeaderView::section {{ background:#16242b; color:{self.COLORS['muted']}; border:0; padding:6px; font-weight:800; }}
            QCheckBox {{ spacing:8px; }} QCheckBox::indicator {{ width:17px; height:17px; border:1px solid #6b7f86; background:#0c1419; }}
            QCheckBox::indicator:checked {{ background:{self.COLORS['good']}; border-color:{self.COLORS['good']}; }}
            QCheckBox#requirementOption {{ background:#0d1214; border:1px solid #343d40; padding:10px 12px; }}
            QCheckBox#requirementOption:hover {{ border-color:{self.COLORS['doom_hot']}; background:#1b1b17; }}
            QLabel#connectionBadge {{ background:#22272a; border:1px solid #596164; padding:6px 12px; font-weight:800; }}
            QLabel#connectionBadge[connected="true"] {{ color:{self.COLORS['good']}; border-color:#758f25; background:#18200f; }}
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
        for index, (key, title) in enumerate((("room", "CONNECTION"), ("mod", "ROOM PACKAGE"), ("game", "GAME INSTALLATION"), ("rpc", "GAME INTEGRATION"))):
            item = QWidget()
            item.setObjectName("statusItem")
            item.setProperty("statusTone", "muted")
            item_layout = QHBoxLayout(item)
            item_layout.setContentsMargins(9, 6, 9, 6)
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
        self.ammo_refill_keybind = AmmoRefillKeyControl(
            str(self.controller.config.get("ammo_refill_keybind", "F9"))
        )
        self.ammo_refill_keybind.changed.connect(self._save_ammo_refill_keybind)
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
        self._entry_row(layout, 9, "AMMO REFILL KEY", self.ammo_refill_keybind)
        self.join_button = QPushButton("CONNECT")
        self.join_button.setObjectName("primary")
        self.join_button.clicked.connect(self._connect)
        layout.addWidget(self.join_button, 10, 1, 1, 2)
        self._toggle_paths(force=not bool(self.game_root.text() and self.saves_root.text()))
        return card

    def _session_card(self) -> QFrame:
        card = self._card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(9)
        layout.addWidget(self._label("ROOM PACKAGE", "section"))
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
        self.launch_option_label = self._label("CONFIGURED FOR THIS SYSTEM", "eyebrow")
        self.launch_option = QLineEdit("Available after setup.")
        self.launch_option.setReadOnly(True)
        self.launch_option.hide()
        self.copy_launch_option_button = QPushButton("COPY")
        self.copy_launch_option_button.clicked.connect(self._copy_launch_option)
        if os.name != "nt":
            layout.addWidget(self.launch_option_label)
            layout.addWidget(self.copy_launch_option_button, alignment=Qt.AlignmentFlag.AlignLeft)
        else:
            self.launch_option_label.hide()
            self.launch_option.hide()
            self.copy_launch_option_button.hide()
        return card

    def _session_hero(self) -> QFrame:
        """Session identity, readiness, and room actions in one primary surface."""
        card = self._card("hero")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(13)
        identity = QGridLayout()
        identity.setHorizontalSpacing(20)
        identity.setVerticalSpacing(4)
        identity.addWidget(self._label("CURRENT SESSION", "eyebrow"), 0, 0, 1, 3)
        self.session_player_name = self._label("NO ROOM CONNECTED", "title")
        identity.addWidget(self.session_player_name, 1, 0, 1, 2)
        self.connection_badge = self._label("OFFLINE", "connectionBadge")
        self.connection_badge.setProperty("connected", False)
        identity.addWidget(self.connection_badge, 1, 2, alignment=Qt.AlignmentFlag.AlignRight)
        self.player_team = self._label("Team —", "muted")
        self.player_slot = self._label("Slot —", "muted")
        self.player_inventory = self._label("Connect to restore inventory", "muted")
        self.player_ammo_refills = self._label(f"AMMO REFILLS — · {self.ammo_refill_keybind.value()}", "muted")
        self.resync_inventory_button = QPushButton("RESYNC INVENTORY")
        self.resync_inventory_button.setEnabled(False)
        self.resync_inventory_button.clicked.connect(self._request_inventory_resync)
        identity.addWidget(self.player_team, 2, 0)
        identity.addWidget(self.player_slot, 2, 1)
        identity.addWidget(self.player_ammo_refills, 2, 2)
        identity.addWidget(self.player_inventory, 3, 0, 1, 3)
        layout.addLayout(identity)
        layout.addWidget(self._status_strip())
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(self.resync_inventory_button)
        self.session_uninstall_button = QPushButton("UNINSTALL MOD")
        self.session_uninstall_button.clicked.connect(self._uninstall_room_package)
        self.session_uninstall_button.setEnabled(False)
        actions.addWidget(self.session_uninstall_button)
        actions.addStretch(1)
        self.stop_button = QPushButton("DISCONNECT")
        self.stop_button.setObjectName("danger")
        self.stop_button.clicked.connect(self._disconnect)
        self.stop_button.setEnabled(False)
        actions.addWidget(self.stop_button)
        layout.addLayout(actions)
        self.session_setup = self._card("actionStrip")
        self.session_setup.setProperty("actionTone", "warning")
        setup_layout = QHBoxLayout(self.session_setup)
        setup_layout.setContentsMargins(15, 12, 15, 12)
        setup_copy = QVBoxLayout()
        setup_copy.setSpacing(2)
        self.session_setup_title = self._label("ROOM PACKAGE", "section")
        self.session_setup_detail = self._label("Connect to a room to check its package.", "muted")
        setup_copy.addWidget(self.session_setup_title)
        setup_copy.addWidget(self.session_setup_detail)
        setup_layout.addLayout(setup_copy, 1)
        setup_buttons = QHBoxLayout()
        self.session_manual_complete_action = QPushButton("I COMPLETED MANUAL INSTALLATION")
        self.session_manual_complete_action.clicked.connect(self._confirm_manual_installation)
        self.session_manual_complete_action.setVisible(False)
        self.session_manual_retry_action = QPushButton("TRY AGAIN")
        self.session_manual_retry_action.clicked.connect(lambda: self._prepare(force=True))
        self.session_manual_retry_action.setVisible(False)
        self.session_setup_action = QPushButton("INSTALL ROOM PACKAGE")
        self.session_setup_action.setObjectName("primary")
        self.session_setup_action.clicked.connect(self._run_setup_action)
        self.reinstall_button = self.session_setup_action
        setup_buttons.addWidget(self.session_manual_complete_action)
        setup_buttons.addWidget(self.session_manual_retry_action)
        setup_buttons.addWidget(self.session_setup_action)
        setup_layout.addLayout(setup_buttons)
        layout.addWidget(self.session_setup)
        return card

    def _session_page(self) -> QWidget:
        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(28, 24, 28, 30)
        outer.setSpacing(14)
        outer.addWidget(self._session_hero())
        header = QHBoxLayout()
        header.addStretch(1)
        self.session_nav_buttons: list[QPushButton] = []
        for text, page in (("ACTIVITY", 0), ("HINTS", 1), ("LOG", 2), ("ROOM", 3)):
            button = QPushButton(text)
            button.setObjectName("sessionNav")
            button.setCheckable(True)
            button.setChecked(page == 0)
            button.clicked.connect(lambda checked=False, target=page: self._show_session_tab(target))
            header.addWidget(button)
            self.session_nav_buttons.append(button)
        outer.addLayout(header)
        self.session_stack = QStackedWidget()
        self.session_stack.addWidget(self._activity_card())
        self.session_stack.addWidget(self._hints_card())
        self.session_stack.addWidget(self._session_log_page())
        self.session_stack.addWidget(self._session_card())
        self.session_stack.currentChanged.connect(self._sync_session_tabs)
        outer.addWidget(self.session_stack, 1)
        outer.addWidget(self._chat_bar())
        self._sync_session_tabs(0)
        return body

    def _session_log_page(self) -> QFrame:
        card = self._card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.addWidget(self._label("SESSION LOG", "section"))
        self.session_log = QPlainTextEdit()
        self.session_log.setReadOnly(True)
        self.session_log.setFont(QFont("monospace", 10))
        self.session_log.document().setMaximumBlockCount(self._session_log_limit)
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

    def _hints_card(self) -> QFrame:
        card = self._card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.addWidget(self._label("HINTS", "section"))
        layout.addWidget(self._label("Hints reported by Archipelago for this slot.", "muted"))
        self.hints_empty = self._label("Hints unavailable while disconnected.", "muted")
        layout.addWidget(self.hints_empty)
        self.hints = QTableWidget(0, 5)
        self.hints.setHorizontalHeaderLabels(["STATUS", "ITEM", "LOCATION", "FOR", "IN WORLD"])
        self.hints.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.hints.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.hints.setAlternatingRowColors(True)
        self.hints.verticalHeader().hide()
        self.hints.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for column in range(1, 5):
            self.hints.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.hints, 1)
        return card

    def _chat_bar(self) -> QFrame:
        card = self._card()
        layout = QHBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        layout.addWidget(self._label("AP CHAT", "eyebrow"))
        self.command_input = QLineEdit()
        self.command_input.setPlaceholderText("Send chat or server command text")
        self.command_input.setEnabled(False)
        self.command_input.returnPressed.connect(self._send_command)
        layout.addWidget(self.command_input, 1)
        self.command_send = QPushButton("SEND")
        self.command_send.setEnabled(False)
        self.command_send.clicked.connect(self._send_command)
        layout.addWidget(self.command_send)
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
        layout.addWidget(self._effective_config_widget())
        options_by_key = {
            str(option["key"]): option
            for option in cast(list[dict[str, object]], self.controller.options_schema["options"])
        }
        groups = (
            ("GOAL & CAMPAIGN", ("goal", "use_dlc_content", "dlc_logic_timing", "additional_victory_requirements")),
            ("STARTING LOADOUT", ("starting_weapon", "special_weapon", "enhanced_melee_damage")),
            ("RANDOMIZATION", ("randomize_chainsaw", "randomize_dash", "randomize_first_battery", "include_weapon_mastery_challenges", "praetor_suit_upgrades_in_pool")),
            ("COMBAT & QoL", ("reveal_ap_locations_on_automap", "trap_percentage", "enabled_traps")),
            ("MULTIWORLD", ("progression_balancing", "accessibility", "death_link")),
        )
        for group, keys in groups:
            card = self._card()
            group_layout = QVBoxLayout(card)
            group_layout.setContentsMargins(20, 18, 20, 20)
            group_layout.setSpacing(9)
            group_layout.addWidget(self._label(group, "section"))
            if group == "STARTING LOADOUT":
                group_layout.addWidget(self._start_inventory_widget())
            for key in keys:
                option = options_by_key.get(key)
                if option is not None:
                    group_layout.addWidget(self._option_widget(option))
                if key == "additional_victory_requirements":
                    self.select_all_side_content = QPushButton("SELECT ALL SIDE CONTENT")
                    self.select_all_side_content.setToolTip(
                        "Selects Slayer Gates, Escalation Encounters, Secret Encounters, and Acquire the Unmaykr."
                    )
                    self.select_all_side_content.clicked.connect(lambda: self._set_all_side_content(True))
                    group_layout.addWidget(self.select_all_side_content)
            layout.addWidget(card)
        self._wire_create_dependencies()
        self._refresh_create_dependencies()
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

    def _effective_config_widget(self) -> QFrame:
        card = self._card("effectiveConfig")
        layout = QGridLayout(card)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(3)
        layout.addWidget(self._label("EFFECTIVE CONFIGURATION", "eyebrow"), 0, 0, 1, 3)
        self.effective_config_values: dict[str, QLabel] = {}
        cells = (("goal", "GOAL"), ("campaign", "CAMPAIGN"), ("dlc", "DLC LOGIC"), ("special", "SPECIAL WEAPON"), ("starting", "STARTING WEAPON"), ("victory", "VICTORY"))
        for index, (key, label) in enumerate(cells):
            cell = QWidget()
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 5, 10, 5)
            cell_layout.setSpacing(2)
            cell_layout.addWidget(self._label(label, "stateName"))
            value = self._label("—")
            value.setStyleSheet("font-weight:800;")
            cell_layout.addWidget(value)
            self.effective_config_values[key] = value
            layout.addWidget(cell, 1 + index // 3, index % 3)
        return card

    def _wire_create_dependencies(self) -> None:
        for key in ("use_dlc_content", "enhanced_melee_damage"):
            control = self.option_controls.get(key)
            if isinstance(control, QCheckBox):
                cast(QCheckBox, control).toggled.connect(self._refresh_create_dependencies)
        for key in ("goal", "special_weapon"):
            control = self.option_controls.get(key)
            if isinstance(control, QComboBox):
                cast(QComboBox, control).currentIndexChanged.connect(self._refresh_create_dependencies)
        requirements = self.option_controls.get("additional_victory_requirements")
        if isinstance(requirements, OptionSetControl):
            for _, check in requirements.checks:
                check.toggled.connect(self._refresh_create_dependencies)
        for key in ("randomize_dash", "randomize_chainsaw"):
            control = self.option_controls.get(key)
            if isinstance(control, QCheckBox):
                checkbox = cast(QCheckBox, control)
                self._warning_state[key] = checkbox.isChecked()
                checkbox.toggled.connect(lambda checked, option_key=key: self._confirm_randomization(option_key, checked))

    def _choice_value(self, key: str) -> object:
        control = self.option_controls.get(key)
        if isinstance(control, QCheckBox):
            return cast(QCheckBox, control).isChecked()
        if isinstance(control, QComboBox):
            return cast(QComboBox, control).currentData()
        return None

    def _set_choice_enabled(self, key: str, value: str, enabled: bool) -> None:
        control = self.option_controls.get(key)
        if not isinstance(control, QComboBox):
            return
        combo = cast(QComboBox, control)
        index = combo.findData(value)
        if index < 0:
            return
        item = combo.model().item(index)
        if item is not None:
            item.setEnabled(enabled)

    def _set_all_side_content(self, checked: bool) -> None:
        if self._syncing_create_dependencies:
            return
        control = self.option_controls.get("additional_victory_requirements")
        if not isinstance(control, OptionSetControl):
            return
        side_content = {
            "Complete All Slayer Gates",
            "Complete All Escalation Encounters",
            "Complete All Secret Encounters",
            "Acquire the Unmaykr",
        }
        for value, check in control.checks:
            if value in side_content:
                check.setChecked(checked)

    def _refresh_create_dependencies(self) -> None:
        if self._syncing_create_dependencies:
            return
        self._syncing_create_dependencies = True
        try:
            dlc_enabled = self._choice_value("use_dlc_content") is True
            special_weapon = self.option_controls.get("special_weapon")
            special_row = self.option_rows.get("special_weapon")
            dlc_timing_row = self.option_rows.get("dlc_logic_timing")
            self._set_choice_enabled("goal", "kill_the_dark_lord", dlc_enabled)
            self._set_choice_enabled("goal", "complete_the_full_saga", dlc_enabled)
            if not dlc_enabled:
                if self._choice_value("goal") in {"kill_the_dark_lord", "complete_the_full_saga"}:
                    goal_control = self.option_controls.get("goal")
                    if isinstance(goal_control, QComboBox):
                        goal = cast(QComboBox, goal_control)
                        goal.setCurrentIndex(max(0, goal.findData("acquire_the_unmaykr")))
                if isinstance(special_weapon, QComboBox):
                    special = cast(QComboBox, special_weapon)
                    special.setCurrentIndex(max(0, special.findData("the_crucible")))
            if special_row is not None:
                special_row.setVisible(dlc_enabled)
            if dlc_timing_row is not None:
                dlc_timing_row.setVisible(dlc_enabled)
            self._refresh_inventory_picker(dlc_enabled)
            requirements = self.option_controls.get("additional_victory_requirements")
            if isinstance(requirements, OptionSetControl):
                side_content = {
                    "Complete All Slayer Gates",
                    "Complete All Escalation Encounters",
                    "Complete All Secret Encounters",
                    "Acquire the Unmaykr",
                }
                selected = set(requirements.value())
                all_selected = side_content.issubset(selected)
                self.select_all_side_content.setText("SIDE CONTENT SELECTED" if all_selected else "SELECT ALL SIDE CONTENT")
                self.select_all_side_content.setEnabled(not all_selected)
            goal_label = self._selected_label("goal")
            special_label = "The Crucible" if not dlc_enabled else self._selected_label("special_weapon")
            requirement_count = len(requirements.value()) if isinstance(requirements, OptionSetControl) else 0
            values = {
                "goal": goal_label,
                "campaign": "Full Saga · 19 missions" if dlc_enabled else "Base Campaign · 13 missions",
                "dlc": self._selected_label("dlc_logic_timing") if dlc_enabled else "Base Campaign only",
                "special": special_label,
                "starting": self._selected_label("starting_weapon"),
                "victory": "Goal only" if not requirement_count else f"Goal + {requirement_count} optional objectives",
            }
            for key, value in values.items():
                self.effective_config_values[key].setText(value)
        finally:
            self._syncing_create_dependencies = False

    def _selected_label(self, key: str) -> str:
        control = self.option_controls.get(key)
        return cast(QComboBox, control).currentText() if isinstance(control, QComboBox) else "—"

    def _confirm_randomization(self, key: str, checked: bool) -> None:
        previous = self._warning_state.get(key, False)
        self._warning_state[key] = checked
        if not checked or previous:
            return
        if key == "randomize_dash":
            title = "Randomize Dash"
            message = (
                "Randomizing Dash can make some routes significantly harder and may require advanced "
                "movement or unintended techniques depending on your seed.\n\nContinue?"
            )
        else:
            title = "Randomize Chainsaw"
            message = (
                "This is actual Hell on Earth.\n\nAmmo economy becomes much harsher, and some encounters "
                "can be significantly more difficult before Chainsaw is found. Enhanced Melee Damage may "
                "make early combat more forgiving.\n\nContinue?"
            )
        answer = QMessageBox.question(
            self, title, message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            control = self.option_controls.get(key)
            if isinstance(control, QCheckBox):
                checkbox = cast(QCheckBox, control)
                checkbox.blockSignals(True)
                checkbox.setChecked(False)
                checkbox.blockSignals(False)
            self._warning_state[key] = False

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
        self.uninstall_button = QPushButton("UNINSTALL MOD")
        self.uninstall_button.setObjectName("secondary")
        self.uninstall_button.setEnabled(False)
        self.uninstall_button.clicked.connect(self._uninstall_room_package)
        buttons.addWidget(self.uninstall_button)
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
        self._ammo_refill_shortcut = QShortcut(
            QKeySequence(self.ammo_refill_keybind.value()), self
        )
        self._ammo_refill_shortcut.activated.connect(self.controller.request_ammo_refill)

    def _save_ammo_refill_keybind(self, captured: str = "") -> None:
        value = captured.strip()
        try:
            self.controller.set_ammo_refill_keybind(value)
        except ValueError as error:
            self.ammo_refill_keybind.set_value("F9")
            self._append_log(f"Ammo Refill keybind rejected: {error}")
            return
        self._ammo_refill_shortcut.setKey(QKeySequence(value))
        self.player_ammo_refills.setText(f"AMMO REFILLS — · {value or 'UNBOUND'}")

    def _show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.top_context.setText(("HOME", "JOIN ROOM", "SESSION", "CREATE PLAYER", "HELP")[index])
        for number, button in enumerate(self.nav_buttons):
            button.setChecked(number == index)

    def _show_session_tab(self, index: int) -> None:
        self.session_stack.setCurrentIndex(index)
        self._sync_session_tabs(index)

    def _sync_session_tabs(self, index: int) -> None:
        for number, button in enumerate(self.session_nav_buttons):
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
        if key == "rpc":
            self._native_health_presentation = None

    def _set_home(self, title: str, detail: str, action: str, state: str, *, enabled: bool = True) -> None:
        self.hero_title.setText(title)
        self.hero_detail.setText(detail)
        self.hero_action.setText(action)
        self.hero_action.setEnabled(enabled)
        self.top_state.setText(state)

    def _set_setup_state(self, state: str, detail: str = "") -> None:
        presentations = {
            "disconnected": ("ROOM PACKAGE", "Connect to a room to check its package.", "", False, "READY"),
            "checking": ("CHECKING ROOM PACKAGE", "Checking this room's package before play.", "CHECKING...", False, "CONNECTED"),
            "install_needed": ("INSTALL ROOM PACKAGE", "Install this room's package before playing.", "INSTALL ROOM PACKAGE", True, "ACTION NEEDED"),
            "update_required": ("UPDATE ROOM PACKAGE", "This room needs its matching package before playing.", "UPDATE ROOM PACKAGE", True, "ACTION NEEDED"),
            "installing": ("INSTALLING ROOM PACKAGE", "Preparing this room for play.", "INSTALLING...", False, "WORKING"),
            "updating": ("UPDATING ROOM PACKAGE", "Preparing this room for play.", "UPDATING...", False, "WORKING"),
            "game_link_needed": ("GAME INTEGRATION SETUP NEEDED", "Set up game integration required by DOOM Eternal Archipelago.", "PREPARE", True, "ACTION NEEDED"),
            "game_link_update_needed": ("GAME INTEGRATION NEEDS REPAIR", "Game integration needs repair before play.", "REPAIR", True, "ACTION NEEDED"),
            "manual_install_required": ("WINDOWS PACKAGE INSTALL NEEDS MANUAL SETUP", "Automatic room package setup could not be completed. Use the Windows Manual Mod Installer guide to continue.", "OPEN MANUAL INSTALL GUIDE", True, "ACTION NEEDED"),
            "ready": (
                "SETUP READY",
                "Start DOOM Eternal normally and keep this launcher open." if os.name == "nt" else "Copy the Steam launch option in Room, then start DOOM Eternal manually and keep this launcher open.",
                "OPEN SESSION",
                True,
                "READY",
            ),
            "failed": ("SETUP NEEDS ATTENTION", "Setup did not finish. Review Help or repair setup.", "REPAIR", True, "ACTION NEEDED"),
        }
        title, fallback, action, enabled, top_state = presentations[state]
        self._setup_state = state
        copy = detail or fallback
        tone = "ready" if state == "ready" else "working" if state in {"checking", "installing", "updating"} else "warning"
        self.session_setup.setProperty("actionTone", tone)
        self.session_setup.style().unpolish(self.session_setup)
        self.session_setup.style().polish(self.session_setup)
        self.session_setup_title.setText(title)
        self.session_setup_detail.setText(copy)
        self.session_setup_action.setText(action)
        self.session_setup_action.setEnabled(enabled)
        self.session_setup_action.setVisible(state not in {"ready", "disconnected"})
        self.session_manual_complete_action.setVisible(state == "manual_install_required")
        self.session_manual_retry_action.setVisible(state == "manual_install_required")
        self.reinstall_button.setText(action)
        self.reinstall_button.setEnabled(enabled)
        self.reinstall_button.setVisible(state not in {"ready", "disconnected"})
        self._set_home(title, copy, action, top_state, enabled=enabled)
        self.hero_action.setVisible(state != "disconnected")

    def _run_setup_action(self) -> None:
        if self._setup_state in {"install_needed", "update_required"}:
            self._prepare()
        elif self._setup_state == "failed":
            self._show_page(4)
        elif self._setup_state == "game_link_needed":
            self._prepare(force=True)
        elif self._setup_state == "manual_install_required":
            self._open_manual_install_guide()
        elif self._setup_state == "game_link_update_needed":
            confirm = QMessageBox.question(
                self,
                "Repair game integration",
                "A different XINPUT1_3.dll is installed in your DOOM Eternal folder.\n\n"
                "Repair game integration will back up its current game file to the launcher state folder "
                "and replace it with the verified version.\n\n"
                "Do you want to proceed with repair?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if confirm == QMessageBox.StandardButton.Yes:
                try:
                    self.controller.install_game_link(force_repair=True)
                    self._prepare(force=True)
                except Exception as error:
                    self._append_log(f"Game integration repair error: {error}")
                    self._set_setup_state("failed", f"Game integration repair failed: {error}")

    def _open_manual_install_guide(self) -> None:
        """Open the Windows Manual Mod Installer section in INSTALL.md."""
        manual_url = "https://github.com/DoomEAP/DoomEternal-AP-Mod/blob/main/docs/INSTALL.md#windows-manual-mod-installer"
        local_candidates = [
            self.controller.client_dir.parent / "docs" / "INSTALL.md",
            self.controller.client_dir / "docs" / "INSTALL.md",
            self.controller.client_dir / "INSTALL.md",
            self.controller.application_dir / "docs" / "INSTALL.md",
            self.controller.application_dir / "INSTALL.md",
        ]
        opened = False
        for candidate in local_candidates:
            if candidate.is_file():
                try:
                    webbrowser.open(candidate.resolve().as_uri())
                    opened = True
                    break
                except Exception:
                    pass
        if not opened:
            try:
                webbrowser.open(manual_url)
            except Exception as error:
                self._append_log(f"Could not open browser: {error}")
        self._append_log("Opened manual installation guide (INSTALL.md → Windows Manual Mod Installer).")

    def _confirm_manual_installation(self) -> None:
        """Prompt user confirmation for manual mod installation completion."""
        reply = QMessageBox.question(
            self,
            "Confirm Manual Installation",
            "Did EternalModInjector complete successfully?\n\n"
            "Select YES if the room mod has been injected into DOOM Eternal.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                self.controller.confirm_manual_installation()
                self._append_log("Manual mod installation confirmed.")
            except Exception as error:
                self._append_log(f"Manual installation confirmation error: {error}")
                QMessageBox.warning(self, "Manual Installation Error", str(error))

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
            if not self.game_root.text():
                self.detected_paths.setText("DOOM Eternal wasn't found automatically. Select your DOOM Eternal installation folder to continue.")
            else:
                self.detected_paths.setText("DOOM Eternal save directory wasn't found automatically. Select your save directory to continue.")
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
        if self._setup_state == "ready":
            self._show_page(2)
            return
        if self._setup_state in {"install_needed", "update_required", "failed", "manual_install_required"}:
            self._run_setup_action()
            return
        text = self.hero_action.text()
        if "JOIN" in text or "RETRY" in text:
            self._focus_join()
        elif "RESUME" in text:
            self._resume()
        elif "UPDATE" in text or "PREPARE" in text or "TRY AGAIN" in text:
            self._prepare(force="UPDATE" in text or "TRY AGAIN" in text)

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
        if QMessageBox.question(
            self,
            "Disconnect session",
            "Disconnect from this room?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.controller.disconnect()
            self.stop_button.setEnabled(False)
        except Exception as error:
            self._append_log(f"Stop error: {error}")

    def _uninstall_room_package(self) -> None:
        if not self._room_connected:
            return
        confirmation = QMessageBox.question(
            self,
            "Uninstall room package",
            "Remove DoomEAP room mod from DOOM Eternal?\n\nOther mods and Game integration will be kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return
        self.uninstall_button.setEnabled(False)
        self.doctor_status.setText("UNINSTALL REQUESTED")
        self.doctor_evidence.setText("Waiting to queue room package removal.")
        try:
            self.controller.uninstall_setup()
        except Exception:
            self.uninstall_button.setEnabled(self._room_connected)
            self.doctor_status.setText("UNINSTALL NEEDS ATTENTION")
            self.doctor_evidence.setText("Room package could not be removed. Close the game and try again.")
            self._append_log("Room package uninstall failed.")

    def _request_inventory_resync(self) -> None:
        if not self._room_connected:
            return
        self.resync_inventory_button.setEnabled(False)
        self.player_inventory.setText("Inventory: Resync requested")
        try:
            self.controller.request_inventory_resync()
        except Exception as error:
            self.resync_inventory_button.setEnabled(True)
            self.player_inventory.setText("Inventory: Resync unavailable")
            self._append_log(f"Inventory resync request failed: {error}")

    def _prepare(self, *, force: bool = False) -> None:
        if not self._room_connected:
            self._append_log("Connect to a room before preparing its mod.")
            return
        if self._setup_state in {"installing", "updating"}:
            self._append_log("Room mod setup is already active.")
            return
        if QMessageBox.question(self, "Confirm room package", "Prepare and install package bound to this room?") != QMessageBox.StandardButton.Yes:
            return
        try:
            started = self.controller.reinstall_setup() if force else self.controller.prepare_setup()
            if not started:
                self._append_log("Setup is already active or room is unavailable.")
                return
            self._set_setup_state("updating" if self._setup_state == "update_required" else "installing")
        except Exception as error:
            self._set_setup_state("failed", str(error))
            self._append_log(f"Setup error: {error}")

    def _reinstall(self) -> None:
        self._run_setup_action()

    def _request_dependency_consent(self, event: dict[str, object]) -> None:
        request_id = str(event.get("request_id", ""))
        if not request_id or request_id in self._resolved_consent_requests:
            self._append_log("Ignored duplicate or invalid dependency consent request.")
            return
        self._resolved_consent_requests.add(request_id)
        name = str(event.get("name") or "Required dependency")
        version = str(event.get("version") or "unspecified version")
        purpose = str(event.get("purpose") or "Complete room setup")
        source = str(event.get("source") or event.get("url") or "Upstream source not supplied")
        details = [
            f"Dependency: {name} {version}",
            f"Required to: {purpose}.",
            f"Upstream: {source}",
            "This download is verified before use.",
        ]
        self._set_setup_state("updating" if self._setup_state == "update_required" else "installing")
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Dependency download approval")
        dialog.setIcon(QMessageBox.Icon.Information)
        dialog.setText(f"Approve {name} download")
        dialog.setInformativeText("\n\n".join(details))
        accept = dialog.addButton("Download and verify", QMessageBox.ButtonRole.AcceptRole)
        dialog.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        accepted = False
        try:
            dialog.exec()
            accepted = dialog.clickedButton() is accept
        except Exception as error:
            self._append_log(f"Dependency consent dialog failed: {error}")
        finally:
            self.controller.resolve_consent(request_id, accepted)
        if not accepted:
            self._set_setup_state("failed", f"{name} download declined. Retry setup when ready.")

    def _entry_row(self, layout: QGridLayout, row: int, label: str, field: QWidget) -> None:
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
        def text_or(value: object, fallback: str) -> str:
            text = str(value).strip() if value is not None else ""
            return text or fallback

        seed = text_or(event.get("seed_name"), "Unknown seed")
        team, slot = event.get("team", "?"), event.get("slot", "?")
        endpoint = text_or(event.get("endpoint") or self.controller.config.get("server_address"), "Unknown server")
        player = text_or(
            event.get("slot_name") or event.get("player_name") or self.controller.config.get("slot"),
            f"Slot {slot}",
        )
        self.session_player_name.setText(player)
        self.connection_badge.setText("CONNECTED")
        self.connection_badge.setProperty("connected", True)
        self.connection_badge.style().unpolish(self.connection_badge)
        self.connection_badge.style().polish(self.connection_badge)
        self.player_team.setText(f"Team {team}")
        self.player_slot.setText(f"Slot {slot} · {player}")
        self.player_inventory.setText("Inventory ready")
        ammo_count = event.get("ammo_refills_available", "—")
        self.player_ammo_refills.setText(f"AMMO REFILLS {ammo_count} · {self.ammo_refill_keybind.value()}")
        self.resync_inventory_button.setEnabled(self._room_connected and not self._connection_pending)
        self.session_uninstall_button.setEnabled(self._room_connected)
        self.room_summary.setText(f"Seed: {seed}\nServer: {endpoint}\nPlayer: {player} · Team {team} · Slot {slot}")
        slot_data = event.get("slot_data")
        if not isinstance(slot_data, dict):
            self.room_options.setText("This room did not send a settings summary.")
            return
        traps = [(key, value) for key, value in sorted(slot_data.items()) if "trap" in str(key).casefold()]
        core = [(key, value) for key, value in sorted(slot_data.items()) if key in {
            "randomize_dash", "reveal_ap_locations_on_automap", "death_link", "death_link_mode"
        }]
        lines = [f"{self._option_label(key)}: {self._option_value_text(value)}" for key, value in core]
        if traps:
            lines.append("Traps: " + ", ".join(f"{self._option_label(key)} {value}%" if isinstance(value, int) and "percentage" in str(key) else f"{self._option_label(key)}: {self._option_value_text(value)}" for key, value in traps))
        else:
            lines.append("Traps: not set for this room")
        self.room_options.setText(" | ".join(lines))

    def _option_label(self, key: object) -> str:
        name = str(key)
        schema = getattr(self.controller, "options_schema", None)
        if isinstance(schema, dict):
            for option in schema.get("options", []) or []:
                if isinstance(option, dict) and option.get("key") == name:
                    label = str(option.get("display_name") or "").strip()
                    if label:
                        return label
        return name.replace("_", " ").title()

    def _option_value_text(self, value: object) -> str:
        if isinstance(value, bool):
            return "On" if value else "Off"
        return str(value)

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
            options.setPlainText("\n".join(
                f"{self._option_label(key)}: {self._option_value_text(value)}"
                for key, value in sorted(slot_data.items(), key=lambda item: str(item[0]))
            ) or "No settings supplied.")
        else:
            options.setPlainText("No settings supplied.")
        layout.addWidget(options)
        close = QPushButton("CLOSE")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        dialog.exec()

    def _send_command(self) -> None:
        text = self.command_input.text()
        if not text.strip() or self._chat_pending_text is not None:
            return
        try:
            self.controller.send_chat(text)
        except Exception as error:
            self._append_log(f"Command error: {error}")
            return
        self._chat_pending_text = text
        self._set_chat_enabled(False)

    def _set_chat_enabled(self, enabled: bool) -> None:
        ready = enabled and self._chat_pending_text is None
        self.command_input.setEnabled(ready)
        self.command_send.setEnabled(ready)

    def _render_hints(self, event: dict[str, object]) -> None:
        records = event.get("hints")
        if not isinstance(records, list):
            return
        deduped: dict[tuple[object, ...], dict[str, object]] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            key = tuple(record.get(field) for field in (
                "receiving_player", "finding_player", "location", "item", "entrance",
            ))
            deduped[key] = record
        self.hints.setRowCount(0)
        for row, record in enumerate(deduped.values()):
            self.hints.insertRow(row)
            values = (
                str(record.get("status_name", "HINT_UNSPECIFIED")).replace("HINT_", "").replace("_", " "),
                str(record.get("item_name", "Unknown item")),
                str(record.get("location_name", "Unknown location")),
                str(record.get("receiving_player_name", record.get("receiving_player", "?"))),
                str(record.get("finding_player_name", record.get("finding_player", "?"))),
            )
            for column, value in enumerate(values):
                self.hints.setItem(row, column, QTableWidgetItem(value))
        self._hints_state = "loaded"
        self.hints_empty.setText("No hints yet." if not deduped else "")
        self.hints_empty.setVisible(not deduped)
        self.hints.setVisible(bool(deduped))

    def _set_hints_state(self, state: str) -> None:
        self._hints_state = state
        self.hints.setRowCount(0)
        self.hints.setVisible(False)
        labels = {
            "loading": "Loading hints…",
            "disconnected": "Hints unavailable while disconnected.",
        }
        self.hints_empty.setText(labels[state])
        self.hints_empty.setVisible(True)

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
        self.option_rows[key] = row
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
        self.inventory_quantity = QSpinBox()
        self.inventory_quantity.setRange(1, 9999)
        self.inventory_add_button = QPushButton("ADD")
        self.inventory_add_button.setObjectName("primary")
        self.inventory_add_button.clicked.connect(self._add_inventory)
        enabled = bool(self.start_inventory_catalog)
        self.inventory_picker.setEnabled(enabled)
        self.inventory_quantity.setEnabled(enabled)
        self.inventory_add_button.setEnabled(enabled)
        controls.addWidget(self.inventory_picker, 1)
        controls.addWidget(self.inventory_quantity)
        controls.addWidget(self.inventory_add_button)
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

    def _refresh_inventory_picker(self, dlc_enabled: bool) -> None:
        selected_special = "the_crucible" if not dlc_enabled else str(self._choice_value("special_weapon"))
        special_items = {
            "progressive_special_weapon": {"Progressive Special Weapon"},
            "progressive_sentinel_hammer": {"Progressive Sentinel Hammer"},
            "the_crucible": {"The Crucible"},
        }
        allowed_special = special_items.get(selected_special, {"The Crucible"})
        unavailable = {"Berserk", "Haste", "Quad Damage"}
        if not dlc_enabled:
            unavailable.update({"Break Blast", "Desperate Punch", "Take Back"})
        allowed = [
            item for item in self.start_inventory_catalog
            if item["name"] not in unavailable
            and (item["name"] not in {"Progressive Special Weapon", "Progressive Sentinel Hammer", "The Crucible"}
                 or item["name"] in allowed_special)
        ]
        current = self.inventory_picker.currentData()
        self.inventory_picker.blockSignals(True)
        self.inventory_picker.clear()
        for item in allowed:
            self.inventory_picker.addItem(item["label"], item["name"])
        self.inventory_picker.setCurrentIndex(max(0, self.inventory_picker.findData(current)))
        self.inventory_picker.blockSignals(False)
        enabled = bool(allowed)
        self.inventory_picker.setEnabled(enabled)
        self.inventory_quantity.setEnabled(enabled)
        self.inventory_add_button.setEnabled(enabled)
        allowed_names = {item["name"] for item in allowed}
        for row in reversed(range(self.inventory.rowCount())):
            item = self.inventory.item(row, 0)
            if item is not None and str(item.data(Qt.ItemDataRole.UserRole)) not in allowed_names:
                self.inventory.removeRow(row)

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
        self._refresh_create_dependencies()

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
            if QApplication.clipboard().text() != value:
                self.copy_launch_option_button.setText("COPY FAILED")
                self._append_log("Steam launch option could not be verified on clipboard.")
                return
            self._append_log("Steam launch option copied.")
            self.copy_launch_option_button.setText("COPIED")
            QTimer.singleShot(1800, lambda: self.copy_launch_option_button.setText("COPY"))

    def _show_setup_complete(self) -> None:
        """Confirm a new room package install without offering game launch control."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Setup Complete")
        dialog.setModal(True)
        dialog.setMinimumWidth(440)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(26, 24, 26, 22)
        layout.setSpacing(12)
        layout.addWidget(self._label("SETUP COMPLETE", "title"))
        layout.addWidget(self._label(
            'Your Archipelago room is ready.\n\n'
            'Start DOOM Eternal normally from Steam.\n\n'
            'Do not use the "Play DOOM Eternal with mods" option.\n\n'
            "For the intended experience, disable tutorials\n"
            "in DOOM Eternal's game settings.\n\n"
            "Keep this launcher open while playing."
        ))
        if os.name != "nt" and self.launch_option.text() and not self.launch_option.text().startswith("Available"):
            layout.addWidget(self._label("STEAM LAUNCH OPTION", "eyebrow"))
            option = QLineEdit(self.launch_option.text())
            option.setReadOnly(True)
            layout.addWidget(option)
            copy = QPushButton("COPY OPTION")
            copy.clicked.connect(lambda: QApplication.clipboard().setText(option.text()))
            layout.addWidget(copy, alignment=Qt.AlignmentFlag.AlignLeft)
        close = QPushButton("CLOSE")
        close.setObjectName("primary")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        close.setFocus(Qt.FocusReason.OtherFocusReason)
        dialog.exec()

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
            self._present_event(event)
        self._refresh_native_health()

    def _refresh_native_health(self) -> None:
        game_root = self.controller.config.get("game_root") or self.controller.config.get("doom_base_dir")
        root = Path(str(game_root)).expanduser().resolve() if game_root else None
        meathook = probe_meathook(root)
        if self._setup_state in {"game_link_needed", "game_link_update_needed"}:
            if meathook.ok:
                self._set_status("rpc", "waiting", self.COLORS["ap"])
                self._set_setup_state("ready")
            else:
                self._set_status("rpc", "needs setup", self.COLORS["warn"])
                target_state = "game_link_update_needed" if meathook.status.value == "incompatible" else "game_link_needed"
                if self._setup_state != target_state:
                    self._set_setup_state(target_state, meathook.message)
                return
        elif self._setup_state == "ready" and not meathook.ok:
            self._set_status("rpc", "needs setup", self.COLORS["warn"])
            target_state = "game_link_update_needed" if meathook.status.value == "incompatible" else "game_link_needed"
            self._set_setup_state(target_state, meathook.message)
            return

        try:
            health = self.controller.native_health()
            state = str(health.get("state", "not_ready")) if isinstance(health, dict) else "not_ready"
        except Exception:
            state = "not_ready"
        if state == "ready":
            presentation = ("ready", self.COLORS["good"])
        elif state == "degraded":
            presentation = ("needs attention", self.COLORS["warn"])
        else:
            presentation = ("waiting", self.COLORS["muted"])
        if presentation == self._native_health_presentation:
            return
        self._set_status("rpc", *presentation)
        self._native_health_presentation = presentation

    def _present_event(self, event: dict[str, object]) -> None:
        kind = str(event.get("type", "event"))
        try:
            self._handle_event(event)
        except Exception as error:
            self._append_log(f"UI lifecycle presentation failed for {kind}: {type(error).__name__}: {error}")
        try:
            self._activity_event(event)
        except Exception as error:
            self._append_log(f"UI activity presentation failed for {kind}: {type(error).__name__}: {error}")

    def _activity_event(self, event: dict[str, object]) -> None:
        kind = str(event.get("type", "event"))
        if kind == "log":
            return
        semantic = {
            "connected": ("CONNECTED", self.COLORS["good"]), "disconnected": ("DISCONNECTED", self.COLORS["muted"]),
            "error": ("NEEDS ATTENTION", self.COLORS["bad"]), "setup_failed": ("NEEDS ATTENTION", self.COLORS["bad"]),
            "warning": ("NOTICE", self.COLORS["warn"]), "room_install_state": ("MOD", self.COLORS["ap"]),
            "setup_ready": ("READY", self.COLORS["good"]), "command_sent": ("MESSAGE SENT", self.COLORS["ap"]),
            "item": ("ITEM RECEIVED", self.COLORS["good"]), "location": ("CHECK COMPLETE", self.COLORS["ap"]),
            "deathlink": ("DEATHLINK", self.COLORS["warn"]),
            "archipelago": ("ARCHIPELAGO", self.COLORS["doom_hot"]),
            "game_link_installed": ("GAME LINK", self.COLORS["good"]),
            "ammo_refill": ("AMMO REFILL", self.COLORS["doom_hot"]),
        }
        segment, color = semantic.get(kind, (kind.replace("_", " ").upper(), self.COLORS["muted"]))
        fields = (("Server", "endpoint"), ("Seed", "seed_name"), ("Status", "state"), ("Reason", "reason"), ("Message", "message"))
        detail = " | ".join(f"{label}: {event[key]}" for label, key in fields if event.get(key) not in (None, ""))
        row = 0
        self.activity.insertRow(row)
        self.activity.setItem(row, 0, QTableWidgetItem(datetime.now().strftime("%H:%M:%S")))
        signal = QTableWidgetItem(segment)
        signal.setForeground(QBrush(QColor(color)))
        self.activity.setItem(row, 1, signal)
        if kind == "archipelago":
            detail_label = QLabel()
            detail_label.setTextFormat(Qt.TextFormat.RichText)
            detail_label.setWordWrap(True)
            detail_label.setText(self._archipelago_activity_detail(event))
            self.activity.setCellWidget(row, 2, detail_label)
            self.activity.resizeRowToContents(row)
        else:
            self.activity.setItem(row, 2, QTableWidgetItem(detail or "Session update received"))
        while self.activity.rowCount() > 100:
            self.activity.removeRow(self.activity.rowCount() - 1)

    def _archipelago_activity_detail(self, event: dict[str, object]) -> str:
        plain = str(event.get("plain") or "Archipelago update received")
        segments = event.get("segments")
        if event.get("schema") != 1 or not isinstance(segments, list):
            return html.escape(plain)
        colors = {
            "text": self.COLORS["text"],
            "location": self.COLORS["ap"],
            "player_self": self.COLORS["good"],
            "player_remote": self.COLORS["doom_hot"],
            "item_filler": "#8fc8e8",
            "item_useful": "#438bc4",
            "item_progression": "#bd84e8",
            "item_trap": self.COLORS["doom"],
        }
        rendered: list[str] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            text = html.escape(str(segment.get("text") or ""))
            if not text:
                continue
            segment_type = str(segment.get("type") or "text")
            if segment_type == "player":
                color = colors["player_self"] if bool(segment.get("self")) else colors["player_remote"]
            elif segment_type == "item":
                color = colors.get(f"item_{segment.get('classification')}", colors["text"])
            else:
                color = colors.get(segment_type, colors["text"])
            rendered.append(f'<span style="color:{color};">{text}</span>')
        return "".join(rendered) or html.escape(plain)

    def _handle_event(self, event: dict[str, object]) -> None:
        kind = str(event.get("type", ""))
        self._append_session_event(event)
        if kind == "log":
            self._append_log(str(event.get("message", "")))
            return
        if kind == "inventory_resync":
            status = str(event.get("status", ""))
            presentation = {
                "queued": ("Inventory: Restoration queued for game", True),
                "noop": ("Inventory: Already current", True),
                "error": ("Inventory: Resync unavailable", True),
            }.get(status)
            if presentation:
                detail, enabled = presentation
                self.player_inventory.setText(detail)
                self.resync_inventory_button.setEnabled(enabled and self._room_connected)
            return
        if kind == "ammo_refill":
            available = event.get("available")
            if isinstance(available, int):
                self.player_ammo_refills.setText(f"AMMO REFILLS {available} · {self.ammo_refill_keybind.value()}")
            self._append_log(
                f"Ammo Refill: {event.get('message') or event.get('status') or 'session update'}"
            )
            return
        if kind == "uninstall_queued":
            self.uninstall_button.setEnabled(False)
            self.doctor_status.setText("UNINSTALL QUEUED")
            self.doctor_evidence.setText("Room package removal is queued.")
            return
        if kind == "uninstall_started":
            self.uninstall_button.setEnabled(False)
            self.doctor_status.setText("REMOVING ROOM PACKAGE")
            self.doctor_evidence.setText("Removing DoomEAP room mod from DOOM Eternal.")
            return
        if kind == "uninstall_progress":
            self.uninstall_button.setEnabled(False)
            self.doctor_status.setText("REMOVING ROOM PACKAGE")
            self.doctor_evidence.setText("Room package removal is in progress. Keep the launcher open.")
            return
        if kind == "injector_post_run_confirmation":
            self.uninstall_button.setEnabled(False)
            self.doctor_status.setText("CHECKING ROOM PACKAGE")
            self.doctor_evidence.setText("Checking that room package removal completed.")
            return
        if kind == "uninstall_complete":
            self.uninstall_button.setEnabled(False)
            self.doctor_status.setText("ROOM PACKAGE UNINSTALLED")
            self.doctor_evidence.setText("DoomEAP room mod is not installed. Other mods and Game integration were kept.")
            self._set_status("mod", "not installed", self.COLORS["warn"])
            self._set_setup_state("install_needed", "Room package is not installed. Install it to play.")
            return
        if kind in {"uninstall_attention", "uninstall_failed"}:
            self.uninstall_button.setEnabled(self._room_connected)
            self.doctor_status.setText("UNINSTALL NEEDS ATTENTION")
            self.doctor_evidence.setText("Room package could not be removed. Close the game and try again.")
            return
        if kind == "connected":
            self._connection_pending = False; self._room_connected = True
            self._set_connection_controls(False); self._render_room(event)
            self.uninstall_button.setEnabled(True)
            self._set_status("room", "connected", self.COLORS["good"])
            self._set_status("mod", "checking", self.COLORS["ap"])
            self._set_setup_state("checking")
            self._set_chat_enabled(True)
            self._set_hints_state("loading")
        elif kind == "hints_loading":
            self._set_hints_state("loading")
        elif kind == "hints":
            self._render_hints(event)
        elif kind == "chat_sent":
            if self._chat_pending_text == event.get("text"):
                self.command_input.clear()
                self._chat_pending_text = None
                self._set_chat_enabled(self._room_connected)
            self._activity_event({"type": "command_sent", "message": event.get("text", "")})
        elif kind == "chat_send_failed":
            self._append_log("Command error: " + str(event.get("message", "Could not send message.")))
            self._chat_pending_text = None
            self._set_chat_enabled(self._room_connected)
        elif kind in {"client_started", "connecting"}:
            self._connection_pending = True; self._set_connection_controls(False)
            self._set_status("room", "connecting", self.COLORS["ap"])
        elif kind == "game_link_installed":
            self._set_status("rpc", "waiting", self.COLORS["ap"])
            self._append_log(f"Game Link runtime verified and installed: {event.get('path')}")
        elif kind == "room_install_state":
            option = str(event.get("steam_launch_option", ""))
            if option:
                self.launch_option.setText(option); self._set_status("game", "ready", self.COLORS["good"])
            state, reason = str(event.get("state", "")), str(event.get("reason", ""))
            readiness = str(event.get("readiness", "ready"))
            readiness_reason = str(event.get("readiness_reason", ""))
            if state == "uninstalled":
                self.drift.hide()
                self.uninstall_button.setEnabled(False)
                self._set_status("mod", "not installed", self.COLORS["warn"])
                self._set_setup_state("install_needed", "Room package is not installed. Install it to play.")
            elif state == "already_installed":
                self.drift.hide()
                self.uninstall_button.setEnabled(self._room_connected)
                if readiness == "blocked":
                    self._set_status("mod", "ready", self.COLORS["good"]); self._set_status("game", "ready", self.COLORS["good"]); self._set_status("rpc", "setup needed", self.COLORS["warn"])
                    self._show_page(2)
                    game_root = self.controller.config.get("game_root") or self.controller.config.get("doom_base_dir")
                    root = Path(str(game_root)).expanduser().resolve() if game_root else None
                    meathook = probe_meathook(root)
                    target_state = "game_link_update_needed" if meathook.status.value == "incompatible" else "game_link_needed"
                    self._set_setup_state(target_state, "Game integration needs setup before play.")
                else:
                    self._set_status("mod", "ready", self.COLORS["good"]); self._set_status("game", "ready", self.COLORS["good"]); self._set_status("rpc", "waiting", self.COLORS["ap"])
                    self._show_page(2)
                    self._set_setup_state("ready")
            else:
                drift = state == "update_required" or "another room" in reason
                self.drift.setText("ROOM UPDATE - " + (reason or "This room needs its matching mod.")); self.drift.setVisible(drift)
                self._set_status("mod", "update needed", self.COLORS["warn"]); self._set_status("game", "waiting", self.COLORS["warn"])
                setup_copy = (
                    "Another room's mod is installed. Update to this room's mod."
                    if "another room" in reason
                    else "This room's installed mod needs an update."
                )
                self._set_setup_state(
                    "update_required" if state == "update_required" else "install_needed",
                    setup_copy if state == "update_required" else "Install this room's package before playing.",
                )
        elif kind in {"setup_started", "mod_building", "runtime_config_ready", "mod_staged", "injector_started"}:
            self._set_status("mod", "updating", self.COLORS["ap"])
            self._set_setup_state("updating" if self._setup_state == "update_required" else "installing")
        elif kind == "dependency_consent_required":
            self._request_dependency_consent(event)
        elif kind == "setup_ready":
            option = str(event.get("steam_launch_option", "")); state = str(event.get("adapter_state", ""))
            if option:
                self.launch_option.setText(option); self._set_status("game", "ready", self.COLORS["good"])
            if state == "applied":
                game_root = self.controller.config.get("game_root") or self.controller.config.get("doom_base_dir")
                root = Path(str(game_root)).expanduser().resolve() if game_root else None
                meathook = probe_meathook(root)
                if not meathook.ok:
                    self._set_status("mod", "ready", self.COLORS["good"]); self._set_status("game", "ready", self.COLORS["good"]); self._set_status("rpc", "setup needed", self.COLORS["warn"])
                    self._show_page(2)
                    target_state = "game_link_update_needed" if meathook.status.value == "incompatible" else "game_link_needed"
                    self._set_setup_state(target_state, "Game integration needs setup before play.")
                else:
                    self._set_status("mod", "ready", self.COLORS["good"]); self._set_status("game", "ready", self.COLORS["good"]); self._set_status("rpc", "waiting", self.COLORS["ap"])
                    self._show_page(2)
                    self._set_setup_state("ready")
            elif state == "manual_action_required":
                self._set_setup_state("installing", str(event.get("message", "Finish the game manager step, then try again.")))
            else:
                self._set_setup_state("failed", str(event.get("message", "Try setup again.")))
            if event.get("new_install") is True:
                self._show_setup_complete()
        elif kind in {"manager_started", "injector_started"}:
            message = str(event.get("message", "Complete installation in EternalModInjector."))
            self._set_setup_state("installing", "Complete installation in EternalModInjector, then close it.")
            self._append_log(message)
        elif kind in {"manager_closed", "injector_closed"}:
            returncode = event.get("returncode")
            if returncode is not None:
                self._append_log(f"EternalModInjector closed (code: {returncode}).")
            else:
                self._append_log("EternalModInjector closed.")
        elif kind == "uninstall_confirmation_required":
            request_id = str(event.get("request_id", ""))
            self.uninstall_button.setEnabled(False)
            reply = QMessageBox.question(
                self,
                "Confirm room package removal",
                "EternalModInjector has finished. Confirm it completed and the DoomEAP room package is absent.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            confirmed = reply == QMessageBox.StandardButton.Yes
            if confirmed:
                self.doctor_status.setText("CHECKING ROOM PACKAGE")
                self.doctor_evidence.setText("Confirming room package removal.")
            else:
                self.doctor_status.setText("UNINSTALL NEEDS ATTENTION")
                self.doctor_evidence.setText("Room package removal was not confirmed.")
            self.controller.resolve_uninstall_confirmation(request_id, confirmed)
        elif kind == "installation_confirmation_required":
            request_id = str(event.get("request_id", ""))
            message = str(event.get("message", "Did the mod installation complete successfully in EternalModInjector?"))
            reply = QMessageBox.question(
                self,
                "Confirm mod installation",
                f"{message}\n\nSelect YES if EternalModInjector applied the mod without errors.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            confirmed = (reply == QMessageBox.StandardButton.Yes)
            self.controller.resolve_installation_confirmation(request_id, confirmed)
        elif kind == "installation_declined":
            self._set_status("mod", "failed", self.COLORS["warn"])
            self._set_setup_state("manual_install_required", "Mod installation was not confirmed in EternalModInjector. Follow the manual install guide.")
            self._append_log("Mod installation was not confirmed. Directing to Windows Manual Mod Installer.")
        elif kind == "manual_install_required":
            message = str(event.get("message", "Manual mod installation required."))
            self._set_status("mod", "failed", self.COLORS["warn"])
            self._set_setup_state("manual_install_required", message)
            self._append_log(message)
        elif kind == "injector_finished":
            state = str(event.get("state", ""))
            returncode = event.get("returncode")
            stdout = str(event.get("stdout", ""))
            stderr = str(event.get("stderr", ""))
            if state == "applied":
                self._append_log("Mod injector completed successfully.")
            else:
                self._append_log(f"Mod injector finished with state: {state} (code: {returncode})")
                if stdout:
                    self._append_log(f"Injector stdout: {stdout[-500:]}")
                if stderr:
                    self._append_log(f"Injector stderr: {stderr[-500:]}")
        elif kind == "disconnected":
            self._connection_pending = False; self._room_connected = False; self._set_connection_controls(True)
            self._chat_pending_text = None; self._set_chat_enabled(False); self._set_hints_state("disconnected")
            self.uninstall_button.setEnabled(False)
            self.connection_badge.setText("OFFLINE")
            self.connection_badge.setProperty("connected", False)
            self.connection_badge.style().unpolish(self.connection_badge)
            self.connection_badge.style().polish(self.connection_badge)
            self.session_player_name.setText("NO ROOM CONNECTED")
            self.player_team.setText("Team —")
            self.player_slot.setText("Slot —")
            self.player_inventory.setText("Connect to restore inventory")
            self.player_ammo_refills.setText(f"AMMO REFILLS — · {self.ammo_refill_keybind.value()}")
            self.resync_inventory_button.setEnabled(False)
            self.session_uninstall_button.setEnabled(False)
            self.drift.hide(); self.room_summary.setText("No room connected. Join a room to start playing.")
            for key in self.statuses:
                self._set_status(key, "waiting", self.COLORS["muted"])
            self._set_setup_state("disconnected")
            self._set_home("SESSION ENDED", "Update room details or reconnect.", "JOIN A ROOM", "OFFLINE")
        elif kind in {"setup_failed", "error"}:
            message = str(event.get("message", "Unknown error"))
            if not self._room_connected:
                self._connection_pending = False; self._set_connection_controls(True); self._set_status("room", "failed", self.COLORS["bad"])
                self._set_home("CONNECTION FAILED", message, "RETRY JOIN", "CONNECTION FAILED")
            else:
                self._set_status("mod", "failed", self.COLORS["bad"]); self._set_setup_state("failed", message)
        elif kind == "warning":
            self._append_log("Warning: " + str(event.get("message", "")))

    def _append_log(self, text: str) -> None:
        sanitized = redact_secrets(text).replace("\r", " ").strip()
        if not sanitized:
            return
        if hasattr(self, "log") and self.log is not None:
            self.log.appendPlainText(sanitized)
        if hasattr(self, "session_log") and self.session_log is not None:
            self.session_log.appendPlainText(sanitized)

    def _append_session_event(self, event: dict[str, object]) -> None:
        kind = str(event.get("type", "event"))
        if "heartbeat" in kind.casefold() or kind == "log":
            return
        details = []
        for key in ("endpoint", "slot", "seed_name", "state", "code", "reason", "message"):
            value = event.get(key)
            if value not in (None, ""):
                details.append(f"{key}={value}")
        self._append_log(
            f"{datetime.now().strftime('%H:%M:%S')} {kind}: {' | '.join(details) or 'received'}"
        )

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
