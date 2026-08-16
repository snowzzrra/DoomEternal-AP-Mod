"""Build current-platform standalone launcher with minimal AP client runtime."""

from __future__ import annotations

import argparse
import importlib.util
import importlib.metadata
import os
import platform
import subprocess
import sys
from pathlib import Path

from tools.release.build_cache import content_key, publish, restore


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = REPO_ROOT.parent
RELEASE_ROOT = (REPO_ROOT / "build/release").resolve()
SOURCE_EXCLUDED_DIRS = frozenset({
    ".git", ".cache", ".pytest_cache", "__pycache__", "build", "dist",
    "node_modules", "test", "tests", "venv", ".venv",
})
PYINSTALLER_EXCLUDES = (
    "kivy", "kvui", "BaseClasses", "Options", "Fill", "entrance_rando",
    "rule_builder", "Cython", "PIL", "jinja2", "setuptools", "pip",
    "tkinter", "_tkinter",
)


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _source_inputs(label: str, root: Path, *, pattern: str | None = None) -> list[tuple[str, Path]]:
    """Expand declared bundled source roots without including generated artifacts."""
    root = root.resolve()
    if root.is_file():
        return [(label, root)]
    if not root.is_dir():
        raise ValueError(f"launcher cache source root missing: {root}")
    inputs: list[tuple[str, Path]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in SOURCE_EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"launcher cache source contains symlink: {path}")
        if path.is_file() and (pattern is None or path.match(pattern)):
            inputs.append((f"{label}/{relative.as_posix()}", path))
    return inputs


def _launcher_inputs(archipelago_source: Path) -> list[tuple[str, Path]]:
    inputs: list[tuple[str, Path]] = []
    inputs.extend(_source_inputs("doom_eap", REPO_ROOT / "doom_eap"))
    inputs.extend(_source_inputs("tools/decls", REPO_ROOT / "tools/decls"))
    inputs.extend(_source_inputs("tools/maps", REPO_ROOT / "tools/maps"))
    inputs.extend(_source_inputs("tools/release", REPO_ROOT / "tools/release"))
    inputs.extend(_source_inputs("packaging/standalone_runtime", REPO_ROOT / "packaging/standalone_runtime"))
    inputs.extend(_source_inputs("data", REPO_ROOT / "data"))
    inputs.extend(_source_inputs("manifests", REPO_ROOT / "manifests"))
    inputs.extend(_source_inputs("requirements-launcher.txt", REPO_ROOT / "requirements-launcher.txt"))
    inputs.extend(_source_inputs("archipelago", archipelago_source))
    return inputs


def _dependency_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "missing"


def build(output_dir: Path, archipelago_source: Path, name: str) -> Path:
    output_dir = output_dir.expanduser().resolve()
    archipelago_source = archipelago_source.expanduser().resolve()
    if not _within(output_dir, RELEASE_ROOT):
        raise ValueError(f"launcher output must remain under {RELEASE_ROOT}")
    if not (archipelago_source / "CommonClient.py").is_file():
        raise ValueError(f"invalid Archipelago source: {archipelago_source}")
    cache_root = Path(os.environ.get("AP_BUILD_CACHE_ROOT", REPO_ROOT / ".cache/ap-build"))
    executable_name = f"{name}.exe" if os.name == "nt" else name
    key = content_key(
        "launcher",
        _launcher_inputs(archipelago_source),
        config={
            "name": name,
            "python": sys.executable,
            "sys_version": sys.version,
            "sys_platform": sys.platform,
            "machine": platform.machine(),
            "architecture": platform.architecture()[0],
            "os_name": os.name,
            "pyinstaller": _dependency_version("pyinstaller"),
            "pyside6": _dependency_version("PySide6"),
            "command": [
                "onefile", "console", "standalone_runtime", "data", "manifests",
                *PYINSTALLER_EXCLUDES,
            ],
        },
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    hit, reason = restore(cache_root, "launcher", key, output_dir, (executable_name,))
    if hit:
        print(f"LAUNCHER cache=hit key={key}")
        return output_dir / executable_name
    print(f"LAUNCHER cache=miss reason={reason} key={key}")
    if importlib.util.find_spec("PyInstaller") is None:
        raise RuntimeError(
            "PyInstaller is missing; install requirements-launcher.txt in launcher build Python"
        )
    if importlib.util.find_spec("PySide6") is None:
        raise RuntimeError(
            "PySide6 is missing; install requirements-launcher.txt in launcher build Python"
        )

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
        str(REPO_ROOT),
        "--paths",
        str(REPO_ROOT / "packaging/standalone_runtime"),
        "--paths",
        str(archipelago_source),
        "--add-data",
        f"{REPO_ROOT / 'doom_eap/runtime/bridge_client.py'}{data_separator}.",
        "--add-data",
        f"{REPO_ROOT / 'data'}{data_separator}data",
        "--add-data",
        f"{REPO_ROOT / 'manifests'}{data_separator}manifests",
        str(REPO_ROOT / "doom_eap/launcher/launcher_app.py"),
    ]
    for excluded_module in PYINSTALLER_EXCLUDES:
        command[-1:-1] = ["--exclude-module", excluded_module]
    if os.name == "nt":
        command[-1:-1] = ["--hide-console", "hide-early"]
    subprocess.run(command, check=True, cwd=REPO_ROOT)
    executable = output_dir / (f"{name}.exe" if os.name == "nt" else name)
    if not executable.is_file():
        raise RuntimeError(f"PyInstaller did not produce {executable}")
    publish(cache_root, "launcher", key, output_dir, (executable_name,))
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
