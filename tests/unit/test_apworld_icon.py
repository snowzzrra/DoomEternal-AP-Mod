import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).parents[2]
APWORLD = ROOT.parent / "Archipelago" / "worlds" / "doometernal"


class APWorldIconTests(unittest.TestCase):
    def test_doom_component_uses_packaged_relative_icon(self):
        init_text = (APWORLD / "__init__.py").read_text(encoding="utf-8")
        self.assertTrue((APWORLD / "doom_logo.png").is_file())
        self.assertIn('icon="doom_eternal"', init_text)
        self.assertIn(
            'icon_paths["doom_eternal"] = f"ap:{__name__}/doom_logo.png"',
            init_text,
        )
        self.assertNotIn("Tools/doom_logo.png", init_text)

    def test_built_apworld_contains_doom_icon(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "doometernal.apworld"
            subprocess.run(
                [sys.executable, str(ROOT / "tools" / "release" / "build_apworld.py"), str(APWORLD), str(output)],
                check=True,
            )
            with zipfile.ZipFile(output) as archive:
                self.assertIn("doometernal/doom_logo.png", archive.namelist())


if __name__ == "__main__":
    unittest.main()
