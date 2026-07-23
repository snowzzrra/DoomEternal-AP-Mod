import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.maps.ap_map_generator import generate_item_notification, generate_map
from tools.maps.map_semantic_baseline import generate_frozen_outputs
from tools.maps.notification_lab import (
    LAB_STRINGS,
    NOTIFICATION_LAB_CONTRACTS,
    generate_notification_lab,
    notification_lab_enabled,
)
from tools.release.build_string_table import build_string_table


class NotificationLabTests(unittest.TestCase):
    def test_lab_requires_explicit_flag_and_selected_map(self):
        self.assertFalse(notification_lab_enabled({}))
        self.assertFalse(notification_lab_enabled({"AP_NOTIFICATION_LAB": "0"}))
        self.assertTrue(notification_lab_enabled({"AP_NOTIFICATION_LAB": "1"}))
        self.assertEqual(generate_notification_lab("e1m1_intro", enabled=False), "")
        self.assertEqual(generate_notification_lab("hub", enabled=True), "")
        self.assertIn(
            "entityDef ap_notify_lab_current",
            generate_notification_lab("e1m1_intro", enabled=True),
        )

    def test_map_generator_emits_lab_only_when_explicitly_enabled(self):
        source = """entity {
    entityDef player_start_test {
        class = "idPlayerStart";
        edit = {
        }
    }
}
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "source" / "fixture.map"
            config_path = root / "config.json"
            input_path.parent.mkdir()
            input_path.write_text(source, encoding="utf-8")
            config_path.write_text(
                json.dumps({"map_key": "e1m1_intro", "entities": {}}),
                encoding="utf-8",
            )
            outputs = {}
            for enabled in (False, True):
                output_path = root / f"generated-{enabled}" / "fixture.entities"
                manifest_path = root / f"generated-{enabled}" / "manifest.json"
                generate_map(
                    input_path,
                    output_path,
                    config_path,
                    manifest_path,
                    {},
                    enable_notification_lab=enabled,
                )
                outputs[enabled] = output_path.read_text(encoding="utf-8")

        self.assertNotIn("ap_notify_lab_", outputs[False])
        self.assertEqual(outputs[True].count("entityDef ap_notify_lab_"), 5)

    def test_frozen_baseline_ignores_lab_environment_opt_in(self):
        with patch.dict(
            os.environ, {"AP_NOTIFICATION_LAB": "1"}, clear=False
        ):
            outputs, temporary = generate_frozen_outputs()
        try:
            e1m1_output = outputs["e1m1_intro"][0].read_text(encoding="utf-8")
            self.assertNotIn("ap_notify_lab_", e1m1_output)
        finally:
            temporary.cleanup()

    def test_lab_has_exactly_five_unique_reusable_notifications(self):
        lab = generate_notification_lab("e1m1_intro", enabled=True)
        names = [
            line.split()[1]
            for line in lab.splitlines()
            if line.strip().startswith("entityDef ap_notify_lab_")
        ]
        self.assertEqual(len(names), 5)
        self.assertEqual(len(set(names)), 5)
        self.assertEqual(
            set(names),
            {
                "ap_notify_lab_current",
                "ap_notify_lab_inventory",
                "ap_notify_lab_codex",
                "ap_notify_lab_collectible",
                "ap_notify_lab_generic",
            },
        )
        self.assertEqual(lab.count('class = "idTarget_Notification";'), 5)
        for forbidden in (
            "idTarget_Count",
            "count =",
            "triggerOnce",
            "removeAfterActivation",
            "disableAfterActivation",
            "noFlood = true",
        ):
            self.assertNotIn(forbidden, lab)

    def test_contracts_keep_complete_vanilla_field_sets(self):
        self.assertEqual(len(NOTIFICATION_LAB_CONTRACTS), 5)
        lab = generate_notification_lab("e1m1_intro", enabled=True)
        for required in (
            'notificationType = "HUD_NOTIFY_SECRET_FOUND";',
            'notificationType = "HUD_NOTIFY_INVENTORY_ACQUIRED";',
            'notificationType = "HUD_NOTIFY_CODEX_RECIEVED";',
            'notificationType = "HUD_NOTIFY_COLLECTIBLE_ACQUIRED";',
            'notificationType = "HUD_NOTIFY_GENERIC_CALLOUT";',
            'rootWidget = "compact_notification";',
            'rootWidget = "weapon";',
            'notificationSound = "play_hud_lower";',
            'notificationSound = "play_ui_notification_collectible";',
            'notificationSound = "play_ui_notification_large";',
        ):
            self.assertIn(required, lab)

    def test_lab_does_not_change_production_item_notification(self):
        expected = generate_item_notification(
            7770024, "#str_ap_notify_item_7770024", 2
        )
        with patch.dict(os.environ, {"AP_NOTIFICATION_LAB": "1"}):
            actual = generate_item_notification(
                7770024, "#str_ap_notify_item_7770024", 2
            )
        self.assertEqual(actual, expected)
        self.assertNotIn("ap_notify_lab_", actual)

    def test_lab_strings_are_bilingual_and_flag_conditional(self):
        items = {"7770000": "give weapon/player/heavy_cannon"}
        policies = {"items": {"7770000": {"name": "Heavy Cannon"}}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items_path = root / "items.json"
            policies_path = root / "policies.json"
            maps = root / "maps"
            maps.mkdir()
            items_path.write_text(json.dumps(items), encoding="utf-8")
            policies_path.write_text(json.dumps(policies), encoding="utf-8")

            production_map = 'header = "#str_ap_notify_item_7770000";\n'
            lab_map = production_map + generate_notification_lab(
                "e1m1_intro", enabled=True
            )
            for locale in ("english", "portuguese"):
                output = root / f"{locale}.json"
                (maps / "e1m1_intro.entities").write_text(
                    production_map, encoding="utf-8"
                )
                with patch.dict(os.environ, {}, clear=True):
                    build_string_table(items_path, policies_path, maps, output)
                names = {
                    entry["name"]
                    for entry in json.loads(output.read_text(encoding="utf-8"))[
                        "strings"
                    ]
                }
                self.assertFalse(names & set(LAB_STRINGS[locale]))

                (maps / "e1m1_intro.entities").write_text(
                    lab_map, encoding="utf-8"
                )
                with patch.dict(
                    os.environ, {"AP_NOTIFICATION_LAB": "1"}, clear=True
                ):
                    build_string_table(items_path, policies_path, maps, output)
                table = {
                    entry["name"]: entry["text"]
                    for entry in json.loads(output.read_text(encoding="utf-8"))[
                        "strings"
                    ]
                }
                for key, text in LAB_STRINGS[locale].items():
                    self.assertEqual(table[key], text)

    def test_playable_build_allows_explicit_lab_opt_in(self):
        script = (
            Path(__file__).parents[2] / "scripts/build/playable_test.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('echo "NOTIFICATION_LAB=enabled"', script)
        self.assertIn('echo "NOTIFICATION_LAB=disabled"', script)
        self.assertNotIn("cannot enter a public playable build", script)
        self.assertNotIn("Dev-only notification lab entered", script)


if __name__ == "__main__":
    unittest.main()
