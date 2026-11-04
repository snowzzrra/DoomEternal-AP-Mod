"""Player YAML editor and schema options; no room or installation orchestration."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import cast

import yaml
from PySide6.QtCore import (
    QIODevice,
    QSaveFile,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from doom_eap.content.enemy_options import enemy_options_for_editing
from doom_eap.content.enemy_species import speed_capability_notice
from doom_eap.content.options_foundation import (
    GAME_NAME,
    load_player_yaml,
    load_start_inventory_catalog,
    player_yaml_document,
    suggested_yaml_filename,
)

from .controls import (
    DictionaryControl,
    NamedRangeControl,
    OptionSetControl,
    _card,
    _label,
    _OptionCategory,
    _ScanlineButton,
    _scroll,
)
from .player_options import PlayerOptions
from .strings import text as player_text


class PlayerFileView(QScrollArea):
    activity = Signal(dict)
    diagnostic = Signal(str)
    _label = staticmethod(_label)
    _card = staticmethod(_card)
    _scroll = staticmethod(_scroll)

    def _activity_event(self, event):
        self.activity.emit(event)

    def _append_log(self, message):
        self.diagnostic.emit(message)

    def __init__(self, controller, colors, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.COLORS = colors
        self.option_controls = {}
        self.option_defaults = {}
        self.option_rows = {}
        self.start_inventory_catalog = []
        self._syncing_create_dependencies = False
        self._warning_state = {}
        self.dependencies = PlayerOptions(self)
        self._create_page()

    def _create_page(self) -> QScrollArea:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(28, 24, 28, 30)
        layout.setSpacing(14)
        header = self._card("hero")
        head = QVBoxLayout(header)
        head.setContentsMargins(24, 20, 24, 20)
        heading = QHBoxLayout()
        text = QVBoxLayout()
        text.addWidget(self._label(player_text("launcher.ui", "create_player_yaml"), "title"))
        description = self._label(
            player_text("launcher.ui", "choose_your_settings_and_starting_items_then_save_your_player_fi"), "muted"
        )
        description.setWordWrap(True)
        text.addWidget(description)
        heading.addLayout(text, 1)
        self.difficulty_skull = QLabel()
        self.difficulty_skull.setFixedSize(64, 64)
        self.difficulty_skull.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.addWidget(self.difficulty_skull)
        head.addLayout(heading)
        layout.addWidget(header)
        player = self._card()
        player_layout = QVBoxLayout(player)
        player_layout.setContentsMargins(20, 18, 20, 20)
        player_layout.addWidget(self._label(player_text("launcher.connection", "player"), "section"))
        self.player_name = QLineEdit(player_text("launcher.app_window", "player"))
        self.player_name.setPlaceholderText(player_text("launcher.connection", "player_example"))
        player_layout.addWidget(self.player_name)
        self._imported_player = None
        layout.addWidget(player)
        layout.addWidget(self._effective_config_widget())
        options_by_key = {
            str(option["key"]): option
            for option in cast(list[dict[str, object]], self.controller.options_schema["options"])
        }
        groups = (
            (
                "goal_campaign",
                player_text("launcher.player_file", "goal_campaign"),
                (
                    "mission_pool",
                    "custom_missions",
                    "custom_dark_lord",
                    "mission_order",
                    "mission_count",
                    "starting_missions",
                    "full_saga_final_boss",
                    "goal",
                    "goal_mission_as_item",
                    "use_dlc_content",
                    "additional_victory_requirements",
                ),
            ),
            (
                "starting_loadout",
                player_text("launcher.player_file", "starting_loadout"),
                ("starting_weapon", "special_weapon", "enhanced_melee_damage"),
            ),
            (
                "randomization",
                player_text("launcher.player_file", "randomization"),
                (
                    "randomize_chainsaw",
                    "randomize_dash",
                    "include_weapon_mastery_challenges",
                    "mastery_requirements",
                    "include_slayer_gates",
                    "include_secret_encounters",
                ),
            ),
            (
                "resources",
                player_text("launcher.player_file", "resources"),
                (
                    "resource_profile",
                    "praetor_suit_upgrades_in_pool",
                    "item_pool_counts",
                    "filler_weights",
                    "trap_weights",
                ),
            ),
            (
                "enemies",
                player_text("launcher.player_file", "enemies"),
                (
                    "enemy_randomizer",
                    "enemy_preset",
                    "enemy_intensity_early",
                    "enemy_intensity_late",
                    "enemy_length",
                    "enemy_density",
                    "enemy_variety",
                    "progressive_difficulty",
                    "easier_secret_encounters",
                ),
            ),
            (
                "killsanity",
                player_text("launcher.player_file", "killsanity"),
                ("killsanity", "kills_per_check", "killsanity_maximum_milestones"),
            ),
            (
                "combat_qol",
                player_text("launcher.player_file", "combat_qol"),
                (
                    "campaign_difficulty",
                    "logic_difficulty",
                    "reveal_ap_locations_on_automap",
                    "trap_percentage",
                    "enabled_traps",
                ),
            ),
            (
                "multiworld",
                player_text("launcher.player_file", "multiworld"),
                (
                    "progression_balancing",
                    "accessibility",
                    "local_items",
                    "non_local_items",
                    "death_link",
                    "death_link_mode",
                ),
            ),
        )
        self.option_categories = []
        displayed = set()
        for group_id, title, keys in groups:
            card, keys_shown = self._option_group(group_id, title, keys, options_by_key)
            displayed.update(keys_shown)
            layout.addWidget(card)
        remaining = set(options_by_key) - displayed
        if remaining:
            card = _OptionCategory(player_text("launcher.player_file", "other_settings"))
            self.option_categories.append(card)
            for key in options_by_key:
                if key in remaining:
                    card.contents.addWidget(self._option_widget(options_by_key[key]))
            layout.addWidget(card)
        self.enemy_option_keys = tuple(
            key
            for key in self.option_controls
            if key.startswith("enemy_") or key in ("progressive_difficulty", "easier_secret_encounters")
        )
        self._set_option_values(enemy_options_for_editing(self.option_defaults))
        self.dependencies._wire_create_dependencies()
        self.dependencies._refresh_create_dependencies()
        actions = QHBoxLayout()
        reset = QPushButton(player_text("launcher.ui", "reset_defaults"))
        reset.clicked.connect(self._reset_options)
        save = _ScanlineButton(player_text("launcher.ui", "save_player_file"))
        save.setObjectName("primary")
        self.save_button = save
        save.clicked.connect(self._save_player_options)
        actions.addWidget(reset)
        import_button = QPushButton(player_text("launcher.ui", "import_player_file"))
        import_button.setObjectName("playerImport")
        import_button.clicked.connect(self._import_player_options)
        actions.addWidget(import_button)
        actions.addStretch(1)
        actions.addWidget(save)
        layout.addLayout(actions)
        layout.addStretch(1)
        self.setWidgetResizable(True)
        self.setWidget(body)
        self.setFrameShape(QFrame.Shape.NoFrame)

    def _option_group(self, group_id, title, keys, options_by_key):
        card = _OptionCategory(title)
        self.option_categories.append(card)
        group_layout = card.contents
        advanced_layout = None
        if group_id == "resources":
            advanced = QWidget()
            advanced_layout = QVBoxLayout(advanced)
            advanced_layout.setContentsMargins(0, 0, 0, 0)
            advanced.hide()
            expand = QPushButton(player_text("launcher.ui", "advanced_resources"))
            expand.setCheckable(True)
            expand.toggled.connect(advanced.setVisible)
            group_layout.addWidget(expand)
            group_layout.addWidget(advanced)
        if group_id == "starting_loadout":
            group_layout.addWidget(self._start_inventory_widget())
        displayed = self._add_group_rows(group_id, keys, options_by_key, group_layout, advanced_layout)
        sections = ()
        if group_id == "enemies":
            self.enemy_preview = self._label("", "muted")
            self.enemy_preview.setWordWrap(True)
            group_layout.addWidget(self.enemy_preview)
            category_actions = QHBoxLayout()
            for title, callback in (
                (player_text("launcher.ui", "import_enemy_settings_6a54a4"), self._import_enemy_options),
                (player_text("launcher.ui", "save_enemy_settings_1fc235"), self._export_enemy_options),
            ):
                button = QPushButton(title)
                button.clicked.connect(callback)
                category_actions.addWidget(button)
            group_layout.addLayout(category_actions)
            sections = (
                (player_text("launcher.player_file", "bestiary"), ("enemy_weights",)),
                (player_text("launcher.player_file", "attributes"), ("enemy_health", "enemy_damage")),
                (player_text("launcher.player_file", "special_enemy_limits"), ("enemy_special_limits",)),
                (
                    player_text("launcher.player_file", "advanced"),
                    (
                        "enemy_count_mode",
                        "enemy_count_multiplier",
                        "enemy_speed",
                        "enemy_size",
                        "enemy_size_variation_chance",
                    ),
                ),
            )
        displayed.update(self._add_group_sections(sections, options_by_key, group_layout))
        return card, displayed

    def _add_group_rows(self, group_id, keys, options_by_key, group_layout, advanced_layout):
        displayed = set()
        for key in keys:
            option = options_by_key.get(key)
            if option is not None:
                target = advanced_layout if group_id == "resources" and key != "resource_profile" else group_layout
                widget = self._option_widget(option)
                if group_id == "resources" and key == "resource_profile":
                    target.insertWidget(0, widget)
                else:
                    target.addWidget(widget)
                displayed.add(key)
        return displayed

    def _add_group_sections(self, sections, options_by_key, group_layout):
        displayed = set()
        for title, section_keys in sections:
            section = _OptionCategory(title, expanded=False)
            group_layout.addWidget(section)
            for key in section_keys:
                if key in options_by_key:
                    section.contents.addWidget(self._option_widget(options_by_key[key]))
                    displayed.add(key)
        return displayed

    def _effective_config_widget(self) -> QFrame:
        card = self._card("effectiveConfig")
        layout = QGridLayout(card)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(3)
        layout.addWidget(self._label(player_text("launcher.ui", "selected_settings"), "eyebrow"), 0, 0, 1, 3)
        self.effective_config_values: dict[str, QLabel] = {}
        cells = (
            ("goal", player_text("launcher.player_file_view", "goal")),
            ("campaign", player_text("launcher.player_file_view", "campaign")),
            ("logic", player_text("launcher.player_file", "logic_difficulty")),
            ("special", player_text("launcher.player_file", "special_weapon")),
            ("starting", player_text("launcher.player_file", "starting_weapon")),
            ("victory", player_text("launcher.player_file_view", "victory")),
        )
        for index, (key, label) in enumerate(cells):
            cell = QWidget()
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 5, 10, 5)
            cell_layout.setSpacing(2)
            cell_layout.addWidget(self._label(label, "stateName"))
            value = self._label(player_text("launcher.ui", "symbol_bda050"))
            value.setStyleSheet("font-weight:800;")
            cell_layout.addWidget(value)
            self.effective_config_values[key] = value
            layout.addWidget(cell, 1 + index // 3, index % 3)
        return card

    @staticmethod
    def _format_markdown(text: object) -> str:
        escaped = html.escape(str(text), quote=False).replace("\n", "<br>")
        escaped = re.sub(r"(\*\*|__)(.+?)\1", r"<b>\2</b>", escaped)
        return re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", escaped)

    @staticmethod
    def _choice_purpose(key: str, fallback: object) -> str:
        return {
            "goal": player_text("launcher.player_file", "choose_your_victory_goal"),
            "special_weapon": player_text(
                "launcher.player_file", "choose_how_the_crucible_and_sentinel_hammer_are_randomized"
            ),
        }.get(key, str(fallback).split("\n\n", 1)[0])

    @staticmethod
    def _choice_description(key: str, value: object, label: str) -> str:
        descriptions = {
            ("goal", "acquire_the_unmaykr"): player_text(
                "launcher.player_file", "claim_the_unmaykr_in_the_fortress_of_doom"
            ),
            ("goal", "kill_the_icon_of_sin"): player_text(
                "launcher.player_file", "defeat_the_icon_of_sin_at_the_end_of_the_base_campaign"
            ),
            ("goal", "kill_the_dark_lord"): player_text(
                "launcher.player_file", "defeat_the_dark_lord_in_the_ancient_gods_part_two"
            ),
            (
                "goal",
                "complete_the_full_saga",
            ): player_text("launcher.player_file", "finish_every_enabled_full_saga_mission_claim_the_unmaykr_defeat"),
            (
                "special_weapon",
                "progressive_special_weapon",
            ): player_text("launcher.player_file", "crucible_sentinel_hammer_fully_upgraded_sentinel_hammer"),
            ("special_weapon", "progressive_sentinel_hammer"): player_text(
                "launcher.player_file", "sentinel_hammer_fully_upgraded_sentinel_hammer"
            ),
            ("special_weapon", "the_crucible"): player_text(
                "launcher.player_file", "only_the_crucible_is_included_in_the_special_weapon_item_sequenc"
            ),
        }
        return descriptions.get(
            (key, str(value)), player_text("launcher.player_file", "current_selection_value_0", value_0=f"{label}")
        )

    def _option_widget(self, option: dict[str, object]) -> QWidget:
        key, default = str(option["key"]), option.get("default")
        self.option_defaults[key] = default
        row = self._card()
        layout = QGridLayout(row)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setColumnStretch(0, 1)
        title = self._label(str(option["display_name"]))
        title.setStyleSheet("font-weight:800;")
        layout.addWidget(title, 0, 0)
        kind = str(option["ui_type"])
        description = option["description"]
        layout.addWidget(self._label(self._format_markdown(description), "muted", rich=True), 1, 0)
        if kind == "toggle":
            control: QWidget = QCheckBox(player_text("launcher.ui", "enabled"))
            control.setChecked(bool(default))
        elif kind == "choice":
            control = QComboBox()
            for choice in cast(list[dict[str, object]], option["choices"]):
                if key == "enemy_count_mode" and choice["key"] == "preset":
                    continue
                control.addItem(str(choice.get("label", choice["key"])), choice["key"])
            control.setCurrentIndex(max(0, control.findData(default)))
        elif kind == "range":
            control = QSpinBox()
            control.setRange(cast(int, option["minimum"]), cast(int, option["maximum"]))
            control.setValue(cast(int, default))
        elif kind == "option_set":
            control = OptionSetControl(option)
        elif kind == "option_dict":
            control = DictionaryControl(option)
        else:
            control = NamedRangeControl(option)
        control.setMinimumWidth(200)
        self.option_controls[key] = control
        self.option_rows[key] = row
        if kind == "option_dict":
            layout.addWidget(control, 2, 0, 1, 2)
        else:
            layout.addWidget(control, 0, 1, 2, 1, Qt.AlignmentFlag.AlignVCenter)
        return row

    def _start_inventory_widget(self) -> QWidget:
        card = self._card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.addWidget(self._label(player_text("launcher.ui", "starting_items"), "section"))
        layout.addWidget(
            self._label(
                player_text("launcher.ui", "choose_the_items_and_quantities_saved_in_your_player_file"), "muted"
            )
        )
        try:
            self.start_inventory_catalog = load_start_inventory_catalog(
                self.controller.client_dir / "data" / "start_inventory_catalog.json"
            )
        except ValueError as error:
            self.start_inventory_catalog = []
            self._append_log(
                player_text(
                    "launcher.player_file",
                    "starting_inventory_choices_could_not_load_value_0_check_help_for",
                    value_0=f"{error}",
                )
            )
        controls = QHBoxLayout()
        self.inventory_picker = QComboBox()
        self.inventory_picker.setMinimumContentsLength(24)
        self.inventory_quantity = QSpinBox()
        self.inventory_quantity.setRange(1, 9999)
        self.inventory_add_button = QPushButton(player_text("launcher.ui", "add"))
        self.inventory_add_button.setObjectName("primary")
        self.inventory_add_button.clicked.connect(self._add_inventory)
        enabled = bool(self.start_inventory_catalog)
        self.inventory_picker.setEnabled(enabled)
        self.inventory_quantity.setEnabled(enabled)
        self.inventory_add_button.setEnabled(enabled)
        controls.addWidget(self.inventory_picker, 1)
        controls.addWidget(self.inventory_quantity)
        controls.addWidget(self.inventory_add_button)
        layout.addLayout(controls)
        self.inventory = QTableWidget(0, 2)
        self.inventory.setHorizontalHeaderLabels(
            [player_text("launcher.items", "item"), player_text("launcher.ui", "count")]
        )
        self.inventory.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.inventory.verticalHeader().hide()
        self.inventory.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.inventory.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.inventory.setMinimumHeight(150)
        layout.addWidget(self.inventory)
        remove = QPushButton(player_text("launcher.ui", "remove_selected"))
        remove.clicked.connect(
            lambda: self.inventory.removeRow(self.inventory.currentRow()) if self.inventory.currentRow() >= 0 else None
        )
        layout.addWidget(remove, alignment=Qt.AlignmentFlag.AlignRight)
        return card

    def _refresh_inventory_picker(self, dlc_enabled: bool) -> None:
        selected_special = "the_crucible" if not dlc_enabled else str(self.dependencies._choice_value("special_weapon"))
        special_items = {
            "progressive_special_weapon": {"Progressive Special Weapon"},
            "progressive_sentinel_hammer": {"Progressive Sentinel Hammer"},
            "the_crucible": {"The Crucible"},
        }
        allowed_special = special_items.get(selected_special, {"The Crucible"})
        unavailable = {"Berserk", "Overdrive", "Onslaught"}
        if not dlc_enabled:
            unavailable.update({"Break Blast", "Desperate Punch", "Take Back"})
        allowed = [
            item
            for item in self.start_inventory_catalog
            if item["name"] not in unavailable
            and (
                item["name"] not in {"Progressive Special Weapon", "Progressive Sentinel Hammer", "The Crucible"}
                or item["name"] in allowed_special
            )
        ]
        current = self.inventory_picker.currentData()
        self.inventory_picker.blockSignals(True)
        self.inventory_picker.clear()
        for item in allowed:
            self.inventory_picker.addItem(item["label"], item["name"])
        self.inventory_picker.setCurrentIndex(max(0, self.inventory_picker.findData(current)))
        self.inventory_picker.blockSignals(False)
        enabled = bool(allowed)
        self.inventory_picker.setEnabled(enabled)
        self.inventory_quantity.setEnabled(enabled)
        self.inventory_add_button.setEnabled(enabled)

    def _add_inventory(self) -> None:
        name = self.inventory_picker.currentData()
        if not isinstance(name, str):
            return
        for row in range(self.inventory.rowCount()):
            if self.inventory.item(row, 0).data(Qt.ItemDataRole.UserRole) == name:
                amount = int(self.inventory.item(row, 1).text()) + self.inventory_quantity.value()
                self.inventory.setItem(row, 1, QTableWidgetItem(str(amount)))
                return
        row = self.inventory.rowCount()
        self.inventory.insertRow(row)
        item = QTableWidgetItem(name)
        item.setData(Qt.ItemDataRole.UserRole, name)
        self.inventory.setItem(row, 0, item)
        self.inventory.setItem(row, 1, QTableWidgetItem(str(self.inventory_quantity.value())))

    def _option_values(self) -> dict[str, object]:
        values: dict[str, object] = {}
        for key, control in self.option_controls.items():
            if isinstance(control, QCheckBox):
                values[key] = control.isChecked()
            elif isinstance(control, QComboBox):
                values[key] = control.currentData()
            elif isinstance(control, QSpinBox):
                values[key] = control.value()
            elif isinstance(control, NamedRangeControl | OptionSetControl | DictionaryControl):
                values[key] = control.value()
        inventory: dict[str, int] = {}
        for row in range(self.inventory.rowCount()):
            item, amount = self.inventory.item(row, 0), self.inventory.item(row, 1)
            if item is not None and amount is not None:
                inventory[str(item.data(Qt.ItemDataRole.UserRole))] = int(amount.text())
        values["start_inventory"] = inventory
        return values

    def _reset_options(self) -> None:
        self._imported_player = None
        self._apply_player_values(self.option_defaults)

    def _set_option_values(self, values: dict[str, object]) -> None:
        syncing = self._syncing_create_dependencies
        self._syncing_create_dependencies = True
        try:
            for key, value in values.items():
                if key not in self.option_controls:
                    continue
                control = self.option_controls[key]
                blocked = control.blockSignals(True)
                try:
                    if isinstance(control, QCheckBox):
                        control.setChecked(bool(value))
                    elif isinstance(control, QComboBox):
                        control.setCurrentIndex(max(0, control.findData(value)))
                    elif isinstance(control, QSpinBox):
                        control.setValue(cast(int, value))
                    elif isinstance(control, NamedRangeControl | OptionSetControl | DictionaryControl):
                        control.setValue(value)
                finally:
                    control.blockSignals(blocked)
                if key in self._warning_state:
                    self._warning_state[key] = bool(value)
        finally:
            self._syncing_create_dependencies = syncing

    def _apply_player_values(self, values: dict[str, object]) -> None:
        self._set_option_values({**values, **enemy_options_for_editing(values)})
        self.inventory.setRowCount(0)
        self.dependencies._refresh_create_dependencies()
        for name, quantity in cast(dict[str, int], values.get("start_inventory", {})).items():
            row = self.inventory.rowCount()
            self.inventory.insertRow(row)
            item = QTableWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.inventory.setItem(row, 0, item)
            self.inventory.setItem(row, 1, QTableWidgetItem(str(quantity)))

    def _apply_enemy_category_values(self, values: dict[str, object]) -> None:
        category = enemy_options_for_editing(values)
        category["easier_secret_encounters"] = values["easier_secret_encounters"]
        self._set_option_values(category)
        self.dependencies._refresh_create_dependencies()

    def _import_enemy_options(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            player_text("launcher.ui", "import_enemy_settings"),
            "",
            player_text("launcher.player_file", "yaml_files_yaml_yml"),
        )
        if not filename:
            return
        try:
            _name, values, _document = load_player_yaml(Path(filename), self.controller.options_schema)
            self._apply_enemy_category_values(values)
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, player_text("launcher.ui", "could_not_import_enemy_settings"), str(error))

    def _export_enemy_options(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self,
            player_text("launcher.ui", "save_enemy_settings"),
            player_text("launcher.ui", "enemies_yaml"),
            player_text("launcher.player_file", "yaml_files_yaml"),
        )
        if not filename:
            return
        try:
            document = player_yaml_document(
                self.controller.options_schema, self.player_name.text(), self._option_values()
            )
            document[GAME_NAME] = {key: document[GAME_NAME][key] for key in self.enemy_option_keys}
            path = Path(filename)
            if path.suffix.lower() not in {".yaml", ".yml"}:
                path = path.with_suffix(".yaml")
            payload = (
                "# " + speed_capability_notice() + "\n" + yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
            ).encode("utf-8")
            destination = QSaveFile(str(path))
            if not destination.open(QIODevice.OpenModeFlag.WriteOnly):
                raise OSError(destination.errorString())
            if destination.write(payload) != len(payload) or not destination.commit():
                raise OSError(destination.errorString())
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, player_text("launcher.ui", "could_not_save_enemy_settings"), str(error))

    def _import_player_options(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            player_text("launcher.ui", "import_player_file_af1ea0"),
            "",
            player_text("launcher.player_file", "yaml_files_yaml_yml"),
        )
        if not filename:
            return
        try:
            name, values, document = load_player_yaml(Path(filename), self.controller.options_schema)
            known_items = {item["name"] for item in self.start_inventory_catalog}
            unknown = set(values["start_inventory"]) - known_items
            if unknown:
                raise ValueError(
                    player_text(
                        "launcher.player_file",
                        "these_starting_items_are_unavailable_remove_them_and_try_again_v",
                        value_0=f"{', '.join(sorted(unknown))}",
                    )
                )
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, player_text("launcher.ui", "could_not_import_player_file"), str(error))
            return
        self._apply_player_values(values)
        self.player_name.setText(name)
        self._imported_player = document
        self._activity_event({"type": "player_yaml_imported", "path": filename})

    def _save_player_options(self) -> None:
        try:
            suggested = suggested_yaml_filename(self.player_name.text())
        except ValueError as error:
            QMessageBox.critical(self, player_text("launcher.ui", "could_not_save_player_file"), str(error))
            return
        filename, _ = QFileDialog.getSaveFileName(
            self,
            player_text("launcher.ui", "save_player_file_5b2a83"),
            suggested,
            player_text("launcher.player_file", "yaml_files_yaml_all_files"),
        )
        if not filename:
            return
        try:
            inventory = self._option_values()["start_inventory"]
            unavailable = set(inventory) - {
                str(self.inventory_picker.itemData(index)) for index in range(self.inventory_picker.count())
            }
            if unavailable:
                raise ValueError(
                    player_text("launcher.player_file", "remove_these_unavailable_starting_items_before_saving")
                    + ", ".join(sorted(unavailable))
                )
            saved = self.controller.save_player_options(
                Path(filename), self.player_name.text(), self._option_values(), imported=self._imported_player
            )
        except Exception as error:
            self._append_log(player_text("launcher.player_file", "player_yaml_save_error_value_0", value_0=f"{error}"))
            QMessageBox.critical(self, player_text("launcher.ui", "could_not_save_player_file"), str(error))
            return
        self._activity_event({"type": "player_yaml_saved", "path": str(saved)})
        QMessageBox.information(
            self,
            player_text("launcher.ui", "player_file_saved"),
            player_text("launcher.ui", "saved_to") + "\n" + player_text("launcher.ui", "value_0", value_0=f"{saved}"),
        )

    def _option_label(self, key: object) -> str:
        name = str(key)
        schema = getattr(self.controller, "options_schema", None)
        if isinstance(schema, dict):
            for option in schema.get("options", []) or []:
                if isinstance(option, dict) and option.get("key") == name:
                    label = str(option.get("display_name") or "").strip()
                    if label:
                        return label
        return name.replace("_", " ").title()

    def _option_value_text(self, value: object) -> str:
        if isinstance(value, bool):
            return "On" if value else player_text("launcher.player_file", "off")
        return str(value)
