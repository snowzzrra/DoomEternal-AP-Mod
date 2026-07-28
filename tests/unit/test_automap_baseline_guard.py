import unittest
import json
from pathlib import Path

from tools.maps.automap_baseline_guard import assert_separate_automap_helper_guard


class AutomapBaselineGuardTests(unittest.TestCase):
    def test_all_physical_locations_have_separate_marker_owners(self):
        root = Path(__file__).resolve().parents[2]
        maps = json.loads((root / "data" / "map_sources.json").read_text())["maps"]
        expected = 0
        for source in maps.values():
            if not source.get("enabled", True):
                continue
            config = json.loads((root / source["level_config"]).read_text())
            expected += len(config.get("entities", {}))
            expected += sum(bool(item.get("automap_owner")) for item in config.get("secret_encounters", []))
        self.assertEqual(assert_separate_automap_helper_guard(), expected)


if __name__ == "__main__":
    unittest.main()
