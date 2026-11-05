"""Choose a catalog language for the next launcher session; game settings stay separate."""

from PySide6.QtCore import QLocale, QSize, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QMessageBox, QToolButton

from doom_eap.strings import load_catalog

from .strings import record_failure, selected_language


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
