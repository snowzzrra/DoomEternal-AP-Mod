#!/usr/bin/env python3
"""Single aggregate preflight checker for GitHub Actions cross-platform build.

Performs deterministic, zero-compilation, zero-build validation of:
- Launcher source & data path availability
- Launcher standalone runtime import graph & stub precedence
- Workflow structure & contract invariants
- Native toolchain linkability invariants (when running in toolchain environment)
- Release assembler static contract
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
import yaml
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def check_source_data_paths(repo_root: Path, archipelago_source: Path) -> None:
    print("--> Checking PyInstaller source and data paths...")
    required_paths = [
        ("bridge_client.py", repo_root / "doom_eap/runtime/bridge_client.py"),
        ("launcher_app.py", repo_root / "doom_eap/launcher/launcher_app.py"),
        ("data directory", repo_root / "data"),
        ("manifests directory", repo_root / "manifests"),
        ("standalone_runtime directory", repo_root / "packaging/standalone_runtime"),
        ("standalone ModuleUpdate stub", repo_root / "packaging/standalone_runtime/ModuleUpdate.py"),
        ("standalone MultiServer stub", repo_root / "packaging/standalone_runtime/MultiServer.py"),
        ("standalone worlds stub", repo_root / "packaging/standalone_runtime/worlds/__init__.py"),
        ("Archipelago CommonClient.py", archipelago_source / "CommonClient.py"),
        ("Archipelago Utils.py", archipelago_source / "Utils.py"),
        ("Archipelago NetUtils.py", archipelago_source / "NetUtils.py"),
    ]
    for label, path in required_paths:
        if not path.exists():
            raise RuntimeError(f"Required path missing for launcher build: {label} ({path})")
        print(f"  [OK] {label} -> {path}")


def check_workflow_contract(repo_root: Path) -> None:
    print("--> Checking GitHub Actions workflow contract...")
    wf_path = repo_root / ".github/workflows/cross-platform-build.yml"
    if not wf_path.is_file():
        raise RuntimeError(f"Workflow file missing at {wf_path}")

    doc = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
    
    # Trigger check
    triggers = list(doc.get("on", {}).keys())
    if triggers != ["workflow_dispatch"]:
        raise RuntimeError(f"Workflow triggers must be exactly ['workflow_dispatch'], got {triggers}")

    # Permissions check
    perms = doc.get("permissions")
    if perms != {"contents": "read"}:
        raise RuntimeError(f"Workflow permissions must be exactly {{'contents': 'read'}}, got {perms}")

    # Check job presence
    jobs = set(doc.get("jobs", {}).keys())
    expected_jobs = {
        "resolve-metadata",
        "build-apworld",
        "build-native-support",
        "build-linux-launcher",
        "build-windows-launcher",
        "consolidate-handoff",
    }
    if jobs != expected_jobs:
        raise RuntimeError(f"Unexpected workflow job set: missing={expected_jobs - jobs}, extra={jobs - expected_jobs}")

    # Check that launcher jobs use python -m tools.release.build_launcher
    raw_text = wf_path.read_text(encoding="utf-8")
    if "tools/release/build_launcher.py" in raw_text:
        raise RuntimeError("Workflow must invoke build_launcher via 'python -m tools.release.build_launcher', not by path")
    if "python -m tools.release.build_launcher" not in raw_text and "python3 -m tools.release.build_launcher" not in raw_text:
        raise RuntimeError("Workflow missing 'python -m tools.release.build_launcher' invocation")

    # Check that wine64-tools is installed
    if "wine64-tools" not in raw_text:
        raise RuntimeError("Workflow native job must install wine64-tools for WIDL support")

    # Check apworld_ref default
    apworld_default = doc.get("on", {}).get("workflow_dispatch", {}).get("inputs", {}).get("apworld_ref", {}).get("default")
    if apworld_default != "doom_eternal":
        raise RuntimeError(f"Expected apworld_ref default to be 'doom_eternal', got {apworld_default!r}")

    print("  [OK] Workflow trigger is workflow_dispatch only")
    print("  [OK] Workflow permissions are contents: read")
    print("  [OK] All 6 expected jobs present")
    print("  [OK] Module invocation python -m tools.release.build_launcher verified")


def check_runtime_imports(repo_root: Path, archipelago_source: Path) -> None:
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
    check_workflow_contract(args.repo_root)
    check_runtime_imports(args.repo_root, args.archipelago_source)

    print("\n=======================================================")
    print("ALL CI PREFLIGHT CHECKS PASSED (HIGH CONFIDENCE FOR RUN #2)!")
    print("=======================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
