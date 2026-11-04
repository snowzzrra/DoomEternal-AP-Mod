"""Render Items and emit typed intentions; the presenter guards selection and confirmation."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from doom_eap.runtime.ledger_records import ResolutionAction

from ..items import LedgerAvailability
from .strings import text


class ItemsView(QWidget):
    query_requested = Signal(object)
    resolution_requested = Signal(object)

    def __init__(self, presenter):
        super().__init__()
        self.presenter = presenter
        self.item_names = {}
        self.player_names = {}
        self._rendering = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        filters = QHBoxLayout()
        self.blocked = QCheckBox(text("launcher.items", "blocked_only"))
        self.blocked.toggled.connect(self._filter_changed)
        filters.addWidget(self.blocked)
        filters.addStretch()
        self.refresh = QPushButton(text("launcher.items", "refresh"))
        self.refresh.clicked.connect(lambda: self.request(refresh=True))
        filters.addWidget(self.refresh)
        layout.addLayout(filters)
        self.table = QTableWidget(0, 4)
        self.table.setAccessibleName(text("launcher.navigation", "items"))
        self.table.setHorizontalHeaderLabels(
            [text("launcher.items", key) for key in ("index", "item", "sender", "status")]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)
        self.details = QLabel()
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setWordWrap(True)
        layout.addWidget(self.details)
        self._build_actions(layout)
        self.more = QPushButton(text("launcher.items", "more"))
        self.more.clicked.connect(lambda: self.request(more=True))
        layout.addWidget(self.more)
        self.render()

    def _build_actions(self, layout):
        local = QHBoxLayout()
        self.local_label = QLabel()
        self.local_label.setTextFormat(Qt.TextFormat.PlainText)
        local.addWidget(self.local_label)
        self.local_buttons = {}
        for kind in ResolutionAction:
            button = QPushButton(text("launcher.items", kind.value))
            button.clicked.connect(lambda checked=False, action=kind: self._choose_local(action))
            self.local_buttons[kind] = button
            local.addWidget(button)
        layout.addLayout(local)
        actions = QHBoxLayout()
        self.action_buttons = {}
        for kind in ResolutionAction:
            button = QPushButton(text("launcher.items", kind.value))
            button.clicked.connect(lambda checked=False, action=kind: self._choose(action))
            self.action_buttons[kind] = button
            actions.addWidget(button)
        self.confirm = QPushButton(text("launcher.items", "confirm"))
        self.confirm.clicked.connect(self._confirm)
        self.cancel = QPushButton(text("launcher.items", "cancel"))
        self.cancel.clicked.connect(self._cancel)
        actions.addWidget(self.confirm)
        actions.addWidget(self.cancel)
        layout.addLayout(actions)

    def request(self, *, more=False, refresh=False):
        query = self.presenter.query(more=more, refresh=refresh)
        if query is not None:
            self.query_requested.emit(query)
        self.render()

    def _filter_changed(self, checked):
        query = self.presenter.filter_blocked(checked)
        if query is not None:
            self.query_requested.emit(query)
        self.render()

    def selected_index(self):
        cell = self.table.item(self.table.currentRow(), 0)
        return cell.data(Qt.ItemDataRole.UserRole) if cell else None

    def render(self):
        selected = self.selected_index()
        self._rendering = True
        model = self.presenter.model
        rows = [row for row in model.rows if not self.blocked.isChecked() or row.status.value == "blocked"]
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = (
                str(row.ap_index),
                self.item_names.get(row.item, str(row.item)),
                self.player_names.get(row.sender, str(row.sender)),
                self.presenter.status(row),
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setData(Qt.ItemDataRole.UserRole, row.ap_index)
                self.table.setItem(index, column, cell)
            if row.ap_index == selected:
                self.table.selectRow(index)
        self._rendering = False
        self._render_status()
        self.more.setVisible(model.more)
        self.more.setEnabled(not model.loading)
        self.refresh.setEnabled(not model.loading)
        self._render_selection()

    def _render_status(self):
        model = self.presenter.model
        key = model.availability.value
        if model.availability is LedgerAvailability.AVAILABLE:
            key = "online" if model.can_resolve else "readonly" if model.online else "offline"
        message = model.message or text("launcher.items", key)
        if model.room:
            message = (
                text("launcher.items", "room", seed=model.room.seed, team=model.room.team, slot=model.room.slot)
                + "\n"
                + message
            )
        self.status.setText(message)

    def _selection_changed(self):
        if not self._rendering:
            self._cancel()

    def _render_selection(self):
        self._render_local()
        selected = self.selected_index()
        row = next((row for row in self.presenter.model.rows if row.ap_index == selected), None)
        self.details.setText(self.presenter.details(row) if row else "")
        allowed = self.presenter.actions(selected) if row else ()
        for kind, button in self.action_buttons.items():
            button.setVisible(any(action.kind is kind for action in allowed))
            button.setEnabled(not self.presenter.submitted)
        pending = self.presenter.resolution
        self.confirm.setVisible(pending is not None)
        self.cancel.setVisible(pending is not None)
        self.confirm.setEnabled(not self.presenter.submitted)
        self.cancel.setEnabled(not self.presenter.submitted)
        if pending:
            key = ("confirm_recover_ammo_operation" if pending.action.command_id.startswith("ammo-operation:")
                   else "confirm_" + pending.action.kind.value)
            self.details.setText(text("launcher.items", key))

    def _render_local(self):
        item_id = self.presenter.local_item_id
        self.local_label.setText(self.item_names.get(item_id, text("launcher.items", "blocked")))
        self.local_label.setVisible(item_id is not None)
        for kind, button in self.local_buttons.items():
            recovery = any(a.kind is kind and a.command_id.startswith("ammo-operation:") for a in self.presenter.local_actions)
            button.setText(text("launcher.items", "recover_ammo_operation" if recovery else kind.value))
            button.setVisible(any(a.kind is kind for a in self.presenter.local_actions) and self.presenter.model.can_resolve)
            button.setEnabled(not self.presenter.submitted)

    def _choose_local(self, kind):
        action = next((a for a in self.presenter.local_actions if a.kind is kind), None)
        if action is not None:
            self.presenter.select_action(action)
        self._render_selection()

    def _choose(self, kind):
        action = next((action for action in self.presenter.actions(self.selected_index()) if action.kind is kind), None)
        if action is not None:
            self.presenter.select_action(action)
        self._render_selection()

    def _confirm(self):
        request = self.presenter.confirm()
        if request is not None:
            self.resolution_requested.emit(request)
        self._render_selection()

    def _cancel(self):
        if not self.presenter.submitted:
            self.presenter.resolution = None
        self._render_selection()
