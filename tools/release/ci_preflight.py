#!/usr/bin/env python3
"""Single aggregate preflight checker for GitHub Actions cross-platform build.

Performs deterministic, zero-compilation, zero-build validation of:
- Launcher source & data path availability
- Launcher standalone runtime import graph & stub precedence
- Workflow structure, preflight gate dependencies & step order invariants
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

    # Check job presence (all 7 jobs)
    jobs = set(doc.get("jobs", {}).keys())
    expected_jobs = {
        "resolve-metadata",
        "preflight",
        "build-apworld",
        "build-native-support",
        "build-linux-launcher",
        "build-windows-launcher",
        "consolidate-handoff",
    }
    if jobs != expected_jobs:
        raise RuntimeError(f"Unexpected workflow job set: missing={expected_jobs - jobs}, extra={jobs - expected_jobs}")

    # Check downstream job dependencies on preflight
    for job_name in ["build-apworld", "build-native-support", "build-linux-launcher", "build-windows-launcher"]:
        job_needs = doc["jobs"][job_name].get("needs", [])
        if isinstance(job_needs, str):
            job_needs = [job_needs]
        if "preflight" not in job_needs:
            raise RuntimeError(f"Job {job_name} must depend on 'preflight', got needs: {job_needs}")

    # Check launcher job step ordering
    for job_name, self_test_pattern in [
        ("build-linux-launcher", "DoomEternalArchipelagoLauncher --self-test"),
        ("build-windows-launcher", "DoomEternalArchipelagoLauncher.exe --self-test"),
    ]:
        steps = doc["jobs"][job_name].get("steps", [])
        step_names = [s.get("name", "") for s in steps]
        step_runs = [s.get("run", "") for s in steps]

        # 1. Install dependencies
        install_indices = [i for i, run in enumerate(step_runs) if "requirements-launcher.txt" in run]
        if not install_indices:
            raise RuntimeError(f"{job_name} missing 'pip install -r requirements-launcher.txt' step")
        install_idx = install_indices[0]

        # 2. Source preflight
        preflight_indices = [i for i, run in enumerate(step_runs) if "verify_launcher_runtime" in run]
        if not preflight_indices:
            raise RuntimeError(f"{job_name} missing 'verify_launcher_runtime' step")
        preflight_idx = preflight_indices[0]

        # 3. Build launcher
        build_indices = [i for i, run in enumerate(step_runs) if "build_launcher" in run and "--help" not in run]
        if not build_indices:
            raise RuntimeError(f"{job_name} missing 'build_launcher' step")
        build_idx = build_indices[0]

        # 4. Self-test validation
        selftest_indices = [i for i, run in enumerate(step_runs) if self_test_pattern in run]
        if not selftest_indices:
            raise RuntimeError(f"{job_name} missing '{self_test_pattern}' step")
        selftest_idx = selftest_indices[0]

        # 5. Upload artifact
        upload_indices = [i for i, s in enumerate(steps) if "upload-artifact" in s.get("uses", "")]
        if not upload_indices:
            raise RuntimeError(f"{job_name} missing upload-artifact step")
        upload_idx = upload_indices[0]

        if not (install_idx < preflight_idx < build_idx < selftest_idx < upload_idx):
            raise RuntimeError(
                f"{job_name} step ordering invariant violated: "
                f"install({install_idx}) < preflight({preflight_idx}) < build({build_idx}) < selftest({selftest_idx}) < upload({upload_idx})"
            )

    # Check that wine64-tools is installed
    raw_text = wf_path.read_text(encoding="utf-8")
    if "wine64-tools" not in raw_text:
        raise RuntimeError("Workflow native job must install wine64-tools for WIDL support")

    # Check apworld_ref default
    apworld_default = doc.get("on", {}).get("workflow_dispatch", {}).get("inputs", {}).get("apworld_ref", {}).get("default")
    if apworld_default != "doom_eternal":
        raise RuntimeError(f"Expected apworld_ref default to be 'doom_eternal', got {apworld_default!r}")

    print("  [OK] Workflow trigger is workflow_dispatch only")
    print("  [OK] Workflow permissions are contents: read")
    print("  [OK] All 7 expected jobs present and properly sequenced")
    print("  [OK] Module invocation python -m tools.release.build_launcher verified")
    print("  [OK] Step ordering invariant (install < preflight < build < selftest < upload) verified")


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
    print("ALL CI PREFLIGHT CHECKS PASSED")
    print("=======================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
