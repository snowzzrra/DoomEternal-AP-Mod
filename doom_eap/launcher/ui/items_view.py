"""Render Items and emit typed intentions; the presenter guards confirmation."""

from PySide6.QtCore import QDateTime, QLocale, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from doom_eap.presentation import ARCHIPELAGO_PRESENTATION_COLORS, item_classification_color_key
from doom_eap.runtime.ledger import ItemStatus, ResolutionAction

from ..items import LedgerAvailability
from .doom_style import DoomButton as QPushButton
from .doom_style import ItemScanlines
from .strings import text


class ItemsView(QWidget):
    query_requested = Signal(object)
    resolution_requested = Signal(object)

    def __init__(self, presenter):
        super().__init__()
        self.presenter = presenter
        self.item_names = {}
        self.player_names = {}
        self.item_classifications = {}
        self.row_actions = {}
        self.row_confirm = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        filters = QHBoxLayout()
        filters.setSpacing(12)
        filters.addStretch()
        self.blocked = QPushButton()
        self.blocked.setObjectName("link")
        self.blocked.setCheckable(True)
        self.blocked.toggled.connect(self._filter_changed)
        filters.addWidget(self.blocked)
        layout.addLayout(filters)
        self.status = QLabel()
        self.status.setObjectName("empty")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status, 1)
        self.list = QListWidget()
        self.list.setMouseTracking(True)
        self.list.setItemDelegate(ItemScanlines(self.list))
        self.list.setAccessibleName(text("launcher.navigation", "items"))
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.verticalScrollBar().valueChanged.connect(self._scroll)
        layout.addWidget(self.list, 1)
        self.details = QLabel()
        self.details.setObjectName("muted")
        self.details.setWordWrap(True)
        layout.addWidget(self.details)
        self._build_actions(layout)
        self.render()

    def _build_actions(self, layout):
        local = QHBoxLayout()
        local.setSpacing(12)
        self.local_label = QLabel()
        self.local_label.setTextFormat(Qt.TextFormat.PlainText)
        local.addWidget(self.local_label)
        self.local_buttons = {}
        for kind in ResolutionAction:
            button = QPushButton(text("launcher.items", kind.value))
            button.setObjectName("link")
            button.clicked.connect(lambda checked=False, action=kind: self._choose_local(action))
            self.local_buttons[kind] = button
            local.addWidget(button)
        layout.addLayout(local)
        self.confirm = QPushButton(text("launcher.items", "confirm"))
        self.confirm.setObjectName("link")
        self.confirm.clicked.connect(self._confirm)
        self.cancel = QPushButton(text("launcher.items", "cancel"))
        self.cancel.setObjectName("link")
        self.cancel.clicked.connect(self._cancel)
        local.addWidget(self.confirm)
        local.addWidget(self.cancel)
    def request(self, *, more=False, refresh=False):
        query = self.presenter.query(more=more, refresh=refresh)
        if query is not None:
            self.query_requested.emit(query)
        self.render()

    def _scroll(self, value):
        bar = self.list.verticalScrollBar()
        if value == bar.maximum() and self.presenter.model.more and not self.presenter.model.loading:
            self.request(more=True)

    def _filter_changed(self, checked):
        query = self.presenter.filter_blocked(checked)
        if query is not None:
            self.query_requested.emit(query)
        self.render()

    def selected_index(self):
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def render(self):
        self._render_rows()
        self._render_status()
        self._render_selection()

    def _render_rows(self):
        selected = self.selected_index()
        scroll = self.list.verticalScrollBar().value()
        self.list.blockSignals(True)
        self.list.clear()
        self.row_actions = {}
        self.row_confirm = {}
        rows = self.visible_rows()
        for row in rows:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, row.ap_index)
            item.setSizeHint(QSize(0, 40))
            item.setToolTip(text("launcher.items", "received_index", index=str(row.ap_index)))
            if row.received_at is not None:
                received = QDateTime.fromMSecsSinceEpoch(row.received_at // 1000).toLocalTime()
                item.setToolTip(text("launcher.items", "received_time", index=str(row.ap_index),
                                     time=self.locale().toString(received, QLocale.FormatType.ShortFormat)))
            self.list.addItem(item)
            widget = self._row(row)
            widget.setToolTip(item.toolTip())
            self.list.setItemWidget(item, widget)
            if row.ap_index == selected:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        self.list.verticalScrollBar().setValue(scroll)
        self.list.setVisible(bool(rows))
        if rows and self.presenter.model.more and not self.presenter.model.loading:
            QTimer.singleShot(0, self._load_visible)

    def visible_rows(self):
        return [row for row in self.presenter.model.rows
                if not self.presenter.blocked_only or row.status is ItemStatus.BLOCKED]

    def _render_status(self):
        model = self.presenter.model
        self.status.setVisible(not self.visible_rows())
        key = "empty_connected" if model.online else "empty_disconnected"
        if model.availability not in {LedgerAvailability.AVAILABLE, LedgerAvailability.INACTIVE}:
            key = model.availability.value
        self.status.setText(text("launcher.items", key))
        count = model.attention_count
        self.blocked.setVisible(bool(count))
        self.blocked.setText(text("launcher.items", "all_items") if self.presenter.blocked_only
                             else text("launcher.items", "attention_filter", n=str(count)))
        if self.presenter.blocked_only and not count and not model.loading:
            self.blocked.setChecked(False)

    def _load_visible(self):
        if self.presenter.model.more and not self.presenter.model.loading and self.list.verticalScrollBar().maximum() == 0:
            self.request(more=True)

    def _row(self, row):
        widget = QWidget()
        widget.setObjectName("itemRow")
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        copy = QVBoxLayout()
        copy.setSpacing(0)
        name = QLabel(self.item_names.get(row.item, str(row.item)))
        name.setTextFormat(Qt.TextFormat.PlainText)
        flags = row.flags if row.flags is not None else self.item_classifications.get(row.item)
        color = ARCHIPELAGO_PRESENTATION_COLORS["text" if flags is None else item_classification_color_key(flags)]
        name.setStyleSheet(f"color:{color};")
        copy.addWidget(name)
        sender = QLabel(self._sender(row))
        sender.setTextFormat(Qt.TextFormat.PlainText)
        sender.setObjectName("muted")
        copy.addWidget(sender)
        layout.addLayout(copy, 1)
        attention = QHBoxLayout()
        attention.setSpacing(16)
        attention.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        status = self.presenter.status(row)
        if status:
            label = QLabel(status)
            label.setObjectName("itemStatus")
            label.setProperty("tone", self.presenter.status_tone(row))
            attention.addWidget(label)
        self._row_buttons(row, attention)
        layout.addLayout(attention)
        return widget

    def _sender(self, row):
        if row.sender == 0:
            return text("launcher.items", "starting_item")
        if self.presenter.model.room and row.sender == self.presenter.model.room.slot:
            return text("launcher.items", "from_you")
        return text("launcher.items", "from_player", player=self.player_names.get(row.sender, str(row.sender)))

    def _row_buttons(self, row, layout):
        pending = self.presenter.resolution
        confirming = pending is not None and pending.action in row.actions
        buttons = {}
        if confirming:
            confirm = self._link("confirm", self._confirm)
            cancel = self._link("cancel", self._cancel)
            self.row_confirm[row.ap_index] = (confirm, cancel)
            layout.addWidget(confirm)
            layout.addWidget(cancel)
        else:
            for action in self.presenter.actions(row.ap_index):
                button = self._link(action.kind.value, lambda checked=False, kind=action.kind, index=row.ap_index: self._choose(kind, index))
                buttons[action.kind] = button
                layout.addWidget(button)
        self.row_actions[row.ap_index] = buttons

    def _link(self, key, callback):
        button = QPushButton(text("launcher.items", key))
        button.setObjectName("link")
        button.setProperty("compact", True)
        button.setEnabled(not self.presenter.submitted)
        button.setToolTip(text('common', 'waiting_for_game') if self.presenter.submitted else "")
        button.clicked.connect(callback)
        return button

    def _render_selection(self):
        self._render_local()
        pending = self.presenter.resolution
        local = pending is not None and pending.action in self.presenter.local_actions
        self.confirm.setVisible(local)
        self.cancel.setVisible(local)
        self.confirm.setEnabled(not self.presenter.submitted)
        self.cancel.setEnabled(not self.presenter.submitted)
        self.details.setVisible(local or bool(self.presenter.model.message))
        self.details.setText(self.presenter.model.message)
        if local:
            key = ("confirm_recover_ammo_operation" if pending.action.command_id.startswith("ammo-operation:")
                   else "confirm_" + pending.action.kind.value)
            self.details.setText(text("launcher.items", "retry_warning")
                                 if key == "confirm_try_again" else text("launcher.items", key))

    def _render_local(self):
        item_id = self.presenter.local_item_id
        self.local_label.setText(self.item_names.get(item_id, text("launcher.items", "blocked")))
        self.local_label.setVisible(item_id is not None)
        for kind, button in self.local_buttons.items():
            recovery = any(a.kind is kind and a.command_id.startswith("ammo-operation:") for a in self.presenter.local_actions)
            button.setText(text('launcher.items', 'recover_ammo_operation') if recovery else
                           text("launcher.items", kind.value))
            button.setVisible(any(a.kind is kind for a in self.presenter.local_actions) and self.presenter.model.can_resolve)
            button.setEnabled(not self.presenter.submitted)

    def _choose_local(self, kind):
        action = next((a for a in self.presenter.local_actions if a.kind is kind), None)
        if action is not None:
            self.presenter.select_action(action)
        self._confirm_selection()

    def _choose(self, kind, index):
        action = next((action for action in self.presenter.actions(index) if action.kind is kind), None)
        if action is not None:
            self.presenter.select_action(action)
        self._confirm_selection()

    def _confirm_selection(self):
        request = self.presenter.resolution
        if request is not None and request.action.kind is ResolutionAction.MARK_AS_RECEIVED and not self.presenter.submitted:
            answer = QMessageBox.question(self, text("launcher.items", "mark_as_received"),
                                          text("launcher.items", "confirm_mark_as_received"),
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No)
            if self.presenter.resolution is request:
                if answer == QMessageBox.StandardButton.Yes:
                    self._confirm()
                else:
                    self._cancel()
        self.render()

    def _confirm(self):
        request = self.presenter.confirm()
        if request is not None:
            self.resolution_requested.emit(request)
        self.render()

    def _cancel(self):
        if not self.presenter.submitted:
            self.presenter.resolution = None
        self.render()
