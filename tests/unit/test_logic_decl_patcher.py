import json
import tempfile
import unittest
from pathlib import Path

from tools.maps.logic_decl_patcher import patch_contract


ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "data" / "scripted_location_contracts.json"


class LogicDeclPatcherTests(unittest.TestCase):
    def test_ice_patch_changes_only_owned_branch_destination(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "info_logic_hub_from_e1m2.decl"
            snapshot = patch_contract(CONTRACTS, "7770074", output)
            expected = json.loads(
                (ROOT / "data/snapshots/ice_logic_decl_patch.json").read_text()
            )
            snapshot.pop("changed_lines")
            self.assertEqual(snapshot, expected)

    def test_source_and_override_differ_by_one_line_only(self):
        contract = json.loads(CONTRACTS.read_text())["locations"]["7770074"]
        source = ROOT / contract["logic_decl_patch"]["source_path"]
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "override.decl"
            snapshot = patch_contract(CONTRACTS, "7770074", output)
            before = source.read_text().splitlines()
            after = output.read_text().splitlines()
            differences = [(a, b) for a, b in zip(before, after) if a != b]
            self.assertEqual(len(before), len(after))
            self.assertEqual(differences, [(
                "\t\t\t\t\t\ttoNodeId = 2191342619;",
                "\t\t\t\t\t\ttoNodeId = 1344507903;",
            )])
            self.assertEqual(snapshot["changed_edge"]["edge"], 1193717636)

    def test_hash_mismatch_fails_closed(self):
        data = json.loads(CONTRACTS.read_text())
        data["locations"]["7770074"]["logic_decl_patch"]["source_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as tmpdir:
            contracts = Path(tmpdir) / "data" / "contracts.json"
            contracts.parent.mkdir()
            contracts.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "source missing|hash mismatch"):
                patch_contract(contracts, "7770074", Path(tmpdir) / "out.decl")


if __name__ == "__main__":
    unittest.main()
