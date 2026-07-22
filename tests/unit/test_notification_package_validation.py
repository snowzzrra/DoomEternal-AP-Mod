import json
import tempfile
import unittest
from pathlib import Path

from tools.validation.validate_item_notification_package import validate


VALID_STRING_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" /
    "item_notification_strings_valid.json"
)


NOTIFICATION = '''entity {
\tentityDef ap_notify_item_7770000 {
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
        client.mkdir()
        identity = {"item_notifications": {"enabled": enabled}}
        (client / "bridge_identity.json").write_text(json.dumps(identity), encoding="utf-8")
        (client / "bridge_client.py").write_text(
            "bridge_identity.json\nreceipt=ENABLE_ITEM_NOTIFICATIONS\n", encoding="utf-8"
        )
        manifest = root / "RELEASE_MANIFEST.json"
        manifest.write_text(json.dumps(identity), encoding="utf-8")
        if enabled:
            (maps / "e1m1_intro.entities").write_text(EFFECT + NOTIFICATION, encoding="utf-8")
            table = mod / "gameresources_patch1" / "EternalMod" / "strings"
            table.mkdir(parents=True)
            fixture = {"strings": [{"name": "#str_ap_notify_item_7770000", "text": "AP: Heavy Cannon"}]}
            (table / "english.json").write_text(json.dumps(fixture), encoding="utf-8")
            (table / "portuguese.json").write_text(json.dumps(fixture), encoding="utf-8")
        else:
            (maps / "e1m1_intro.entities").write_text(
                'entityDef ap_rpc_v3_7770000 { }', encoding="utf-8"
            )
        return maps, mod, client, manifest

    def test_enabled_package_requires_all_notifier_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            validate(True, *self._layout(Path(directory), True))

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
