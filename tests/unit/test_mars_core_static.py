import json
import tempfile
import unittest
from pathlib import Path

from campaign_goal_contract import load_campaign_goal_contract
from tools.maps.ap_map_generator import (
    extract_target_names,
    find_entity_block_bounds,
    generate_map,
    validate_target_policies,
)
from tools.maps.mission_complete_map_patcher import _patch_campaign_goal


ROOT = Path(__file__).resolve().parents[2]


class MarsCoreStaticContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temporary.name, "e2m3_core.entities")
        cls.generated_manifest = Path(cls.temporary.name, "e2m3_core.json")
        cls.source_path = ROOT / "vanillamaps" / "e2m3_core.map"
        cls.config = json.loads(
            (ROOT / "level_configs" / "e2m3_core.json").read_text()
        )
        cls.items = json.loads((ROOT / "data" / "items.json").read_text())
        generate_map(
            cls.source_path,
            cls.output,
            ROOT / "level_configs" / "e2m3_core.json",
            cls.generated_manifest,
            cls.items,
        )
        cls.goal = load_campaign_goal_contract()
        _patch_campaign_goal(cls.goal, ROOT, cls.output)
        cls.source = cls.source_path.read_text(encoding="utf-8")
        cls.generated = cls.output.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_every_config_entry_reaches_manifest_map_event_and_automap(self):
        manifest = json.loads(self.generated_manifest.read_text())
        expected = dict(self.config["entities"])
        expected.update({
            entry["ap_check"]: entry["location_id"]
            for entry in self.config["secret_encounters"]
        })
        self.assertEqual(manifest, expected)
        self.assertEqual(len(expected), 28)
        for ap_check, location_id in expected.items():
            if ap_check in self.config["entities"]:
                owner = ap_check.removeprefix("AP_CHECK_").lower()
                self.assertEqual(self.source.count(f"entityDef {owner}"), 1)
            self.assertEqual(self.generated.count(f"entityDef {ap_check}"), 1)
            self.assertEqual(
                self.generated.count(f"entityDef ap_event_{location_id}"), 1
            )
            self.assertEqual(
                self.generated.count(
                    f"entityDef ap_automap_location_{location_id}"
                ),
                1,
            )

    def test_bfg_vanilla_sequence_is_byte_preserved(self):
        for vanilla_owner in (
            "phobos_pickup_weapon_bfg_2",
            "phobos_pickup_weapon_bfg_1",
            "_pickup_weapon_bfg_1",
        ):
            source_bounds = find_entity_block_bounds(self.source, vanilla_owner)
            generated_bounds = find_entity_block_bounds(self.generated, vanilla_owner)
            self.assertIsNotNone(source_bounds)
            self.assertIsNotNone(generated_bounds)
            self.assertEqual(
                self.source[source_bounds[0]:source_bounds[1]],
                self.generated[generated_bounds[0]:generated_bounds[1]],
            )
        self.assertNotIn("AP_CHECK_PHOBOS_PICKUP_WEAPON_BFG_2", self.generated)
        for preserved_owner in (
            "phobos_target_timeline_sg_deck_fire",
            "objective_target_objective_give_1",
            "objective_target_objective_complete_1",
            "cinematic_info_logic_bfg10k_firing",
            "cinematic_target_relay_bfg10k",
        ):
            source_bounds = find_entity_block_bounds(self.source, preserved_owner)
            generated_bounds = find_entity_block_bounds(self.generated, preserved_owner)
            self.assertIsNotNone(source_bounds)
            self.assertIsNotNone(generated_bounds)
            self.assertEqual(
                self.source[source_bounds[0]:source_bounds[1]],
                self.generated[generated_bounds[0]:generated_bounds[1]],
            )

    def test_secret_automap_owners_are_replaced_by_targetless_helpers(self):
        for location_id, owner in (
            (7770283, "automap_info_null_secret_enc_1"),
            (7770284, "automap_info_null_secret_enc_2"),
        ):
            self.assertNotIn(f"entityDef {owner} ", self.generated)
            bounds = find_entity_block_bounds(
                self.generated, f"ap_automap_location_{location_id}"
            )
            helper = self.generated[bounds[0]:bounds[1]]
            self.assertEqual(extract_target_names(helper), [])
            self.assertNotIn("renderModelInfo", helper)
            self.assertNotIn("useableComponentDecl", helper)

    def test_campaign_goal_is_only_on_audited_mars_terminal_owner(self):
        bounds = find_entity_block_bounds(self.generated, self.goal["owner"])
        targets = extract_target_names(self.generated[bounds[0]:bounds[1]])
        source_bounds = find_entity_block_bounds(self.source, self.goal["owner"])
        vanilla_targets = extract_target_names(
            self.source[source_bounds[0]:source_bounds[1]]
        )
        self.assertEqual(len(vanilla_targets), 7)
        self.assertEqual(targets[:7], vanilla_targets)
        self.assertEqual(
            targets[-2:],
            [self.goal["location_event_target"], "ap_campaign_goal_event"],
        )
        self.assertEqual(targets.count(self.goal["location_event_target"]), 1)
        self.assertEqual(targets.count("ap_campaign_goal_event"), 1)
        self.assertEqual(
            self.generated.count(f"AP_CHECK_EVENT_{self.goal['location_id']}"), 1
        )
        self.assertEqual(self.generated.count(self.goal["marker"]), 1)
        hub = (ROOT / "vanillamaps" / "hub.map").read_text(encoding="utf-8")
        self.assertNotIn(self.goal["marker"], hub)

    def test_campaign_goal_runtime_dependency_is_packaged_and_manifested(self):
        build_script = (
            ROOT / "scripts" / "build" / "playable_test.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            '"$REPO_ROOT/bridge_client.py" "$REPO_ROOT/bootstrap_actions.py" \\\n'
            '    "$REPO_ROOT/campaign_goal_contract.py" \\\n',
            build_script,
        )
        self.assertIn(
            '"$REPO_ROOT/data/map_sources.json" \\\n'
            '    "$REPO_ROOT/data/campaign_goal_contract.json" \\\n'
            '    "$OUTPUT_DIR/client/data/"',
            build_script,
        )
        self.assertEqual(
            build_script.count('"client/campaign_goal_contract.py",'), 1
        )
        self.assertEqual(
            build_script.count('"client/data/campaign_goal_contract.json",'), 1
        )

    def test_mars_core_is_packaged_only_in_patch2(self):
        registry = json.loads(
            (ROOT / "data" / "map_sources.json").read_text(encoding="utf-8")
        )
        mars = registry["maps"]["e2m3_core"]
        expected_owner = "game/sp/e2m3_core/e2m3_core_patch2.resources"
        self.assertEqual(mars["resource_path"], expected_owner)
        self.assertEqual(mars["resource_owner"], expected_owner)
        self.assertEqual(mars["resource_priority"], 10)
        packaged_path = (
            Path(Path(mars["resource_path"]).stem)
            / "maps"
            / mars["relative_entities_path"]
        )
        self.assertEqual(
            packaged_path.as_posix(),
            "e2m3_core_patch2/maps/game/sp/e2m3_core/e2m3_core.entities",
        )
        self.assertNotIn("e2m3_core_patch3", packaged_path.as_posix())
        forbidden_asset = (
            ROOT / "packaging" / "mod_assets" / "e2m3_core_patch3"
            / "maps" / mars["relative_entities_path"]
        )
        self.assertFalse(forbidden_asset.exists())
        onboarding = json.loads(
            (ROOT / "data" / "onboarding" / "e2m3_core.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(onboarding["resource_owner"], expected_owner)
        self.assertEqual(onboarding["resource_priority"], 10)
        self.assertEqual(
            onboarding["mission_complete_transition"],
            {
                "kind": "direct_owner",
                "owner": "hell_chunk_2_trigger_trigger_end_portal",
                "target": "ap_event_7770289",
                "classification": "progression",
            },
        )
        automap_registry = json.loads(
            (ROOT / "data" / "automap_family_registry.json").read_text(
                encoding="utf-8"
            )
        )
        map_owners = automap_registry["evidence"]["map_owners"]
        self.assertIn(expected_owner, map_owners)
        self.assertNotIn(
            "game/sp/e2m3_core/e2m3_core_patch3.resources", map_owners
        )

    def test_all_mars_feedback_is_explicit_ap_only(self):
        policies = json.loads(
            (ROOT / "data" / "location_feedback_policies.json").read_text()
        )["policies"]["e2m3_core"]
        expected = {
            *self.config["entities"],
            *(entry["ap_check"] for entry in self.config["secret_encounters"]),
        }
        self.assertEqual(set(policies), expected)
        for location_id in [
            *range(7770256, 7770282),
            7770283,
            7770284,
        ]:
            self.assertEqual(
                self.generated.count(f"entityDef ap_notify_location_{location_id}"),
                1,
            )

    def test_empty_native_contract_is_rejected_by_key_presence(self):
        source = """entity {
	entityDef owner {
		inherit = "interact/test";
		class = "idInteractable";
		edit = {
			whenToSave = "SGT_CHECKPOINT";
			spawnPosition = { x = 0; y = 0; z = 0; }
		}
	}
}
"""
        with self.assertRaisesRegex(ValueError, "native_entity_contract"):
            validate_target_policies(
                {"AP_CHECK_OWNER": 7770999},
                {"owner": {"native_entity_contract": {}}},
                source,
            )


if __name__ == "__main__":
    unittest.main()
