import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.validation.validate_data import (
    APWORLD, ROOT, extract_frozenset_constant, extract_namedtuple_table,
    runtime_automap_families, validate_id_namespaces,
)
from bootstrap_actions import SUIT_PAGE_UNLOCKING_ITEM_IDS
from content_catalog import load_content_catalog


class ValidateDataNamespaceTests(unittest.TestCase):
    def test_runtime_automap_families_cover_registry_exactly(self):
        runtime_ids = set(
            json.loads(
                (ROOT / "data" / "runtime_locations.json").read_text()
            ).values()
        )
        families = json.loads(
            (ROOT / "data" / "automap_family_registry.json").read_text()
        )["families"]
        self.assertEqual(set(runtime_automap_families(families)), runtime_ids)

    def test_mission_challenge_registry_matches_apworld_by_name_and_id(self):
        registry = json.loads(
            (ROOT / "data" / "challenge_location_registry.json").read_text()
        )
        locations = extract_namedtuple_table(
            APWORLD / "locations.py", "location_data_table"
        )
        expected = {
            entry["name"]: entry["location_id"]
            for entry in (
                *registry["mission_challenges"],
                *registry["all_mission_challenges"],
            )
        }
        self.assertEqual(
            {name: locations[name] for name in expected},
            expected,
        )

    def test_praetor_shared_policy_count_is_derived_from_apworld(self):
        locations = extract_namedtuple_table(
            APWORLD / "locations.py", "location_data_table"
        )
        expected = sum("Praetor Suit Token" in name for name in locations)
        configured = sum(
            "PRAETOR" in location.ap_check
            for location in load_content_catalog().physical_locations
        )
        self.assertEqual(configured, expected)

    def test_bootstrap_suit_predicate_matches_canonical_apworld_metadata(self):
        items = extract_namedtuple_table(APWORLD / "items.py", "item_data_table")
        expected_names = {
            "Progressive Health Upgrade", "Progressive Armor Upgrade",
            "Progressive Ammo Upgrade", "Frag Grenade", "Ice Bomb",
        }
        expected_names.update(
            name for name, item_id in items.items() if 7770097 <= item_id <= 7770121
        )
        self.assertEqual(
            SUIT_PAGE_UNLOCKING_ITEM_IDS,
            {items[name] for name in expected_names},
        )
        self.assertNotIn(7770021, SUIT_PAGE_UNLOCKING_ITEM_IDS)
        self.assertNotIn(7770012, SUIT_PAGE_UNLOCKING_ITEM_IDS)

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

    def test_drain_traps_do_not_use_zero(self):
        items = (ROOT / "data" / "items.json").read_text(encoding="utf-8")

        self.assertIn('"7770055": "give ammo/sharedammopool/fuel -3"', items)
        self.assertIn('"7770056": "give ammo/sharedammopool/bfg -90"', items)
        self.assertNotIn("sharedammopool/fuel 0", items)
        self.assertNotIn("sharedammopool/bfg 0", items)
        self.assertNotIn("give armor -200", items)

    def test_armor_drain_id_is_tombstoned_and_commandless(self):
        items = extract_namedtuple_table(APWORLD / "items.py", "item_data_table")
        reserved = extract_frozenset_constant(APWORLD / "items.py", "RESERVED_ITEM_IDS")
        commands = __import__("json").loads((ROOT / "data" / "items.json").read_text())
        self.assertIn(7770057, reserved)
        self.assertNotIn(7770057, items.values())
        self.assertNotIn("Armor Drain Trap", items)
        self.assertNotIn("7770057", commands)

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

    def test_weapon_mastery_token_item_id_is_deprecated_and_commandless(self):
        items = extract_namedtuple_table(APWORLD / "items.py", "item_data_table")
        reserved = extract_frozenset_constant(APWORLD / "items.py", "RESERVED_ITEM_IDS")
        commands = __import__("json").loads((ROOT / "data" / "items.json").read_text())
        self.assertIn(7770019, reserved)
        self.assertNotIn(7770019, items.values())
        self.assertNotIn("Weapon Mastery Token", items)
        self.assertNotIn("7770019", commands)
        self.assertNotIn("CURRENCY_WEAPON_MASTERY", __import__("json").dumps(commands))

if __name__ == "__main__":
    unittest.main()
