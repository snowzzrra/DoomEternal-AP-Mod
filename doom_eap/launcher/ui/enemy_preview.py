"""Present the current enemy configuration without editing option values."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget

from .controls import _label
from .strings import text


class EnemyPreview(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        heading.addWidget(_label(text("launcher.enemy_preview", "title"), "fieldLabel"))
        heading.addStretch()
        self.mode = _label("", "muted")
        heading.addWidget(self.mode)
        layout.addLayout(heading)
        self.content = QWidget()
        body = QVBoxLayout(self.content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(12)
        self.values = {}
        for index, key in enumerate(("intensity", "density", "variety", "size_variation", "content")):
            cell = QVBoxLayout()
            cell.setSpacing(4)
            cell.addWidget(_label(text("common", key) if key in {"density", "variety"} else text("launcher.enemy_preview", key), "muted"))
            value = _label("")
            cell.addWidget(value)
            self.values[key] = value
            grid.addLayout(cell, index // 3, index % 3)
            grid.setColumnStretch(index % 3, 1)
        body.addLayout(grid)
        for key in ("mix", "speed"):
            body.addWidget(_label(text("common", key) if key in {"density", "variety"} else text("launcher.enemy_preview", key), "muted"))
            value = _label("", "muted")
            value.setTextFormat(Qt.TextFormat.PlainText)
            self.values[key] = value
            body.addWidget(value)
        layout.addWidget(self.content)
        self.error = _label("", "warning")
        self.error.hide()
        layout.addWidget(self.error)

    def render(self, values):
        self.error.hide()
        self.content.show()
        self.mode.show()
        self.mode.setText(values["mode"])
        for key, label in self.values.items():
            label.setText(values[key])

    def set_error(self, message):
        self.content.hide()
        self.mode.hide()
        self.error.setText(message)
        self.error.show()
