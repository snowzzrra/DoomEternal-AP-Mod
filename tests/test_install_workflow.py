import hashlib
import json
import os
import shutil
import ssl
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import certifi

from doom_eap.launcher.launcher_core import LaunchWorkflow, ModCompiler, RoomSnapshot, release_identity
from doom_eap.launcher.launcher_doctor import (
    Diagnostic,
    DoctorReport,
    LauncherDoctor,
    _log_freshness,
    write_support_bundle,
)
from doom_eap.launcher.launcher_integration import IntegratedLaunchWorkflow
from doom_eap.launcher.launcher_platform import (
    DependencyManager,
    DependencySpec,
    PrerequisiteStatus,
    UrlDownloadTransport,
    create_secure_ssl_context,
    probe_meathook,
    probe_runtime_prerequisites,
)


def _snapshot() -> RoomSnapshot:
    ids = ModCompiler().active_location_ids(False)
    identity = release_identity()
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
            },
            "missing_locations": ids[::2],
            "checked_locations": ids[1::2],
        },
    )


class TestInstallWorkflow(unittest.TestCase):
    def test_same_manifest_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            target = Path(tmp_str) / "active"
            workflow = LaunchWorkflow()
            first = workflow.execute(_snapshot(), target)
            before = (target / "seed_manifest.json").stat().st_mtime_ns
            second = workflow.execute(_snapshot(), target)
            self.assertEqual(second.manifest_hash, first.manifest_hash)
            self.assertEqual((target / "seed_manifest.json").stat().st_mtime_ns, before)


class TestSecureDownloadTransport(unittest.TestCase):
    def test_create_secure_ssl_context(self):
        context = create_secure_ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertTrue(Path(certifi.where()).is_file())

    def test_url_download_transport_uses_secure_context(self):
        custom_context = create_secure_ssl_context()
        transport = UrlDownloadTransport(ssl_context=custom_context, timeout=45.0)
        self.assertIs(transport.ssl_context, custom_context)
        self.assertEqual(transport.timeout, 45.0)

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

            # Verify checksum mismatch rejection
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


class TestSupportLogFreshnessAndFailureSurfacing(unittest.TestCase):
    def test_log_freshness_classification(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            log_path = Path(tmp_str) / "test.log"
            log_path.write_text("log content\n")

            now = time.time()
            session_start = now - 60.0  # Session started 1 minute ago

            # 1. Log written during current session (30s ago) -> active_session
            os.utime(log_path, (now - 30.0, now - 30.0))
            freshness, _ = _log_freshness(log_path.stat(), session_start=session_start)
            self.assertEqual(freshness, "active_session")

            # 2. Log written 11 hours before current session -> recent_previous
            eleven_hours_ago = session_start - (11 * 3600)
            os.utime(log_path, (eleven_hours_ago, eleven_hours_ago))
            freshness, _ = _log_freshness(log_path.stat(), session_start=session_start)
            self.assertEqual(freshness, "recent_previous")

            # 3. Log written 5 days before session -> historical_stale
            five_days_ago = session_start - (5 * 86400)
            os.utime(log_path, (five_days_ago, five_days_ago))
            freshness, _ = _log_freshness(log_path.stat(), session_start=session_start)
            self.assertTrue(freshness.startswith("historical_stale"))

    def test_support_bundle_surfaces_last_setup_failure(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            dest = Path(tmp_str) / "support.zip"
            report = DoctorReport("beta.4", (Diagnostic("test", "ok", "passed"),))
            failure_info = {
                "type": "setup_failed",
                "stage": "dependency_acquisition",
                "message": "SSL verification failed",
            }
            write_support_bundle(
                dest,
                report,
                logs=["setup started", "dependency consent required", "setup failed"],
                session_start=time.time(),
                last_setup_failure=failure_info,
            )

            self.assertTrue(dest.is_file())
            with zipfile.ZipFile(dest) as zf:
                self.assertIn("doctor.json", zf.namelist())
                self.assertIn("launcher.log", zf.namelist())
                doc = json.loads(zf.read("doctor.json").decode("utf-8"))
                self.assertIn("last_setup_failure", doc)
                self.assertEqual(doc["last_setup_failure"]["stage"], "dependency_acquisition")
                self.assertEqual(doc["last_setup_failure"]["message"], "SSL verification failed")


class TestMeathookPrerequisiteGate(unittest.TestCase):
    def _create_mock_game_root(self, path: Path, with_meathook: bool = False, meathook_bytes: bytes = b"valid_dll_content") -> Path:
        path.mkdir(parents=True, exist_ok=True)
        (path / "DOOMEternalx64vk.exe").write_bytes(b"exe")
        (path / "base").mkdir(parents=True, exist_ok=True)
        (path / "Mods").mkdir(parents=True, exist_ok=True)
        if with_meathook:
            (path / "XINPUT1_3.dll").write_bytes(meathook_bytes)
        return path

    def _create_mock_room_resources(self, client_dir: Path) -> None:
        resources = client_dir / "resources"
        resources.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(resources / "base_mod.zip", "w") as zf:
            zf.writestr("base.txt", "base")
        with zipfile.ZipFile(resources / "room_payloads.zip", "w") as zf:
            zf.writestr("payload.txt", "payload")
        (client_dir / "bridge_client.py").write_text("# bridge client stub\n", encoding="utf-8")
        data_dir = client_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        schema_src = Path(__file__).parents[1] / "data" / "options_schema.json"
        if schema_src.is_file():
            shutil.copy(schema_src, data_dir / "options_schema.json")

    def test_probe_meathook_status_variations(self):
        self.assertEqual(probe_meathook(None).status, PrerequisiteStatus.MISSING)

        with tempfile.TemporaryDirectory() as tmp_str:
            root = Path(tmp_str)
            # Missing XINPUT1_3.dll
            check = probe_meathook(root)
            self.assertEqual(check.status, PrerequisiteStatus.MISSING)
            self.assertFalse(check.ok)

            # 0-byte XINPUT1_3.dll
            dll = root / "XINPUT1_3.dll"
            dll.write_bytes(b"")
            check_empty = probe_meathook(root)
            self.assertEqual(check_empty.status, PrerequisiteStatus.INVALID)
            self.assertFalse(check_empty.ok)

            # Valid XINPUT1_3.dll
            dll.write_bytes(b"pe_header_meathook_library_data")
            check_valid = probe_meathook(root)
            self.assertEqual(check_valid.status, PrerequisiteStatus.OK)
            self.assertTrue(check_valid.ok)
            self.assertEqual(check_valid.details["status"], "present_unverified")

    def test_probe_runtime_prerequisites_gate(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            game_root = self._create_mock_game_root(Path(tmp_str) / "doom", with_meathook=False)
            app_dir = Path(tmp_str) / "app"
            self._create_mock_room_resources(app_dir)

            # Missing Meathook -> not ok
            prereqs = probe_runtime_prerequisites(game_root, app_dir)
            self.assertFalse(prereqs.ok)
            self.assertEqual(prereqs.meathook.status, PrerequisiteStatus.MISSING)

            # Add Meathook -> ok
            (game_root / "XINPUT1_3.dll").write_bytes(b"meathook_data")
            prereqs_ok = probe_runtime_prerequisites(game_root, app_dir)
            self.assertTrue(prereqs_ok.ok)
            self.assertEqual(prereqs_ok.meathook.status, PrerequisiteStatus.OK)

    def test_execute_fails_closed_without_meathook_and_causes_zero_mutation(self):
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

            # Stage a previous mod (Room A)
            prev_mod = game_root / "Mods" / "DOOMEternalArchipelago_roomA.zip"
            prev_mod.write_bytes(b"previous_room_mod_content")
            receipt_path = state_dir / "launcher_setup.json"
            receipt_path.write_text(json.dumps({
                "manifest_hash": "hash_room_A",
                "staged_mod": str(prev_mod),
                "staged_sha256": hashlib.sha256(b"previous_room_mod_content").hexdigest(),
                "adapter_state": "applied",
            }), encoding="utf-8")

            prev_mod_mtime = prev_mod.stat().st_mtime_ns
            receipt_mtime = receipt_path.stat().st_mtime_ns

            workflow = IntegratedLaunchWorkflow(
                app_dir,
                state_dir,
                config_path,
                platform_name="linux",
            )

            with self.assertRaises(RuntimeError) as ctx:
                workflow.execute(_snapshot())

            self.assertIn("Meathook runtime is not installed", str(ctx.exception))

            # Zero-mutation assertion: previous mod untouched, receipt untouched, no new mod in Mods
            self.assertTrue(prev_mod.is_file())
            self.assertEqual(prev_mod.read_bytes(), b"previous_room_mod_content")
            self.assertEqual(prev_mod.stat().st_mtime_ns, prev_mod_mtime)
            self.assertEqual(receipt_path.stat().st_mtime_ns, receipt_mtime)

            mod_files = list((game_root / "Mods").glob("*.zip"))
            self.assertEqual(len(mod_files), 1)
            self.assertEqual(mod_files[0], prev_mod)

    def test_install_state_vs_play_readiness_separation(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            base_dir = Path(tmp_str)
            game_root = self._create_mock_game_root(base_dir / "doom", with_meathook=True)
            app_dir = base_dir / "app"
            state_dir = base_dir / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            self._create_mock_room_resources(app_dir)

            config_path = state_dir / "config.json"
            config_path.write_text(json.dumps({
                "game_root": str(game_root),
                "doom_base_dir": str(game_root / "base"),
            }), encoding="utf-8")

            snapshot = _snapshot()
            manifest = LaunchWorkflow().manifest_for(snapshot)

            # Build and stage valid room mod matching manifest
            staged = game_root / "Mods" / "DOOMEternalArchipelago_active.zip"
            with zipfile.ZipFile(staged, "w") as zf:
                zf.writestr("seed_manifest.json", json.dumps({"manifest_hash": manifest.manifest_hash}))

            staged_sha = hashlib.sha256(staged.read_bytes()).hexdigest()
            receipt_path = state_dir / "launcher_setup.json"
            receipt_path.write_text(json.dumps({
                "manifest_hash": manifest.manifest_hash,
                "staged_mod": str(staged),
                "staged_sha256": staged_sha,
                "adapter_state": "applied",
            }), encoding="utf-8")

            workflow = IntegratedLaunchWorkflow(app_dir, state_dir, config_path, platform_name="linux")

            # 1. When Meathook is present -> already_installed, readiness=ready
            state_with_dll = workflow.install_state(snapshot)
            self.assertEqual(state_with_dll.state, "already_installed")
            self.assertEqual(state_with_dll.readiness, "ready")

            # 2. When Meathook is removed -> already_installed (package intact), but readiness=blocked
            (game_root / "XINPUT1_3.dll").unlink()
            state_without_dll = workflow.install_state(snapshot)
            self.assertEqual(state_without_dll.state, "already_installed")
            self.assertEqual(state_without_dll.readiness, "blocked")
            self.assertIn("Meathook", state_without_dll.readiness_reason)

    def test_doctor_report_fails_closed_when_meathook_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            game_root = self._create_mock_game_root(Path(tmp_str) / "doom", with_meathook=False)
            doctor = LauncherDoctor(config={"game_root": str(game_root)})

            # 1. Missing Meathook -> report.ok is False
            report_missing = doctor.run()
            self.assertFalse(report_missing.ok)
            meathook_diag = next(d for d in report_missing.diagnostics if d.key == "meathook")
            self.assertEqual(meathook_diag.status, "missing")

            # 2. Add Meathook -> report.ok is True
            (game_root / "XINPUT1_3.dll").write_bytes(b"meathook_library")
            report_present = doctor.run()
            self.assertTrue(report_present.ok)
            meathook_diag_ok = next(d for d in report_present.diagnostics if d.key == "meathook")
            self.assertEqual(meathook_diag_ok.status, "ok")

    def test_pre_launch_gate_blocks_launch_when_meathook_missing(self):
        import doom_eap.launcher.launcher_controller as lc

        with tempfile.TemporaryDirectory() as tmp_str:
            game_root = self._create_mock_game_root(Path(tmp_str) / "doom", with_meathook=False)
            app_dir = Path(tmp_str) / "app"
            self._create_mock_room_resources(app_dir)

            controller = lc.LauncherController(application_dir=app_dir)
            try:
                controller.config = {"game_root": str(game_root), "doom_base_dir": str(game_root / "base")}

                # Blocked launch without Meathook
                with self.assertRaises(RuntimeError) as ctx:
                    controller.launch_game()
                self.assertIn("Meathook runtime is not installed", str(ctx.exception))

                # Allowed launch once Meathook is present
                (game_root / "XINPUT1_3.dll").write_bytes(b"meathook_dll")
                with patch.object(lc, "launch_doom_via_steam") as mock_launch:
                    mock_launch.return_value = "steam://rungameid/782330"
                    url = controller.launch_game()
                    self.assertEqual(url, "steam://rungameid/782330")
                    mock_launch.assert_called_once()
            finally:
                controller.close()


if __name__ == "__main__":
    unittest.main()
