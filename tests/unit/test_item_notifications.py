import json
import tempfile
import unittest
from pathlib import Path

from foundation import build_primitive
from tools.maps.ap_map_generator import (
    generate_item_notification,
    generate_rpc_command_entities,
)
from tools.maps.notification_formatting import notification_key, notification_text
from tools.release.build_string_table import build_string_table


class ItemNotificationFormattingTests(unittest.TestCase):
    def test_item_notification_copies_the_proven_location_hud_contract(self):
        block = generate_item_notification(7770000, "#str_ap_notify_item_7770000")
        for field in (
            'class = "idTarget_Notification";',
            'notificationType = "HUD_NOTIFY_SECRET_FOUND";',
            'notificationHudEventID = "HUD_EVENT_PLAYER_NOTIFICATION_SECRET_FOUND";',
            'doNotShowDuplicate = false;',
            'rootWidget = "tier3centered";',
            'icon = "art/ui/dossier/icons/ico_secrets_off";',
            'header = "#str_ap_notify_item_7770000";',
            'notificationSound = "play_secret_encounter_found";',
            'noFlood = false;',
        ):
            self.assertIn(field, block)
        self.assertNotIn("inherit =", block)
        with self.assertRaisesRegex(ValueError, "invalid parameter"):
            build_primitive("item_notification", "bad", {"header_key": "#str_bad"}, release=False)

    def test_notification_entity_is_independent_and_reactivatable(self):
        notification = generate_item_notification(
            7770024, "#str_ap_notify_item_7770024"
        )
        for one_shot_field in (
            'noFlood = true;', 'triggerOnce = true;',
            'removeAfterActivation = true;', 'disableAfterActivation = true;',
            'startOff = true;',
        ):
            self.assertNotIn(one_shot_field, notification)
        self.assertNotIn("ap_rpc_item_", notification)

    def test_progressive_and_multi_command_receipts_keep_one_notification_per_item(self):
        generated = generate_rpc_command_entities(
            {
                7770024: "give ammo",
                7770097: ["give weapon/player/bfg", "give ammo/bfg 30"],
                7770098: {"type": "progressive_perk", "perks": ["perk/one", "perk/two"]},
            },
            {
                7770024: "Ammo Refill",
                7770097: "BFG Bundle",
                7770098: "Progressive Perk",
            },
            enable_notifications=True,
        )
        self.assertIn('entityDef ap_rpc_v3_7770097 {', generated)
        self.assertNotIn("ap_rpc_item_", generated)
        self.assertEqual(generated.count("entityDef ap_notify_item_7770097 {"), 1)
        for stage in (0, 1):
            self.assertIn(f'entityDef ap_rpc_v3_7770098_{stage} {{', generated)
            self.assertIn(f'entityDef ap_notify_item_7770098_{stage} {{', generated)

    def test_formatter_uses_canonical_keys_counts_stages_and_sanitization(self):
        currency = {"type": "currency", "currency": "CURRENCY_SENTINEL_BATTERY", "count": 2}
        progressive = {"type": "progressive_perk", "perks": ["one", "two", "three", "four"]}
        self.assertEqual(notification_key(7770016, currency), "#str_ap_notify_item_7770016")
        self.assertEqual(notification_text(7770016, currency, "^1Sentinel {player}Battery"), "AP: Sentinel Battery x2")
        self.assertEqual(notification_key(7770017, progressive, stage=1), "#str_ap_notify_item_7770017_1")
        self.assertEqual(notification_text(7770017, progressive, "Progressive Ammo Upgrade", stage=1), "AP: Progressive Ammo Upgrade (2/4)")
        with self.assertRaisesRegex(ValueError, "out of range"):
            notification_key(7770017, progressive, stage=4)

    def test_string_table_matches_generated_map_keys_exactly(self):
        items = {
            "7770000": "give weapon/player/heavy_cannon",
            "7770016": {"type": "currency", "currency": "CURRENCY_SENTINEL_BATTERY", "count": 2},
            "7770017": {"type": "progressive_perk", "perks": ["one", "two"]},
            "7770999": {"type": "no_op"},
        }
        policies = {"items": {
            "7770000": {"name": "Heavy Cannon"},
            "7770016": {"name": "Sentinel Battery"},
            "7770017": {"name": "Progressive Ammo Upgrade"},
            "7770999": {"name": "Ignored"},
        }}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items_path = root / "items.json"
            policies_path = root / "policies.json"
            maps_dir = root / "maps"
            maps_dir.mkdir()
            output = root / "mod" / "gameresources_patch1" / "EternalMod" / "strings" / "english.json"
            items_path.write_text(json.dumps(items), encoding="utf-8")
            policies_path.write_text(json.dumps(policies), encoding="utf-8")
            keys = [
                notification_key(7770000, items["7770000"]),
                notification_key(7770016, items["7770016"]),
                notification_key(7770017, items["7770017"], stage=0),
                notification_key(7770017, items["7770017"], stage=1),
            ]
            (maps_dir / "all.entities").write_text(
                "\n".join(f'header = "{key}";' for key in keys), encoding="utf-8"
            )
            build_string_table(items_path, policies_path, maps_dir, output)
            strings = json.loads(output.read_text(encoding="utf-8"))["strings"]
            self.assertEqual([entry["name"] for entry in strings], sorted(keys))
            table = {entry["name"]: entry["text"] for entry in strings}
            self.assertEqual(table[keys[1]], "AP: Sentinel Battery x2")
            self.assertEqual(table[keys[2]], "AP: Progressive Ammo Upgrade (1/2)")
            self.assertNotIn("#str_ap_notify_item_7770999", table)
            self.assertEqual(
                strings[:2],
                [
                    {"name": "#str_ap_notify_item_7770000", "text": "AP: Heavy Cannon"},
                    {"name": "#str_ap_notify_item_7770016", "text": "AP: Sentinel Battery x2"},
                ],
            )


if __name__ == "__main__":
    unittest.main()
