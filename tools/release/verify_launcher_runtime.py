#!/usr/bin/env python3
"""Preflight verification for launcher and standalone AP client runtime imports.

Verifies path precedence, stub resolution, third-party dependency availability,
and bridge client importability without building, starting the UI, or invoking
interactive setup prompts.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
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

    # Configure sys.path in the exact precedence order used by PyInstaller
    ordered_paths = [str(root), str(standalone_runtime), str(ap_source)]
    remaining_paths = [p for p in sys.path if p not in ordered_paths]
    sys.path[:] = ordered_paths + remaining_paths

    importlib.invalidate_caches()
    for mod in [
        "ModuleUpdate", "MultiServer", "worlds", "Utils", "NetUtils", "CommonClient",
        "doom_eap", "doom_eap.runtime.bridge_client", "doom_eap.launcher.launcher_app",
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
    env_keys = ("DOOM_AP_CONFIG_FILE", "DOOM_AP_APPLICATION_DIR", "ARCHIPELAGO_SOURCE")
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
            sys.modules.pop("doom_eap.launcher.launcher_app", None)
            launcher_mod = importlib.import_module("doom_eap.launcher.launcher_app")
            print(f"  [OK] doom_eap.launcher.launcher_app -> {launcher_mod.__file__}")

        finally:
            # Restore environment
            for k, v in orig_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            # Clear bridge module so caller does not retain temporary paths
            sys.modules.pop("doom_eap.runtime.bridge_client", None)

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
