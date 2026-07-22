import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from map_registry import load_map_registry, release_plan
from tools.validation.audit_item_notification_release import (
    _extract_playable_zip,
    audit_release,
)


ENTITY = '''entityDef ap_rpc_v3_7770000 {
	inherit = "target/relay";
	class = "idTarget_Relay";
}
entityDef ap_notify_item_7770000 {
	header = "#str_ap_notify_item_7770000";
}
'''


class ItemNotificationReleaseAuditTests(unittest.TestCase):
    def test_final_playable_zip_audits_real_inner_mod_payload(self):
        registry = Path(__file__).resolve().parents[2] / "data" / "map_sources.json"
        plans = release_plan(load_map_registry(registry))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated"
            mod = root / "mod"
            client = root / "client"
            generated.mkdir()
            client.mkdir()
            identity = {"item_notifications": {"enabled": True}}
            (client / "bridge_identity.json").write_text(json.dumps(identity), encoding="utf-8")
            manifest = root / "RELEASE_MANIFEST.json"
            manifest.write_text(json.dumps(identity), encoding="utf-8")
            table = mod / "gameresources_patch1" / "EternalMod" / "strings"
            table.mkdir(parents=True)
            strings = {"strings": [{"name": "#str_ap_notify_item_7770000", "text": "AP: Heavy Cannon"}]}
            for locale in ("english.json", "portuguese.json"):
                (table / locale).write_text(json.dumps(strings), encoding="utf-8")
            for plan in plans:
                (generated / plan.generated_output).write_text(ENTITY, encoding="utf-8")
                packaged = mod / Path(plan.resource_path).stem / "maps" / plan.relative_entities_path
                packaged.parent.mkdir(parents=True, exist_ok=True)
                packaged.write_text(ENTITY, encoding="utf-8")

            records = audit_release(True, generated, mod, client, manifest, registry, None, True)
            self.assertEqual(len(records), 5)
            self.assertTrue(all(record["receipt_root_count"] == 0 for record in records.values()))
            self.assertTrue(all(record["effect_entity_count"] == 1 for record in records.values()))

            inner = root / "DoomEternalArchipelagoAlpha.zip"
            with zipfile.ZipFile(inner, "w") as archive:
                for path in mod.rglob("*"):
                    if path.is_file():
                        archive.write(path, path.relative_to(mod))
            playable = root / "playable.zip"
            with zipfile.ZipFile(playable, "w") as archive:
                archive.write(inner, "DoomEternalArchipelagoAlpha.zip")
                archive.write(manifest, "RELEASE_MANIFEST.json")
                archive.write(client / "bridge_identity.json", "client/bridge_identity.json")
            extracted = root / "extracted"
            packaged_mod, packaged_client, packaged_manifest = _extract_playable_zip(playable, extracted)
            self.assertEqual(
                audit_release(True, generated, packaged_mod, packaged_client,
                              packaged_manifest, registry, None),
                records,
            )


if __name__ == "__main__":
    unittest.main()
