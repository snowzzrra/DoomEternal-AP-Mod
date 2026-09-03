import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHIPELAGO_ROOT = (REPO_ROOT.parent / "Archipelago").resolve()


class TestCIPreflight(unittest.TestCase):
    def test_workflow_structure_and_module_invocations(self):
        wf_path = REPO_ROOT / ".github/workflows/cross-platform-build.yml"
        self.assertTrue(wf_path.is_file(), f"Workflow file missing at {wf_path}")

        content = wf_path.read_text(encoding="utf-8")
        doc = yaml.safe_load(content)

        # Trigger is workflow_dispatch only
        self.assertEqual(list(doc.get("on", {}).keys()), ["workflow_dispatch"])

        # Permissions are read-only
        self.assertEqual(doc.get("permissions"), {"contents": "read"})

        # Expected jobs (all 8 jobs)
        expected_jobs = {
            "resolve-metadata",
            "preflight",
            "build-apworld",
            "build-native-support",
            "build-linux-launcher",
            "build-windows-launcher",
            "consolidate-handoff",
            "assemble-release",
        }
        self.assertEqual(set(doc["jobs"].keys()), expected_jobs)

        # Preflight job dependencies
        for job_name in ["build-apworld", "build-native-support", "build-linux-launcher", "build-windows-launcher"]:
            job_needs = doc["jobs"][job_name].get("needs", [])
            if isinstance(job_needs, str):
                job_needs = [job_needs]
            self.assertIn("preflight", job_needs, f"{job_name} must depend on 'preflight'")

        # Invocations use python -m tools.release.build_launcher
        self.assertNotIn("python tools/release/build_launcher.py", content)
        self.assertNotIn("python3 tools/release/build_launcher.py", content)
        self.assertIn("python3 -m tools.release.build_launcher", content)
        self.assertIn("python -m tools.release.build_launcher", content)

        # Single-owner preflight invariant: verify_launcher_runtime is called exactly once, in preflight
        verify_invocations = [
            job_name
            for job_name, job_def in doc["jobs"].items()
            for step in job_def.get("steps", [])
            if "verify_launcher_runtime" in step.get("run", "")
        ]
        self.assertEqual(verify_invocations, ["preflight"], "verify_launcher_runtime must be owned exclusively by jobs.preflight")

        # Launcher jobs run frozen --self-test before artifact upload
        self.assertIn("DoomEternalArchipelagoLauncher --self-test", content)
        self.assertIn("DoomEternalArchipelagoLauncher.exe --self-test", content)

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

        # Timeout configured on preflight and Windows validate
        preflight_steps = doc["jobs"]["preflight"]["steps"]
        win_steps = doc["jobs"]["build-windows-launcher"]["steps"]

        preflight_step = [s for s in preflight_steps if s.get("name") == "Standalone Runtime Import Preflight"]
        self.assertEqual(len(preflight_step), 1)
        self.assertEqual(preflight_step[0].get("timeout-minutes"), 1)

        win_validate = [s for s in win_steps if "Validate Windows" in s.get("name", "")]
        self.assertEqual(len(win_validate), 1)
        self.assertEqual(win_validate[0].get("timeout-minutes"), 1)

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

    def test_ci_requirements_declared(self):
        req_path = REPO_ROOT / "requirements-ci.txt"
        self.assertTrue(req_path.is_file())
        content = req_path.read_text(encoding="utf-8")
        self.assertIn("ruff", content)

    def test_bundle_and_application_directory_resolution(self):
        from doom_eap.launcher.launcher_controller import application_directory, bundle_directory

        # In source mode: bundle_directory() == application_directory() == repo root
        app_dir = application_directory()
        bundle_dir = bundle_directory()
        self.assertEqual(app_dir, bundle_dir)
        self.assertTrue((bundle_dir / "data" / "options_schema.json").is_file())

        # In simulated frozen mode:
        old_frozen = getattr(sys, "frozen", None)
        old_mei = getattr(sys, "_MEIPASS", None)
        old_exe = sys.executable
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp_path = Path(tmp_str)
            fake_exe = tmp_path / "dist" / "launcher.exe"
            fake_mei = tmp_path / "mei"
            fake_exe.parent.mkdir()
            fake_mei.mkdir()
            try:
                sys.frozen = True
                sys.executable = str(fake_exe)
                sys._MEIPASS = str(fake_mei)

                self.assertEqual(application_directory(), fake_exe.parent.resolve())
                self.assertEqual(bundle_directory(), fake_mei.resolve())
            finally:
                if old_frozen is None:
                    delattr(sys, "frozen")
                else:
                    sys.frozen = old_frozen
                if old_mei is None:
                    delattr(sys, "_MEIPASS")
                else:
                    sys._MEIPASS = old_mei
                sys.executable = old_exe

    def test_simulated_raw_frozen_launcher_self_test(self):
        """Prove that a raw PyInstaller binary (no external client/data sidecars) passes self-test using bundled resources."""
        from doom_eap.launcher.launcher_app import _run_self_test

        old_frozen = getattr(sys, "frozen", None)
        old_mei = getattr(sys, "_MEIPASS", None)
        old_exe = sys.executable

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp_root = Path(tmp_str)
            raw_dist = tmp_root / "build/release"
            raw_dist.mkdir(parents=True)
            raw_exe = raw_dist / "DoomEternalArchipelagoLauncher"
            raw_exe.touch()

            mei_dir = tmp_root / "_MEI12345"
            (mei_dir / "data").mkdir(parents=True)
            shutil.copy2(REPO_ROOT / "data/options_schema.json", mei_dir / "data/options_schema.json")
            shutil.copytree(REPO_ROOT / "content", mei_dir / "content")

            try:
                sys.frozen = True
                sys.executable = str(raw_exe)
                sys._MEIPASS = str(mei_dir)

                # Raw binary without external sidecars must pass launcher-only self-test
                exit_code = _run_self_test(["--self-test"])
                self.assertEqual(exit_code, 0)
            finally:
                if old_frozen is None:
                    delattr(sys, "frozen")
                else:
                    sys.frozen = old_frozen
                if old_mei is None:
                    delattr(sys, "_MEIPASS")
                else:
                    sys._MEIPASS = old_mei
                sys.executable = old_exe

    def test_simulated_raw_frozen_missing_bundled_schema_fails(self):
        """Prove that self-test fails if PyInstaller failed to bundle options_schema.json."""
        from doom_eap.launcher.launcher_app import _run_self_test

        old_frozen = getattr(sys, "frozen", None)
        old_mei = getattr(sys, "_MEIPASS", None)
        old_exe = sys.executable

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp_root = Path(tmp_str)
            raw_dist = tmp_root / "build/release"
            raw_dist.mkdir(parents=True)
            raw_exe = raw_dist / "DoomEternalArchipelagoLauncher"
            raw_exe.touch()

            empty_mei = tmp_root / "_MEI_EMPTY"
            empty_mei.mkdir()

            try:
                sys.frozen = True
                sys.executable = str(raw_exe)
                sys._MEIPASS = str(empty_mei)

                exit_code = _run_self_test(["--self-test"])
                self.assertEqual(exit_code, 1)
            finally:
                if old_frozen is None:
                    delattr(sys, "frozen")
                else:
                    sys.frozen = old_frozen
                if old_mei is None:
                    delattr(sys, "_MEIPASS")
                else:
                    sys._MEIPASS = old_mei
                sys.executable = old_exe

    def test_simulated_assembled_package_self_test(self):
        """Prove that self-test validates assembled package sidecars when present."""
        from doom_eap.launcher.launcher_app import _run_self_test

        old_frozen = getattr(sys, "frozen", None)
        old_mei = getattr(sys, "_MEIPASS", None)
        old_exe = sys.executable

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp_root = Path(tmp_str)
            pkg_root = tmp_root / "DoomEternalArchipelago"
            (pkg_root / "client/data").mkdir(parents=True)
            (pkg_root / "client/resources").mkdir(parents=True)
            shutil.copy2(REPO_ROOT / "data/options_schema.json", pkg_root / "client/data/options_schema.json")
            (pkg_root / "RELEASE_MANIFEST.json").write_text('{"schema_version": 2}\n', encoding="utf-8")
            (pkg_root / "doometernal.apworld").touch()
            (pkg_root / "client/resources/room_payload_manifest.json").write_text("{}\n", encoding="utf-8")
            (pkg_root / "client/ap_client.exe").touch()

            pkg_exe = pkg_root / "DoomEternalArchipelagoLauncher"
            pkg_exe.touch()

            mei_dir = tmp_root / "_MEI_PKG"
            (mei_dir / "data").mkdir(parents=True)
            shutil.copy2(REPO_ROOT / "data/options_schema.json", mei_dir / "data/options_schema.json")
            shutil.copytree(REPO_ROOT / "content", mei_dir / "content")

            try:
                sys.frozen = True
                sys.executable = str(pkg_exe)
                sys._MEIPASS = str(mei_dir)

                exit_code = _run_self_test(["--self-test", "--package"])
                self.assertEqual(exit_code, 0)
            finally:
                if old_frozen is None:
                    delattr(sys, "frozen")
                else:
                    sys.frozen = old_frozen
                if old_mei is None:
                    delattr(sys, "_MEIPASS")
                else:
                    sys._MEIPASS = old_mei
                sys.executable = old_exe

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

    def test_launcher_self_test_cli(self):
        from doom_eap.launcher.launcher_app import main as launcher_main
        code = launcher_main(["--self-test", "--launcher-only"])
        self.assertEqual(code, 0)

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

    def test_authorial_contract_accepts_valid_identity(self):
        from tools.validation.authorial import check_authorial
        counts = check_authorial()
        self.assertIn("json_files", counts)
        self.assertIn("items", counts)
        self.assertIn("locations", counts)
        self.assertIn("maps", counts)

    def test_authorial_contract_rejects_divergent_keys(self):
        from tools.validation.authorial import IDENTITY_FIELDS, read_json
        real_id = read_json(REPO_ROOT / "data/content_identity.json")

        missing_key_id = dict(real_id)
        del missing_key_id["slot_data_revision"]
        self.assertNotEqual(set(missing_key_id), set(IDENTITY_FIELDS))

        unknown_key_id = dict(real_id)
        unknown_key_id["unexpected_future_key"] = 123
        self.assertNotEqual(set(unknown_key_id), set(IDENTITY_FIELDS))

    def test_workflow_has_no_github_release_build(self):
        wf_path = REPO_ROOT / ".github/workflows/cross-platform-build.yml"
        doc = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        preflight_steps = doc["jobs"]["preflight"]["steps"]
        step_runs = [s.get("run", "") for s in preflight_steps]

        for run_text in step_runs:
            self.assertNotIn("release --build", run_text, "release --build must NOT run on clean GitHub runner")

    def test_workflow_validates_frozen_room_resources(self):
        wf_path = REPO_ROOT / ".github/workflows/cross-platform-build.yml"
        doc = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        preflight_steps = doc["jobs"]["preflight"]["steps"]
        step_runs = [s.get("run", "") for s in preflight_steps]

        self.assertTrue(
            any("tools.release.prebuilt_room_resources" in run_text for run_text in step_runs),
            "Workflow preflight must invoke tools.release.prebuilt_room_resources",
        )

    def test_prebuilt_room_resources_canonical_bundle(self):
        from tools.release.prebuilt_room_resources import (
            get_frozen_bundle_dir,
            validate_prebuilt_room_resources,
        )
        bundle_dir = get_frozen_bundle_dir(REPO_ROOT, "v0.5.0")
        self.assertTrue(bundle_dir.is_dir())
        result = validate_prebuilt_room_resources(bundle_dir, repo_root=REPO_ROOT)
        self.assertEqual(result["status"], "PASS")

    def test_prebuilt_room_resources_fails_on_checksum_mismatch(self):
        from tools.release.prebuilt_room_resources import (
            get_frozen_bundle_dir,
            validate_prebuilt_room_resources,
        )
        bundle_dir = get_frozen_bundle_dir(REPO_ROOT, "v0.5.0")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_bundle = Path(tmpdir)
            for p in bundle_dir.iterdir():
                tmp_bundle.joinpath(p.name).write_bytes(p.read_bytes())
            (tmp_bundle / "base_mod.zip").write_bytes(b"corrupted binary payload")

            with self.assertRaises(ValueError) as ctx:
                validate_prebuilt_room_resources(tmp_bundle, repo_root=REPO_ROOT)
            self.assertIn("Checksum mismatch", str(ctx.exception))

    def test_prebuilt_room_resources_fails_on_stale_fingerprint(self):
        from tools.release.prebuilt_room_resources import (
            get_frozen_bundle_dir,
            validate_prebuilt_room_resources,
        )
        bundle_dir = get_frozen_bundle_dir(REPO_ROOT, "v0.5.0")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_bundle = Path(tmpdir)
            for p in bundle_dir.iterdir():
                tmp_bundle.joinpath(p.name).write_bytes(p.read_bytes())
            prov_path = tmp_bundle / "ROOM_RESOURCES_PROVENANCE.json"
            doc = json.loads(prov_path.read_text(encoding="utf-8"))
            doc["room_resource_input_fingerprint"] = "0" * 64
            prov_path.write_text(json.dumps(doc), encoding="utf-8")

            with self.assertRaises(ValueError) as ctx:
                validate_prebuilt_room_resources(tmp_bundle, repo_root=REPO_ROOT)
            self.assertIn("STALE ROOM RESOURCES", str(ctx.exception))

    def test_room_resource_fingerprint_sensitivity(self):
        from tools.release.prebuilt_room_resources import (
            compute_room_resource_input_fingerprint,
            DEPENDENCY_DIRS,
            DEPENDENCY_FILES,
        )
        fp_baseline, _ = compute_room_resource_input_fingerprint(REPO_ROOT)
        self.assertEqual(len(fp_baseline), 64)

        # Confirm non-resource files are excluded from dependency sets
        self.assertNotIn("docs", DEPENDENCY_DIRS)
        self.assertNotIn(".github", DEPENDENCY_DIRS)
        self.assertNotIn("tests", DEPENDENCY_DIRS)
        self.assertNotIn(".github/workflows/ci.yml", DEPENDENCY_FILES)
        self.assertNotIn("README.md", DEPENDENCY_FILES)


if __name__ == "__main__":
    unittest.main()
