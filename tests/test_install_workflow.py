import hashlib
import io
import json
import os
import shutil
import ssl
import tarfile
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import certifi

import doom_eap.launcher.launcher_controller as launcher_controller_mod
from doom_eap.launcher.launcher_controller import LauncherController
from doom_eap.launcher.launcher_core import (
    LaunchWorkflow,
    ModCompiler,
    RoomCompiler,
    RoomSnapshot,
    release_identity,
)
from doom_eap.launcher.launcher_doctor import (
    Diagnostic,
    DoctorReport,
    LauncherDoctor,
    _log_freshness,
    write_support_bundle,
)
from doom_eap.launcher.launcher_integration import IntegratedLaunchWorkflow
from doom_eap.launcher.launcher_platform import (
    WINDOWS_INJECTOR_REQUIRED_MEMBERS,
    WINDOWS_MOD_INJECTOR,
    DependencyManager,
    DependencySpec,
    InstalledDependency,
    PrerequisiteStatus,
    UrlDownloadTransport,
    WindowsModInjectorAdapter,
    create_secure_ssl_context,
    install_meathook,
    is_transient_download_error,
    probe_meathook,
    probe_runtime_prerequisites,
    stage_windows_injector_toolchain,
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

    def test_dependency_manager_direct_file_support(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            root = Path(tmp_str) / "deps"
            file_artifact = Path(tmp_str) / "sample.dll"
            file_content = b"sample_dll_bytes_12345"
            file_artifact.write_bytes(file_content)
            file_sha = hashlib.sha256(file_content).hexdigest()

            spec = DependencySpec(
                name="DirectFileTool",
                version="2.0",
                url="https://example.com/sample.dll",
                sha256=file_sha,
                executable_glob="sample.dll",
                archive_type="file",
            )

            manager = DependencyManager(root)
            installed = manager.acquire(spec, consent=lambda _s: True, local_artifact=file_artifact)
            self.assertEqual(installed.name, "DirectFileTool")
            self.assertEqual(installed.version, "2.0")
            self.assertEqual(installed.artifact_sha256, file_sha)
            self.assertTrue(Path(installed.executable).is_file())
            self.assertEqual(Path(installed.executable).read_bytes(), file_content)

            # Idempotent re-acquisition from cache
            reinstalled = manager.acquire(spec, consent=lambda _s: False)
            self.assertEqual(reinstalled.executable, installed.executable)

            # Corrupted cache entry rejection
            Path(installed.executable).write_bytes(b"corrupted_bytes")
            with self.assertRaises(PermissionError):
                manager.acquire(spec, consent=lambda _s: False)


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

    def test_probe_meathook_status_variations(self):
        self.assertEqual(probe_meathook(None).status, PrerequisiteStatus.MISSING)

        with tempfile.TemporaryDirectory() as tmp_str:
            root = Path(tmp_str)
            # 1. Missing XINPUT1_3.dll
            check_missing = probe_meathook(root)
            self.assertEqual(check_missing.status, PrerequisiteStatus.MISSING)
            self.assertFalse(check_missing.ok)

            # 2. 0-byte XINPUT1_3.dll
            dll = root / "XINPUT1_3.dll"
            dll.write_bytes(b"")
            check_empty = probe_meathook(root)
            self.assertEqual(check_empty.status, PrerequisiteStatus.INVALID)
            self.assertFalse(check_empty.ok)

            # 3. Different hash XINPUT1_3.dll -> INCOMPATIBLE
            dll.write_bytes(b"random_other_xinput_content")
            check_incompatible = probe_meathook(root)
            self.assertEqual(check_incompatible.status, PrerequisiteStatus.INCOMPATIBLE)
            self.assertFalse(check_incompatible.ok)
            self.assertEqual(check_incompatible.details["status"], "incompatible")

            # 4. Verified official hash Meathook v7.2 -> OK
            mock_spec = DependencySpec("Meathook", "7.2", "https://example.com/meathook", hashlib.sha256(b"mock_v72_dll").hexdigest(), "XINPUT1_3.dll", "file")
            with patch("doom_eap.launcher.launcher_platform.MEATHOOK", mock_spec):
                dll.write_bytes(b"mock_v72_dll")
                check_valid = probe_meathook(root)
                self.assertEqual(check_valid.status, PrerequisiteStatus.OK)
                self.assertTrue(check_valid.ok)
                self.assertEqual(check_valid.details["status"], "compatible")
                self.assertEqual(check_valid.details["identity"], "verified")

    def test_probe_runtime_prerequisites_gate(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            game_root = self._create_mock_game_root(Path(tmp_str) / "doom", with_meathook=False)
            app_dir = Path(tmp_str) / "app"
            self._create_mock_room_resources(app_dir)

            # Missing Meathook -> not ok
            prereqs = probe_runtime_prerequisites(game_root, app_dir)
            self.assertFalse(prereqs.ok)
            self.assertEqual(prereqs.meathook.status, PrerequisiteStatus.MISSING)

            # Incompatible Meathook -> not ok
            (game_root / "XINPUT1_3.dll").write_bytes(b"incompatible_dll")
            prereqs_bad = probe_runtime_prerequisites(game_root, app_dir)
            self.assertFalse(prereqs_bad.ok)
            self.assertEqual(prereqs_bad.meathook.status, PrerequisiteStatus.INCOMPATIBLE)

            # Verified Meathook -> ok
            mock_spec = DependencySpec("Meathook", "7.2", "https://example.com/meathook", hashlib.sha256(b"official_dll").hexdigest(), "XINPUT1_3.dll", "file")
            with patch("doom_eap.launcher.launcher_platform.MEATHOOK", mock_spec):
                (game_root / "XINPUT1_3.dll").write_bytes(b"official_dll")
                prereqs_ok = probe_runtime_prerequisites(game_root, app_dir)
                self.assertTrue(prereqs_ok.ok)
                self.assertEqual(prereqs_ok.meathook.status, PrerequisiteStatus.OK)

    def test_install_meathook_lifecycle_and_repair_backup(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            base = Path(tmp_str)
            game_root = self._create_mock_game_root(base / "doom", with_meathook=False)
            state_dir = base / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            dep_manager = DependencyManager(state_dir / "dependencies")

            fake_v72_bytes = b"verified_meathook_v72_binary_payload"
            fake_v72_sha = hashlib.sha256(fake_v72_bytes).hexdigest()
            local_meathook_artifact = base / "official_XINPUT1_3.dll"
            local_meathook_artifact.write_bytes(fake_v72_bytes)

            mock_spec = DependencySpec("Meathook", "7.2", "https://example.com/meathook", fake_v72_sha, "XINPUT1_3.dll", "file")
            with patch("doom_eap.launcher.launcher_platform.MEATHOOK", mock_spec):
                # 1. Missing -> Install
                result_install = install_meathook(
                    game_root,
                    dep_manager,
                    state_dir=state_dir,
                    consent=lambda _s: True,
                    local_artifact=local_meathook_artifact,
                )
                self.assertEqual(result_install.state, "installed")
                self.assertEqual(result_install.ownership, "launcher_installed")
                self.assertEqual(result_install.sha256, fake_v72_sha)
                self.assertTrue((game_root / "XINPUT1_3.dll").is_file())
                self.assertEqual((game_root / "XINPUT1_3.dll").read_bytes(), fake_v72_bytes)

                # 2. Matching -> Idempotent verified no-op
                mtime_before = (game_root / "XINPUT1_3.dll").stat().st_mtime_ns
                result_verified = install_meathook(
                    game_root,
                    dep_manager,
                    state_dir=state_dir,
                    consent=lambda _s: False,
                )
                self.assertEqual(result_verified.state, "verified")
                self.assertEqual(result_verified.ownership, "preexisting_verified")
                self.assertEqual((game_root / "XINPUT1_3.dll").stat().st_mtime_ns, mtime_before)

                # 3. Foreign / Incompatible DLL -> needs_repair without force_repair
                (game_root / "XINPUT1_3.dll").write_bytes(b"foreign_old_mod_dll")
                foreign_sha = hashlib.sha256(b"foreign_old_mod_dll").hexdigest()
                result_check = install_meathook(
                    game_root,
                    dep_manager,
                    state_dir=state_dir,
                    consent=lambda _s: False,
                    force_repair=False,
                )
                self.assertEqual(result_check.state, "needs_repair")
                self.assertEqual(result_check.ownership, "unverified_foreign")
                self.assertEqual(result_check.sha256, foreign_sha)
                # Unchanged
                self.assertEqual((game_root / "XINPUT1_3.dll").read_bytes(), b"foreign_old_mod_dll")

                # 4. Force repair -> Back up foreign DLL and replace with verified v7.2
                result_repair = install_meathook(
                    game_root,
                    dep_manager,
                    state_dir=state_dir,
                    consent=lambda _s: True,
                    local_artifact=local_meathook_artifact,
                    force_repair=True,
                )
                self.assertEqual(result_repair.state, "repaired")
                self.assertEqual(result_repair.ownership, "launcher_replaced")
                self.assertEqual(result_repair.sha256, fake_v72_sha)
                self.assertEqual((game_root / "XINPUT1_3.dll").read_bytes(), fake_v72_bytes)
                self.assertTrue(Path(result_repair.backup_path).is_file())
                self.assertEqual(Path(result_repair.backup_path).read_bytes(), b"foreign_old_mod_dll")

                # Backup metadata check
                meta_path = Path(result_repair.backup_path).parent / "metadata.json"
                self.assertTrue(meta_path.is_file())
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                self.assertEqual(metadata["sha256"], foreign_sha)
                self.assertEqual(metadata["replacement_version"], "7.2")

    def test_one_click_workflow_executes_meathook_before_room_mod_and_injector(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            base_dir = Path(tmp_str)
            game_root = self._create_mock_game_root(base_dir / "doom", with_meathook=False)
            app_dir = base_dir / "app"
            state_dir = base_dir / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            self._create_mock_room_resources(app_dir)

            fake_v72_bytes = b"meathook_v72_payload"
            fake_v72_sha = hashlib.sha256(fake_v72_bytes).hexdigest()
            local_meathook = base_dir / "mock_meathook.dll"
            local_meathook.write_bytes(fake_v72_bytes)

            # Also provide dummy linux mod injector dependency
            inj_archive = base_dir / "injector.tar.gz"
            with tarfile.open(inj_archive, "w:gz") as tar:
                tar_info = tarfile.TarInfo("EternalModInjectorShell.sh")
                tar_info.size = len(b"#!/bin/sh\nexit 0\n")
                tar.addfile(tar_info, io.BytesIO(b"#!/bin/sh\nexit 0\n"))
            inj_sha = hashlib.sha256(inj_archive.read_bytes()).hexdigest()
            mock_inj_spec = DependencySpec("EternalModInjectorShell", "6.66-rev3.12", "https://example.com/inj.tar.gz", inj_sha, "**/EternalModInjectorShell.sh", "tar.gz")

            config_path = state_dir / "config.json"
            config_path.write_text(json.dumps({
                "game_root": str(game_root),
                "doom_base_dir": str(game_root / "base"),
                "meathook_dll": str(local_meathook),
                "eternal_basher_archive": str(inj_archive),
            }), encoding="utf-8")

            events_recorded: list[tuple[str, dict[str, object]]] = []
            workflow = IntegratedLaunchWorkflow(
                app_dir,
                state_dir,
                config_path,
                platform_name="linux",
                event_sink=lambda kind, payload: events_recorded.append((kind, payload)),
                consent=lambda _s: True,
            )

            mock_spec = DependencySpec("Meathook", "7.2", "https://example.com/meathook", fake_v72_sha, "XINPUT1_3.dll", "file")
            with patch("doom_eap.launcher.launcher_platform.MEATHOOK", mock_spec), \
                 patch("doom_eap.launcher.launcher_platform.LINUX_MOD_INJECTOR", mock_inj_spec), \
                 patch("doom_eap.launcher.launcher_integration.LINUX_MOD_INJECTOR", mock_inj_spec), \
                 patch("doom_eap.launcher.launcher_platform.LinuxModManagerAdapter.activate") as mock_activate:
                from doom_eap.launcher.launcher_platform import AdapterResult
                mock_activate.return_value = AdapterResult(state="applied", message="ok", command=["mock"])

                fake_generated = base_dir / "generated.zip"
                manifest = LaunchWorkflow().manifest_for(_snapshot())
                with zipfile.ZipFile(fake_generated, "w") as zf:
                    zf.writestr("seed_manifest.json", json.dumps({"manifest_hash": manifest.manifest_hash}))

                with patch.object(RoomCompiler, "__init__", return_value=None), \
                     patch.object(RoomCompiler, "build", return_value=fake_generated):
                    record = workflow.execute(_snapshot())
                    self.assertEqual(record.adapter_state, "applied")

                    # Verify exact ordering: game_link_installed before room mod compilation / staging
                    event_names = [e[0] for e in events_recorded]
                    self.assertIn("game_link_installed", event_names)
                    self.assertIn("mod_building", event_names)
                    self.assertIn("mod_staged", event_names)
                    self.assertIn("injector_started", event_names)

                    gl_idx = event_names.index("game_link_installed")
                    build_idx = event_names.index("mod_building")
                    staged_idx = event_names.index("mod_staged")
                    self.assertLess(gl_idx, build_idx)
                    self.assertLess(build_idx, staged_idx)

                    # Verify Meathook DLL is physically present and verified
                    self.assertTrue((game_root / "XINPUT1_3.dll").is_file())
                    self.assertEqual((game_root / "XINPUT1_3.dll").read_bytes(), fake_v72_bytes)

    def test_declined_consent_stops_workflow_with_zero_mutation(self):
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
                consent=lambda _spec: False,  # User declines consent
            )

            with self.assertRaises(RuntimeError) as ctx:
                workflow.execute(_snapshot())

            self.assertIn("Game Link download was not approved", str(ctx.exception))
            # Assert zero mutations
            self.assertFalse((game_root / "XINPUT1_3.dll").exists())
            self.assertEqual(list((game_root / "Mods").glob("*.zip")), [])


class TestDoctorAndPreLaunchGate(unittest.TestCase):
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

    def test_doctor_report_fails_closed_when_meathook_is_missing_or_incompatible(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            game_root = self._create_mock_game_root(Path(tmp_str) / "doom", with_meathook=False)
            doctor = LauncherDoctor(config={"game_root": str(game_root)})

            # 1. Missing Meathook -> report.ok is False, repair action offered
            report_missing = doctor.run()
            self.assertFalse(report_missing.ok)
            meathook_diag = next(d for d in report_missing.diagnostics if d.key == "meathook")
            self.assertEqual(meathook_diag.status, "missing")
            actions = doctor.repair_actions()
            self.assertTrue(any(a.action_id == "install_game_link" for a in actions))

            # 2. Incompatible Meathook -> report.ok is False, repair action offered
            (game_root / "XINPUT1_3.dll").write_bytes(b"foreign_dll")
            report_incompat = doctor.run()
            self.assertFalse(report_incompat.ok)
            meathook_diag_inc = next(d for d in report_incompat.diagnostics if d.key == "meathook")
            self.assertEqual(meathook_diag_inc.status, "incompatible")
            actions_inc = doctor.repair_actions()
            self.assertTrue(any(a.action_id == "repair_game_link" for a in actions_inc))

            # 3. Add Verified Meathook -> report.ok is True
            mock_spec = DependencySpec("Meathook", "7.2", "https://example.com/meathook", hashlib.sha256(b"official_dll").hexdigest(), "XINPUT1_3.dll", "file")
            with patch("doom_eap.launcher.launcher_platform.MEATHOOK", mock_spec):
                (game_root / "XINPUT1_3.dll").write_bytes(b"official_dll")
                report_present = doctor.run()
                self.assertTrue(report_present.ok)
                meathook_diag_ok = next(d for d in report_present.diagnostics if d.key == "meathook")
                self.assertEqual(meathook_diag_ok.status, "ok")
                self.assertEqual(meathook_diag_ok.details["identity"], "verified")

    def test_pre_launch_gate_blocks_launch_when_meathook_missing_or_incompatible(self):
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
                self.assertIn("Game Link runtime is not installed", str(ctx.exception))

                # Blocked launch with incompatible Meathook
                (game_root / "XINPUT1_3.dll").write_bytes(b"bad_dll")
                with self.assertRaises(RuntimeError) as ctx:
                    controller.launch_game()
                self.assertIn("does not match supported Meathook v7.2", str(ctx.exception))

                # Allowed launch once Meathook is verified
                mock_spec = DependencySpec("Meathook", "7.2", "https://example.com/meathook", hashlib.sha256(b"good_dll").hexdigest(), "XINPUT1_3.dll", "file")
                with patch("doom_eap.launcher.launcher_platform.MEATHOOK", mock_spec):
                    (game_root / "XINPUT1_3.dll").write_bytes(b"good_dll")
                    with patch.object(lc, "launch_doom_via_steam") as mock_launch:
                        mock_launch.return_value = "steam://rungameid/782330"
                        url = controller.launch_game()
                        self.assertEqual(url, "steam://rungameid/782330")
                        mock_launch.assert_called_once()
            finally:
                controller.close()


class TestUrlDownloadTransportRetries(unittest.TestCase):
    def test_transient_error_retries_and_succeeds(self):
        import urllib.error
        transport = UrlDownloadTransport(max_retries=2, backoff_base=0.01)
        mock_response = MagicMock()
        mock_response.__enter__.return_value = io.BytesIO(b"payload_data")
        mock_response.__exit__.return_value = False

        with patch("urllib.request.urlopen") as mock_urlopen, tempfile.TemporaryDirectory() as tmp_str:
            dest = Path(tmp_str) / "file.zip"
            mock_urlopen.side_effect = [
                urllib.error.HTTPError("https://example.com", 503, "Service Unavailable", {}, None),
                mock_response,
            ]
            transport.fetch("https://example.com/file.zip", dest)
            self.assertTrue(dest.is_file())
            self.assertEqual(dest.read_bytes(), b"payload_data")
            self.assertEqual(mock_urlopen.call_count, 2)

    def test_non_transient_error_fails_immediately(self):
        import urllib.error
        transport = UrlDownloadTransport(max_retries=2, backoff_base=0.01)
        with patch("urllib.request.urlopen") as mock_urlopen, tempfile.TemporaryDirectory() as tmp_str:
            dest = Path(tmp_str) / "file.zip"
            mock_urlopen.side_effect = urllib.error.HTTPError("https://example.com", 404, "Not Found", {}, None)
            with self.assertRaises(urllib.error.HTTPError):
                transport.fetch("https://example.com/file.zip", dest)
            self.assertEqual(mock_urlopen.call_count, 1)

    def test_transient_download_error_classification(self):
        import socket
        import urllib.error
        self.assertTrue(is_transient_download_error(urllib.error.HTTPError("url", 503, "msg", {}, None)))
        self.assertTrue(is_transient_download_error(urllib.error.HTTPError("url", 429, "msg", {}, None)))
        self.assertTrue(is_transient_download_error(urllib.error.URLError(socket.gaierror("dns"))))
        self.assertFalse(is_transient_download_error(urllib.error.HTTPError("url", 404, "msg", {}, None)))
        self.assertFalse(is_transient_download_error(urllib.error.URLError(ssl.SSLError("cert"))))

    def test_windows_specs(self):
        self.assertEqual(WINDOWS_MOD_INJECTOR.name, "EternalModInjector")
        self.assertEqual(WINDOWS_MOD_INJECTOR.version, "2026-08-18")
        self.assertEqual(WINDOWS_MOD_INJECTOR.archive_type, "zip")
        self.assertEqual(
            WINDOWS_MOD_INJECTOR.sha256,
            "94d2e04783800e983222f90b8eb304d02fc216e43c3a71f39cd324f5f1970a84",
        )
        self.assertEqual(WINDOWS_MOD_INJECTOR.url, "https://gamebanana.com/dl/1788872")
        self.assertEqual(WINDOWS_MOD_INJECTOR.executable_glob, "**/EternalModInjector.bat")
        self.assertEqual(len(WINDOWS_INJECTOR_REQUIRED_MEMBERS), 14)
        self.assertIn("EternalModInjector.bat", WINDOWS_INJECTOR_REQUIRED_MEMBERS)
        self.assertIn("EternalModManager.exe", WINDOWS_INJECTOR_REQUIRED_MEMBERS)
        self.assertIn("base/BlangParser.dll", WINDOWS_INJECTOR_REQUIRED_MEMBERS)
        self.assertIn("base/DEternal_loadMods.exe", WINDOWS_INJECTOR_REQUIRED_MEMBERS)


class TestWindowsToolchainStaging(unittest.TestCase):
    def _create_mock_dep_root(self, root: Path) -> InstalledDependency:
        root.mkdir(parents=True, exist_ok=True)
        for member in WINDOWS_INJECTOR_REQUIRED_MEMBERS:
            member_path = root / member
            member_path.parent.mkdir(parents=True, exist_ok=True)
            member_path.write_bytes(f"content_of_{member}".encode("utf-8"))
        bat_path = root / "EternalModInjector.bat"
        return InstalledDependency(
            "EternalModInjector", "2026-08-18", "mock_sha", "mock_url", str(root), str(bat_path)
        )

    def test_staging_on_clean_game_root(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            dep_root = tmp / "dep"
            dep = self._create_mock_dep_root(dep_root)
            game_root = tmp / "game"
            game_root.mkdir()
            state_dir = tmp / "state"

            bat = stage_windows_injector_toolchain(dep, game_root, state_dir=state_dir)
            self.assertTrue(bat.is_file())
            self.assertTrue((game_root / "EternalModInjector.bat").is_file())
            self.assertTrue((game_root / "EternalModManager.exe").is_file())
            self.assertTrue((game_root / "base" / "DEternal_loadMods.exe").is_file())
            self.assertTrue((game_root / "Mods").is_dir())

            settings = game_root / "EternalModInjector Settings.txt"
            self.assertTrue(settings.is_file())
            content = settings.read_text(encoding="utf-8")
            self.assertIn(":AUTO_LAUNCH_GAME=0", content)
            self.assertIn(":AUTO_UPDATE=0", content)
            self.assertNotIn(":HAS_READ_FIRST_TIME=1", content)

    def test_staging_preserves_identical_files_and_backs_up_differing_files(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            dep_root = tmp / "dep"
            dep = self._create_mock_dep_root(dep_root)
            game_root = tmp / "game"
            game_root.mkdir()
            state_dir = tmp / "state"

            # Pre-create one identical file and one differing file
            (game_root / "base").mkdir(parents=True, exist_ok=True)
            (game_root / "EternalModInjector.bat").write_bytes(b"content_of_EternalModInjector.bat")  # identical
            differing_file = game_root / "base" / "DEternal_loadMods.exe"
            differing_file.write_bytes(b"old_different_loader_bytes")

            stage_windows_injector_toolchain(dep, game_root, state_dir=state_dir)

            # Check differing file was replaced with verified member
            self.assertEqual(differing_file.read_bytes(), b"content_of_base/DEternal_loadMods.exe")

            # Check backup directory was created for differing file
            backup_root = state_dir / "repair-backups" / "windows-injector"
            self.assertTrue(backup_root.is_dir())
            backup_dirs = list(backup_root.glob("*"))
            self.assertEqual(len(backup_dirs), 1)
            backed_up = backup_dirs[0] / "base" / "DEternal_loadMods.exe"
            self.assertTrue(backed_up.is_file())
            self.assertEqual(backed_up.read_bytes(), b"old_different_loader_bytes")

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

            # Mods folder untouched
            self.assertTrue(custom_player_mod.is_file())
            self.assertEqual(custom_player_mod.read_bytes(), b"custom_skin")

            # Settings preserved
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
            # Missing other 13 files
            dep = InstalledDependency(
                "EternalModInjector", "2026-08-18", "sha", "url", str(dep_root), str(dep_root / "EternalModInjector.bat")
            )
            game_root = tmp / "game"
            game_root.mkdir()
            with self.assertRaises(RuntimeError) as ctx:
                stage_windows_injector_toolchain(dep, game_root)
            self.assertIn("missing", str(ctx.exception).casefold())


class TestWindowsModInjectorAdapter(unittest.TestCase):
    def _create_mock_dep_root(self, root: Path) -> InstalledDependency:
        root.mkdir(parents=True, exist_ok=True)
        for member in WINDOWS_INJECTOR_REQUIRED_MEMBERS:
            member_path = root / member
            member_path.parent.mkdir(parents=True, exist_ok=True)
            member_path.write_bytes(f"content_of_{member}".encode("utf-8"))
        bat_path = root / "EternalModInjector.bat"
        return InstalledDependency(
            "EternalModInjector", "2026-08-18", "mock_sha", "mock_url", str(root), str(bat_path)
        )

    def test_adapter_launches_batch_waits_for_exit_and_requests_confirmation_yes(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            game_root = tmp / "doom"
            game_root.mkdir()
            (game_root / "Mods").mkdir()
            mod_zip = tmp / "DoomEternalArchipelago-123.zip"
            with zipfile.ZipFile(mod_zip, "w") as zf:
                zf.writestr("test.txt", "mod")

            dep_root = tmp / "dep"
            dep = self._create_mock_dep_root(dep_root)

            call_log: list[str] = []
            events: list[tuple[str, dict[str, object]]] = []

            class FakeProcess:
                def __init__(self):
                    self.returncode = 0

                def wait(self):
                    call_log.append("process_wait")
                    return 0

            def mock_opener(cmd, cwd):
                call_log.append(f"opened:{Path(cmd[3]).name} in {cwd}")
                return FakeProcess()

            def mock_confirmer():
                call_log.append("confirmer_called")
                return True

            def mock_event_sink(kind, **payload):
                events.append((kind, payload))

            adapter = WindowsModInjectorAdapter(
                dep,
                state_dir=tmp / "state",
                opener=mock_opener,
                confirmer=mock_confirmer,
                event_sink=mock_event_sink,
            )

            result = adapter.activate(game_root, mod_zip)

            self.assertEqual(result.state, "applied")
            self.assertEqual(result.returncode, 0)
            self.assertEqual(
                call_log,
                [f"opened:EternalModInjector.bat in {game_root}", "process_wait", "confirmer_called"],
            )
            event_names = [e[0] for e in events]
            self.assertEqual(event_names, ["injector_started", "injector_closed"])
            self.assertTrue((game_root / "Mods" / "DoomEternalArchipelago-123.zip").is_file())
            self.assertEqual(result.details.get("installation_mode"), "windows_injector_assisted")

    def test_adapter_confirmation_no_results_in_manual_install_required(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            game_root = tmp / "doom"
            game_root.mkdir()
            (game_root / "Mods").mkdir()
            mod_zip = tmp / "mod.zip"
            with zipfile.ZipFile(mod_zip, "w") as zf:
                zf.writestr("test.txt", "mod")

            dep_root = tmp / "dep"
            dep = self._create_mock_dep_root(dep_root)

            events: list[str] = []

            class FakeProcess:
                returncode = 0

                def wait(self):
                    return 0

            adapter = WindowsModInjectorAdapter(
                dep,
                state_dir=tmp / "state",
                opener=lambda cmd, cwd: FakeProcess(),
                confirmer=lambda: False,
                event_sink=lambda kind, **payload: events.append(kind),
            )

            result = adapter.activate(game_root, mod_zip)
            self.assertEqual(result.state, "manual_install_required")
            self.assertIn("installation_declined", events)

    def test_adapter_handles_nonzero_exit_code_and_opener_failure(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            game_root = tmp / "doom"
            game_root.mkdir()
            (game_root / "Mods").mkdir()
            mod_zip = tmp / "mod.zip"
            with zipfile.ZipFile(mod_zip, "w") as zf:
                zf.writestr("test.txt", "mod")

            dep_root = tmp / "dep"
            dep = self._create_mock_dep_root(dep_root)

            # Nonzero exit code
            class FailedProcess:
                returncode = 2

                def wait(self):
                    return 2

            adapter_nonzero = WindowsModInjectorAdapter(
                dep,
                state_dir=tmp / "state",
                opener=lambda cmd, cwd: FailedProcess(),
                confirmer=lambda: True,
            )
            res_nonzero = adapter_nonzero.activate(game_root, mod_zip)
            self.assertEqual(res_nonzero.state, "manual_install_required")
            self.assertEqual(res_nonzero.returncode, 2)

            # Opener error
            adapter_opener_err = WindowsModInjectorAdapter(
                dep,
                state_dir=tmp / "state",
                opener=MagicMock(side_effect=OSError("Access denied")),
                confirmer=lambda: True,
            )
            res_open_err = adapter_opener_err.activate(game_root, mod_zip)
            self.assertEqual(res_open_err.state, "manual_install_required")
            self.assertIn("Could not launch", res_open_err.message)


class TestWindowsEndToEndAndDoctor(unittest.TestCase):
    def _create_mock_game_root(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        (path / "DOOMEternalx64vk.exe").write_bytes(b"exe")
        (path / "base").mkdir(parents=True, exist_ok=True)
        (path / "Mods").mkdir(parents=True, exist_ok=True)
        (path / "XINPUT1_3.dll").write_bytes(b"mock_meathook")
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

    def _create_mock_injector_zip(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w") as zf:
            for member in WINDOWS_INJECTOR_REQUIRED_MEMBERS:
                zf.writestr(member, f"content_{member}")
        return destination

    def test_windows_workflow_and_coordinator_with_confirmation_yes(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            app_dir = tmp / "app"
            state_dir = tmp / "state"
            game_root = self._create_mock_game_root(tmp / "doom")
            self._create_mock_room_resources(app_dir)

            injector_zip = tmp / "injector.zip"
            self._create_mock_injector_zip(injector_zip)
            injector_sha = hashlib.sha256(injector_zip.read_bytes()).hexdigest()
            spec_injector = DependencySpec(
                "EternalModInjector", "2026-08-18", "https://example.com/emi", injector_sha, "**/EternalModInjector.bat", "zip"
            )

            meathook_sha = hashlib.sha256(b"mock_meathook").hexdigest()
            spec_meathook = DependencySpec(
                "Meathook", "7.2", "https://example.com/mh", meathook_sha, "XINPUT1_3.dll", "file"
            )

            config_path = state_dir / "launcher_config.json"
            state_dir.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps({
                    "game_root": str(game_root),
                    "doom_base_dir": str(game_root / "base"),
                    "eternal_mod_injector_archive": str(injector_zip),
                }),
                encoding="utf-8",
            )

            emitted_events: list[str] = []

            def sink(kind: str, payload: dict[str, object]) -> None:
                emitted_events.append(kind)

            workflow = IntegratedLaunchWorkflow(
                app_dir,
                state_dir,
                config_path,
                platform_name="windows",
                event_sink=sink,
                consent=lambda _s: True,
                confirmation=lambda: True,
            )

            fake_generated = tmp / "generated.zip"
            manifest = LaunchWorkflow().manifest_for(_snapshot())
            with zipfile.ZipFile(fake_generated, "w") as zf:
                zf.writestr("seed_manifest.json", json.dumps({"manifest_hash": manifest.manifest_hash}))

            class FakeProcess:
                returncode = 0

                def wait(self):
                    return 0

            with patch("doom_eap.launcher.launcher_platform.WINDOWS_MOD_INJECTOR", spec_injector), \
                 patch("doom_eap.launcher.launcher_integration.WINDOWS_MOD_INJECTOR", spec_injector), \
                 patch("doom_eap.launcher.launcher_platform.MEATHOOK", spec_meathook), \
                 patch.object(RoomCompiler, "__init__", return_value=None), \
                 patch.object(RoomCompiler, "build", return_value=fake_generated), \
                 patch("subprocess.Popen", return_value=FakeProcess()):
                record = workflow.execute(_snapshot())
                state = workflow.install_state(_snapshot())

            self.assertEqual(record.adapter_state, "applied")
            self.assertIn("room_validated", emitted_events)
            self.assertIn("mod_building", emitted_events)
            self.assertIn("runtime_config_ready", emitted_events)
            self.assertIn("mod_staged", emitted_events)
            self.assertIn("injector_started", emitted_events)
            self.assertIn("injector_closed", emitted_events)

            self.assertEqual(state.state, "already_installed")
            self.assertEqual(state.readiness, "ready")

    def test_manual_fallback_completion_and_doctor_flow(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            app_dir = tmp / "app"
            state_dir = tmp / "state"
            game_root = self._create_mock_game_root(tmp / "doom")
            self._create_mock_room_resources(app_dir)

            meathook_sha = hashlib.sha256(b"mock_meathook").hexdigest()
            spec_meathook = DependencySpec(
                "Meathook", "7.2", "https://example.com/mh", meathook_sha, "XINPUT1_3.dll", "file"
            )

            config_path = state_dir / "launcher_config.json"
            state_dir.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps({
                    "game_root": str(game_root),
                    "doom_base_dir": str(game_root / "base"),
                }),
                encoding="utf-8",
            )

            workflow = IntegratedLaunchWorkflow(
                app_dir,
                state_dir,
                config_path,
                platform_name="windows",
                consent=lambda _s: False,  # Declines download
            )

            fake_generated = tmp / "generated.zip"
            manifest = LaunchWorkflow().manifest_for(_snapshot())
            with zipfile.ZipFile(fake_generated, "w") as zf:
                zf.writestr("seed_manifest.json", json.dumps({"manifest_hash": manifest.manifest_hash}))

            with patch("doom_eap.launcher.launcher_platform.MEATHOOK", spec_meathook), \
                 patch.object(RoomCompiler, "__init__", return_value=None), \
                 patch.object(RoomCompiler, "build", return_value=fake_generated):
                record = workflow.execute(_snapshot())

                self.assertEqual(record.adapter_state, "manual_install_required")
                state_before = workflow.install_state(_snapshot())
                self.assertEqual(state_before.state, "install_needed")

                # Doctor fails closed before confirmation
                doctor = LauncherDoctor(
                    config={"game_root": str(game_root)},
                    paths=MagicMock(state_dir=state_dir, config_dir=tmp, data_dir=tmp),
                )
                report_before = doctor.run()
                self.assertFalse(report_before.ok)

                # Player follows manual guide and clicks "I completed manual installation"
                manual_record = workflow.confirm_manual_installation(_snapshot())
                self.assertEqual(manual_record.adapter_state, "applied")
                self.assertEqual(manual_record.installation_mode, "manual_fallback")

                state_after = workflow.install_state(_snapshot())
                self.assertEqual(state_after.state, "already_installed")
                self.assertEqual(state_after.readiness, "ready")

                # Doctor now reports OK
                report_after = doctor.run()
                self.assertTrue(report_after.ok)


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
        mh_bytes = b"MZ_FAKE_MEATHOOK"
        mh_hash = hashlib.sha256(mh_bytes).hexdigest()
        (self.game_root / "XINPUT1_3.dll").write_bytes(mh_bytes)
        self.spec_meathook = DependencySpec(
            "Meathook", "7.2", "https://example.com/mh", mh_hash, "XINPUT1_3.dll", "file"
        )
        self.meathook_patcher = patch("doom_eap.launcher.launcher_platform.MEATHOOK", self.spec_meathook)
        self.meathook_patcher.start()

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
        self.meathook_patcher.stop()
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp_dir.cleanup()

    def test_windows_native_client_correct_command_and_cwd(self):
        self.controller.connected_room = True
        fake_process = MagicMock()
        fake_process.poll.return_value = None

        with patch("subprocess.Popen", return_value=fake_process) as mock_popen:
            started = self.controller._ensure_native_client(platform="nt")
            self.assertTrue(started)
            mock_popen.assert_called_once_with(
                [str(self.client_exe), str(self.game_root.resolve())],
                cwd=str(self.game_root.resolve()),
                creationflags=0x08000000,
            )

    def test_windows_native_client_idempotency(self):
        self.controller.connected_room = True
        fake_process = MagicMock()
        fake_process.poll.return_value = None

        with patch("subprocess.Popen", return_value=fake_process) as mock_popen:
            self.assertTrue(self.controller._ensure_native_client(platform="nt"))
            self.assertTrue(self.controller._ensure_native_client(platform="nt"))
            self.assertEqual(mock_popen.call_count, 1)

    def test_windows_native_client_restart_after_exit(self):
        self.controller.connected_room = True
        fake_process_1 = MagicMock()
        fake_process_1.poll.return_value = None
        fake_process_2 = MagicMock()
        fake_process_2.poll.return_value = None

        with patch("subprocess.Popen", side_effect=[fake_process_1, fake_process_2]) as mock_popen:
            self.assertTrue(self.controller._ensure_native_client(platform="nt"))
            self.assertEqual(mock_popen.call_count, 1)

            # Process exits unexpectedly with code 1
            fake_process_1.poll.return_value = 1
            self.assertFalse(self.controller._native_client_running())

            # Next ensure restarts client
            self.assertTrue(self.controller._ensure_native_client(platform="nt"))
            self.assertEqual(mock_popen.call_count, 2)

    def test_windows_native_client_started_on_setup_ready(self):
        self.controller.connected_room = True
        with patch.object(self.controller, "_ensure_native_client") as mock_ensure:
            self.controller._setup_event("setup_ready", {"adapter_state": "applied"})
            mock_ensure.assert_called_once()

    def test_windows_native_client_started_on_already_installed_room(self):
        fake_snapshot = _snapshot()
        fake_state = MagicMock(
            state="already_installed",
            readiness="ready",
            manifest_hash="h123",
            staged_mod="m123",
            steam_launch_option="",
            reason="",
            readiness_reason="",
        )
        with patch.object(self.controller.workflow, "install_state", return_value=fake_state), \
             patch("doom_eap.launcher.launcher_core.RoomSnapshot.from_event", return_value=fake_snapshot), \
             patch.object(self.controller, "_ensure_native_client") as mock_ensure:
            self.controller.process_event({"type": "connected"})
            mock_ensure.assert_called_once()

    def test_windows_native_client_ensured_before_game_launch(self):
        order = []
        fake_prereqs = MagicMock(ok=True)
        with patch.object(self.controller, "_ensure_native_client", side_effect=lambda: order.append("ensure_client")), \
             patch.object(launcher_controller_mod, "validate_game_root", return_value=self.game_root), \
             patch.object(launcher_controller_mod, "probe_runtime_prerequisites", return_value=fake_prereqs), \
             patch.object(launcher_controller_mod, "launch_doom_via_steam", side_effect=lambda: order.append("launch_steam") or "steam://rungameid/782330"):
            self.controller.launch_game()
            self.assertEqual(order, ["ensure_client", "launch_steam"])

    def test_windows_native_client_not_started_on_unready_or_failed_states(self):
        # 1. Disconnected
        self.controller.connected_room = False
        with patch("subprocess.Popen") as mock_popen:
            self.assertFalse(self.controller._ensure_native_client(platform="nt"))
            mock_popen.assert_not_called()

        # 2. Missing Meathook
        self.controller.connected_room = True
        with patch.object(launcher_controller_mod, "probe_meathook", return_value=MagicMock(ok=False)), \
             patch("subprocess.Popen") as mock_popen:
            self.assertFalse(self.controller._ensure_native_client(platform="nt"))
            mock_popen.assert_not_called()

        # 3. Setup ready with manual_action_required (not applied)
        with patch.object(self.controller, "_ensure_native_client") as mock_ensure:
            self.controller._setup_event("setup_ready", {"adapter_state": "manual_action_required"})
            mock_ensure.assert_not_called()

    def test_windows_native_client_stopped_on_disconnect_and_close(self):
        fake_process = MagicMock()
        fake_process.poll.return_value = None
        self.controller._native_client_process = fake_process

        self.controller.disconnect()
        fake_process.terminate.assert_called_once()
        self.assertIsNone(self.controller._native_client_process)

        # Re-attach and test close
        fake_process_2 = MagicMock()
        fake_process_2.poll.return_value = None
        self.controller._native_client_process = fake_process_2

        self.controller.close()
        fake_process_2.terminate.assert_called_once()
        self.assertIsNone(self.controller._native_client_process)

    def test_linux_native_client_unchanged(self):
        self.controller.connected_room = True
        with patch("subprocess.Popen") as mock_popen:
            started = self.controller._ensure_native_client(platform="posix")
            self.assertFalse(started)
            mock_popen.assert_not_called()

    def test_no_manual_ap_client_contract_in_ui_and_docs(self):
        ui_path = Path(__file__).resolve().parents[1] / "doom_eap" / "launcher" / "launcher_ui.py"
        install_doc_path = Path(__file__).resolve().parents[1] / "docs" / "INSTALL.md"
        ui_text = ui_path.read_text(encoding="utf-8")
        install_text = install_doc_path.read_text(encoding="utf-8")

        # Confirm no UI instruction or docs require manual execution of ap_client.exe
        for text in [ui_text, install_text]:
            self.assertNotIn("Run ap_client.exe", text)
            self.assertNotIn("Open ap_client.exe", text)
            self.assertNotIn("Start ap_client.exe", text)
            self.assertNotIn("Launch ap_client.exe manually", text)


if __name__ == "__main__":
    unittest.main()
