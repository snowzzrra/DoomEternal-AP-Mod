"""Build current-platform standalone launcher with minimal AP client runtime."""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = REPO_ROOT.parent
RELEASE_ROOT = (REPO_ROOT / "build/release").resolve()
def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def build(output_dir: Path, archipelago_source: Path, name: str) -> Path:
    output_dir = output_dir.expanduser().resolve()
    archipelago_source = archipelago_source.expanduser().resolve()
    if not _within(output_dir, RELEASE_ROOT):
        raise ValueError(f"launcher output must remain under {RELEASE_ROOT}")
    if not (archipelago_source / "CommonClient.py").is_file():
        raise ValueError(f"invalid Archipelago source: {archipelago_source}")
    if importlib.util.find_spec("PyInstaller") is None:
        raise RuntimeError(
            "PyInstaller is missing; install requirements-launcher.txt in launcher build Python"
        )
    if importlib.util.find_spec("PySide6") is None:
        raise RuntimeError(
            "PySide6 is missing; install requirements-launcher.txt in launcher build Python"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    build_root = RELEASE_ROOT / "build/launcher"
    build_root.mkdir(parents=True, exist_ok=True)
    data_separator = os.pathsep
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--name",
        name,
        "--distpath",
        str(output_dir),
        "--workpath",
        str(build_root / "work"),
        "--specpath",
        str(build_root / "spec"),
        "--paths",
        str(REPO_ROOT / "packaging/standalone_runtime"),
        "--paths",
        str(archipelago_source),
        "--add-data",
        f"{REPO_ROOT / 'bridge_client.py'}{data_separator}.",
        "--add-data",
        f"{REPO_ROOT / 'data'}{data_separator}data",
        "--add-data",
        f"{REPO_ROOT / 'manifests'}{data_separator}manifests",
        str(REPO_ROOT / "launcher_app.py"),
    ]
    for excluded_module in (
        "kivy",
        "kvui",
        "BaseClasses",
        "Options",
        "Fill",
        "entrance_rando",
        "rule_builder",
        "Cython",
        "PIL",
        "jinja2",
        "setuptools",
        "pip",
        "tkinter",
        "_tkinter",
    ):
        command[-1:-1] = ["--exclude-module", excluded_module]
    if os.name == "nt":
        command[-1:-1] = ["--hide-console", "hide-early"]
    subprocess.run(command, check=True, cwd=REPO_ROOT)
    executable = output_dir / (f"{name}.exe" if os.name == "nt" else name)
    if not executable.is_file():
        raise RuntimeError(f"PyInstaller did not produce {executable}")
    return executable


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=RELEASE_ROOT)
    parser.add_argument("--archipelago-source", type=Path, default=WORKSPACE / "Archipelago")
    parser.add_argument("--name", default="DoomEternalArchipelagoLauncher")
    arguments = parser.parse_args()
    print(build(arguments.output_dir, arguments.archipelago_source, arguments.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
