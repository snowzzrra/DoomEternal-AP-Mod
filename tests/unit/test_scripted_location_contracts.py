import json
import tempfile
import unittest
from pathlib import Path

from tools.maps.ap_map_generator import generate_map
from tools.validation.audit_scripted_location import verify_generated_location


ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "data" / "scripted_location_contracts.json"


class ScriptedLocationContractTests(unittest.TestCase):
    def generate_hub(self, directory: Path) -> Path:
        output = directory / "hub.entities"
        generate_map(
            ROOT / "vanillamaps" / "hub.map",
            output,
            ROOT / "level_configs" / "hub.json",
            directory / "hub.json",
            json.loads((ROOT / "data" / "items.json").read_text()),
        )
        return output

    def test_current_ice_geometry_visual_and_targets_match_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = self.generate_hub(Path(tmpdir))
            self.assertEqual(
                verify_generated_location(CONTRACTS, output, "7770074"),
                [],
            )

    def test_visual_target_fails_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = self.generate_hub(Path(tmpdir))
            text = output.read_text()
            visual_start = text.index("entityDef ap_location_visual_7770074")
            before, visual = text[:visual_start], text[visual_start:]
            visual = visual.replace(
                '\t\t\tdormancy = {\n\t\t\t\tallowPvsDormancy = false;',
                '\t\t\ttargets = {\n\t\t\t\tnum = 1;\n'
                '\t\t\t\titem[0] = "target_relay_enable_prison_lift";\n'
                '\t\t\t}\n\t\t\tdormancy = {\n'
                '\t\t\t\tallowPvsDormancy = false;',
                1,
            )
            output.write_text(before + visual)
            errors = verify_generated_location(CONTRACTS, output, "7770074")
            self.assertTrue(any("visual" in error for error in errors), errors)

    def test_trigger_progression_target_fails_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = self.generate_hub(Path(tmpdir))
            text = output.read_text().replace(
                'item[0] = "AP_CHECK_PICKUP_EQUIPMENT_ICE_BOMB";',
                'item[0] = "target_relay_enable_prison_lift";',
                1,
            )
            output.write_text(text)
            errors = verify_generated_location(CONTRACTS, output, "7770074")
            self.assertTrue(any("targets" in error for error in errors), errors)

    def test_cleanup_targeting_functional_entity_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = self.generate_hub(Path(tmpdir))
            text = output.read_text()
            cleanup_start = text.index("entityDef ap_remove_location_visual_7770074")
            before, cleanup = text[:cleanup_start], text[cleanup_start:]
            cleanup = cleanup.replace(
                'item[0] = "ap_location_visual_7770074";',
                'item[0] = "target_relay_enable_prison_lift";',
                1,
            )
            output.write_text(before + cleanup)
            errors = verify_generated_location(CONTRACTS, output, "7770074")
            self.assertTrue(any("cleanup" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
