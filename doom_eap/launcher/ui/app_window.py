from __future__ import annotations

import html
import json
import os
import queue
import re
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from PySide6.QtCore import (
    QEasingCurve,
    QLocale,
    QPropertyAnimation,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from doom_eap.content.item_classification import load_item_classifications
from doom_eap.launcher.installation_adapters import LinuxModManagerAdapter
from doom_eap.launcher.launcher_controller import LauncherController
from doom_eap.launcher.launcher_platform import (
    WINDOWS_MANUAL_INSTALL_GUIDE_URL,
    doom_saved_games_base,
    is_saved_games_base_shape,
    redact_secrets,
)
from doom_eap.presentation import ARCHIPELAGO_PRESENTATION_COLORS
from doom_eap.runtime.ledger import RoomKey
from doom_eap.strings import PlayerError

from ..connection_errors import enrich_connection_failure
from ..events import (
    AccountingCapabilityChanged,
    ActionId,
    DeliveryProgress,
    ItemResolutionResult,
    LocalDeliveryStatus,
    ResyncProgress,
    adapt_event,
)
from ..items import ItemsSnapshot, LedgerAvailability
from ..launcher_integration import RoomSetupCoordinator
from ..presenters.diagnostics_presenter import DiagnosticsPresenter
from ..presenters.items_presenter import ItemsPresenter
from ..presenters.play_presenter import PlayPresenter
from ..setup_progress import STEP_TEXT, SetupAction
from .controls import (
    AmmoRefillKeyControl,
    ElidedLabel,
    ElidedPath,
    ToggleSwitch,
    _AmmoRefillPips,
    _card,
    _label,
    _ScanlineButton,
    _scroll,
)
from .doom_style import DoomBackground, animations_enabled, fade_page
from .doom_style import DoomButton as QPushButton
from .help_view import HelpView
from .items_view import ItemsView
from .play_view import PlayView
from .player_file_view import PlayerFileView
from .report_view import ReportDialog
from .settings_view import LanguageButton, SettingsView
from .strings import configure_language, failure_text, installer_instruction, record_failure
from .strings import text as player_text


class LauncherUI(QMainWindow):
    """Controller-backed, keyboard-first launcher shell."""

    COLORS: ClassVar = dict(re.findall(
        '@([a-z0-9_]+)@ = (#[0-9a-f]{6})', Path(__file__).with_name('theme.qss').read_text(encoding='utf-8'),
    ))

    _label = staticmethod(_label)
    _card = staticmethod(_card)
    _scroll = staticmethod(_scroll)

    def __init__(self, controller: LauncherController):
        super().__init__()
        self.controller = controller
        language = configure_language(controller.config.get("launcher_language"), QLocale.system().name())
        self.setLocale(QLocale(language))
        self.setWindowTitle(player_text("launcher.navigation", "brand"))
        self.resize(1280, 720)
        self.setMinimumSize(760, 600)
        self.room_event: dict[str, object] = {}
        self._room_connected = False
        self._reconnecting = False
        self._close_requested = False
        self._manual_retry = False
        self._last_integration_status = None
        self._connection_pending = False
        self._shown_connection_error_attempts: set[int] = set()
        self._event_poll_scheduled = False
        self._setup_state = "disconnected"
        self._previous_setup_state = ""
        self._play_presenter = PlayPresenter(
            windows=os.name == 'nt', installer_wait_seconds=LinuxModManagerAdapter.TIMEOUT_SECONDS,
        )
        self._setup_progress = self._play_presenter.progress
        self._resolved_consent_requests: set[str] = set()
        self._dependency_request = None
        self._repair_preview_request = None
        self._manual_installation_generation = None
        self._native_health_presentation: tuple[str, str] | None = None
        self._session_log_limit = 400
        self._chat_pending_text: str | None = None
        self._hints_state = "disconnected"
        self._ammo_refills_available: int | None = None
        self._ammo_refill_debounce = False
        self.items_presenter = ItemsPresenter()
        self._configure_style()
        self._build()
        self._set_chat_enabled(False)
        self._set_connection_badge(player_text("launcher.ui", "offline"), False)
        self._set_hints_state("disconnected")
        self._set_setup_state("disconnected")
        self._load_icon()
        self._install_shortcuts()
        self._discover()
        self._restore_items_view()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_events)
        self.timer.start(75)
        self.lifecycle_timer = QTimer(self)
        self.lifecycle_timer.timeout.connect(self._poll_slow_state)
        self.lifecycle_timer.start(1000)

    def _configure_style(self) -> None:
        style = Path(__file__).with_name("theme.qss").read_text(encoding="utf-8")
        for key, value in self.COLORS.items():
            style = style.replace("@" + key + "@", value)
        self.setStyleSheet(style)

    def _build(self) -> None:
        root = DoomBackground()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        topbar = self._card("topbar")
        topbar.setFixedHeight(56)
        top = QHBoxLayout(topbar)
        top.setContentsMargins(24, 0, 24, 0)
        top.setSpacing(12)
        icon = QLabel()
        icon.setPixmap(QIcon(str(self.controller.bundle_dir / "assets/launcher/EternalAP.ico")).pixmap(28, 28))
        top.addWidget(icon)
        brand = QVBoxLayout()
        brand.setSpacing(0)
        brand.addWidget(self._label(player_text("launcher.navigation", "brand_top"), "brandTop"))
        brand.addWidget(self._label(player_text("launcher.navigation", "brand_bottom"), "brand"))
        top.addLayout(brand)
        top.addStretch()
        self.nav_buttons = []
        for key, target in (("play", 0), ("create_player", 3), ("help", 4), ("settings", 5)):
            button = QPushButton(player_text("common", "create_player") if key == "create_player" else player_text("launcher.navigation", key))
            button.setObjectName("nav")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, page=target: self._show_page(page))
            top.addWidget(button)
            self.nav_buttons.append(button)
        self.language_button = LanguageButton(self.controller, self)
        top.addWidget(self.language_button)
        layout.addWidget(topbar)
        self.pages = QStackedWidget()
        connection = self._join_card()
        session = self._session_hero()
        self.hero_title = self.session_setup_title
        self.hero_detail = self.session_setup_detail
        self.hero_action = self.session_setup_action
        self._action_id = ActionId.CONNECT
        self.items_view = ItemsView(self.items_presenter)
        classification_path = self.controller.bundle_dir / "data/item_classifications.json"
        if classification_path.is_file():
            self.items_view.item_classifications = load_item_classifications(classification_path)
        self.items_view.query_requested.connect(self._request_items)
        self.items_view.resolution_requested.connect(self._resolve_item)
        self._capability_request = None
        self._items_generation = self.controller.operation_generation
        self.play_view = PlayView(
            connection, session, self.session_setup, self.items_view, self._activity_card(), self._hints_card(),
            self._chat_bar(), self.session_setup_action,
        )
        self.session_stack = self.play_view.tabs
        self.session_stack.setCornerWidget(self.player_ammo_refills, Qt.Corner.TopRightCorner)
        self.pages.addWidget(self.play_view)
        self.player_view = PlayerFileView(self.controller, self.COLORS, self)
        self.player_view.activity.connect(self._activity_event)
        self.player_view.diagnostic.connect(self._append_log)
        self.pages.addWidget(self.player_view)
        self.help_view = HelpView(
            self.controller,
            {
                ActionId.DIAGNOSE: self._run_doctor, ActionId.FIX_SETUP: self._preview_repairs,
                ActionId.SAVE_REPORT: self._save_support_bundle, ActionId.OPEN_ISSUE: self._open_github_issue,
                ActionId.SELECT_PROTON: self._select_proton, ActionId.UNINSTALL: self._uninstall_room_package,
                ActionId.LOCAL_BUILD: self._select_local_core, ActionId.AUTOMATIC_CORE: self._clear_local_core,
                ActionId.REPAIR_CORE: lambda: self._runtime_repair('repair_core'),
                ActionId.REPAIR_SESSION: lambda: self._runtime_repair('repair_session'),
                ActionId.ROLLBACK_CORE: lambda: self._runtime_repair('rollback_core'),
                ActionId.SHOW_PLAY: lambda: self._show_page(0), ActionId.CHOOSE_GAME_FOLDER: self._open_game_paths,
                ActionId.REPAIR_INVENTORY: self._request_inventory_resync,
                ActionId.REINSTALL: lambda: self._prepare(force=True),
            },
            self.ap_save_button,
            self._session_log_page(),
            self._session_card(),
            self.player_inventory,
            self.resync_inventory_button,
            self,
        )
        self.pages.addWidget(self.help_view)
        self.help_view.repair_confirmed.connect(self._apply_repair_preview)
        self.help_view.download_answered.connect(self._answer_dependency_consent)
        self.play_view.set_launch_option(
            self.launch_option_label, self.launch_option, self.copy_launch_option_button, enabled=os.name != 'nt',
        )
        self.settings_view = SettingsView(
            self.ammo_refill_keybind, self.special_toggle_keybind, self.killsanity_counter_visible, self.grace_control, self.language_button
        )
        self.pages.addWidget(self.settings_view)
        self.pages.currentChanged.connect(self._sync_navigation)
        layout.addWidget(self.pages, 1)
        self._show_page(0)

    def _restore_items_view(self):
        room = self.controller.items_room
        if room is not None:
            self.items_presenter.activate(room, online=False)
            for entry in self.controller.config.get("recent_rooms", []):
                if entry.get("room") == vars(room):
                    self._set_item_names(entry)
            self.items_view.request()

    def _set_item_names(self, event):
        self.items_view.item_names = {int(key): name for key, name in event.get("item_names", {}).items()}
        self.items_view.player_names = {int(key): name for key, name in event.get("player_names", {}).items()}

    def _join_card(self) -> QFrame:
        card = self._card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        outer = layout
        self.room_section = QWidget()
        self.room_section.setObjectName("roomSection")
        layout = QVBoxLayout(self.room_section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        outer.addWidget(self.room_section)
        layout.addWidget(self._label(player_text("launcher.connection", "title"), "eyebrow"))
        self.room_fields = QWidget()
        self.room_fields.setObjectName("roomFields")
        fields = QVBoxLayout(self.room_fields)
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(12)
        layout.addWidget(self.room_fields)
        self.recent_rooms = QComboBox()
        self.recent_rooms.setAccessibleName(player_text("launcher.connection", "recent"))
        self.recent_rooms.addItem(player_text("launcher.connection", "recent"), None)
        for entry in self.controller.config.get("recent_rooms", []):
            self.recent_rooms.addItem(entry["server"] + " · " + entry["player"], entry)
        self.recent_rooms.activated.connect(self._select_recent_room)
        fields.addWidget(self.recent_rooms)
        self.server = QLineEdit(str(self.controller.config.get("server_address", "")))
        self.slot = QLineEdit(str(self.controller.config.get("slot", "")))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText(player_text("launcher.connection", "optional"))
        self.server.setPlaceholderText('archipelago.gg:38281')
        self.slot.setPlaceholderText(player_text("launcher.connection", "player_example"))
        self._create_settings_controls()
        for key, field in (("server", self.server), ("player", self.slot), ("password", self.password)):
            row = QVBoxLayout()
            row.setSpacing(4)
            label = self._label(player_text("launcher.connection", key), "fieldLabel")
            label.setBuddy(field)
            row.addWidget(label)
            row.addWidget(field)
            if key == "player":
                row.addWidget(self._label(player_text("launcher.connection", "player_help"), "muted"))
            fields.addLayout(row)
        self.room_identity = QWidget()
        self.room_identity.setObjectName("roomIdentity")
        identity = QVBoxLayout(self.room_identity)
        identity.setContentsMargins(0, 0, 0, 0)
        identity.setSpacing(4)
        self.connected_server = self._label("")
        self.connected_player = self._label("")
        for key, value in (("server", self.connected_server), ("player", self.connected_player)):
            if key == "player":
                identity.addSpacing(8)
            identity.addWidget(self._label(player_text("launcher.connection", key), "fieldLabel"))
            identity.addWidget(value)
        self.room_identity.hide()
        layout.addWidget(self.room_identity)
        self.join_button = QPushButton(player_text("launcher.ui", "connect"))
        self.join_button.setObjectName("primary")
        self.join_button.clicked.connect(self._room_action)
        layout.addWidget(self.join_button)
        self.join_error = self._label("", "warning")
        self.join_error.hide()
        layout.addWidget(self.join_error)
        layout = outer
        self.game_summary = QWidget()
        self.game_summary.setObjectName("gameSummary")
        layout.addWidget(self.game_summary)
        layout = QVBoxLayout(self.game_summary)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        separator = QFrame()
        separator.setObjectName("separator")
        layout.addWidget(separator)
        layout.addWidget(self._label(player_text("launcher.play", "game"), "eyebrow"))
        game = QVBoxLayout()
        game.setSpacing(4)
        title = QHBoxLayout()
        title.setSpacing(12)
        self.detected_paths = self._label("", "gameTitle")
        title.addWidget(self.detected_paths, 1)
        self.change_paths = QPushButton(player_text("launcher.play", "change"))
        self.change_paths.setObjectName("link")
        self.change_paths.setProperty("compact", True)
        self.change_paths.setProperty("gameChange", True)
        self.change_paths.clicked.connect(self._toggle_paths)
        title.addWidget(self.change_paths)
        game.addLayout(title)
        self.game_path_summary = ElidedLabel("", "muted")
        self.game_path_summary.setWordWrap(False)
        game.addWidget(self.game_path_summary)
        layout.addLayout(game)
        layout = outer
        self.game_paths = QWidget()
        self.game_paths.setObjectName("gamePaths")
        paths = QVBoxLayout(self.game_paths)
        paths.setContentsMargins(0, 0, 0, 0)
        paths.setSpacing(12)
        paths.addWidget(self._label(player_text("launcher.play", "game_folders"), "eyebrow"))
        self.game_root = ElidedPath(str(self.controller.config.get("game_root", "")))
        self.saves_root = ElidedPath(str(self.controller.config.get("save_games_dir", "")))
        self.game_path_label, self.game_browse = self._folder_control(paths, "game_folder", self.game_root, self._browse_game)
        self.save_path_label, self.save_browse = self._folder_control(paths, "save_folder", self.saves_root, self._browse_saves)
        self.proton_executable = None
        if sys.platform.startswith("linux"):
            self.proton_executable = ElidedPath(str(self.controller.config.get("proton_executable") or ""))
            self._folder_control(paths, "proton_program", self.proton_executable, self._browse_proton)
        done = QPushButton(player_text("launcher.play", "done"))
        done.setObjectName("primary")
        done.clicked.connect(lambda: self._toggle_paths(force=False))
        paths.addWidget(done)
        layout.insertWidget(0, self.game_paths)
        layout.addStretch()
        self.game_root.textChanged.connect(self._render_game_block)
        self.saves_root.textChanged.connect(self._render_game_block)
        self._toggle_paths(force=not bool(self.game_root.text() and self.saves_root.text()))
        self._render_game_block()
        return card

    def _folder_control(self, layout, key, field, callback):
        group = QVBoxLayout()
        group.setSpacing(4)
        layout.addLayout(group)
        label = self._label(player_text("launcher.ui", key), "fieldLabel")
        label.setBuddy(field)
        group.addWidget(label)
        group.addWidget(field)
        browse = QPushButton(player_text("launcher.ui", "browse"))
        browse.clicked.connect(callback)
        group.addWidget(browse, alignment=Qt.AlignmentFlag.AlignRight)
        return label, browse

    def _room_action(self):
        action = ActionId.DISCONNECT if self._room_connected else ActionId.CONNECT
        if action is ActionId.CONNECT:
            self._connect()
        else:
            self._disconnect()

    def _render_game_block(self):
        found = bool(self.game_root.text() and self.saves_root.text())
        self._play_presenter.observe(adapt_event({"type": "discovery_changed", "game_root": self.game_root.text() if found else None}))
        if hasattr(self, "play_view"):
            self.play_view.render(self._play_presenter.present())
        self.detected_paths.setText(player_text('common', 'game_title') if found else player_text('launcher.play', 'game_missing'))
        self.detected_paths.setProperty("tone", "text" if found else "warn")
        self.detected_paths.style().unpolish(self.detected_paths)
        self.detected_paths.style().polish(self.detected_paths)
        path = self.game_root.text()
        self.game_path_summary.setToolTip(path)
        self.game_path_summary.setText(path)
        if not found:
            self._toggle_paths(force=True)

    def _select_recent_room(self, index):
        entry = self.recent_rooms.itemData(index)
        if entry is None:
            return
        self.server.setText(entry["server"])
        self.slot.setText(entry["player"])
        self.password.clear()
        self.password.setFocus(Qt.FocusReason.OtherFocusReason)
        self.items_presenter.activate(RoomKey(**entry["room"]), online=False)
        self._set_item_names(entry)
        self.items_view.request(refresh=True)

    def _session_card(self) -> QFrame:
        card = self._card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        layout.addWidget(self._label(player_text('common', 'room_mod'), "section"))
        self.room_summary = self._label(
            player_text('launcher.ui', 'no_room_connected_join_a_room_to_start_playing'), 'muted', rich=True,
        )
        self.room_summary.setWordWrap(True)
        layout.addWidget(self.room_summary)
        self.room_options = self._label(player_text("launcher.ui", "room_settings_appear_after_connection"), "muted")
        self.room_options.setWordWrap(True)
        view_options = QPushButton(player_text("launcher.ui", "view_all_settings"))
        view_options.clicked.connect(self._view_room_options)
        self.room_options.hide()
        view_options.hide()
        self.drift = self._label("")
        self.drift.setObjectName("warning")
        self.drift.hide()
        layout.addWidget(self.drift)
        self.launch_option_label = self._label(player_text("launcher.ui", "steam_launch_option"), "eyebrow")
        self.launch_option = QLineEdit(player_text("launcher.app_window", "available_after_setup"))
        self.launch_option.setReadOnly(True)
        self.launch_option.hide()
        self.copy_launch_option_button = QPushButton(player_text("launcher.ui", "copy"))
        self.copy_launch_option_button.clicked.connect(self._copy_launch_option)
        if os.name != "nt":
            layout.addWidget(self.launch_option_label)
            layout.addWidget(self.launch_option)
            self.launch_option.show()
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
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        identity = QHBoxLayout()
        identity.setSpacing(12)
        copy = QVBoxLayout()
        copy.setSpacing(4)
        copy.addWidget(self._label(player_text("launcher.ui", "current_session"), "eyebrow"))
        self.session_player_name = self._label(player_text("launcher.ui", "no_room_connected"), "sessionPlayerName")
        self.session_player_name.setWordWrap(True)
        self.session_player_name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        copy.addWidget(self.session_player_name)
        identity_copy = QWidget()
        identity_copy.setLayout(copy)
        identity_copy.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        identity.addWidget(identity_copy, 1, Qt.AlignmentFlag.AlignVCenter)
        identity.addStretch(1)
        identity.addWidget(self._ammo_refill_block(), 0, Qt.AlignmentFlag.AlignVCenter)

        self.connection_badge = self._label(player_text("launcher.ui", "offline"), "connectionBadge")
        self.connection_badge.setProperty("connected", False)
        identity.addWidget(self.connection_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(identity)

        self.player_team = self._label(player_text("launcher.ui", "team"), "muted")
        self.player_slot = self._label(player_text("launcher.ui", "player_number"), "muted")
        self.player_inventory = self._label(player_text("launcher.ui", "connect_to_load_your_received_items"), "muted")
        self.player_inventory.setWordWrap(True)
        self.resync_inventory_button = QPushButton(player_text('common', 'repair_inventory'))
        self.resync_inventory_button.setEnabled(False)
        self.resync_inventory_button.clicked.connect(self._request_inventory_resync)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(self.resync_inventory_button)
        self.session_uninstall_button = QPushButton(player_text("launcher.ui", "uninstall_mod"))
        self.session_uninstall_button.clicked.connect(self._uninstall_room_package)
        self.session_uninstall_button.setEnabled(False)
        actions.addWidget(self.session_uninstall_button)
        actions.addStretch(1)
        self.stop_button = QPushButton(player_text("launcher.ui", "disconnect"))
        self.stop_button.setObjectName("danger")
        self.stop_button.clicked.connect(self._disconnect)
        self.stop_button.setEnabled(False)
        self.stop_button.hide()
        self.session_uninstall_button.hide()
        actions.addWidget(self.stop_button)
        layout.addLayout(actions)
        layout.addWidget(self.player_inventory)
        backup_actions = QHBoxLayout()
        backup_actions.setSpacing(12)
        backup_button = QPushButton(player_text("launcher.ui", "ap_save"))
        backup_button.setToolTip(player_text('launcher.ui', 'save_actions_help'))
        backup_button.clicked.connect(self._ap_save_actions)
        self.ap_save_button = backup_button
        backup_actions.addWidget(backup_button)
        backup_actions.addStretch()
        layout.addLayout(backup_actions)
        self._build_setup_strip()
        return card


    def _install_shortcuts(self) -> None:
        for key, page in (("Alt+P", 0), ("Alt+J", 1), ("Alt+C", 3), ("Alt+H", 4), ("Alt+S", 5)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(lambda target=page: self._show_page(target))
        primary = QShortcut(QKeySequence("Ctrl+Return"), self)
        primary.activated.connect(self._primary_action)

    def _select_local_core(self):
        path, _ = QFileDialog.getOpenFileName(
            self, player_text('launcher.ui', 'select_local_game_connection_files'), '',
            'Game connection settings (distribution.json)',
        )
        if path:
            self.controller.save_config({"core_runtime_manifest": path})
            self.help_view.core_status.setText(
                player_text("launcher.ui", "using_local_game_connection_files_d66635") + path
            )

    def _clear_local_core(self):
        self.controller.save_config({"core_runtime_manifest": None})
        self.help_view.core_status.setText(player_text("launcher.ui", "game_connection_files_download_during_setup"))
        self.help_view.core_status.show()

    def _runtime_repair(self, action):
        if action == "repair_session":
            self.help_view.doctor_action.setText(player_text("launcher.ui", "session_repair_help"))
        if action == "rollback_core":
            message = player_text("launcher.ui", "rollback_connection_help")
            if (
                QMessageBox.question(
                    self, player_text('launcher.ui', 'confirm_recovery'), message,
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
        self.controller.request_repair(action)

    def _save_death_link_grace(self, deaths: int) -> None:
        try:
            self.controller.set_death_link_grace(deaths)
            progress = dict(self._deathlink_progress, interval=max(1, deaths), remaining=max(1, deaths))
            self._on_deathlink_progress(progress)
        except (OSError, ValueError) as error:
            self._append_log('DeathLink Grace setting failed: {value_0}'.format(value_0=f'{error}'))
            self.death_link_grace.blockSignals(True)
            self.death_link_grace.setValue(int(self.controller.config.get("death_link_grace", 0)))
            self.death_link_grace.blockSignals(False)

    def _save_ammo_refill_keybind(self, captured: str = "") -> None:
        try:
            if self._save_keybinds():
                self._set_ammo_refill_indicator(self._ammo_refills_available)
        except (ValueError, OSError) as error:
            self._append_log(f'Ammo Refill keybind rejected: {error!s}')

    def _save_special_toggle_keybind(self, captured: str) -> None:
        try:
            self._save_keybinds()
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, player_text("launcher.ui", "keyboard_shortcut"), self._operation_failure('repair', error))

    def _save_keybinds(self):
        if self.settings_view.render_conflict():
            return False
        try:
            self.controller.set_ap_keybinds(self.ammo_refill_keybind.value(), self.special_toggle_keybind.value())
        except (ValueError, OSError):
            self.ammo_refill_keybind.set_value(str(self.controller.config.get("ammo_refill_keybind", "F9")))
            self.special_toggle_keybind.set_value(str(self.controller.config.get("special_toggle_keybind", "F10")))
            self.settings_view.render_conflict()
            raise
        return True

    def _save_killsanity_counter_visible(self, visible: bool) -> None:
        try:
            self.controller.set_killsanity_counter_visible(visible)
        except (ValueError, OSError) as error:
            self.killsanity_counter_visible.blockSignals(True)
            self.killsanity_counter_visible.setChecked(
            self.controller.config.get("killsanity_counter_visible", True) is True
        )
            self.killsanity_counter_visible.blockSignals(False)
            self._append_log('Killsanity counter preference could not be saved: {value_0}'.format(value_0=f'{error}'))

    def _set_ammo_refill_indicator(self, value: object) -> None:
        """Render Ammo Refill availability in three fixed indicator slots."""
        available = max(0, min(3, value)) if isinstance(value, int) and not isinstance(value, bool) else None
        self._ammo_refills_available = available
        keybind = self.ammo_refill_keybind.value() or player_text("launcher.app_window", "not_set")
        state = (
            "unavailable"
            if available is None
            else player_text("launcher.app_window", "value_0_of_3_available", value_0=f"{available}")
        )
        description = player_text(
            'launcher.ui', 'ammo_refills_value_0_key_value_1', value_0=f'{state}', value_1=f'{keybind}',
        )
        self.player_ammo_refills.setToolTip(description)
        self.player_ammo_refills.setVisible(self._room_connected and available is not None)
        self.player_ammo_refills.setAccessibleName(player_text("launcher.ui", "ammo_refill_availability"))
        self.player_ammo_refills.setAccessibleDescription(description)
        self.ammo_refill_key.setText(keybind)
        self.ammo_refill_key.setToolTip(player_text("launcher.ui", "ammo_refill_key", keybind=keybind))
        self.ammo_refill_pips.set_available(available)
        self.ammo_refill_pips.setToolTip(description)
        self._update_ammo_refill_button()

    def _update_ammo_refill_button(self) -> None:
        enabled = (
            self._room_connected
            and not self._connection_pending
            and not self._ammo_refill_debounce
            and isinstance(self._ammo_refills_available, int)
            and self._ammo_refills_available > 0
            and self._spending_available()
        )
        self.ammo_refill_button.setEnabled(enabled)
        self.ammo_refill_button.setVisible(self._spending_available())

    def _spending_available(self):
        if self.items_presenter.authority is None:
            model = self.items_presenter.model
            return model.room is not None and not model.loading and model.availability is LedgerAvailability.INACTIVE
        from doom_eap.runtime.ledger import Authority

        return self.items_presenter.authority is not Authority.LEDGER or self.items_presenter.model.can_spend

    def _request_ammo_refill(self) -> None:
        if not self.ammo_refill_button.isEnabled():
            return
        self._ammo_refill_debounce = True
        self._update_ammo_refill_button()
        self.controller.request_ammo_refill()
        QTimer.singleShot(650, self._end_ammo_refill_debounce)

    def _end_ammo_refill_debounce(self) -> None:
        self._ammo_refill_debounce = False
        self._update_ammo_refill_button()

    def _show_page(self, index: int) -> None:
        page = {0: 0, 1: 0, 2: 0, 3: 1, 4: 2, 5: 3}[index]
        previous = None
        if page != self.pages.currentIndex() and self.isVisible() and animations_enabled(self):
            previous = self.pages.currentWidget().grab()
        self.pages.setCurrentIndex(page)
        if previous is not None:
            self._fade_in(self.pages.currentWidget(), previous)
        if index == 1:
            self.play_view.focus_connection()
        if index == 4:
            self._refresh_help_room_package()
        self._sync_navigation(page)

    def _sync_navigation(self, page: int) -> None:
        for number, button in enumerate(self.nav_buttons):
            button.setChecked(number == page)

    def _open_game_paths(self):
        self._show_page(0)
        self._toggle_paths(force=True)

    def _open_github_issue(self):
        from ..launcher_reporting import github_issue_url

        url, _ = github_issue_url({"title": "", "description": "", "diagnostics": ""})
        webbrowser.open(url)

    def _refresh_help_room_package(self) -> None:
        if not hasattr(self, "help_view"):
            return
        failure = getattr(self.controller, "last_setup_failure", None) or {}
        self.help_view.doctor_proton_button.setVisible(failure.get("failure_domain") == "proton_runtime")
        title, tone = DiagnosticsPresenter.room_status(self._setup_state)
        self.help_view.doctor_room_status.setText(title)
        self.help_view.doctor_room_status.setStyleSheet(f"color:{self.COLORS[tone]};")
        button = self.help_view.symptom_buttons[ActionId.REINSTALL]
        button.setEnabled(self._room_connected)
        button.setToolTip("" if self._room_connected else player_text("launcher.play", "connect_guidance"))
        self.resync_inventory_button.setToolTip(self.player_inventory.text() if not self.resync_inventory_button.isEnabled() else "")
        self.help_view.uninstall_button.setToolTip("" if self._room_connected else player_text("launcher.play", "connect_guidance"))

    def _set_connection_badge(self, text: str, connected: bool) -> None:
        self.connection_badge.setText(text)
        self.connection_badge.setProperty("connected", connected)
        self.connection_badge.style().unpolish(self.connection_badge)
        self.connection_badge.style().polish(self.connection_badge)
        metrics = QFontMetrics(self.connection_badge.font())
        self.connection_badge.setFixedSize(metrics.horizontalAdvance(text) + 22, metrics.height() + 10)
        self.connection_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.connection_badge.setWordWrap(False)

    def _fade_in(self, widget: QWidget, previous=None) -> None:
        if not animations_enabled(widget):
            widget.setGraphicsEffect(None)
            return
        if previous is not None:
            fade_page(widget, previous)
            return
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", widget)
        animation.setDuration(140)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.finished.connect(lambda: widget.setGraphicsEffect(None))
        animation.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def _set_home(self, title: str, detail: str, action: str, state: str, *, enabled: bool = True) -> None:
        self.hero_title.setText(title)
        self.hero_detail.setText(detail)
        self.hero_action.setText(action)
        self.hero_action.setEnabled(enabled)
        self._action_id = self._setup_action_id()
        self.session_setup.setVisible(bool(detail))


    def _set_connection_controls(self, editable: bool) -> None:
        for field in (self.game_root, self.saves_root, self.server, self.slot, self.password, self.join_button):
            field.setEnabled(editable)
        self.stop_button.setEnabled(not editable and (self._room_connected or self._connection_pending))

    def _discover(self) -> None:
        found = self.controller.discover()
        self._play_presenter.observe(adapt_event({"type": "discovery_changed", **found}))
        if not self.game_root.text() and found.get("game_root"):
            self.game_root.setText(str(found["game_root"]))
        if not self.saves_root.text() and found.get("save_games_dir"):
            self.saves_root.setText(str(found["save_games_dir"]))
        self._toggle_paths(force=not bool(self.game_root.text() and self.saves_root.text()))
        self._render_game_block()
        self.play_view.render(self._play_presenter.present())
        if not self.player_view.start_inventory_catalog:
            self._append_log('Starting items could not load. Open Help and select Check Setup.')

    def _focus_join(self) -> None:
        self._show_page(1)
        self.server.setFocus(Qt.FocusReason.OtherFocusReason)

    def _resume(self) -> None:
        if self._room_connected:
            self._show_page(2)
            return
        endpoint, slot = (
            str(self.controller.config.get('server_address', '')), str(self.controller.config.get('slot', '')),
        )
        if not endpoint or not slot:
            self._focus_join()
            return
        password, accepted = QInputDialog.getText(
            self, player_text('launcher.ui', 'resume_session'),
            player_text('launcher.ui', 'room_password_for_value_0_if_required', value_0=f'{slot}'),
            QLineEdit.EchoMode.Password,
        )
        if not accepted:
            return
        self.server.setText(endpoint)
        self.slot.setText(slot)
        self.password.setText(password)
        self._connect()

    def _primary_action(self) -> None:
        action = self._action_id
        if action is ActionId.VIEW_SESSION:
            self._show_page(2)
        elif action is ActionId.RESUME:
            self._resume()
        elif action is ActionId.CONNECT:
            self._connect()
        else:
            self._run_setup_action()

    def _connect(self) -> None:
        try:
            if self.proton_executable is not None:
                self._persist_proton()
            self.controller.connect(
                endpoint=self.server.text(), slot=self.slot.text(), password=self.password.text(),
                game_root=self.game_root.text(), saves_root=self.saves_root.text(),
            )
        except (OSError, RuntimeError, ValueError) as error:
            raw = str(error)
            if "server address" in raw.casefold() or "player name" in raw.casefold():
                event = enrich_connection_failure(
                    {
                        'type': 'error',
                        'code': 'missing_slot' if 'player name' in raw.casefold() else 'malformed_address',
                        'technical_message': raw,
                    },
                    attempt_id=self.controller.connection_attempt_id,
                )
                self.controller.last_connection_error = dict(event)
                self._show_connection_failure(event)
            else:
                self._append_log(
                    'Local setup input error: {value_0}: {value_1}'.format(value_0=f'{type(error).__name__}', value_1=f'{raw}')
                )
                message = self._operation_failure('connect', error)
                self.join_error.setText(message)
                self.join_error.show()
                self._set_home(
                    player_text('launcher.ui', 'check_game_setup'), message, player_text('launcher.ui', 'join_room'),
                    player_text('launcher.ui', 'setup_required'),
                )
                QMessageBox.warning(self, player_text("launcher.ui", "game_setup_failed"), message)
            self._activity_event({"type": "connection_input_error", "message": raw})
            return
        self.join_error.hide()
        self._connection_pending = True
        self._play_presenter.observe(adapt_event({"type": "connecting", "endpoint": self.server.text(), "player": self.slot.text()}))
        self.play_view.render(self._play_presenter.present())
        self._set_connection_controls(False)
        self._set_connection_badge(player_text("launcher.ui", "connecting"), False)
        self._show_page(2)
        self._set_home(
            player_text('launcher.ui', 'connecting_to_room'),
            player_text('launcher.ui', 'waiting_for_the_server_to_send_your_room_settings'),
            player_text('launcher.ui', 'resume_session_a94678'), player_text('launcher.ui', 'connecting'),
            enabled=False,
        )

    def _toggle_paths(self, _checked: bool = False, *, force: bool | None = None) -> None:
        show = force if force is not None else self.game_paths.isHidden()
        self.game_paths.setVisible(show)
        self.room_section.setVisible(not show)
        self.game_summary.setVisible(not show)

    def _disconnect(self) -> None:
        try:
            self.controller.disconnect()
            self.stop_button.setEnabled(False)
        except (OSError, RuntimeError, ValueError) as error:
            self._append_log('Stop error: {value_0}'.format(value_0=f'{error}'))

    def _uninstall_room_package(self) -> None:
        if not self._room_connected:
            return
        generation = self.controller.operation_generation
        confirmation = QMessageBox.question(
            self, player_text('launcher.ui', 'uninstall_room_mod'), player_text('launcher.ui', 'confirm_uninstall'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return
        if self.controller.operation_generation != generation:
            return
        self.help_view.uninstall_button.setEnabled(False)
        self.help_view.doctor_room_status.setText(player_text("launcher.ui", "waiting_to_uninstall"))
        self.help_view.doctor_room_evidence.setText(
            player_text("launcher.ui", "waiting_for_the_current_operation_to_finish")
        )
        try:
            self.controller.uninstall_setup(generation=generation)
        except (OSError, RuntimeError, ValueError):
            self.help_view.uninstall_button.setEnabled(self._room_connected)
            self.help_view.doctor_room_status.setText(player_text("launcher.ui", "uninstall_failed"))
            self.help_view.doctor_room_evidence.setText(player_text('launcher.ui', 'close_game_retry'))
            self._append_log('Room package uninstall failed.')


    def _browse_game(self) -> None:
        value = QFileDialog.getExistingDirectory(self, player_text("launcher.ui", "select_doom_eternal_folder"))
        if value:
            self.game_root.setText(value)

    def _persist_proton(self) -> None:
        value = self.proton_executable.text().strip()
        if value:
            path = Path(value).expanduser().resolve()
            if path.name != "proton" or not path.is_file():
                raise PlayerError('launcher.app_window', 'proton_file_help')
            value = str(path)
        if self.controller.config.get("proton_executable") != (value or None):
            self.controller.save_config({"proton_executable": value or None})

    def _save_proton(self) -> None:
        try:
            self._persist_proton()
        except (OSError, ValueError, RuntimeError) as error:
            QMessageBox.warning(self, player_text("launcher.ui", "proton_program_004d43"), self._operation_failure('install_core', error))

    def _browse_proton(self) -> None:
        value, _ = QFileDialog.getOpenFileName(
            self, player_text('launcher.ui', 'select_doom_eternal_s_proton_program'), self.proton_executable.text(),
            player_text('launcher.app_window', 'proton_program_proton_all_files'),
        )
        if value:
            self.proton_executable.setText(value)
            self._save_proton()

    def _select_proton(self) -> None:
        self._show_page(1)
        if self.proton_executable is not None:
            self.proton_executable.setFocus(Qt.FocusReason.OtherFocusReason)
            self._toggle_paths(force=True)
            self._browse_proton()

    def _browse_saves(self) -> None:
        start_dir = ""
        try:
            current = self.saves_root.text().strip()
            if current and is_saved_games_base_shape(current):
                start_dir = current
            else:
                canonical = doom_saved_games_base()
                if canonical is not None and canonical.is_dir():
                    start_dir = str(canonical)
        except (OSError, TypeError, ValueError, RuntimeError):
            start_dir = ""
        value = QFileDialog.getExistingDirectory(
            self, player_text('launcher.ui', 'select_doom_eternal_save_folder'), start_dir,
        )
        if value:
            self.saves_root.setText(value)

    def _render_item_history_status(self, event: dict[str, object]) -> None:
        blocked = event.get("status") == "blocked"
        self.player_inventory.setText(str(event.get("message", "")))
        self.resync_inventory_button.setEnabled(not blocked and self._room_connected)

    def _render_room(self, event: dict[str, object]) -> None:
        self.room_event = dict(event)

        def text_or(value: object, fallback: str) -> str:
            text = str(value).strip() if value is not None else ""
            return text or fallback

        seed = text_or(event.get("seed_name"), player_text("launcher.app_window", "room_name_unavailable"))
        team, slot = (event.get("team", "?"), event.get("slot", "?"))
        endpoint = text_or(
            event.get('endpoint') or self.controller.config.get('server_address'),
            player_text('launcher.app_window', 'server_unavailable'),
        )
        player = text_or(
            event.get('slot_name') or event.get('player_name') or self.controller.config.get('slot'),
            player_text('launcher.app_window', 'player_number_value_0', value_0=f'{slot}'),
        )
        self.session_player_name.setText(
            player_text(
                'launcher.ui', 'value_0_team_value_1_player_number_value_2', value_0=f'{player}', value_1=f'{team}',
                value_2=f'{slot}',
            )
        )
        self._set_connection_badge(player_text('common', 'connected'), True)
        self.player_team.setText(player_text("launcher.ui", "team_value_0", value_0=f"{team}"))
        self.player_slot.setText(
            player_text("launcher.ui", "player_number_value_0_value_1", value_0=f"{slot}", value_1=f"{player}")
        )
        self.player_inventory.setText(player_text("launcher.ui", "received_items_loaded"))
        self._set_ammo_refill_indicator(event.get("ammo_refills_available"))
        self.resync_inventory_button.setEnabled(self._room_connected and (not self._connection_pending))
        history = self.controller.item_history_status
        if history:
            self._render_item_history_status(history)
        self.session_uninstall_button.setEnabled(self._room_connected)
        self._clear_drift()
        raw_slot_data = event.get("slot_data")
        slot_data: dict[object, object] = raw_slot_data if isinstance(raw_slot_data, dict) else {}
        options = slot_data.get("tracker_options", {})
        options = options if isinstance(options, dict) else {}
        goal = self._room_option_value(options, "goal", player_text("launcher.app_window", "not_supplied"))
        campaign = self._room_option_value(options, "mission_pool", player_text("launcher.app_window", "not_supplied"))
        logic = self._room_option_value(options, "logic_difficulty", player_text("launcher.app_window", "not_supplied"))
        fields = (
            ("Seed", seed),
            (
                player_text("launcher.app_window", "player"),
                player_text(
                    'launcher.ui', 'value_0_team_value_1_player_number_value_2', value_0=f'{player}', value_1=f'{team}',
                    value_2=f'{slot}',
                ),
            ),
            ("Server", endpoint),
            ("Goal", goal),
            ("Campaign", campaign),
            (player_text('common', 'logic_difficulty'), logic),
        )
        self.room_summary.setText(
            "<br>".join((f"<b>{html.escape(label.upper())}</b> &nbsp; {html.escape(value)}" for label, value in fields))
        )
        self.help_view.room_card.show()

    def _room_choice_label(self, option, value):
        for choice in option.get("choices", []) or []:
            if isinstance(choice, dict) and value in (choice.get("key"), choice.get("value")):
                return str(choice.get("label", value))

    def _room_option_label(self, key, value):
        schema = self.controller.options_schema
        if isinstance(schema, dict):
            for option in schema.get("options", []) or []:
                if not isinstance(option, dict) or option.get("key") != key:
                    continue
                label = self._room_choice_label(option, value)
                if label is not None:
                    return label

    def _room_option_value(self, slot_data: dict[object, object], key: str, fallback: str) -> str:
        if key not in slot_data:
            return fallback
        value = slot_data[key]
        label = self._room_option_label(key, value)
        if label is not None:
            return label
        return self.player_view._option_value_text(value)

    def _clear_drift(self) -> None:
        self.drift.clear()
        self.drift.hide()

    def _view_room_options(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(player_text("launcher.ui", "room_settings"))
        dialog.resize(640, 480)
        layout = QVBoxLayout(dialog)
        layout.setSpacing(12)
        layout.addWidget(self._label(player_text("launcher.ui", "room_settings_fff13c"), "section"))
        options = QPlainTextEdit()
        options.setReadOnly(True)
        slot_data = self.room_event.get("slot_data", {})
        if isinstance(slot_data, dict):
            options.setPlainText(
                "\n".join(
                    f"{self.player_view._option_label(key)}: {self.player_view._option_value_text(value)}"
                    for key, value in sorted(slot_data.items(), key=lambda item: str(item[0]))
                    if key != "mission_difficulty"
                )
                or player_text("launcher.app_window", "no_settings_supplied")
            )
        else:
            options.setPlainText(player_text("launcher.app_window", "no_settings_supplied"))
        layout.addWidget(options)
        close = QPushButton(player_text("launcher.ui", "close"))
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        dialog.exec()


    def _run_doctor(self) -> None:
        self.controller.request_doctor()

    def _render_doctor_report(self, report):
        model = DiagnosticsPresenter.present(
            report,
            failure=self.controller.last_setup_failure,
            connected=self._room_connected,
            room_issue=bool(
                self._current_room_package_failure() or self._setup_state in {"install_needed", "update_required"}
            ),
        )
        self.help_view.render(model, self.COLORS)
        self._refresh_help_room_package()
        for line in model.technical_lines:
            self._append_log(line)

    def _present_problem_report(self, event):
        dialog = ReportDialog(self.controller, event, self)
        dialog.exec()
        try:
            dialog.save_draft()
        except (OSError, ValueError) as error:
            self._append_log('Report draft: {value_0}'.format(value_0=f'{error}'))

    def _save_support_bundle(self) -> None:
        self.controller.request_support_bundle(
            Path.home() / 'DOOM-Eternal-Archipelago-support.zip', logs=self.help_view.log.toPlainText().splitlines(),
        )

    def _preview_repairs(self) -> None:
        if self._setup_state in {"package_failed", "package_incompatible"}:
            self._prepare(force=True)
            return
        self.controller.request_doctor(preview=True)

    def _present_repair_preview(self, actions, generation) -> None:
        self._repair_preview_request = None
        self.help_view.repair_preview.hide()
        if not actions:
            self.help_view.doctor_action.setText(player_text('launcher.ui', 'manual_repair_help'))
            return
        action = actions[0]
        prompt = "".join(
            (
                '{value_0}'.format(value_0=f"{action['title']}"), '\n', '', '\n', player_text('launcher.ui', 'changes'),
                '\n', '', '\n'.join(f'• {change}' for change in action['changes']), '', '\n', '', '\n',
                player_text('launcher.ui', 'recovery_options_value_0', value_0=f"{action['rollback']}"),
            )
        )
        self._repair_preview_request = (action["action_id"], generation)
        self.help_view.repair_changes.setText(prompt)
        self.help_view.repair_preview.show()

    def _apply_repair_preview(self):
        if self.help_view.repair_preview.isHidden():
            return
        request = self._repair_preview_request
        self._repair_preview_request = None
        self.help_view.repair_preview.hide()
        if request is not None and self.controller.request_repair(request[0], generation=request[1]):
            self.help_view.doctor_action.setText(player_text("launcher.ui", "repairing"))

    def _clear_pending_confirmations(self):
        self._manual_installation_generation = None
        self._repair_preview_request = None
        self.help_view.repair_preview.hide()
        self.help_view.download_prompt.hide()
        request = self._dependency_request
        self._dependency_request = None
        if request is not None:
            self.controller.resolve_consent(request[0], False)

    def _sync_items_room(self, event):
        kind = event.get("type")
        if kind in {"connected", "connecting", "disconnected"}:
            self._clear_pending_confirmations()
            self._capability_request = None
            self._installation_request = None
            self.install_yes.hide()
            self.install_no.hide()
        if kind == "connected":
            data = event["slot_data"]
            room = RoomKey(event["seed_name"], event["team"], event["slot"], data["native_generation_fingerprint"])
            self.items_presenter.activate(room, online=True)
            self._set_item_names(event)
            self._capability_request = None
            self.items_view.request()
        elif kind == "connecting":
            self.items_presenter = ItemsPresenter()
            self.items_presenter.blocked_only = self.items_view.blocked.isChecked()
            self.items_view.presenter = self.items_presenter
            self.items_view.render()
        elif self._items_offline_event(kind) and self.items_presenter.model.room:
            self.items_presenter.activate(self.items_presenter.model.room, online=False)
            self.items_view.render()
        self._update_ammo_refill_button()

    def _items_offline_event(self, kind):
        return kind in {"disconnected", "connection_lost", "error"} or (
            kind == "setup_failed" and not self.controller.connected_room
        )

    def _request_items(self, query):
        if not self.controller.request_items(query, online=self.items_presenter.model.online):
            self.items_presenter.cancel_query()

    def _request_items_capabilities(self):
        model = self.items_presenter.model
        if model.online and model.room and self._capability_request is None:
            self._capability_request = str(uuid4())
            if not self.controller.request_delivery_capabilities(model.room, self._capability_request):
                self._capability_request = None
                self.items_presenter.capabilities(model.room, can_resolve=False)

    def _resolve_item(self, request):
        try:
            self.controller.resolve_item(request)
        except (RuntimeError, OSError):
            self.items_presenter.resolution_result(request.room, request.request_id, ok=False)
            self.items_view.render()

    def _present_items_event(self, event):
        if self._stale_resync(event):
            return True
        if isinstance(event, LocalDeliveryStatus):
            self.items_presenter.local_status(event.room, event.item_id, event.actions)
            self.items_view.render()
            return True
        if isinstance(event, DeliveryProgress):
            self._present_delivery_progress(event)
            return True
        if isinstance(event, ItemsSnapshot):
            followup = self.items_presenter.observe(event)
            if followup:
                self._request_items(followup)
            self._update_ammo_refill_button()
            self.items_view.render()
            return True
        if isinstance(event, AccountingCapabilityChanged):
            if event.request_id == self._capability_request:
                self._capability_request = None
                self.items_presenter.capabilities(event.room, can_resolve=event.can_resolve, can_spend=event.can_spend)
                self._update_ammo_refill_button()
                self.items_view.render()
            return True
        if isinstance(event, ItemResolutionResult):
            if self.items_presenter.resolution_result(event.room, event.request_id, ok=event.succeeded):
                self.items_view.request()
            return True
        return False

    def _stale_resync(self, event):
        return (
            isinstance(event, ResyncProgress)
            and event.room is not None
            and event.room != self.items_presenter.model.room
        )

    def _present_delivery_progress(self, event):
        if event.room == self.items_presenter.model.room:
            self.items_view.request()

    def _create_settings_controls(self):
        self.ammo_refill_keybind = AmmoRefillKeyControl(str(self.controller.config.get("ammo_refill_keybind", "F9")))
        self.ammo_refill_keybind.changed.connect(self._save_ammo_refill_keybind)
        self.special_toggle_keybind = AmmoRefillKeyControl(
            str(self.controller.config.get('special_toggle_keybind', 'F10')), default='F10',
        )
        self.special_toggle_keybind.changed.connect(self._save_special_toggle_keybind)
        self.killsanity_counter_visible = ToggleSwitch(player_text("launcher.settings", "killsanity"))
        self.killsanity_counter_visible.setChecked(
            self.controller.config.get("killsanity_counter_visible", True) is True
        )
        self.killsanity_counter_visible.toggled.connect(self._save_killsanity_counter_visible)
        self.death_link_grace = QSpinBox()
        self.death_link_grace.setRange(0, 99)
        self.death_link_grace.setFixedWidth(56)
        self.death_link_grace.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.death_link_grace.setAlignment(Qt.AlignmentFlag.AlignCenter)
        grace_control = QWidget()
        self.grace_control = QWidget()
        progress_layout = QVBoxLayout(self.grace_control)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.addWidget(grace_control)
        self.death_link_progress = self._label(player_text('launcher.settings', 'deathlink_unavailable'), 'muted')
        self.death_link_progress.setWordWrap(True)
        progress_layout.addWidget(self.death_link_progress)
        self._deathlink_progress = dict(status='unavailable')
        grace_control.setObjectName("stepper")
        grace_control.setFixedSize(128, 36)
        grace_layout = QHBoxLayout(grace_control)
        grace_layout.setContentsMargins(0, 0, 0, 0)
        grace_layout.setSpacing(0)
        decrease, increase = (QPushButton('\u2212'), QPushButton('+'))
        for button, action, label in (
            (decrease, self.death_link_grace.stepDown, player_text('launcher.ui', 'decrease_deaths_per_deathlink')),
            (increase, self.death_link_grace.stepUp, player_text('launcher.ui', 'increase_deaths_per_deathlink')),
        ):
            button.setFixedSize(36, 36)
            button.setAccessibleName(label)
            button.clicked.connect(action)
        grace_layout.addWidget(decrease)
        decrease.setObjectName("stepDown")
        increase.setObjectName("stepUp")
        grace_layout.addWidget(self.death_link_grace, 1)
        grace_layout.addWidget(increase)
        self.death_link_grace.setValue(int(self.controller.config.get("death_link_grace", 0)))
        self.death_link_grace.setToolTip(player_text("launcher.settings", "deathlink_help"))
        self.death_link_grace.valueChanged.connect(self._save_death_link_grace)

    def _ammo_refill_block(self):
        ammo_refill_block = QWidget()
        ammo_refill_block.setMinimumWidth(230)
        ammo_refill_block.setMaximumWidth(260)
        ammo_refill_block.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        ammo_block_layout = QVBoxLayout(ammo_refill_block)
        ammo_block_layout.setContentsMargins(0, 0, 0, 0)
        ammo_block_layout.setSpacing(4)
        self.player_ammo_refills = QWidget()
        self.player_ammo_refills.setObjectName("ammoRefillIndicator")
        self.player_ammo_refills.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        ammo_layout = QHBoxLayout(self.player_ammo_refills)
        ammo_layout.setContentsMargins(0, 0, 0, 0)
        ammo_layout.setSpacing(8)
        ammo_layout.addWidget(self._label(player_text('common', 'ammo_refill'), "ammoRefillTitle"))
        self.ammo_refill_pips = _AmmoRefillPips()
        ammo_layout.addWidget(self.ammo_refill_pips, 1)
        self.ammo_refill_key = self._label("", "ammoRefillKey")
        self.ammo_refill_key.setFixedSize(20, 20)
        self.ammo_refill_key.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ammo_layout.addWidget(self.ammo_refill_key)
        ammo_block_layout.addWidget(self.player_ammo_refills)
        self.ammo_refill_button = QPushButton(player_text("launcher.ui", "use_ammo_refill"))
        self.ammo_refill_button.setObjectName("ammoRefillButton")
        self.ammo_refill_button.setToolTip(player_text("launcher.ui", "use_one_available_ammo_refill_charge"))
        self.ammo_refill_button.clicked.connect(self._request_ammo_refill)
        ammo_block_layout.addWidget(self.ammo_refill_button)
        self._set_ammo_refill_indicator(None)
        return ammo_refill_block


    _EVENT_HANDLERS: ClassVar = {
        'deathlink_progress': '_on_deathlink_progress',
        'core_update_status': '_on_core_update_status', 'core_runtime_selected': '_on_core_update_status',
        'support_bundle_ready': '_on_support_bundle_ready', 'support_bundle_failed': '_on_support_bundle_failed',
        'problem_report_ready': '_on_problem_report_ready', 'ap_backup_result': '_on_ap_backup_result',
        'ap_save_inspection': '_on_ap_save_inspection', 'ap_save_error': '_on_ap_save_error',
        'problem_report_submitted': '_on_problem_report_submitted', 'problem_report_retry': '_on_problem_report_retry',
        'integration_status': '_on_integration_status', 'doctor_report': '_on_doctor_report',
        'repair_preview_result': '_on_repair_preview_result', 'doctor_failed': '_on_doctor_failed',
        'integration_repair_result': '_on_integration_repair_result', 'ui_repair_result': '_on_ui_repair_result',
        'log': '_on_log', 'native_client_start_failed': '_on_native_client_start_failed',
        'item_history_status': '_on_item_history_status', 'inventory_resync': '_on_inventory_resync',
        'ammo_refill': '_on_ammo_refill', 'ammo_refill_keybind_status': '_on_ammo_refill_keybind_status',
        'uninstall_queued': '_on_uninstall_queued', 'uninstall_started': '_on_uninstall_started',
        'uninstall_progress': '_on_uninstall_started',
        'injector_post_run_confirmation': '_on_injector_post_run_confirmation',
        'uninstall_complete': '_on_uninstall_complete', 'uninstall_attention': '_on_uninstall_attention',
        'uninstall_failed': '_on_uninstall_attention', 'connected': '_on_connected',
        'hints_loading': '_on_hints_loading', 'hints': '_on_hints', 'chat_sent': '_on_chat_sent',
        'chat_send_failed': '_on_chat_send_failed', 'client_started': '_on_client_started',
        'connecting': '_on_client_started', 'game_link_installed': '_on_game_link_installed',
        'room_install_state': '_on_room_install_state', 'setup_started': '_on_setup_started',
        'mod_building': '_on_setup_started', 'runtime_config_ready': '_on_setup_started',
        'mod_staged': '_on_setup_started', 'dependency_consent_required': '_on_dependency_consent_required',
        'setup_ready': '_on_setup_ready', 'manager_started': '_on_manager_started',
        'injector_started': '_on_manager_started', 'manager_closed': '_on_manager_closed',
        'injector_closed': '_on_manager_closed',
        'uninstall_confirmation_required': '_on_uninstall_confirmation_required',
        'installation_confirmation_required': '_on_installation_confirmation_required',
        'installation_declined': '_on_installation_declined', 'manual_install_required': '_on_manual_install_required',
        'injector_finished': '_on_injector_finished', 'disconnected': '_on_disconnected',
        'connection_lost': '_on_connection_lost', 'setup_failed': '_on_setup_failed', 'error': '_on_setup_failed',
        'warning': '_on_warning',
    }


    def _show_installation_confirmation(self):
        self.session_setup_detail.setText(player_text("launcher.setup", "confirm_question"))
        self.session_setup.show()
        self.install_yes.show()
        self.install_no.show()

    def _answer_installation(self, confirmed):
        request = self._installation_request
        generation = self._manual_installation_generation
        self._installation_request = None
        self._manual_installation_generation = None
        self.install_yes.hide()
        self.install_no.hide()
        if request is not None:
            self.controller.resolve_installation_confirmation(request, confirmed)
        elif confirmed and generation is not None and generation == self.controller.operation_generation:
            self._complete_manual_installation(generation)


    def _load_icon(self) -> None:
        icon = self.controller.bundle_dir / "assets/launcher/EternalAP.ico"
        if icon.is_file():
            self.setWindowIcon(QIcon(str(icon)))


    def closeEvent(self, event) -> None:
        if not self.controller.workflow.session_owner.can_close():
            QMessageBox.information(
                self, player_text('launcher.ui', 'game_still_running'),
                player_text('launcher.ui', 'close_doom_eternal_before_closing_this_launcher'),
            )
            event.ignore()
            return
        try:
            if self.controller.close() is False:
                self._close_requested = True
                self.centralWidget().setEnabled(False)
                self._render_setup_progress()
                QTimer.singleShot(100, self._finish_close)
                event.ignore()
                return
        except RuntimeError as error:
            QMessageBox.information(self, player_text("launcher.ui", "game_still_running"), str(error))
            event.ignore()
            return
        self.timer.stop()
        self.lifecycle_timer.stop()
        event.accept()

    def _finish_close(self) -> None:
        if self.controller.workers.active:
            QTimer.singleShot(100, self._finish_close)
        else:
            self.close()

    def run(self) -> None:
        self.show()
        if os.name == "nt" and getattr(sys, "frozen", False):
            import ctypes

            kernel = ctypes.windll.kernel32
            user = ctypes.windll.user32
            kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
            kernel.GetModuleHandleW.restype = ctypes.c_void_p
            user.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            user.LoadIconW.restype = ctypes.c_void_p
            user.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
            user.SendMessageW.restype = ctypes.c_ssize_t
            icon = user.LoadIconW(kernel.GetModuleHandleW(None), 1)
            if icon:
                for size in (0, 1):
                    user.SendMessageW(int(self.winId()), 0x80, size, icon)
        app = QApplication.instance()
        if app is None:
            raise RuntimeError('LauncherUI requires QApplication')
        app.exec()

    def _session_log_page(self) -> QFrame:
        card = self._card()
        layout = QVBoxLayout(card)
        layout.setSpacing(12)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(self._label(player_text("launcher.ui", "session_log"), "section"))
        self.session_log = QPlainTextEdit()
        self.session_log.setReadOnly(True)
        self.session_log.setFont(QFont("monospace", 10))
        self.session_log.document().setMaximumBlockCount(self._session_log_limit)
        layout.addWidget(self.session_log, 1)
        return card

    def _activity_card(self) -> QFrame:
        card = self._card("plain")
        layout = QVBoxLayout(card)
        layout.setSpacing(12)
        layout.setContentsMargins(0, 0, 0, 0)
        self.activity_empty = self._label(player_text("launcher.play", "activity_empty"), "empty")
        self.activity_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.activity_empty, 1)
        self.activity = QTableWidget(0, 3)
        self.activity.setHorizontalHeaderLabels(
            [
                player_text('launcher.ui', 'time'), player_text('launcher.ui', 'category'),
                player_text('launcher.ui', 'details'),
            ]
        )
        self.activity.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.activity.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.activity.setShowGrid(False)
        self.activity.horizontalHeader().hide()
        self.activity.hide()
        self.activity.verticalHeader().hide()
        self.activity.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.activity.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.activity.setColumnWidth(0, 86)
        self.activity.setColumnWidth(1, 132)
        self.activity.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.activity, 1)
        return card

    def _hints_card(self) -> QFrame:
        card = self._card("plain")
        layout = QVBoxLayout(card)
        layout.setSpacing(12)
        layout.setContentsMargins(0, 0, 0, 0)
        self.hints_empty = self._label(player_text("launcher.ui", "connect_to_the_room_to_see_hints"), "muted")
        self.hints_empty.setObjectName("empty")
        self.hints_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.hints_empty, 1)
        self.hints = QTableWidget(0, 5)
        self.hints.setHorizontalHeaderLabels(
            [
                player_text('launcher.items', 'status'), player_text('launcher.items', 'item'),
                player_text('launcher.ui', 'location'), player_text('launcher.ui', 'for'),
                player_text('launcher.ui', 'in_world'),
            ]
        )
        self.hints.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.hints.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.hints.setShowGrid(False)
        self.hints.horizontalHeader().hide()
        self.hints.verticalHeader().hide()
        self.hints.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for column in range(1, 5):
            self.hints.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.hints, 1)
        return card

    def _chat_bar(self) -> QFrame:
        card = self._card("plain")
        layout = QHBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self._label(player_text('common', 'chat'), "eyebrow"))
        self.command_input = QLineEdit()
        self.command_input.setPlaceholderText(player_text("launcher.ui", "send_a_message_or_server_command"))
        self.command_input.setEnabled(False)
        self.command_input.returnPressed.connect(self._send_command)
        layout.addWidget(self.command_input, 1)
        self.command_send = QPushButton(player_text("launcher.ui", "send"))
        self.command_send.setEnabled(False)
        self.command_send.clicked.connect(self._send_command)
        layout.addWidget(self.command_send)
        return card

    def _send_command(self) -> None:
        text = self.command_input.text()
        if not text.strip() or self._chat_pending_text is not None:
            return
        try:
            self.controller.send_chat(text)
        except (OSError, RuntimeError, ValueError) as error:
            self._append_log('Command error: {value_0}'.format(value_0=f'{error}'))
            return
        self._chat_pending_text = text
        self._set_chat_enabled(False)

    def _set_chat_enabled(self, enabled: bool) -> None:
        ready = enabled and self._chat_pending_text is None
        self.command_input.setEnabled(ready)
        self.command_send.setEnabled(ready)
        message = player_text("launcher.play", "connect_chat") if not enabled else ""
        self.command_input.setPlaceholderText(message or player_text("launcher.ui", "send_a_message_or_server_command"))
        self.command_input.setToolTip(message)
        self.command_send.setToolTip(message)

    def _render_hints(self, event: dict[str, object]) -> None:
        records = event.get("hints")
        if not isinstance(records, list):
            return
        deduped: dict[tuple[object, ...], dict[str, object]] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            key = tuple(
                record.get(field)
                for field in ('receiving_player', 'finding_player', 'location', 'item', 'entrance')
            )
            deduped[key] = record
        self.hints.setRowCount(0)
        for row, record in enumerate(deduped.values()):
            self.hints.insertRow(row)
            values = (
                str(record.get('status_name', 'HINT_UNSPECIFIED')).replace('HINT_', '').replace('_', ' '),
                str(record.get('item_name', player_text('launcher.app_window', 'item_name_unavailable'))),
                str(record.get('location_name', player_text('launcher.app_window', 'location_name_unavailable'))),
                str(record.get('receiving_player_name', record.get('receiving_player', '?'))),
                str(record.get('finding_player_name', record.get('finding_player', '?'))),
            )
            for column, value in enumerate(values):
                self.hints.setItem(row, column, QTableWidgetItem(value))
        self._hints_state = "loaded"
        self.hints_empty.setText(player_text("launcher.ui", "no_hints_yet") if not deduped else "")
        self.hints_empty.setVisible(not deduped)
        self.hints.setVisible(bool(deduped))

    def _set_hints_state(self, state: str) -> None:
        self._hints_state = state
        self.hints.setRowCount(0)
        self.hints.setVisible(False)
        labels = {
            'loading': player_text('launcher.app_window', 'loading_hints'),
            'disconnected': player_text('launcher.ui', 'connect_to_the_room_to_see_hints'),
        }
        self.hints_empty.setText(labels[state])
        self.hints_empty.setVisible(True)

    def _present_event(self, event: dict[str, object]) -> None:
        if self._present_items_event(event):
            return
        kind = str(event.get("type", "event"))
        try:
            event = self._observe_setup_progress(event)
            if event is None:
                return
            if event.get("offer_new_save"):
                self.controller.request_ap_save_inspection()
            self._handle_event(event)
            self._sync_items_room(event)
            self._render_setup_progress()
        except (OSError, RuntimeError, ValueError) as error:
            self._append_log(
                'UI lifecycle presentation failed for {value_0}: {value_1}: {value_2}'.format(value_0=f'{kind}', value_1=f'{type(error).__name__}', value_2=f'{error}')
            )
        try:
            self._activity_event(event)
        except (OSError, RuntimeError, ValueError) as error:
            self._append_log(
                'UI activity presentation failed for {value_0}: {value_1}: {value_2}'.format(value_0=f'{kind}', value_1=f'{type(error).__name__}', value_2=f'{error}')
            )

    def _ammo_refill_message(self, event, status):
        if status in {"error", "blocked"}:
            return self._operation_failure("ammo_refill", event)
        return str(event.get("message") or player_text("launcher.app_window", "ammo_refill_update"))

    def _activity_event(self, event: dict[str, object]) -> None:
        kind = str(event.get("type", "event"))
        normalized = self._normalize_activity_event(event)
        if normalized is None:
            return
        segment, color, detail = normalized
        scroll = self.activity.verticalScrollBar()
        scroll_val = scroll.value()
        was_at_top = scroll_val == scroll.minimum()
        selected_row = self.activity.currentRow()
        selected_col = max(0, self.activity.currentColumn())
        row = 0
        self.activity_empty.hide()
        self.activity.show()
        self.activity.insertRow(row)
        self.activity.setRowHeight(row, 40)
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
            self.activity.setRowHeight(row, 38)
        else:
            self.activity.setItem(row, 2, QTableWidgetItem(detail))
        while self.activity.rowCount() > 100:
            self.activity.removeRow(self.activity.rowCount() - 1)
        if selected_row >= 0:
            target_row = selected_row + 1
            if target_row < self.activity.rowCount():
                self.activity.setCurrentCell(target_row, selected_col)
            else:
                self.activity.clearSelection()
                self.activity.setCurrentCell(-1, -1)
        if was_at_top:
            scroll.setValue(scroll.minimum())
        else:
            scroll.setValue(scroll_val + 1)

    def _room_install_activity(self, event):
        kind = str(event.get("type", ""))
        if kind == "room_install_state":
            if self._is_room_package_failure(event) or str(event.get("readiness")) == "blocked":
                return (
                    player_text('launcher.support', 'activity_installation'), self.COLORS['bad'],
                    player_text('launcher.app_window', 'installation_failed'),
                )
            return (
                player_text('launcher.support', 'activity_installation'), self.COLORS['ap'],
                player_text('launcher.app_window', 'preparing_this_room_for_play'),
            )

    def _notification_activity(self, kind, event):
        if kind == "deathlink":
            detail = str(
                event.get("message") or event.get("cause") or player_text("launcher.app_window", "deathlink_received")
            )
            return player_text('common', 'deathlink'), self.COLORS["warn"], detail[:512]
        if kind in {"item", "location"}:
            detail = str(
                event.get("message") or event.get("plain") or player_text("launcher.app_window", "archipelago_update")
            )
            return player_text('common', 'archipelago'), self.COLORS["ap"], detail[:512]

    def _normalize_activity_event(self, event: dict[str, object]) -> tuple[str, str, str] | None:
        """Project whitelisted player events into Activity; diagnostics stay in Log."""
        kind = str(event.get("type", ""))
        if kind == "archipelago":
            return player_text('common', 'archipelago'), ARCHIPELAGO_PRESENTATION_COLORS["player_remote"], ""
        if kind == "chat_sent":
            message = str(event.get("text", "")).strip()
            return (player_text('common', 'chat'), self.COLORS["ap"], message) if message else None
        if kind in {"deathlink", "item", "location"}:
            return self._notification_activity(kind, event)
        if kind in {"ammo_refill", "stage_restart"}:
            return self._action_activity(event, kind)
        if kind in {"setup_ready", "native_client_stopped_for_install"}:
            return (
                player_text('launcher.support', 'activity_installation'), self.COLORS['accent'],
                player_text('launcher.app_window', {'setup_ready': 'installation_complete'}.get(kind, kind)),
            )
        return self._room_install_activity(event)

    def _action_activity(self, event, kind):
        status = str(event.get("status", ""))
        if kind == "stage_restart":
            if status not in {"pending", "done", "failed"}:
                return None
            return ("Ultra-Nightmare", self.COLORS["warn"],
                    player_text("stage_restart", status, stage=str(event.get("stage", ""))))
        if status not in {"used", "empty", "blocked", "error"}:
            return None
        detail = self._ammo_refill_message(event, status)
        return player_text('common', 'archipelago'), ARCHIPELAGO_PRESENTATION_COLORS["player_remote"], detail[:512]

    def _activity_segment_color(self, segment, colors):
        segment_type = str(segment.get("type") or "text")
        if segment_type == "player":
            color = colors["player_self"] if bool(segment.get("self")) else colors["player_remote"]
        elif segment_type == "item":
            color = colors.get(f"item_{segment.get('classification')}", colors["text"])
        else:
            color = colors.get(segment_type, colors["text"])
        return color

    def _archipelago_activity_detail(self, event: dict[str, object]) -> str:
        plain = str(event.get("plain") or "")
        segments = event.get("segments")
        if event.get("schema") != 1 or not isinstance(segments, list):
            return html.escape(plain)
        colors = ARCHIPELAGO_PRESENTATION_COLORS
        rendered: list[str] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            text = html.escape(str(segment.get("text") or ""))
            if not text:
                continue
            color = self._activity_segment_color(segment, colors)
            rendered.append(f'<span style="color:{color};">{text}</span>')
        return "".join(rendered) or html.escape(plain)

    def _append_log(self, text: str) -> None:
        sanitized = redact_secrets(text).replace("\r", " ").strip()
        if not sanitized:
            return
        if hasattr(self, "help_view"):
            self.help_view.log.appendPlainText(sanitized)
        if hasattr(self, "session_log") and self.session_log is not None:
            self.session_log.appendPlainText(sanitized)

    def _append_session_event(self, event: dict[str, object]) -> None:
        kind = str(event.get("type", "event"))
        if "heartbeat" in kind.casefold() or kind == "log":
            return
        if kind == "integration_status":
            semantic = (
                *(event.get(key) for key in ('meathook_ok', 'meathook_status', 'native_state', 'reason')),
                event.get('session', {}).get('state'),
            )
            if semantic == self._last_integration_status:
                return
            self._last_integration_status = semantic
        details = []
        for key in ("endpoint", "slot", "seed_name", "state", "code", "reason", "message"):
            value = event.get(key)
            if value not in (None, ""):
                details.append(f"{key}={value}")
        self._append_log(f"{datetime.now().strftime("%H:%M:%S")} {kind}: {' | '.join(details) or 'received'}")

    def _poll_events(self) -> None:
        self._event_poll_scheduled = False
        started = time.perf_counter()
        processed = 0
        while processed < 100 and (time.perf_counter() - started) < 0.005:
            try:
                event = self.controller.events.get_nowait()
            except queue.Empty:
                break
            if self.controller.process_event(event) is not False:
                self._present_event(event)
            processed += 1
        if not self.controller.events.empty() and not self._event_poll_scheduled:
            self._event_poll_scheduled = True
            QTimer.singleShot(0, self._poll_events)

    def _poll_slow_state(self) -> None:
        if self._items_generation != self.controller.operation_generation:
            self._items_generation = self.controller.operation_generation
            self._capability_request = None
            self.items_presenter.cancel_query()
            self.items_presenter.capabilities(self.items_presenter.model.room, can_resolve=False)
        key = RoomSetupCoordinator.room_key(self.room_event) if self._room_connected else None
        self._play_presenter.activate(self.controller.operation_generation, key, self.controller.connection_attempt_id)
        self._render_setup_progress()
        self.controller.poll_game_lifecycle()
        self._refresh_native_health()
        self.items_view.request()
        self._request_items_capabilities()

    def _refresh_native_health(self) -> None:
        self.controller.request_integration_status()

    def _render_proton_runtime(self, event):
        proton = event.get("session", {}).get("proton_runtime")
        if proton and self.proton_executable is not None:
            self.proton_executable.setPlaceholderText(player_text("launcher.ui", "automatic") + proton["executable"])
            self.proton_executable.setToolTip(
                player_text("launcher.ui", "selected_proton_value_0", value_0=f"{proton['tool']}")
                + "\n"
                + '{value_1}'.format(value_1=f"{proton['executable']}")
            )

    def _render_native_health(self, event) -> None:
        self._render_proton_runtime(event)
        if self._setup_state in {"game_link_needed", "game_link_update_needed"}:
            if event["meathook_ok"]:
                self._native_health_presentation = None
                self._set_setup_state("ready")
            else:
                self._native_health_presentation = None
                target_state = (
                    "game_link_update_needed" if event["meathook_status"] == "incompatible" else "game_link_needed"
                )
                if self._setup_state != target_state:
                    self._append_log(str(event["meathook_message"]))
                    self._set_setup_state(target_state, failure_text("install_core", event))
                return
        elif self._setup_state == "ready" and (not event["meathook_ok"]):
            self._native_health_presentation = None
            target_state = (
                "game_link_update_needed" if event["meathook_status"] == "incompatible" else "game_link_needed"
            )
            self._append_log(str(event["meathook_message"]))
            self._set_setup_state(target_state, failure_text("install_core", event))
            return

    def _render_core_update(self, event) -> None:
        state = event.get("state")
        if state == "skipped":
            return
        if state in {"failed", "error", "deferred", "unavailable"}:
            message = self._operation_failure("install_core", event)
        elif event.get("type") == "core_runtime_selected":
            message = player_text("launcher.ui", "game_connection_files_selected")
        elif state in {"checking", "checked", "downloading"}:
            message = str(event.get("message", ""))
        else:
            self._append_log(str(event.get("message", "")))
            message = player_text("launcher.ui", "game_connection_files_download_during_setup")
        self.help_view.core_status.setText(message)
        self.help_view.core_status.setVisible(bool(message))

    def _log_installation_failure(self, event, stdout, stderr):
        self._append_log(
            str(
                event.get("message")
                or 'Installation failed at injector: exit code {value_0}. Select Help -> SAVE SUPPORT REPORT.'.format(value_0=f"{event.get("returncode")}")
            )
        )
        if stdout:
            self._append_log('Injector stdout: {value_0}'.format(value_0=f'{stdout[-500:]}'))
        if stderr:
            self._append_log('Injector stderr: {value_0}'.format(value_0=f'{stderr[-500:]}'))

    def _handle_event(self, event: dict[str, object]) -> None:
        self._append_session_event(event)
        handler = self._EVENT_HANDLERS.get(event.get("type"))
        if handler:
            getattr(self, handler)(event)

    def _on_deathlink_progress(self, event):
        status = event.get('status', 'unavailable')
        if status == 'ready' and event.get('interval') != max(1, self.death_link_grace.value()):
            return
        self._deathlink_progress = event
        if status != 'ready':
            key = 'deathlink_off' if status == 'off' else 'deathlink_unavailable'
            message = player_text('launcher.settings', key)
        elif event.get('remaining') == 1:
            message = player_text('launcher.settings', 'deathlink_remaining_one')
        else:
            message = player_text('launcher.settings', 'deathlink_remaining_many', count=event['remaining'])
        self.death_link_progress.setText(message)

    def _on_core_update_status(self, event):
        self._render_core_update(event)
        return

    def _on_support_bundle_ready(self, event):
        self.help_view.doctor_action.setText(
            player_text("launcher.ui", "support_report_saved_value_0", value_0=f"{event.get('path', '')}")
        )
        return

    def _on_support_bundle_failed(self, event):
        self.help_view.doctor_action.setText(self._operation_failure("report", event))

    def _on_problem_report_ready(self, event):
        self._present_problem_report(event)
        return

    def _on_ap_backup_result(self, event):
        self._append_log(str(event["message"]))
        QMessageBox.information(self, player_text('common', 'ap_save'), str(event["message"]))
        return

    def _on_ap_save_inspection(self, event):
        self._show_ap_save_actions(
            event['inspection'], event.get('message', player_text('launcher.save', 'backup_guidance')),
        )
        return

    def _on_ap_save_error(self, event):
        QMessageBox.warning(self, player_text("common", "ap_save"), self._operation_failure("saves", event))

    def _on_problem_report_submitted(self, event):
        self._append_log('Problem report submitted: ' + event["url"])
        QMessageBox.information(self, player_text("launcher.ui", "report_submitted"), event["url"])
        return

    def _on_problem_report_retry(self, event):
        self.help_view.doctor_action.setText(self._operation_failure("report", event))

    def _on_integration_status(self, event):
        self._render_native_health(event)
        return

    def _on_doctor_report(self, event):
        self._render_doctor_report(event.get("report"))
        return

    def _on_repair_preview_result(self, event):
        self._present_repair_preview(event.get("actions", ()), event["launcher_job_generation"])
        return

    def _on_doctor_failed(self, event):
        message = self._operation_failure("repair" if event.get("preview") else "check", event)
        self.help_view.doctor_status.setText(player_text("launcher.ui", "setup_check_could_not_run"))
        self.help_view.doctor_evidence.setText(message)

    def _on_integration_repair_result(self, event):
        if event.get("success", False):
            self._set_setup_state("game_link_update_needed")
            self._prepare(force=True)
        else:
            self._append_log(str(event.get("message", "")))
            self._set_setup_state('game_link_update_needed', player_text('launcher.ui', 'connection_repair_failed'))
        return

    def _on_ui_repair_result(self, event):
        self.help_view.core_auto_update.setChecked(bool(self.controller.config.get("core_auto_update", True)))
        self._append_log(str(event.get("message", "")))
        message = (player_text("launcher.ui", "setup_complete") if event.get("success", False)
                   else self._operation_failure("repair", event))
        self.help_view.doctor_action.setText(message)

    def _on_log(self, event):
        self._append_log(str(event.get("message", "")))
        return

    def _on_native_client_start_failed(self, event):
        QMessageBox.warning(self, player_text("launcher.controller", "game_connection_could_not_start"),
                            self._operation_failure("install_core", event))

    def _on_item_history_status(self, event):
        message = str(event.get("message", ""))
        self._render_item_history_status(event)
        self._append_log(message)
        return

    def _on_inventory_resync(self, event):
        status = str(event.get("status", ""))
        presentation = {
            'waiting': (player_text('launcher.app_window', 'return_to_doom_eternal_and_unpause'), False),
            'observed': (player_text('launcher.app_window', 'inventory_repair_confirmed'), True),
            'queued': (player_text('launcher.app_window', 'inventory_repair_waiting_for_the_game'), True),
            'noop': (player_text('launcher.app_window', 'inventory_is_up_to_date'), True),
            'error': (player_text('launcher.ui', 'inventory_repair_unavailable'), True),
        }.get(status)
        if presentation:
            detail, enabled = presentation
            self.player_inventory.setText(detail)
            self.resync_inventory_button.setEnabled(enabled and self._room_connected)
            if event.get("message"):
                self._append_log(str(event["message"]))
        return

    def _on_ammo_refill(self, event):
        available = event.get("available")
        if isinstance(available, int):
            self._set_ammo_refill_indicator(available)
        self._append_log(
            'Ammo Refill: {value_0}'.format(value_0=f"{event.get("message") or event.get("status") or 'session update'}")
        )
        return

    def _on_ammo_refill_keybind_status(self, event):
        state = str(event.get("state", ""))
        key = str(event.get("configured_key", ""))
        if state == "configured" and key:
            self._append_log('Ammo Refill key: {value_0}'.format(value_0=f'{key}'))
        elif state == "unbound" or not key:
            self._append_log('Ammo Refill key is not set.')
        return

    def _on_uninstall_queued(self, event):
        self.help_view.uninstall_button.setEnabled(False)
        self.help_view.doctor_room_status.setText(player_text("launcher.ui", "waiting_to_uninstall"))
        self.help_view.doctor_room_evidence.setText(
            player_text("launcher.ui", "waiting_for_the_current_operation_to_finish")
        )
        return

    def _on_uninstall_started(self, event):
        self.help_view.uninstall_button.setEnabled(False)
        self.help_view.doctor_room_status.setText(player_text("launcher.ui", "removing_room_mod"))
        self.help_view.doctor_room_evidence.setText(
            player_text("launcher.ui", "removing_this_room_s_mod_keep_the_launcher_open")
        )
        return

    def _on_injector_post_run_confirmation(self, event):
        self.help_view.uninstall_button.setEnabled(False)
        self.help_view.doctor_room_status.setText(player_text("launcher.ui", "checking_removal"))
        self.help_view.doctor_room_evidence.setText(player_text("launcher.ui", "checking_that_the_mod_was_removed"))
        return

    def _on_uninstall_complete(self, event):
        self.help_view.uninstall_button.setEnabled(False)
        self.help_view.doctor_room_status.setText(player_text("launcher.ui", "room_mod_removed"))
        self.help_view.doctor_room_evidence.setText(player_text('launcher.ui', 'removal_details'))
        self._set_setup_state(
            'install_needed', player_text('launcher.ui', 'select_install_room_package_before_playing'),
        )
        return

    def _on_uninstall_attention(self, event):
        self.help_view.uninstall_button.setEnabled(self._room_connected)
        self.help_view.doctor_room_status.setText(player_text("launcher.ui", "uninstall_failed"))
        self.help_view.doctor_room_evidence.setText(self._operation_failure("uninstall", event))

    def _on_connected(self, event):
        self._reconnecting = False
        self._connection_pending = False
        self._room_connected = True
        self.join_error.hide()
        self._set_connection_controls(False)
        self._render_room(event)
        self.help_view.uninstall_button.setEnabled(True)
        self._set_setup_state("checking")
        self._set_chat_enabled(True)
        self._set_hints_state("loading")

    def _on_hints_loading(self, event):
        self._set_hints_state("loading")

    def _on_hints(self, event):
        self._render_hints(event)

    def _on_chat_sent(self, event):
        if self._chat_pending_text == event.get("text"):
            self.command_input.clear()
            self._chat_pending_text = None
            self._set_chat_enabled(self._room_connected)

    def _on_chat_send_failed(self, event):
        self._append_log('Command error: ' + str(event.get('message', 'Could not send message.')))
        self._chat_pending_text = None
        self._set_chat_enabled(self._room_connected)

    def _on_client_started(self, event):
        self._connection_pending = True
        self._set_connection_controls(False)
        self._set_connection_badge(player_text("launcher.ui", "connecting"), False)

    def _on_game_link_installed(self, event):
        self._native_health_presentation = None
        self._append_log('Game Link runtime verified and installed: {value_0}'.format(value_0=f"{event.get('path')}"))

    def _on_room_install_state(self, event):
        option = str(event.get("steam_launch_option", ""))
        if option:
            self.launch_option.setText(option)
        state, reason = (str(event.get("state", "")), str(event.get("reason", "")))
        readiness = str(event.get("readiness", "ready"))
        readiness_reason = str(event.get("readiness_reason", ""))
        if state == "uninstalled":
            self._clear_drift()
            self.help_view.uninstall_button.setEnabled(False)
            self._set_setup_state(
                'install_needed', player_text('launcher.ui', 'select_install_room_package_before_playing'),
            )
        elif state == "already_installed":
            self._installed_room_state(event)
        else:
            self._room_update_state(event, state, reason, readiness, readiness_reason)

    def _on_setup_started(self, event):
        self._set_setup_state("updating" if self._setup_state == "update_required" else "installing")

    def _on_dependency_consent_required(self, event):
        self._request_dependency_consent(event)

    def _on_setup_ready(self, event):
        option = str(event.get("steam_launch_option", ""))
        state = str(event.get("adapter_state", ""))
        if option:
            self.launch_option.setText(option)
        if state == "applied":
            self._clear_drift()
            if not event["meathook_ok"]:
                self._native_health_presentation = None
                self._show_page(2)
                target_state = (
                    "game_link_update_needed" if event["meathook_status"] == "incompatible" else "game_link_needed"
                )
                self._set_setup_state(target_state, player_text('launcher.ui', 'repair_connection_help'))
            else:
                self._native_health_presentation = None
                self._show_page(2)
                self._set_setup_state("ready")
        elif state == "manual_action_required":
            self._append_log(str(event.get("message", player_text("launcher.setup", "action_finish_install"))))
            self._set_setup_state("installing", player_text("launcher.setup", "action_finish_install"))
        elif self._is_room_package_failure(event):
            self._set_setup_state("package_failed")
        else:
            self._append_log(str(event.get('message', 'Setup failed. Open Help and select Fix Setup.')))
            self._set_setup_state("failed")
        if event.get("new_install") is True:
            self._show_setup_complete()

    def _on_manager_started(self, event):
        message = str(event.get('message', 'Complete installation in EternalModInjector.'))
        self._set_setup_state("installing", installer_instruction(windows=os.name == "nt"))
        self._append_log(message)

    def _on_manager_closed(self, event):
        returncode = event.get("returncode")
        if returncode is not None:
            self._append_log('Installer closed.')
        else:
            self._append_log('Installer closed.')

    def _on_uninstall_confirmation_required(self, event):
        request_id = str(event.get("request_id", ""))
        self.help_view.uninstall_button.setEnabled(False)
        reply = QMessageBox.question(
            self, player_text('launcher.ui', 'confirm_removal'), player_text('launcher.ui', 'confirm_injector_removal'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        confirmed = reply == QMessageBox.StandardButton.Yes
        if confirmed:
            self.help_view.doctor_room_status.setText(player_text("launcher.ui", "checking_removal"))
            self.help_view.doctor_room_evidence.setText(player_text("launcher.ui", "checking_that_the_mod_was_removed"))
        else:
            self.help_view.doctor_room_status.setText(player_text("launcher.ui", "uninstall_failed"))
            self.help_view.doctor_room_evidence.setText(player_text('launcher.ui', 'removal_unconfirmed'))
        self.controller.resolve_uninstall_confirmation(request_id, confirmed)

    def _on_installation_confirmation_required(self, event):
        self._manual_installation_generation = None
        self._installation_request = event["request_id"]
        self._show_installation_confirmation()

    def _on_installation_declined(self, event):
        self._set_setup_state('manual_install_required', player_text('launcher.ui', 'install_unconfirmed_help'))
        self._append_log(player_text("launcher.ui", "install_unconfirmed_help"))

    def _on_manual_install_required(self, event):
        self._show_manual_installation(event)

    def _on_injector_finished(self, event):
        state = str(event.get("state", ""))
        stdout = str(event.get("stdout", ""))
        stderr = str(event.get("stderr", ""))
        if state == "applied":
            self._append_log('Installation finished.')
        else:
            self._log_installation_failure(event, stdout, stderr)

    def _on_disconnected(self, event):
        self._on_deathlink_progress(dict(status='unavailable'))
        self._reconnecting = False
        self._connection_pending = False
        self._room_connected = False
        self._set_connection_controls(True)
        self._chat_pending_text = None
        self._set_chat_enabled(False)
        self._set_hints_state("disconnected")
        self.help_view.uninstall_button.setEnabled(False)
        self._set_connection_badge(player_text("launcher.ui", "offline"), False)
        self.session_player_name.setText(player_text("launcher.ui", "no_room_connected"))
        self.player_team.setText(player_text("launcher.ui", "team"))
        self.player_slot.setText(player_text("launcher.ui", "player_number"))
        self.player_inventory.setText(player_text("launcher.ui", "connect_to_load_your_received_items"))
        self._set_ammo_refill_indicator(None)
        self.resync_inventory_button.setEnabled(False)
        self.session_uninstall_button.setEnabled(False)
        self._clear_drift()
        self.room_summary.setText(player_text("launcher.ui", "no_room_connected_join_a_room_to_start_playing"))
        self.help_view.room_card.hide()
        self._set_setup_state("disconnected")
        self._set_home(
            player_text('launcher.ui', 'session_ended'), player_text('launcher.ui', 'update_room_details_or_reconnect'),
            player_text('launcher.ui', 'join_a_room'), player_text('launcher.ui', 'offline'),
        )

    def _on_connection_lost(self, event):
        self._on_deathlink_progress(dict(status='unavailable'))
        if self._reconnecting:
            return
        self._reconnecting = True
        self._set_connection_badge(player_text("launcher.ui", "reconnecting"), False)
        self._set_home(
            player_text('launcher.ui', 'room_connection_interrupted'), player_text('launcher.ui', 'reconnecting_help'),
            player_text('launcher.ui', 'view_session'), player_text('launcher.ui', 'reconnecting'), enabled=True,
        )
        self._append_log(
            'Archipelago connection interrupted; automatic reconnect remains active: '
            + str(event.get('technical_message') or event.get('message') or 'connection closed')
        )

    def _on_setup_failed(self, event):
        kind = event["type"]
        message = str(event.get("message", 'Unknown error'))
        self._append_log(f"{kind}: {message}")
        if not self._room_connected:
            if kind == "error" and event.get("failure_domain") == "archipelago_connection":
                self._show_connection_failure(event)
            else:
                self._show_setup_failure(event, message)
        else:
            package_failure = self._is_room_package_failure(event)
            self._set_setup_state(self._room_package_issue_state(event) if package_failure else "failed")

    def _on_warning(self, event):
        self._append_log("Warning: " + str(event.get("message", "")))

    def _ap_save_actions(self) -> None:
        try:
            self.controller.request_ap_save_inspection()
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.warning(self, player_text('common', 'ap_save'), self._operation_failure('saves', error))

    def _ap_save_dialog(self, inspection, guidance):
        choices = [
            player_text('launcher.app_window', 'details_copy_details'),
            player_text('launcher.app_window', 'back_up_local_files'),
            player_text('launcher.app_window', 'create_backup'),
            player_text('launcher.app_window', 'open_backup_folder'),
            player_text('launcher.app_window', 'restore_compatible_backup'),
            player_text('launcher.app_window', 'new_doom_save_in_this_room'),
        ]
        dialog = QInputDialog(self)
        dialog.setWindowTitle(player_text('common', 'ap_save'))
        dialog.setLabelText(
            (
                player_text(
                    'launcher.ui', 'room_value_0_player_value_1', value_0=f"{inspection['seed']}",
                    value_1=f"{inspection['slot']}",
                )
                + ""
                + "\n"
                + player_text("launcher.app_window", "save_files")
                + '{value_0}'.format(value_0=f"{inspection['status'].replace('_', ' ')}")
                + ""
                + "\n"
                + ""
            )
            + guidance
        )
        dialog.setComboBoxItems(choices)
        combo = dialog.findChild(QComboBox)
        if inspection.get("new_save_available"):
            combo.setCurrentIndex(5)
        combo.model().item(2).setEnabled(inspection.get("native_backup_available") is True)
        return dialog

    def _new_save_guidance(self):
        from doom_eap.runtime.ledger import Authority

        if self.items_presenter.authority is Authority.LEDGER:
            return player_text("launcher.save", "ledger_restart_guidance")
        return player_text("launcher.ui", "new_save_help")

    def _show_ap_save_actions(self, inspection, guidance=None) -> None:
        dialog = self._ap_save_dialog(inspection, guidance or player_text("launcher.save", "backup_guidance"))
        if not dialog.exec():
            return
        action = ("details", "copy_files", "backup", "open_backups", "restore", "new_save")[
            dialog.findChild(QComboBox).currentIndex()
        ]
        try:
            if action == "details":
                details = json.dumps(inspection, indent=2, ensure_ascii=False)
                message = QMessageBox(self)
                message.setWindowTitle(player_text("launcher.ui", "ap_save_details"))
                message.setText(player_text('launcher.ui', 'save_inspection_help'))
                message.setDetailedText(details)
                copy = message.addButton(
                    player_text('launcher.app_window', 'copy_details'), QMessageBox.ButtonRole.ActionRole,
                )
                copy.clicked.connect(lambda: QApplication.clipboard().setText(details))
                message.addButton(QMessageBox.StandardButton.Close)
                message.exec()
            elif action == "copy_files":
                self.controller.request_ap_save_inspection(copy_files=True)
            elif action == "open_backups":
                from PySide6.QtCore import QUrl
                from PySide6.QtGui import QDesktopServices

                QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.controller.user_paths.data_dir / "campaigns")))
            elif action == "backup":
                self.controller.request_ap_backup()
            elif action == "new_save":
                if (
                    QMessageBox.question(
                        self,
                        player_text("launcher.ui", "new_doom_eternal_save"),
                        player_text(
                            'launcher.ui', 'room_value_0_player_value_1', value_0=f"{inspection['seed']}",
                            value_1=f"{inspection['slot']}",
                        )
                        + "\n"
                        + player_text("launcher.ui", "confirm_new_save")
                        + "\n"
                        + ""
                        + "\n"
                        + self._new_save_guidance(),
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    == QMessageBox.StandardButton.Yes
                ):
                    self.controller.request_new_ap_save()
            else:
                self._restore_ap_backup_dialog()
        except (OSError, RuntimeError, ValueError) as error:
            QMessageBox.warning(self, player_text('common', 'ap_save'), self._operation_failure('saves', error))

    def _restore_ap_backup_dialog(self):
        backups = self.controller.ap_backups()
        if not backups:
            raise PlayerError('launcher.app_window', 'backup_missing')
        basename, accepted = QInputDialog.getItem(
            self, player_text('launcher.ui', 'restore_ap_save'), player_text('launcher.ui', 'backup'), backups, 0,
            False,
        )
        if (
            accepted
            and QMessageBox.question(
                self, player_text('launcher.ui', 'restore_ap_save'), player_text('launcher.ui', 'confirm_restore'),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.controller.request_ap_backup(restore=basename)

    def _request_inventory_resync(self) -> None:
        if not self._room_connected:
            return
        from doom_eap.contracts.inventory_domain import ALL_PERSISTENT_DOMAIN_IDS, classify_inventory_domain

        choices = (
            ('all', 'everything'), ('weapons', 'weapons'), ('equipment', 'equipment'), ('capacities', 'capacities'),
            ('persistent_upgrades', 'persistent_upgrades'), ('special_weapons', 'special_weapons'),
            ('item', 'specific_item'),
        )
        dialog = QInputDialog(self)
        dialog.setWindowTitle(player_text('common', 'repair_inventory'))
        dialog.setLabelText(player_text("launcher.ui", "repair_scope"))
        dialog.setComboBoxItems([player_text("launcher.support", key) for _, key in choices])
        combo = dialog.findChild(QComboBox)
        for index, (domain, _) in enumerate(choices):
            combo.setItemData(index, domain)
        if not dialog.exec():
            return
        domain = combo.currentData()
        item_id = None
        if domain == "item":
            catalog = json.loads(
                (self.controller.client_dir / "data/item_classifications.json").read_text(encoding="utf-8")
            )
            items = {
                str(item["name"]): int(item_id)
                for item_id, item in catalog["items"].items()
                if int(item_id) in ALL_PERSISTENT_DOMAIN_IDS
            }
            selected, accepted = QInputDialog.getItem(
                self, player_text('common', 'repair_inventory'), player_text('launcher.ui', 'persistent_item'),
                sorted(items), 0, False,
            )
            if not accepted:
                return
            item_id = items[selected]
            domain = classify_inventory_domain(item_id)
        self.resync_inventory_button.setEnabled(False)
        try:
            self.controller.request_inventory_resync(domain=domain, item_id=item_id)
        except (OSError, RuntimeError, ValueError) as error:
            self.resync_inventory_button.setEnabled(True)
            self.player_inventory.setText(player_text('launcher.ui', 'inventory_repair_unavailable'))
            self._append_log('Inventory resync request failed: {value_0}'.format(value_0=f'{error}'))

    def _setup_action_id(self):
        actions = {
            'install_needed': ActionId.PREPARE, 'update_required': ActionId.PREPARE,
            'package_failed': ActionId.REINSTALL, 'package_incompatible': ActionId.REINSTALL,
            'failed': ActionId.DIAGNOSE, 'campaign_failed': ActionId.SAVE, 'proton_needed': ActionId.SELECT_PROTON,
            'game_link_needed': ActionId.REINSTALL, 'manual_install_required': ActionId.MANUAL_INSTALL,
            'game_link_update_needed': ActionId.REPAIR_CONNECTION,
        }
        if self._setup_state in actions:
            return actions[self._setup_state]
        if self._room_connected or self._connection_pending:
            return ActionId.VIEW_SESSION
        return ActionId.RESUME if self.controller.config.get("server_address") else ActionId.CONNECT

    def _setup_failure_presentation(self, state, detail):
        failure = self.controller.last_setup_failure
        if (
            state == "failed"
            and isinstance(failure, dict)
            and failure.get("failure_domain") in {"campaign_session", "proton_runtime"}
        ):
            state = "proton_needed" if failure["failure_domain"] == "proton_runtime" else "campaign_failed"
            detail = str(failure.get("user_message") or "")
        return state, detail

    def _set_setup_state(self, state: str, detail: str = "") -> None:
        state, detail = self._setup_failure_presentation(state, detail)
        title, fallback, action, enabled, top_state = self._setup_presentation(state)
        self._setup_state = state
        copy = fallback if state in {"package_failed", "package_incompatible"} else detail or fallback
        self._setup_copy = copy
        tone = (
            "ready" if state == "ready" else "working" if state in {"checking", "installing", "updating"} else "warning"
        )
        self.session_setup.setProperty("actionTone", tone)
        self.session_setup.style().unpolish(self.session_setup)
        self.session_setup.style().polish(self.session_setup)
        self.session_setup_title.setText(title)
        self.session_setup_detail.setText(copy)
        self.session_setup_action.setText(action)
        self.session_setup_action.setEnabled(enabled)
        self.session_setup_action.setVisible(state not in {"ready", "disconnected"})
        strip_visible = state not in {"ready", "disconnected"}
        self.session_setup.setVisible(strip_visible)
        if strip_visible and state != self._previous_setup_state:
            self._fade_in(self.session_setup)
        self._previous_setup_state = state
        if state in {"campaign_failed", "proton_needed"}:
            self.controller.request_doctor()
        self.session_manual_complete_action.setVisible(state == "manual_install_required")
        self.session_manual_retry_action.setVisible(state == "manual_install_required")
        self.reinstall_button.setText(action)
        self.reinstall_button.setEnabled(enabled)
        self.reinstall_button.setVisible(state not in {"ready", "disconnected"})
        self._set_home(title, copy, action, top_state, enabled=enabled)
        self.hero_action.setVisible(state != "disconnected")
        self._refresh_help_room_package()
        self._render_setup_progress()

    def _observe_setup_progress(self, event) -> dict | None:
        kind = event.get("type")
        room = self.room_event if self._room_connected else None
        if kind == "connected":
            room = event
        elif kind == "disconnected":
            room = None
        key = RoomSetupCoordinator.room_key(room) if room else None
        generation = self.controller.operation_generation
        attempt = self.controller.connection_attempt_id
        self._play_presenter.activate(generation, key, attempt)
        self._play_presenter.room_details(self.server.text(), self.slot.text())
        return self._play_presenter.observe(adapt_event(event, generation=generation, room=key, attempt=attempt))

    def _render_room_controls(self):
        self.room_fields.setVisible(not self._room_connected)
        self.room_identity.setVisible(self._room_connected)
        self.connected_server.setText(str(self.room_event.get("endpoint") or self.server.text()))
        self.connected_player.setText(str(self.room_event.get("slot_name") or self.room_event.get("player_name") or self.slot.text()))
        self.join_button.setText(player_text("launcher.ui", "disconnect") if self._room_connected else player_text("launcher.ui", "connect"))
        self.join_button.setObjectName("secondary" if self._room_connected else "primary")
        self.join_button.setEnabled(not self._connection_pending)
        self.join_button.setToolTip(player_text("launcher.play", "connecting_guidance", server=self.server.text()) if self._connection_pending else "")
        self.join_button.style().unpolish(self.join_button)
        self.join_button.style().polish(self.join_button)

    def _render_setup_progress(self) -> None:
        self.play_view.render(self._play_presenter.present())
        self._render_room_controls()
        if self._installation_request is not None or self._manual_installation_generation is not None:
            return
        if self._close_requested:
            message = player_text("launcher.setup", "stop_requested")
            self.session_setup_detail.setText(message)
            self.hero_detail.setText(message)
            return
        model = self._play_presenter.present()
        view = model.progress
        if model.recovery_message:
            self.session_setup_detail.setText(model.recovery_message)
            self.hero_detail.setText(model.recovery_message)
            return
        active = view is not None and self._setup_state in {"checking", "installing", "updating"}
        self.setup_elapsed.setVisible(active)
        if active:
            minutes, seconds = divmod(view.elapsed_seconds, 60)
            self.setup_elapsed.setText('{minutes}:{seconds}'.format(minutes=str(minutes), seconds=f'{seconds:02d}'))
        self.session_setup_help.setVisible(active and view.next_action is SetupAction.DIAGNOSE)
        if self._setup_state in {"checking", "installing", "updating"}:
            operation = STEP_TEXT[view.step] if active else self._setup_copy
            copy = '{operation}…'.format(operation=operation.rstrip("…."))
            self.session_setup_detail.setText(copy)
            self.hero_detail.setText(copy)

    def _show_progress_help(self) -> None:
        self._show_page(4)
        self._run_doctor()

    def _run_setup_action(self) -> None:
        action = self._setup_action_id()
        actions = {
            ActionId.PREPARE: self._prepare, ActionId.REINSTALL: lambda: self._prepare(force=True),
            ActionId.DIAGNOSE: lambda: self._show_page(4), ActionId.SAVE: self._ap_save_actions,
            ActionId.SELECT_PROTON: self._select_proton, ActionId.MANUAL_INSTALL: self._manual_install_action,
            ActionId.REPAIR_CONNECTION: self._repair_connection,
        }
        callback = actions.get(action)
        if callback:
            callback()

    def _repair_connection(self):
        generation = self.controller.operation_generation
        try:
            if self.controller.request_repair("repair_game_link", integration_only=True, generation=generation):
                self._set_setup_state("installing", player_text("launcher.ui", "repairing_the_game_connection"))
        except (OSError, RuntimeError, ValueError) as error:
            self._append_log('Game integration repair error: {value_0}'.format(value_0=f'{error}'))
            self._set_setup_state('game_link_update_needed', player_text('launcher.ui', 'connection_repair_failed'))

    def _manual_install_action(self) -> None:
        if self._manual_retry:
            self._prepare(force=True)
        else:
            self._open_manual_install_guide()

    def _open_manual_install_guide(self) -> None:
        """Open the Windows manual installation section in INSTALL.md."""
        local_candidates = [
            self.controller.client_dir.parent / 'docs' / 'INSTALL.md',
            self.controller.client_dir / 'docs' / 'INSTALL.md', self.controller.client_dir / 'INSTALL.md',
            self.controller.application_dir / 'docs' / 'INSTALL.md', self.controller.application_dir / 'INSTALL.md',
        ]
        opened = False
        for candidate in local_candidates:
            if candidate.is_file():
                try:
                    webbrowser.open(candidate.resolve().as_uri())
                    opened = True
                    break
                except (OSError, RuntimeError, ValueError):
                    pass
        if not opened:
            try:
                webbrowser.open(WINDOWS_MANUAL_INSTALL_GUIDE_URL)
            except (OSError, RuntimeError, ValueError) as error:
                self._append_log('Could not open browser: {value_0}'.format(value_0=f'{error}'))
        self._append_log('Opened manual installation guide (INSTALL.md → Windows manual installation).')

    def _confirm_manual_installation(self) -> None:
        if self._installation_request is not None:
            return
        self._manual_installation_generation = self.controller.operation_generation
        self._show_installation_confirmation()

    def _complete_manual_installation(self, generation):
        try:
            self.controller.confirm_manual_installation(generation=generation)
            self._append_log('Manual mod installation confirmed.')
        except (OSError, RuntimeError, ValueError) as error:
            self._append_log('Manual installation confirmation error: {value_0}'.format(value_0=f'{error}'))
            QMessageBox.warning(self, player_text("launcher.ui", "manual_installation_error"), self._operation_failure('install_mod', error))

    def _prepare(self, *, force: bool = False) -> None:
        if not self._room_connected:
            self._append_log('Connect to a room before preparing its mod.')
            return
        if self._setup_state in {"installing", "updating"}:
            self._append_log('Room mod setup is already active.')
            return
        generation = self.controller.operation_generation
        try:
            started = (
                self.controller.reinstall_setup(generation=generation)
                if force
                else self.controller.prepare_setup(generation=generation)
            )
            if not started:
                self._append_log('Setup is already active or room is unavailable.')
                return
            self._set_setup_state("updating" if self._setup_state == "update_required" else "installing")
        except (OSError, RuntimeError, ValueError) as error:
            self._append_log('Setup error: {value_0}'.format(value_0=f'{error}'))
            self._set_setup_state(self._room_package_issue_state({"code": getattr(error, "code", None)}))

    @staticmethod
    def _room_package_issue_state(event) -> str:
        return (
            "package_incompatible"
            if event.get("code")
            in {
                'unsupported_contract', 'unsupported_capabilities', 'unsupported_schema', 'unsupported_revision',
                'incompatible_bridge_protocol',
            }
            else "package_failed"
        )

    @staticmethod
    def _is_room_package_failure(event) -> bool:
        return event.get("failure_domain") in {"room_package", "installed_room_package"} or event.get('recovery_action') in {"rebuild_room_package", "update_room_package"}

    def _current_room_package_failure(self) -> bool:
        if self._setup_state in {"package_failed", "package_incompatible"}:
            return True
        failure = self.controller.last_setup_failure
        if isinstance(failure, dict):
            return self._is_room_package_failure(failure)
        return False

    def _dependency_consent_details(self, event):
        name = str(event.get("name") or player_text("launcher.app_window", "required_download"))
        version = str(event.get("version") or player_text("launcher.app_window", "version_not_provided"))
        purpose = str(event.get("purpose") or player_text("launcher.app_window", "finish_room_setup"))
        source = str(
            event.get("source")
            or event.get("url")
            or player_text("launcher.app_window", "download_source_not_provided")
        )
        details = [
            player_text('launcher.app_window', 'tool') + f'{name} {version}',
            player_text('launcher.app_window', 'used_for_value_0', value_0=f'{purpose}'),
            player_text('launcher.app_window', 'download_source_value_0', value_0=f'{source}'),
            player_text('launcher.app_window', 'the_launcher_checks_this_download_before_using_it'),
        ]
        return name, details

    def _request_dependency_consent(self, event: dict[str, object]) -> None:
        request_id = str(event.get("request_id", ""))
        if not request_id or request_id in self._resolved_consent_requests:
            self._append_log('Ignored duplicate or invalid dependency consent request.')
            return
        self._resolved_consent_requests.add(request_id)
        if self._dependency_request is not None:
            self.controller.resolve_consent(request_id, False)
            return
        name, details = self._dependency_consent_details(event)
        self._set_setup_state("updating" if self._setup_state == "update_required" else "installing")
        self._dependency_request = (request_id, name)
        try:
            title = player_text("launcher.ui", "download_value_0", value_0=f"{name}")
            self.help_view.download_details.setText("\n\n".join((title, *details)))
            self.help_view.download_prompt.show()
            self._show_page(4)
            self.help_view.ensureWidgetVisible(self.help_view.download_prompt)
        except (OSError, RuntimeError, ValueError) as error:
            self._append_log('Dependency consent dialog failed: {value_0}'.format(value_0=f'{error}'))
            self._answer_dependency_consent(False)

    def _answer_dependency_consent(self, accepted):
        request = self._dependency_request
        self._dependency_request = None
        self.help_view.download_prompt.hide()
        if request is None:
            return
        request_id, name = request
        self.controller.resolve_consent(request_id, accepted)
        if not accepted:
            self._set_setup_state(
                'failed',
                player_text('launcher.ui', 'value_0_download_declined_retry_setup_when_ready', value_0=f'{name}'),
            )

    def _copy_launch_option(self) -> None:
        value = self._play_presenter.present().launch_option
        if value:
            QApplication.clipboard().setText(value)
            if QApplication.clipboard().text() != value:
                self.copy_launch_option_button.setText(player_text("launcher.ui", "copy_failed"))
                self._append_log('Steam launch option could not be verified on clipboard.')
                return
            self._append_log('Steam launch option copied.')
            self.copy_launch_option_button.setText(player_text("launcher.ui", "copied"))
            QTimer.singleShot(1800, lambda: self.copy_launch_option_button.setText(player_text("launcher.ui", "copy")))

    def _show_setup_complete(self) -> None:
        """Confirm a new room package install without offering game launch control."""
        dialog = QDialog(self)
        dialog.setWindowTitle(player_text("launcher.ui", "setup_complete"))
        dialog.setModal(True)
        dialog.setMinimumWidth(440)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(self._label(player_text("launcher.ui", "setup_complete"), "title"))
        layout.addWidget(self._label(player_text('launcher.ui', 'play_steam_help')))
        if os.name != "nt" and self._play_presenter.model.launch_option:
            layout.addWidget(self._label(player_text("launcher.ui", "steam_launch_option"), "eyebrow"))
            option = QLineEdit(self._play_presenter.model.launch_option)
            option.setReadOnly(True)
            layout.addWidget(option)
            copy = QPushButton(player_text("launcher.ui", "copy_option"))
            copy.clicked.connect(lambda: QApplication.clipboard().setText(option.text()))
            layout.addWidget(copy, alignment=Qt.AlignmentFlag.AlignLeft)
        close = QPushButton(player_text("launcher.ui", "close"))
        close.setObjectName("primary")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        close.setFocus(Qt.FocusReason.OtherFocusReason)
        dialog.exec()

    def _build_setup_strip(self):
        self.session_setup = self._card("actionStrip")
        self.session_setup.setProperty("actionTone", "warning")
        setup_layout = QVBoxLayout(self.session_setup)
        setup_layout.setContentsMargins(0, 0, 0, 0)
        setup_layout.setSpacing(12)
        setup_copy = QHBoxLayout()
        setup_copy.setSpacing(12)
        self.session_setup_title = self._label(player_text('common', 'room_mod'), "stateName")
        self.session_setup_detail = self._label(
            player_text('launcher.ui', 'connect_to_a_room_to_check_its_mod'), 'muted',
        )
        setup_layout.addWidget(self.session_setup_title)
        self.session_setup_title.hide()
        setup_copy.addWidget(self.session_setup_detail, 1)
        self.setup_elapsed = self._label("", "muted")
        setup_copy.addWidget(self.setup_elapsed)
        setup_layout.addLayout(setup_copy)
        setup_buttons = QHBoxLayout()
        setup_buttons.setSpacing(12)
        self.session_manual_complete_action = QPushButton(player_text("launcher.ui", "i_completed_manual_installation"))
        self.session_manual_complete_action.clicked.connect(self._confirm_manual_installation)
        self.session_manual_complete_action.setVisible(False)
        self.session_manual_retry_action = QPushButton(player_text("launcher.setup", "install_again"))
        self.session_manual_retry_action.clicked.connect(lambda: self._prepare(force=True))
        self.session_manual_retry_action.setVisible(False)
        self.session_setup_action = _ScanlineButton(player_text('common', 'install_room_mod'))
        self.session_setup_action.setObjectName("primary")
        self.session_setup_action.clicked.connect(self._run_setup_action)
        self.reinstall_button = self.session_setup_action
        setup_buttons.addWidget(self.session_manual_complete_action)
        setup_buttons.addWidget(self.session_manual_retry_action)
        setup_buttons.addWidget(self.session_setup_action)
        self.session_setup_help = QPushButton(player_text('common', 'check_setup'))
        self.session_setup_help.clicked.connect(self._show_progress_help)
        self.session_setup_help.setVisible(False)
        setup_buttons.addWidget(self.session_setup_help)
        self._installation_request = None
        self.install_yes = QPushButton(player_text("launcher.setup", "confirm_yes"))
        self.install_no = QPushButton(player_text("launcher.setup", "confirm_no"))
        self.install_yes.clicked.connect(lambda: self._answer_installation(True))
        self.install_no.clicked.connect(lambda: self._answer_installation(False))
        for button in (self.install_no, self.install_yes):
            button.hide()
            setup_buttons.addWidget(button)
        setup_buttons.addStretch(1)
        setup_layout.addLayout(setup_buttons)

    def _setup_presentation(self, state):
        presentations = {
            "disconnected": (
                player_text('common', 'room_mod'), player_text('launcher.ui', 'connect_to_a_room_to_check_its_mod'), '',
                False, player_text('launcher.ui', 'ready'),
            ),
            "checking": (
                player_text('launcher.ui', 'checking_room_mod'),
                player_text('launcher.ui', 'checking_the_installed_mod'), player_text('launcher.ui', 'checking_74fde8'),
                False, player_text('common', 'connected'),
            ),
            "install_needed": (
                player_text('common', 'install_room_mod'),
                player_text('launcher.ui', 'install_this_room_s_package_before_playing'),
                player_text('common', 'install_room_mod'), True, player_text('launcher.ui', 'action_needed'),
            ),
            "update_required": (
                player_text('launcher.ui', 'update_room_package'), player_text('launcher.ui', 'room_package_required'),
                player_text('launcher.ui', 'update_room_package'), True, player_text('launcher.ui', 'action_needed'),
            ),
            "installing": (
                player_text('launcher.ui', 'installing_room_mod'),
                player_text('launcher.ui', 'preparing_this_room_for_play'), player_text('launcher.ui', 'installing'),
                False, player_text('launcher.ui', 'working'),
            ),
            "updating": (
                player_text('launcher.ui', 'updating_room_mod'),
                player_text('launcher.ui', 'preparing_this_room_for_play'),
                player_text('launcher.ui', 'updating_00b6dd'), False, player_text('launcher.ui', 'working'),
            ),
            "game_link_needed": (
                player_text('launcher.ui', 'game_integration_setup_needed'),
                player_text('launcher.ui', 'connection_setup_help'), player_text('launcher.ui', 'prepare'), True,
                player_text('launcher.ui', 'action_needed'),
            ),
            "game_link_update_needed": (
                player_text('launcher.ui', 'game_integration_needs_repair'),
                player_text('launcher.ui', 'game_integration_needs_repair_before_play'),
                player_text('launcher.ui', 'repair'), True, player_text('launcher.ui', 'action_needed'),
            ),
            "package_failed": (
                player_text('launcher.ui', 'room_mod_could_not_be_built'),
                player_text('launcher.ui', 'rebuild_room_help'), player_text('launcher.ui', 'rebuild_room_package'),
                True, player_text('launcher.ui', 'action_needed'),
            ),
            "package_incompatible": (
                player_text('launcher.ui', 'room_mod_needs_rebuilding'),
                player_text('launcher.ui', 'launcher_mismatch_help'),
                player_text('launcher.ui', 'rebuild_room_package'), True, player_text('launcher.ui', 'action_needed'),
            ),
            "manual_install_required": (
                player_text('launcher.ui', 'manual_installation_required'),
                player_text('launcher.ui', 'manual_install_needed'),
                player_text('launcher.ui', 'open_installation_guide'), True,
                player_text('launcher.ui', 'action_needed'),
            ),
            "ready": (
                player_text("launcher.ui", "setup_ready"),
                player_text("launcher.ui", "play_help")
                if os.name == "nt"
                else player_text("launcher.ui", "steam_launch_help"),
                player_text("launcher.ui", "open_session"),
                True,
                player_text("launcher.ui", "ready"),
            ),
            "failed": (
                player_text('launcher.ui', 'game_setup_failed'), player_text('launcher.ui', 'setup_incomplete_help'),
                player_text('launcher.ui', 'fix_setup'), True, player_text('launcher.ui', 'action_needed'),
            ),
            "campaign_failed": (
                player_text('launcher.ui', 'save_setup_failed'), player_text('launcher.ui', 'inspect_backups_help'),
                player_text('launcher.ui', 'ap_save_bbd136'), True, player_text('launcher.ui', 'action_needed'),
            ),
            "proton_needed": (
                player_text('launcher.ui', 'select_proton'), player_text('launcher.ui', 'proton_selection_help'),
                player_text('launcher.ui', 'select_proton'), True, player_text('launcher.ui', 'action_needed'),
            ),
        }
        return presentations[state]

    def _room_update_state(self, event, state, reason, readiness, readiness_reason):
        drift = state == "update_required"
        self.drift.setText(
            player_text("launcher.ui", "room_update")
            + player_text("launcher.app_window", "this_room_needs_its_matching_mod")
        )
        self.drift.setVisible(drift)
        setup_copy = (
            player_text("launcher.ui", "room_update_required")
            if event.get("reason_code") == "room_changed"
            else player_text("launcher.ui", "this_room_s_installed_mod_needs_an_update")
        )
        if readiness == "blocked":
            self._set_setup_state(self._room_package_issue_state(event))
        else:
            self._set_setup_state(
                "update_required" if state == "update_required" else "install_needed",
                setup_copy
                if state == "update_required"
                else player_text("launcher.ui", "install_this_room_s_package_before_playing"),
            )

    def _installed_room_state(self, event):
        readiness = str(event.get("readiness", "ready"))
        readiness_reason = str(event.get("readiness_reason", ""))
        self._clear_drift()
        self.help_view.uninstall_button.setEnabled(self._room_connected)
        if readiness == "blocked":
            self._native_health_presentation = None
            self._show_page(2)
            if event.get("failure_domain") in {"campaign_session", "proton_runtime"}:
                self._set_setup_state("failed", str(event.get("user_message") or readiness_reason))
            else:
                target_state = (
                    "game_link_update_needed" if event["meathook_status"] == "incompatible" else "game_link_needed"
                )
                self._set_setup_state(target_state, player_text('launcher.ui', 'repair_connection_help'))
        else:
            self._native_health_presentation = None
            self._show_page(2)
            self._set_setup_state("ready")

    def _show_setup_failure(self, event, message):
        operation = {"campaign_session": "saves", "room_package": "install_mod",
                     "installed_room_package": "install_mod"}.get(event.get("failure_domain"), "repair")
        message = self._operation_failure(operation, event)
        self._reconnecting = False
        self._connection_pending = False
        self._set_connection_controls(True)
        self._set_connection_badge(player_text("launcher.ui", "failed"), False)
        title = player_text("launcher.ui", "game_setup_failed")
        self._set_home(title, message, player_text("launcher.ui", "retry_join"), title)
        self.join_error.setText(message)
        self.join_error.show()
        attempt_id = int(event.get("attempt_id") or self.controller.connection_attempt_id)
        if attempt_id not in self._shown_connection_error_attempts:
            self._shown_connection_error_attempts.add(attempt_id)
            QMessageBox.warning(self, title, message)

    def _show_connection_failure(self, event: dict[str, object]) -> None:
        attempt_id = int(event.get("attempt_id") or self.controller.connection_attempt_id)
        title = str(event.get("title") or player_text("launcher.app_window", "could_not_connect_to_the_room"))
        message = str(event.get("message") or player_text("launcher.app_window", "the_archipelago_connection_failed"))
        action = str(event.get("action") or player_text("launcher.app_window", "check_the_room_details_and_try_again"))
        player_copy = (
            '{value_0}'.format(value_0=f'{message}')
            + "\n"
            + ""
            + "\n"
            + '{value_1}'.format(value_1=f'{action}')
        )
        self._reconnecting = False
        self._connection_pending = False
        self._set_connection_controls(True)
        self._set_connection_badge(player_text("launcher.ui", "failed"), False)
        self.join_error.setText(
            '{value_0}. {value_1} {value_2}'.format(value_0=f'{title}', value_1=f'{message}', value_2=f'{action}')
        )
        self.join_error.show()
        self._set_home(
            title.upper(), '{value_0} {value_1}'.format(value_0=f'{message}', value_1=f'{action}'),
            player_text('launcher.ui', 'retry_join'), player_text('launcher.ui', 'connection_failed'),
        )
        self._show_page(1)
        if attempt_id not in self._shown_connection_error_attempts:
            self._shown_connection_error_attempts.add(attempt_id)
            QMessageBox.warning(self, title, player_copy)

    def _operation_failure(self, operation, error):
        technical = record_failure(self.controller, operation, error)
        self._append_log(f"{operation}: {technical}")
        return failure_text(operation, error)

    def _show_manual_installation(self, event) -> None:
        message = str(event.get("message", player_text("launcher.setup", "action_confirm_install")))
        self._set_setup_state("manual_install_required", message)
        self._manual_retry = event.get("recovery_required", False) or os.name != "nt"
        self.session_manual_complete_action.setVisible(not self._manual_retry)
        if self._manual_retry:
            action = player_text("launcher.setup", "install_again")
            for button in (self.hero_action, self.session_setup_action, self.reinstall_button):
                button.setText(action)
        self._append_log(message)
