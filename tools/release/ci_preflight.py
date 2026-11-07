#!/usr/bin/env python3
"""Validate release source paths and standalone runtime imports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def check_source_data_paths(repo_root: Path, archipelago_source: Path) -> None:
    print("--> Checking PyInstaller source and data paths...")
    required_paths = [
        ("bridge_client.py", repo_root / "doom_eap/runtime/bridge_client.py"),
        ("launcher_app.py", repo_root / "doom_eap/launcher/launcher_app.py"),
        ("data directory", repo_root / "data"),
        ("options_schema.json", repo_root / "data/options_schema.json"),
        ("manifests directory", repo_root / "manifests"),
        ("standalone_runtime directory", repo_root / "packaging/standalone_runtime"),
        ("standalone ModuleUpdate stub", repo_root / "packaging/standalone_runtime/ModuleUpdate.py"),
        ("standalone MultiServer stub", repo_root / "packaging/standalone_runtime/MultiServer.py"),
        ("standalone worlds stub", repo_root / "packaging/standalone_runtime/worlds/__init__.py"),
        ("requirements-ci.txt", repo_root / "requirements-ci.txt"),
        ("Archipelago CommonClient.py", archipelago_source / "CommonClient.py"),
        ("Archipelago Utils.py", archipelago_source / "Utils.py"),
        ("Archipelago NetUtils.py", archipelago_source / "NetUtils.py"),
    ]
    for label, path in required_paths:
        if not path.exists():
            raise RuntimeError(f"Required path missing for launcher build: {label} ({path})")
        print(f"  [OK] {label} -> {path}")


def check_runtime_imports(repo_root: Path, archipelago_source: Path) -> None:
    repo_str = str(repo_root.resolve())
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)
    from tools.release.verify_launcher_runtime import verify_runtime
    verify_runtime(archipelago_source, repo_root)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run aggregate cross-platform preflight verification.")
    parser.add_argument("--archipelago-source", type=Path, required=True, help="Path to Archipelago source root")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help="Path to DoomEternal-AP-Mod repo root")
    args = parser.parse_args()

    print("=======================================================")
    print("DOOM ETERNAL AP CI PREFLIGHT VERIFICATION")
    print("=======================================================")
    check_source_data_paths(args.repo_root, args.archipelago_source)
    check_runtime_imports(args.repo_root, args.archipelago_source)

    print("\n=======================================================")
    print("ALL CI PREFLIGHT CHECKS PASSED")
    print("=======================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
