"""Reusable launcher controls and layout helpers; no application actions."""

from __future__ import annotations

from typing import ClassVar, cast

from PySide6.QtCore import (
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QKeyEvent,
    QPainter,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..launcher_controller import normalize_ammo_refill_keybind
from .strings import text as player_text


def _simple_physical_key_token(event: QKeyEvent) -> str | None:
    """Map only documented simple physical keys to canonical launcher tokens."""
    if event.isAutoRepeat() or event.modifiers() != Qt.KeyboardModifier.NoModifier:
        return None
    key = int(event.key())
    key_a, key_z = int(Qt.Key.Key_A), int(Qt.Key.Key_Z)
    key_0, key_9 = int(Qt.Key.Key_0), int(Qt.Key.Key_9)
    key_f1, key_f12 = int(Qt.Key.Key_F1), int(Qt.Key.Key_F12)
    if key_a <= key <= key_z:
        return chr(ord("A") + key - key_a)
    if key_0 <= key <= key_9:
        return str(key - key_0)
    if key_f1 <= key <= key_f12:
        return f"F{key - key_f1 + 1}"
    return {
        int(Qt.Key.Key_Space): "Space",
        int(Qt.Key.Key_Tab): "Tab",
        int(Qt.Key.Key_Backspace): "Backspace",
        int(Qt.Key.Key_Insert): "Insert",
        int(Qt.Key.Key_Delete): "Delete",
        int(Qt.Key.Key_Home): "Home",
        int(Qt.Key.Key_End): "End",
        int(Qt.Key.Key_PageUp): "PageUp",
        int(Qt.Key.Key_PageDown): "PageDown",
        int(Qt.Key.Key_Up): "Up",
        int(Qt.Key.Key_Down): "Down",
        int(Qt.Key.Key_Left): "Left",
        int(Qt.Key.Key_Right): "Right",
    }.get(key)


class _AmmoRefillKeyEdit(QLineEdit):
    captured = Signal(str)
    rejected = Signal(str)

    def __init__(self, value: str):
        super().__init__()
        self.setReadOnly(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.set_value(value)

    def set_value(self, value: str) -> None:
        self._value = normalize_ammo_refill_keybind(value)
        self.setText(self._value or player_text("launcher.app_window", "not_set"))

    def value(self) -> str:
        return self._value

    def keyPressEvent(self, event: QKeyEvent) -> None:
        token = _simple_physical_key_token(event)
        if token is None:
            self.rejected.emit(
                player_text("launcher.player_file", "choose_one_letter_number_function_key_or_navigation_key")
            )
            event.accept()
            return
        self._value = token
        self.setText(token)
        self.captured.emit(token)
        event.accept()


class _AmmoRefillPips(QWidget):
    """Three compact diagonal resource pips."""

    def __init__(self) -> None:
        super().__init__()
        self._available: int | None = None
        metrics = QFontMetrics(self.font())
        self.setMinimumSize(metrics.horizontalAdvance("MMM"), max(12, metrics.height() // 2))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_available(self, available: int | None) -> None:
        self._available = available
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = self.contentsRect().adjusted(1, 1, -1, -1)
        gap = max(2.0, bounds.width() / 24.0)
        width = max(7.0, (bounds.width() - gap * 2.0) / 3.0)
        height = max(6.0, min(float(bounds.height()), QFontMetrics(self.font()).height() * 0.55))
        top = bounds.center().y() - height / 2.0
        slant = min(5.0, width * 0.28)
        for index in range(3):
            left = bounds.left() + index * (width + gap)
            shape = QPolygonF(
                [
                    QPointF(left + slant, top),
                    QPointF(left + width, top),
                    QPointF(left + width - slant, top + height),
                    QPointF(left, top + height),
                ]
            )
            active = self._available is not None and index < self._available
            painter.setPen(QColor("#c7ef82") if active else QColor("#4a543c"))
            painter.setBrush(QColor("#9cdc4a") if active else QColor("#182019"))
            painter.drawPolygon(shape)


class _ScanlineFrame(QFrame):
    """Panel background with a static, clipped green-and-ink scan texture."""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        bounds = self.contentsRect().adjusted(1, 1, -1, -1)
        if bounds.width() < 32 or bounds.height() < 18:
            return
        painter = QPainter(self)
        painter.setClipRect(bounds)
        dpr = self.devicePixelRatioF()
        spacing = max(4.0, 7.0 / dpr)
        pen = QPen(QColor(133, 185, 83, 15))
        pen.setWidthF(1.0 / dpr)
        painter.setPen(pen)
        y = float(bounds.top()) + spacing * 0.5
        while y < bounds.bottom():
            painter.drawLine(QPointF(bounds.left(), y), QPointF(bounds.right(), y - 1.5 / dpr))
            y += spacing
        pen.setColor(QColor(0, 0, 0, 18))
        painter.setPen(pen)
        y = float(bounds.top()) + spacing
        while y < bounds.bottom():
            painter.drawLine(QPointF(bounds.left(), y), QPointF(bounds.right(), y - 1.5 / dpr))
            y += spacing


class _ScanlineButton(QPushButton):
    """Primary-action button with a quiet, non-text scanline edge."""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        bounds = self.contentsRect().adjusted(3, 3, -3, -3)
        if bounds.width() < 20 or bounds.height() < 14:
            return
        painter = QPainter(self)
        painter.setClipRect(bounds.left(), bounds.top(), min(6, bounds.width()), bounds.height())
        dpr = self.devicePixelRatioF()
        pen = QPen(QColor(15, 37, 17, 38))
        pen.setWidthF(1.0 / dpr)
        painter.setPen(pen)
        spacing = max(4.0, 6.0 / dpr)
        y = float(bounds.top()) + spacing * 0.5
        while y < bounds.bottom():
            painter.drawLine(QPointF(bounds.left(), y), QPointF(bounds.left() + 7, y - 1.0 / dpr))
            y += spacing


class AmmoRefillKeyControl(QWidget):
    """Capture one supported physical key or UNBOUND."""

    changed = Signal(str)
    rejected = Signal(str)

    def __init__(self, value: str = "F9", *, default: str = "F9"):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        self.editor = _AmmoRefillKeyEdit(value)
        self.editor.setToolTip(
            player_text("launcher.ui", "press_one_key_ctrl_alt_shift_and_numpad_keys_cannot_be_assigned")
        )
        self.editor.captured.connect(self._emit_value)
        self.editor.rejected.connect(self.rejected)
        clear = QPushButton(player_text("launcher.ui", "clear"))
        clear.clicked.connect(self._clear)
        reset = QPushButton(player_text("launcher.ui", "reset"))
        reset.clicked.connect(lambda: self.set_value(default, emit=True))
        layout.addWidget(self.editor, 1)
        layout.addWidget(clear)
        layout.addWidget(reset)

    def value(self) -> str:
        return self.editor.value()

    def set_value(self, value: str, *, emit: bool = False) -> None:
        self.editor.set_value(value)
        if emit:
            self._emit_value()

    def _emit_value(self) -> None:
        self.changed.emit(self.value())

    def _clear(self) -> None:
        self.editor.set_value("")
        self._emit_value()


class _OptionCategory(QFrame):
    """Keyboard-accessible category with a short, interruptible reveal."""

    def __init__(self, title: str, *, expanded: bool = True):
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 14)
        self.toggle = QToolButton()
        self.toggle.setText(title.replace("&", "&&"))
        self.toggle.setCheckable(True)
        self.toggle.setChecked(expanded)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.toggle.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.toggle.setStyleSheet(
            "QToolButton { color: #e9a64b; font-weight: 800; padding: 7px; text-align: left; } QToolButton:focus { border: 1px solid #e9a64b; }"
        )
        layout.addWidget(self.toggle)
        self.body = QWidget()
        self.contents = QVBoxLayout(self.body)
        self.contents.setContentsMargins(0, 8, 0, 0)
        self.contents.setSpacing(9)
        layout.addWidget(self.body)
        self.body.setVisible(expanded)
        self.animation = QPropertyAnimation(self.body, b"maximumHeight", self)
        self.animation.setDuration(160)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.finished.connect(self._finished)
        self.toggle.toggled.connect(self._toggle)

    def _toggle(self, expanded: bool) -> None:
        self.animation.stop()
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        current = self.body.height() if self.body.isVisible() else 0
        self.body.setVisible(True)
        self.animation.setStartValue(current)
        self.animation.setEndValue(self.body.sizeHint().height() if expanded else 0)
        self.animation.start()

    def _finished(self) -> None:
        self.body.setVisible(self.toggle.isChecked())
        if self.toggle.isChecked():
            self.body.setMaximumHeight(16777215)


class NamedRangeControl(QWidget):
    """Numeric named-range input."""

    valueChanged = Signal()

    def __init__(self, option: dict[str, object]):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.special = QComboBox()
        self.special.addItem(player_text("launcher.ui", "exact_value"), None)
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
        self.spin.valueChanged.connect(self.valueChanged)
        self.special.currentIndexChanged.connect(self._special_changed)
        self.special.currentIndexChanged.connect(self.valueChanged)
        self.setValue(option["default"])

    def _special_changed(self, _index: int) -> None:
        key = self.special.currentData()
        if key is None:
            self.numeric_row.show()
            self.slider.setEnabled(True)
            self.spin.setEnabled(True)
            return
        value = self.special_values.get(str(key))
        displayable = (
            isinstance(value, int)
            and not isinstance(value, bool)
            and self.slider.minimum() <= value <= self.slider.maximum()
        )
        self.numeric_row.setVisible(displayable)
        if displayable:
            self.spin.setValue(cast(int, value))
        self.slider.setEnabled(False)
        self.spin.setEnabled(False)

    def value(self) -> int | str:
        return self.spin.value() if self.special.currentData() is None else str(self.special.currentData())

    def setValue(self, value: object) -> None:
        if not isinstance(value, str):
            value = next((key for key, number in self.special_values.items() if number == value), value)
        if isinstance(value, str):
            index = self.special.findData(value)
            self.special.setCurrentIndex(index if index >= 0 else 0)
        else:
            self.special.setCurrentIndex(0)
            self.spin.setValue(cast(int, value))


class DictionaryControl(QWidget):
    """Edit explicit catalog values; absent entries use Auto."""

    valueChanged = Signal()

    def __init__(self, option: dict[str, object]):
        super().__init__()
        self.entries = {entry["key"]: entry for entry in option["entries"]}
        self.range_bounds = {"minimum", "maximum", "early_minimum", "early_maximum", "late_minimum", "late_maximum"}
        self.is_attribute = self.range_bounds <= self.entries.keys()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.picker = QComboBox()
        for key, entry in self.entries.items():
            self.picker.addItem(str(entry["label"]), key)
        self.amount = QSpinBox()
        self.picker.currentIndexChanged.connect(self._select_entry)
        apply = QPushButton(player_text("launcher.ui", "set_value"))
        apply.clicked.connect(self._set_entry)
        controls.addWidget(self.picker, 1)
        controls.addWidget(self.amount)
        controls.addWidget(apply)
        layout.addLayout(controls)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(
            [player_text("launcher.items", "item"), player_text("launcher.ui", "value")]
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setFixedHeight(150)
        layout.addWidget(self.table)
        remove = QPushButton(player_text("launcher.ui", "reset_selected_to_auto"))
        remove.clicked.connect(self._remove_entry)
        layout.addWidget(remove)
        self.setValue(option["default"])

    def _select_entry(self, _index: int = 0) -> None:
        key = self.picker.currentData()
        entry = self.entries[key]
        self.amount.setRange(entry["minimum"], entry["maximum"])
        self.amount.setValue(self.value().get(key, entry["minimum"]))

    def _set_entry(self) -> None:
        values = self.value()
        key = self.picker.currentData()
        if self.is_attribute:
            species, separator, bound = key.rpartition(".")
            prefix = f"{species}." if separator else ""
            bound = bound if separator else key
            active = (
                {"minimum", "maximum"}
                if bound in {"minimum", "maximum"}
                else self.range_bounds - {"minimum", "maximum"}
            )
            for other in self.range_bounds - active:
                values.pop(prefix + other, None)
            for other in active:
                values.setdefault(prefix + other, self.amount.value())
        values[key] = self.amount.value()
        self.setValue(values)
        self.valueChanged.emit()

    def _remove_entry(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        values = self.value()
        key = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if self.is_attribute:
            species, separator, _bound = key.rpartition(".")
            prefix = f"{species}." if separator else ""
            for bound in self.range_bounds:
                values.pop(prefix + bound, None)
        else:
            values.pop(key)
        self.setValue(values)
        self.valueChanged.emit()

    def value(self) -> dict[str, int]:
        return {
            self.table.item(row, 0).data(Qt.ItemDataRole.UserRole): int(self.table.item(row, 1).text())
            for row in range(self.table.rowCount())
        }

    def setValue(self, values: dict[str, int]) -> None:
        self.table.setRowCount(0)
        for key, value in values.items():
            row = self.table.rowCount()
            self.table.insertRow(row)
            item = QTableWidgetItem(str(self.entries[key]["label"]))
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.table.setItem(row, 0, item)
            self.table.setItem(row, 1, QTableWidgetItem(str(value)))
        self._select_entry()


class OptionSetControl(QWidget):
    """Schema-driven compact chooser for OptionSet values."""

    LOCKED_PREFIX = player_text("launcher.player_file", "required_by_your_goal")
    UNAVAILABLE_PREFIX = player_text("launcher.player_file", "unavailable_in_this_room")

    TOOLTIPS: ClassVar = {
        "Acquire the Unmaykr": player_text(
            "launcher.player_file", "collect_six_empyrean_keys_from_the_base_campaign_slayer_gates_th"
        ),
        "Complete All Included Missions": player_text(
            "launcher.player_file", "finish_every_mission_included_in_this_room_including_the_final_b"
        ),
        "Complete All Slayer Gates": player_text(
            "launcher.player_file", "complete_all_included_slayer_gates_six_in_the_base_campaign_plus"
        ),
        "Complete All Escalation Encounters": player_text(
            "launcher.player_file", "complete_both_wave_1_and_wave_2_of_every_escalation_encounter_in"
        ),
        "Complete All Secret Encounters": player_text(
            "launcher.player_file", "complete_every_secret_encounter_included_in_this_room_these_are"
        ),
        "Complete All Mission Challenges": player_text(
            "launcher.player_file", "complete_the_mission_challenges_in_every_included_mission_that_h"
        ),
        "Complete All Weapon Mastery Challenges": player_text(
            "launcher.player_file", "complete_every_included_weapon_mastery_challenge_enable_weapon_m"
        ),
    }

    def __init__(self, option: dict[str, object]):
        super().__init__()
        self.checks: list[tuple[object, QCheckBox]] = []
        self._locked: set[str] = set()
        self._unavailable: set[str] = set()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        actions = QHBoxLayout()
        select_all = QPushButton(player_text("launcher.ui", "select_all"))
        select_all.clicked.connect(lambda: self._set_all(True))
        clear = QPushButton(player_text("launcher.ui", "clear"))
        clear.clicked.connect(lambda: self._set_all(False))
        actions.addWidget(select_all)
        actions.addWidget(clear)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(5)
        default = option.get("default", [])
        selected = set(default) if isinstance(default, list) else set()
        for index, choice in enumerate(cast(list[object], option.get("choices", []))):
            if not isinstance(choice, dict):
                continue
            key = choice.get("key")
            check = QCheckBox(str(choice.get("label", key)))
            check.setObjectName("requirementOption")
            check.setToolTip(self.TOOLTIPS.get(str(key), ""))
            check.setChecked(key in selected)
            self.grid.addWidget(check, index, 0)
            self.checks.append((key, check))
        if option.get("key") in {"local_items", "non_local_items"}:
            search = QLineEdit()
            search.setPlaceholderText(player_text("launcher.ui", "find_an_item"))
            search.textChanged.connect(
                lambda text: [check.setVisible(text.casefold() in check.text().casefold()) for _, check in self.checks]
            )
            layout.addWidget(search)
            contents = QWidget()
            contents.setLayout(self.grid)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(contents)
            scroll.setFixedHeight(240)
            layout.addWidget(scroll)
        else:
            layout.addLayout(self.grid)
        self._refresh_lock_state()

    def resizeEvent(self, event) -> None:
        columns = 2 if self.width() >= 650 else 1
        if columns != getattr(self, "_columns", None):
            self._columns = columns
            for index, (_, check) in enumerate(self.checks):
                self.grid.addWidget(check, index // columns, index % columns)
        super().resizeEvent(event)

    def set_dependencies(self, locked: set[str], unavailable: set[str]) -> None:
        """Mark requirements implied by the Goal (locked) or absent from the room."""
        self._locked = {str(key) for key in locked}
        self._unavailable = {str(key) for key in unavailable}
        self._refresh_lock_state()

    def _refresh_lock_state(self) -> None:
        for key, check in self.checks:
            name = str(key)
            locked = name in self._locked
            unavailable = name in self._unavailable
            check.setEnabled(not locked and not unavailable)
            check.setProperty("goalLocked", locked)
            prefix = self.LOCKED_PREFIX if locked else self.UNAVAILABLE_PREFIX if unavailable else ""
            check.setToolTip(prefix + self.TOOLTIPS.get(name, ""))
            style = check.style()
            style.unpolish(check)
            style.polish(check)

    def _set_all(self, checked: bool) -> None:
        for key, check in self.checks:
            name = str(key)
            if name in self._locked or name in self._unavailable:
                continue
            check.setChecked(checked)

    def value(self) -> list[object]:
        return [key for key, check in self.checks if check.isChecked()]

    def setValue(self, value: object) -> None:
        selected = set(value) if isinstance(value, list) else set()
        for key, check in self.checks:
            check.setChecked(key in selected)


def _label(text: str, name: str = "", *, rich: bool = False) -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _scroll(widget: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(widget)
    return scroll


def _card(name: str = "card") -> QFrame:
    card = _ScanlineFrame() if name in {"hero", "actionStrip", "effectiveConfig"} else QFrame()
    card.setObjectName(name)
    return card
