import json
import hashlib
import re
import tempfile
import unittest
from pathlib import Path

from ap_map_generator import (
    EVENT_ENTITY_PREFIX,
    add_ap_check_target,
    apply_checkpoint_cleanup_contract,
    assert_no_weapon_mastery_token_currency,
    command_requires_map_side_rpc,
    compute_file_sha256,
    ensure_distinct_input_output_paths,
    find_entity_block_bounds,
    find_generated_prefixes,
    generate_check_event,
    generate_event_relay,
    generate_bootstrap_entities,
    generate_map,
    generate_rpc_command_entities,
    generate_target_relay,
    inject_secret_encounter_completion,
    extract_target_names,
    remove_balanced_entity_blocks,
    remove_property_blocks,
    validate_source_file,
    validate_target_policies,
    _timeline_event_targets,
)

ROOT = Path(__file__).parents[1]


class MapGeneratorTests(unittest.TestCase):
    def test_rejected_stat_write_bootstraps_are_absent_from_all_maps(self):
        self.assertEqual(generate_bootstrap_entities(), "")

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

    def test_sentinel_battery_uses_restored_direct_currency_primitive(self):
        entities = generate_rpc_command_entities({
            "7770016": {
                "type": "currency",
                "currency": "CURRENCY_SENTINEL_BATTERY",
                "count": 1,
            }
        })
        self.assertIn('entityDef ap_rpc_v3_7770016 {', entities)
        self.assertIn('class = "idTarget_GiveItems";', entities)
        self.assertNotIn('inherit = "target/give_item";', entities)
        self.assertNotIn('ap_rpc_v3_7770016_currency', entities)
        self.assertIn('currencyType = "CURRENCY_SENTINEL_BATTERY";', entities)
        self.assertEqual(entities.count('currencyType = "CURRENCY_SENTINEL_BATTERY";'), 1)
        self.assertEqual(entities.count("count = 1;"), 1)
        self.assertNotIn("AP_BATTERY_RELAY_ACTIVATED", entities)

    def test_sentinel_battery_bundle_uses_exact_count_two_currency_primitive(self):
        entities = generate_rpc_command_entities({
            "7770142": {
                "type": "currency",
                "currency": "CURRENCY_SENTINEL_BATTERY",
                "count": 2,
            }
        })
        self.assertIn('entityDef ap_rpc_v3_7770142 {', entities)
        self.assertIn('class = "idTarget_GiveItems";', entities)
        self.assertIn('currencyType = "CURRENCY_SENTINEL_BATTERY";', entities)
        self.assertEqual(entities.count("count = 2;"), 1)
        self.assertNotIn("inherit =", entities)


    def test_weapon_mastery_token_currency_is_rejected_from_registered_maps(self):
        with self.assertRaisesRegex(ValueError, "CURRENCY_WEAPON_MASTERY"):
            assert_no_weapon_mastery_token_currency(
                'currencyType = "CURRENCY_WEAPON_MASTERY";', "fixture"
            )

    def test_item_commands_have_required_map_side_entities(self):
        entities = generate_rpc_command_entities(
            {
                "7770000": "give weapon/player/heavy_cannon",
                "7770045": "chrispy ai/heavy/revenant",
                "7770997": ["give ammo", "chrispy ai/fodder/imp"],
            }
        )

        self.assertTrue(command_requires_map_side_rpc("give weapon/player/heavy_cannon"))
        self.assertTrue(command_requires_map_side_rpc("chrispy ai/heavy/revenant"))
        self.assertIn("entityDef ap_rpc_v3_7770000 {", entities)
        self.assertIn('commandText = "give weapon/player/heavy_cannon";', entities)
        self.assertIn("entityDef ap_rpc_v3_7770045 {", entities)
        self.assertIn('commandText = "chrispy ai/heavy/revenant";', entities)
        self.assertIn("entityDef ap_rpc_v3_7770997_0 {", entities)
        self.assertIn('commandText = "give ammo";', entities)
        self.assertIn("entityDef ap_rpc_v3_7770997_1 {", entities)
        self.assertIn('commandText = "chrispy ai/fodder/imp";', entities)

    def test_all_current_item_mappings_have_generated_map_side_entities(self):
        items_path = Path(__file__).parents[1] / "data" / "items.json"
        items = json.loads(items_path.read_text(encoding="utf-8"))

        entities = generate_rpc_command_entities(items)
        for item_id, command_value in items.items():
            if isinstance(command_value, str):
                self.assertIn(f"entityDef ap_rpc_v3_{item_id} {{", entities)
            elif isinstance(command_value, list):
                for command_index, command in enumerate(command_value):
                    self.assertIn(
                        f"entityDef ap_rpc_v3_{item_id}_{command_index} {{",
                        entities,
                    )

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


    def test_target_policy_fails_when_expected_target_is_missing(self):
        content = """
        edit = {
            targets = {
                num = 1;
                item[0] = "target_relay_pickup_ice_bomb";
            }
        }
        """

        with self.assertRaisesRegex(ValueError, "target_give_item_ice_bomb"):
            add_ap_check_target(
                content,
                "pickup_equipment_ice_bomb",
                "AP_CHECK_PICKUP_EQUIPMENT_ICE_BOMB",
                {
                    "preserve_targets": ["target_relay_pickup_ice_bomb"],
                    "drop_targets": ["target_give_item_ice_bomb"],
                },
            )

    def test_hub_ice_bomb_is_one_shot_check_only_trigger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir, "hub.entities")
            manifest = Path(tmpdir, "hub.json")
            generate_map(
                ROOT / "vanillamaps" / "hub.map",
                output,
                ROOT / "level_configs" / "hub.json",
                manifest,
                json.loads((ROOT / "data" / "items.json").read_text()),
            )
            generated = output.read_text(encoding="utf-8")
            bounds = find_entity_block_bounds(generated, "pickup_equipment_ice_bomb")
            self.assertIsNotNone(bounds)
            block = generated[bounds[0]:bounds[1]]
            self.assertIn('inherit = "info/null";', block)
            self.assertIn('class = "idInfo";', block)
            self.assertNotIn('useableComponentDecl', block)
            self.assertNotIn('equipment/ice_bomb', block)
            self.assertNotIn('throwable/player/ice_bomb', block)
            self.assertNotIn("target_give_item_ice_bomb", block)
            trigger_bounds = find_entity_block_bounds(
                generated, "ap_independent_pickup_equipment_ice_bomb"
            )
            self.assertIsNotNone(trigger_bounds)
            trigger = generated[trigger_bounds[0]:trigger_bounds[1]]
            self.assertIn('inherit = "trigger/trigger";', trigger)
            self.assertIn('class = "idTrigger";', trigger)
            self.assertIn('triggerOnce = true;', trigger)
            self.assertIn('item[0] = "AP_CHECK_PICKUP_EQUIPMENT_ICE_BOMB";', trigger)
            self.assertIn('item[1] = "ap_remove_location_visual_7770074";', trigger)
            for coordinate in (
                "x = 0.47;", "y = -22.27;", "z = -14.38;",
                "x = 4.0;", "y = 3.0;", "z = 3.5;",
            ):
                self.assertIn(coordinate, trigger)
            self.assertEqual(
                extract_target_names(trigger),
                [
                    "AP_CHECK_PICKUP_EQUIPMENT_ICE_BOMB",
                    "ap_remove_location_visual_7770074",
                ],
            )
            self.assertNotIn("target_show_ice_bomb", trigger)
            self.assertNotIn("target_give_item_ice_bomb", trigger)
            for forbidden in (
                "equipment/ice_bomb",
                "throwable/player/ice_bomb",
                "itemList",
                "give",
                "canBePossessed",
            ):
                self.assertNotIn(forbidden, trigger)

    def test_flame_belch_replaces_vanilla_with_only_progression_and_ap_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir, "hub.entities")
            manifest = Path(tmpdir, "hub.json")
            generate_map(
                ROOT / "vanillamaps" / "hub.map", output,
                ROOT / "level_configs" / "hub.json", manifest,
                json.loads((ROOT / "data" / "items.json").read_text()),
            )
            generated = output.read_text(encoding="utf-8")
            self.assertIsNone(find_entity_block_bounds(
                generated, "pickup_equipment_flame_belch_1"
            ))
            trigger_bounds = find_entity_block_bounds(
                generated, "ap_independent_pickup_equipment_flame_belch_1"
            )
            self.assertIsNotNone(trigger_bounds)
            trigger = generated[trigger_bounds[0]:trigger_bounds[1]]
            self.assertEqual(
                extract_target_names(trigger),
                [
                    "target_relay_pickup_flame_belch",
                    "AP_CHECK_PICKUP_EQUIPMENT_FLAME_BELCH_1",
                ],
            )
            self.assertEqual(trigger.count("target_relay_pickup_flame_belch"), 1)
            self.assertEqual(trigger.count("AP_CHECK_PICKUP_EQUIPMENT_FLAME_BELCH_1"), 1)
            self.assertNotIn("target_give_item_flame_belch", trigger)
            self.assertNotRegex(
                generated,
                r'item\[\d+\]\s*=\s*"target_give_item_flame_belch";',
            )
            for forbidden in (
                "pickup/equipment/flame_belch", "useableComponentDecl",
                "equipOnPickup", "forceEquip", "itemList", "currencyList",
            ):
                self.assertNotIn(forbidden, trigger)

    def test_praetor_suit_page_keeps_one_native_interaction_owner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir, "hub.entities")
            manifest = Path(tmpdir, "hub.json")
            generate_map(
                ROOT / "vanillamaps" / "hub.map", output,
                ROOT / "level_configs" / "hub.json", manifest,
                json.loads((ROOT / "data" / "items.json").read_text()),
            )
            generated = output.read_text(encoding="utf-8")
            bounds = find_entity_block_bounds(generated, "progress_praetor_point_hub_1")
            self.assertIsNotNone(bounds)
            native = generated[bounds[0]:bounds[1]]
            for required in (
                'inherit = "progress/praetor_token";',
                'class = "idInteractable_GiveItems";',
                'onUseCodexEntry = "codex/tutorials/praetor_suit_perks";',
                'useStat = "STAT_SUIT_PAGE_UNLOCKED";',
                "interaction = {",
                'item[0] = "target_relay_complete_praetor_obj";',
            ):
                self.assertIn(required, native)
            self.assertNotIn("currencyList", native)
            self.assertNotIn("CURRENCY_PRAETOR_UPGRADE", native)
            self.assertEqual(
                extract_target_names(native),
                [
                    "target_relay_complete_praetor_obj",
                    "AP_CHECK_PROGRESS_PRAETOR_POINT_HUB_1",
                ],
            )
            self.assertIsNone(find_entity_block_bounds(
                generated, "ap_independent_progress_praetor_point_hub_1"
            ))
            self.assertNotIn("ap_location_visual_7770081", generated)
            self.assertEqual(
                generated.count('item[0] = "target_relay_complete_praetor_obj";'), 1
            )

    def test_bound_pickup_triggers_keep_vanilla_local_coordinates_and_edit_siblings(self):
        cases = (
            (
                "e1m2_war", "capitol_progress_dash_1",
                "capitol_func_mover_16",
            ),
            (
                "e1m3_cult", "game_pickup_extra_life_extra_life_1_10_e1m3",
                "game_func_mover_43",
            ),
        )
        items = json.loads((ROOT / "data" / "items.json").read_text())
        with tempfile.TemporaryDirectory() as tmpdir:
            for map_key, entity_name, parent in cases:
                output = Path(tmpdir, f"{map_key}.entities")
                manifest = Path(tmpdir, f"{map_key}.json")
                generate_map(
                    ROOT / "vanillamaps" / f"{map_key}.map", output,
                    ROOT / "level_configs" / f"{map_key}.json", manifest, items,
                )
                generated = output.read_text(encoding="utf-8")
                source = (ROOT / "vanillamaps" / f"{map_key}.map").read_text(
                    encoding="utf-8"
                )
                source_bounds = find_entity_block_bounds(source, entity_name)
                trigger_bounds = find_entity_block_bounds(
                    generated, f"ap_independent_{entity_name}"
                )
                self.assertIsNotNone(source_bounds)
                self.assertIsNotNone(trigger_bounds)
                source_block = source[source_bounds[0]:source_bounds[1]]
                trigger = generated[trigger_bounds[0]:trigger_bounds[1]]
                source_position = re.search(
                    r'spawnPosition\s*=\s*\{.*?\n\s*\}', source_block, re.DOTALL
                ).group(0)
                self.assertIn(source_position, trigger)
                self.assertIn(f'bindParent = "{parent}";', trigger)
                self.assertIn("bindInfo", remove_property_blocks(trigger, "clipModelInfo"))
                self.assertLess(trigger.index("clipModelInfo"), trigger.index("bindInfo"))

    def test_target_policy_schema_rejects_unknown_and_unconsumed_configuration(self):
        source = '''entity { entityDef pickup_test { edit = { targets = {
            num = 1; item[0] = "known_target"; } } } }'''
        with self.assertRaisesRegex(ValueError, "unsupported key"):
            validate_target_policies(
                {"AP_CHECK_PICKUP_TEST": 7770991},
                {"pickup_test": {"not_a_policy": True}}, source,
            )
        with self.assertRaisesRegex(ValueError, "missing from source targets"):
            validate_target_policies(
                {"AP_CHECK_PICKUP_TEST": 7770991},
                {"pickup_test": {"drop_targets": ["missing_target"]}}, source,
            )


    def test_cultist_rocket_is_independent_one_shot_trigger_with_safe_relay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir, "cult.entities")
            manifest = Path(tmpdir, "cult.json")
            generate_map(
                ROOT / "vanillamaps" / "e1m3_cult.map",
                output,
                ROOT / "level_configs" / "e1m3_cult.json",
                manifest,
                json.loads((ROOT / "data" / "items.json").read_text()),
            )
            generated = output.read_text(encoding="utf-8")
            self.assertIsNone(find_entity_block_bounds(
                generated, "game_pickup_weapon_rocket_launcher_1"
            ))
            self.assertNotIn("game_pickup_weapon_rocket_launcher_1", generated)
            bounds = find_entity_block_bounds(
                generated, "ap_independent_rocket_launcher_7770056"
            )
            self.assertIsNotNone(bounds)
            block = generated[bounds[0]:bounds[1]]
            self.assertIn('inherit = "trigger/trigger";', block)
            self.assertIn('class = "idTrigger";', block)
            self.assertIn('triggerOnce = true;', block)
            self.assertNotIn('useableComponentDecl', block)
            self.assertNotIn('equipOnPickup', block)
            self.assertNotIn('forceEquip', block)
            self.assertNotIn('ammo/rocket', block)
            self.assertNotIn('itemList', block)
            self.assertNotIn('give', block)
            self.assertIn('item[0] = "game_target_relay_1244";', block)
            self.assertIn('AP_CHECK_GAME_PICKUP_WEAPON_ROCKET_LAUNCHER_1', block)
            self.assertEqual(block.count("AP_CHECK_GAME_PICKUP_WEAPON_ROCKET_LAUNCHER_1"), 1)
            self.assertEqual(
                extract_target_names(block),
                [
                    "game_target_relay_1244",
                    "AP_CHECK_GAME_PICKUP_WEAPON_ROCKET_LAUNCHER_1",
                ],
            )
            self.assertNotIn('pickup/weapon/rocket_launcher', block)
            self.assertNotIn('idProp2', block)
            self.assertNotIn('canBePossessed', block)
            self.assertNotIn('layers {', block)
            self.assertIn('type = "CLIPMODEL_BOX";', block)
            self.assertNotIn("ap_location_visual_7770056", generated)
            self.assertNotIn("ap_remove_location_visual_7770056", generated)
            helper_bounds = find_entity_block_bounds(
                generated, "ap_automap_location_7770056"
            )
            self.assertIsNotNone(helper_bounds)
            helper = generated[helper_bounds[0]:helper_bounds[1]]
            self.assertIn('class = "idInfo";', helper)
            self.assertIn('inherit = "info/null";', helper)
            self.assertIn('automapPropertiesDecl = "default";', helper)
            self.assertEqual(extract_target_names(helper), [])
            vanilla = (ROOT / "vanillamaps" / "e1m3_cult.map").read_text(encoding="utf-8")
            vanilla_bounds = find_entity_block_bounds(
                vanilla, "game_pickup_weapon_rocket_launcher_1"
            )
            vanilla_block = vanilla[vanilla_bounds[0]:vanilla_bounds[1]]
            for coordinate in ("x = 123.849915;", "y = -254.599991;", "z = 14.7841377;"):
                self.assertIn(coordinate, block)
                self.assertIn(coordinate, vanilla_block)
            alternate = find_entity_block_bounds(generated, "game_trigger_trigger_994")
            self.assertIsNotNone(alternate)
            self.assertIn('item[0] = "game_target_relay_1244";', generated[alternate[0]:alternate[1]])
            relay = find_entity_block_bounds(generated, "game_target_relay_1244")
            self.assertIsNotNone(relay)
            relay_block = generated[relay[0]:relay[1]]
            self.assertEqual(
                extract_target_names(relay_block),
                ["movers_func_mover_162", "movers_func_mover_163"],
            )
            checkpoint_start = find_entity_block_bounds(generated, "game_player_start_6")
            self.assertIsNotNone(checkpoint_start)
            checkpoint_block = generated[checkpoint_start[0]:checkpoint_start[1]]
            self.assertNotIn('game_target_give_item_1', checkpoint_block)
            self.assertIsNone(find_entity_block_bounds(generated, "game_target_give_item_1"))
            timeline = find_entity_block_bounds(generated, "game_target_timeline_6")
            self.assertIsNotNone(timeline)
            timeline_block = generated[timeline[0]:timeline[1]]
            self.assertEqual(
                _timeline_event_targets(timeline_block),
                [
                    "game_target_timeline_5",
                    "ap_independent_rocket_launcher_7770056",
                    "game_trigger_trigger_990",
                    "game_trigger_trigger_994",
                ],
            )
            self.assertEqual(
                timeline_block.count(
                    'entity = "ap_independent_rocket_launcher_7770056";'
                ),
                1,
            )
            self.assertIn('eventDef = "remove";', timeline_block)

            prior_output = output.read_bytes().replace(
                b'entity = "ap_independent_rocket_launcher_7770056";',
                b'entity = "";',
            )
            self.assertEqual(
                hashlib.sha256(prior_output).hexdigest(),
                "1b7f583d346819227e69023356bd300c76d756121203c1a3876ee17fb3bf2b89",
            )

    def test_rocket_checkpoint_cleanup_contract_fails_closed_on_drift(self):
        source = (ROOT / "vanillamaps" / "e1m3_cult.map").read_text(encoding="utf-8")
        config = json.loads((ROOT / "level_configs" / "e1m3_cult.json").read_text())
        contract = config["target_policies"][
            "game_pickup_weapon_rocket_launcher_1"
        ]["checkpoint_cleanup"]
        changed = apply_checkpoint_cleanup_contract(source, contract)
        self.assertEqual(
            changed.count('entity = "ap_independent_rocket_launcher_7770056";'),
            1,
        )
        restored = changed.replace(
            'entity = "ap_independent_rocket_launcher_7770056";',
            'entity = "game_pickup_weapon_rocket_launcher_1";',
        )
        self.assertEqual(restored, source)

        bad_hash = dict(contract, source_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "source hash drift"):
            apply_checkpoint_cleanup_contract(source, bad_hash)

        drifted = source.replace(
            'entity = "game_trigger_trigger_990";',
            'entity = "unexpected_checkpoint_target";',
            1,
        )
        bounds = find_entity_block_bounds(drifted, "game_target_timeline_6")
        drifted_block = drifted[bounds[0]:bounds[1]]
        target_drift = dict(
            contract,
            source_sha256=hashlib.sha256(drifted_block.encode("utf-8")).hexdigest(),
        )
        with self.assertRaisesRegex(ValueError, "target order drift"):
            apply_checkpoint_cleanup_contract(drifted, target_drift)

    def test_unchanged_legacy_generated_maps_remain_byte_identical(self):
        expected = {
            "e1m1_intro": "f2b9b36630702bfbf7fb9172aebfede52c1fd29ce91dabde6c05ec58164eca2d",
            "e1m2_war": "5bc85983a032a3029b0be27fd1123ab0cb51eb0c817204a33f6f0802ca096499",
        }
        items = json.loads((ROOT / "data" / "items.json").read_text())
        for map_key, digest in expected.items():
            with self.subTest(map_key=map_key), tempfile.TemporaryDirectory() as tmpdir:
                output = Path(tmpdir, f"{map_key}.entities")
                manifest = Path(tmpdir, f"{map_key}.json")
                generate_map(
                    ROOT / "vanillamaps" / f"{map_key}.map",
                    output,
                    ROOT / "level_configs" / f"{map_key}.json",
                    manifest,
                    items,
                )
                self.assertEqual(compute_file_sha256(output), digest)

    def test_exultia_heavy_cannon_fallback_is_removed_without_references(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir, "exultia.entities")
            manifest = Path(tmpdir, "exultia.json")
            generate_map(
                ROOT / "vanillamaps" / "e1m2_war.map",
                output,
                ROOT / "level_configs" / "e1m2_war.json",
                manifest,
                json.loads((ROOT / "data" / "items.json").read_text()),
            )
            generated = output.read_text(encoding="utf-8")
            self.assertIsNone(find_entity_block_bounds(
                generated, "pickups_pickup_weapon_heavy_cannon_1"
            ))
            self.assertNotIn("pickups_pickup_weapon_heavy_cannon_1", generated)



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
