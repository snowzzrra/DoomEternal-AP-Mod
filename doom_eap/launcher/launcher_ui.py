"""Compatibility imports for the launcher window; implementation lives in ui.app_window."""

import subprocess
import webbrowser

from PySide6.QtGui import QDesktopServices

from .ui.app_window import (
    LauncherUI,
)
from .ui.controls import AmmoRefillKeyControl, DictionaryControl, NamedRangeControl, OptionSetControl

__all__ = [
    "AmmoRefillKeyControl",
    "DictionaryControl",
    "LauncherUI",
    "NamedRangeControl",
    "OptionSetControl",
    "QDesktopServices",
    "subprocess",
    "webbrowser",
]
