import hashlib
import json
import os
import shutil
import ssl
import stat
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
    _log_freshness,
    write_support_bundle,
)
from doom_eap.launcher.launcher_platform import (
    LINUX_MOD_INJECTOR,
    WINDOWS_MOD_MANAGER,
    DependencyManager,
    DependencySpec,
    UrlDownloadTransport,
    create_secure_ssl_context,
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


if __name__ == "__main__":
    unittest.main()
