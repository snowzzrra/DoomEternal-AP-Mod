import threading
from unittest.mock import MagicMock

import pytest

from doom_eap.launcher.launcher_controller import LauncherController


def _controller(*, connected: bool):
    controller = object.__new__(LauncherController)
    controller._lifecycle_lock = threading.RLock()
    controller.connected_room = connected
    controller.supervisor = MagicMock()
    return controller


def test_send_chat_forwards_exact_text_only_when_connected():
    controller = _controller(connected=True)

    controller.send_chat("  !hint Super Shotgun  ")

    controller.supervisor.send_chat.assert_called_once_with("  !hint Super Shotgun  ")


def test_send_chat_ignores_whitespace_and_rejects_disconnected_room():
    controller = _controller(connected=True)
    controller.send_chat(" \t ")
    controller.supervisor.send_chat.assert_not_called()

    controller.connected_room = False
    with pytest.raises(RuntimeError, match="not connected"):
        controller.send_chat("hello")
