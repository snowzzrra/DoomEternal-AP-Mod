import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.diagnostics import save_scenarios as scenarios


class SaveScenarioTests(unittest.TestCase):
    def _source(self, root: Path) -> Path:
        source = root / "live-save"
        source.mkdir()
        (source / "game.details").write_bytes(b"fixture")
        (source / "nested").mkdir()
        (source / "nested/game_duration.dat").write_bytes(b"duration")
        return source

    def test_capture_records_checksums_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self._source(base)
            archive_root = base / "archive"
            captured = scenarios.capture("HUB_VISIT_2_WITH_ICE", archive_root, str(source), False)
            manifest = json.loads((captured / "manifest.json").read_text())
            self.assertEqual(manifest["checksums"], scenarios.checksums(captured / "save"))
            with self.assertRaisesRegex(RuntimeError, "Scenario exists"):
                scenarios.capture("HUB_VISIT_2_WITH_ICE", archive_root, str(source), False)

    def test_restore_requires_confirmation_and_uses_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = self._source(base)
            archive_root = base / "archive"
            scenarios.capture("HUB_VISIT_2_WITH_ICE", archive_root, str(source), False)
            with self.assertRaisesRegex(RuntimeError, "requires --confirm"):
                scenarios.restore("HUB_VISIT_2_WITH_ICE", archive_root, str(source), False)
            (source / "game.details").write_bytes(b"changed")
            with patch.object(scenarios, "assert_runtime_stopped"):
                restored = scenarios.restore(
                    "HUB_VISIT_2_WITH_ICE", archive_root, str(source), True
                )
            self.assertEqual((restored / "game.details").read_bytes(), b"fixture")
            self.assertTrue(list(base.glob("live-save.pre-restore-*")))


if __name__ == "__main__":
    unittest.main()
