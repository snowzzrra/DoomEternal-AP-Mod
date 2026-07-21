import hashlib
import copy
import json
import unittest
from pathlib import Path

from tools.maps.ap_map_generator import generate_bootstrap_entities, generate_rpc_command_entities
from foundation import (
    build_primitive,
    compile_all_item_plans,
    compile_item_delivery_plan,
    family_counts,
    FROZEN_CONSTRUCTOR_HASHES,
    load_foundation_contracts,
    load_primitive_registry,
    primitive,
    validate_entity_shape,
    validate_primitive_registry,
)
from challenge_registry import load_challenge_registry
from map_registry import (
    generation_plan, load_map_registry, package_plan, release_plan,
    validate_map_registry, validation_plan,
)
from tools.maps.map_preflight import EDGE_KEYS, LOCATION_KEYS, _exact_keys


ROOT = Path(__file__).parents[2]


class FoundationRegistryTests(unittest.TestCase):
    def setUp(self):
        self.definitions = {
            int(key): value
            for key, value in json.loads((ROOT / "data/items.json").read_text()).items()
        }

    def test_registry_and_contracts_are_complete(self):
        registry = validate_primitive_registry()
        self.assertEqual(len(registry["primitives"]), 11)
        self.assertEqual(len(compile_all_item_plans(self.definitions)), 116)
        self.assertEqual(sum(family_counts(self.definitions).values()), 116)
        contracts = load_foundation_contracts()
        self.assertEqual(contracts["counts"]["items"], 116)
        self.assertEqual(contracts["counts"]["locations"], 133)
        self.assertEqual(contracts["counts"]["map_checks"], 108)
        self.assertEqual(contracts["counts"]["runtime_locations"], 25)
        self.assertEqual(contracts["counts"]["runtime_goals"], 1)
        for override in contracts["map_overrides"].values():
            self.assertTrue(override["justification"])

    def test_powerup_extender_keeps_canonical_eternal_mapping(self):
        self.assertEqual(
            self.definitions[7770110],
            {"type": "perk", "perk": "perk/player/suit/powerup/powerup_duration"},
        )

    def test_rejected_and_experimental_primitives_cannot_enter_release(self):
        for primitive_id in ("target_player_stat_modifier_inherit", "target_give_item_inherit"):
            with self.assertRaisesRegex(ValueError, "Rejected primitive"):
                primitive(primitive_id)
        with self.assertRaisesRegex(ValueError, "Experimental primitive"):
            primitive("boolean_stat_modifier_direct")
        invalid = 'entityDef x { inherit = "target/give_item"; class = "idTarget_GiveItems"; }'
        with self.assertRaises(ValueError):
            validate_entity_shape("currency_grant_direct", invalid)

    def test_only_battery_items_remain_runtime_repeatable_currency(self):
        contracts = load_foundation_contracts()
        self.assertEqual(contracts["map_overrides"], {})
        self.assertEqual(
            contracts["repeatability"], {
                "7770016": "repeatable_runtime_proven",
                "7770142": "repeatable_runtime_proven",
            }
        )
        currency = load_primitive_registry()["primitives"]["currency_grant_direct"]
        self.assertEqual(currency["status"], "runtime_verified")
        self.assertNotIn("map_exceptions", currency)

    def test_frozen_constructor_snapshots_do_not_drift(self):
        fixtures = {
            "target_command": ("snapshot_command", {"command": "give weapon/player/heavy_cannon"}),
            "target_count_relay": ("snapshot_relay", {"targets": ["snapshot_a", "snapshot_b"]}),
            "currency_grant_direct": ("snapshot_currency", {"currency": "CURRENCY_SENTINEL_BATTERY", "count": 1}),
        }
        for primitive_id, (entity_name, parameters) in fixtures.items():
            output = build_primitive(
                primitive_id, entity_name, parameters
            )
            self.assertEqual(
                hashlib.sha256(output.encode()).hexdigest(), FROZEN_CONSTRUCTOR_HASHES[primitive_id],
                primitive_id,
            )

    def test_all_item_plans_are_map_side_and_deterministic(self):
        for item_id, definition in self.definitions.items():
            stage = 0 if isinstance(definition, dict) and definition.get("type") == "progressive_perk" else None
            first = compile_item_delivery_plan(item_id, self.definitions, stage=stage)
            second = compile_item_delivery_plan(item_id, self.definitions, stage=stage)
            self.assertEqual(first, second)
            for command in first.commands:
                self.assertRegex(command.command, r"^ai_ScriptCmdEnt ap_rpc_v3_[0-9_]+ activate$")

    def test_progressive_stage_and_multi_command_order_are_preserved(self):
        progressive = compile_item_delivery_plan(7770088, self.definitions, stage=2)
        self.assertEqual(progressive.stage, 2)
        self.assertEqual(progressive.commands[0].entity, "ap_rpc_v3_7770088_2")
        multi = compile_item_delivery_plan(7770012, self.definitions)
        self.assertEqual(
            [command.entity for command in multi.commands],
            [f"ap_rpc_v3_7770012_{index}" for index in range(len(multi.commands))],
        )

    def test_all_masteries_are_typed_perks_with_ordered_give_then_activate(self):
        generated = generate_rpc_command_entities(self.definitions)
        for entry in load_challenge_registry()["weapon_masteries"]:
            perk = entry["gameplay_perk"]
            self.assertEqual(
                self.definitions[entry["item_id"]],
                {"type": "perk", "perk": perk},
            )
            body = generated.split(
                f"entityDef ap_rpc_v3_{entry['item_id']} {{", 1
            )[1].split("\n}\n", 1)[0]
            self.assertIn(
                f"givePlayerPerk {perk};ai_ScriptCmdEnt player1 activatePlayerPerk {perk}",
                body,
            )

    def test_currency_path_is_direct_and_rejected_inherit_is_absent(self):
        generated = generate_rpc_command_entities(self.definitions)
        marker = "entityDef ap_rpc_v3_7770016 {"
        body = generated.split(marker, 1)[1].split("\n}\n", 1)[0]
        self.assertIn('class = "idTarget_GiveItems";', body)
        self.assertNotIn("inherit =", body)
        self.assertNotIn("CURRENCY_WEAPON_MASTERY", generated)
        self.assertNotIn('inherit = "target/give_item";', generated)

    def test_battery_single_and_bundle_have_exact_direct_currency_counts(self):
        generated = generate_rpc_command_entities(self.definitions)
        for item_id, count in ((7770016, 1), (7770142, 2)):
            body = generated.split(
                f"entityDef ap_rpc_v3_{item_id} {{", 1
            )[1].split("\n}\n", 1)[0]
            self.assertIn('class = "idTarget_GiveItems";', body)
            self.assertIn('currencyType = "CURRENCY_SENTINEL_BATTERY";', body)
            self.assertIn(f"count = {count};", body)
            self.assertNotIn("inherit =", body)

    def test_normal_build_contains_no_stat_write_bootstrap_controls(self):
        controls = generate_bootstrap_entities()
        self.assertEqual(controls, "")


class MapExpansionFoundationTests(unittest.TestCase):
    def test_five_release_maps_and_test_only_sixth_fixture(self):
        registry = load_map_registry()
        self.assertEqual(len(release_plan(registry)), 5)
        fixture = copy.deepcopy(registry["maps"]["e1m1_intro"])
        fixture.update({
            "display_name": "Registry Sixth Fixture", "test_only": True,
            "onboarding_status": "onboarding",
            "onboarding_audit": "tests/fixtures/fifth-map-audit.json",
            "generated_output": "fixture_sixth.entities",
            "runtime_map": "test/fixture/sixth",
        })
        registry["maps"]["fixture_sixth"] = fixture
        validate_map_registry(registry)
        for plan_builder in (generation_plan, validation_plan, package_plan):
            self.assertEqual(plan_builder(registry)[-1].map_key, "fixture_sixth")
        self.assertFalse(package_plan(registry)[-1].release_asset)
        self.assertNotIn("fixture_sixth", {p.map_key for p in release_plan(registry)})

    def test_unknown_registry_and_audit_fields_fail(self):
        registry = copy.deepcopy(load_map_registry())
        registry["maps"]["hub"]["typo"] = True
        with self.assertRaisesRegex(ValueError, "unknown"):
            validate_map_registry(registry)
        location = {key: [] for key in LOCATION_KEYS}
        location["unused"] = True
        with self.assertRaisesRegex(ValueError, "unknown"):
            _exact_keys(location, LOCATION_KEYS, "fixture")
        with self.assertRaisesRegex(ValueError, "missing"):
            _exact_keys({"target": "give_reward"}, EDGE_KEYS, "edge")


if __name__ == "__main__":
    unittest.main()
