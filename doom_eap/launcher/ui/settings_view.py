"""Display launcher preferences; controls retain their existing persistence handlers."""

from PySide6.QtWidgets import QFormLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from .strings import text


class SettingsView(QScrollArea):
    def __init__(self, refill, special, killsanity, grace):
        super().__init__()
        self.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(24, 16, 24, 16)
        title = QLabel(text("launcher.navigation", "settings"))
        title.setObjectName("title")
        layout.addWidget(title)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        for key, control in (
            ("ammo_key", refill),
            ("special_key", special),
            ("killsanity", killsanity),
            ("deathlink", grace),
        ):
            label = QLabel(text("launcher.settings", key))
            label.setBuddy(control)
            form.addRow(label, control)
        layout.addLayout(form)
        detail = QLabel(text("launcher.settings", "deathlink_help"))
        detail.setWordWrap(True)
        layout.addWidget(detail)
        killsanity.setToolTip(text("launcher.settings", "killsanity_help"))
        layout.addStretch()
        self.setWidget(body)
