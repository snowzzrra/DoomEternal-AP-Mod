"""Responsive option tiles and column-first collections; values stay in their controls."""

import math
import re

from PySide6.QtCore import QEvent, QPointF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QTextLayout
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QStyleOptionButton,
    QWidget,
)

from .doom_style import COLORS, DoomButton
from .strings import text


def stepper(spin: QSpinBox, *, compact=False):
    body = QWidget()
    body.setObjectName("compactStepper" if compact else "stepper")
    height = 28 if compact else 36
    body.setFixedHeight(height)
    layout = QHBoxLayout(body)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
    spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
    if compact:
        spin.setFixedWidth(48)
    for key, name, callback in (("\N{MINUS SIGN}", "stepDown", spin.stepDown), ("+", "stepUp", spin.stepUp)):
        button = DoomButton(key)
        button.setObjectName(name)
        button.setFixedSize(height, height)
        button.clicked.connect(callback)
        button.setAccessibleName(text('launcher.player_file', 'decrease') if name == 'stepDown' else text('launcher.player_file', 'increase'))
        layout.addWidget(button)
        if name == "stepDown":
            layout.addWidget(spin, 1)
    return body


class TileGrid(QWidget):
    def __init__(self):
        super().__init__()
        self.tiles = []
        self.columns = 0
        self._pending = False
        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.timeout.connect(self.reflow)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(24)
        self.grid.setVerticalSpacing(20)

    def add_tile(self, widget, *, wide=False):
        widget.setParent(self)
        widget.setVisible(True)
        widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        widget.setMinimumWidth(0)
        widget.installEventFilter(self)
        self.tiles.append((widget, wide))
        self.reflow()

    def eventFilter(self, watched, event):
        if event.type() in {QEvent.Type.ShowToParent, QEvent.Type.HideToParent} and not self._pending:
            self._pending = True
            self._reflow_timer.start(0)
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        columns = 4 if self.width() >= 1500 else 2 if self.width() < 960 else 3
        if columns != self.columns:
            self.reflow()
        super().resizeEvent(event)

    def reflow(self):
        self._pending = False
        columns = 4 if self.width() >= 1500 else 2 if self.width() < 960 else 3
        while self.grid.count():
            self.grid.takeAt(0)
        for column in range(max(columns, self.columns)):
            self.grid.setColumnStretch(column, int(column < columns))
        self.columns = columns
        row, column = 0, 0
        for widget, wide in self.tiles:
            if widget.isHidden():
                continue
            if wide:
                row += bool(column)
                self.grid.addWidget(widget, row, 0, 1, columns, Qt.AlignmentFlag.AlignTop)
                row, column = row + 1, 0
            else:
                self.grid.addWidget(widget, row, column, Qt.AlignmentFlag.AlignTop)
                column += 1
                if column == columns:
                    row, column = row + 1, 0
        self.updateGeometry()


class ColumnFlow(QWidget):
    def __init__(self):
        super().__init__()
        self.entries = []
        self._columns = 0
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(24)
        self.grid.setVerticalSpacing(8)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def add_entry(self, widget):
        widget.setMinimumWidth(240)
        policy = widget.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Expanding)
        widget.setSizePolicy(policy)
        self.entries.append(widget)
        self.grid.addWidget(widget, len(self.entries) - 1, 0)

    def heightForWidth(self, width):
        entries = [widget for widget in self.entries if not widget.isHidden()]
        rows = math.ceil(len(entries) / self._column_count(width))
        height = max((widget.sizeHint().height() for widget in entries), default=0)
        return rows * height + max(0, rows - 1) * 8

    def resizeEvent(self, event):
        if self._column_count(self.width()) != self._columns:
            self.reflow()
        super().resizeEvent(event)

    def reflow(self):
        entries = [widget for widget in self.entries if not widget.isHidden()]
        columns = self._column_count(self.width())
        for column in range(max(columns, self._columns)):
            self.grid.setColumnStretch(column, int(column < columns))
        self._columns = columns
        rows = max(1, math.ceil(len(entries) / columns))
        while self.grid.count():
            self.grid.takeAt(0)
        for index, widget in enumerate(entries):
            self.grid.addWidget(widget, index % rows, index // rows)
        self.updateGeometry()

    def _column_count(self, width):
        count = sum(not widget.isHidden() for widget in self.entries)
        return max(1, min(count, (width + 24) // 264))


class Description(QLabel):
    def __init__(self, description):
        plain = re.sub(r"\*\*?|__?", "", str(description)).strip()
        first = re.split(r"(?<=[.!?])\s+", plain, maxsplit=1)[0].replace("\n", " ")
        super().__init__(first)
        self.setObjectName("muted")
        self.setWordWrap(True)
        self.setMinimumWidth(0)
        self.setFixedHeight(self.fontMetrics().lineSpacing() * 2)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in {QEvent.Type.FontChange, QEvent.Type.StyleChange}:
            self.setFixedHeight(self.fontMetrics().lineSpacing() * 2)

    def sizeHint(self):
        return QSize(0, self.fontMetrics().lineSpacing() * 2)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(QColor(COLORS["muted"]))
        painter.setFont(self.font())
        layout = QTextLayout(self.text(), self.font())
        layout.beginLayout()
        first = layout.createLine()
        if not first.isValid():
            layout.endLayout()
            return
        first.setLineWidth(self.width())
        split = first.textLength()
        layout.endLayout()
        metrics = self.fontMetrics()
        painter.drawText(QPointF(0, metrics.ascent()), self.text()[:split].rstrip())
        tail = metrics.elidedText(self.text()[split:].strip(), Qt.TextElideMode.ElideRight, self.width())
        painter.drawText(QPointF(0, metrics.ascent() + metrics.lineSpacing()), tail)


class ListCheckBox(QCheckBox):
    def paintEvent(self, event):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, max(0, self.width() - 22))
        painter = QPainter(self)
        self.style().drawControl(QStyle.ControlElement.CE_CheckBox, option, painter, self)
        if self.isChecked():
            rect = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(COLORS["ink"]), 1.5))
            painter.drawLine(rect.left() + 3, rect.center().y(), rect.left() + 6, rect.bottom() - 3)
            painter.drawLine(rect.left() + 6, rect.bottom() - 3, rect.right() - 2, rect.top() + 3)
