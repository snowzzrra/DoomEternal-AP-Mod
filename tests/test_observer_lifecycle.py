from __future__ import annotations

from observer_lifecycle import (
    OBSERVER_CONTRACTS,
    RuntimeObservationLease,
    SaveObserverBaselineStore,
)


def _binding(session: str = "seed") -> str:
    return SaveObserverBaselineStore.binding_key(
        session_identity=session,
        team=1,
        slot=2,
        doom_save_slot="GAME-AUTOSAVE2",
        registry_revision="revision",
    )


def test_every_save_observer_declares_the_shared_evidence_policy() -> None:
    policies = OBSERVER_CONTRACTS["policies"]
    for observer in OBSERVER_CONTRACTS["observers"].values():
        policy = policies[observer["policy"]]
        if observer["policy"] == "save_based":
            assert policy == {
                "requires_live_runtime": True,
                "requires_authoritative_save": True,
                "baseline_policy": "first_binding_snapshot",
                "binding_scope": "ap_session_team_slot_doom_save_registry",
                "completion_policy": "false_to_true_edge",
            }


def test_game_closed_blocks_live_runtime_lease() -> None:
    lease = RuntimeObservationLease(process_probe=lambda: False, started_ns=100)
    lease.observe_gameplay_loaded(110)
    assert lease.validate(
        evidence_mtime_ns=120,
        evidence_state="gameplay",
        evidence_map="map",
        current_map="map",
        details_map="map",
    ) == (False, "game_not_running")


def test_live_lease_requires_fresh_gameplay_and_coherent_map() -> None:
    lease = RuntimeObservationLease(process_probe=lambda: True, started_ns=100)
    assert lease.validate(
        evidence_mtime_ns=120,
        evidence_state="gameplay",
        evidence_map="map",
        current_map="map",
        details_map="map",
    ) == (False, "gameplay_not_loaded")
    lease.observe_gameplay_loaded(110)
    assert lease.validate(
        evidence_mtime_ns=120,
        evidence_state="gameplay",
        evidence_map="map",
        current_map="map",
        details_map="map",
    ) == (True, "live")


def test_old_save_becomes_new_session_baseline() -> None:
    state: dict = {}
    store = SaveObserverBaselineStore(state)
    pending, created, new_edges = store.observe(
        binding_key=_binding("new-seed"),
        observer_key="mission_challenges",
        records={"old_complete": True, "future": False},
        acknowledged_records=set(),
    )
    assert created
    assert pending == set()
    assert new_edges == set()
    observer = state["observer_baselines"][_binding("new-seed")]["observers"]["mission_challenges"]
    assert observer["baseline_preexisting"] == ["old_complete"]


def test_reconnect_same_session_does_not_rebaseline_and_later_edge_is_pending() -> None:
    state: dict = {}
    store = SaveObserverBaselineStore(state)
    store.observe(
        binding_key=_binding(),
        observer_key="masteries",
        records={"record": False},
        acknowledged_records=set(),
    )
    pending, created, new_edges = SaveObserverBaselineStore(state).observe(
        binding_key=_binding(),
        observer_key="masteries",
        records={"record": True},
        acknowledged_records=set(),
    )
    assert not created
    assert pending == {"record"}
    assert new_edges == {"record"}
    pending, _, new_edges = SaveObserverBaselineStore(state).observe(
        binding_key=_binding(),
        observer_key="masteries",
        records={"record": True},
        acknowledged_records=set(),
    )
    assert pending == {"record"}
    assert new_edges == set()
    pending, _, _ = SaveObserverBaselineStore(state).observe(
        binding_key=_binding(),
        observer_key="masteries",
        records={"record": True},
        acknowledged_records={"record"},
    )
    assert pending == set()
