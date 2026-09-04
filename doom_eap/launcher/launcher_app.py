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

        bundled_content = bundle_dir / "content"
        bundled_topology = bundled_content / "catalog" / "region_topology.json"
        bundled_global = bundled_content / "global_runtime.json"
        bundled_maps = bundled_content / "maps"
        if not (bundled_topology.is_file() and bundled_global.is_file() and bundled_maps.is_dir()):
            raise RuntimeError(
                f"Bundled content catalog incomplete in bundle root: {bundled_content} "
                f"(frozen={getattr(sys, 'frozen', False)})"
            )
        print(f"  [OK] Bundled content catalog verified -> {bundled_content}")

        # Bundled TAG DevInv source verification & compilation smoke test
        from tools.decls.devinv_builder import build_tag_devinv_overrides

        tag_overrides = build_tag_devinv_overrides({}, "Combat Shotgun")
        expected_tag_prefixes = (
            "e4m1_rig", "e4m2_swamp", "e4m3_mcity",
            "e5m1_spear", "e5m2_earth", "e5m3_hell",
        )
        for prefix in expected_tag_prefixes:
            if not any(k.startswith(prefix) for k in tag_overrides):
                raise RuntimeError(f"TAG DevInv build missing output for {prefix}")
        print(f"  [OK] Bundled TAG DevInv overrides verified ({len(tag_overrides)} declarations)")
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

                # Hermetic UI construction smoke test (executes __init__, _build, _session_page, etc.)
                os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

                from PySide6.QtWidgets import QApplication
                from doom_eap.launcher.launcher_ui import LauncherUI

                app = QApplication.instance()
                if app is None:
                    app = QApplication(["launcher_selftest", "-platform", "offscreen"])
                ui = LauncherUI(controller)
                if ui.pages.count() < 5:
                    raise RuntimeError(f"Expected at least 5 UI pages, found {ui.pages.count()}")
                ui.timer.stop()
                ui.close()
                ui.deleteLater()
                app.processEvents()
                print(f"  [OK] Hermetic LauncherUI constructed and verified (pages={ui.pages.count()}, offscreen)")
            finally:
                for k, v in old_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
    except Exception as e:
        print(f"  [FAIL] LauncherController / LauncherUI instantiation failed: {e}", file=sys.stderr)
        return 1

    # 3b. Windows filesystem corrective smoke: publish helper, dependency
    # cache-hit lock release, no-game doctor, and unique support-bundle names.
    try:
        import hashlib
        import time

        from doom_eap.launcher.launcher_doctor import LauncherDoctor, write_support_bundle
        from doom_eap.launcher.launcher_platform import DependencyManager, DependencySpec, publish_file

        with tempfile.TemporaryDirectory(prefix="doomeap_selftest_fs_") as tmp_str:
            tmp_root = Path(tmp_str)
            env_override = {
                "XDG_CONFIG_HOME": str(tmp_root / "config"),
                "XDG_STATE_HOME": str(tmp_root / "state"),
                "XDG_DATA_HOME": str(tmp_root / "data"),
                "APPDATA": str(tmp_root / "config"),
                "LOCALAPPDATA": str(tmp_root / "state"),
            }
            for key, value in env_override.items():
                Path(value).mkdir(parents=True, exist_ok=True)
            old_env = {k: os.environ.get(k) for k in env_override}
            try:
                for k, v in env_override.items():
                    os.environ[k] = v

                incoming = tmp_root / "payload.incoming"
                incoming.write_bytes(b"selftest")
                final = tmp_root / "payload.bin"
                publish_file(incoming, final, operation="selftest_publish")
                if final.read_bytes() != b"selftest" or incoming.exists():
                    raise RuntimeError("publish_file round-trip failed")
                print("  [OK] publish_file round-trip verified")

                content = b"selftest_dependency"
                artifact = tmp_root / "SelfTestTool.dll"
                artifact.write_bytes(content)
                spec = DependencySpec(
                    name="SelfTestTool",
                    version="1.0",
                    url="https://example.com/SelfTestTool.dll",
                    sha256=hashlib.sha256(content).hexdigest(),
                    executable_glob="SelfTestTool.dll",
                    archive_type="file",
                )
                manager = DependencyManager(tmp_root / "deps")
                installed = manager.acquire(
                    spec, consent=lambda _s: True, local_artifact=artifact
                )

                def _refuse_consent(_s):
                    raise RuntimeError("cache hit must not request consent")

                cached = manager.acquire(spec, consent=_refuse_consent)
                if cached.executable != installed.executable:
                    raise RuntimeError("dependency cache-hit mismatch")
                if manager.inspect(spec) is None:
                    raise RuntimeError("dependency lock not released after cache hit")
                print("  [OK] dependency cache-hit lock release verified")

                report = LauncherDoctor(config={}).run()
                if not report.diagnostics:
                    raise RuntimeError("no-game doctor run produced no diagnostics")
                bundle = tmp_root / "support.zip"
                first = write_support_bundle(
                    bundle, report, logs=["self-test"], session_start=time.time()
                )
                second = write_support_bundle(
                    bundle, report, logs=["self-test"], session_start=0.0
                )
                if first != bundle or second == bundle or not second.is_file():
                    raise RuntimeError("support-bundle unique naming failed")
                print("  [OK] no-game doctor and unique support bundle verified")
            finally:
                for k, v in old_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
    except Exception as e:
        print(f"  [FAIL] Filesystem corrective self-test failed: {e}", file=sys.stderr)
        return 1
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
