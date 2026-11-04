"""Compose Play and render its milestones; application commands stay in the window/controller."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QTabWidget, QVBoxLayout, QWidget

from .strings import text


class PlayView(QWidget):
    def __init__(self, connection, session, progress, items, activity, hints, chat):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        self.milestones = []
        steps = QGridLayout()
        for index, key in enumerate(("game_found", "room_connected", "ready_to_play")):
            label = QLabel(text("launcher.play", key))
            label.setObjectName("milestone")
            label.setProperty("milestoneTitle", label.text())
            label.setWordWrap(True)
            label.setAccessibleName(label.text())
            self.milestones.append(label)
            steps.addWidget(label, 0, index)
            steps.setColumnStretch(index, 1)
        layout.addLayout(steps)
        self.guidance = QLabel(text("launcher.play", "connect_guidance"))
        self.guidance.setWordWrap(True)
        layout.addWidget(self.guidance)
        self.launch_option_box = QFrame()
        self.launch_option_box.hide()
        layout.addWidget(self.launch_option_box)
        content = QHBoxLayout()
        self.connection = QScrollArea()
        self.connection.setWidgetResizable(True)
        self.connection.setFrameShape(QFrame.Shape.NoFrame)
        self.connection.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.connection.setWidget(connection)
        self.connection.setMinimumWidth(260)
        self.connection.setMaximumWidth(340)
        content.addWidget(self.connection)
        current = QVBoxLayout()
        current.setSpacing(8)
        self.session = session
        current.addWidget(session)
        current.addWidget(progress)
        self.tabs = QTabWidget()
        for widget, key in ((items, "items"), (activity, "activity"), (hints, "hints")):
            self.tabs.addTab(widget, text("launcher.navigation", key))
        current.addWidget(self.tabs, 1)
        current.addWidget(chat)
        content.addLayout(current, 1)
        layout.addLayout(content, 1)

    def set_launch_option(self, label, field, copy, *, enabled):
        self.launch_option_enabled = enabled
        layout = QHBoxLayout(self.launch_option_box)
        layout.setContentsMargins(0, 0, 0, 0)
        for widget in (label, field, copy):
            layout.addWidget(widget)
            widget.show()
        self.launch_option_field = field

    def render(self, model):
        for label, ready in zip(
            self.milestones, (model.game_found, model.room_connected, model.ready_to_play), strict=False
        ):
            label.setProperty("complete", ready)
            label.setText(
                text(
                    "launcher.play",
                    "step_complete" if ready else "step_pending",
                    title=label.property("milestoneTitle"),
                )
            )
            label.setAccessibleDescription(text("launcher.play", "complete" if ready else "pending"))
            label.style().unpolish(label)
            label.style().polish(label)
        self.connection.setVisible(not model.room_connected)
        self.session.setVisible(model.room_connected)
        key = (
            "confirmed_guidance"
            if model.game_confirmed
            else "ready_guidance"
            if model.ready_to_play
            else "connect_guidance"
        )
        self.guidance.setText(text("launcher.play", key))
        self.launch_option_box.setVisible(self.launch_option_enabled and bool(model.launch_option))
        self.launch_option_field.setText(model.launch_option)

    def focus_connection(self):
        self.connection.show()
