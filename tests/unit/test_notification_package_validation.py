import json
import tempfile
import unittest
from pathlib import Path

from tools.maps.notification_lab import LAB_STRINGS, generate_notification_lab
from tools.validation.validate_item_notification_package import validate

VALID_STRING_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" /
    "item_notification_strings_valid.json"
)


NOTIFICATION = '''entity {
\tentityDef ap_notify_item_major_7770000_a {
\t\tclass = "idTarget_Notification";
\t\tedit = {
\t\t\tflags = { noFlood = false; }
\t\t\tnotificationType = "HUD_NOTIFY_SECRET_FOUND";
\t\t\tnotificationHudEventID = "HUD_EVENT_PLAYER_NOTIFICATION_SECRET_FOUND";
\t\t\tdoNotShowDuplicate = false;
\t\t\trootWidget = "tier3centered";
\t\t\ticon = "art/ui/dossier/icons/ico_secrets_off";
\t\t\theader = "#str_ap_notify_item_7770000";
\t\t\tsubtext = "";
\t\t\tnotificationSound = "play_secret_encounter_found";
\t\t}
\t}
}
entity {
\tentityDef ap_notify_item_major_7770000_b {
\t\tclass = "idTarget_Notification";
\t\tedit = {
\t\t\tflags = { noFlood = false; }
\t\t\tnotificationType = "HUD_NOTIFY_SECRET_FOUND";
\t\t\tnotificationHudEventID = "HUD_EVENT_PLAYER_NOTIFICATION_SECRET_FOUND";
\t\t\tdoNotShowDuplicate = false;
\t\t\trootWidget = "tier3centered";
\t\t\ticon = "art/ui/dossier/icons/ico_secrets_off";
\t\t\theader = "#str_ap_notify_item_7770000";
\t\t\tsubtext = "";
\t\t\tnotificationSound = "play_secret_encounter_found";
\t\t}
\t}
}
'''

LOCATION_NOTIFICATION = '''entity {
\tentityDef ap_notify_location_7770001 {
\t\tclass = "idTarget_Notification";
\t\tedit = {
\t\t\tflags = { noFlood = false; }
\t\t\tnotificationType = "HUD_NOTIFY_CODEX_RECIEVED";
\t\t\tnotificationHudEventID = "HUD_EVENT_PLAYER_NOTIFICATION_CODEX";
\t\t\tnotificationEndHudEventID = "HUD_EVENT_PLAYER_NOTIFICATION_CODEX_END";
\t\t\tdoNotShowDuplicate = false;
\t\t\trootWidget = "compact_notification";
\t\t\ticon = "art/ui/icons/notifications/demons";
\t\t\theader = "#str_ap_location_sent";
\t\t\tsubtext = "#str_ap_location_7770001";
\t\t\tnotificationSound = "play_hud_lower";
\t\t}
\t}
}
'''

EFFECT = '''entity {
\tentityDef ap_rpc_v3_7770000 {
\t\tclass = "idTarget_Command";
\t}
}
'''


class ItemNotificationPackageValidationTests(unittest.TestCase):
    def _layout(self, root: Path, enabled: bool) -> tuple[Path, Path, Path, Path]:
        maps = root / "maps"
        mod = root / "mod"
        client = root / "client"
        maps.mkdir()
        (client / "data").mkdir(parents=True)
        identity = {"item_notifications": {"enabled": enabled}}
        (client / "bridge_identity.json").write_text(json.dumps(identity), encoding="utf-8")
        (client / "bridge_client.py").write_text(
            "bridge_identity.json\nreceipt=ENABLE_ITEM_NOTIFICATIONS\n", encoding="utf-8"
        )
        manifest = root / "RELEASE_MANIFEST.json"
        manifest.write_text(json.dumps(identity), encoding="utf-8")
        (client / "data" / "items.json").write_text(
            '{"7770000": "give weapon/player/heavy_cannon"}',
            encoding="utf-8",
        )
        (client / "data" / "item_classifications.json").write_text(
            json.dumps({
                "schema_version": 1,
                "item_mapping_revision": 5,
                "source": "Archipelago/worlds/doometernal/items.py",
                "items": {
                    "7770000": {
                        "name": "Heavy Cannon",
                        "classification": 1,
                    }
                },
            }),
            encoding="utf-8",
        )
        table = mod / "gameresources_patch1" / "EternalMod" / "strings"
        table.mkdir(parents=True)
        strings = [
            {"name": "#str_ap_location_sent", "text": "AP Location Sent"},
            {
                "name": "#str_ap_location_7770001",
                "text": "AP: Hell on Earth - Chainsaw",
            },
        ]
        if enabled:
            (maps / "e1m1_intro.entities").write_text(
                EFFECT + NOTIFICATION + LOCATION_NOTIFICATION,
                encoding="utf-8",
            )
            strings.append({
                "name": "#str_ap_notify_item_7770000",
                "text": "AP: Heavy Cannon",
            })
        else:
            (maps / "e1m1_intro.entities").write_text(
                EFFECT + LOCATION_NOTIFICATION, encoding="utf-8"
            )
        fixture = {"strings": strings}
        (table / "english.json").write_text(
            json.dumps(fixture), encoding="utf-8"
        )
        (table / "portuguese.json").write_text(
            json.dumps(fixture), encoding="utf-8"
        )
        return maps, mod, client, manifest

    def test_enabled_package_requires_all_notifier_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            validate(True, *self._layout(Path(directory), True))

    def test_enabled_package_accepts_complete_lab_on_e1m1(self):
        with tempfile.TemporaryDirectory() as directory:
            maps, mod, client, manifest = self._layout(Path(directory), True)
            source = maps / "e1m1_intro.entities"
            source.write_text(
                source.read_text(encoding="utf-8")
                + generate_notification_lab("e1m1_intro", enabled=True),
                encoding="utf-8",
            )
            table_dir = mod / "gameresources_patch1" / "EternalMod" / "strings"
            for locale in ("english", "portuguese"):
                path = table_dir / f"{locale}.json"
                data = json.loads(path.read_text(encoding="utf-8"))
                data["strings"].extend(
                    {"name": name, "text": text}
                    for name, text in LAB_STRINGS[locale].items()
                )
                path.write_text(json.dumps(data), encoding="utf-8")
            validate(True, maps, mod, client, manifest)

    def test_disabled_package_rejects_notifier_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            maps, mod, client, manifest = self._layout(Path(directory), False)
            validate(False, maps, mod, client, manifest)
            (maps / "e1m1_intro.entities").write_text(EFFECT + NOTIFICATION, encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "disabled notifier"):
                validate(False, maps, mod, client, manifest)

    def test_dict_string_schema_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            maps, mod, client, manifest = self._layout(Path(directory), True)
            table = mod / "gameresources_patch1" / "EternalMod" / "strings" / "english.json"
            table.write_text(
                json.dumps({"strings": {"#str_ap_notify_item_7770000": "AP: Heavy Cannon"}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AssertionError, "must be a list"):
                validate(True, maps, mod, client, manifest)

    def test_receipt_root_is_rejected_regardless_of_its_class(self):
        with tempfile.TemporaryDirectory() as directory:
            maps, mod, client, manifest = self._layout(Path(directory), True)
            source = maps / "e1m1_intro.entities"
            source.write_text(
                source.read_text(encoding="utf-8") + '\\nentityDef ap_rpc_item_7770000 { class = "idTarget_Count"; }',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AssertionError, "forbidden ap_rpc_item"):
                validate(True, maps, mod, client, manifest)

    def test_known_valid_string_fixture_uses_common_and_progressive_stage_zero(self):
        fixture = json.loads(VALID_STRING_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(
            fixture["strings"][1],
            {
                "name": "#str_ap_notify_item_7770017_0",
                "text": "AP: Progressive Ammo Upgrade (1/2)",
            },
        )


if __name__ == "__main__":
    unittest.main()
