"""Build current-platform standalone launcher with minimal AP client runtime."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = REPO_ROOT.parent
RELEASE_ROOT = (REPO_ROOT / "build/release").resolve()
RUNTIME_DISTRIBUTIONS = (
    "colorama",
    "websockets",
    "PyYAML",
    "pathspec",
    "typing_extensions",
    "platformdirs",
    "certifi",
    "PySide6",
    "shiboken6",
    "pyinstaller",
)


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _copy_python_license(destination: Path) -> None:
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidates = (
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE",
        Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
        Path(sys.base_prefix) / "lib" / version / "LICENSE.txt",
    )
    for candidate in candidates:
        if candidate.is_file():
            shutil.copy2(candidate, destination / "Python-LICENSE.txt")
            return
    raise RuntimeError("Python runtime license was not found")


def _copy_distribution_licenses(destination: Path) -> None:
    package_root = destination / "python-packages"
    package_root.mkdir(parents=True, exist_ok=True)
    for name in RUNTIME_DISTRIBUTIONS:
        distribution = importlib.metadata.distribution(name)
        copied = 0
        target = package_root / distribution.metadata["Name"]
        for entry in distribution.files or ():
            basename = Path(str(entry)).name.lower()
            if not any(token in basename for token in ("license", "copying", "notice")):
                continue
            source = Path(str(distribution.locate_file(entry)))
            if not source.is_file():
                continue
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / Path(str(entry)).name)
            copied += 1
        if not copied:
            license_text = distribution.metadata.get("License", "").strip()
            if name in {"PySide6", "shiboken6"} and license_text:
                target.mkdir(parents=True, exist_ok=True)
                (target / "LICENSE-SPDX.txt").write_text(
                    f"{distribution.metadata['Name']} {distribution.version}: {license_text}\n",
                    encoding="utf-8",
                )
                copied = 1
            else:
                raise RuntimeError(f"license file not found for {name}")


def _copy_licenses(output_dir: Path, archipelago_source: Path) -> None:
    destination = output_dir / "licenses"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    shutil.copy2(archipelago_source / "LICENSE", destination / "Archipelago-LICENSE.txt")
    _copy_python_license(destination)
    _copy_distribution_licenses(destination)


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
    _copy_licenses(output_dir, archipelago_source)
    return executable


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=RELEASE_ROOT / "client")
    parser.add_argument("--archipelago-source", type=Path, default=WORKSPACE / "Archipelago")
    parser.add_argument("--name", default="DoomEternalArchipelagoLauncher")
    arguments = parser.parse_args()
    print(build(arguments.output_dir, arguments.archipelago_source, arguments.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
