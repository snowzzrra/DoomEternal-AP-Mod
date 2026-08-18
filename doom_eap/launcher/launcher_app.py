"""Standalone DOOM Eternal Archipelago launcher entrypoint."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _run_bridge_worker(arguments: list[str]) -> int:
    archipelago_source = os.environ.get("ARCHIPELAGO_SOURCE", "").strip()
    if archipelago_source and archipelago_source not in sys.path:
        sys.path.insert(0, archipelago_source)

    from doom_eap.runtime.bridge_client import launch

    launch(*(argument for argument in arguments if argument != "--bridge-worker"))
    return 0


def _run_self_test(arguments: list[str]) -> int:
    """Non-interactive startup and environment self-test for frozen and source launchers."""
    import json
    import shutil
    import tempfile

    mode_package_required = "--package" in arguments or "package" in arguments
    mode_launcher_only = "--launcher-only" in arguments or "launcher" in arguments

    print("--> Executing DOOM Eternal Archipelago Launcher self-test...")

    # 1. Verify third-party core packages & certifi CA bundle in bundle_directory
    try:
        import certifi
        import ssl

        ca_bundle = Path(certifi.where())
        if not ca_bundle.is_file():
            raise RuntimeError(f"certifi CA bundle file missing: {ca_bundle}")
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_bundle))
        if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:
            raise RuntimeError("SSL context does not enforce CERT_REQUIRED and check_hostname")
        print(f"  [OK] certifi CA bundle verified ({ca_bundle.stat().st_size} bytes)")
    except Exception as e:
        print(f"  [FAIL] SSL / certifi check failed: {e}", file=sys.stderr)
        return 1

    # 2. Check bundled resource root (sys._MEIPASS in frozen mode, repo root in source mode)
    try:
        from doom_eap.launcher.launcher_controller import (
            LauncherController,
            LauncherState,
            application_directory,
            bundle_directory,
        )

        bundle_dir = bundle_directory()
        bundled_schema = bundle_dir / "data" / "options_schema.json"
        if not bundled_schema.is_file():
            raise RuntimeError(
                f"Bundled options_schema.json missing in bundle root: {bundled_schema} "
                f"(frozen={getattr(sys, 'frozen', False)})"
            )
        schema_data = json.loads(bundled_schema.read_text(encoding="utf-8"))
        if not isinstance(schema_data, dict):
            raise RuntimeError("Bundled options_schema.json is not a valid JSON object")
        print(f"  [OK] Bundled options schema verified -> {bundled_schema} ({bundled_schema.stat().st_size} bytes)")
    except Exception as e:
        print(f"  [FAIL] Bundled resource resolution failed: {e}", file=sys.stderr)
        return 1

    # 3. Instantiate LauncherController in hermetic fixture using BUNDLED resources
    try:
        with tempfile.TemporaryDirectory(prefix="doomeap_selftest_") as tmp_str:
            tmp_root = Path(tmp_str)
            fake_app = tmp_root / "application"
            fake_client_data = fake_app / "client" / "data"
            fake_client_data.mkdir(parents=True)
            shutil.copy2(bundled_schema, fake_client_data / "options_schema.json")

            fake_user_state = tmp_root / "user_state"
            fake_user_config = tmp_root / "user_config"
            fake_user_data = tmp_root / "user_data"
            fake_user_state.mkdir(parents=True)
            fake_user_config.mkdir(parents=True)
            fake_user_data.mkdir(parents=True)

            env_override = {
                "XDG_CONFIG_HOME": str(fake_user_config),
                "XDG_STATE_HOME": str(fake_user_state),
                "XDG_DATA_HOME": str(fake_user_data),
                "APPDATA": str(fake_user_config),
                "LOCALAPPDATA": str(fake_user_state),
            }
            old_env = {k: os.environ.get(k) for k in env_override}
            try:
                for k, v in env_override.items():
                    os.environ[k] = v
                controller = LauncherController(application_dir=fake_app)
                if controller.state != LauncherState.IDLE:
                    raise RuntimeError(f"Unexpected initial controller state: {controller.state}")
                if controller.session_start_time <= 0:
                    raise RuntimeError("Invalid session_start_time in controller")
                print(f"  [OK] Hermetic LauncherController constructed from bundled resources (state={controller.state.value})")
            finally:
                for k, v in old_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
    except Exception as e:
        print(f"  [FAIL] LauncherController instantiation failed: {e}", file=sys.stderr)
        return 1

    # 4. Assembled-package validation (if package present or explicitly requested)
    app_dir = application_directory()
    is_assembled = (app_dir / "RELEASE_MANIFEST.json").is_file() or (app_dir / "client" / "data" / "options_schema.json").is_file()

    if mode_package_required and not is_assembled:
        print(f"  [FAIL] Assembled package validation requested, but RELEASE_MANIFEST.json not found in {app_dir}", file=sys.stderr)
        return 1

    if is_assembled and not mode_launcher_only:
        print("  --> Validating assembled package layout...")
        try:
            client_dir = app_dir / "client" if (app_dir / "client").is_dir() else app_dir
            pkg_schema = client_dir / "data" / "options_schema.json"
            if not pkg_schema.is_file():
                raise RuntimeError(f"Missing external client options schema at {pkg_schema}")
            print(f"    [OK] Package client options schema -> {pkg_schema}")

            if (app_dir / "doometernal.apworld").is_file():
                print(f"    [OK] Package APWorld -> {app_dir / 'doometernal.apworld'}")
            if (client_dir / "resources" / "room_payload_manifest.json").is_file():
                print(f"    [OK] Package room resources -> {client_dir / 'resources' / 'room_payload_manifest.json'}")
            if (client_dir / "ap_client.exe").is_file():
                print(f"    [OK] Package native client -> {client_dir / 'ap_client.exe'}")
        except Exception as e:
            print(f"  [FAIL] Assembled package check failed: {e}", file=sys.stderr)
            return 1

    print("\n[OK] Launcher self-test passed successfully.")
    return 0


def _run_ui() -> int:
    from PySide6.QtWidgets import QApplication

    from doom_eap.launcher.launcher_controller import LauncherController
    from doom_eap.launcher.launcher_ui import LauncherUI

    application = QApplication(sys.argv[:1])
    application.setApplicationName("DOOM Eternal Archipelago")
    LauncherUI(LauncherController()).run()
    return 0


def main(arguments: list[str] | None = None) -> int:
    parsed = list(sys.argv[1:] if arguments is None else arguments)
    if "--bridge-worker" in parsed:
        return _run_bridge_worker(parsed)
    if "--self-test" in parsed or "--smoke-test" in parsed:
        return _run_self_test(parsed)
    return _run_ui()


if __name__ == "__main__":
    raise SystemExit(main())
