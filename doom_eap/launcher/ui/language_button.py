"""Choose a catalog language for the next launcher session; game settings stay separate."""

from PySide6.QtCore import QLocale, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QMessageBox, QToolButton

from doom_eap.strings import load_catalog

from .strings import selected_language


class LanguageButton(QToolButton):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.catalog = load_catalog()
        self.language = selected_language(controller.config.get("launcher_language"), QLocale.system().name())
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setIconSize(QSize(30, 20))
        self.setMenu(QMenu(self))
        self._render()

    def _render(self):
        language = self.catalog.languages[self.language]
        icon = QIcon()
        if language.flag:
            path = self.controller.bundle_dir / "assets/launcher/flags" / language.flag
            if path.is_file():
                icon = QIcon(str(path))
        self.setIcon(icon)
        self.setText(self.language if icon.isNull() else "")
        title = self.catalog.text("launcher.language", "choose", locale=self.language)
        self.setToolTip(f"{title}: {language.name}")
        self.setAccessibleName(self.toolTip())
        self.menu().clear()
        for code, details in self.catalog.languages.items():
            if code != self.language:
                action = self.menu().addAction(details.name)
                action.setData(code)
                action.triggered.connect(lambda checked=False, code=code: self._select(code))

    def _select(self, code):
        title = self.catalog.text("launcher.language", "choose", locale=code)
        try:
            self.controller.set_launcher_language(code)
        except OSError:
            QMessageBox.warning(self, title, self.catalog.text("launcher.language", "save_failed", locale=code))
            return
        self.language = code
        self._render()
        QMessageBox.information(self, title, self.catalog.text("launcher.language", "restart", locale=code))
