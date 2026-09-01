import json
from pathlib import Path
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_DONORS = [
    {"name": "e5m1_spear.resources"},
    {"name": "e5m1_spear_patch1.resources"},
    {"name": "e5m1_spear_patch2.resources"},
    {"name": "e6m3_mcity_horde.resources"},
    {"name": "e6m3_mcity_horde_patch1.resources"},
]


class TestHubWorldTextPackaging(unittest.TestCase):
    def test_hub_world_text_assetsinfo_uses_exact_kaizo_donors(self):
        assetsinfo_path = REPO_ROOT / "packaging" / "hub_world_text_assetsinfo.json"
        self.assertTrue(assetsinfo_path.is_file(), f"Missing {assetsinfo_path}")

        content = json.loads(assetsinfo_path.read_text(encoding="utf-8"))
        self.assertIn("resources", content)
        resources = content["resources"]
        self.assertEqual(
            resources,
            EXPECTED_DONORS,
            "hub_world_text_assetsinfo.json must specify exact 5-donor retail list matching Kaizo parity",
        )

        donor_names = [r.get("name") for r in resources]
        self.assertNotIn(
            "hub.resources",
            donor_names,
            "hub.resources is redundant because Hub already loads its own resource",
        )
        self.assertNotIn(
            "e1m1_intro.resources",
            donor_names,
            "e1m1_intro.resources alone is insufficient for modern SWF/mapresource binding",
        )

        assets = content.get("assets", [])
        self.assertEqual(
            assets,
            [],
            "hub_world_text_assetsinfo.json must not register generic_text.swf in assets[] as it creates invalid local MapAsset records",
        )

    def test_build_script_copies_hub_assetsinfo_unchanged(self):
        build_script = REPO_ROOT / "scripts" / "build" / "playable_test.sh"
        self.assertTrue(build_script.is_file(), f"Missing {build_script}")

        text = build_script.read_text(encoding="utf-8")
        self.assertIn(
            'cp "$REPO_ROOT/packaging/hub_world_text_assetsinfo.json"',
            text,
        )
        self.assertIn(
            '"$MOD_STAGING_DIR/hub_patch2/EternalMod/assetsinfo/hub.json"',
            text,
        )
