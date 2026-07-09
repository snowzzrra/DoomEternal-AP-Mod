import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from validate_data import validate_id_namespaces


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


if __name__ == "__main__":
    unittest.main()
