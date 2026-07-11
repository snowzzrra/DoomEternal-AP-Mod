import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from validate_data import APWORLD, ROOT, extract_namedtuple_table, validate_id_namespaces


class ValidateDataNamespaceTests(unittest.TestCase):
    def test_item_and_location_can_share_same_numeric_id(self):
        errors = validate_id_namespaces(
            {"Air Control": 7770089},
            {"Exultia - Secret Encounter 1": 7770089},
        )
        self.assertEqual(errors, [])

    def test_duplicate_item_ids_fail(self):
        errors = validate_id_namespaces(
            {"Air Control": 7770089, "Dazed and Confused": 7770089},
            {"Exultia - Secret Encounter 1": 7771001},
        )
        self.assertEqual(
            errors,
            [
                "Duplicate AP item ID 7770089: ['Air Control', 'Dazed and Confused']"
            ],
        )

    def test_duplicate_location_ids_fail(self):
        errors = validate_id_namespaces(
            {"Air Control": 7770089},
            {
                "Exultia - Secret Encounter 1": 7770089,
                "Cultist Base - Secret Encounter 1": 7770089,
            },
        )
        self.assertEqual(
            errors,
            [
                "Duplicate AP location ID 7770089: ['Exultia - Secret Encounter 1', 'Cultist Base - Secret Encounter 1']"
            ],
        )

    def test_hell_on_earth_extra_life_names_keep_ids(self):
        locations = extract_namedtuple_table(
            APWORLD / "locations.py", "location_data_table"
        )

        self.assertEqual(
            locations["Hell on Earth - Extra Life - Cliffside in Last Arena"],
            7770004,
        )
        self.assertEqual(
            locations["Hell on Earth - Extra Life - Shopping Center Elevator"],
            7770005,
        )
        self.assertEqual(
            locations[
                "Hell on Earth - Extra Life - Street Arena Behind Breakable Wall"
            ],
            7770006,
        )
        self.assertEqual(
            locations["Hell on Earth - Extra Life - Street Arena Behind Bars"],
            7770007,
        )

    def test_native_client_serializes_keepalive_and_execute_rpc(self):
        source = (ROOT / "mhclient.cpp").read_text(encoding="utf-8")
        header = (ROOT / "mhclient.h").read_text(encoding="utf-8")

        self.assertIn("CRITICAL_SECTION m_RpcMutex", header)
        self.assertIn('EnterRpcCall("KeepAlive"', source)
        self.assertIn('EnterRpcCall("ExecuteConsoleCommand"', source)
        self.assertIn("MarkBindingInvalid()", source)

    def test_native_client_has_execute_watchdog(self):
        source = (ROOT / "ap_client_exe.cpp").read_text(encoding="utf-8")

        self.assertIn("RPC_CALL_STALLED", source)
        self.assertIn("CreateThread(nullptr, 0, RpcCallWatchdog", source)

    def test_drain_traps_do_not_use_zero(self):
        items = (ROOT / "data" / "items.json").read_text(encoding="utf-8")

        self.assertIn('"7770055": "give ammo/sharedammopool/fuel -3"', items)
        self.assertIn('"7770056": "give ammo/sharedammopool/bfg -2"', items)
        self.assertNotIn("sharedammopool/fuel 0", items)
        self.assertNotIn("sharedammopool/bfg 0", items)

    def test_reserved_location_ids_are_not_reused(self):
        locations = extract_namedtuple_table(
            APWORLD / "locations.py", "location_data_table"
        )
        for reserved_id in (7770055, 7770068):
            self.assertNotIn(reserved_id, locations.values())
        for directory in (ROOT / "level_configs", ROOT / "manifests"):
            for path in directory.glob("*.json"):
                values = (__import__("json").loads(path.read_text()).get("entities", {}).values()
                          if directory.name == "level_configs" else __import__("json").loads(path.read_text()).values())
                self.assertNotIn(7770055, values)
                self.assertNotIn(7770068, values)

    def test_scripted_pickups_have_no_decl_overrides(self):
        hub = (ROOT / "level_configs" / "hub.json").read_text(encoding="utf-8")
        cult = (ROOT / "level_configs" / "e1m3_cult.json").read_text(encoding="utf-8")
        generator = (ROOT / "ap_map_generator.py").read_text(encoding="utf-8")
        packaged = "\n".join(
            str(path.relative_to(ROOT))
            for path in (ROOT / "packaging" / "mod_assets").rglob("*")
            if path.is_file()
        )
        self.assertNotIn("propitem/ap/", hub + cult + generator + packaged)
        self.assertFalse((ROOT / "packaging" / "mod_assets" / "hub_patch2" /
                          "generated" / "decls" / "propitem" / "propitem" /
                          "equipment" / "ice_bomb.decl").exists())
        self.assertFalse((ROOT / "packaging" / "mod_assets" / "e1m3_cult_patch3" /
                          "generated" / "decls" / "propitem" / "propitem" /
                          "weapon" / "rocket_launcher" / "base.decl").exists())

if __name__ == "__main__":
    unittest.main()
