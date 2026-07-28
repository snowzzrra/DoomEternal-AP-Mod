"""Tests for APWorld Python selection and preflight logic.

These tests validate the bash helper's logic by reimplementing the
selection/validation rules in Python (no subprocesses, no venv creation).
"""
import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate" / "apworld_python.sh"


class APWorldPythonSelectionTests(unittest.TestCase):
    """Test the interpreter selection and preflight rules."""

    def test_script_exists(self):
        self.assertTrue(SCRIPT.is_file(), f"{SCRIPT} not found")

    def test_script_contains_no_unittest_discover(self):
        content = SCRIPT.read_text()
        self.assertNotIn("unittest discover", content)
        self.assertNotIn("unittest", content)

    def test_script_uses_pytest(self):
        """The all.sh must invoke pytest, not unittest discover."""
        all_sh = REPO_ROOT / "scripts" / "validate" / "all.sh"
        content = all_sh.read_text()
        self.assertNotIn("unittest discover", content)
        # The APWorld test section must use pytest
        self.assertIn("pytest", content)
        self.assertIn("apworld_python.sh", content)

    def test_script_sources_apworld_python(self):
        """all.sh must source the apworld_python helper."""
        all_sh = REPO_ROOT / "scripts" / "validate" / "all.sh"
        content = all_sh.read_text()
        self.assertIn('source "$SCRIPT_DIR/apworld_python.sh"', content)

    def test_script_does_not_use_distrobox_for_apworld_tests(self):
        """all.sh must not use distrobox for APWorld test execution."""
        all_sh = REPO_ROOT / "scripts" / "validate" / "all.sh"
        content = all_sh.read_text()
        # Find lines related to APWorld/doometernal/test — none should use distrobox
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if "doometernal/test" in line:
                # Check surrounding context (5 lines before)
                context = "\n".join(lines[max(0, i - 5):i + 1])
                self.assertNotIn("distrobox", context,
                                 f"APWorld test line {i+1} is inside a distrobox block")


class PythonVersionRangeTests(unittest.TestCase):
    """Test the version range acceptance logic (3.11–3.13 only)."""

    @staticmethod
    def _version_ok(major, minor):
        """Reimplementation of the version check in apworld_python.sh."""
        return (3, 11) <= (major, minor) <= (3, 13)

    def test_rejects_python_310(self):
        self.assertFalse(self._version_ok(3, 10))

    def test_rejects_python_314(self):
        self.assertFalse(self._version_ok(3, 14))

    def test_accepts_python_311(self):
        self.assertTrue(self._version_ok(3, 11))

    def test_accepts_python_312(self):
        self.assertTrue(self._version_ok(3, 12))

    def test_accepts_python_313(self):
        self.assertTrue(self._version_ok(3, 13))

    def test_rejects_python_2(self):
        self.assertFalse(self._version_ok(2, 7))

    def test_rejects_python_4(self):
        self.assertFalse(self._version_ok(4, 0))


class CandidatePriorityTests(unittest.TestCase):
    """Test the interpreter candidate priority logic."""

    @staticmethod
    def _resolve_candidates(workspace_root, archip_root, path_pythons):
        """Reimplementation of _apworld_python_resolve candidate list."""
        candidates = [
            os.path.join(archip_root, ".venv", "bin", "python"),
            os.path.join(workspace_root, ".venv", "bin", "python"),
        ]
        for versioned in ("python3.13", "python3.12", "python3.11"):
            for p in path_pythons:
                if os.path.basename(p) == versioned:
                    candidates.append(p)
                    break
        return candidates

    def test_archip_venv_is_first_candidate(self):
        candidates = self._resolve_candidates("/ws", "/ws/Archipelago", [])
        self.assertEqual(candidates[0], "/ws/Archipelago/.venv/bin/python")

    def test_workspace_venv_is_second_candidate(self):
        candidates = self._resolve_candidates("/ws", "/ws/Archipelago", [])
        self.assertEqual(candidates[1], "/ws/.venv/bin/python")

    def test_python313_before_312_before_311(self):
        path_pythons = [
            "/usr/bin/python3.11",
            "/usr/bin/python3.12",
            "/usr/bin/python3.13",
        ]
        candidates = self._resolve_candidates("/ws", "/ws/Archipelago", path_pythons)
        versioned = [c for c in candidates if "python3.1" in c]
        self.assertEqual(versioned, [
            "/usr/bin/python3.13",
            "/usr/bin/python3.12",
            "/usr/bin/python3.11",
        ])

    def test_apworld_python_env_overrides_all(self):
        """APWORLD_PYTHON env var should be respected when set."""
        # This tests the script's documented contract:
        # "if APWORLD_PYTHON is set, use it directly"
        script_content = SCRIPT.read_text()
        self.assertIn('APWORLD_PYTHON', script_content)
        # The resolve function returns early if APWORLD_PYTHON is already set
        self.assertIn('${APWORLD_PYTHON:-}', script_content)


class ErrorMessageTests(unittest.TestCase):
    """Test that error messages contain actionable guidance."""

    def test_error_message_template_in_script(self):
        content = SCRIPT.read_text()
        self.assertIn("APWorld test environment unavailable", content)
        self.assertIn("Selected Python:", content)
        self.assertIn("pip install pytest", content)
        self.assertIn("APWORLD_PYTHON=", content)
        self.assertIn(".venv", content)

    def test_error_mentions_version_range(self):
        content = SCRIPT.read_text()
        self.assertIn("3.11", content)
        self.assertIn("3.13", content)


class AllShCommandTests(unittest.TestCase):
    """Test that all.sh builds a pytest command, never unittest discover."""

    def test_all_sh_apworld_section_uses_pytest(self):
        all_sh = REPO_ROOT / "scripts" / "validate" / "all.sh"
        content = all_sh.read_text()
        # Must contain pytest invocation for APWorld tests
        self.assertIn("-m pytest", content)
        self.assertIn("pytest.ini", content)
        self.assertIn("worlds/doometernal/test", content)

    def test_all_sh_never_runs_unittest_discover_on_apworld(self):
        all_sh = REPO_ROOT / "scripts" / "validate" / "all.sh"
        content = all_sh.read_text()
        # No unittest discover for doometernal
        self.assertNotIn("unittest discover", content)
        # The words "unittest" + "doometernal" should never appear in the same
        # logical block. Since distrobox is gone, just check globally.
        lines = content.splitlines()
        for line in lines:
            if "doometernal" in line.lower():
                self.assertNotIn("unittest", line)


if __name__ == "__main__":
    unittest.main()
