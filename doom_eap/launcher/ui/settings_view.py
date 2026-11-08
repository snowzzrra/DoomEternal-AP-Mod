"""Display preference state; controls retain their persistence handlers."""

from dataclasses import dataclass

from PySide6.QtCore import QLocale, QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QMenu,
    QMessageBox,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from doom_eap.strings import load_catalog

from .controls import _label
from .strings import record_failure, selected_language, text


class SettingsView(QScrollArea):
    def __init__(self, refill, special, killsanity, grace, language_button):
        super().__init__()
        self.refill, self.special = refill, special
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        outer = QHBoxLayout(body)
        outer.setSpacing(12)
        outer.setContentsMargins(24, 24, 24, 24)
        content = QWidget()
        content.setMaximumWidth(760)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(_label(text("launcher.navigation", "settings"), "title"))
        layout.addSpacing(16)
        layout.addWidget(_label(text("launcher.settings", "controls"), "eyebrow"))
        layout.addSpacing(8)
        layout.addWidget(self._row("ammo_key", refill))
        layout.addSpacing(12)
        special_control = QWidget()
        special_layout = QVBoxLayout(special_control)
        special_layout.setContentsMargins(0, 0, 0, 0)
        special_layout.setSpacing(4)
        special_layout.addWidget(special)
        self.conflict_warning = _label(text("launcher.settings", "key_conflict"), "conflictWarning")
        special_layout.addWidget(self.conflict_warning)
        layout.addWidget(self._row("special_key", special_control))
        layout.addSpacing(24)
        layout.addWidget(_label(text("launcher.settings", "in_game"), "eyebrow"))
        layout.addSpacing(8)
        killsanity.setText("")
        killsanity.setAccessibleName(text("launcher.settings", "killsanity"))
        killsanity.setObjectName("switch")
        layout.addWidget(self._row("killsanity", killsanity, "killsanity_help"))
        layout.addSpacing(24)
        layout.addWidget(_label(text('common', 'deathlink'), "eyebrow"))
        layout.addSpacing(8)
        layout.addWidget(self._row("deathlink", grace, "deathlink_help"))
        layout.addSpacing(24)
        layout.addWidget(_label(text("launcher.settings", "language_section"), "eyebrow"))
        layout.addSpacing(8)
        self.language = QComboBox()
        self.language.setFixedWidth(240)
        self.language.setIconSize(QSize(24, 16))
        for code, details in language_button.catalog.languages.items():
            self.language.addItem(language_button.icon_for(code), details.name, code)
        self.language.setCurrentIndex(self.language.findData(language_button.language))
        self.language.activated.connect(lambda index: self._select_language(language_button, index))
        language_button.selected.connect(lambda code: self.language.setCurrentIndex(self.language.findData(code)))
        layout.addWidget(self._row("language", self.language, "language_help"))
        layout.addStretch()
        outer.addWidget(content, 1)
        outer.addStretch()
        self.setWidget(body)
        refill.changed.connect(self.render_conflict)
        special.changed.connect(self.render_conflict)
        self.render_conflict()

    def _select_language(self, button, index):
        button._select(self.language.itemData(index))
        self.language.setCurrentIndex(self.language.findData(button.language))

    @staticmethod
    def _row(key, control, description=None):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        labels = QWidget()
        labels.setFixedWidth(260)
        copy = QVBoxLayout(labels)
        copy.setContentsMargins(0, 0, 0, 0)
        copy.setSpacing(4)
        title = _label(text(*{"ammo_key": ("common", "ammo_refill"), "deathlink_section": ("common", "deathlink"), "language": ("common", "launcher_language")}.get(key, ("launcher.settings", key))))
        title.setBuddy(control)
        copy.addWidget(title)
        if description:
            copy.addWidget(_label(text("launcher.settings", description), "muted"))
        layout.addWidget(labels)
        right = QWidget()
        controls = QHBoxLayout(right)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(0)
        controls.addWidget(control, 1, Qt.AlignmentFlag.AlignTop)
        if control.maximumWidth() < 16777215:
            controls.addStretch()
        layout.addWidget(right, 1)
        return row

    def render_conflict(self, *_):
        model = SettingsPresenter.present(self.refill.value(), self.special.value())
        self.conflict_warning.setVisible(model.conflict)
        self.special.editor.setProperty("conflict", model.conflict)
        self.special.editor.style().unpolish(self.special.editor)
        self.special.editor.style().polish(self.special.editor)
        return model.conflict


@dataclass(frozen=True)
class SettingsModel:
    conflict: bool


class SettingsPresenter:
    @staticmethod
    def present(refill: str, special: str) -> SettingsModel:
        return SettingsModel(bool(refill) and refill == special)


class LanguageButton(QToolButton):
    selected = Signal(str)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setObjectName("language")
        self.controller = controller
        self.catalog = load_catalog()
        self.language = selected_language(controller.config.get("launcher_language"), QLocale.system().name())
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setIconSize(QSize(24, 16))
        self.setMenu(QMenu(self))
        self._render()

    def _render(self):
        language = self.catalog.languages[self.language]
        icon = self.icon_for(self.language)
        self.setIcon(icon)
        self.setText(self.language if icon.isNull() else "")
        title = self.catalog.text('common', 'launcher_language', locale=self.language)
        self.setToolTip(f"{title}: {language.name}")
        self.setAccessibleName(self.toolTip())
        self.menu().clear()
        for code, details in self.catalog.languages.items():
            if code != self.language:
                action = self.menu().addAction(details.name)
                action.setData(code)
                action.triggered.connect(lambda checked=False, code=code: self._select(code))

    def icon_for(self, code):
        language = self.catalog.languages[code]
        icon = QIcon()
        if language.flag:
            path = self.controller.bundle_dir / "assets/launcher/flags" / f"{language.flag}.svg"
            if path.is_file():
                icon = QIcon(str(path))
        return icon

    def _select(self, code):
        title = self.catalog.text('common', 'launcher_language', locale=code)
        try:
            self.controller.set_launcher_language(code)
        except OSError as error:
            self.controller.emit("log", message=record_failure(self.controller, "language", error))
            QMessageBox.warning(self, title, self.catalog.text("launcher.language", "save_failed", locale=code))
            return
        self.language = code
        self._render()
        self.selected.emit(code)
        QMessageBox.information(self, title, self.catalog.text("launcher.language", "restart", locale=code))
