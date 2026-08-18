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
    import tempfile

    print("--> Executing DOOM Eternal Archipelago Launcher self-test...")

    # 1. Verify third-party core packages & certifi CA bundle
    try:
        import ssl
        import certifi

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

    # 2. Check application directory & packaged options schema
    try:
        from doom_eap.launcher.launcher_controller import LauncherController, LauncherState, application_directory

        app_dir = application_directory()
        client_dir = app_dir / "client" if (app_dir / "client").is_dir() else app_dir
        options_schema_path = client_dir / "data" / "options_schema.json"

        if not options_schema_path.is_file():
            repo_schema = app_dir / "data" / "options_schema.json"
            if repo_schema.is_file():
                options_schema_path = repo_schema
            else:
                raise RuntimeError(f"options_schema.json not found at {options_schema_path}")
        print(f"  [OK] options schema located -> {options_schema_path}")
    except Exception as e:
        print(f"  [FAIL] Application directory / schema resolution failed: {e}", file=sys.stderr)
        return 1

    # 3. Instantiate LauncherController in hermetic environment
    try:
        with tempfile.TemporaryDirectory(prefix="doomeap_selftest_") as tmp_str:
            fake_user_state = Path(tmp_str) / "user_state"
            fake_user_config = Path(tmp_str) / "user_config"
            fake_user_data = Path(tmp_str) / "user_data"
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
                controller = LauncherController(application_dir=app_dir)
                if controller.state != LauncherState.IDLE:
                    raise RuntimeError(f"Unexpected initial controller state: {controller.state}")
                if controller.session_start_time <= 0:
                    raise RuntimeError("Invalid session_start_time in controller")
                print(f"  [OK] LauncherController constructed successfully (state={controller.state.value})")
            finally:
                for k, v in old_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
    except Exception as e:
        print(f"  [FAIL] LauncherController instantiation failed: {e}", file=sys.stderr)
        return 1

    # 4. Optional checks if executed from assembled package
    package_root = app_dir if (app_dir / "RELEASE_MANIFEST.json").is_file() else app_dir.parent
    if (package_root / "RELEASE_MANIFEST.json").is_file():
        print("  [OK] Assembled package detected:")
        if (package_root / "doometernal.apworld").is_file():
            print("    [OK] doometernal.apworld present")
        if (package_root / "client" / "resources" / "room_payload_manifest.json").is_file():
            print("    [OK] room compiler resources present")

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
