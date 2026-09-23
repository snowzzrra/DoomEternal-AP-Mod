import json
from pathlib import Path
import unittest


class TestItemRegistry(unittest.TestCase):
    def test_generic_rune_is_not_a_runtime_item(self) -> None:
        data = Path(__file__).resolve().parents[1] / "data"
        for name in ("items", "item_classifications", "item_replay_policies"):
            registry = json.loads((data / f"{name}.json").read_text(encoding="utf-8"))
            self.assertNotIn("7770020", registry, name)
        for name in ("item_runtime_contracts", "devinv_start_mapping"):
            registry = json.loads((data / f"{name}.json").read_text(encoding="utf-8"))
            self.assertNotIn("7770020", registry["items"], name)
        start = json.loads((data / "start_inventory_catalog.json").read_text(encoding="utf-8"))
        self.assertNotIn("Rune", {item["name"] for item in start["items"]})
        locations = json.loads((data / "location_names.json").read_text(encoding="utf-8"))
        self.assertEqual(locations["locations"]["7770020"], "Hell on Earth - Infinite Extra Lives Cheat")
