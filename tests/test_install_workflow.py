from types import SimpleNamespace
import hashlib
import json
import multiprocessing
import os
import shutil
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from tests.test_core_distribution import fixture as core_distribution_fixture


import doom_eap.launcher.launcher_controller as launcher_controller_mod
import doom_eap.launcher.launcher_platform as launcher_platform_mod
from doom_eap.launcher.launcher_controller import LauncherController
from doom_eap.launcher.launcher_core import (
    ModCompiler,
    RoomCompiler,
    RoomSnapshot,
    release_identity,
)
from doom_eap.launcher.launcher_integration import IntegratedLaunchWorkflow
from doom_eap.launcher.launcher_platform import (
    WINDOWS_INJECTOR_REQUIRED_MEMBERS,
    DependencyManager,
    DependencySpec,
    InstalledDependency,
    UrlDownloadTransport,
    install_meathook,
    stage_windows_injector_toolchain,
)


def _install_core_fixture(game_root: Path) -> Path:
    runtime = game_root.parent / (game_root.name + "-core")
    runtime.mkdir(parents=True, exist_ok=True)
    core_distribution_fixture(runtime)
    manifest = runtime / "distribution.json"
    with patch("doom_eap.launcher.launcher_platform.detect_doom_processes", return_value=()):
        install_meathook(game_root, None, consent=lambda _: False, local_artifact=manifest)
    return manifest


def _snapshot() -> RoomSnapshot:
    ids = ModCompiler().active_location_ids(False)
    identity = release_identity()
    placements = [
        {
            "location_id": location_id,
            "location_name": f"Location {location_id}",
            "item_id": 1,
            "item_name": "Nothing",
            "recipient_slot": 2,
            "recipient_name": "Self",
            "classification": 0,
            "trap": False,
            "local": True,
        }
        for location_id in ids
    ]
    return RoomSnapshot.from_packets(
        {"seed_name": "install-seed"},
        {
            "team": 1,
            "slot": 2,
            "slot_data": {
                "randomize_chainsaw": False,
                "randomize_dash": False,
                "randomize_first_battery": False,
                "reveal_ap_locations_on_automap": False,
                "bridge_protocol": 4,
                "content_revision": identity["content_revision"],
                "campaign_plan": {},
                "native_generation_fingerprint": "a" * 64,
            },
            "missing_locations": ids[::2],
            "checked_locations": ids[1::2],
            "placements": placements,
        },
    )




class TestSecureDownloadTransport(unittest.TestCase):


    def test_transport_passes_context_and_headers_to_urlopen(self):
        transport = UrlDownloadTransport()
        with patch("urllib.request.urlopen") as mock_urlopen, tempfile.TemporaryDirectory() as tmp_str:
            mock_resp = MagicMock()
            mock_resp.read.side_effect = [b"downloaded_bytes", b""]
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            dest = Path(tmp_str) / "output.bin"
            transport.fetch("https://example.com/asset.zip", dest)

            self.assertTrue(dest.is_file())
            self.assertEqual(dest.read_bytes(), b"downloaded_bytes")
            self.assertEqual(mock_urlopen.call_count, 1)

            req = mock_urlopen.call_args[0][0]
            self.assertEqual(req.full_url, "https://example.com/asset.zip")
            self.assertEqual(req.headers.get("User-agent"), "DoomEternal-AP-Launcher")
            self.assertEqual(mock_urlopen.call_args[1]["timeout"], 60.0)
            self.assertIs(mock_urlopen.call_args[1]["context"], transport.ssl_context)


class TestDependencyAcquisition(unittest.TestCase):
    def test_dependency_manager_acquisition_and_sha_verification(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            root = Path(tmp_str) / "deps"
            archive = Path(tmp_str) / "fake_tool.zip"

            content = b"executable_shell_binary_data"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("Tool.sh", content)

            archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
            spec = DependencySpec(
                name="TestTool",
                version="1.0.0",
                url="https://example.com/tool.zip",
                sha256=archive_sha,
                executable_glob="**/Tool.sh",
                archive_type="zip",
            )

            manager = DependencyManager(root)
            installed = manager.acquire(spec, consent=lambda _s: True, local_artifact=archive)

            self.assertEqual(installed.name, "TestTool")
            self.assertEqual(installed.version, "1.0.0")
            self.assertEqual(installed.artifact_sha256, archive_sha)
            self.assertTrue(Path(installed.executable).is_file())

            # check checksum mismatch rejection
            bad_spec = DependencySpec(
                name="BadTool",
                version="1.0.0",
                url="https://example.com/bad.zip",
                sha256="0" * 64,
                executable_glob="**/Tool.sh",
                archive_type="zip",
            )
            with self.assertRaises(ValueError) as ctx:
                manager.acquire(bad_spec, consent=lambda _s: True, local_artifact=archive)
            self.assertIn("SHA-256 mismatch", str(ctx.exception))





class TestCorePrerequisiteGate(unittest.TestCase):
    def _create_mock_game_root(self, path: Path, with_meathook: bool = False, meathook_bytes: bytes | None = None) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        (path / "DOOMEternalx64vk.exe").write_bytes(b"exe")
        (path / "base").mkdir(parents=True, exist_ok=True)
        (path / "Mods").mkdir(parents=True, exist_ok=True)
        if with_meathook:
            bytes_to_write = meathook_bytes if meathook_bytes is not None else b"mock_meathook_bytes"
            (path / "XINPUT1_3.dll").write_bytes(bytes_to_write)
        return path

    def _create_mock_room_resources(self, client_dir: Path) -> None:
        resources = client_dir / "resources"
        resources.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(resources / "base_mod.zip", "w") as zf:
            zf.writestr("base.txt", "base")
        with zipfile.ZipFile(resources / "room_payloads.zip", "w") as zf:
            zf.writestr("payload.txt", "payload")
        (resources / "room_payload_manifest.json").write_text(
            json.dumps({"schema_version": 1, "payload_format": "zip", "maps": {}}),
            encoding="utf-8",
        )
        (client_dir / "bridge_client.py").write_text("# bridge client stub\n", encoding="utf-8")
        data_dir = client_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        schema_src = Path(__file__).parents[1] / "data" / "options_schema.json"
        if schema_src.is_file():
            shutil.copy(schema_src, data_dir / "options_schema.json")



    def test_install_core_identity_and_transaction_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._create_mock_game_root(Path(directory) / "doom")
            manifest = _install_core_fixture(root)
            before = {name: (root / name).read_bytes() for name in
                      ("sentinel_core.dll", "msimg32.dll", "sentinel-distribution.json")}
            with patch("doom_eap.launcher.launcher_platform.detect_doom_processes", return_value=()):
                result = install_meathook(root, None, consent=lambda _: False)
                self.assertEqual(result.state, "verified")
                with patch("doom_eap.launcher.launcher_platform._atomic_write_bytes",
                           wraps=launcher_platform_mod._atomic_write_bytes) as write:
                    original_write = write._mock_wraps
                    calls = 0
                    def fail_once(path, data):
                        nonlocal calls
                        calls += 1
                        if calls == 2:
                            raise OSError("transaction interrupted")
                        return original_write(path, data)
                    write.side_effect = fail_once
                    with self.assertRaises(OSError):
                        install_meathook(root, None, consent=lambda _: False, local_artifact=manifest)
                self.assertEqual(before, {name: (root / name).read_bytes() for name in before})
                (root / "XINPUT1_3.dll").write_bytes(b"foreign")
                with self.assertRaises(RuntimeError):
                    install_meathook(root, None, consent=lambda _: True, local_artifact=manifest, force_repair=True)
                self.assertEqual((root / "XINPUT1_3.dll").read_bytes(), b"foreign")


    def test_missing_core_distribution_stops_workflow_with_zero_mutation(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            base_dir = Path(tmp_str)
            game_root = self._create_mock_game_root(base_dir / "doom", with_meathook=False)
            app_dir = base_dir / "app"
            state_dir = base_dir / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            self._create_mock_room_resources(app_dir)

            config_path = state_dir / "config.json"
            config_path.write_text(json.dumps({
                "game_root": str(game_root),
                "doom_base_dir": str(game_root / "base"),
            }), encoding="utf-8")

            workflow = IntegratedLaunchWorkflow(
                app_dir,
                state_dir,
                config_path,
                platform_name="linux",
                consent=lambda _spec: False,  # user declines consent
                session_owner=MagicMock(prepare=MagicMock(return_value={"state": "prelaunch_ready", "ready": False})),
            )

            mock_digest = "0" * 64
            with patch.object(RoomCompiler, "__init__", return_value=None), \
                 patch.object(RoomCompiler, "static_content_digest", mock_digest, create=True):
                with self.assertRaises(RuntimeError) as ctx:
                    workflow.execute(_snapshot())

            self.assertIn("compatible Core runtime release", str(ctx.exception))
            # assert zero mutations
            self.assertFalse((game_root / "XINPUT1_3.dll").exists())
            self.assertEqual(list((game_root / "Mods").glob("*.zip")), [])






class TestWindowsToolchainStaging(unittest.TestCase):
    def _create_mock_dep_root(self, root: Path) -> InstalledDependency:
        root.mkdir(parents=True, exist_ok=True)
        for member in WINDOWS_INJECTOR_REQUIRED_MEMBERS:
            member_path = root / member
            member_path.parent.mkdir(parents=True, exist_ok=True)
            member_path.write_bytes(f"content_of_{member}".encode("utf-8"))
        bat_path = root / "EternalModInjector.bat"
        return InstalledDependency(
            "EternalModInjector", "2026-09-04", "mock_sha", "mock_url", str(root), str(bat_path)
        )



    def test_staging_preserves_existing_settings_and_mods(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            dep_root = tmp / "dep"
            dep = self._create_mock_dep_root(dep_root)
            game_root = tmp / "game"
            game_root.mkdir()
            mods_dir = game_root / "Mods"
            mods_dir.mkdir()
            custom_player_mod = mods_dir / "my_custom_skin.zip"
            custom_player_mod.write_bytes(b"custom_skin")

            settings_file = game_root / "EternalModInjector Settings.txt"
            settings_file.write_text(
                ":ASSET_VERSION=2025-01-01\n:AUTO_LAUNCH_GAME=1\n:CUSTOM_OPTION=true\n",
                encoding="utf-8",
            )

            stage_windows_injector_toolchain(dep, game_root)

            # mods folder untouched
            self.assertTrue(custom_player_mod.is_file())
            self.assertEqual(custom_player_mod.read_bytes(), b"custom_skin")

            # settings kept
            content = settings_file.read_text(encoding="utf-8")
            self.assertIn(":ASSET_VERSION=2025-01-01", content)
            self.assertIn(":AUTO_LAUNCH_GAME=0", content)
            self.assertIn(":AUTO_UPDATE=0", content)
            self.assertIn(":CUSTOM_OPTION=true", content)

    def test_staging_missing_required_members_raises_error(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            dep_root = tmp / "dep_corrupted"
            dep_root.mkdir()
            (dep_root / "EternalModInjector.bat").write_bytes(b"batch")
            # missing other 13 files
            dep = InstalledDependency(
                "EternalModInjector", "2026-09-04", "sha", "url", str(dep_root), str(dep_root / "EternalModInjector.bat")
            )
            game_root = tmp / "game"
            game_root.mkdir()
            with self.assertRaises(RuntimeError) as ctx:
                stage_windows_injector_toolchain(dep, game_root)
            self.assertIn("missing", str(ctx.exception).casefold())






class TestWindowsNativeClientLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="doomeap_native_client_test_")
        self.tmp = Path(self.tmp_dir.name)
        self.app_dir = self.tmp / "app"
        self.client_dir = self.app_dir / "client"
        self.client_data = self.client_dir / "data"
        self.client_data.mkdir(parents=True, exist_ok=True)
        schema_src = Path(__file__).resolve().parents[1] / "data" / "options_schema.json"
        if schema_src.is_file():
            shutil.copy2(schema_src, self.client_data / "options_schema.json")
        self.client_exe = self.client_dir / "ap_client.exe"
        self.client_exe.write_bytes(b"MZ_FAKE_CLIENT")

        self.game_root = self.tmp / "Program Files (x86)" / "Steam" / "steamapps" / "common" / "DOOMEternal"
        (self.game_root / "base").mkdir(parents=True, exist_ok=True)
        (self.game_root / "DOOMEternalx64vk.exe").write_bytes(b"MZ_FAKE_DOOM")
        _install_core_fixture(self.game_root)

        self.saves_dir = self.tmp / "saves" / "id Software" / "DOOMEternal" / "base"
        self.saves_dir.mkdir(parents=True, exist_ok=True)

        self.state_dir = self.tmp / "user_state"
        self.config_dir = self.tmp / "user_config"
        self.data_dir = self.tmp / "user_data"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.env_override = {
            "XDG_CONFIG_HOME": str(self.config_dir),
            "XDG_STATE_HOME": str(self.state_dir),
            "XDG_DATA_HOME": str(self.data_dir),
            "APPDATA": str(self.config_dir),
            "LOCALAPPDATA": str(self.state_dir),
        }
        self.old_env = {k: os.environ.get(k) for k in self.env_override}
        for k, v in self.env_override.items():
            os.environ[k] = v

        self.controller = LauncherController(application_dir=self.app_dir)
        self.controller.config = {
            "game_root": str(self.game_root),
            "doom_base_dir": str(self.game_root / "base"),
            "save_games_dir": str(self.saves_dir),
        }

    def tearDown(self):
        self.controller.close()
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp_dir.cleanup()












    def test_room_password_retry_is_transient_and_cancelable(self):
        request = dict(endpoint="localhost:38281", slot="Marine",
                       game_root=str(self.game_root), saves_root=str(self.saves_dir))
        with patch.object(self.controller, "_start_supervisor") as start, \
             patch.object(self.controller, "ensure_ammo_refill_config"):
            self.controller.connect(password="wrong-room-password", **request)
            start.assert_called_once_with({
                "endpoint": "localhost:38281", "slot": "Marine", "password": "wrong-room-password",
            })
            self.controller.state = launcher_controller_mod.LauncherState.FAILED
            self.controller.supervisor = MagicMock()
            self.controller.connect(password="correct-room-password", **request)
            self.controller.supervisor.stop.assert_called_once_with(emit_disconnected=False)
            self.assertEqual(self.controller._pending_connect["password"], "correct-room-password")
            self.controller.disconnect()
            self.assertIsNone(self.controller._pending_connect)
        persisted = self.controller.config_path.read_text()
        self.assertNotIn("wrong-room-password", persisted)
        self.assertNotIn("correct-room-password", persisted)

    def test_retired_dialog_cannot_start_repair_or_confirm_a_different_room(self):
        generation = self.controller.operation_generation
        self.controller.workers.invalidate()
        with patch.object(self.controller.workflow, "for_job") as scoped, \
             patch.object(self.controller, "ensure_ammo_refill_config") as configure:
            self.assertFalse(self.controller.request_repair("repair_game_link", generation=generation))
            self.assertFalse(self.controller.confirm_manual_installation(generation=generation))
            self.assertFalse(self.controller.prepare_setup(generation=generation))
            self.assertFalse(self.controller.reinstall_setup(generation=generation))
            with self.assertRaisesRegex(RuntimeError, "Room changed"):
                self.controller.uninstall_setup(generation=generation)
        scoped.assert_not_called()
        configure.assert_not_called()


    def test_late_doctor_does_not_publish_to_replacement_connection(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()

        def run():
            entered.set()
            self.assertTrue(release.wait(3))
            return SimpleNamespace(document=lambda: {"old": True})

        try:
            with patch.object(launcher_controller_mod, "LauncherDoctor") as doctor, \
                 patch.object(self.controller, "_live_support_diagnostics", return_value={}):
                doctor.return_value.run.side_effect = run
                self.assertTrue(self.controller.request_doctor())
                self.assertTrue(entered.wait(3))
                self.controller.workers.invalidate()
                release.set()
                self.assertTrue(self.controller.workers.submit("sentinel", lambda job: completed.set()))
                self.assertTrue(completed.wait(3))
            self.assertTrue(self.controller.events.empty())
        finally:
            release.set()


    def test_worker_error_path_stops_native_client_without_deadlock(self):
        fake_supervisor = MagicMock()
        fake_supervisor.running = False
        self.controller.supervisor = fake_supervisor

        fake_process = MagicMock()
        fake_process.poll.return_value = None
        self.controller._native_client_process = fake_process

        def worker_call():
            self.controller._worker_event(fake_supervisor, {"type": "error", "message": "server error"})

        thread = threading.Thread(target=worker_call)
        thread.start()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive(), "Worker error handling deadlocked on _lifecycle_lock")
        fake_process.terminate.assert_called_once()
        self.assertIsNone(self.controller._native_client_process)
        self.assertEqual(self.controller.state, launcher_controller_mod.LauncherState.FAILED)



    def test_windows_native_client_stopped_on_disconnect_and_close(self):
        fake_process = MagicMock()
        fake_process.poll.return_value = None
        self.controller._native_client_process = fake_process

        self.controller.disconnect()
        fake_process.terminate.assert_called_once()
        self.assertIsNone(self.controller._native_client_process)

        # re-attach and test close
        fake_process_2 = MagicMock()
        fake_process_2.poll.return_value = None
        self.controller._native_client_process = fake_process_2

        self.controller.close()
        fake_process_2.terminate.assert_called_once()
        self.assertIsNone(self.controller._native_client_process)






class TestSandboxCompatibilityGuard(unittest.TestCase):


    def test_case_3_and_4_sandbox_restored_on_failure_and_exception(self):
        from doom_eap.launcher.launcher_platform import hold_sandbox
        with tempfile.TemporaryDirectory() as tmp_str:
            game_root = Path(tmp_str)
            sandbox_dir = game_root / "doomSandBox"
            sandbox_dir.mkdir()
            sandbox_exe = sandbox_dir / "DOOMSandBox64vk.exe"
            sandbox_exe.write_bytes(b"sandbox_payload_for_exception_test")
            original_sha = hashlib.sha256(sandbox_exe.read_bytes()).hexdigest()

            with self.assertRaises(ZeroDivisionError):
                with hold_sandbox(game_root):
                    _ = 1 / 0

            self.assertTrue(sandbox_exe.is_file())
            self.assertEqual(hashlib.sha256(sandbox_exe.read_bytes()).hexdigest(), original_sha)

    def test_case_5_stale_hold_crash_recovery(self):
        from doom_eap.launcher.launcher_platform import hold_sandbox
        with tempfile.TemporaryDirectory() as tmp_str:
            game_root = Path(tmp_str)
            state_dir = game_root / "state"
            state_dir.mkdir()
            sandbox_dir = game_root / "doomSandBox"
            sandbox_dir.mkdir()
            hold_file = sandbox_dir / "DOOMSandBox64vk.exe.doom_eap_hold"
            hold_file.write_bytes(b"crashed_run_sandbox_data")
            hold_sha = hashlib.sha256(hold_file.read_bytes()).hexdigest()

            tx_file = state_dir / "sandbox_guard_tx.json"
            tx_file.write_text(json.dumps({"sha256": hold_sha, "size": len(b"crashed_run_sandbox_data")}), encoding="utf-8")

            events: list[tuple[str, dict]] = []
            with hold_sandbox(game_root, state_dir=state_dir, event_sink=lambda k, **p: events.append((k, p))):
                pass

            sandbox_exe = sandbox_dir / "DOOMSandBox64vk.exe"
            self.assertTrue(sandbox_exe.is_file())
            self.assertEqual(hashlib.sha256(sandbox_exe.read_bytes()).hexdigest(), hold_sha)
            self.assertTrue(any(p.get("state") == "recovered_stale_hold" for _, p in events))

    def test_case_6_ambiguous_state_fails_closed(self):
        from doom_eap.launcher.launcher_platform import hold_sandbox
        with tempfile.TemporaryDirectory() as tmp_str:
            game_root = Path(tmp_str)
            sandbox_dir = game_root / "doomSandBox"
            sandbox_dir.mkdir()
            (sandbox_dir / "DOOMSandBox64vk.exe").write_bytes(b"exe_data")
            (sandbox_dir / "DOOMSandBox64vk.exe.doom_eap_hold").write_bytes(b"hold_data")

            with self.assertRaises(RuntimeError) as ctx:
                with hold_sandbox(game_root):
                    pass
            self.assertIn("Ambiguous sandbox state", str(ctx.exception))
            self.assertTrue((sandbox_dir / "DOOMSandBox64vk.exe").is_file())
            self.assertTrue((sandbox_dir / "DOOMSandBox64vk.exe.doom_eap_hold").is_file())

    def test_case_7_restoration_integrity_failure(self):
        from doom_eap.launcher.launcher_platform import hold_sandbox
        with tempfile.TemporaryDirectory() as tmp_str:
            game_root = Path(tmp_str)
            sandbox_dir = game_root / "doomSandBox"
            sandbox_dir.mkdir()
            sandbox_exe = sandbox_dir / "DOOMSandBox64vk.exe"
            sandbox_exe.write_bytes(b"original_data")

            with self.assertRaises(RuntimeError) as ctx:
                with hold_sandbox(game_root):
                    hold = sandbox_dir / "DOOMSandBox64vk.exe.doom_eap_hold"
                    hold.write_bytes(b"tampered_data")
            self.assertIn("integrity check failed", str(ctx.exception))




def _acquire_cache_entry_worker(payload):
    """Multiprocessing target: acquire one file dependency from a shared cache root."""
    from pathlib import Path

    from doom_eap.launcher.launcher_platform import DependencyManager, DependencySpec

    manager = DependencyManager(Path(payload["root"]))
    spec = DependencySpec(**payload["spec"])
    installed = manager.acquire(
        spec,
        consent=lambda _s: True,
        local_artifact=Path(payload["artifact"]),
    )
    return installed.executable


class TestWindowsFilesystemCorrective(unittest.TestCase):
    """Cache-hit, cross-process, no-game, and publication coverage for Errno 13."""

    def _file_spec(self, tmp: Path, name="CacheTool", version="1.0"):
        artifact = tmp / f"{name}.dll"
        content = f"{name}_bytes_{version}".encode("utf-8")
        artifact.write_bytes(content)
        spec = DependencySpec(
            name=name,
            version=version,
            url=f"https://example.com/{name}.dll",
            sha256=hashlib.sha256(content).hexdigest(),
            executable_glob=f"{name}.dll",
            archive_type="file",
        )
        return spec, artifact



    def test_cross_process_acquisition_serializes(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            spec, artifact = self._file_spec(tmp)
            payload = {
                "root": str(tmp / "deps"),
                "artifact": str(artifact),
                "spec": {
                    "name": spec.name,
                    "version": spec.version,
                    "url": spec.url,
                    "sha256": spec.sha256,
                    "executable_glob": spec.executable_glob,
                    "archive_type": spec.archive_type,
                },
            }
            context = multiprocessing.get_context("spawn")
            with context.Pool(processes=2) as pool:
                results = pool.map_async(
                    _acquire_cache_entry_worker, [payload, payload]
                ).get(timeout=180)
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])
            self.assertTrue(Path(results[0]).is_file())









if __name__ == "__main__":
    unittest.main()
