import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHIPELAGO_ROOT = (REPO_ROOT.parent / "Archipelago").resolve()
if str(ARCHIPELAGO_ROOT) not in sys.path:
    sys.path.insert(0, str(ARCHIPELAGO_ROOT))

import doom_eap.runtime.bridge_client as bridge_client
from doom_eap.runtime.bridge_client import (
    GameplaySaveEvidence,
    PrimarySaveSelection,
    expected_save_prefix_for_campaign,
    MAP_ENTITY_SAFE,
    CHECKED_VISUAL_HIDE,
)

NetworkItem = SimpleNamespace


def _active_bridge_client():
    return sys.modules.get("doom_eap.runtime.bridge_client", bridge_client)


def _create_test_context():
    bc = _active_bridge_client()
    context = object.__new__(bc.DoomEternalContext)
    context.state_key = "room:test:1"
    context.team = 0
    context.slot = 1
    context.room_seed_name = "test_seed"
    context.session_state = {}
    context.client_state = {"version": 1, "sessions": {}}
    context.items_received = []
    context.items_processed = 0
    context.item_state_ready = True
    context.save_candidate_tokens = {}
    context.save_slot_observations = {}
    context.selected_observation_slot = None
    context.active_save_slot = None
    context.active_save_path = None
    context.active_save_token = None
    context.active_native_evidence_epoch = None
    context.active_save_proof_evidence_epoch = None
    context.active_save_proof_load_epoch = None
    context.active_save_proof_authoritative = False
    context.active_save_proof_slot = None
    context.runtime_observers_frozen = True
    context.mission_select_observation_map = None
    context.mission_select_observation_epoch = None
    context.last_observer_lease_block = None
    context.last_save_proof_rejection = None
    context.last_accepted_marker_mtime = 0
    context.last_accepted_map_evidence_epoch = None
    context.last_marker_reject_reason = None
    context.last_duration_cache_key = None
    context.death_probe_warning = None
    context.death_link_enabled = True
    context.cached_map_identity = None
    context.pending_map_identity = None
    context.current_map_name = None
    context.runtime_observation_lease = None
    context.transient_effect_manager = SimpleNamespace(reset=lambda *args: None)
    context.checkpoint_death_by_save_slot = {}
    context.death_detector_initialized_slots = set()
    context.death_consumed_tokens = set()
    context.awaiting_respawn_carryover_by_save_slot = {}
    context.death_consumed_in_epoch_by_save_slot = {}
    context.last_consumed_death_event_by_save_slot = {}
    context.fast_travel_submitted = {}
    context.fast_travel_epoch_state = None
    context.fast_travel_eligibility_snapshot = None
    context.fast_travel_last_transition = None
    context.automap_cleanup_epoch = None
    context.automap_cleanup_session = "test_cleanup_session"
    context.automap_cleanup_submitted = set()
    context.automap_cleanup_retry = {}
    context.automap_cleanup_status = {}
    context.automap_local_cleanup_owned = set()
    context.completed_level_ready_epochs = set()
    context.pending_level_ready = {}
    context.server_checked_locations_ready = True
    context.checked_locations = set()
    context._item_delivery_lock = asyncio.Lock()
    context.get_ap_state_key = lambda: "room:test:1"
    context.persist_session_state = lambda: None
    context.reconcile_fast_travel_unlock = lambda *args, **kwargs: None
    context.arm_final_sin_completion_candidate = lambda *args, **kwargs: None
    context._record_context_evidence_rejection = lambda *args, **kwargs: None
    context.runtime_effects_ready = lambda *args, **kwargs: True
    context.ingest_visible_runtime_lifecycle = lambda *args, **kwargs: False
    return context


def _create_materialization_context(campaign="TAG1", map_key="e4m1_rig", runtime_map="game/dlc/e4m1_rig/e4m1_rig"):
    bc = _active_bridge_client()
    if 7770010 not in bc.ITEM_ID_TO_COMMAND:
        with open(REPO_ROOT / "data" / "items.json", encoding="utf-8") as f:
            bc.ITEM_ID_TO_COMMAND = {
                int(k): v for k, v in json.load(f).items()
            }
    context = object.__new__(bc.DoomEternalContext)
    context.state_key = "room:test:1"
    context.team = 0
    context.slot = 1
    context.room_seed_name = "test_seed"
    context.session_state = {}
    context.client_state = {"version": 1, "sessions": {}}
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
    context.fast_travel_submitted = {}
    context.fast_travel_epoch_state = None
    context.fast_travel_eligibility_snapshot = None
    context.fast_travel_last_transition = None
    return context


class TestCrossCampaignSaveAuthority(unittest.TestCase):
    def setUp(self):
        self.bc = _active_bridge_client()

    def test_expected_save_prefix_mapping(self):
        self.assertEqual(expected_save_prefix_for_campaign("Base"), "GAME-AUTOSAVE")
        self.assertEqual(expected_save_prefix_for_campaign("TAG1"), "DLC1-AUTOSAVE")
        self.assertEqual(expected_save_prefix_for_campaign("ARC"), "DLC1-AUTOSAVE")
        self.assertEqual(expected_save_prefix_for_campaign("TAG2"), "DLC2-AUTOSAVE")
        self.assertEqual(expected_save_prefix_for_campaign("Dark Lord"), "DLC2-AUTOSAVE")
        self.assertEqual(expected_save_prefix_for_campaign("Horde"), "HORDE-AUTOSAVE")
        self.assertIsNone(expected_save_prefix_for_campaign(None))
        self.assertIsNone(expected_save_prefix_for_campaign("Unknown"))

    def test_base_gameplay_selects_game_autosave(self):
        context = _create_test_context()
        mock_candidates = [
            PrimarySaveSelection("GAME-AUTOSAVE1", Path("/fake/GAME-AUTOSAVE1/game_duration.dat"), 1000),
        ]
        evidence = GameplaySaveEvidence("gameplay", 1, "GAME-AUTOSAVE1", "game/sp/e1m1_intro/e1m1_intro", native_safe=True)

        with patch.object(self.bc, "primary_save_candidates", return_value=mock_candidates), \
             patch.object(self.bc, "primary_save_for_slot", side_effect=lambda s: mock_candidates[0] if s == "GAME-AUTOSAVE1" else None), \
             patch.object(self.bc, "read_gameplay_save_evidence", return_value=evidence), \
             patch.object(self.bc, "read_game_details_for_selection", return_value={"mapName": "game/sp/e1m1_intro/e1m1_intro", "_mtime_ns": 1000}), \
             patch.object(context, "read_active_map_identity", return_value={"runtime_map": "game/sp/e1m1_intro/e1m1_intro", "map_key": "e1m1_intro"}):
            selected = context.update_save_slot_lifecycle()

        self.assertIsNotNone(selected)
        self.assertEqual(selected.slot_directory, "GAME-AUTOSAVE1")
        self.assertEqual(context.active_save_slot, "GAME-AUTOSAVE1")
        self.assertTrue(context.active_save_proof_authoritative)

    def test_base_to_tag1_switches_to_dlc1_autosave(self):
        context = _create_test_context()
        context.active_save_slot = "GAME-AUTOSAVE1"
        context.active_save_path = "/fake/GAME-AUTOSAVE1/game_duration.dat"
        context.active_save_proof_authoritative = True
        context.active_save_proof_slot = "GAME-AUTOSAVE1"

        dlc_candidate = PrimarySaveSelection("DLC1-AUTOSAVE1", Path("/fake/DLC1-AUTOSAVE1/game_duration.dat"), 2000)
        base_candidate = PrimarySaveSelection("GAME-AUTOSAVE1", Path("/fake/GAME-AUTOSAVE1/game_duration.dat"), 1000)

        def mock_candidates_fn(filename="game_duration.dat", slot_prefix=None):
            if slot_prefix == "DLC1-AUTOSAVE":
                return [dlc_candidate]
            if slot_prefix == "GAME-AUTOSAVE":
                return [base_candidate]
            return [dlc_candidate, base_candidate]

        def mock_primary_for_slot(slot):
            return dlc_candidate if slot == "DLC1-AUTOSAVE1" else (base_candidate if slot == "GAME-AUTOSAVE1" else None)

        evidence = GameplaySaveEvidence("gameplay", 2, "DLC1-AUTOSAVE1", "game/dlc/e4m1_rig/e4m1_rig", native_safe=True)

        with patch.object(self.bc, "primary_save_candidates", side_effect=mock_candidates_fn), \
             patch.object(self.bc, "primary_save_for_slot", side_effect=mock_primary_for_slot), \
             patch.object(self.bc, "read_gameplay_save_evidence", return_value=evidence), \
             patch.object(self.bc, "read_game_details_for_selection", return_value={"mapName": "game/dlc/e4m1_rig/e4m1_rig", "_mtime_ns": 2000}), \
             patch.object(context, "read_active_map_identity", return_value={"runtime_map": "game/dlc/e4m1_rig/e4m1_rig", "map_key": "e4m1_rig"}):
            selected = context.update_save_slot_lifecycle()

        self.assertIsNotNone(selected)
        self.assertEqual(selected.slot_directory, "DLC1-AUTOSAVE1")
        self.assertEqual(context.active_save_slot, "DLC1-AUTOSAVE1")
        self.assertEqual(context.selected_observation_slot, "DLC1-AUTOSAVE1")

    def test_tag1_to_base_switches_back_without_restart(self):
        context = _create_test_context()
        context.active_save_slot = "DLC1-AUTOSAVE1"
        context.active_save_path = "/fake/DLC1-AUTOSAVE1/game_duration.dat"
        context.active_save_proof_authoritative = True
        context.active_save_proof_slot = "DLC1-AUTOSAVE1"
        context.selected_observation_slot = "DLC1-AUTOSAVE1"

        dlc_candidate = PrimarySaveSelection("DLC1-AUTOSAVE1", Path("/fake/DLC1-AUTOSAVE1/game_duration.dat"), 3000)
        base_candidate = PrimarySaveSelection("GAME-AUTOSAVE1", Path("/fake/GAME-AUTOSAVE1/game_duration.dat"), 2500)

        def mock_candidates_fn(filename="game_duration.dat", slot_prefix=None):
            if slot_prefix == "GAME-AUTOSAVE":
                return [base_candidate]
            if slot_prefix == "DLC1-AUTOSAVE":
                return [dlc_candidate]
            return [dlc_candidate, base_candidate]

        def mock_primary_for_slot(slot):
            return base_candidate if slot == "GAME-AUTOSAVE1" else (dlc_candidate if slot == "DLC1-AUTOSAVE1" else None)

        evidence = GameplaySaveEvidence("gameplay", 3, "GAME-AUTOSAVE1", "game/sp/e3m2_hell/e3m2_hell", native_safe=True)

        with patch.object(self.bc, "primary_save_candidates", side_effect=mock_candidates_fn), \
             patch.object(self.bc, "primary_save_for_slot", side_effect=mock_primary_for_slot), \
             patch.object(self.bc, "read_gameplay_save_evidence", return_value=evidence), \
             patch.object(self.bc, "read_game_details_for_selection", return_value={"mapName": "game/sp/e3m2_hell/e3m2_hell", "_mtime_ns": 2500}), \
             patch.object(context, "read_active_map_identity", return_value={"runtime_map": "game/sp/e3m2_hell/e3m2_hell", "map_key": "e3m2_hell"}):
            selected = context.update_save_slot_lifecycle()

        self.assertIsNotNone(selected)
        self.assertEqual(selected.slot_directory, "GAME-AUTOSAVE1")
        self.assertEqual(context.active_save_slot, "GAME-AUTOSAVE1")
        self.assertEqual(context.selected_observation_slot, "GAME-AUTOSAVE1")

    def test_tag1_to_tag2_direct_transition(self):
        context = _create_test_context()
        context.active_save_slot = "DLC1-AUTOSAVE1"
        context.active_save_path = "/fake/DLC1-AUTOSAVE1/game_duration.dat"
        context.active_save_proof_authoritative = True
        context.active_save_proof_slot = "DLC1-AUTOSAVE1"

        dlc1_candidate = PrimarySaveSelection("DLC1-AUTOSAVE1", Path("/fake/DLC1-AUTOSAVE1/game_duration.dat"), 3000)
        dlc2_candidate = PrimarySaveSelection("DLC2-AUTOSAVE1", Path("/fake/DLC2-AUTOSAVE1/game_duration.dat"), 3100)

        def mock_candidates_fn(filename="game_duration.dat", slot_prefix=None):
            if slot_prefix == "DLC2-AUTOSAVE":
                return [dlc2_candidate]
            if slot_prefix == "DLC1-AUTOSAVE":
                return [dlc1_candidate]
            return [dlc2_candidate, dlc1_candidate]

        def mock_primary_for_slot(slot):
            return dlc2_candidate if slot == "DLC2-AUTOSAVE1" else (dlc1_candidate if slot == "DLC1-AUTOSAVE1" else None)

        evidence = GameplaySaveEvidence("gameplay", 4, "DLC2-AUTOSAVE1", "game/dlc2/e5m1_spear/e5m1_spear", native_safe=True)

        with patch.object(self.bc, "primary_save_candidates", side_effect=mock_candidates_fn), \
             patch.object(self.bc, "primary_save_for_slot", side_effect=mock_primary_for_slot), \
             patch.object(self.bc, "read_gameplay_save_evidence", return_value=evidence), \
             patch.object(self.bc, "read_game_details_for_selection", return_value={"mapName": "game/dlc2/e5m1_spear/e5m1_spear", "_mtime_ns": 3100}), \
             patch.object(context, "read_active_map_identity", return_value={"runtime_map": "game/dlc2/e5m1_spear/e5m1_spear", "map_key": "e5m1_spear"}):
            selected = context.update_save_slot_lifecycle()

        self.assertIsNotNone(selected)
        self.assertEqual(selected.slot_directory, "DLC2-AUTOSAVE1")
        self.assertEqual(context.active_save_slot, "DLC2-AUTOSAVE1")

    def test_coexisting_saves_selects_campaign_even_with_older_mtime(self):
        context = _create_test_context()
        dlc_candidate = PrimarySaveSelection("DLC1-AUTOSAVE1", Path("/fake/DLC1-AUTOSAVE1/game_duration.dat"), 9999999)
        base_candidate = PrimarySaveSelection("GAME-AUTOSAVE1", Path("/fake/GAME-AUTOSAVE1/game_duration.dat"), 100)

        def mock_candidates_fn(filename="game_duration.dat", slot_prefix=None):
            if slot_prefix == "GAME-AUTOSAVE":
                return [base_candidate]
            if slot_prefix == "DLC1-AUTOSAVE":
                return [dlc_candidate]
            return [dlc_candidate, base_candidate]

        def mock_primary_for_slot(slot):
            return base_candidate if slot == "GAME-AUTOSAVE1" else (dlc_candidate if slot == "DLC1-AUTOSAVE1" else None)

        evidence = GameplaySaveEvidence("gameplay", 5, "GAME-AUTOSAVE1", "game/sp/e1m2_battle/e1m2_battle", native_safe=True)

        with patch.object(self.bc, "primary_save_candidates", side_effect=mock_candidates_fn), \
             patch.object(self.bc, "primary_save_for_slot", side_effect=mock_primary_for_slot), \
             patch.object(self.bc, "read_gameplay_save_evidence", return_value=evidence), \
             patch.object(self.bc, "read_game_details_for_selection", return_value={"mapName": "game/sp/e1m2_battle/e1m2_battle", "_mtime_ns": 100}), \
             patch.object(context, "read_active_map_identity", return_value={"runtime_map": "game/sp/e1m2_battle/e1m2_battle", "map_key": "e1m2_war"}):
            selected = context.update_save_slot_lifecycle()

        self.assertIsNotNone(selected)
        self.assertEqual(selected.slot_directory, "GAME-AUTOSAVE1")

    def test_mission_challenge_observation_after_dlc_return_to_base(self):
        context = _create_test_context()
        context.active_save_slot = "DLC1-AUTOSAVE1"
        context.active_save_path = "/fake/DLC1-AUTOSAVE1/game_duration.dat"
        context.active_save_proof_authoritative = True
        context.active_save_proof_slot = "DLC1-AUTOSAVE1"

        base_candidate = PrimarySaveSelection("GAME-AUTOSAVE1", Path("/fake/GAME-AUTOSAVE1/game_duration.dat"), 5000)

        def mock_candidates_fn(filename="game_duration.dat", slot_prefix=None):
            return [base_candidate]

        evidence = GameplaySaveEvidence("gameplay", 6, "GAME-AUTOSAVE1", "game/sp/e3m2_hell/e3m2_hell", native_safe=True)

        with patch.object(self.bc, "primary_save_candidates", side_effect=mock_candidates_fn), \
             patch.object(self.bc, "primary_save_for_slot", return_value=base_candidate), \
             patch.object(self.bc, "read_gameplay_save_evidence", return_value=evidence), \
             patch.object(self.bc, "read_game_details_for_selection", return_value={"mapName": "game/sp/e3m2_hell/e3m2_hell", "_mtime_ns": 5000}), \
             patch.object(context, "read_active_map_identity", return_value={"runtime_map": "game/sp/e3m2_hell/e3m2_hell", "map_key": "e3m2_hell"}):
            selected = context.update_save_slot_lifecycle()

        self.assertEqual(selected.slot_directory, "GAME-AUTOSAVE1")
        self.assertEqual(context.selected_observation_slot, "GAME-AUTOSAVE1")

        observed_slots = []
        with patch.object(context, "observe_save_edges", side_effect=lambda key, recs, entries, slot: observed_slots.append(slot) or set()):
            context.observe_mission_challenges({}, base_candidate)

        self.assertIn("GAME-AUTOSAVE1", observed_slots)

    def test_deathlink_outbound_observes_dlc_save(self):
        context = _create_test_context()
        dlc_candidate = PrimarySaveSelection("DLC1-AUTOSAVE1", Path("/fake/DLC1-AUTOSAVE1/game_duration.dat"), 7000)

        with patch.object(context, "update_save_slot_lifecycle", return_value=dlc_candidate), \
             patch.object(self.bc, "probe_game_duration", return_value={
                 "mastery_records": {},
                 "mission_challenge_records": {},
                 "checkpoint_death": True,
                 "raw_num_checkpoint_deaths": 1,
             }), \
             patch.object(context, "read_active_map_identity", return_value={"runtime_map": "game/dlc/e4m1_rig/e4m1_rig", "map_key": "e4m1_rig"}), \
             patch.object(context, "observe_weapon_masteries"), \
             patch.object(context, "observe_mission_challenges"):
            result = asyncio.run(context.check_game_duration_death())

        self.assertTrue(result)
        self.assertIn("DLC1-AUTOSAVE1", context.checkpoint_death_by_save_slot)
        self.assertTrue(context.checkpoint_death_by_save_slot["DLC1-AUTOSAVE1"])

    def test_nekravol_part1_to_part2_transition_does_not_inherit_fast_travel(self):
        context = _create_test_context()
        context.checked_locations = {7770362}

        context.cached_map_identity = {
            "map_key": "e3m2_hell",
            "runtime_map": "game/sp/e3m2_hell/e3m2_hell",
            "gameplay_epoch": "1:100",
            "mtime_ns": 100,
            "materialization_evidence_epoch": 1,
        }
        context.current_map_name = "game/sp/e3m2_hell/e3m2_hell"
        snapshot_p1 = context.snapshot_fast_travel_eligibility(context.cached_map_identity)
        self.assertIsNotNone(snapshot_p1)
        self.assertEqual(snapshot_p1[1], "e3m2_hell")

        evidence = GameplaySaveEvidence(
            "gameplay", 2, "GAME-AUTOSAVE1", "game/sp/e3m2_hell_b/e3m2_hell_b", native_safe=True
        )
        transitioned = context.advance_known_map_materialization(evidence)
        self.assertTrue(transitioned)
        self.assertEqual(context.cached_map_identity["map_key"], "e3m2_hell_b")
        self.assertEqual(context.current_map_name, "game/sp/e3m2_hell_b/e3m2_hell_b")

        snapshot_p2 = context.snapshot_fast_travel_eligibility()
        self.assertIsNone(snapshot_p2)
        state = context.fast_travel_epoch_state
        self.assertEqual(state["map_key"], "e3m2_hell_b")
        self.assertFalse(state["completed_before_epoch"])
        self.assertEqual(state["ineligible_reason"], "not_completed_before_epoch")

    def test_nekravol_part2_reload_preserves_map_epoch_and_cleanup(self):
        context = _create_test_context()
        context.cached_map_identity = {
            "map_key": "e3m2_hell_b",
            "runtime_map": "game/sp/e3m2_hell_b/e3m2_hell_b",
            "gameplay_epoch": "2:200",
            "mtime_ns": 200,
            "materialization_evidence_epoch": 2,
        }
        context.current_map_name = "game/sp/e3m2_hell_b/e3m2_hell_b"
        context.automap_cleanup_epoch = "2:200"

        evidence = GameplaySaveEvidence(
            "gameplay", 3, "GAME-AUTOSAVE1", "game/sp/e3m2_hell_b/e3m2_hell_b", native_safe=True
        )
        with patch.object(self.bc, "gameplay_evidence_mtime_ns", return_value=300):
            reloaded = context.advance_known_map_materialization(evidence)

        self.assertTrue(reloaded)
        self.assertEqual(context.cached_map_identity["map_key"], "e3m2_hell_b")
        self.assertEqual(context.cached_map_identity["gameplay_epoch"], "3:300")
        self.assertEqual(context.automap_cleanup_epoch, "3:300")

        context.checked_locations = {7770363}
        sent = []
        with patch.object(self.bc, "send_command", side_effect=lambda cmd, **kwargs: sent.append((cmd, kwargs)) or True), \
             patch.object(self.bc, "rpc_execution_enabled", return_value=True):
            context.reconcile_checked_automap_cleanup("test_reload")

        self.assertEqual(context.automap_cleanup_epoch, "3:300")
        self.assertTrue(any("ap_hide_location_visual_7770363" in cmd for cmd, _ in sent))
        self.assertIn(("3:300", "room:test:1", "game/sp/e3m2_hell_b/e3m2_hell_b", "7770363"), context.automap_cleanup_submitted)

    def test_stale_dlc_evidence_slot_discarded_in_base_campaign(self):
        context = _create_test_context()
        context.active_save_slot = "DLC1-AUTOSAVE1"
        context.active_save_proof_authoritative = True

        base_candidate = PrimarySaveSelection("GAME-AUTOSAVE1", Path("/fake/GAME-AUTOSAVE1/game_duration.dat"), 1000)
        dlc_candidate = PrimarySaveSelection("DLC1-AUTOSAVE1", Path("/fake/DLC1-AUTOSAVE1/game_duration.dat"), 2000)

        def mock_candidates_fn(filename="game_duration.dat", slot_prefix=None):
            if slot_prefix == "GAME-AUTOSAVE":
                return [base_candidate]
            if slot_prefix == "DLC1-AUTOSAVE":
                return [dlc_candidate]
            return [dlc_candidate, base_candidate]

        def mock_primary_for_slot(slot):
            return base_candidate if slot == "GAME-AUTOSAVE1" else (dlc_candidate if slot == "DLC1-AUTOSAVE1" else None)

        # Stale evidence still reporting DLC1-AUTOSAVE1 despite active map being Base campaign
        evidence = GameplaySaveEvidence("gameplay", 5, "DLC1-AUTOSAVE1", "game/sp/e1m2_battle/e1m2_battle", native_safe=True)

        with patch.object(self.bc, "primary_save_candidates", side_effect=mock_candidates_fn), \
             patch.object(self.bc, "primary_save_for_slot", side_effect=mock_primary_for_slot), \
             patch.object(self.bc, "read_gameplay_save_evidence", return_value=evidence), \
             patch.object(self.bc, "read_game_details_for_selection", return_value={"mapName": "game/sp/e1m2_battle/e1m2_battle", "_mtime_ns": 1000}), \
             patch.object(context, "read_active_map_identity", return_value={"runtime_map": "game/sp/e1m2_battle/e1m2_battle", "map_key": "e1m2_war"}):
            selected = context.update_save_slot_lifecycle()

        self.assertIsNotNone(selected)
        self.assertEqual(selected.slot_directory, "GAME-AUTOSAVE1")
        self.assertEqual(context.active_save_slot, "GAME-AUTOSAVE1")

    def test_resync_eligibility_with_newly_authoritative_save(self):
        context = _create_test_context()
        context.server = SimpleNamespace(socket=SimpleNamespace(closed=False))
        context._queue_session_authoritative = True
        context.active_save_slot = "GAME-AUTOSAVE1"
        context.active_native_evidence_epoch = 1
        context.published_materialization_lease = "1:100"
        context.cached_map_identity = {
            "map_key": "e1m1_intro",
            "runtime_map": "game/sp/e1m1_intro/e1m1_intro",
            "gameplay_epoch": "1:100",
            "mtime_ns": 100,
        }
        evidence = GameplaySaveEvidence("gameplay", 1, "GAME-AUTOSAVE1", "game/sp/e1m1_intro/e1m1_intro", native_safe=True)

        with patch.object(self.bc, "read_gameplay_save_evidence", return_value=evidence), \
             patch.object(self.bc, "discover_active_map_markers", return_value=[]):
            ev, error = context._reconciliation_eligibility(require_connection=True)

        self.assertIsNone(error)
        self.assertEqual(ev.slot_directory, "GAME-AUTOSAVE1")


class TestChainsawHistoricalOwnership(unittest.TestCase):
    def test_case_a_randomize_chainsaw_false_with_hoe_check_materializes_chainsaw_in_dlc(self):
        ctx = _create_materialization_context(campaign="TAG1", map_key="e4m1_rig", runtime_map="game/dlc/e4m1_rig/e4m1_rig")
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
        ctx = _create_materialization_context(campaign="TAG1", map_key="e4m1_rig", runtime_map="game/dlc/e4m1_rig/e4m1_rig")
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
        ctx = _create_materialization_context(campaign="TAG1", map_key="e4m1_rig", runtime_map="game/dlc/e4m1_rig/e4m1_rig")
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
        ctx = _create_materialization_context(campaign="TAG1", map_key="e4m1_rig", runtime_map="game/dlc/e4m1_rig/e4m1_rig")
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


class TestSentinelHammerTAG2Upgrades(unittest.TestCase):
    def test_legitimate_hammer_in_tag2_includes_upgrade_perks(self):
        ctx = _create_materialization_context(campaign="TAG2", map_key="e5m1_spear", runtime_map="game/dlc2/e5m1_spear/e5m1_spear")
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
        ctx = _create_materialization_context(campaign="TAG2", map_key="e5m1_spear", runtime_map="game/dlc2/e5m1_spear/e5m1_spear")
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
        ctx = _create_materialization_context(campaign="TAG1", map_key="e4m1_rig", runtime_map="game/dlc/e4m1_rig/e4m1_rig")
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
        ctx = _create_materialization_context(campaign="Base", map_key="e1m3_cult", runtime_map="game/sp/e1m3_cult/e1m3_cult")
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
        ctx = _create_materialization_context(campaign="Base", map_key="e1m3_cult", runtime_map="game/sp/e1m3_cult/e1m3_cult")
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
        ctx = _create_materialization_context(campaign="Base", map_key="e1m3_cult", runtime_map="game/sp/e1m3_cult/e1m3_cult")
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
        ctx = _create_materialization_context(campaign="Base", map_key="e1m3_cult", runtime_map="game/sp/e1m3_cult/e1m3_cult")
        ctx.items_received = []
        ctx.items_processed = 0
        evidence = SimpleNamespace(epoch=1, state="gameplay", native_safe=True)

        sent_commands = []
        with patch("doom_eap.runtime.bridge_client.send_command", side_effect=lambda cmd, **kw: sent_commands.append(cmd) or True):
            plan, error = ctx._context_materialize_inventory(evidence, trigger="context")

        command_texts = [cmd.command for cmd in plan.commands] if plan else []
        self.assertFalse(any("slayer_key" in c or "7770151" in c for c in command_texts))

    def test_generic_gate_key_audit_exultia_key(self):
        ctx = _create_materialization_context(campaign="Base", map_key="e1m2_war", runtime_map="game/sp/e1m2_battle/e1m2_battle")
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
        ctx = _create_materialization_context(campaign="TAG2", map_key="e5m3_hell", runtime_map="game/dlc2/e5m3_hell/e5m3_hell")
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
        ctx = _create_materialization_context(campaign="Dark Lord", map_key="e5m4_boss", runtime_map="game/dlc2/e5m4_boss/e5m4_boss")
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
        ctx_swamp = _create_materialization_context(campaign="TAG1", map_key="e4m2_swamp", runtime_map="game/dlc/e4m2_swamp/e4m2_swamp")
        ctx_swamp.checked_locations = set()
        snapshot_swamp = ctx_swamp.snapshot_fast_travel_eligibility(refresh=True)
        self.assertIsNone(snapshot_swamp)
        self.assertFalse(ctx_swamp.fast_travel_epoch_state.get("completed_before_epoch"))
        self.assertEqual(ctx_swamp.fast_travel_epoch_state.get("ineligible_reason"), "not_completed_before_epoch")

        ctx_swamp.checked_locations = {7770443}
        snapshot_swamp_replay = ctx_swamp.snapshot_fast_travel_eligibility(refresh=True)
        self.assertIsNotNone(snapshot_swamp_replay)
        self.assertTrue(ctx_swamp.fast_travel_epoch_state.get("completed_before_epoch"))
        self.assertEqual(snapshot_swamp_replay[1], "e4m2_swamp")

    def test_the_holt_first_visit_no_fast_travel_vs_replay_eligible(self):
        ctx_holt = _create_materialization_context(campaign="TAG1", map_key="e4m3_mcity", runtime_map="game/dlc/e4m3_mcity/e4m3_mcity")
        ctx_holt.checked_locations = set()
        snapshot_holt = ctx_holt.snapshot_fast_travel_eligibility(refresh=True)
        self.assertIsNone(snapshot_holt)
        self.assertFalse(ctx_holt.fast_travel_epoch_state.get("completed_before_epoch"))
        self.assertEqual(ctx_holt.fast_travel_epoch_state.get("ineligible_reason"), "not_completed_before_epoch")

        ctx_holt.checked_locations = {7770457}
        snapshot_holt_replay = ctx_holt.snapshot_fast_travel_eligibility(refresh=True)
        self.assertIsNotNone(snapshot_holt_replay)
        self.assertTrue(ctx_holt.fast_travel_epoch_state.get("completed_before_epoch"))
        self.assertEqual(snapshot_holt_replay[1], "e4m3_mcity")


if __name__ == "__main__":
    unittest.main()
