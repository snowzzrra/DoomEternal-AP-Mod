import unittest

from ap_map_generator import (
    EVENT_ENTITY_PREFIX,
    add_ap_check_target,
    generate_check_event,
    generate_rpc_command_entities,
    generate_target_relay,
    remove_balanced_entity_blocks,
    remove_property_blocks,
)


class MapGeneratorTests(unittest.TestCase):
    def test_check_relay_targets_notification_and_event(self):
        relay = generate_target_relay("AP_CHECK_TEST", 7770999, "")

        self.assertIn('item[0] = "ap_notify_AP_CHECK_TEST";', relay)
        self.assertIn(f'item[1] = "{EVENT_ENTITY_PREFIX}7770999";', relay)

    def test_check_event_writes_location_specific_file(self):
        event = generate_check_event(7770999)

        self.assertIn("entityDef ap_event_7770999", event)
        self.assertIn(
            'commandText = "echo AP_CHECK_EVENT_7770999; '
            'condump ap_event_7770999.txt";',
            event,
        )

    def test_generated_entities_are_removed_by_exact_or_prefix_name(self):
        content = """
entity {
    entityDef ap_deathlink {
    }
}
entity {
    entityDef ap_event_7770999 {
    }
}
"""

        content = remove_balanced_entity_blocks(content, "ap_deathlink")
        content = remove_balanced_entity_blocks(content, EVENT_ENTITY_PREFIX)

        self.assertNotIn("ap_deathlink", content)
        self.assertNotIn("ap_event_7770999", content)

    def test_multi_command_uses_validated_count_relay(self):
        entities = generate_rpc_command_entities(
            {"7770006": ["give weapon/player/bfg", "give ammo/bfg 30"]}
        )

        self.assertIn('inherit = "target/relay";', entities)
        self.assertIn('class = "idTarget_Count";', entities)
        self.assertIn("count = 1;", entities)
        self.assertIn("num = 2;", entities)
        self.assertIn('item[0] = "ap_rpc_v3_7770006_0";', entities)
        self.assertIn('item[1] = "ap_rpc_v3_7770006_1";', entities)
        self.assertIn('commandText = "give weapon/player/bfg";', entities)
        self.assertIn('commandText = "give ammo/bfg 30";', entities)
        self.assertNotIn('class = "idTarget_Relay";', entities)

    def test_multi_command_rejects_empty_command_list(self):
        with self.assertRaisesRegex(ValueError, "has no commands"):
            generate_rpc_command_entities({"7770006": []})

    def test_nested_clip_model_block_is_removed(self):
        content = """
        clipModelInfo = {
            type = "CLIPMODEL_BOX";
            size = {
                x = 1;
                y = 1;
                z = 1;
            }
            forceObstacle = true;
        }
        triggerDef = "trigger/props/weapons/flame_belch";
        """

        content = remove_property_blocks(content, "clipModelInfo")

        self.assertNotIn("clipModelInfo", content)
        self.assertNotIn("forceObstacle", content)
        self.assertIn('triggerDef = "trigger/props/weapons/flame_belch";', content)

    def test_known_problem_pickups_replace_vanilla_targets(self):
        content = """
        edit = {
            targets = {
                num = 2;
                item[0] = "vanilla_reward";
                item[1] = "tutorial_sequence";
            }
        }
        """

        content = add_ap_check_target(
            content,
            "pickup_equipment_flame_belch_1",
            "AP_CHECK_PICKUP_EQUIPMENT_FLAME_BELCH_1",
        )

        self.assertIn("num = 1;", content)
        self.assertIn(
            'item[0] = "AP_CHECK_PICKUP_EQUIPMENT_FLAME_BELCH_1";',
            content,
        )
        self.assertNotIn("vanilla_reward", content)
        self.assertNotIn("tutorial_sequence", content)

    def test_non_problem_pickups_preserve_existing_targets(self):
        content = """
        edit = {
            targets = {
                num = 1;
                item[0] = "keep_me";
            }
        }
        """

        content = add_ap_check_target(
            content,
            "pickup_collectible_test",
            "AP_CHECK_PICKUP_COLLECTIBLE_TEST",
        )

        self.assertIn("num = 2;", content)
        self.assertIn('item[0] = "keep_me";', content)
        self.assertIn('item[1] = "AP_CHECK_PICKUP_COLLECTIBLE_TEST";', content)


if __name__ == "__main__":
    unittest.main()
