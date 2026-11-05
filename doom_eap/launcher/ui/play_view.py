"""Compose Play and render its milestones; application commands stay in the window/controller."""

from PySide6.QtCore import QPropertyAnimation, Qt
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..events import ActionId, InstallationState
from ..presenters.play_presenter import PlayPresenter
from .doom_style import DoomTabBar, animations_enabled
from .strings import text


class PlayView(QWidget):
    def __init__(self, connection, session, progress, items, activity, hints, chat, install_action):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        self.milestones = []
        self.milestone_icons = []
        self.step_details = []
        self._current_step = None
        self._pulse_index = None
        self._pulse_animation = None
        trail = QWidget()
        trail.setFixedHeight(40)
        steps = QHBoxLayout(trail)
        steps.setContentsMargins(0, 0, 0, 0)
        steps.setSpacing(0)
        for index, key in enumerate(("game_found", "room_connected", "ready_to_play")):
            column = QWidget()
            column.setMinimumWidth(0)
            column.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            step = QVBoxLayout(column)
            step.setContentsMargins(0, 0, 16 if index < 2 else 0, 0)
            step.setSpacing(4)
            heading = QHBoxLayout()
            heading.setSpacing(4)
            icon = QLabel()
            self.milestone_icons.append(icon)
            heading.addWidget(icon)
            label = QLabel(text("launcher.play", key))
            label.setObjectName("milestone")
            label.setProperty("milestoneTitle", label.text())
            label.setAccessibleName(label.text())
            self.milestones.append(label)
            heading.addWidget(label)
            if index < 2:
                heading.addSpacing(12)
                line = QFrame()
                line.setObjectName("stepLine")
                heading.addWidget(line, 1, Qt.AlignmentFlag.AlignVCenter)
            else:
                heading.addStretch()
            step.addLayout(heading)
            detail = QLabel()
            detail.setTextFormat(Qt.TextFormat.PlainText)
            detail.setObjectName("muted")
            self.step_details.append(detail)
            step.addWidget(detail)
            steps.addWidget(column, 1)
        layout.addWidget(trail)
        self.next_step = QWidget()
        next_step = QHBoxLayout(self.next_step)
        next_step.setContentsMargins(0, 0, 0, 0)
        next_step.setSpacing(12)
        self.guidance = QLabel(text("launcher.play", "connect_guidance"))
        self.guidance.setWordWrap(True)
        next_step.addWidget(self.guidance, 1)
        self.install_action = install_action
        next_step.addWidget(self.install_action)
        layout.addWidget(self.next_step)
        layout.addWidget(progress)
        self.launch_option_box = QFrame()
        self.launch_option_box.hide()
        layout.addWidget(self.launch_option_box)
        content = QHBoxLayout()
        content.setSpacing(16)
        self.connection = connection
        self.connection.setFixedWidth(320)
        content.addWidget(self.connection)
        current = QVBoxLayout()
        current.setSpacing(16)
        self.session = session
        session.setParent(self)
        session.hide()
        self.progress = progress
        self.tabs = QTabWidget()
        self.tabs.setTabBar(DoomTabBar())
        for widget, key in ((items, "items"), (activity, "activity"), (hints, "hints")):
            self.tabs.addTab(widget, text("launcher.navigation", key))
        current.addWidget(self.tabs, 1)
        current.addWidget(chat)
        content.addLayout(current, 1)
        layout.addLayout(content, 1)

    def set_launch_option(self, label, field, copy, *, enabled):
        self.launch_option_enabled = enabled
        layout = QVBoxLayout(self.launch_option_box)
        layout.setSpacing(4)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(label)
        label.show()
        row = QHBoxLayout()
        row.setSpacing(12)
        for widget in (field, copy):
            row.addWidget(widget)
            widget.show()
        layout.addLayout(row)
        self.launch_option_field = field

    def render(self, model):
        self._render_steps(model)
        self._render_details(model)
        self.guidance.setText(PlayPresenter.guidance(model))
        self._render_actions(model)
        self.next_step.setVisible(self.progress.isHidden())
        self.launch_option_box.setVisible(self.launch_option_enabled and bool(model.launch_option))
        self.launch_option_field.setText(model.launch_option)

    def _render_steps(self, model):
        completed = (model.game_found, model.room_connected, model.ready_to_play)
        current = 1 if not model.room_connected else next((index for index, ready in enumerate(completed) if not ready), 3)
        self._current_step = current
        pending = self._pending_step_labels(model)
        for index, (label, ready) in enumerate(zip(
            self.milestones, completed, strict=False
        )):
            self._render_step(index, label, ready, current, pending)
        self._update_pulse(current)

    def _render_step(self, index, label, ready, current, pending):
        label.setProperty("complete", ready)
        label.setProperty("current", index == current)
        icon = self.milestone_icons[index]
        icon.setProperty("tone", "accent" if ready else "ap" if index == current else "muted")
        state = "step_complete" if ready else "step_current" if index == current else "step_pending"
        icon.setText({"step_complete": "✓", "step_current": "●", "step_pending": "○"}[state].strip())
        label.setText(label.property("milestoneTitle") if ready else pending[index])
        label.setAccessibleName(label.text())
        icon.style().unpolish(icon)
        icon.style().polish(icon)
        label.setAccessibleDescription(text('common', 'ready') if ready else text('launcher.play', 'pending'))
        label.style().unpolish(label)
        label.style().polish(label)

    @staticmethod
    def _pending_step_labels(model):
        install = model.installation in {InstallationState.REQUIRED, InstallationState.INSTALLING, InstallationState.AWAITING_CONFIRMATION}
        return (text("launcher.play", "find_game"), text("launcher.play", "connect_room"),
                (text('launcher.play', 'install_room') if install else text('launcher.play', 'get_ready')))

    def _update_pulse(self, index):
        if not self.isVisible() or not animations_enabled(self) or index is None or index >= 3:
            index = None
        if index == self._pulse_index:
            return
        if self._pulse_animation is not None:
            self._pulse_animation.stop()
            self._pulse_animation.deleteLater()
            self.milestone_icons[self._pulse_index].setGraphicsEffect(None)
            self._pulse_animation = None
        self._pulse_index = index
        if index is None:
            return
        icon = self.milestone_icons[index]
        effect = QGraphicsOpacityEffect(icon)
        icon.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", icon)
        animation.setDuration(1600)
        animation.setKeyValueAt(0, 0.35)
        animation.setKeyValueAt(0.5, 1.0)
        animation.setKeyValueAt(1, 0.35)
        animation.setLoopCount(-1)
        self._pulse_animation = animation
        animation.start()

    def showEvent(self, event):
        super().showEvent(event)
        self._update_pulse(self._current_step)

    def hideEvent(self, event):
        self._update_pulse(None)
        super().hideEvent(event)

    def _render_actions(self, model):
        needs_install = model.room_connected and model.installation is InstallationState.REQUIRED
        exceptional = model.installation in {InstallationState.BLOCKED, InstallationState.FAILED}
        self.install_action.setVisible(needs_install or exceptional)
        if needs_install:
            self.install_action.setText(text('common', 'install_room_mod'))
        self.progress.setVisible(model.progress is not None or model.action in {
            ActionId.CONFIRM_INSTALL, ActionId.FINISH_INSTALL, ActionId.MANUAL_INSTALL,
        } or model.installation in {
            InstallationState.INSTALLING, InstallationState.AWAITING_CONFIRMATION,
            InstallationState.BLOCKED, InstallationState.FAILED,
        })

    def _render_details(self, model):
        self.step_details[1].setText(f"{model.server} · {model.player}" if model.room_connected else "")

    def focus_connection(self):
        self.connection.show()
