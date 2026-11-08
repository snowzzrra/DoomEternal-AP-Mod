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
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
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
from doom_eap.strings import PlayerError

from .controls import (
    DictionaryControl,
    NamedRangeControl,
    OptionSetControl,
    ToggleSwitch,
    _card,
    _label,
    _OptionCategory,
    _ScanlineButton,
    _scroll,
)
from .doom_style import DoomButton
from .option_grid import Description, TileGrid, stepper
from .player_options import PlayerOptions
from .strings import failure_text, option_text, record_failure
from .strings import text as player_text

QPushButton = DoomButton


class PlayerFileView(QWidget):
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
        self.dependencies = PlayerOptions(self)
        self._create_page()

    def _create_page(self) -> None:
        page = QVBoxLayout(self)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(0)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        header = self._card("hero")
        head = QVBoxLayout(header)
        head.setSpacing(12)
        head.setContentsMargins(0, 16, 0, 16)
        heading = QHBoxLayout()
        heading.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(12)
        text.addWidget(self._label(player_text('common', 'create_player'), "title"))
        description = self._label(
            player_text("launcher.ui", "player_file_help"), "muted"
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
        player = QWidget()
        player_layout = QVBoxLayout(player)
        player_layout.setSpacing(12)
        player_layout.setContentsMargins(0, 0, 0, 0)
        player_layout.addWidget(self._label(player_text("launcher.connection", "player"), "fieldLabel"))
        player_layout.setSpacing(4)
        self.player_name = QLineEdit("Player")
        self.player_name.setFixedWidth(480)
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
                    "arena_checks",
                ),
            ),
            (
                "resources",
                player_text("launcher.player_file", "resources"),
                (
                    "resource_profile",
                    "trap_percentage",
                    "enabled_traps",
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
            grid = TileGrid()
            card.contents.addWidget(grid)
            for key in options_by_key:
                if key in remaining:
                    self._add_tile(grid, options_by_key[key])
            layout.addWidget(card)
        self.enemy_option_keys = tuple(
            key
            for key in self.option_controls
            if key.startswith("enemy_") or key in ("progressive_difficulty", "easier_secret_encounters")
        )
        self._set_option_values(enemy_options_for_editing(self.option_defaults))
        self.dependencies._wire_create_dependencies()
        self.dependencies._refresh_create_dependencies()
        footer = QWidget()
        actions = QHBoxLayout(footer)
        actions.setContentsMargins(24, 12, 24, 24)
        actions.setSpacing(12)
        actions.addStretch(1)
        reset = QPushButton(player_text("launcher.ui", "reset_defaults"))
        reset.setObjectName("link")
        reset.clicked.connect(self._reset_options)
        save = _ScanlineButton(player_text("launcher.ui", "save_player_file"))
        save.setObjectName("primary")
        self.save_button = save
        save.clicked.connect(self._save_player_options)
        import_button = QPushButton(player_text("launcher.ui", "import_player_file"))
        import_button.setObjectName("playerImport")
        import_button.clicked.connect(self._import_player_options)
        actions.addWidget(import_button)
        actions.addWidget(reset)
        actions.addWidget(save)
        layout.addStretch(1)
        self.scroll = self._scroll(body)
        page.addWidget(self.scroll, 1)
        page.addWidget(footer)

    def _option_group(self, group_id, title, keys, options_by_key):
        card = _OptionCategory(title)
        self.option_categories.append(card)
        group_layout = card.contents
        grid = TileGrid()
        group_layout.addWidget(grid)
        advanced_layout = None
        if group_id == "resources":
            advanced_layout = self._advanced_grid(group_layout)
        if group_id == "starting_loadout":
            grid.add_tile(self._start_inventory_widget(), wide=True)
        displayed = self._add_group_rows(group_id, keys, options_by_key, grid, advanced_layout)
        sections = ()
        if group_id == "enemies":
            self.enemy_preview = EnemyPreview()
            grid.add_tile(self.enemy_preview, wide=True)
            category_actions = QHBoxLayout()
            category_actions.setSpacing(12)
            for title, callback in (
                (player_text("launcher.ui", "import_enemy_settings_6a54a4"), self._import_enemy_options),
                (player_text("launcher.ui", "save_enemy_settings_1fc235"), self._export_enemy_options),
            ):
                button = QPushButton(title)
                button.clicked.connect(callback)
                category_actions.addWidget(button)
            category_actions.addStretch()
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

    @staticmethod
    def _advanced_grid(layout):
        toggle = QPushButton(player_text("launcher.help", "advanced"))
        toggle.setObjectName("link")
        toggle.setProperty("compact", True)
        toggle.setCheckable(True)
        grid = TileGrid()
        grid.hide()
        toggle.toggled.connect(grid.setVisible)
        toggle.toggled.connect(lambda checked: toggle.setText(player_text('launcher.help', 'advanced_open') if checked else player_text("launcher.help", "advanced")))
        layout.addWidget(toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(grid)
        return grid

    def _add_tile(self, grid, option):
        grid.add_tile(self._option_widget(option), wide=option["ui_type"] in {"option_set", "option_dict"})

    def _add_group_rows(self, group_id, keys, options_by_key, grid, advanced_grid):
        displayed = set()
        for key in keys:
            option = options_by_key.get(key)
            if option is not None:
                main = key in {"resource_profile", "trap_percentage", "enabled_traps"}
                target = advanced_grid if group_id == "resources" and not main else grid
                self._add_tile(target, option)
                displayed.add(key)
        return displayed

    def _add_group_sections(self, sections, options_by_key, group_layout):
        displayed = set()
        for title, section_keys in sections:
            if title == player_text("launcher.player_file", "advanced"):
                grid = self._advanced_grid(group_layout)
            else:
                group_layout.addSpacing(12)
                group_layout.addWidget(self._label(title, "fieldLabel"))
                grid = TileGrid()
                group_layout.addWidget(grid)
            for key in section_keys:
                if key in options_by_key:
                    self._add_tile(grid, options_by_key[key])
                    displayed.add(key)
        return displayed

    def _effective_config_widget(self) -> QFrame:
        card = self._card("effectiveConfig")
        layout = QGridLayout(card)
        layout.setContentsMargins(0, 12, 0, 12)
        layout.setSpacing(12)
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
            cell_layout.setContentsMargins(0, 0, 12, 0)
            cell_layout.setSpacing(4)
            cell_layout.addWidget(self._label(label.upper(), "fieldLabel"))
            value = self._label('—')
            cell_layout.addWidget(value)
            self.effective_config_values[key] = value
            layout.addWidget(cell, 1 + index // 3, index % 3)
        return card

    @staticmethod
    def _format_markdown(text: object) -> str:
        escaped = html.escape(str(text), quote=False).replace("\n", "<br>")
        escaped = re.sub(r"(\*\*|__)(.+?)\1", r"<b>\2</b>", escaped)
        return re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", escaped)

    def _option_widget(self, option):
        key, default = str(option["key"]), option.get("default")
        self.option_defaults[key] = default
        row = QWidget()
        row.setObjectName("optionRow")
        layout = QGridLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        heading = QHBoxLayout()
        heading.setSpacing(8)
        name = option_text(key, "name", str(option["display_name"]))
        description = option_text(key, "description", option["description"])
        title = self._label(name)
        title.setMinimumWidth(0)
        title.setToolTip(self._format_markdown(description))
        heading.addWidget(title, 1)
        control = self._option_control(option)
        if option["ui_type"] == "toggle":
            heading.addWidget(control)
        else:
            shown = stepper(control) if isinstance(control, QSpinBox) else control
            layout.addWidget(shown, 1, 0)
        for action in getattr(control, "header_actions", ()):
            heading.addWidget(action)
        help = QPushButton('?')
        help.setObjectName("helpIcon")
        help.setFixedSize(14, 14)
        help.setToolTip(title.toolTip())
        help.setAccessibleName(name)
        heading.addWidget(help)
        layout.addLayout(heading, 0, 0)
        layout.addWidget(Description(description), 3, 0)
        self.option_controls[key] = control
        self.option_rows[key] = row
        return row

    @staticmethod
    def _option_control(option):
        key, default, kind = str(option["key"]), option.get("default"), option["ui_type"]
        if kind == "toggle":
            control = ToggleSwitch()
            control.setFixedSize(36, 18)
            control.setAccessibleName(option_text(key, "name", str(option["display_name"])))
            control.setChecked(bool(default))
        elif kind == "choice":
            control = QComboBox()
            control.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            control.setMinimumContentsLength(1)
            for choice in option["choices"]:
                if key == "enemy_count_mode" and choice["key"] == "preset":
                    continue
                control.addItem(option_text(key, "choice", str(choice.get("label", choice["key"])),
                                            choice_key=choice["key"]), choice["key"])
            control.setCurrentIndex(max(0, control.findData(default)))
        elif kind == "range":
            control = QSpinBox()
            control.setRange(option["minimum"], option["maximum"])
            control.setValue(default)
        elif kind == "option_set":
            control = OptionSetControl(option)
        elif kind == "option_dict":
            control = DictionaryControl(option)
        else:
            control = NamedRangeControl(option)
        return control

    def _start_inventory_widget(self) -> QWidget:
        card = QWidget()
        layout = QVBoxLayout(card)
        layout.setSpacing(8)
        layout.setContentsMargins(0, 0, 0, 0)
        heading = QHBoxLayout()
        heading.addWidget(self._label(player_text("launcher.ui", "starting_items")), 1)
        layout.addLayout(heading)
        layout.addWidget(
            self._label(
                player_text("launcher.ui", "starting_inventory_help"), "muted"
            )
        )
        try:
            self.start_inventory_catalog = load_start_inventory_catalog(
                self.controller.client_dir / "data" / "start_inventory_catalog.json"
            )
        except ValueError as error:
            self.start_inventory_catalog = []
            self._append_log(
                'Starting Inventory choices could not load: {value_0}. Check Help for details.'.format(value_0=f'{error}')
            )
        controls = QHBoxLayout()
        controls.setSpacing(12)
        self.inventory_picker = QComboBox()
        self.inventory_picker.setMinimumContentsLength(24)
        self.inventory_quantity = QSpinBox()
        self.inventory_quantity.setRange(1, 9999)
        self.inventory_add_button = QPushButton(player_text("launcher.player_file", "add_extra"))
        self.inventory_add_button.setObjectName("secondary")
        self.inventory_add_button.clicked.connect(self._add_inventory)
        self.inventory_pool_add_button = QPushButton(player_text("launcher.player_file", "add_from_pool"))
        self.inventory_pool_add_button.setObjectName("secondary")
        self.inventory_pool_add_button.clicked.connect(lambda: self._add_inventory(from_pool=True))
        enabled = bool(self.start_inventory_catalog)
        self.inventory_picker.setEnabled(enabled)
        self.inventory_quantity.setEnabled(enabled)
        self.inventory_add_button.setEnabled(enabled)
        self.inventory_pool_add_button.setEnabled(enabled)
        controls.addWidget(self.inventory_picker, 1)
        controls.addWidget(stepper(self.inventory_quantity, compact=True))
        controls.addWidget(self.inventory_add_button)
        controls.addWidget(self.inventory_pool_add_button)
        layout.addLayout(controls)
        entries = [{"key": item["name"], "label": item["label"], "minimum": 1, "maximum": 9999} for item in self.start_inventory_catalog]
        self.inventory = DictionaryControl({"entries": entries, "default": {}}, selected_only=True)
        self.inventory_pool = DictionaryControl({"entries": entries, "default": {}}, selected_only=True)
        inventories = QHBoxLayout()
        for key, control in (("extra_starting_items", self.inventory), ("starting_items_from_pool", self.inventory_pool)):
            column = QVBoxLayout()
            title = QHBoxLayout()
            title.addWidget(self._label(player_text("launcher.player_file", key)), 1)
            for action in control.header_actions:
                title.addWidget(action)
            column.addLayout(title)
            column.addWidget(control)
            inventories.addLayout(column, 1)
        layout.addLayout(inventories)
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
        self.inventory_pool_add_button.setEnabled(enabled)

    def _add_inventory(self, *, from_pool=False):
        name = self.inventory_picker.currentData()
        if isinstance(name, str):
            control = self.inventory_pool if from_pool else self.inventory
            values = control.value()
            values[name] = values.get(name, 0) + self.inventory_quantity.value()
            control.setValue(values)

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
        values["start_inventory"] = self.inventory.value()
        values["start_inventory_from_pool"] = self.inventory_pool.value()
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
        finally:
            self._syncing_create_dependencies = syncing

    def _apply_player_values(self, values: dict[str, object]) -> None:
        self._set_option_values({**values, **enemy_options_for_editing(values)})
        self.inventory.setValue({})
        self.inventory_pool.setValue({})
        self.dependencies._refresh_create_dependencies()
        self.inventory.setValue(cast(dict[str, int], values.get("start_inventory", {})))
        self.inventory_pool.setValue(cast(dict[str, int], values.get("start_inventory_from_pool", {})))

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
            QMessageBox.critical(self, player_text("launcher.ui", "could_not_import_enemy_settings"), self._file_failure(error))

    def _export_enemy_options(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self,
            player_text("launcher.ui", "save_enemy_settings"),
            'Enemies.yaml',
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
            QMessageBox.critical(self, player_text("launcher.ui", "could_not_save_enemy_settings"), self._file_failure(error))

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
            unknown = (set(values["start_inventory"]) | set(values["start_inventory_from_pool"])) - known_items
            if unknown:
                raise PlayerError('launcher.player_file', 'invalid_starting_items', value_0=f"{', '.join(sorted(unknown))}")
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, player_text("launcher.ui", "could_not_import_player_file"), self._file_failure(error))
            return
        self._apply_player_values(values)
        self.player_name.setText(name)
        self._imported_player = document
        self._activity_event({"type": "player_yaml_imported", "path": filename})

    def _save_player_options(self) -> None:
        try:
            suggested = suggested_yaml_filename(self.player_name.text())
        except ValueError as error:
            QMessageBox.critical(self, player_text("launcher.ui", "could_not_save_player_file"), self._file_failure(error))
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
            inventory = self.inventory.value().keys() | self.inventory_pool.value().keys()
            unavailable = set(inventory) - {
                str(self.inventory_picker.itemData(index)) for index in range(self.inventory_picker.count())
            }
            if unavailable:
                raise PlayerError("launcher.player_file", "starting_items_unavailable",
                                  items=", ".join(sorted(unavailable)))
            saved = self.controller.save_player_options(
                Path(filename), self.player_name.text(), self._option_values(), imported=self._imported_player
            )
        except (OSError, RuntimeError, ValueError) as error:
            self._append_log('Player YAML save error: {value_0}'.format(value_0=f'{error}'))
            QMessageBox.critical(self, player_text("launcher.ui", "could_not_save_player_file"), self._file_failure(error))
            return
        self._activity_event({"type": "player_yaml_saved", "path": str(saved)})
        QMessageBox.information(
            self,
            player_text("launcher.ui", "player_file_saved"),
            player_text("launcher.ui", "saved_to") + "\n" + '{value_0}'.format(value_0=f'{saved}'),
        )

    def _file_failure(self, error):
        self.controller.emit("log", message=record_failure(self.controller, "player_options", error))
        return failure_text("player_options", error)

    def _option_label(self, key: object) -> str:
        name = str(key)
        schema = getattr(self.controller, "options_schema", None)
        if isinstance(schema, dict):
            for option in schema.get("options", []) or []:
                if isinstance(option, dict) and option.get("key") == name:
                    label = str(option.get("display_name") or "").strip()
                    if label:
                        return option_text(name, "name", label)
        return name.replace("_", " ").title()

    def _option_value_text(self, value: object) -> str:
        if isinstance(value, bool):
            return "On" if value else player_text("launcher.player_file", "off")
        return str(value)


class EnemyPreview(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        heading.addWidget(_label(player_text("launcher.enemy_preview", "title"), "fieldLabel"))
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
            cell.addWidget(_label(player_text("common", key) if key in {"density", "variety"} else player_text("launcher.enemy_preview", key), "muted"))
            value = _label("")
            cell.addWidget(value)
            self.values[key] = value
            grid.addLayout(cell, index // 3, index % 3)
            grid.setColumnStretch(index % 3, 1)
        body.addLayout(grid)
        for key in ("mix", "speed"):
            body.addWidget(_label(player_text("common", key) if key in {"density", "variety"} else player_text("launcher.enemy_preview", key), "muted"))
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
        self.mode.setVisible(bool(values["mode"]))
        self.mode.setText(values["mode"])
        for key, label in self.values.items():
            label.setText(values[key])

    def set_error(self, message):
        self.content.hide()
        self.mode.hide()
        self.error.setText(message)
        self.error.show()
