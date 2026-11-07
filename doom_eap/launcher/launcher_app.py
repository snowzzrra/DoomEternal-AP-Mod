"""Standalone DOOM Eternal Archipelago launcher entrypoint."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from doom_eap.launcher.launcher_controller import (
    LauncherController,
    LauncherState,
    application_directory,
    bundle_directory,
)
from doom_eap.launcher.ui.strings import text as player_text


def _run_bridge_worker(arguments: list[str]) -> int:
    archipelago_source = os.environ.get("ARCHIPELAGO_SOURCE", "").strip()
    if not getattr(sys, 'frozen', False) and archipelago_source and archipelago_source not in sys.path:
        sys.path.insert(0, archipelago_source)

    from doom_eap.runtime.bridge_client import launch

    launch(*(argument for argument in arguments if argument != "--bridge-worker"))
    return 0


def _self_test_core(arguments):
    if "--core-only" in arguments:
        from doom_eap.contracts.core_distribution import verify_runtime
        try:
            manifest = Path(arguments[arguments.index("--core-runtime") + 1])
            value, _ = verify_runtime(manifest, bootstrap_from_archive=True)
            print(json.dumps({"product": value["product"], "version": value["version"],
                              "build_id": value["build_id"], "mod_versions": value.get("mod_versions"),
                              "mod_version_range": value.get("mod_version_range"),
                              "abi": value["abi"], "gameplay_evidence": "not_exercised"}, sort_keys=True))
            return 0
        except (OSError, RuntimeError, ValueError, KeyError, ImportError) as error:
            print('Core distribution self-test failed: {value_0}'.format(value_0=f'{error}'), file=sys.stderr)
            return 1

def _self_test_ssl():
    try:
        import ssl

        import certifi

        ca_bundle = Path(certifi.where())
        if not ca_bundle.is_file():
            raise RuntimeError('certifi CA bundle file missing: {value_0}'.format(value_0=f'{ca_bundle}'))
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_bundle))
        if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:
            raise RuntimeError('SSL context does not enforce CERT_REQUIRED and check_hostname')
        print('  [OK] certifi CA bundle verified ({value_0} bytes)'.format(value_0=f'{ca_bundle.stat().st_size}'))
    except (OSError, RuntimeError, ValueError, KeyError, ImportError) as e:
        print('  [FAIL] SSL / certifi check failed: {value_0}'.format(value_0=f'{e}'), file=sys.stderr)
        return 1

def _self_test_crypto():
    try:
        from doom_eap.launcher.build_manifest import _KEYS, _aes_gcm_encrypt, decode_manifest
        nonce = bytes(12)
        document = {"files": {}}
        encoded = nonce + _aes_gcm_encrypt(_KEYS[0], nonce, json.dumps(document).encode(), b"build-manifest") + bytes(64)
        if decode_manifest(encoded)[0] != document:
            raise RuntimeError('Build manifest round trip differs')
        print('  [OK] Build manifest authenticated encryption verified')
    except (OSError, RuntimeError, ValueError, KeyError, ImportError) as e:
        print('  [FAIL] Build manifest crypto check failed: {value_0}'.format(value_0=f'{e}'), file=sys.stderr)
        return 1

def _self_test_tag_loadouts(build_tag_devinv_overrides):
    tag_overrides = build_tag_devinv_overrides({}, 'Combat Shotgun')
    expected_tag_prefixes = (
        "e4m1_rig", "e4m2_swamp", "e4m3_mcity",
        "e5m1_spear", "e5m2_earth", "e5m3_hell",
    )
    for prefix in expected_tag_prefixes:
        if not any(k.startswith(prefix) for k in tag_overrides):
            raise RuntimeError('TAG DevInv build missing output for {value_0}'.format(value_0=f'{prefix}'))
    print('  [OK] Bundled TAG DevInv overrides verified ({value_0} declarations)'.format(value_0=f'{len(tag_overrides)}'))

def _self_test_resources():
    try:
        from doom_eap.launcher.launcher_controller import bundle_directory

        bundle_dir = bundle_directory()
        bundled_schema = bundle_dir / "data" / "options_schema.json"
        if not bundled_schema.is_file():
            raise RuntimeError(
                'Bundled options_schema.json missing in bundle root: {value_0} (frozen={value_1})'.format(value_0=f'{bundled_schema}', value_1=f"{getattr(sys, 'frozen', False)}")
            )
        schema_data = json.loads(bundled_schema.read_text(encoding="utf-8"))
        if not isinstance(schema_data, dict):
            raise RuntimeError('Bundled options_schema.json is not a valid JSON object')
        print('  [OK] Bundled options schema verified -> {value_0} ({value_1} bytes)'.format(value_0=f'{bundled_schema}', value_1=f'{bundled_schema.stat().st_size}'))

        bundled_content = bundle_dir / "content"
        bundled_topology = bundled_content / "catalog" / "region_topology.json"
        bundled_global = bundled_content / "global_runtime.json"
        bundled_maps = bundled_content / "maps"
        if not (bundled_topology.is_file() and bundled_global.is_file() and bundled_maps.is_dir()):
            raise RuntimeError(
                'Bundled content catalog incomplete in bundle root: {value_0} (frozen={value_1})'.format(value_0=f'{bundled_content}', value_1=f"{getattr(sys, 'frozen', False)}")
            )
        print('  [OK] Bundled content catalog verified -> {value_0}'.format(value_0=f'{bundled_content}'))

        # Bundled TAG DevInv source verification & compilation smoke test
        from tools.decls.devinv_builder import build_tag_devinv_overrides

        _self_test_tag_loadouts(build_tag_devinv_overrides)
        from doom_eap.content.compiler_identity import load_compiler_source_identity
        compiler_identity = load_compiler_source_identity(bundle_dir, frozen=getattr(sys, 'frozen', False))
        if "doom_eap/launcher/launcher_core.py" not in compiler_identity["source_hashes"]:
            raise RuntimeError('Bundled compiler identity omits launcher_core')
        print('  [OK] Compiler source identity verified ({value_0} modules)'.format(value_0=f"{len(compiler_identity['source_hashes'])}"))
    except (OSError, RuntimeError, ValueError, KeyError, ImportError) as e:
        print('  [FAIL] Bundled resource resolution failed: {value_0}'.format(value_0=f'{e}'), file=sys.stderr)
        return 1
    return bundled_schema


def _self_test_controller_window(fake_app):
    controller = LauncherController(application_dir=fake_app)
    if controller.state != LauncherState.IDLE:
        raise RuntimeError('Unexpected initial controller state: {value_0}'.format(value_0=f'{controller.state}'))
    if controller.session_start_time <= 0:
        raise RuntimeError('Invalid session_start_time in controller')
    print('  [OK] Hermetic LauncherController constructed from bundled resources (state={value_0})'.format(value_0=f'{controller.state.value}'))
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from doom_eap.launcher.launcher_ui import LauncherUI
    app = QApplication.instance()
    if app is None:
        app = QApplication(["launcher_selftest", "-platform", "offscreen"])
    ui = LauncherUI(controller)
    if ui.windowIcon().pixmap(32).isNull():
        raise RuntimeError('Launcher window icon could not be loaded')
    if ui.pages.count() < 4:
        raise RuntimeError(f'Expected at least {4} UI pages, found {ui.pages.count()}')
    ui.timer.stop()
    ui.close()
    ui.deleteLater()
    app.processEvents()
    print('  [OK] Hermetic LauncherUI constructed and verified (pages={value_0}, offscreen)'.format(value_0=f'{ui.pages.count()}'))

def _self_test_window(bundled_schema):
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
                from doom_eap.launcher.launcher_platform import launcher_user_paths
                fake_paths = launcher_user_paths()
                fake_paths.config_dir.mkdir(parents=True, exist_ok=True)
                (fake_paths.config_dir / "launcher.json").write_text(json.dumps({"core_auto_update": False}))
                _self_test_controller_window(fake_app)
            finally:
                for k, v in old_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
    except (OSError, RuntimeError, ValueError, KeyError, ImportError) as e:
        print('  [FAIL] LauncherController / LauncherUI instantiation failed: {value_0}'.format(value_0=f'{e}'), file=sys.stderr)
        return 1

def _self_test_dependency_files(tmp_root, DependencyManager, DependencySpec, publish_file, hashlib):
    incoming = tmp_root / "payload.incoming"
    incoming.write_bytes(b"selftest")
    final = tmp_root / "payload.bin"
    publish_file(incoming, final, operation="selftest_publish")
    if final.read_bytes() != b"selftest" or incoming.exists():
        raise RuntimeError('publish_file round-trip failed')
    print('  [OK] publish_file round-trip verified')
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
        raise RuntimeError('cache hit must not request consent')
    cached = manager.acquire(spec, consent=_refuse_consent)
    if cached.executable != installed.executable:
        raise RuntimeError('dependency cache-hit mismatch')
    if manager.inspect(spec) is None:
        raise RuntimeError('dependency lock not released after cache hit')
    print('  [OK] dependency cache-hit lock release verified')

def _self_test_support_archive(tmp_root, LauncherDoctor, write_support_bundle, time):
    report = LauncherDoctor(config={}).run()
    if not report.diagnostics:
        raise RuntimeError('no-game doctor run produced no diagnostics')
    bundle = tmp_root / "support.zip"
    first = write_support_bundle(
        bundle, report, logs=["self-test"], session_start=time.time()
    )
    second = write_support_bundle(
        bundle, report, logs=["self-test"], session_start=0.0
    )
    if first != bundle or second == bundle or not second.is_file():
        raise RuntimeError('support-bundle unique naming failed')
    print('  [OK] no-game doctor and unique support bundle verified')

def _self_test_save_selection(tmp_root):
    from doom_eap.launcher.launcher_platform import select_saved_games_dir
    extract = tmp_root / "Downloads" / "DoomEternalArchipelago"
    extract.mkdir(parents=True, exist_ok=True)
    canonical = (
        tmp_root / 'Saved Games' / 'id Software' / "DOOMEternal" / "base"
    )
    canonical.mkdir(parents=True, exist_ok=True)
    stale = select_saved_games_dir(
        str(extract), known_base=canonical, app_dir=extract.parent
    )
    if stale.path != canonical or not stale.repaired:
        raise RuntimeError('stale Saved Games config was not self-healed')
    closed = select_saved_games_dir(
        str(extract), known_base=None, app_dir=extract.parent
    )
    if closed.path is not None:
        raise RuntimeError('launcher dir must never be Saved Games authority')
    print('  [OK] Saved Games authority self-heal verified')

def _self_test_filesystem():
    try:
        import hashlib
        import time

        from doom_eap.launcher.launcher_doctor import LauncherDoctor
        from doom_eap.launcher.launcher_injector import DependencyManager, DependencySpec
        from doom_eap.launcher.launcher_platform import publish_file
        from doom_eap.launcher.launcher_support import write_support_bundle

        with tempfile.TemporaryDirectory(prefix="doomeap_selftest_fs_") as tmp_str:
            tmp_root = Path(tmp_str)
            env_override = {
                "XDG_CONFIG_HOME": str(tmp_root / "config"),
                "XDG_STATE_HOME": str(tmp_root / "state"),
                "XDG_DATA_HOME": str(tmp_root / "data"),
                "APPDATA": str(tmp_root / "config"),
                "LOCALAPPDATA": str(tmp_root / "state"),
            }
            for _key, value in env_override.items():
                Path(value).mkdir(parents=True, exist_ok=True)
            old_env = {k: os.environ.get(k) for k in env_override}
            try:
                for k, v in env_override.items():
                    os.environ[k] = v

                _self_test_dependency_files(tmp_root, DependencyManager, DependencySpec, publish_file, hashlib)

                _self_test_support_archive(tmp_root, LauncherDoctor, write_support_bundle, time)

                _self_test_save_selection(tmp_root)
            finally:
                for k, v in old_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
    except (OSError, RuntimeError, ValueError, KeyError, ImportError) as e:
        print('  [FAIL] Filesystem corrective self-test failed: {value_0}'.format(value_0=f'{e}'), file=sys.stderr)
        return 1

def _self_test_assembled_package(is_assembled, mode_launcher_only, app_dir):
    if is_assembled and not mode_launcher_only:
        print("  --> Validating assembled package layout...")
        try:
            client_dir = app_dir / "client" if (app_dir / "client").is_dir() else app_dir
            pkg_schema = client_dir / "data" / "options_schema.json"
            if not pkg_schema.is_file():
                raise RuntimeError('Missing external client options schema at {value_0}'.format(value_0=f'{pkg_schema}'))
            print('    [OK] Package client options schema -> {value_0}'.format(value_0=f'{pkg_schema}'))

            if (app_dir / "doometernal.apworld").is_file():
                print('    [OK] Package APWorld -> {value_0}'.format(value_0=f"{app_dir / 'doometernal.apworld'}"))
            if (client_dir / "resources" / "room_payload_manifest.json").is_file():
                print('    [OK] Package room resources -> {value_0}'.format(value_0=f"{client_dir / 'resources' / 'room_payload_manifest.json'}"))
            if (client_dir / "ap_client.exe").is_file():
                print('    [OK] Package native client -> {value_0}'.format(value_0=f"{client_dir / 'ap_client.exe'}"))
        except (OSError, RuntimeError, ValueError, KeyError, ImportError) as e:
            print('  [FAIL] Assembled package check failed: {value_0}'.format(value_0=f'{e}'), file=sys.stderr)
            return 1

def _self_test_package_layout(mode_package_required, mode_launcher_only):
    app_dir = application_directory()
    is_assembled = (app_dir / "RELEASE_MANIFEST.json").is_file() or (app_dir / "client" / "data" / "options_schema.json").is_file()

    if mode_package_required and not is_assembled:
        print('  [FAIL] Assembled package validation requested, but RELEASE_MANIFEST.json not found in {value_0}'.format(value_0=f'{app_dir}'), file=sys.stderr)
        return 1

    result = _self_test_assembled_package(is_assembled, mode_launcher_only, app_dir)
    if result:
        return result

    print('' + '\n' + '[OK] Launcher self-test passed successfully.')
    return 0

def _run_self_test(arguments: list[str]) -> int:
    """Non-interactive startup and environment self-test for frozen and source launchers."""

    mode_package_required = "--package" in arguments or "package" in arguments
    mode_launcher_only = "--launcher-only" in arguments or "launcher" in arguments

    print("--> Executing DOOM Eternal Archipelago Launcher self-test...")
    if "--core-only" in arguments:
        return _self_test_core(arguments)

    # 1. Verify third-party core packages & certifi CA bundle in bundle_directory
    if _self_test_ssl():
        return 1

    if _self_test_crypto():
        return 1

    # 2. Check bundled resource root (sys._MEIPASS in frozen mode, repo root in source mode)
    bundled_schema = _self_test_resources()
    if bundled_schema == 1:
        return 1

    # 3. Instantiate LauncherController in hermetic fixture using BUNDLED resources
    if _self_test_window(bundled_schema):
        return 1

    # 3b. Windows filesystem corrective smoke: publish helper, dependency
    # cache-hit lock release, no-game doctor, and unique support-bundle names.
    if _self_test_filesystem():
        return 1
    return _self_test_package_layout(mode_package_required, mode_launcher_only)


def _run_ui() -> int:
    if sys.platform.startswith("linux"):
        import faulthandler

        from doom_eap.launcher.launcher_platform import launcher_user_paths
        state = launcher_user_paths().state_dir
        state.mkdir(parents=True, exist_ok=True)
        with (state / "launcher-boot.log").open("w", encoding="utf-8") as boot:
            os.dup2(boot.fileno(), 2)
        faulthandler.enable()
        print('Launcher boot: {value_0}; display={value_1}; wayland={value_2}; qt_platform={value_3}'.format(value_0=f'{sys.version}', value_1=f"{os.environ.get('DISPLAY', '')}", value_2=f"{os.environ.get('WAYLAND_DISPLAY', '')}", value_3=f"{os.environ.get('QT_QPA_PLATFORM', 'auto')}"), file=sys.stderr, flush=True)
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from doom_eap.launcher.launcher_controller import LauncherController
    from doom_eap.launcher.launcher_ui import LauncherUI

    if os.name == "nt":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DoomEternal.Archipelago.Launcher")
    from PySide6.QtCore import QLocale
    from PySide6.QtWidgets import QMessageBox

    from doom_eap.launcher.ui.strings import configure_language, failure_text
    from doom_eap.strings import PlayerError

    application = QApplication(sys.argv[:1])
    application.setApplicationName(player_text('launcher.navigation', 'brand'))
    application.setWindowIcon(QIcon(str(bundle_directory() / "assets/launcher/EternalAP.ico")))
    configure_language(None, QLocale.system().name())
    try:
        controller = LauncherController()
    except PlayerError as error:
        QMessageBox.critical(None, application.applicationName(), failure_text("startup", error))
        return 1
    LauncherUI(controller).run()
    return 0


def main(arguments: list[str] | None = None) -> int:
    parsed = list(sys.argv[1:] if arguments is None else arguments)
    from doom_eap.launcher.launcher_injector import restore_standard_streams
    restore_standard_streams(require_pipes='--bridge-worker' in parsed)
    from doom_eap.content.compiler_identity import load_compiler_source_identity
    frozen = bool(getattr(sys, 'frozen', False))
    root = Path(sys._MEIPASS) if frozen else Path(__file__).resolve().parents[2]
    load_compiler_source_identity(root, frozen=frozen)
    if "--bridge-worker" in parsed:
        return _run_bridge_worker(parsed)
    if "--self-test" in parsed or "--smoke-test" in parsed:
        return _run_self_test(parsed)
    return _run_ui()


if __name__ == "__main__":
    raise SystemExit(main())
