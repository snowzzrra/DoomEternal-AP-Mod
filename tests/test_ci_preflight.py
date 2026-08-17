import builtins
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

        # Timeouts configured on preflight steps
        linux_steps = doc["jobs"]["build-linux-launcher"]["steps"]
        win_steps = doc["jobs"]["build-windows-launcher"]["steps"]

        linux_preflight = [s for s in linux_steps if s.get("name") == "Standalone Runtime Import Preflight"]
        self.assertEqual(len(linux_preflight), 1)
        self.assertEqual(linux_preflight[0].get("timeout-minutes"), 1)

        win_preflight = [s for s in win_steps if s.get("name") == "Standalone Runtime Import Preflight"]
        self.assertEqual(len(win_preflight), 1)
        self.assertEqual(win_preflight[0].get("timeout-minutes"), 1)

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
        self.assertIn("#if defined(__MINGW32__) || defined(__MINGW64__)", content)
        self.assertIn("int RPC_ENTRY RpcExceptionFilter(unsigned long ExceptionCode);", content)
        self.assertIn("RpcExcept(RpcExceptionFilter(RpcExceptionCode()))", content)

    def test_source_and_data_paths_exist(self):
        from tools.release.ci_preflight import check_source_data_paths
        check_source_data_paths(REPO_ROOT, ARCHIPELAGO_ROOT)

    def test_verify_launcher_runtime_passes_hermetically(self):
        from tools.release.verify_launcher_runtime import verify_runtime
        verify_runtime(ARCHIPELAGO_ROOT, REPO_ROOT)

    def test_hermetic_no_interactive_prompts(self):
        """Prove that verify_runtime invokes zero interactive prompts (GUI or CLI)."""
        from tools.release.verify_launcher_runtime import verify_runtime

        def _forbidden_interactive(*args, **kwargs):
            raise AssertionError("Forbidden interactive prompt invoked during hermetic preflight!")

        with patch("builtins.input", _forbidden_interactive):
            try:
                import tkinter.filedialog
                import tkinter.messagebox
                with patch("tkinter.filedialog.askdirectory", _forbidden_interactive), \
                     patch("tkinter.messagebox.showerror", _forbidden_interactive):
                    verify_runtime(ARCHIPELAGO_ROOT, REPO_ROOT)
            except ImportError:
                verify_runtime(ARCHIPELAGO_ROOT, REPO_ROOT)

    def test_verify_launcher_runtime_rejects_missing_ap_source(self):
        from tools.release.verify_launcher_runtime import verify_runtime
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(RuntimeError) as ctx:
                verify_runtime(Path(tmpdir), REPO_ROOT)
            self.assertIn("missing CommonClient.py", str(ctx.exception))

    def test_verify_launcher_runtime_in_isolated_subprocess(self):
        """Run verify_launcher_runtime in an isolated subprocess with empty temporary HOME and no ap_config."""
        import site
        with tempfile.TemporaryDirectory() as tmp_home:
            clean_env = os.environ.copy()
            clean_env["HOME"] = tmp_home
            clean_env["USERPROFILE"] = tmp_home
            clean_env.pop("DOOM_AP_CONFIG_FILE", None)
            clean_env.pop("DOOM_AP_APPLICATION_DIR", None)
            user_site = site.getusersitepackages()
            pythonpath = [str(REPO_ROOT), user_site] if isinstance(user_site, str) and os.path.isdir(user_site) else [str(REPO_ROOT)]
            clean_env["PYTHONPATH"] = os.pathsep.join(pythonpath)
            res = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tools.release.verify_launcher_runtime",
                    "--archipelago-source",
                    str(ARCHIPELAGO_ROOT),
                    "--repo-root",
                    str(REPO_ROOT),
                ],
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(REPO_ROOT),
            )
            self.assertEqual(res.returncode, 0, f"Subprocess failed:\nstdout: {res.stdout}\nstderr: {res.stderr}")
            self.assertIn("ALL CHECKS PASSED", res.stdout)


if __name__ == "__main__":
    unittest.main()
