"""Reusable launcher controls and layout helpers; no application actions."""

from __future__ import annotations

from typing import ClassVar, cast

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QKeyEvent,
    QPainter,
    QPalette,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QStyleOptionButton,
    QStyleOptionFrame,
    QVBoxLayout,
    QWidget,
)

from doom_eap.launcher.launcher_controller import normalize_ammo_refill_keybind

from .doom_style import DoomButton, DoomFrame, animations_enabled
from .option_grid import ColumnFlow, ListCheckBox, stepper
from .strings import option_text
from .strings import text as player_text

QPushButton = DoomButton


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


class ToggleSwitch(QCheckBox):
    def __init__(self, label=""):
        super().__init__(label)
        self.setObjectName("switch")

    def paintEvent(self, event):
        super().paintEvent(event)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        rect = QRectF(self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, self))
        colors = self.window().COLORS
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(colors["accent" if self.isChecked() else "line"]), 1))
        painter.setBrush(QColor(colors["accent"]) if self.isChecked() else Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 9, 9)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colors["ink" if self.isChecked() else "muted"]))
        painter.drawEllipse(QRectF(rect.x() + (21 if self.isChecked() else 3), rect.y() + 3, 12, 12))


class ElidedLabel(QLabel):
    def __init__(self, value="", name=""):
        super().__init__()
        self.setObjectName(name)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(False)
        self.setText(value)

    def setText(self, value):
        self.setToolTip(value)
        self._elide()

    def _elide(self):
        super().setText(self.fontMetrics().elidedText(self.toolTip(), Qt.TextElideMode.ElideMiddle, self.width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()


class ElidedPath(QLineEdit):
    def __init__(self, value=""):
        super().__init__(value)
        self.setReadOnly(True)
        self.setToolTip(value)
        self.textChanged.connect(self.setToolTip)

    def paintEvent(self, event):
        option = QStyleOptionFrame()
        self.initStyleOption(option)
        painter = QPainter(self)
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_PanelLineEdit, option, painter, self)
        area = self.contentsRect().adjusted(8, 0, -8, 0)
        painter.setPen(self.palette().color(QPalette.ColorRole.Text))
        painter.drawText(area, Qt.AlignmentFlag.AlignVCenter,
                         self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideMiddle, area.width()))


class _AmmoRefillKeyEdit(QPushButton):
    captured = Signal(str)
    rejected = Signal(str)

    def __init__(self, value: str):
        super().__init__()
        self.capturing = False
        self.setFixedWidth(140)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.clicked.connect(self._capture)
        self.set_value(value)

    def set_value(self, value: str) -> None:
        self._value = normalize_ammo_refill_keybind(value)
        self.capturing = False
        self.setText(self._value or player_text("launcher.app_window", "not_set"))

    def value(self) -> str:
        return self._value

    def _capture(self):
        self.capturing = True
        self.setText(player_text("launcher.settings", "press_key"))
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def focusOutEvent(self, event):
        self.set_value(self._value)
        super().focusOutEvent(event)

    def event(self, event):
        if event.type() == QEvent.Type.KeyPress and self.capturing:
            self.keyPressEvent(event)
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self.capturing:
            super().keyPressEvent(event)
            return
        if event.key() == Qt.Key.Key_Escape:
            self.set_value(self._value)
            event.accept()
            return
        token = _simple_physical_key_token(event)
        if token is None:
            self.rejected.emit(
                player_text("launcher.player_file", "key_choice_help")
            )
            event.accept()
            return
        self.set_value(token)
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
            colors = self.window().COLORS
            painter.setPen(QColor(colors["accent"] if active else colors["line"]))
            painter.setBrush(QColor(colors["accent"] if active else colors["panel2"]))
            painter.drawPolygon(shape)


_ScanlineFrame = DoomFrame
_ScanlineButton = DoomButton


class AmmoRefillKeyControl(QWidget):
    """Capture one supported physical key or UNBOUND."""

    changed = Signal(str)
    rejected = Signal(str)

    def __init__(self, value: str = "F9", *, default: str = "F9"):
        super().__init__()
        self.default = default
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.editor = _AmmoRefillKeyEdit(value)
        self.editor.setToolTip(
            player_text("launcher.ui", "key_capture_help")
        )
        self.editor.captured.connect(self._emit_value)
        self.editor.rejected.connect(self.rejected)
        clear = QPushButton(player_text('common', 'clear'))
        clear.clicked.connect(self._clear)
        reset = QPushButton(player_text("launcher.ui", "reset"))
        self.reset = reset
        clear.setObjectName("link")
        reset.setObjectName("link")
        reset.clicked.connect(lambda: self.set_value(default, emit=True))
        layout.addWidget(self.editor)
        layout.addWidget(clear)
        layout.addWidget(reset)
        layout.addStretch()
        self.reset.setVisible(value != default)

    def value(self) -> str:
        return self.editor.value()

    def set_value(self, value: str, *, emit: bool = False) -> None:
        self.editor.set_value(value)
        self.reset.setVisible(self.value() != self.default)
        if emit:
            self._emit_value()

    def _emit_value(self) -> None:
        self.reset.setVisible(self.value() != self.default)
        self.changed.emit(self.value())

    def _clear(self) -> None:
        self.editor.set_value("")
        self._emit_value()


class _OptionCategory(DoomFrame):
    """Keyboard-accessible category with a short, interruptible reveal."""

    def __init__(self, title: str, *, expanded: bool = True):
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)
        heading = QHBoxLayout()
        heading.setSpacing(4)
        self.arrow = QLabel('▾' if expanded else '▸')
        self.arrow.setObjectName("muted")
        self.arrow.setFixedWidth(12)
        self.toggle = QPushButton()
        self.toggle.setText(title.replace("&", "&&"))
        self.toggle.setCheckable(True)
        self.toggle.setChecked(expanded)
        self.toggle.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.toggle.setObjectName("optionCategory")
        heading.addWidget(self.toggle)
        heading.addWidget(self.arrow)
        heading.addStretch()
        layout.addLayout(heading)
        self.body = QWidget()
        self.body.setObjectName("optionBody")
        self.contents = QVBoxLayout(self.body)
        self.contents.setContentsMargins(0, 8, 0, 0)
        self.contents.setSpacing(12)
        layout.addWidget(self.body)
        self.body.setVisible(expanded)
        self.animation = QPropertyAnimation(self.body, b"maximumHeight", self)
        self.animation.setDuration(160)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.finished.connect(self._finished)
        self.toggle.toggled.connect(self._toggle)

    def _toggle(self, expanded: bool) -> None:
        self.animation.stop()
        if not animations_enabled(self):
            self.arrow.setText('▾' if expanded else '▸')
            self.body.setVisible(expanded)
            self.body.setMaximumHeight(16777215)
            return
        self.arrow.setText('▾' if expanded else '▸')
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
        layout.setSpacing(4)
        self.special = QComboBox()
        self.special.addItem(player_text("launcher.ui", "exact_value"), None)
        self.special_values: dict[str, object] = {}
        for special in cast(list[dict[str, object]], option["special_values"]):
            key = str(special["key"])
            self.special_values[key] = special.get("value")
            self.special.addItem(option_text(option["key"], "choice", str(special["label"]), choice_key=key), key)
        layout.addWidget(self.special)
        self.spin = QSpinBox()
        self.spin.setRange(cast(int, option["minimum"]), cast(int, option["maximum"]))
        self.numeric_row = stepper(self.spin)
        layout.addWidget(self.numeric_row)
        self.spin.valueChanged.connect(self.valueChanged)
        self.special.currentIndexChanged.connect(self._special_changed)
        self.special.currentIndexChanged.connect(self.valueChanged)
        self.setValue(option["default"])

    def _special_changed(self, _index: int) -> None:
        key = self.special.currentData()
        if key is None:
            self.numeric_row.show()
            self.spin.setEnabled(True)
            return
        value = self.special_values.get(str(key))
        displayable = (
            isinstance(value, int)
            and not isinstance(value, bool)
            and self.spin.minimum() <= value <= self.spin.maximum()
        )
        self.numeric_row.setVisible(displayable)
        if displayable:
            self.spin.setValue(cast(int, value))
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
    """Keep explicit values distinct from Auto while arranging entries by column."""

    valueChanged = Signal()

    def __init__(self, option, *, selected_only=False):
        super().__init__()
        self.entries = {
            entry["key"]: {**entry, "label": option_text(option.get("key", ""), "choice", entry["label"],
                                                        choice_key=entry["key"])}
            for entry in option["entries"]
        }
        self.range_bounds = {"minimum", "maximum", "early_minimum", "early_maximum", "late_minimum", "late_maximum"}
        self.is_attribute = self.range_bounds <= self.entries.keys()
        self.selected_only = selected_only
        self._values = {}
        self.fields = {}
        self.rows = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.flow = ColumnFlow()
        layout.addWidget(self.flow)
        self.clear = QPushButton(player_text('common', 'clear'))
        self.clear.setObjectName("link")
        self.clear.setProperty("compact", True)
        self.clear.clicked.connect(self._clear)
        self.header_actions = (self.clear,)
        for key, entry in self.entries.items():
            self._add_entry(key, entry)
        self.setValue(option["default"])

    def _add_entry(self, key, entry):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        name = ElidedLabel(str(entry["label"]))
        name.setToolTip(str(entry["label"]))
        layout.addWidget(name, 1)
        spin = QSpinBox()
        spin.setRange(entry["minimum"] - 1, entry["maximum"])
        spin.setSpecialValueText(player_text('common', 'auto'))
        spin.setAccessibleName(str(entry["label"]))
        spin.valueChanged.connect(lambda value, key=key: self._changed(key, value))
        layout.addWidget(stepper(spin, compact=True))
        self.fields[key] = spin
        self.rows[key] = row
        self.flow.add_entry(row)

    def _changed(self, key, value):
        values = self.value()
        if value < self.entries[key]["minimum"]:
            self._remove_value(values, key)
        else:
            self._set_entry(values, key, value)
        self.setValue(values)
        self.valueChanged.emit()

    def _set_entry(self, values, key, value):
        if self.is_attribute:
            species, separator, bound = key.rpartition(".")
            prefix = f"{species}." if separator else ""
            bound = bound if separator else key
            active = {"minimum", "maximum"} if bound in {"minimum", "maximum"} else self.range_bounds - {"minimum", "maximum"}
            for other in self.range_bounds - active:
                values.pop(prefix + other, None)
            for other in active:
                values.setdefault(prefix + other, value)
        values[key] = value

    def _remove_value(self, values, key):
        if self.is_attribute:
            species, separator, _bound = key.rpartition(".")
            prefix = f"{species}." if separator else ""
            for bound in self.range_bounds:
                values.pop(prefix + bound, None)
        else:
            values.pop(key, None)

    def _clear(self):
        self.setValue({})
        self.valueChanged.emit()

    def value(self):
        return dict(self._values)

    def setValue(self, values):
        self._values = dict(values)
        for key, spin in self.fields.items():
            blocked = spin.blockSignals(True)
            spin.setValue(values.get(key, self.entries[key]["minimum"] - 1))
            spin.blockSignals(blocked)
            self.rows[key].setVisible(not self.selected_only or key in values)
        self.flow.reflow()


class OptionSetControl(QWidget):
    """Schema-driven compact chooser for OptionSet values."""

    LOCKED_PREFIX = player_text("launcher.player_file", "required_by_your_goal")
    UNAVAILABLE_PREFIX = player_text("launcher.player_file", "unavailable_in_this_room")

    TOOLTIPS: ClassVar = {
        "Acquire the Unmaykr": player_text(
            "launcher.player_file", "goal_unmaykr_help"
        ),
        "Complete All Included Missions": player_text(
            "launcher.player_file", "goal_all_missions_help"
        ),
        "Complete All Slayer Gates": player_text(
            "launcher.player_file", "goal_slayer_gates_help"
        ),
        "Complete All Escalation Encounters": player_text(
            "launcher.player_file", "goal_escalations_help"
        ),
        "Complete All Secret Encounters": player_text(
            "launcher.player_file", "goal_encounters_help"
        ),
        "Complete All Mission Challenges": player_text(
            "launcher.player_file", "goal_challenges_help"
        ),
        "Complete All Weapon Mastery Challenges": player_text(
            "launcher.player_file", "goal_masteries_help"
        ),
    }

    def __init__(self, option: dict[str, object]):
        super().__init__()
        self.checks: list[tuple[object, QCheckBox]] = []
        self._locked: set[str] = set()
        self._unavailable: set[str] = set()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        actions = QHBoxLayout()
        actions.setSpacing(12)
        select_all = QPushButton(player_text("launcher.ui", "select_all"))
        select_all.clicked.connect(lambda: self._set_all(True))
        clear = QPushButton(player_text('common', 'clear'))
        clear.clicked.connect(lambda: self._set_all(False))
        for button in (select_all, clear):
            button.setObjectName("link")
            button.setProperty("compact", True)
        self.header_actions = (select_all, clear)
        self.flow = ColumnFlow()
        self.grid = self.flow.grid
        default = option.get("default", [])
        selected = set(default) if isinstance(default, list) else set()
        for choice in cast(list[object], option.get("choices", [])):
            if not isinstance(choice, dict):
                continue
            key = choice.get("key")
            check = ListCheckBox(option_text(option["key"], "choice", str(choice.get("label", key)), choice_key=key))
            check.setObjectName("requirementOption")
            check.setToolTip(self.TOOLTIPS.get(str(key), ""))
            check.setChecked(key in selected)
            self.flow.add_entry(check)
            self.checks.append((key, check))
        if option.get("key") in {"local_items", "non_local_items"}:
            search = QLineEdit()
            search.setPlaceholderText(player_text("launcher.ui", "find_an_item"))
            search.textChanged.connect(
                self._filter
            )
            layout.addWidget(search)
            scroll = QScrollArea()
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidgetResizable(True)
            scroll.setWidget(self.flow)
            scroll.setFixedHeight(240)
            layout.addWidget(scroll)
        else:
            layout.addWidget(self.flow)
        self._refresh_lock_state()

    def _filter(self, text):
        for _, check in self.checks:
            check.setVisible(text.casefold() in check.text().casefold())
        self.flow.reflow()

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
            check.setToolTip(prefix + check.text() + "\n" + self.TOOLTIPS.get(name, ""))
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
    card = DoomFrame()
    card.setObjectName(name)
    return card
