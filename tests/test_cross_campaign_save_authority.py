from unittest.mock import Mock
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
from doom_eap.contracts.save_observation import SaveReadinessSnapshot
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
    context.locations_checked = set()
    context.server_locations = set()
    context.server = None
    context.exit_event = asyncio.Event()
    context.physical_checks = bc.PhysicalChecks(bc.logger)
    context.level_ready = bc.LevelReady(bc.logger)
    context.session_tasks = bc.SessionTasks()
    context.location_setup = bc.LocationSetup(bc.logger)
    context.protocol_feed = bc.ProtocolFeed()
    context.receipt_delivery = bc.ReceiptDelivery(bc.logger)
    context.save_checks = bc.SaveChecks(bc.WEAPON_MASTERY_BY_UNLOCKABLE, bc.MISSION_CHALLENGE_BY_UNLOCKABLE, bc.MISSION_CHALLENGE_RUNTIME_MAP_BY_UNLOCKABLE, bc.ALL_MISSION_CHALLENGES_ENTRIES, bc.logger)
    context.goals = bc.GoalProgress(bc.CAMPAIGN_GOAL_CONTRACT["runtime_map"], bc.CULTIST_BASE_MAP, bc.logger)
    context.publisher_dispatch = bc.PublisherDispatch(bc.PUBLISHERS, context.goals, bc.logger)
    context.deathlink = bc.DeathLinkSession(bc.DeathLinkReceiver(), bc.DEATHLINK_MESSAGES, lambda *a, **kw: bc.emit_launcher_event(*a, **kw), bc.logger)
    context.ammo = bc.AmmoRefill(
        bc.AmmoCommandPublication(bc.send_command, bc.discard_queued_coalesced_command,
                                      bc.rpc_execution_enabled, bc.set_rpc_execution, bc.logger),
        bc.AmmoStorage(context.send_msgs),
        lambda *args, **kwargs: bc.emit_launcher_event(*args, **kwargs), bc.logger,
    )
    context.deathlink.configure(True)
    context.ammo_requests = bc.AmmoRequestPump(context.ammo, context.request_ammo_refill, context.exit_event, bc.logger)
    context.base_directory = bc.DOOM_BASE_DIR
    context.state_key = "room:test:1"
    context.runtime_lifecycle = bc.RuntimeLifecycle()
    context.team = 0
    context.slot = 1
    context.room_seed_name = "test_seed"
    context.session_state = {}
    context.receipt_delivery.bind(context.session_state)
    context.materialization = bc.MaterializationCoordinator(bc.logger)
    context.runes = bc.RuneReconciliation(bc.logger)
    context.runes.bind(context.session_state.setdefault("rune_reconciliation", {}), context.session_state.setdefault("perk_reconciliation", {"epoch": 0, "delivered": {}}))
    context.bootstrap = bc.Bootstrap(bc.logger)
    context.bootstrap.bind(context.session_state.setdefault("bootstrap", {"actions": {}}))
    context.materialization.bind(context.session_state.setdefault("context_materialization", {}))
    context.client_state = {"version": 1, "sessions": {}}
    context.items_received = []
    context.receipt_session = bc.ReceiptSession()
    context.item_state_ready = True
    context.death_observer = bc.DeathObservation(bc.logger)
    context.save_observer = bc.SaveObserver()
    context.last_observer_lease_block = None
    context.last_save_proof_rejection = None
    context.runtime_lifecycle.observe_marker_timestamp(0, None)
    context.last_marker_reject_reason = None
    context.runtime_observation_lease = None
    context.transient_effect_manager = SimpleNamespace(reset=lambda *args: None)
    context.fast_travel = _active_bridge_client().FastTravel(_active_bridge_client().KNOWN_CATALOG_MAPS, _active_bridge_client().FAST_TRAVEL_MAP_KEYS, _active_bridge_client().FAST_TRAVEL_MISSION_COMPLETE_IDS, _active_bridge_client().logger)
    context.checked_visuals = _active_bridge_client().CheckedVisuals(_active_bridge_client().KNOWN_CATALOG_MAPS, _active_bridge_client().AUTOMAP_VISUALS_BY_MAP, "test_sess", _active_bridge_client().logger)
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


def test_context_selection_preserves_marker_precedence_and_suspension():
    context = _create_test_context()
    rejected = []
    resets = []
    context._record_context_evidence_rejection = lambda *args: rejected.append(args)
    context.transient_effect_manager = SimpleNamespace(reset=lambda reason, *_: resets.append(reason))
    context.runtime_lifecycle.accept_marker({"runtime_map": "game/dlc/e4m1_rig/e4m1_rig"}, None)
    evidence = SimpleNamespace(map_name="game/dlc2/e5m1_spear/e5m1_spear")

    selected = context._refresh_runtime_context({}, evidence)
    assert selected.campaign == "TAG1"
    assert context.pending_context_transition == ("Unknown", "TAG1")
    assert rejected[0][0] == "accepted_marker_authority"
    assert resets == ["context_transition"]

    context.runtime_lifecycle.suspend_marker(None, "fixture")
    selected = context._refresh_runtime_context({}, evidence)
    assert selected.campaign == "TAG2"
    assert context.pending_context_transition == ("TAG1", "TAG2")
    assert resets == ["context_transition", "context_transition"]

    retained = context._refresh_runtime_context({}, SimpleNamespace(map_name="unknown"))
    assert retained is selected
    assert context.pending_context_transition == ("TAG1", "TAG2")
    assert len(resets) == 2


def test_context_snapshot_is_immutable_and_preserves_transition_log_lifetime():
    from dataclasses import FrozenInstanceError
    import pytest

    bc = _active_bridge_client()
    lifecycle = bc.RuntimeLifecycle()
    original = lifecycle.snapshot
    context = bc.classify_runtime_context("game/dlc/e4m1_rig/e4m1_rig")
    transition = lifecycle.bind_context(context)
    assert original.identity == "unknown"
    assert lifecycle.snapshot.identity == context.identity
    assert lifecycle.snapshot.pending_transition is None
    assert lifecycle.record_transition(transition, context.identity)
    assert not lifecycle.record_transition(transition, context.identity)
    with pytest.raises(FrozenInstanceError):
        lifecycle.snapshot.campaign = "Base"

    lifecycle.complete_transition()
    assert lifecycle.snapshot.pending_transition is None
    lifecycle.clear_context()
    transition = lifecycle.bind_context(context)
    assert not lifecycle.record_transition(transition, context.identity)
    lifecycle.clear_context(clear_log=True)
    transition = lifecycle.bind_context(context)
    assert lifecycle.record_transition(transition, context.identity)


def test_lifecycle_marker_snapshots_do_not_alias_inputs_or_advance_epochs():
    import pytest

    lifecycle = _active_bridge_client().RuntimeLifecycle()
    marker = {"runtime_map": "game/sp/e3m2_hell/e3m2_hell", "gameplay_epoch": "1:100"}
    lifecycle.stage_marker(marker)
    pending = lifecycle.map_identity
    assert pending.cached_marker is None
    assert pending.pending_marker["evidence_epoch"] is None
    accepted = lifecycle.accept_marker(marker, 2)
    marker["gameplay_epoch"] = "changed"
    assert accepted["gameplay_epoch"] == "1:100"
    with pytest.raises(TypeError):
        accepted["gameplay_epoch"] = "changed"
    lifecycle.bind_materialization_evidence(3)
    assert accepted["evidence_epoch"] == 2
    assert lifecycle.map_identity.cached_marker["evidence_epoch"] == 3
    assert lifecycle.map_identity.cached_marker["gameplay_epoch"] == "1:100"
    lifecycle.suspend_marker(4, "provisional")
    assert lifecycle.map_identity.current_map is None
    assert lifecycle.map_identity.cached_marker["materialization_suspended"]
    assert lifecycle.map_identity.pending_marker is None
    assert pending.pending_marker is not None
    lifecycle.clear_map()
    assert lifecycle.map_identity.cached_marker is None


def test_native_load_policy_separates_admission_timestamp_and_acceptance():
    lifecycle = _active_bridge_client().RuntimeLifecycle()
    hub = "game/hub/hub"
    tag = "game/dlc/e4m1_rig/e4m1_rig"
    catalog = {"hub": hub, "rig": tag}

    def evidence(epoch=1, map_name=hub, state="gameplay", provisional=False):
        return SimpleNamespace(epoch=epoch, map_name=map_name, state=state, provisional=provisional)

    for rejected in (None, evidence(state="menu"), evidence(epoch=True),
                     evidence(map_name="unknown"), evidence(provisional=True)):
        assert lifecycle.classify_native_load(rejected, catalog) is None
    initial = lifecycle.classify_native_load(evidence(map_name="game/sp/hub/hub"), catalog)
    assert initial.action == "initialize"
    for timestamp in (None, True, -1):
        assert lifecycle.native_marker_proposal(initial, timestamp) is None
    marker = lifecycle.native_marker_proposal(initial, 100)
    assert marker["gameplay_epoch"] == "1:100"
    assert lifecycle.map_identity.cached_marker is None
    lifecycle.accept_marker(marker, 1)
    assert lifecycle.classify_native_load(evidence(), catalog) is None
    assert lifecycle.classify_native_load(evidence(epoch=0), catalog) is None
    assert lifecycle.classify_native_load(evidence(epoch=2, provisional=True), catalog).action == "suspend"
    reload = lifecycle.classify_native_load(evidence(epoch=2), catalog)
    assert reload.action == "reload"
    proposal = lifecycle.native_marker_proposal(reload, 200)
    assert proposal["secondary_materialization"] is True
    assert proposal["mtime_ns"] == 100
    assert proposal["evidence_mtime_ns"] == 200
    assert lifecycle.map_identity.cached_marker["gameplay_epoch"] == "1:100"
    assert lifecycle.classify_native_load(evidence(epoch=2, map_name=tag, provisional=True), catalog) is None
    assert lifecycle.classify_native_load(evidence(epoch=2, map_name=tag), catalog).action == "transition"


def test_authored_timestamp_decisions_preserve_process_and_marker_bounds():
    lifecycle = _active_bridge_client().RuntimeLifecycle()
    lifecycle.observe_marker_timestamp(200, 1)
    lifecycle.stage_marker({"runtime_map": "game/hub/hub", "mtime_ns": 250})
    assert lifecycle.authored_timestamp_action(251, 300) == "ignore"
    assert lifecycle.authored_timestamp_action(250, 100) == "native_fallback"
    assert lifecycle.authored_timestamp_action(251, 100) == "parse"
    lifecycle.clear_pending_marker()
    assert lifecycle.authored_timestamp_action(201, None) == "parse"
    lifecycle.accept_marker({"runtime_map": "game/hub/hub", "mtime_ns": 400}, 2)
    assert lifecycle.authored_timestamp_action(400, 100) == "native_fallback"
    proposal = lifecycle.authored_marker_proposal({"runtime_map": "game/hub/hub"}, 500, None, True)
    assert proposal["gameplay_epoch"] == "500:500"
    assert proposal["evidence_epoch"] is True
    assert proposal["materialization_evidence_epoch"] is None
    assert lifecycle.map_identity.cached_marker["mtime_ns"] == 400


def test_first_same_map_evidence_binds_without_advancing_gameplay_epoch():
    context = _create_test_context()
    runtime_map = "game/sp/e3m2_hell/e3m2_hell"
    context.runtime_lifecycle.accept_marker({
        "runtime_map": runtime_map, "map_key": "e3m2_hell",
        "gameplay_epoch": "1:100", "mtime_ns": 100,
    }, None)
    evidence = GameplaySaveEvidence("gameplay", 2, "GAME-AUTOSAVE1", runtime_map, native_safe=True)
    assert not context.advance_known_map_materialization(evidence)
    assert context.cached_map_identity["materialization_evidence_epoch"] == 2
    assert context.cached_map_identity["evidence_epoch"] == 2
    assert context.cached_map_identity["gameplay_epoch"] == "1:100"
    stale = GameplaySaveEvidence("gameplay", 1, "GAME-AUTOSAVE1", runtime_map, native_safe=True)
    assert not context.advance_known_map_materialization(stale)
    assert context.cached_map_identity["materialization_evidence_epoch"] == 2


def test_marker_acceptance_and_lease_publication_are_distinct():
    context = _create_test_context()
    marker = {
        "runtime_map": "game/sp/e3m2_hell/e3m2_hell", "map_key": "e3m2_hell",
        "gameplay_epoch": "2:100", "mtime_ns": 100, "path": None,
    }
    with patch.object(_active_bridge_client(), "publish_materialization_lease", return_value=False):
        accepted = context.accept_map_identity(marker, 2)
    assert context.cached_map_identity == accepted
    assert context.current_map_name == marker["runtime_map"]
    assert context.published_materialization_lease is None
    assert context.pending_level_ready == {"2:100": None}
    assert context.last_accepted_map_evidence_epoch == 2
    with patch.object(_active_bridge_client(), "publish_materialization_lease", return_value=True):
        context.accept_map_identity(marker, 3)
    assert context.published_materialization_lease == "2:100"
    assert context.cached_map_identity["evidence_epoch"] == 3
    assert context.last_accepted_map_evidence_epoch == 2


def test_provisional_family_switch_requires_native_safety_and_fresher_candidate(monkeypatch):
    bc = _active_bridge_client()
    target_map = "game/dlc/e4m1_rig/e4m1_rig"
    for native_safe, candidate_mtime, accepted in ((True, 200, True), (False, 200, False), (True, 100, False)):
        ctx = _create_test_context()
        old = SimpleNamespace(slot_directory="GAME-AUTOSAVE1", path=Path("old.dat"), mtime_ns=100)
        selected = SimpleNamespace(slot_directory="DLC1-AUTOSAVE1", path=Path("new.dat"), mtime_ns=candidate_mtime)
        ctx.save_observer = bc.SaveObserver(SaveReadinessSnapshot(evidence_epoch=1))
        ctx.save_observer.update_selection(slot=old.slot_directory, path=str(old.path))
        ctx.read_active_map_identity = lambda **kwargs: None
        evidence = SimpleNamespace(state="gameplay", epoch=2, map_name=target_map,
                                   slot_directory=selected.slot_directory, provisional=True, native_safe=native_safe)
        events = []
        ctx.log_save_proof_accepted = lambda *args, **kwargs: events.append("proof")

        def activate(selection):
            events.append("activate")
            ctx.save_observer.update_selection(slot=selection.slot_directory, path=str(selection.path))

        ctx.activate_save_selection = activate
        monkeypatch.setattr(bc, "read_gameplay_save_evidence", lambda: evidence)
        monkeypatch.setattr(bc, "primary_save_candidates", lambda **kwargs: [selected])
        monkeypatch.setattr(bc, "primary_save_for_slot", lambda slot: old if slot == old.slot_directory else selected)
        monkeypatch.setattr(bc, "read_game_details_for_selection", lambda selection: {"mapName": target_map})
        monkeypatch.setattr(bc, "gameplay_evidence_mtime_ns", lambda: 210)
        monkeypatch.setattr(bc, "publish_materialization_lease", lambda epoch: events.append("publish") or True)

        assert ctx.runtime_lifecycle.classify_native_load(evidence, bc.KNOWN_CATALOG_MAPS) is None
        result = ctx.update_save_slot_lifecycle()
        if accepted:
            assert result is selected
            assert events == ["proof", "activate", "publish"]
            assert ctx.cached_map_identity["runtime_map"] == target_map
            assert ctx.cached_map_identity["gameplay_epoch"] == "2:210"
            assert ctx.active_save_proof_authoritative
        else:
            assert result is None
            assert ctx.cached_map_identity is None
            assert events == []


def test_goal_file_batch_stops_after_rebind_and_preserves_unconsumed_file(tmp_path, monkeypatch):
    from doom_eap.contracts.publisher_contracts import PublisherContract, PublisherEngine

    bc = _active_bridge_client()
    context = _create_test_context()
    contracts = tuple(
        PublisherContract(key, "map", ({"strategy": "map_event_file", "filename": "event.txt", "marker": "MARK"},),
                          ({"strategy": "location_check", "location_id": index},), "room", "first_success_wins")
        for index, key in enumerate(("first", "second"), 1)
    )
    monkeypatch.setattr(bc, "PUBLISHER_ENGINE", PublisherEngine(contracts))
    monkeypatch.setattr(bc, "INV_DUMP_DIR", str(tmp_path))
    monkeypatch.setattr(bc, "goal_event_files", lambda: [])
    path = tmp_path / "event.txt"
    path.write_text("MARK")
    sent = []

    async def publish(ctx, publisher, *_):
        sent.append(publisher.key)
        ctx.receipt_session.begin_rebind()
        return True

    monkeypatch.setattr(bc.DoomEternalContext, "execute_publisher", publish)
    assert asyncio.run(context.check_campaign_goal_event())
    assert sent == ["first"] and path.read_text() == "MARK"


def _create_materialization_context(campaign="TAG1", map_key="e4m1_rig", runtime_map="game/dlc/e4m1_rig/e4m1_rig"):
    bc = _active_bridge_client()
    if 7770010 not in bc.ITEM_ID_TO_COMMAND:
        with open(REPO_ROOT / "data" / "items.json", encoding="utf-8") as f:
            bc.ITEM_ID_TO_COMMAND = {
                int(k): v for k, v in json.load(f).items()
            }
    context = object.__new__(bc.DoomEternalContext)
    context.locations_checked = set()
    context.server_locations = set()
    context.server = None
    context.exit_event = asyncio.Event()
    context.physical_checks = bc.PhysicalChecks(bc.logger)
    context.level_ready = bc.LevelReady(bc.logger)
    context.session_tasks = bc.SessionTasks()
    context.location_setup = bc.LocationSetup(bc.logger)
    context.protocol_feed = bc.ProtocolFeed()
    context.receipt_delivery = bc.ReceiptDelivery(bc.logger)
    context.save_checks = bc.SaveChecks(bc.WEAPON_MASTERY_BY_UNLOCKABLE, bc.MISSION_CHALLENGE_BY_UNLOCKABLE, bc.MISSION_CHALLENGE_RUNTIME_MAP_BY_UNLOCKABLE, bc.ALL_MISSION_CHALLENGES_ENTRIES, bc.logger)
    context.goals = bc.GoalProgress(bc.CAMPAIGN_GOAL_CONTRACT["runtime_map"], bc.CULTIST_BASE_MAP, bc.logger)
    context.publisher_dispatch = bc.PublisherDispatch(bc.PUBLISHERS, context.goals, bc.logger)
    context.deathlink = bc.DeathLinkSession(bc.DeathLinkReceiver(), bc.DEATHLINK_MESSAGES, lambda *a, **kw: bc.emit_launcher_event(*a, **kw), bc.logger)
    context.ammo = bc.AmmoRefill(
        bc.AmmoCommandPublication(bc.send_command, bc.discard_queued_coalesced_command,
                                      bc.rpc_execution_enabled, bc.set_rpc_execution, bc.logger),
        bc.AmmoStorage(context.send_msgs),
        lambda *args, **kwargs: bc.emit_launcher_event(*args, **kwargs), bc.logger,
    )
    context.deathlink.configure(True)
    context.ammo_requests = bc.AmmoRequestPump(context.ammo, context.request_ammo_refill, context.exit_event, bc.logger)
    context.base_directory = bc.DOOM_BASE_DIR
    context.state_key = "room:test:1"
    context.team = 0
    context.slot = 1
    context.room_seed_name = "test_seed"
    context.session_state = {}
    context.receipt_delivery.bind(context.session_state)
    context.client_state = {"version": 1, "sessions": {}}
    context.items_received = []
    context.receipt_session = bc.ReceiptSession()
    context.item_state_ready = True
    context.server_checked_locations_ready = True
    context.checked_locations = set()
    context.locations_checked = set()
    context.materialization = bc.MaterializationCoordinator(bc.logger)
    context.runes = bc.RuneReconciliation(bc.logger)
    context.runes.bind(context.session_state.setdefault("rune_reconciliation", {}), context.session_state.setdefault("perk_reconciliation", {"epoch": 0, "delivered": {}}))
    context.bootstrap = bc.Bootstrap(bc.logger)
    context.bootstrap.bind(context.session_state.setdefault("bootstrap", {"actions": {}}))
    context.materialization.bind(context.session_state.setdefault("context_materialization", {}))
    from doom_eap.contracts.runtime_context import RuntimeContextSnapshot
    context.runtime_lifecycle = bc.RuntimeLifecycle(RuntimeContextSnapshot(campaign=campaign))
    context.runtime_lifecycle.record_lease_publication("1:1", True)
    context.runtime_lifecycle.accept_marker({
        "gameplay_epoch": "1:1",
        "runtime_map": runtime_map,
        "map_key": map_key,
    }, None)
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
    context.death_observer = bc.DeathObservation(bc.logger)
    context.save_observer = bc.SaveObserver()
    context.save_observer.update_selection(slot="DLC1-AUTOSAVE1")
    context.fast_travel = _active_bridge_client().FastTravel(_active_bridge_client().KNOWN_CATALOG_MAPS, _active_bridge_client().FAST_TRAVEL_MAP_KEYS, _active_bridge_client().FAST_TRAVEL_MISSION_COMPLETE_IDS, _active_bridge_client().logger)
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
        context.save_observer.update_selection(slot="GAME-AUTOSAVE1", path="/fake/GAME-AUTOSAVE1/game_duration.dat")
        context.save_observer.activate_slot("GAME-AUTOSAVE1")

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
        context.save_observer.update_selection(slot="DLC1-AUTOSAVE1", path="/fake/DLC1-AUTOSAVE1/game_duration.dat")
        context.save_observer.activate_slot("DLC1-AUTOSAVE1")
        context.save_observer.select_observation_slot("DLC1-AUTOSAVE1")

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
        context.save_observer.update_selection(slot="DLC1-AUTOSAVE1", path="/fake/DLC1-AUTOSAVE1/game_duration.dat")
        context.save_observer.activate_slot("DLC1-AUTOSAVE1")

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
        context.save_observer.update_selection(slot="DLC1-AUTOSAVE1", path="/fake/DLC1-AUTOSAVE1/game_duration.dat")
        context.save_observer.activate_slot("DLC1-AUTOSAVE1")

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
        with patch.object(self.bc.SaveCheckObservations, "observe_edges", side_effect=lambda key, recs, entries, slot: observed_slots.append(slot) or set()):
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
        self.assertIn("DLC1-AUTOSAVE1", context.death_observer.checkpoints)
        self.assertTrue(context.death_observer.checkpoints["DLC1-AUTOSAVE1"])

    def test_late_duration_result_cannot_observe_rebound_session_or_reloaded_save(self):
        for invalidation in ("receipt", "proof"):
            for fails in (False, True):
                context = _create_test_context()
                selected = PrimarySaveSelection("GAME-AUTOSAVE1", Path("/fake/game_duration.dat"), 7)
                context.save_observer.accept_proof(selected.slot_directory, 1, 1)

                async def decode_late(*args):
                    if invalidation == "receipt":
                        context.receipt_session.begin_rebind()
                    else:
                        context.save_observer.invalidate_proof()
                        context.save_observer.accept_proof(selected.slot_directory, 1, 1)
                    if fails:
                        raise OSError("old read failed")
                    return {"mastery_records": {}, "mission_challenge_records": {}, "checkpoint_death": True}

                with patch.object(context, "update_save_slot_lifecycle", return_value=selected), \
                     patch.object(self.bc.asyncio, "to_thread", side_effect=decode_late), \
                     patch.object(context, "observe_weapon_masteries") as mastery, \
                     patch.object(context, "observe_mission_challenges") as challenges:
                    self.assertTrue(asyncio.run(context.check_game_duration_death()))
                mastery.assert_not_called()
                challenges.assert_not_called()
                self.assertFalse(context.save_observer.duration_is_current(selected))
                self.assertEqual(context.death_observer.checkpoints, {})
                self.assertIsNone(context.death_observer.warning)

    def test_nekravol_part1_to_part2_transition_does_not_inherit_fast_travel(self):
        context = _create_test_context()
        context.checked_locations = {7770362}

        context.runtime_lifecycle.accept_marker({
            "map_key": "e3m2_hell",
            "runtime_map": "game/sp/e3m2_hell/e3m2_hell",
            "gameplay_epoch": "1:100",
            "mtime_ns": 100,
            "materialization_evidence_epoch": 1,
        }, None)
        context.runtime_lifecycle.project_current_map({"runtime_map": "game/sp/e3m2_hell/e3m2_hell"})
        snapshot_p1 = context.snapshot_fast_travel_eligibility(context.cached_map_identity)
        self.assertIsNotNone(snapshot_p1)
        self.assertEqual(snapshot_p1[1], "e3m2_hell")

        evidence = GameplaySaveEvidence(
            "gameplay", 2, "GAME-AUTOSAVE1", "game/sp/e3m2_hell_b/e3m2_hell_b", native_safe=True
        )
        with patch.object(_active_bridge_client(), "gameplay_evidence_mtime_ns", return_value=None):
            self.assertFalse(context.advance_known_map_materialization(evidence))
        self.assertEqual(context.cached_map_identity["gameplay_epoch"], "1:100")
        with patch.object(_active_bridge_client(), "gameplay_evidence_mtime_ns", return_value=200):
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

    def test_room_update_refresh_preserves_existing_late_and_current_visit_completion_behavior(self):
        # v0.5.2 refresh does not distinguish late initial history from this visit's
        # server-confirmed completion. P-1 preserves that behavior without inventing a rule.
        for initial_history in (None, set()):
            context = _create_test_context()
            context.checked_locations = initial_history
            context.runtime_lifecycle.accept_marker({
                "map_key": "e3m2_hell", "runtime_map": "game/sp/e3m2_hell/e3m2_hell",
                "gameplay_epoch": "1:100", "mtime_ns": 100,
            }, None)
            self.assertIsNone(context.snapshot_fast_travel_eligibility())
            context.checked_locations = set()
            context.reconcile_checked_automap_cleanup = lambda *args: None
            async def no_checks():
                return None
            context.check_mission_challenge_locations = no_checks
            def close_task(coroutine):
                coroutine.close()
                return Mock()
            with patch.object(self.bc.asyncio, "create_task", side_effect=close_task):
                context.on_package("RoomUpdate", {"checked_locations": [7770362]})
            self.assertTrue(context.fast_travel_epoch_state["completed_before_epoch"])
            self.assertIsNotNone(context.fast_travel_eligibility_snapshot)

    def test_nekravol_part2_reload_preserves_map_epoch_and_cleanup(self):
        context = _create_test_context()
        context.runtime_lifecycle.accept_marker({
            "map_key": "e3m2_hell_b",
            "runtime_map": "game/sp/e3m2_hell_b/e3m2_hell_b",
            "gameplay_epoch": "2:200",
            "mtime_ns": 200,
            "materialization_evidence_epoch": 2,
        }, None)
        context.runtime_lifecycle.project_current_map({"runtime_map": "game/sp/e3m2_hell_b/e3m2_hell_b"})
        context.checked_visuals.advance_epoch("2:200")

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
        self.assertEqual(context.automap_cleanup_status[("3:300", "room:test:1", "game/sp/e3m2_hell_b/e3m2_hell_b", "7770363")], "SUBMITTED")
        with patch.object(self.bc, "send_command", side_effect=lambda cmd, **kwargs: sent.append((cmd, kwargs)) or True), \
             patch.object(self.bc, "rpc_execution_enabled", return_value=True):
            count = len(sent)
            context.reconcile_checked_automap_cleanup("same_epoch")
            self.assertEqual(len(sent), count)

    def test_stale_dlc_evidence_slot_discarded_in_base_campaign(self):
        context = _create_test_context()
        context.save_observer = _active_bridge_client().SaveObserver(SaveReadinessSnapshot(authoritative=True))
        context.save_observer.update_selection(slot="DLC1-AUTOSAVE1")

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
        context.save_observer.update_selection(slot="GAME-AUTOSAVE1", native_evidence_epoch=1)
        context.runtime_lifecycle.record_lease_publication("1:100", True)
        context.runtime_lifecycle.accept_marker({
            "map_key": "e1m1_intro",
            "runtime_map": "game/sp/e1m1_intro/e1m1_intro",
            "gameplay_epoch": "1:100",
            "mtime_ns": 100,
        }, None)
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
        ctx.receipt_session.restore_boundary(1)
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
        ctx.runtime_lifecycle.record_lease_publication("1:1", True)
        ctx.runtime_lifecycle.accept_marker({**ctx.cached_map_identity, "gameplay_epoch": "1:1"}, None)
        ctx.items_received = [
            NetworkItem(item=7770901, location=0, player=1, flags=0),
            NetworkItem(item=7770901, location=1, player=1, flags=0),
        ]
        ctx.receipt_session.restore_boundary(2)
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
        ctx.receipt_session.restore_boundary(1)
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
        ctx.receipt_session.restore_boundary(2)
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
        ctx.receipt_session.restore_boundary(1)
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
        ctx.receipt_session.restore_boundary(1)
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
        ctx.receipt_session.restore_boundary(1)
        evidence = SimpleNamespace(epoch=1, state="gameplay", native_safe=True)

        with patch("doom_eap.runtime.bridge_client.send_command", return_value=True):
            ctx._context_materialize_inventory(evidence, trigger="context")

        ctx.runtime_lifecycle.record_lease_publication("2:1", True)
        ctx.runtime_lifecycle.accept_marker({**ctx.cached_map_identity, "gameplay_epoch": "2:1"}, None)
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
        ctx.receipt_session.restore_boundary(0)
        evidence = SimpleNamespace(epoch=1, state="gameplay", native_safe=True)

        sent_commands = []
        with patch("doom_eap.runtime.bridge_client.send_command", side_effect=lambda cmd, **kw: sent_commands.append(cmd) or True):
            plan, error = ctx._context_materialize_inventory(evidence, trigger="context")

        command_texts = [cmd.command for cmd in plan.commands] if plan else []
        self.assertFalse(any("slayer_key" in c or "7770151" in c for c in command_texts))

    def test_generic_gate_key_audit_exultia_key(self):
        ctx = _create_materialization_context(campaign="Base", map_key="e1m2_war", runtime_map="game/sp/e1m2_battle/e1m2_battle")
        ctx.items_received = [NetworkItem(item=7770150, location=0, player=1, flags=0)]
        ctx.receipt_session.restore_boundary(1)
        evidence = SimpleNamespace(epoch=1, state="gameplay", native_safe=True)

        with patch("doom_eap.runtime.bridge_client.send_command", return_value=True):
            plan, error = ctx._context_materialize_inventory(evidence, trigger="context")
        self.assertIsNotNone(plan)

        ctx.runtime_lifecycle.record_lease_publication("2:1", True)
        ctx.runtime_lifecycle.accept_marker({**ctx.cached_map_identity, "gameplay_epoch": "2:1"}, None)
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
