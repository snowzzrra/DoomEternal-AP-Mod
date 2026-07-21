import json
import tempfile
import unittest
from pathlib import Path

from tools.maps.ap_map_generator import (
    extract_target_names,
    find_entity_block_bounds,
    generate_map,
)
from tools.maps.mission_complete_map_patcher import patch_mission_complete_maps


ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "data" / "mission_complete_map_contracts.json"


class MissionCompleteMapPatcherTests(unittest.TestCase):
    def _generated_maps(self, directory):
        maps = {}
        items = json.loads((ROOT / "data" / "items.json").read_text(encoding="utf-8"))
        for key, source, config in (
            ("e1m1_intro", "e1m1_intro.map", "e1m1_intro.json"),
            ("e1m2_war", "e1m2_war.map", "e1m2_war.json"),
            ("hub", "hub.map", "hub.json"),
            ("e1m3_cult", "e1m3_cult.map", "e1m3_cult.json"),
            ("e1m4_boss", "e1m4_boss.map", "e1m4_boss.json"),
        ):
            output = Path(directory, f"{key}.entities")
            generate_map(
                ROOT / "vanillamaps" / source,
                output,
                ROOT / "level_configs" / config,
                Path(directory, f"{key}.json"),
                items,
            )
            maps[key] = output
        return maps

    def test_hell_decl_patch_is_hash_locked_and_changes_one_target_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            maps = self._generated_maps(tmpdir)
            audit = patch_mission_complete_maps(CONTRACTS, maps, Path(tmpdir, "mod"))
            hell = audit["hell_on_earth"]
            override = Path(tmpdir, "mod", hell["override_path"])
            source = ROOT / hell["source_path"]
            self.assertEqual(hell["source_sha256"], hell["expected_source_sha256"])
            self.assertEqual(hell["override_sha256"], hell["expected_override_sha256"])
            self.assertEqual(hell["changed_lists"], 1)
            self.assertEqual(hell["before_targets"], ["citadel_target_level_transition_3"])
            self.assertEqual(
                hell["after_targets"],
                ["AP_CHECK_MISSION_COMPLETE_HELL_ON_EARTH", "citadel_target_level_transition_3"],
            )
            self.assertEqual(source.read_text(encoding="utf-8").count("citadel_target_level_transition_3"), 1)
            self.assertEqual(override.read_text(encoding="utf-8").count("AP_CHECK_MISSION_COMPLETE_HELL_ON_EARTH"), 1)
            for forbidden in ("currency", "perk", "reward", "target_objective"):
                self.assertNotIn(forbidden, "\n".join(hell["after_targets"]))

    def test_exultia_target_order_and_unrelated_blocks_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            maps = self._generated_maps(tmpdir)
            before = maps["e1m2_war"].read_text(encoding="utf-8")
            audit = patch_mission_complete_maps(CONTRACTS, maps, Path(tmpdir, "mod"))
            after = maps["e1m2_war"].read_text(encoding="utf-8")
            bounds = find_entity_block_bounds(after, "extraction_map_exit_level_transition_relay_1723732033")
            self.assertEqual(
                extract_target_names(after[bounds[0]:bounds[1]]),
                ["AP_CHECK_MISSION_COMPLETE_EXULTIA", "extraction_target_level_transition_1"],
            )
            self.assertEqual(audit["exultia"]["changed_lists"], 1)
            self.assertEqual(audit["unrelated_generated_entity_diff_count"], 0)
            self.assertNotIn("AP_CHECK_MISSION_COMPLETE_EXULTIA", before)
            for map_key, ap_check, location_id in (
                ("e1m1_intro", "AP_CHECK_MISSION_COMPLETE_HELL_ON_EARTH", 7770122),
                ("e1m2_war", "AP_CHECK_MISSION_COMPLETE_EXULTIA", 7770123),
                ("e1m4_boss", "AP_CHECK_MISSION_COMPLETE_DOOM_HUNTER_BASE", 7770162),
            ):
                generated = maps[map_key].read_text(encoding="utf-8")
                relay_bounds = find_entity_block_bounds(generated, ap_check)
                relay = generated[relay_bounds[0]:relay_bounds[1]]
                self.assertEqual(extract_target_names(relay), [f"ap_event_{location_id}"])
                event_bounds = find_entity_block_bounds(generated, f"ap_event_{location_id}")
                event = generated[event_bounds[0]:event_bounds[1]]
                self.assertIn('class = "idTarget_Command";', event)
                self.assertIn(f"AP_CHECK_EVENT_{location_id}", event)

    def test_missing_or_duplicate_native_owner_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            maps = self._generated_maps(tmpdir)
            text = maps["e1m2_war"].read_text(encoding="utf-8")
            maps["e1m2_war"].write_text(
                text.replace(
                    "entityDef extraction_map_exit_level_transition_relay_1723732033",
                    "entityDef missing_terminal_owner",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Exultia.*owner"):
                patch_mission_complete_maps(CONTRACTS, maps, Path(tmpdir, "mod"))

        with tempfile.TemporaryDirectory() as tmpdir:
            maps = self._generated_maps(tmpdir)
            text = maps["e1m2_war"].read_text(encoding="utf-8")
            bounds = find_entity_block_bounds(
                text, "extraction_map_exit_level_transition_relay_1723732033"
            )
            maps["e1m2_war"].write_text(
                text + "\n" + text[bounds[0]:bounds[1]], encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "Exultia.*owner"):
                patch_mission_complete_maps(CONTRACTS, maps, Path(tmpdir, "mod"))


if __name__ == "__main__":
    unittest.main()
