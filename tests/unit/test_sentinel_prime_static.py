import json
import tempfile
import unittest
from pathlib import Path

from tools.maps.ap_map_generator import (
    extract_target_names,
    find_entity_block_bounds,
    generate_map,
)
from tools.maps.mission_complete_map_patcher import _patch_sentinel_prime_end
from tools.validation.audit_e2m4_resource_package import (
    MODEL_PATH,
    MODEL_SHA256,
    audit_e2m4_resource_package,
)


ROOT = Path(__file__).resolve().parents[2]


class SentinelPrimeStaticContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads(
            (ROOT / "level_configs" / "e2m4_boss.json").read_text()
        )
        cls.source = (ROOT / "vanillamaps" / "e2m4_boss.map").read_text()
        cls.temporary = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temporary.name, "e2m4_boss.entities")
        cls.manifest = Path(cls.temporary.name, "e2m4_boss.json")
        generate_map(
            ROOT / "vanillamaps" / "e2m4_boss.map",
            cls.output,
            ROOT / "level_configs" / "e2m4_boss.json",
            cls.manifest,
            json.loads((ROOT / "data" / "items.json").read_text()),
        )
        cls.generated = cls.output.read_text()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_campaign_extra_life_audit_excludes_non_campaign_variants(self):
        extra_lives = {
            "game_pickup_extra_life_extra_life_1_1": 7770301,
            "game_pickup_extra_life_extra_life_1_3_e2m4": 7770302,
        }
        source_owners = set()
        for entity in self.source.split("entity {")[1:]:
            if 'inherit = "pickup/extra_life/extra_life_1";' not in entity:
                continue
            owner = entity.split("entityDef ", 1)[1].split(None, 1)[0]
            source_owners.add(owner)
        self.assertEqual(source_owners, set(extra_lives))
        self.assertEqual(
            {
                self.config["entities"][f"AP_CHECK_{owner.upper()}"]
                for owner in extra_lives
            },
            set(extra_lives.values()),
        )
        for owner in extra_lives:
            bounds = find_entity_block_bounds(self.source, owner)
            block = self.source[bounds[0]:bounds[1]]
            self.assertEqual(extract_target_names(block), [])
            self.assertNotIn("removeForMasterLevels", block)
            self.assertNotIn("mission_select", block.lower())

    def test_each_physical_location_has_one_complete_inert_ap_graph(self):
        expected_owners = {
            **{f"game_progress_codex_{index}_e2m4": 7770290 + index
               for index in range(1, 10)},
            "game_progress_praetor_token_1_e2m4": 7770300,
            "game_pickup_extra_life_extra_life_1_1": 7770301,
            "game_pickup_extra_life_extra_life_1_3_e2m4": 7770302,
        }
        self.assertEqual(len(self.config["entities"]), len(expected_owners))
        self.assertEqual(json.loads(self.manifest.read_text()), self.config["entities"])
        for owner, location_id in expected_owners.items():
            ap_check = f"AP_CHECK_{owner.upper()}"
            self.assertEqual(self.config["entities"][ap_check], location_id)
            self.assertEqual(self.source.count(f"entityDef {owner}"), 1)
            self.assertEqual(self.generated.count(f"entityDef {ap_check}"), 1)
            for entity_name in (
                f"ap_event_{location_id}",
                f"ap_location_visual_{location_id}",
                f"ap_remove_location_visual_{location_id}",
                f"ap_automap_location_{location_id}",
            ):
                self.assertEqual(self.generated.count(f"entityDef {entity_name}"), 1)

            generated_owner_bounds = find_entity_block_bounds(self.generated, owner)
            if location_id <= 7770300:
                self.assertIsNone(generated_owner_bounds)
            else:
                self.assertIsNotNone(generated_owner_bounds)
                generated_owner = self.generated[
                    generated_owner_bounds[0]:generated_owner_bounds[1]
                ]
                self.assertIn('inherit = "info/null";', generated_owner)
                self.assertIn('class = "idInfo";', generated_owner)
                self.assertNotIn("extraLife.lwo", generated_owner)
                self.assertNotIn("useableComponentDecl", generated_owner)
                self.assertNotIn("triggerDef", generated_owner)
                if location_id == 7770302:
                    self.assertIn(
                        '"game/sp/extralives/extralives_many"', generated_owner
                    )
            visual_bounds = find_entity_block_bounds(
                self.generated, f"ap_location_visual_{location_id}"
            )
            visual = self.generated[visual_bounds[0]:visual_bounds[1]]
            self.assertEqual(extract_target_names(visual), [])
            self.assertIn('model = "art/pickups/question_mark_a.lwo";', visual)
            self.assertIn('type = "CLIPMODEL_NONE";', visual)
            forbidden = (
                "useableComponentDecl", "itemList", "currencyList", "inventory",
                "triggerDef", "extraLife.lwo", "art/pickups/codex.lwo",
            )
            if location_id >= 7770301:
                forbidden += ("automapPropertiesDecl",)
            for forbidden_field in forbidden:
                self.assertNotIn(forbidden_field, visual)

    def test_codex_visual_contract_is_explicit_and_not_suppressed(self):
        for index in range(1, 10):
            owner = f"game_progress_codex_{index}_e2m4"
            policy = self.config["target_policies"][owner]
            self.assertEqual(policy, {
                "independent_ap_trigger": True,
                "remove_original": True,
            })
            self.assertNotIn("no_auto_visual", policy)
            self.assertNotIn("native_entity_contract", policy)
        token_policy = self.config["target_policies"].get(
            "game_progress_praetor_token_1_e2m4", {}
        )
        self.assertNotIn("no_auto_visual", token_policy)
        self.assertNotIn("native_entity_contract", token_policy)

    def test_extra_life_visuals_leave_automap_to_the_separate_helper(self):
        for owner in (
            "game_pickup_extra_life_extra_life_1_1",
            "game_pickup_extra_life_extra_life_1_3_e2m4",
        ):
            visual = self.config["target_policies"][owner]["independent_visual"]
            self.assertNotIn("automap_properties_decl", visual)

    def test_question_mark_model_is_carried_by_the_effective_patch2_package(self):
        asset_root = ROOT / "packaging" / "mod_assets"
        dependency = asset_root / "e2m4_boss_patch2" / MODEL_PATH
        self.assertTrue(dependency.is_file())
        with tempfile.TemporaryDirectory() as tmpdir:
            mod_root = Path(tmpdir, "mod")
            package_root = mod_root / "e2m4_boss_patch2"
            packaged_model = package_root / MODEL_PATH
            packaged_model.parent.mkdir(parents=True)
            packaged_model.write_bytes(dependency.read_bytes())
            packaged_entities = package_root / "maps/game/sp/e2m4_boss/e2m4_boss.entities"
            packaged_entities.parent.mkdir(parents=True)
            packaged_entities.write_text(self.generated, encoding="utf-8")
            record = audit_e2m4_resource_package(
                ROOT / "data/map_sources.json", asset_root, mod_root, packaged_entities
            )
        self.assertEqual(record["resource_name"], "e2m4_boss_patch2")
        self.assertEqual(record["model_sha256"], MODEL_SHA256)

    def test_terminal_has_two_independent_nonempty_publishers(self):
        contracts = json.loads(
            (ROOT / "data/mission_complete_map_contracts.json").read_text()
        )
        goal = json.loads((ROOT / "data/campaign_goal_contract.json").read_text())
        with tempfile.TemporaryDirectory() as tmpdir:
            generated = Path(tmpdir, "e2m4_boss.entities")
            generated.write_text(self.generated, encoding="utf-8")
            _patch_sentinel_prime_end(
                contracts["sentinel_prime"], goal, ROOT, generated
            )
            output = generated.read_text(encoding="utf-8")
        mission_bounds = find_entity_block_bounds(output, "ap_event_7770290")
        goal_bounds = find_entity_block_bounds(output, "ap_campaign_goal_event")
        mission = output[mission_bounds[0]:mission_bounds[1]]
        campaign_goal = output[goal_bounds[0]:goal_bounds[1]]
        self.assertIn("AP_CHECK_EVENT_7770290", mission)
        self.assertIn('condump \\"ap_event_7770290.txt\\"', mission)
        self.assertIn(goal["marker"], campaign_goal)
        self.assertIn("condump ap_goal_sentinel_prime_complete.txt", campaign_goal)
        self.assertNotIn(goal["marker"], mission)
        self.assertNotIn("AP_CHECK_EVENT_7770290", campaign_goal)
        self.assertNotIn("condump ;", mission.lower())
        self.assertNotIn("condump ;", campaign_goal.lower())


if __name__ == "__main__":
    unittest.main()
