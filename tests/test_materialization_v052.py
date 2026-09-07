import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHIPELAGO_ROOT = (REPO_ROOT.parent / "Archipelago").resolve()
if str(ARCHIPELAGO_ROOT) not in sys.path:
    sys.path.insert(0, str(ARCHIPELAGO_ROOT))

import doom_eap.runtime.bridge_client as bridge_client
from doom_eap.runtime.context_registry import (
    GATE_KEY_TO_MAP,
    TAG_SPECIAL_CAPABILITY,
    classify_runtime_context,
)
from tools.decls.devinv_builder import (
    TAG_REQUIRED_BLOOD_PUNCH_PERKS,
    build_devinv_loadout,
    build_tag_devinv_overrides,
    validate_tag_devinv_source,
)

NetworkItem = SimpleNamespace


def _create_test_context(campaign="TAG1", map_key="e4m1_rig", runtime_map="game/dlc/e4m1_rig/e4m1_rig"):
    if 7770010 not in bridge_client.ITEM_ID_TO_COMMAND:
        with open(REPO_ROOT / "data" / "items.json", encoding="utf-8") as f:
            bridge_client.ITEM_ID_TO_COMMAND = {
                int(k): v for k, v in json.load(f).items()
            }
    context = object.__new__(bridge_client.DoomEternalContext)
    context.state_key = "room:test:1"
    context.team = 0
    context.slot = 1
    context.room_seed_name = "test_seed"
    context.session_state = {}
    context.items_received = []
    context.items_processed = 0
    context.item_state_ready = True
    context.server_checked_locations_ready = True
    context.checked_locations = set()
    context.locations_checked = set()
    context._pending_materialization_triggers = set()
    context.pending_context_transition = None
    context.context_campaign = campaign
    context.published_materialization_lease = "1:1"
    context.cached_map_identity = {
        "gameplay_epoch": "1:1",
        "runtime_map": runtime_map,
        "map_key": map_key,
    }
    context._connected_slot_data = {
        "slot_data_version": 1,
        "slot_data_revision": "0.5-D",
        "required_capabilities": ["cross_campaign_materialization_v1"],
        "use_dlc_content": True,
        "include_dlc_missions": True,
        "dlc_logic_timing": "late_game",
        "special_weapon": "progressive_special_weapon",
    }
    context.dlc_evidence = SimpleNamespace(blocks_enabled=False, reason=None)
    context.get_ap_state_key = lambda: "room:test:1"
    context.persist_session_state = lambda: None
    context.runtime_effects_ready = lambda *args, **kwargs: True
    context.active_save_slot = "DLC1-AUTOSAVE1"
    return context


class TestChainsawHistoricalOwnership(unittest.TestCase):
    def test_case_a_randomize_chainsaw_false_with_hoe_check_materializes_chainsaw_in_dlc(self):
        ctx = _create_test_context(campaign="TAG1", map_key="e4m1_rig", runtime_map="game/dlc/e4m1_rig/e4m1_rig")
        ctx._connected_slot_data["randomize_chainsaw"] = False
        ctx.checked_locations = {7770002}  # Hell on Earth - Heavy Cannon
        evidence = SimpleNamespace(epoch=1, state="gameplay", native_safe=True)

        sent_commands = []
        with patch("doom_eap.runtime.bridge_client.send_command", side_effect=lambda cmd, **kw: sent_commands.append(cmd) or True):
            plan, error = ctx._context_materialize_inventory(evidence, trigger="context")

        self.assertIsNone(error)
        self.assertIsNotNone(plan)
        command_texts = [cmd.command for cmd in plan.commands]
        self.assertTrue(
            any(cmd.item_id == 7770010 or "7770010" in cmd.command for cmd in plan.commands),
            f"Chainsaw command missing in plan: {command_texts}",
        )

    def test_case_b_randomize_chainsaw_false_with_zero_hoe_checks_does_not_materialize_chainsaw(self):
        ctx = _create_test_context(campaign="TAG1", map_key="e4m1_rig", runtime_map="game/dlc/e4m1_rig/e4m1_rig")
        ctx._connected_slot_data["randomize_chainsaw"] = False
        ctx.checked_locations = {7770025}  # Exultia location, not HoE
        evidence = SimpleNamespace(epoch=1, state="gameplay", native_safe=True)

        sent_commands = []
        with patch("doom_eap.runtime.bridge_client.send_command", side_effect=lambda cmd, **kw: sent_commands.append(cmd) or True):
            plan, error = ctx._context_materialize_inventory(evidence, trigger="context")

        self.assertIsNone(error)
        command_texts = [cmd.command for cmd in plan.commands] if plan else []
        self.assertFalse(
            any(cmd.item_id == 7770010 or "7770010" in cmd.command for cmd in plan.commands) if plan else False,
            f"Chainsaw unexpectedly materialized without HoE checks: {command_texts}",
        )

    def test_case_c_randomize_chainsaw_true_with_hoe_checks_unreceived_does_not_materialize_chainsaw(self):
        ctx = _create_test_context(campaign="TAG1", map_key="e4m1_rig", runtime_map="game/dlc/e4m1_rig/e4m1_rig")
        ctx._connected_slot_data["randomize_chainsaw"] = True
        ctx.checked_locations = {7770001, 7770002}
        evidence = SimpleNamespace(epoch=1, state="gameplay", native_safe=True)

        sent_commands = []
        with patch("doom_eap.runtime.bridge_client.send_command", side_effect=lambda cmd, **kw: sent_commands.append(cmd) or True):
            plan, error = ctx._context_materialize_inventory(evidence, trigger="context")

        self.assertIsNone(error)
        command_texts = [cmd.command for cmd in plan.commands] if plan else []
        self.assertFalse(
            any(cmd.item_id == 7770010 or "7770010" in cmd.command for cmd in plan.commands) if plan else False,
            f"Chainsaw materialized when randomize_chainsaw=True but unreceived: {command_texts}",
        )

    def test_case_d_randomize_chainsaw_true_with_chainsaw_received_materializes_normally(self):
        ctx = _create_test_context(campaign="TAG1", map_key="e4m1_rig", runtime_map="game/dlc/e4m1_rig/e4m1_rig")
        ctx._connected_slot_data["randomize_chainsaw"] = True
        ctx.items_received = [NetworkItem(item=7770010, location=0, player=1, flags=0)]
        ctx.items_processed = 1
        evidence = SimpleNamespace(epoch=1, state="gameplay", native_safe=True)

        sent_commands = []
        with patch("doom_eap.runtime.bridge_client.send_command", side_effect=lambda cmd, **kw: sent_commands.append(cmd) or True):
            plan, error = ctx._context_materialize_inventory(evidence, trigger="context")

        self.assertIsNone(error)
        self.assertIsNotNone(plan)
        command_texts = [cmd.command for cmd in plan.commands]
        self.assertTrue(
            any(cmd.item_id == 7770010 or "7770010" in cmd.command for cmd in plan.commands),
            f"Chainsaw command missing when received in items_received: {command_texts}",
        )


class TestBloodPunchDLC(unittest.TestCase):
    def test_build_tag_devinv_overrides_includes_required_blood_punch_perks(self):
        overrides = build_tag_devinv_overrides({}, "Combat Shotgun")
        self.assertGreaterEqual(len(overrides), 6)
        expected_perks = {
            "perk/player/blood_punch/area_of_effect",
            "perk/player/blood_punch/ai_charge_rate",
            "perk/player/blood_punch/max_charges",
        }
        for path, decl_text in overrides.items():
            for perk in expected_perks:
                self.assertIn(perk, decl_text, f"Missing {perk} in {path}")
            self.assertNotIn("perk/player/blood_punch/base", decl_text, f"Unconditionally granted blood_punch/base in {path}")
            validate_tag_devinv_source(decl_text)

    def test_validator_fails_if_blood_punch_perk_is_missing(self):
        overrides = build_tag_devinv_overrides({}, "Combat Shotgun")
        sample_decl = next(iter(overrides.values()))
        tampered = sample_decl.replace('perk = "perk/player/blood_punch/area_of_effect";', 'perk = "perk/player/other";')
        with self.assertRaises(ValueError):
            validate_tag_devinv_source(tampered)

    def test_base_campaign_devinv_loadout_builder_unaltered(self):
        base_decl = build_devinv_loadout({}, "Combat Shotgun")
        self.assertIn("startingInventory", base_decl)
        self.assertIn("weapon/player/shotgun", base_decl)


class TestSentinelHammerTAG2Upgrades(unittest.TestCase):
    def test_legitimate_hammer_in_tag2_includes_upgrade_perks(self):
        ctx = _create_test_context(campaign="TAG2", map_key="e5m1_spear", runtime_map="game/dlc2/e5m1_spear/e5m1_spear")
        ctx.published_materialization_lease = "1:1"
        ctx.cached_map_identity["gameplay_epoch"] = "1:1"
        ctx.items_received = [
            NetworkItem(item=7770901, location=0, player=1, flags=0),
            NetworkItem(item=7770901, location=1, player=1, flags=0),
        ]
        ctx.items_processed = 2
        evidence = SimpleNamespace(epoch=1, state="gameplay", native_safe=True)

        sent_commands = []
        with patch("doom_eap.runtime.bridge_client.send_command", side_effect=lambda cmd, **kw: sent_commands.append(cmd) or True):
            plan, error = ctx._context_materialize_inventory(evidence, trigger="context")

        self.assertIsNone(error)
        self.assertIsNotNone(plan)
        command_texts = [cmd.command for cmd in plan.commands]
        self.assertTrue(any("7770901" in c for c in command_texts))
        self.assertTrue(any("perk/player/weapons/hammer/ammo_drops_upgraded" in c for c in command_texts))
        self.assertTrue(any("perk/player/weapons/hammer/armor_and_health_drops_upgraded" in c for c in command_texts))

    def test_no_hammer_ownership_does_not_grant_hammer_or_upgrades(self):
        ctx = _create_test_context(campaign="TAG2", map_key="e5m1_spear", runtime_map="game/dlc2/e5m1_spear/e5m1_spear")
        ctx.items_received = [
            NetworkItem(item=7770901, location=0, player=1, flags=0),
        ]
        ctx.items_processed = 1
        evidence = SimpleNamespace(epoch=1, state="gameplay", native_safe=True)

        sent_commands = []
        with patch("doom_eap.runtime.bridge_client.send_command", side_effect=lambda cmd, **kw: sent_commands.append(cmd) or True):
            plan, error = ctx._context_materialize_inventory(evidence, trigger="context")

        self.assertIsNone(error)
        command_texts = [cmd.command for cmd in plan.commands] if plan else []
        self.assertFalse(any("hammer" in c.lower() for c in command_texts))

    def test_non_tag2_context_does_not_grant_hammer_upgrades(self):
        ctx = _create_test_context(campaign="TAG1", map_key="e4m1_rig", runtime_map="game/dlc/e4m1_rig/e4m1_rig")
        ctx.items_received = [
            NetworkItem(item=7770901, location=0, player=1, flags=0),
            NetworkItem(item=7770901, location=1, player=1, flags=0),
        ]
        ctx.items_processed = 2
        evidence = SimpleNamespace(epoch=1, state="gameplay", native_safe=True)

        sent_commands = []
        with patch("doom_eap.runtime.bridge_client.send_command", side_effect=lambda cmd, **kw: sent_commands.append(cmd) or True):
            plan, error = ctx._context_materialize_inventory(evidence, trigger="context")

        self.assertIsNone(error)
        command_texts = [cmd.command for cmd in plan.commands] if plan else []
        self.assertFalse(any("perk/player/weapons/hammer" in c for c in command_texts))


class TestSlayerGateKeyRematerialization(unittest.TestCase):
    def test_cultist_base_gate_key_delivered_on_initial_materialization(self):
        ctx = _create_test_context(campaign="Base", map_key="e1m3_cult", runtime_map="game/sp/e1m3_cult/e1m3_cult")
        ctx.items_received = [NetworkItem(item=7770151, location=0, player=1, flags=0)]
        ctx.items_processed = 1
        evidence = SimpleNamespace(epoch=1, state="gameplay", native_safe=True)

        sent_commands = []
        with patch("doom_eap.runtime.bridge_client.send_command", side_effect=lambda cmd, **kw: sent_commands.append(cmd) or True):
            plan, error = ctx._context_materialize_inventory(evidence, trigger="context")

        self.assertIsNone(error)
        self.assertIsNotNone(plan)
        command_texts = [cmd.command for cmd in plan.commands]
        self.assertTrue(
            any("7770151" in c or "slayer_key" in c for c in command_texts),
            f"Gate key command missing: {command_texts}",
        )

    def test_same_stable_epoch_does_not_spam_gate_key(self):
        ctx = _create_test_context(campaign="Base", map_key="e1m3_cult", runtime_map="game/sp/e1m3_cult/e1m3_cult")
        ctx.items_received = [NetworkItem(item=7770151, location=0, player=1, flags=0)]
        ctx.items_processed = 1
        evidence = SimpleNamespace(epoch=1, state="gameplay", native_safe=True)

        with patch("doom_eap.runtime.bridge_client.send_command", return_value=True):
            plan, error = ctx._context_materialize_inventory(evidence, trigger="context")
        self.assertIsNotNone(plan)

        sent_commands = []
        with patch("doom_eap.runtime.bridge_client.send_command", side_effect=lambda cmd, **kw: sent_commands.append(cmd) or True):
            plan2, error2 = ctx._context_materialize_inventory(evidence, trigger="poll")

        self.assertIsNone(error2)
        self.assertIsNone(plan2)
        self.assertEqual(len(sent_commands), 0, "Gate key spammed during stable epoch!")
        self.assertEqual(ctx.context_materialization_status, "completed_noop")

    def test_new_reloaded_map_epoch_rematerializes_owned_gate_key(self):
        ctx = _create_test_context(campaign="Base", map_key="e1m3_cult", runtime_map="game/sp/e1m3_cult/e1m3_cult")
        ctx.items_received = [NetworkItem(item=7770151, location=0, player=1, flags=0)]
        ctx.items_processed = 1
        evidence = SimpleNamespace(epoch=1, state="gameplay", native_safe=True)

        with patch("doom_eap.runtime.bridge_client.send_command", return_value=True):
            ctx._context_materialize_inventory(evidence, trigger="context")

        ctx.published_materialization_lease = "2:1"
        ctx.cached_map_identity["gameplay_epoch"] = "2:1"
        evidence_reload = SimpleNamespace(epoch=2, state="gameplay", native_safe=True)

        sent_commands = []
        with patch("doom_eap.runtime.bridge_client.send_command", side_effect=lambda cmd, **kw: sent_commands.append(cmd) or True):
            plan_reload, error_reload = ctx._context_materialize_inventory(evidence_reload, trigger="level_ready")

        self.assertIsNone(error_reload)
        self.assertIsNotNone(plan_reload)
        command_texts = [cmd.command for cmd in plan_reload.commands]
        self.assertTrue(
            any("7770151" in c or "slayer_key" in c for c in command_texts),
            f"Gate key not re-materialized on new epoch: {command_texts}",
        )
        self.assertEqual(ctx.context_materialization_status, "gate_key_rematerialized")

    def test_unowned_gate_key_never_granted(self):
        ctx = _create_test_context(campaign="Base", map_key="e1m3_cult", runtime_map="game/sp/e1m3_cult/e1m3_cult")
        ctx.items_received = []
        ctx.items_processed = 0
        evidence = SimpleNamespace(epoch=1, state="gameplay", native_safe=True)

        sent_commands = []
        with patch("doom_eap.runtime.bridge_client.send_command", side_effect=lambda cmd, **kw: sent_commands.append(cmd) or True):
            plan, error = ctx._context_materialize_inventory(evidence, trigger="context")

        command_texts = [cmd.command for cmd in plan.commands] if plan else []
        self.assertFalse(any("slayer_key" in c or "7770151" in c for c in command_texts))

    def test_generic_gate_key_audit_exultia_key(self):
        ctx = _create_test_context(campaign="Base", map_key="e1m2_war", runtime_map="game/sp/e1m2_battle/e1m2_battle")
        ctx.items_received = [NetworkItem(item=7770150, location=0, player=1, flags=0)]
        ctx.items_processed = 1
        evidence = SimpleNamespace(epoch=1, state="gameplay", native_safe=True)

        with patch("doom_eap.runtime.bridge_client.send_command", return_value=True):
            plan, error = ctx._context_materialize_inventory(evidence, trigger="context")
        self.assertIsNotNone(plan)

        ctx.published_materialization_lease = "2:1"
        ctx.cached_map_identity["gameplay_epoch"] = "2:1"
        evidence_reload = SimpleNamespace(epoch=2, state="gameplay", native_safe=True)

        sent_commands = []
        with patch("doom_eap.runtime.bridge_client.send_command", side_effect=lambda cmd, **kw: sent_commands.append(cmd) or True):
            plan_reload, error_reload = ctx._context_materialize_inventory(evidence_reload, trigger="level_ready")

        self.assertIsNone(error_reload)
        self.assertIsNotNone(plan_reload)
        command_texts = [cmd.command for cmd in plan_reload.commands]
        self.assertTrue(any("7770150" in c or "slayer_key" in c for c in command_texts))


class TestCyberMancubusToyHitbox(unittest.TestCase):
    def test_cyber_mancubus_toy_bounds_reduced_and_does_not_cross_gate(self):
        loc_path = REPO_ROOT / "content" / "maps" / "e3m1_slayer" / "locations.json"
        data = json.loads(loc_path.read_text(encoding="utf-8"))
        toy_policy = data["target_policies"]["pickups_pickup_collectible_toys_mancubus_goo_1"]

        self.assertTrue(toy_policy.get("independent_ap_trigger"))
        size = toy_policy.get("independent_size")
        self.assertEqual(size, [1.3, 1.3, 1.3], f"Expected size [1.3, 1.3, 1.3], got {size}")

        toy_origin_x = -81.9301224
        gate_origin_x = -80.2495728
        half_x = size[0] / 2.0
        max_extent_x = toy_origin_x + half_x

        self.assertLess(
            max_extent_x,
            gate_origin_x,
            f"Trigger extends past the gate: max_extent_x={max_extent_x} >= gate_origin_x={gate_origin_x}",
        )
        margin = gate_origin_x - max_extent_x
        self.assertGreater(margin, 1.0, f"Margin between trigger and gate should be > 1.0m, got {margin}")


class TestDarkLordGoalAuthority(unittest.TestCase):
    def test_dark_lord_goal_objective_ids_include_7770419(self):
        slot_data = {
            "goal": "Kill the Dark Lord",
            "goal_endpoint_event": "Internal Goal Endpoint: Kill the Dark Lord",
            "goal_endpoint_available": True,
            "required_capabilities": list(bridge_client.GOAL_CAPABILITIES),
            "additional_victory_requirements": [],
            "use_dlc_content": True,
            "include_dlc_missions": True,
        }
        objective_ids = bridge_client.DoomEternalContext.goal_objective_ids(slot_data)
        self.assertIn(7770419, objective_ids)

    def test_full_saga_goal_objective_ids_include_7770419(self):
        slot_data = {
            "goal": "Complete the Full Saga",
            "goal_endpoint_event": "Internal Goal Endpoint: Complete the Full Saga",
            "goal_endpoint_available": True,
            "required_capabilities": list(bridge_client.GOAL_CAPABILITIES),
            "additional_victory_requirements": [],
            "use_dlc_content": True,
            "include_dlc_missions": True,
        }
        objective_ids = bridge_client.DoomEternalContext.goal_objective_ids(slot_data)
        self.assertIn(7770419, objective_ids)
        self.assertIn(7770485, objective_ids)

    def test_immora_complete_alone_does_not_fire_goal(self):
        ctx = _create_test_context(campaign="TAG2", map_key="e5m3_hell", runtime_map="game/dlc2/e5m3_hell/e5m3_hell")
        ctx._connected_slot_data = {
            "goal": "Kill the Dark Lord",
            "goal_endpoint_event": "Internal Goal Endpoint: Kill the Dark Lord",
            "goal_endpoint_available": True,
            "required_capabilities": list(bridge_client.GOAL_CAPABILITIES),
            "additional_victory_requirements": [],
            "use_dlc_content": True,
            "include_dlc_missions": True,
        }
        ctx.checked_locations = {7770485}
        ctx.server_checked_locations_ready = True
        ctx.server = SimpleNamespace(socket=SimpleNamespace(closed=False))
        dispatched_messages = []

        async def fake_send_msgs(msgs):
            dispatched_messages.extend(msgs)

        ctx.send_msgs = fake_send_msgs

        result = asyncio.run(ctx.evaluate_campaign_goal("test_evaluation"))
        self.assertFalse(result)
        self.assertFalse(getattr(ctx, "goal_dispatch_sent", False))
        self.assertEqual(len(dispatched_messages), 0)

    def test_dark_lord_check_dispatches_goal(self):
        ctx = _create_test_context(campaign="Dark Lord", map_key="e5m4_boss", runtime_map="game/dlc2/e5m4_boss/e5m4_boss")
        ctx._connected_slot_data = {
            "goal": "Kill the Dark Lord",
            "goal_endpoint_event": "Internal Goal Endpoint: Kill the Dark Lord",
            "goal_endpoint_available": True,
            "required_capabilities": list(bridge_client.GOAL_CAPABILITIES),
            "additional_victory_requirements": [],
            "use_dlc_content": True,
            "include_dlc_missions": True,
        }
        ctx.checked_locations = {7770485, 7770419}
        ctx.server_checked_locations_ready = True
        ctx.server = SimpleNamespace(socket=SimpleNamespace(closed=False))
        dispatched_messages = []

        async def fake_send_msgs(msgs):
            dispatched_messages.extend(msgs)

        ctx.send_msgs = fake_send_msgs

        result = asyncio.run(ctx.evaluate_campaign_goal("test_evaluation"))
        self.assertTrue(result)
        self.assertTrue(getattr(ctx, "goal_dispatch_sent", True))
        self.assertEqual(len(dispatched_messages), 1)
        self.assertEqual(dispatched_messages[0]["status"], bridge_client.ClientStatus.CLIENT_GOAL)



class TestLinuxInjectorSettingsEnforcement(unittest.TestCase):
    def test_configure_first_run_enforces_settings_when_file_exists(self):
        from doom_eap.launcher.launcher_platform import LinuxModManagerAdapter
        import tempfile
        import shutil
        from pathlib import Path

        temp_dir = tempfile.mkdtemp()
        try:
            game_root = Path(temp_dir)
            settings_file = game_root / "EternalModInjector Settings.txt"
            settings_file.write_text(":AUTO_UPDATE=1\n:AUTO_LAUNCH_GAME=1\n:SOME_OTHER_SETTING=1\n", encoding="utf-8")

            LinuxModManagerAdapter._configure_first_run(game_root)

            content = settings_file.read_text(encoding="utf-8")
            self.assertIn(":AUTO_UPDATE=0", content)
            self.assertIn(":AUTO_LAUNCH_GAME=0", content)
            self.assertIn(":SOME_OTHER_SETTING=1", content)
            self.assertNotIn(":AUTO_UPDATE=1", content)
            self.assertNotIn(":AUTO_LAUNCH_GAME=1", content)
        finally:
            shutil.rmtree(temp_dir)


class TestTag1FastTravelNeutralization(unittest.TestCase):
    def test_vanilla_fast_travel_unlock_entities_stripped(self):
        from tools.maps.ap_map_generator import remove_balanced_entity_blocks
        sample_map = (
            'entity {\n'
            '\tentityDef fast_travel_trigger_unlock_fast_travel {\n'
            '\t\titem[0] = "fast_travel_target_fast_travel_unlock_2";\n'
            '\t}\n'
            '}\n'
            'entity {\n'
            '\tentityDef fast_travel_target_fast_travel_unlock_2 {\n'
            '\t\tinherit = "target/fast_travel_unlock";\n'
            '\t\tclass = "idTarget_FastTravelUnlock";\n'
            '\t}\n'
            '}\n'
            'entity {\n'
            '\tentityDef fasttravel_target_fast_travel_unlock_1 {\n'
            '\t\tinherit = "target/fast_travel_unlock";\n'
            '\t\tclass = "idTarget_FastTravelUnlock";\n'
            '\t}\n'
            '}\n'
            'entity {\n'
            '\tentityDef player_start {\n'
            '\t\tclass = "idPlayerStart";\n'
            '\t}\n'
            '}\n'
        )
        content = remove_balanced_entity_blocks(sample_map, "fast_travel_target_fast_travel_unlock_2")
        content = remove_balanced_entity_blocks(content, "fasttravel_target_fast_travel_unlock_1")
        self.assertNotIn("entityDef fast_travel_target_fast_travel_unlock_2", content)
        self.assertNotIn("entityDef fasttravel_target_fast_travel_unlock_1", content)
        self.assertIn("entityDef player_start", content)
        self.assertIn("entityDef fast_travel_trigger_unlock_fast_travel", content)

    def test_blood_swamps_first_visit_no_fast_travel_vs_replay_eligible(self):
        ctx_swamp = _create_test_context(campaign="TAG1", map_key="e4m2_swamp", runtime_map="game/dlc/e4m2_swamp/e4m2_swamp")
        # First visit: uncompleted mission -> no fast travel
        ctx_swamp.checked_locations = set()
        snapshot_swamp = ctx_swamp.snapshot_fast_travel_eligibility(refresh=True)
        self.assertIsNone(snapshot_swamp)
        self.assertFalse(ctx_swamp.fast_travel_epoch_state.get("completed_before_epoch"))
        self.assertEqual(ctx_swamp.fast_travel_epoch_state.get("ineligible_reason"), "not_completed_before_epoch")

        # Replay visit: completed mission (7770443) -> fast travel eligible
        ctx_swamp.checked_locations = {7770443}
        snapshot_swamp_replay = ctx_swamp.snapshot_fast_travel_eligibility(refresh=True)
        self.assertIsNotNone(snapshot_swamp_replay)
        self.assertTrue(ctx_swamp.fast_travel_epoch_state.get("completed_before_epoch"))
        self.assertEqual(snapshot_swamp_replay[1], "e4m2_swamp")

    def test_the_holt_first_visit_no_fast_travel_vs_replay_eligible(self):
        ctx_holt = _create_test_context(campaign="TAG1", map_key="e4m3_mcity", runtime_map="game/dlc/e4m3_mcity/e4m3_mcity")
        # First visit: uncompleted mission -> no fast travel
        ctx_holt.checked_locations = set()
        snapshot_holt = ctx_holt.snapshot_fast_travel_eligibility(refresh=True)
        self.assertIsNone(snapshot_holt)
        self.assertFalse(ctx_holt.fast_travel_epoch_state.get("completed_before_epoch"))
        self.assertEqual(ctx_holt.fast_travel_epoch_state.get("ineligible_reason"), "not_completed_before_epoch")

        # Replay visit: completed mission (7770457) -> fast travel eligible
        ctx_holt.checked_locations = {7770457}
        snapshot_holt_replay = ctx_holt.snapshot_fast_travel_eligibility(refresh=True)
        self.assertIsNotNone(snapshot_holt_replay)
        self.assertTrue(ctx_holt.fast_travel_epoch_state.get("completed_before_epoch"))
        self.assertEqual(snapshot_holt_replay[1], "e4m3_mcity")


if __name__ == "__main__":
    unittest.main()
