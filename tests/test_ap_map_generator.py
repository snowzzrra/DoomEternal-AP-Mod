import json
import tempfile
import unittest
from pathlib import Path

from ap_map_generator import (
    EVENT_ENTITY_PREFIX,
    add_ap_check_target,
    compute_file_sha256,
    ensure_distinct_input_output_paths,
    find_generated_prefixes,
    generate_check_event,
    generate_event_relay,
    generate_map,
    generate_rpc_command_entities,
    generate_target_relay,
    inject_secret_encounter_completion,
    remove_balanced_entity_blocks,
    remove_property_blocks,
    validate_source_file,
)


class MapGeneratorTests(unittest.TestCase):
    def _write_generation_fixture(self, tmpdir, *, source_text=None):
        tmp_path = Path(tmpdir)
        input_path = tmp_path / "custom" / "fixture.map"
        output_path = tmp_path / "generated" / "fixture.entities"
        manifest_path = tmp_path / "generated" / "fixture.json"
        config_path = tmp_path / "fixture_config.json"
        items_path = tmp_path / "items.json"

        input_path.parent.mkdir(parents=True, exist_ok=True)
        source = source_text or """
entity {
    entityDef pickup_weapon_test {
        inherit = "pickup/weapon/test";
        class = "idProp2";
        edit = {
                spawnPosition = {
                        x = 1;
                        y = 2;
                        z = 3;
                }
                targets = {
                        num = 1;
                        item[0] = "keep_me";
                }
                useableComponentDecl = "useable/test";
        }
    }
}
entity {
    entityDef player_start_test {
        class = "idPlayerStart";
        edit = {
        }
    }
}
"""
        input_path.write_text(source.strip() + "\n", encoding="utf-8")
        config_path.write_text(
            json.dumps(
                {
                    "entities": {
                        "AP_CHECK_PICKUP_WEAPON_TEST": 7770991,
                    }
                }
            ),
            encoding="utf-8",
        )
        items_path.write_text(
            json.dumps({"7770001": "give weapon/player/test"}, indent=4),
            encoding="utf-8",
        )
        return input_path, output_path, config_path, manifest_path, items_path

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

    def test_secret_encounter_relay_emits_only_event(self):
        relay = generate_event_relay(
            "AP_CHECK_SECRET_TEST", 7770999, "", include_notification=False
        )

        self.assertIn('class = "idTarget_Count";', relay)
        self.assertIn("count = 1;", relay)
        self.assertIn("num = 1;", relay)
        self.assertIn(f'item[0] = "{EVENT_ENTITY_PREFIX}7770999";', relay)
        self.assertNotIn("ap_notify_AP_CHECK_SECRET_TEST", relay)

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

    def test_secret_encounter_hook_is_inserted_after_last_wait(self):
        content = """
entity {
	entityDef capitol_encounter_manager_4 {
		edit = {
			encounterComponent = {
				entityEvents = {
					num = 1;
					item[0] = {
						entity = "capitol_encounter_manager_4";
						events = {
							num = 4;
							item[0] = {
								eventCall = {
									eventDef = "spawnSingleAI";
								}
							}
							item[1] = {
								eventCall = {
									eventDef = "spawnSingleAI";
								}
							}
							item[2] = {
								eventCall = {
									eventDef = "spawnSingleAI";
								}
							}
							item[3] = {
								eventCall = {
									eventDef = "waitAIRemaining";
								}
							}
						}
					}
				}
			}
		}
	}
}
"""

        updated = inject_secret_encounter_completion(
            content,
            "capitol_encounter_manager_4",
            "AP_CHECK_SECRET_ENCOUNTER_EXULTIA_1",
            3,
        )

        self.assertIn("num = 5;", updated)
        self.assertIn('item[4] = {', updated)
        self.assertIn('eventDef = "activateTarget";', updated)
        self.assertIn('entity = "AP_CHECK_SECRET_ENCOUNTER_EXULTIA_1";', updated)

    def test_secret_encounter_hook_is_idempotent(self):
        content = """
entity {
	entityDef capitol_encounter_manager_4 {
		edit = {
			encounterComponent = {
				entityEvents = {
					num = 1;
					item[0] = {
						entity = "capitol_encounter_manager_4";
						events = {
							num = 5;
							item[0] = {
								eventCall = {
									eventDef = "spawnSingleAI";
								}
							}
							item[1] = {
								eventCall = {
									eventDef = "spawnSingleAI";
								}
							}
							item[2] = {
								eventCall = {
									eventDef = "spawnSingleAI";
								}
							}
							item[3] = {
								eventCall = {
									eventDef = "waitAIRemaining";
								}
							}
							item[4] = {
								eventCall = {
									eventDef = "activateTarget";
									args = {
										num = 2;
										item[0] = {
											entity = "AP_CHECK_SECRET_ENCOUNTER_EXULTIA_1";
										}
									}
								}
							}
						}
					}
				}
			}
		}
	}
}
"""

        updated = inject_secret_encounter_completion(
            content,
            "capitol_encounter_manager_4",
            "AP_CHECK_SECRET_ENCOUNTER_EXULTIA_1",
            3,
        )

        self.assertEqual(
            updated.count('entity = "AP_CHECK_SECRET_ENCOUNTER_EXULTIA_1";'), 1
        )

    def test_find_generated_prefixes_detects_ap_content(self):
        self.assertEqual(
            find_generated_prefixes('foo AP_CHECK_TEST ap_rpc_v3_7770001'),
            ["AP_CHECK_", "ap_rpc_v3"],
        )

    def test_input_equal_to_output_fails(self):
        with self.assertRaisesRegex(ValueError, "Input and output must be different"):
            ensure_distinct_input_output_paths("/tmp/test.map", "/tmp/test.map")

    def test_source_with_ap_check_prefix_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path, output_path, _, _, _ = self._write_generation_fixture(
                tmpdir,
                source_text="""
entity {
    entityDef AP_CHECK_TEST {
    }
}
""",
            )
            with self.assertRaisesRegex(ValueError, "AP_CHECK_"):
                validate_source_file(input_path, output_path)

    def test_source_with_rpc_prefix_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path, output_path, _, _, _ = self._write_generation_fixture(
                tmpdir,
                source_text="""
entity {
    entityDef ap_rpc_v3_7770001 {
    }
}
""",
            )
            with self.assertRaisesRegex(ValueError, "ap_rpc_v3"):
                validate_source_file(input_path, output_path)

    def test_source_vanilla_is_never_modified(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path, output_path, config_path, manifest_path, items_path = (
                self._write_generation_fixture(tmpdir)
            )
            before_hash = compute_file_sha256(input_path)
            items_dict = json.loads(items_path.read_text(encoding="utf-8"))

            generate_map(
                str(input_path),
                str(output_path),
                str(config_path),
                str(manifest_path),
                items_dict,
            )

            self.assertEqual(before_hash, compute_file_sha256(input_path))

    def test_two_generations_from_same_source_are_identical(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path, output_path, config_path, manifest_path, items_path = (
                self._write_generation_fixture(tmpdir)
            )
            second_output = Path(tmpdir) / "generated-second" / "fixture.entities"
            second_manifest = Path(tmpdir) / "generated-second" / "fixture.json"
            items_dict = json.loads(items_path.read_text(encoding="utf-8"))

            generate_map(
                str(input_path),
                str(output_path),
                str(config_path),
                str(manifest_path),
                items_dict,
            )
            generate_map(
                str(input_path),
                str(second_output),
                str(config_path),
                str(second_manifest),
                items_dict,
            )

            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                second_output.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                json.loads(manifest_path.read_text(encoding="utf-8")),
                json.loads(second_manifest.read_text(encoding="utf-8")),
            )

    def test_fixture_outside_vanillamaps_still_generates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path, output_path, config_path, manifest_path, items_path = (
                self._write_generation_fixture(tmpdir)
            )
            items_dict = json.loads(items_path.read_text(encoding="utf-8"))

            generate_map(
                str(input_path),
                str(output_path),
                str(config_path),
                str(manifest_path),
                items_dict,
            )

            self.assertTrue(output_path.exists())
            self.assertTrue(manifest_path.exists())


if __name__ == "__main__":
    unittest.main()
