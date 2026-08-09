"""Standalone DOOM Eternal Archipelago launcher entrypoint."""

from __future__ import annotations

import os
import sys


def _run_bridge_worker(arguments: list[str]) -> int:
    archipelago_source = os.environ.get("ARCHIPELAGO_SOURCE", "").strip()
    if archipelago_source and archipelago_source not in sys.path:
        sys.path.insert(0, archipelago_source)

    from bridge_client import launch

    launch(*(argument for argument in arguments if argument != "--bridge-worker"))
    return 0


def _run_ui() -> int:
    from PySide6.QtWidgets import QApplication

    from launcher_controller import LauncherController
    from launcher_ui import LauncherUI

    application = QApplication(sys.argv[:1])
    application.setApplicationName("DOOM Eternal Archipelago")
    LauncherUI(LauncherController()).run()
    return 0


def main(arguments: list[str] | None = None) -> int:
    parsed = list(sys.argv[1:] if arguments is None else arguments)
    if "--bridge-worker" in parsed:
        return _run_bridge_worker(parsed)
    return _run_ui()


if __name__ == "__main__":
    raise SystemExit(main())
