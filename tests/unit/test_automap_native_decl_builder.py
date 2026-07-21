import tempfile
import unittest
from pathlib import Path

from tools.maps.automap_native_decl_builder import OWNER, build_toy_override


class AutomapNativeDeclBuilderTests(unittest.TestCase):
    def test_toy_override_changes_only_inherited_xp(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audit = build_toy_override(Path(tmpdir))
            target = (
                Path(tmpdir) / OWNER["container"] / "generated" / "decls" /
                OWNER["path"]
            )
            text = target.read_text(encoding="utf-8")
            self.assertEqual(text.count("xp = 0;"), 1)
            self.assertIn(
                'inventoryDecl = "collectible/toys/doom_slayer";', text
            )
            self.assertIn('collectible = "toys/doomguy";', text)
            for forbidden in (
                "currency", "itemList", "inventoryCount", "perk", "give",
            ):
                self.assertNotIn(forbidden, text)
            self.assertEqual(audit["runtime_status"], "pending")
            self.assertEqual(
                audit["reward_cut"],
                {"field": "xp", "inherited_value": 20, "override_value": 0},
            )
            self.assertEqual(list(Path(tmpdir).rglob("*.decl")), [target])


if __name__ == "__main__":
    unittest.main()
