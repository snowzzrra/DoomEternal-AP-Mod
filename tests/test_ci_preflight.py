import re
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHIPELAGO_ROOT = REPO_ROOT.parent / "Archipelago"


class TestCIPreflightAndHardening(unittest.TestCase):
    def test_workflow_structure_and_module_invocations(self):
        wf_path = REPO_ROOT / ".github/workflows/cross-platform-build.yml"
        self.assertTrue(wf_path.is_file())

        content = wf_path.read_text(encoding="utf-8")
        doc = yaml.safe_load(content)

        # Trigger is workflow_dispatch only
        self.assertEqual(list(doc.get("on", {}).keys()), ["workflow_dispatch"])

        # apworld_ref default is doom_eternal
        apworld_input = doc["on"]["workflow_dispatch"]["inputs"]["apworld_ref"]
        self.assertEqual(apworld_input["default"], "doom_eternal")

        # Permissions are read-only
        self.assertEqual(doc.get("permissions"), {"contents": "read"})

        # Expected jobs
        expected_jobs = {
            "resolve-metadata",
            "build-apworld",
            "build-native-support",
            "build-linux-launcher",
            "build-windows-launcher",
            "consolidate-handoff",
        }
        self.assertEqual(set(doc["jobs"].keys()), expected_jobs)

        # Invocations use python -m tools.release.build_launcher
        self.assertNotIn("python tools/release/build_launcher.py", content)
        self.assertNotIn("python3 tools/release/build_launcher.py", content)
        self.assertIn("python3 -m tools.release.build_launcher", content)
        self.assertIn("python -m tools.release.build_launcher", content)

        # Launcher jobs run verify_launcher_runtime before PyInstaller
        self.assertIn("tools.release.verify_launcher_runtime", content)

        # Native job installs wine64-tools and runs linkability preflight
        self.assertIn("wine64-tools", content)
        self.assertIn("Native Toolchain & Linkability Preflight", content)
        self.assertIn("RpcExceptionFilter", content)

        # Consolidate job sets chmod +x before test -x
        chmod_idx = content.find("chmod +x linux/DoomEternalArchipelagoLauncher")
        test_idx = content.find("test -x linux/DoomEternalArchipelagoLauncher")
        self.assertNotEqual(chmod_idx, -1)
        self.assertNotEqual(test_idx, -1)
        self.assertLess(chmod_idx, test_idx, "chmod +x must precede test -x in consolidate-handoff")

    def test_minimal_launcher_requirements_declared(self):
        req_path = REPO_ROOT / "requirements-launcher.txt"
        self.assertTrue(req_path.is_file())

        lines = [line.strip() for line in req_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        declared_pkgs = {line.split("==")[0].split(">=")[0] for line in lines}

        expected_minimal = {
            "PySide6",
            "PyInstaller",
            "colorama",
            "websockets",
            "PyYAML",
            "pathspec",
            "typing_extensions",
            "certifi",
            "platformdirs",
        }
        self.assertTrue(expected_minimal.issubset(declared_pkgs))

    def test_native_c_filter_guard(self):
        c_path = REPO_ROOT / "native/client/ap_runtime_rpc_seh.c"
        self.assertTrue(c_path.is_file())

        content = c_path.read_text(encoding="utf-8")
        # Verify RpcExceptionFilter guard exists
        self.assertIn("#if defined(__MINGW32__) || defined(__MINGW64__)", content)
        self.assertIn("int RPC_ENTRY RpcExceptionFilter(unsigned long ExceptionCode);", content)
        self.assertIn("RpcExcept(RpcExceptionFilter(RpcExceptionCode()))", content)

    def test_source_and_data_paths_exist(self):
        from tools.release.ci_preflight import check_source_data_paths
        check_source_data_paths(REPO_ROOT, ARCHIPELAGO_ROOT)

    def test_verify_launcher_runtime_passes(self):
        from tools.release.verify_launcher_runtime import verify_runtime
        verify_runtime(ARCHIPELAGO_ROOT, REPO_ROOT)

    def test_verify_launcher_runtime_rejects_missing_ap_source(self):
        from tools.release.verify_launcher_runtime import verify_runtime
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(RuntimeError) as ctx:
                verify_runtime(Path(tmpdir), REPO_ROOT)
            self.assertIn("missing CommonClient.py", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
