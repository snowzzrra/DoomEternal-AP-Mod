"""Schema option dependencies and previews; no YAML or installation writes."""

from __future__ import annotations

from typing import cast

from PySide6.QtCore import (
    Qt,
)
from PySide6.QtGui import (
    QPixmap,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
)

from doom_eap.content.enemy_options import enemy_options_for_editing, normalize_enemy_options
from doom_eap.content.enemy_species import enemy_speed_capabilities

from .controls import (
    OptionSetControl,
    _label,
)
from .strings import text as player_text


class PlayerOptions:
    def __init__(self, view):
        self.view = view

    def _wire_create_dependencies(self):
        self._wire_common()
        self._wire_warnings()
        self._wire_enemy()

    def _wire_common(self):
        for key in (
            "use_dlc_content",
            "include_dlc_missions",
            "enhanced_melee_damage",
            "include_weapon_mastery_challenges",
            "include_slayer_gates",
            "include_secret_encounters",
        ):
            control = self.view.option_controls.get(key)
            if isinstance(control, QCheckBox):
                cast(QCheckBox, control).toggled.connect(self._refresh_create_dependencies)
        for key in (
            "goal",
            "special_weapon",
            "starting_weapon",
            "logic_difficulty",
            "mission_pool",
            "mission_order",
            "full_saga_final_boss",
            "campaign_difficulty",
        ):
            control = self.view.option_controls.get(key)
            if isinstance(control, QComboBox):
                cast(QComboBox, control).currentIndexChanged.connect(self._refresh_create_dependencies)

    def _wire_warnings(self):
        self.randomization_warnings = {}
        requirements = self.view.option_controls.get("additional_victory_requirements")
        if isinstance(requirements, OptionSetControl):
            for _, check in requirements.checks:
                check.toggled.connect(self._refresh_create_dependencies)
        for key in ("randomize_dash", "randomize_chainsaw"):
            control = self.view.option_controls.get(key)
            if isinstance(control, QCheckBox):
                checkbox = cast(QCheckBox, control)
                message = "before_finding_dash_some_routes_require_bunny_hopping_or_ballist" if key == "randomize_dash" else "hell_on_earth_starts_early_ammo_will_be_scarce_until_you_find_th"
                warning = _label(player_text("launcher.ui", message), "warning")
                self.view.option_rows[key].layout().addWidget(warning, 2, 0, 1, 2)
                warning.setVisible(checkbox.isChecked())
                checkbox.toggled.connect(warning.setVisible)
                self.randomization_warnings[key] = warning

    def _wire_enemy(self):
        for key in (*self.view.enemy_option_keys, "killsanity"):
            control = self.view.option_controls[key]
            if key == "enemy_preset":
                control.currentIndexChanged.connect(self._apply_enemy_preset)
                continue
            callback = (
                self._refresh_create_dependencies
                if key in ("enemy_randomizer", "easier_secret_encounters", "killsanity")
                else self._enemy_option_changed
            )
            if isinstance(control, QCheckBox):
                control.toggled.connect(callback)
            elif isinstance(control, QComboBox):
                control.currentIndexChanged.connect(callback)
            else:
                control.valueChanged.connect(callback)

    def _apply_enemy_preset(self) -> None:
        if self.view._syncing_create_dependencies:
            return
        preset = self._choice_value("enemy_preset")
        if preset != "custom":
            self.view._set_option_values(
                enemy_options_for_editing(
                    {
                        "enemy_preset": preset,
                        "enemy_randomizer": self._choice_value("enemy_randomizer"),
                        "progressive_difficulty": self._choice_value("progressive_difficulty"),
                    }
                )
            )
        self._refresh_create_dependencies()

    def _enemy_option_changed(self) -> None:
        if self.view._syncing_create_dependencies:
            return
        self._refresh_create_dependencies()

    def _refresh_enemy_dependencies(self) -> None:
        enabled = self._choice_value("enemy_randomizer") is True
        progressive = self._choice_value("progressive_difficulty") is True
        multiplier = self._choice_value("enemy_count_mode") == "multiplier"
        speed_qualified = "qualified" in enemy_speed_capabilities().values()
        active_rows = {
            "enemy_randomizer": True,
            "easier_secret_encounters": True,
            "enemy_intensity_early": enabled and not multiplier,
            "enemy_length": enabled and not multiplier,
            "enemy_density": enabled and not multiplier,
            "enemy_intensity_late": enabled and progressive and not multiplier,
            "enemy_count_multiplier": enabled and multiplier,
            "enemy_speed": enabled and speed_qualified,
        }
        self._set_enemy_rows(active_rows, enabled)
        for key in ("kills_per_check", "killsanity_maximum_milestones"):
            self.view.option_rows[key].setEnabled(self._choice_value("killsanity") is True)
        try:
            values = normalize_enemy_options(self.view._option_values())
        except ValueError as error:
            self.view.enemy_preview.set_error(self.view._file_failure(error))
            return
        self._enemy_preview(values, multiplier)

    def _set_enemy_rows(self, active_rows, enabled):
        for key in self.view.enemy_option_keys:
            self.view.option_rows[key].setEnabled(active_rows.get(key, enabled))

    def _enemy_preview(self, values, multiplier):
        weights = values["weights"]
        total = sum(weights.values())
        names = self.view.option_controls["enemy_weights"].entries
        ranked = sorted(
            ((key, weight) for key, weight in weights.items() if weight), key=lambda pair: (-pair[1], pair[0])
        )
        shown = ranked[:2]
        parts = [f"{names[key]['label']} {100 * weight / total:.1f}%" for key, weight in shown]
        other = sum(weight for _, weight in ranked[2:])
        if other:
            parts.append(player_text("launcher.player_file", "other_value_0", value_0=f"{100 * other / total:.1f}"))
        mode = player_text("launcher.player_file", "multiplier_value_0", value_0=str(values["count_multiplier"])) if multiplier else player_text('common', 'auto')
        pool = (player_text('launcher.player_file', 'base_game_enemies') if self._choice_value("use_dlc_content") is False else player_text('launcher.player_file', 'base_game_and_dlc_enemies'))
        self.view.enemy_preview.render({
            "mode": mode,
            "intensity": '{early}% → {late}%'.format(early=str(values["intensity"][0]), late=str(values["intensity"][1])),
            "density": '{value}%'.format(value=str(values["density"])),
            "variety": '{value}%'.format(value=str(values["variety"])),
            "size_variation": '{value}%'.format(value=str(values["size_variation_chance"])),
            "content": pool,
            "mix": ", ".join(parts) or player_text("launcher.enemy_preview", "none"),
            "speed": self._speed_preview(names),
        })

    @staticmethod
    def _speed_preview(names):
        supported = [names.get(key, {"label": key.replace("_", " ").title()})["label"]
                     for key, status in enemy_speed_capabilities().items() if status == "qualified"]
        if supported:
            return player_text("launcher.enemy_preview", "speed_supported", species=", ".join(supported))
        return player_text('common', 'enemy_speed_fixed')

    def _refresh_difficulty_skull(self) -> None:
        key = str(self._choice_value("campaign_difficulty"))
        if self.view.difficulty_skull.property("difficulty") == key:
            return
        name = {
            "im_too_young_to_die": "itytd",
            "hurt_me_plenty": "hurtmeplenty",
            "ultra_violence": "ultraviolence",
            "nightmare": "nightmare",
        }.get(key)
        if name is None:
            self.view.difficulty_skull.clear()
            return
        scale = self.view.difficulty_skull.devicePixelRatioF()
        source = QPixmap(str(self.view.controller.bundle_dir / "assets" / "launcher" / f"logo_{name}.png"))
        pixmap = source.scaled(
            round(64 * scale),
            round(64 * scale),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        pixmap.setDevicePixelRatio(scale)
        self.view.difficulty_skull.setPixmap(pixmap)
        self.view.difficulty_skull.setProperty("difficulty", key)
        label = self._selected_label("campaign_difficulty")
        self.view.difficulty_skull.setAccessibleName(label)
        self.view.difficulty_skull.setToolTip(label)

    def _choice_value(self, key: str) -> object:
        control = self.view.option_controls.get(key)
        if isinstance(control, QCheckBox):
            return cast(QCheckBox, control).isChecked()
        if isinstance(control, QComboBox):
            return cast(QComboBox, control).currentData()
        return None

    def _set_choice_enabled(
        self,
        key: str,
        value: str,
        enabled: bool,
        reason: str = player_text("launcher.player_file", "enable_dlc_missions_to_use_this_option"),
    ) -> None:
        control = self.view.option_controls.get(key)
        if not isinstance(control, QComboBox):
            return
        combo = cast(QComboBox, control)
        index = combo.findData(value)
        if index < 0:
            return
        item = cast(QStandardItemModel, combo.model()).item(index)
        if item is not None:
            item.setEnabled(enabled)
        combo.setItemData(index, None if enabled else reason, Qt.ItemDataRole.ToolTipRole)

    def _refresh_create_dependencies(self) -> None:
        if self.view._syncing_create_dependencies:
            return
        self.view._syncing_create_dependencies = True
        try:
            dlc_enabled = self._choice_value("use_dlc_content") is True
            dlc_missions = self._refresh_mission_options(dlc_enabled)
            requirements = self._refresh_requirements(dlc_missions)
            self._refresh_effective_config(dlc_enabled, requirements)
            self._refresh_enemy_dependencies()
            self._refresh_randomization_warnings()
        finally:
            self.view._syncing_create_dependencies = False

    def _selected_label(self, key: str) -> str:
        control = self.view.option_controls.get(key)
        return cast(QComboBox, control).currentText() if isinstance(control, QComboBox) else "—"

    def _refresh_randomization_warnings(self):
        for key, warning in self.randomization_warnings.items():
            warning.setVisible(self.view.option_controls[key].isChecked())

    def _refresh_mission_options(self, dlc_enabled):
        pool = self._choice_value("mission_pool")
        self._refresh_difficulty_skull()
        custom = pool == "custom"
        access = self._choice_value("mission_order") == "mission_access_as_items"
        for key, enabled in (
            ("custom_missions", custom),
            ("custom_dark_lord", custom),
            ("starting_missions", access),
            ("goal_mission_as_item", access),
            ("mission_count", self._choice_value("mission_order") != "vanilla_order"),
        ):
            if key in self.view.option_rows:
                self.view.option_rows[key].setVisible(enabled)
        dlc_missions = pool in {"full_saga", "dlc_only", "custom"}
        special_row = self.view.option_rows.get("special_weapon")
        self._set_choice_enabled("goal", "kill_the_dark_lord", dlc_missions)
        self._set_choice_enabled("goal", "complete_the_full_saga", dlc_missions)
        self._set_choice_enabled(
            "goal",
            "acquire_the_unmaykr",
            self._choice_value("include_slayer_gates") is not False,
            player_text("launcher.player_file", "enable_include_slayer_gates_to_use_this_option"),
        )
        if special_row is not None:
            special_row.setVisible(dlc_enabled)
        self.view._refresh_inventory_picker(dlc_enabled)
        return dlc_missions

    def _refresh_requirements(self, dlc_missions):
        goal_key = self._choice_value("goal")
        mastery_control = self.view.option_controls.get("include_weapon_mastery_challenges")
        mastery_enabled = (
            bool(cast(QCheckBox, mastery_control).isChecked()) if isinstance(mastery_control, QCheckBox) else True
        )
        unavailable: set[str] = set()
        if goal_key == "acquire_the_unmaykr":
            unavailable.add("Acquire the Unmaykr")
            if not dlc_missions:
                unavailable.add("Complete All Slayer Gates")
        if not dlc_missions:
            unavailable.add("Complete All Escalation Encounters")
        if not mastery_enabled:
            unavailable.add("Complete All Weapon Mastery Challenges")
        if self._choice_value("include_slayer_gates") is False:
            unavailable.update({"Complete All Slayer Gates", "Acquire the Unmaykr"})
        if self._choice_value("include_secret_encounters") is False:
            unavailable.add("Complete All Secret Encounters")
        requirements = self.view.option_controls.get("additional_victory_requirements")
        if isinstance(requirements, OptionSetControl):
            requirements.set_dependencies(set(), unavailable)
        return requirements

    def _refresh_effective_config(self, dlc_enabled, requirements):
        goal_label = self._selected_label("goal")
        special_control = cast(QComboBox, self.view.option_controls["special_weapon"])
        special_label = (
            special_control.itemText(special_control.findData("the_crucible"))
            if not dlc_enabled
            else self._selected_label("special_weapon")
        )
        requirement_count = len(requirements.value()) if isinstance(requirements, OptionSetControl) else 0
        victory_summary = player_text("launcher.player_file", "goal_only")
        if requirement_count:
            victory_summary = (player_text('launcher.player_file.extra_objective', 'one', count=requirement_count) if requirement_count == 1 else player_text('launcher.player_file.extra_objective', 'other', count=requirement_count))
        values = {
            "goal": goal_label,
            "campaign": self._selected_label("mission_pool") + " · " + self._selected_label("mission_order"),
            "logic": self._selected_label("logic_difficulty"),
            "special": special_label,
            "starting": self._selected_label("starting_weapon"),
            "victory": victory_summary,
        }
        for key, value in values.items():
            self.view.effective_config_values[key].setText(value)
