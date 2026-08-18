#!/usr/bin/env python3
"""Preflight verification for launcher and standalone AP client runtime imports.

Verifies path precedence, stub resolution, third-party dependency availability,
strict Ruff F821/F822/F823 static name correctness, hermetic LauncherController
construction from bundled resources, and non-interactive launcher self-test execution.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def check_undefined_names(repo_root: Path) -> None:
    """Run strict Ruff static undefined-name checks (F821/F822/F823)."""
    print("--> Checking for undefined names (F821/F822/F823 via Ruff)...")
    target_dirs = [repo_root / "doom_eap", repo_root / "tools/release"]
    ruff_bin = shutil.which("ruff")
    if not ruff_bin:
        # Also try invoking via sys.executable -m ruff
        res_probe = subprocess.run([sys.executable, "-m", "ruff", "--version"], capture_output=True)
        if res_probe.returncode == 0:
            cmd = [sys.executable, "-m", "ruff", "check", "--select", "F821,F822,F823", *(str(p) for p in target_dirs)]
        else:
            raise RuntimeError(
                "Ruff static analyzer is required for F821/F822/F823 undefined-name verification; "
                "install requirements-ci.txt"
            )
    else:
        cmd = [ruff_bin, "check", "--select", "F821,F822,F823", *(str(p) for p in target_dirs)]

    res = subprocess.run(cmd, capture_output=True, text=True, cwd=repo_root)
    if res.returncode != 0:
        raise RuntimeError(f"Undefined name check failed:\n{res.stdout}\n{res.stderr}")
    print("  [OK] Ruff undefined name verification passed (F821, F822, F823)")


def check_syntax(repo_root: Path) -> None:
    """Verify syntax compilation for all runtime and release Python files."""
    print("--> Checking Python syntax compilation...")
    target_dirs = [repo_root / "doom_eap", repo_root / "tools/release"]
    for target in target_dirs:
        for py_file in target.rglob("*.py"):
            if "__pycache__" in py_file.parts:
                continue
            source = py_file.read_text(encoding="utf-8")
            compile(source, str(py_file), "exec")
    print("  [OK] Python syntax compilation verified for runtime and release modules")


def verify_runtime(archipelago_source: Path, repo_root: Path | None = None) -> None:
    root = (repo_root or REPO_ROOT).resolve()
    ap_source = archipelago_source.resolve()
    standalone_runtime = (root / "packaging/standalone_runtime").resolve()

    if not root.is_dir():
        raise RuntimeError(f"Repository root directory does not exist: {root}")
    if not standalone_runtime.is_dir():
        raise RuntimeError(f"Standalone runtime directory does not exist: {standalone_runtime}")
    if not (ap_source / "CommonClient.py").is_file():
        raise RuntimeError(f"Invalid Archipelago source (missing CommonClient.py): {ap_source}")

    # 0. Strict Static Undefined Name & Syntax Checks
    check_undefined_names(root)
    check_syntax(root)

    # Configure sys.path in the exact precedence order used by PyInstaller
    ordered_paths = [str(root), str(standalone_runtime), str(ap_source)]
    remaining_paths = [p for p in sys.path if p not in ordered_paths]
    sys.path[:] = ordered_paths + remaining_paths

    importlib.invalidate_caches()
    for mod in [
        "ModuleUpdate", "MultiServer", "worlds", "Utils", "NetUtils", "CommonClient",
        "doom_eap", "doom_eap.runtime.bridge_client", "doom_eap.launcher.launcher_app",
        "doom_eap.launcher.launcher_controller",
    ]:
        sys.modules.pop(mod, None)

    # 1. Verify Third-Party Dependencies
    required_deps = [
        "colorama",
        "websockets",
        "yaml",
        "pathspec",
        "typing_extensions",
        "certifi",
        "platformdirs",
    ]
    print("--> Checking third-party dependencies...")
    for dep in required_deps:
        try:
            mod = importlib.import_module(dep)
            ver = getattr(mod, "__version__", "unknown")
            print(f"  [OK] {dep} ({ver})")
        except ImportError as e:
            raise RuntimeError(f"Missing required third-party dependency: {dep} ({e})") from e

    import ssl

    import certifi
    ca_path = Path(certifi.where())
    if not ca_path.is_file():
        raise RuntimeError(f"certifi CA bundle does not exist at {ca_path}")
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_path))
    if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:
        raise RuntimeError("SSL context failed to enforce CERT_REQUIRED and check_hostname")
    print(f"  [OK] certifi CA bundle verified ({ca_path.stat().st_size} bytes) -> {ca_path}")

    # 2. Verify Stub Path Precedence
    print("--> Checking standalone runtime stub precedence...")
    stub_checks = [
        ("ModuleUpdate", standalone_runtime),
        ("MultiServer", standalone_runtime),
        ("worlds", standalone_runtime),
    ]
    for mod_name, expected_parent in stub_checks:
        mod = importlib.import_module(mod_name)
        mod_file = getattr(mod, "__file__", None)
        if not mod_file:
            raise RuntimeError(f"Module {mod_name} has no __file__ attribute")
        mod_path = Path(mod_file).resolve()
        if not _is_relative_to(mod_path, expected_parent):
            raise RuntimeError(
                f"Stub precedence failed for {mod_name}: resolved to {mod_path} instead of under {expected_parent}"
            )
        print(f"  [OK] {mod_name} stub -> {mod_path}")

    # 3. Verify Archipelago Client Core Modules
    print("--> Checking Archipelago client core module resolution...")
    ap_core_checks = [
        ("Utils", ap_source),
        ("NetUtils", ap_source),
        ("CommonClient", ap_source),
    ]
    for mod_name, expected_parent in ap_core_checks:
        mod = importlib.import_module(mod_name)
        mod_file = getattr(mod, "__file__", None)
        if not mod_file:
            raise RuntimeError(f"Module {mod_name} has no __file__ attribute")
        mod_path = Path(mod_file).resolve()
        if not _is_relative_to(mod_path, expected_parent):
            raise RuntimeError(
                f"Archipelago core module {mod_name} resolved to {mod_path} instead of under {expected_parent}"
            )
        print(f"  [OK] {mod_name} -> {mod_path}")

    # 4. Hermetic Bridge Client Verification
    print("--> Checking DOOM Eternal bridge client importability (hermetic setup)...")
    env_keys = (
        "DOOM_AP_CONFIG_FILE", "DOOM_AP_APPLICATION_DIR", "ARCHIPELAGO_SOURCE",
        "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA",
    )
    orig_env = {k: os.environ.get(k) for k in env_keys}

    with tempfile.TemporaryDirectory(prefix="doomeap_preflight_hermetic_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        fake_doom = tmp_dir / "fake-doom"
        (fake_doom / "base" / "classicwads").mkdir(parents=True, exist_ok=True)
        (fake_doom / "DOOMEternalx64vk.exe").write_bytes(b"")
        fake_saves = tmp_dir / "fake-saves" / "id Software" / "DOOMEternal" / "base"
        fake_saves.mkdir(parents=True, exist_ok=True)
        fake_steam = tmp_dir / "fake-steam" / "userdata" / "12345678" / "782330" / "remote"
        fake_steam.mkdir(parents=True, exist_ok=True)
        fake_app = tmp_dir / "app"
        fake_app.mkdir(parents=True, exist_ok=True)
        fake_config = tmp_dir / "ap_config.json"
        fake_config.write_text(
            json.dumps({
                "doom_base_dir": str(fake_doom),
                "save_games_dir": str(fake_saves),
                "steam_remote_dir": str(fake_steam),
                "steam_id3": 12345678,
            }, indent=2) + "\n",
            encoding="utf-8",
        )

        try:
            os.environ["DOOM_AP_CONFIG_FILE"] = str(fake_config)
            os.environ["DOOM_AP_APPLICATION_DIR"] = str(fake_app)
            os.environ["ARCHIPELAGO_SOURCE"] = str(ap_source)

            # Invalidate bridge_client cache and re-import under hermetic environment
            sys.modules.pop("doom_eap.runtime.bridge_client", None)
            bridge_mod = importlib.import_module("doom_eap.runtime.bridge_client")

            # Verify hermetic paths are active
            if bridge_mod.CONFIG_FILE.resolve() != fake_config.resolve():
                raise RuntimeError(
                    f"Bridge client used unexpected config: {bridge_mod.CONFIG_FILE} != {fake_config}"
                )
            if Path(bridge_mod.DOOM_BASE_DIR).resolve() != (fake_doom / "base").resolve():
                raise RuntimeError(
                    f"Bridge client DOOM_BASE_DIR mismatch: {bridge_mod.DOOM_BASE_DIR} != {fake_doom / 'base'}"
                )
            if Path(bridge_mod.SAVE_GAMES_DIR).resolve() != fake_saves.resolve():
                raise RuntimeError(
                    f"Bridge client SAVE_GAMES_DIR mismatch: {bridge_mod.SAVE_GAMES_DIR} != {fake_saves}"
                )
            if Path(bridge_mod.STEAM_REMOTE_DIR).resolve() != fake_steam.resolve():
                raise RuntimeError(
                    f"Bridge client STEAM_REMOTE_DIR mismatch: {bridge_mod.STEAM_REMOTE_DIR} != {fake_steam}"
                )
            if bridge_mod.STEAM_ID3 != 12345678:
                raise RuntimeError(
                    f"Bridge client STEAM_ID3 mismatch: {bridge_mod.STEAM_ID3} != 12345678"
                )

            print(f"  [OK] doom_eap.runtime.bridge_client -> {bridge_mod.__file__}")
            print(f"  [OK] Hermetic DOOM_BASE_DIR -> {bridge_mod.DOOM_BASE_DIR}")
            print(f"  [OK] Hermetic SAVE_GAMES_DIR -> {bridge_mod.SAVE_GAMES_DIR}")
            print(f"  [OK] Hermetic STEAM_REMOTE_DIR -> {bridge_mod.STEAM_REMOTE_DIR}")

            # 5. Verify Launcher App Module
            print("--> Checking DOOM Eternal launcher app importability...")
            launcher_mod = importlib.import_module("doom_eap.launcher.launcher_app")
            print(f"  [OK] doom_eap.launcher.launcher_app -> {launcher_mod.__file__}")

            # 6. Verify Frozen Mode Identity Resolution
            print("--> Checking bridge runtime identity in simulated frozen environment...")
            from doom_eap.runtime.bridge_client import resolve_bridge_identity
            with tempfile.TemporaryDirectory(prefix="doomeap_preflight_frozen_") as frozen_dir_str:
                frozen_dir = Path(frozen_dir_str)
                missing_module = frozen_dir / "_MEI99999/doom_eap/runtime/bridge_client.py"
                missing_app = frozen_dir / "app"
                _b_file, b_sha, b_rev = resolve_bridge_identity(
                    application_dir=missing_app,
                    repo_root=root,
                    module_file=missing_module,
                    is_frozen=True,
                )
                if not b_sha or len(b_sha) != 64 or not b_rev.startswith("mission-unified-"):
                    raise RuntimeError(
                        f"Frozen mode identity resolution failed: sha={b_sha}, rev={b_rev}"
                    )
                print(f"  [OK] Frozen bridge identity -> sha256={b_sha[:12]}... revision={b_rev}")

            # 7. Hermetic LauncherController Construction from explicit fixture
            print("--> Checking hermetic LauncherController construction...")
            fixture_app = tmp_dir / "fixture_app"
            fixture_client_data = fixture_app / "client" / "data"
            fixture_client_data.mkdir(parents=True, exist_ok=True)
            schema_source = root / "data" / "options_schema.json"
            if not schema_source.is_file():
                raise RuntimeError(f"Canonical schema source missing at {schema_source}")
            shutil.copy2(schema_source, fixture_client_data / "options_schema.json")

            fake_user_state = tmp_dir / "user_state"
            fake_user_config = tmp_dir / "user_config"
            fake_user_data = tmp_dir / "user_data"
            fake_user_state.mkdir(parents=True, exist_ok=True)
            fake_user_config.mkdir(parents=True, exist_ok=True)
            fake_user_data.mkdir(parents=True, exist_ok=True)

            os.environ["XDG_CONFIG_HOME"] = str(fake_user_config)
            os.environ["XDG_STATE_HOME"] = str(fake_user_state)
            os.environ["XDG_DATA_HOME"] = str(fake_user_data)
            os.environ["APPDATA"] = str(fake_user_config)
            os.environ["LOCALAPPDATA"] = str(fake_user_state)

            from doom_eap.launcher.launcher_controller import LauncherController, LauncherState
            controller = LauncherController(application_dir=fixture_app)
            if controller.state != LauncherState.IDLE:
                raise RuntimeError(f"LauncherController initial state unexpected: {controller.state}")
            if controller.session_start_time <= 0:
                raise RuntimeError("LauncherController session_start_time was not initialized")
            print(f"  [OK] LauncherController constructed successfully (state={controller.state.value})")

            # 8. Non-interactive Launcher Self-Test (Launcher-Only Mode)
            print("--> Checking non-interactive launcher self-test mode...")
            test_exit_code = launcher_mod.main(["--self-test", "--launcher-only"])
            if test_exit_code != 0:
                raise RuntimeError(f"launcher_app.main(['--self-test', '--launcher-only']) returned non-zero code {test_exit_code}")
            print("  [OK] launcher_app --self-test returned code 0")

        finally:
            # Restore environment
            for k, v in orig_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            # Clear bridge and launcher modules so caller does not retain temporary paths
            sys.modules.pop("doom_eap.runtime.bridge_client", None)
            sys.modules.pop("doom_eap.launcher.launcher_controller", None)

    print("\nStandalone runtime preflight verification: ALL CHECKS PASSED.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify launcher standalone runtime import graph.")
    parser.add_argument("--archipelago-source", type=Path, required=True, help="Path to Archipelago source root")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help="Path to DoomEternal-AP-Mod repo root")
    args = parser.parse_args()

    verify_runtime(args.archipelago_source, args.repo_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
