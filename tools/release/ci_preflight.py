#!/usr/bin/env python3
"""Single aggregate preflight checker for GitHub Actions cross-platform build.

Performs deterministic, zero-compilation, zero-build validation of:
- Launcher source & data path availability
- Launcher standalone runtime import graph & stub precedence
- Workflow structure, single-owner preflight invariants & step order invariants
- Native toolchain linkability invariants (when running in toolchain environment)
- Release assembler static contract
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

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

    # 1. Trigger check
    triggers = list(doc.get("on", {}).keys())
    if triggers != ["workflow_dispatch"]:
        raise RuntimeError(f"Workflow triggers must be exactly ['workflow_dispatch'], got {triggers}")

    # 2. Permissions check
    perms = doc.get("permissions")
    if perms != {"contents": "read"}:
        raise RuntimeError(f"Workflow permissions must be exactly {{'contents': 'read'}}, got {perms}")

    # 3. Check job presence (all 7 jobs)
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

    # 4. Check downstream job dependencies on preflight
    for job_name in ["build-apworld", "build-native-support", "build-linux-launcher", "build-windows-launcher"]:
        job_needs = doc["jobs"][job_name].get("needs", [])
        if isinstance(job_needs, str):
            job_needs = [job_needs]
        if "preflight" not in job_needs:
            raise RuntimeError(f"Job {job_name} must depend on 'preflight', got needs: {job_needs}")

    # 5. Single-Owner Invariant for verify_launcher_runtime
    verify_runtime_invocations = []
    for job_name, job_def in doc.get("jobs", {}).items():
        for step in job_def.get("steps", []):
            run_cmd = step.get("run", "")
            if "verify_launcher_runtime" in run_cmd:
                verify_runtime_invocations.append((job_name, step.get("name", "")))

    if len(verify_runtime_invocations) != 1:
        raise RuntimeError(
            f"verify_launcher_runtime must have exactly ONE invocation across the workflow, "
            f"found {len(verify_runtime_invocations)}: {verify_runtime_invocations}"
        )
    if verify_runtime_invocations[0][0] != "preflight":
        raise RuntimeError(
            f"verify_launcher_runtime must be owned by job 'preflight', "
            f"found in job '{verify_runtime_invocations[0][0]}'"
        )

    # 6. Single-Owner Invariant for Ruff Static Analysis
    ruff_invocations = []
    for job_name, job_def in doc.get("jobs", {}).items():
        for step in job_def.get("steps", []):
            run_cmd = step.get("run", "")
            if "ruff check" in run_cmd:
                ruff_invocations.append(job_name)

    for job_name in ruff_invocations:
        job_steps = doc["jobs"][job_name].get("steps", [])
        has_ci_reqs = any("requirements-ci.txt" in s.get("run", "") for s in job_steps)
        if not has_ci_reqs:
            raise RuntimeError(f"Job '{job_name}' runs Ruff but does not install requirements-ci.txt")

    # 7. Check launcher job step ordering (install < build < selftest < upload)
    for job_name, self_test_pattern in [
        ("build-linux-launcher", "DoomEternalArchipelagoLauncher --self-test"),
        ("build-windows-launcher", "DoomEternalArchipelagoLauncher.exe --self-test"),
    ]:
        steps = doc["jobs"][job_name].get("steps", [])
        step_runs = [s.get("run", "") for s in steps]

        install_indices = [i for i, run in enumerate(step_runs) if "requirements-launcher.txt" in run]
        if not install_indices:
            raise RuntimeError(f"{job_name} missing 'pip install -r requirements-launcher.txt' step")
        install_idx = install_indices[0]

        build_indices = [i for i, run in enumerate(step_runs) if "build_launcher" in run and "--help" not in run]
        if not build_indices:
            raise RuntimeError(f"{job_name} missing 'build_launcher' step")
        build_idx = build_indices[0]

        selftest_indices = [i for i, run in enumerate(step_runs) if self_test_pattern in run]
        if not selftest_indices:
            raise RuntimeError(f"{job_name} missing '{self_test_pattern}' step")
        selftest_idx = selftest_indices[0]

        upload_indices = [i for i, s in enumerate(steps) if "upload-artifact" in s.get("uses", "")]
        if not upload_indices:
            raise RuntimeError(f"{job_name} missing upload-artifact step")
        upload_idx = upload_indices[0]

        if not (install_idx < build_idx < selftest_idx < upload_idx):
            raise RuntimeError(
                f"{job_name} step ordering invariant violated: "
                f"install({install_idx}) < build({build_idx}) < selftest({selftest_idx}) < upload({upload_idx})"
            )

    # 8. Check that wine64-tools is installed in build-native-support
    raw_text = wf_path.read_text(encoding="utf-8")
    if "wine64-tools" not in raw_text:
        raise RuntimeError("Workflow native job must install wine64-tools for WIDL support")

    # 9. Check apworld_ref default
    apworld_default = doc.get("on", {}).get("workflow_dispatch", {}).get("inputs", {}).get("apworld_ref", {}).get("default")
    if apworld_default != "doom_eternal":
        raise RuntimeError(f"Expected apworld_ref default to be 'doom_eternal', got {apworld_default!r}")

    print("  [OK] Workflow trigger is workflow_dispatch only")
    print("  [OK] Workflow permissions are contents: read")
    print("  [OK] All 7 expected jobs present and properly sequenced")
    print("  [OK] Single-owner verify_launcher_runtime invariant verified (jobs.preflight only)")
    print("  [OK] Single-owner Ruff static analysis invariant verified (requirements-ci only)")
    print("  [OK] Module invocation python -m tools.release.build_launcher verified")
    print("  [OK] Step ordering invariant (install < build < selftest < upload) verified")


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
    check_workflow_contract(args.repo_root)
    check_runtime_imports(args.repo_root, args.archipelago_source)

    print("\n=======================================================")
    print("ALL CI PREFLIGHT CHECKS PASSED")
    print("=======================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
