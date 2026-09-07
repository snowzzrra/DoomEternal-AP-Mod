import asyncio
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


if __name__ == "__main__":
    unittest.main()
