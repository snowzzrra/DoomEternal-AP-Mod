"""Unit tests for authoritative map-start identity marker (P0.2) and challenge reward suppression container (P0.3)."""

import os
import tempfile
import time
from pathlib import Path
import pytest

from tools.maps.ap_map_generator import generate_system_command_entities
from bridge_client import (
    parse_active_map_marker,
    discover_active_map_markers,
    GameplaySaveEvidence,
)
from tools.decls.mission_challenge_decl_builder import build_mission_challenge_overrides
from tools.validation.validate_challenge_overrides import validate_overrides_from_mod_root


@pytest.mark.parametrize(
    "map_key,runtime_map",
    [
        ("e1m4_boss", "game/sp/e1m4_boss/e1m4_boss"),
        ("e3m2_hell", "game/sp/e3m2_hell/e3m2_hell"),
        ("e3m2_hell_b", "game/sp/e3m2_hell_b/e3m2_hell_b"),
        ("hub", "game/hub/hub"),
    ],
)
def test_map_start_identity_entity_generation(map_key, runtime_map):
    entities = generate_system_command_entities(
        map_key=map_key, runtime_map=runtime_map
    )
    assert "entityDef ap_rpc_auto_enable" in entities
    assert f"AP_ACTIVE_MAP_V1 map_key={map_key}" in entities
    assert f"runtime_map={runtime_map}" in entities
    assert f"marker=AP_MAP_START_{map_key.upper()}" in entities
    assert f"condump ap_active_map_{map_key}.txt" in entities
    assert "condump ap_telemetry_ready.txt" in entities

    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as f:
        f.write(entities)
        f_path = f.name
    try:
        marker = parse_active_map_marker(f_path, time.time_ns())
        assert marker is not None
        assert marker["map_key"] == map_key
        assert marker["runtime_map"] == runtime_map
    finally:
        if os.path.exists(f_path):
            os.remove(f_path)


def test_parse_active_map_marker_returns_last_complete_occurrence():
    sample_content = (
        "AP_ACTIVE_MAP_V1 map_key=hub runtime_map=game/hub/hub marker=AP_MAP_START_HUB\n"
        "AP_ACTIVE_MAP_V1 map_key=e1m4_boss runtime_map=game/sp/e1m4_boss/e1m4_boss marker=AP_MAP_START_E1M4_BOSS\n"
        "AP_ACTIVE_MAP_V1 map_key=e1m3_cult"
    )
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as f:
        f.write(sample_content)
        f_path = f.name

    try:
        now_ns = time.time_ns()
        marker = parse_active_map_marker(f_path, now_ns)
        assert marker is not None
        assert marker["map_key"] == "e1m4_boss"
        assert marker["runtime_map"] == "game/sp/e1m4_boss/e1m4_boss"
        assert marker["marker"] == "AP_MAP_START_E1M4_BOSS"
        assert marker["mtime_ns"] == now_ns
    finally:
        if os.path.exists(f_path):
            os.remove(f_path)


def test_parse_active_map_marker_validates_tuple_and_unknown_map():
    # Unknown map key
    unknown_content = "AP_ACTIVE_MAP_V1 map_key=custom_map runtime_map=game/sp/custom marker=AP_MAP_START_CUSTOM\n"
    # Mismatched runtime_map
    mismatch_runtime = "AP_ACTIVE_MAP_V1 map_key=e1m4_boss runtime_map=game/hub/hub marker=AP_MAP_START_E1M4_BOSS\n"
    # Mismatched marker
    mismatch_marker = "AP_ACTIVE_MAP_V1 map_key=e1m4_boss runtime_map=game/sp/e1m4_boss/e1m4_boss marker=AP_MAP_START_HUB\n"

    for content in (unknown_content, mismatch_runtime, mismatch_marker):
        with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as f:
            f.write(content)
            f_path = f.name
        try:
            assert parse_active_map_marker(f_path, time.time_ns()) is None
        finally:
            if os.path.exists(f_path):
                os.remove(f_path)


def test_marker_rejected_on_menu_loading_or_game_closed(monkeypatch):
    import asyncio
    monkeypatch.setattr(asyncio, "create_task", lambda *a, **kw: None)
    from bridge_client import DoomEternalContext

    ctx = DoomEternalContext("localhost:38281", "")
    assert ctx.read_active_map_identity(evidence=None) is None
    assert ctx.current_map_name is None

    menu_evidence = GameplaySaveEvidence("menu", 100, "GAME-AUTOSAVE1", "game/hub/hub")
    assert ctx.read_active_map_identity(evidence=menu_evidence) is None
    assert ctx.current_map_name is None

    from observer_lifecycle import RuntimeObservationLease
    ctx.runtime_observation_lease = RuntimeObservationLease(
        process_probe=lambda: False,
        started_ns=time.time_ns(),
    )
    ctx.current_map_name = "game/sp/e1m4_boss/e1m4_boss"
    ctx.mission_select_observation_map = "game/sp/e1m4_boss/e1m4_boss"
    ctx.mission_select_observation_epoch = 123

    gameplay_evidence = GameplaySaveEvidence("gameplay", 100, "GAME-AUTOSAVE1", "game/sp/e1m4_boss/e1m4_boss")
    assert ctx.read_active_map_identity(evidence=gameplay_evidence) is None
    assert ctx.current_map_name is None


def test_check_rpc_autopause_passes_evidence_and_handles_menu(monkeypatch):
    import asyncio
    monkeypatch.setattr(asyncio, "create_task", lambda *a, **kw: None)
    from bridge_client import DoomEternalContext, GameplaySaveEvidence

    ctx = DoomEternalContext("localhost:38281", "")

    marker_data = {
        "map_key": "e1m4_boss",
        "runtime_map": "game/sp/e1m4_boss/e1m4_boss",
        "marker": "AP_MAP_START_E1M4_BOSS",
        "mtime_ns": time.time_ns(),
        "path": "/dummy/path.txt",
    }

    monkeypatch.setattr("bridge_client.read_gameplay_save_evidence", lambda: GameplaySaveEvidence("gameplay", 100, "GAME-AUTOSAVE1", "game/hub/hub"))
    monkeypatch.setattr(ctx, "read_active_map_identity", lambda evidence=None: marker_data if (evidence and getattr(evidence, "state", None) == "gameplay") else None)

    ctx.check_rpc_autopause()
    assert ctx.current_map_name == "game/sp/e1m4_boss/e1m4_boss"

    monkeypatch.setattr("bridge_client.read_gameplay_save_evidence", lambda: GameplaySaveEvidence("menu", 100, "GAME-AUTOSAVE1", "game/hub/hub"))
    ctx.check_rpc_autopause()
    assert ctx.current_map_name is None


def test_update_save_slot_lifecycle_safe_when_evidence_is_none(monkeypatch):
    import asyncio
    monkeypatch.setattr(asyncio, "create_task", lambda *a, **kw: None)
    from bridge_client import DoomEternalContext

    ctx = DoomEternalContext("localhost:38281", "")
    ctx.active_save_slot = "GAME-AUTOSAVE1"

    res = ctx.update_save_slot_lifecycle()
    assert res is None
    assert ctx.runtime_observers_frozen is True


def test_mission_challenge_overrides_winner_isolation_and_validator():
    with tempfile.TemporaryDirectory() as tmpdir:
        mod_root = Path(tmpdir) / "mod"
        registry_path = Path(__file__).resolve().parents[2] / "data" / "challenge_location_registry.json"
        
        audit = build_mission_challenge_overrides(mod_root)
        
        assert audit["child_owner"] == "gameresources"
        assert audit["aggregate_owner"] == "gameresources_patch2"
        assert audit["challenge_count"] == 27
        assert audit["registration_experiment"]["mission_count"] == 1
        
        for p in audit["written_paths"]:
            assert "gameresources_patch3" not in p

        errors = validate_overrides_from_mod_root(mod_root, registry_path)
        assert errors == [], f"Validation failed with errors: {errors}"


def test_discover_active_map_markers_finds_telemetry_dump_markers(monkeypatch):
    import bridge_client
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(bridge_client, "INV_DUMP_DIR", tmpdir)
        telemetry_file = os.path.join(tmpdir, "ap_telemetry_ready.txt")
        marker_line = "AP_ACTIVE_MAP_V1 map_key=e1m4_boss runtime_map=game/sp/e1m4_boss/e1m4_boss marker=AP_MAP_START_E1M4_BOSS\n"
        with open(telemetry_file, "w", encoding="utf-8") as f:
            f.write(marker_line)

        markers = discover_active_map_markers()
        assert len(markers) == 1
        mtime_ns, path = markers[0]
        assert path == telemetry_file
        parsed = parse_active_map_marker(path, mtime_ns)
        assert parsed is not None
        assert parsed["map_key"] == "e1m4_boss"
        assert parsed["runtime_map"] == "game/sp/e1m4_boss/e1m4_boss"
        assert parsed["marker"] == "AP_MAP_START_E1M4_BOSS"


def test_map_identity_persists_in_cache_across_telemetry_file_deletion_and_menu_invalidates(monkeypatch):
    import asyncio
    monkeypatch.setattr(asyncio, "create_task", lambda *a, **kw: None)
    import bridge_client
    from bridge_client import DoomEternalContext, GameplaySaveEvidence, PrimarySaveSelection

    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(bridge_client, "INV_DUMP_DIR", tmpdir)
        ctx = DoomEternalContext("localhost:38281", "")

        now_ns = time.time_ns()
        telemetry_file = os.path.join(tmpdir, "ap_telemetry_ready.txt")
        marker_line = "AP_ACTIVE_MAP_V1 map_key=e1m4_boss runtime_map=game/sp/e1m4_boss/e1m4_boss marker=AP_MAP_START_E1M4_BOSS\n"
        with open(telemetry_file, "w", encoding="utf-8") as f:
            f.write(marker_line)
        os.utime(telemetry_file, (now_ns / 1e9, now_ns / 1e9))
        marker_mtime_ns = os.stat(telemetry_file).st_mtime_ns

        class MockLease:
            started_ns = marker_mtime_ns - 10_000_000_000
            gameplay_loaded_ns = marker_mtime_ns
            def process_probe(self):
                return True
            def validate(self, **kwargs):
                return (False, "map_mismatch")
            def validate_mission_select(self, **kwargs):
                return (True, "mission_select_lease_valid")

        ctx.runtime_observation_lease = MockLease()

        mock_evidence = GameplaySaveEvidence(
            "gameplay", 4, "GAME-AUTOSAVE2", "game/hub/hub", True
        )
        save_candidate = PrimarySaveSelection(
            "GAME-AUTOSAVE2", Path(tmpdir) / "save", marker_mtime_ns
        )

        monkeypatch.setattr("bridge_client.read_gameplay_save_evidence", lambda: mock_evidence)
        monkeypatch.setattr("bridge_client.primary_save_candidates", lambda: [save_candidate])
        monkeypatch.setattr("bridge_client.primary_save_for_slot", lambda slot: save_candidate)
        monkeypatch.setattr("bridge_client.read_game_details_for_selection", lambda s: {"mapName": "game/hub/hub", "_mtime_ns": now_ns})

        marker = ctx.read_active_map_identity(evidence=mock_evidence)
        assert marker is not None
        assert marker["map_key"] == "e1m4_boss"
        assert marker["gameplay_epoch"] == marker_mtime_ns
        assert marker["evidence_epoch"] == 4
        assert ctx.cached_map_identity is not None

        active_save = ctx.update_save_slot_lifecycle()
        assert active_save is not None
        assert ctx.active_save_proof_evidence_epoch == 4
        assert ctx.active_save_proof_load_epoch == marker_mtime_ns
        assert ctx.mission_select_observation_map == "game/sp/e1m4_boss/e1m4_boss"
        assert ctx.runtime_observers_frozen is False

        os.remove(telemetry_file)
        assert not os.path.exists(telemetry_file)

        for _ in range(5):
            marker_cached = ctx.read_active_map_identity(evidence=mock_evidence)
            assert marker_cached is not None
            assert marker_cached["map_key"] == "e1m4_boss"

            active_save_cached = ctx.update_save_slot_lifecycle()
            assert active_save_cached is not None
            assert ctx.mission_select_observation_map == "game/sp/e1m4_boss/e1m4_boss"
            assert ctx.runtime_observers_frozen is False

        # A transiently missing evidence file is safe only within the same load.
        monkeypatch.setattr("bridge_client.read_gameplay_save_evidence", lambda: None)
        assert ctx.update_save_slot_lifecycle() is not None
        assert ctx.runtime_observers_frozen is False
        assert ctx.active_save_proof_evidence_epoch == 4
        assert ctx.active_save_proof_load_epoch == marker_mtime_ns

        ctx.runtime_observation_lease.gameplay_loaded_ns = marker_mtime_ns + 1
        assert ctx.update_save_slot_lifecycle() is None
        assert ctx.runtime_observers_frozen is True

        menu_evidence = GameplaySaveEvidence("menu", 100, "GAME-AUTOSAVE2", "game/hub/hub")
        monkeypatch.setattr("bridge_client.read_gameplay_save_evidence", lambda: menu_evidence)

        marker_menu = ctx.read_active_map_identity(evidence=menu_evidence)
        assert marker_menu is None
        assert ctx.cached_map_identity is None

        active_save_menu = ctx.update_save_slot_lifecycle()
        assert active_save_menu is None
        assert ctx.mission_select_observation_map is None
        assert ctx.runtime_observers_frozen is True
        monkeypatch.setattr("bridge_client.read_gameplay_save_evidence", lambda: None)
        assert ctx.update_save_slot_lifecycle() is None
        assert ctx.runtime_observers_frozen is True


def test_new_load_epoch_rejects_old_identity_and_old_save_authority(monkeypatch):
    import asyncio
    monkeypatch.setattr(asyncio, "create_task", lambda *a, **kw: None)
    import bridge_client
    from bridge_client import DoomEternalContext, GameplaySaveEvidence, PrimarySaveSelection

    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(bridge_client, "INV_DUMP_DIR", tmpdir)
        ctx = DoomEternalContext("localhost:38281", "")

        marker_path = os.path.join(tmpdir, "ap_active_map_e1m4_boss.txt")
        with open(marker_path, "w", encoding="utf-8") as f:
            f.write(
                "AP_ACTIVE_MAP_V1 map_key=e1m4_boss "
                "runtime_map=game/sp/e1m4_boss/e1m4_boss "
                "marker=AP_MAP_START_E1M4_BOSS\n"
            )
        first_epoch = os.stat(marker_path).st_mtime_ns

        class MockLease:
            started_ns = first_epoch - 1
            gameplay_loaded_ns = first_epoch
            def process_probe(self):
                return True
            def validate(self, **kwargs):
                return (False, "map_mismatch")
            def validate_mission_select(self, **kwargs):
                return (True, "mission_select_lease_valid")

        ctx.runtime_observation_lease = MockLease()
        first_evidence = GameplaySaveEvidence(
            "gameplay", 100, "GAME-AUTOSAVE2", "game/hub/hub", True
        )
        selected = PrimarySaveSelection(
            "GAME-AUTOSAVE2", Path(tmpdir) / "save", first_epoch
        )
        monkeypatch.setattr("bridge_client.read_gameplay_save_evidence", lambda: first_evidence)
        monkeypatch.setattr("bridge_client.primary_save_candidates", lambda: [selected])
        monkeypatch.setattr("bridge_client.primary_save_for_slot", lambda slot: selected)
        monkeypatch.setattr(
            "bridge_client.read_game_details_for_selection",
            lambda selection: {"mapName": "game/hub/hub", "_mtime_ns": first_epoch},
        )

        assert ctx.update_save_slot_lifecycle() is not None
        assert ctx.mission_select_observation_map == "game/sp/e1m4_boss/e1m4_boss"

        second_evidence = GameplaySaveEvidence(
            "gameplay", 101, "GAME-AUTOSAVE2", "game/hub/hub", True
        )
        monkeypatch.setattr("bridge_client.read_gameplay_save_evidence", lambda: second_evidence)

        assert ctx.read_active_map_identity(evidence=second_evidence) is None
        assert ctx.cached_map_identity is None
        assert ctx.update_save_slot_lifecycle() is None
        assert ctx.runtime_observers_frozen is True
        assert ctx.mission_select_observation_map is None


def test_old_marker_is_rejected_by_new_load_epoch(monkeypatch):
    import asyncio
    monkeypatch.setattr(asyncio, "create_task", lambda *a, **kw: None)
    import bridge_client
    from bridge_client import DoomEternalContext, GameplaySaveEvidence

    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(bridge_client, "INV_DUMP_DIR", tmpdir)
        ctx = DoomEternalContext("localhost:38281", "")
        old_epoch = time.time_ns()
        marker_path = os.path.join(tmpdir, "ap_active_map_e1m4_boss.txt")
        with open(marker_path, "w", encoding="utf-8") as handle:
            handle.write("AP_ACTIVE_MAP_V1 map_key=e1m4_boss runtime_map=game/sp/e1m4_boss/e1m4_boss marker=AP_MAP_START_E1M4_BOSS\n")
        os.utime(marker_path, ns=(old_epoch, old_epoch))

        class Lease:
            started_ns = old_epoch
            gameplay_loaded_ns = old_epoch + 100
            def process_probe(self): return True

        ctx.runtime_observation_lease = Lease()
        evidence = GameplaySaveEvidence("gameplay", 4, "GAME-AUTOSAVE2", "game/hub/hub")
        assert ctx.read_active_map_identity(evidence) is None
        assert ctx.cached_map_identity is None


def test_tracker_ingests_marker_before_challenge_observation(monkeypatch):
    import asyncio
    import bridge_client
    from bridge_client import DoomEternalContext

    ctx = DoomEternalContext.__new__(DoomEternalContext)
    events = []
    class Exit:
        def __init__(self): self.done = False
        def is_set(self): return self.done
        def set(self): self.done = True
    class Socket: closed = False
    ctx.exit_event = Exit()
    ctx.server = type("Server", (), {"socket": Socket()})()
    ctx.runtime_observation_lease = type("Lease", (), {
        "gameplay_loaded_ns": 122,
        "observe_gameplay_loaded": lambda self, epoch: (events.append(("load", epoch)), setattr(self, "gameplay_loaded_ns", epoch)),
    })()
    ctx.mission_select_observation_map = None
    ctx.mission_select_observation_epoch = None
    ctx.cached_map_identity = None
    ctx.active_save_proof_authoritative = True
    ctx.active_save_proof_slot = "GAME-AUTOSAVE2"
    ctx.active_save_proof_evidence_epoch = 4
    ctx.active_save_proof_load_epoch = 122
    ctx.item_state_ready = False
    ctx.items_received = []
    ctx.items_processed = 0
    ctx.last_heartbeat_timestamp = None
    ctx.heartbeat_iteration_count = 0
    ctx.active_save_slot = None
    ctx.current_map_name = None
    ctx.items_received = []
    ctx.locations_checked = set()
    ctx.onboard_bootstrap = lambda *a: None
    ctx.reconcile_owned_perks = lambda *a: None
    ctx.reconcile_checked_automap_cleanup = lambda *a: None
    ctx.repair_item_mappings = lambda: True
    ctx.advance_reconciliation_epoch = lambda *a: 1
    ctx.advance_automap_cleanup_epoch = lambda: None
    ctx.check_mission_challenge_locations = lambda: None

    async def challenge():
        events.append(("challenge", ctx.cached_map_identity is not None,
                       ctx.active_save_proof_authoritative is False))
        ctx.exit_event.set()
    ctx.check_mission_challenge_locations = challenge
    marker = (123, "marker.txt")
    monkeypatch.setattr(bridge_client, "discover_telemetry_markers", lambda: [marker])
    monkeypatch.setattr(bridge_client, "parse_active_map_marker", lambda p, m: {"map_key": "hub", "runtime_map": "game/hub/hub", "mtime_ns": m})
    monkeypatch.setattr(bridge_client, "migrate_direct_item_command_jobs", lambda: None)
    monkeypatch.setattr(bridge_client, "rpc_execution_enabled", lambda: True)
    async def no_flush():
        return None
    monkeypatch.setattr(ctx, "flush_check_event_files", no_flush)
    async def no_sleep(_delay):
        return None
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    asyncio.run(ctx.tracker_loop())
    assert events == [("load", 123), ("challenge", False, True)]


def test_central_guard_revokes_unconsumed_new_load_and_malformed_load(monkeypatch):
    import bridge_client
    from bridge_client import DoomEternalContext
    ctx = DoomEternalContext.__new__(DoomEternalContext)
    ctx.runtime_observers_frozen = False
    ctx.active_save_proof_authoritative = True
    ctx.active_save_proof_slot = "GAME-AUTOSAVE2"
    ctx.active_save_proof_evidence_epoch = 4
    ctx.active_save_proof_load_epoch = 100
    ctx.cached_map_identity = {"map_key": "e1m4_boss"}
    ctx.current_map_name = "game/sp/e1m4_boss/e1m4_boss"
    ctx.mission_select_observation_map = "game/sp/e1m4_boss/e1m4_boss"
    class Lease:
        started_ns = 100
        gameplay_loaded_ns = 100
        def observe_gameplay_loaded(self, epoch): self.gameplay_loaded_ns = epoch
    ctx.runtime_observation_lease = Lease()
    monkeypatch.setattr(bridge_client, "discover_active_map_markers", lambda: [(200, "l2.txt")])
    monkeypatch.setattr(bridge_client, "parse_active_map_marker", lambda p, m: None)
    assert ctx.ingest_visible_runtime_lifecycle(evidence=None) is None
    assert ctx.runtime_observation_lease.gameplay_loaded_ns == 200
    assert ctx.active_save_proof_authoritative is False
    assert ctx.runtime_observers_frozen is True
    assert ctx.cached_map_identity is None


def test_central_guard_does_not_rewind_valid_load(monkeypatch):
    import bridge_client
    from bridge_client import DoomEternalContext
    ctx = DoomEternalContext.__new__(DoomEternalContext)
    ctx.runtime_observers_frozen = False
    ctx.active_save_proof_authoritative = True
    ctx.active_save_proof_slot = "GAME-AUTOSAVE2"
    ctx.active_save_proof_evidence_epoch = 4
    ctx.active_save_proof_load_epoch = 200
    class Lease:
        started_ns = 100
        gameplay_loaded_ns = 200
        def observe_gameplay_loaded(self, epoch): self.gameplay_loaded_ns = epoch
    ctx.runtime_observation_lease = Lease()
    monkeypatch.setattr(bridge_client, "discover_active_map_markers", lambda: [(150, "old.txt")])
    assert ctx.ingest_visible_runtime_lifecycle() is None
    assert ctx.runtime_observation_lease.gameplay_loaded_ns == 200
    assert ctx.active_save_proof_authoritative is True


def test_fresh_marker_survives_transient_menu_then_promotes_same_load(monkeypatch):
    import asyncio
    monkeypatch.setattr(asyncio, "create_task", lambda *a, **kw: None)
    import bridge_client
    from bridge_client import DoomEternalContext, GameplaySaveEvidence

    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(bridge_client, "INV_DUMP_DIR", tmpdir)
        ctx = DoomEternalContext("localhost:38281", "")
        marker_path = os.path.join(tmpdir, "ap_active_map_e1m4_boss.txt")
        with open(marker_path, "w", encoding="utf-8") as f:
            f.write(
                "AP_ACTIVE_MAP_V1 map_key=e1m4_boss "
                "runtime_map=game/sp/e1m4_boss/e1m4_boss "
                "marker=AP_MAP_START_E1M4_BOSS\n"
            )
        marker_epoch = os.stat(marker_path).st_mtime_ns

        class Lease:
            started_ns = marker_epoch - 1
            gameplay_loaded_ns = None
            def process_probe(self):
                return True
            def observe_gameplay_loaded(self, epoch):
                self.gameplay_loaded_ns = epoch

        ctx.runtime_observation_lease = Lease()
        menu = GameplaySaveEvidence("menu", 100, "GAME-AUTOSAVE2", "game/hub/hub")
        assert ctx.read_active_map_identity(menu) is None
        assert ctx.pending_map_identity["map_key"] == "e1m4_boss"
        assert ctx.cached_map_identity is None
        assert ctx.current_map_name is None
        assert ctx.runtime_observers_frozen is True

        gameplay = GameplaySaveEvidence(
            "gameplay", 101, "GAME-AUTOSAVE2", "game/sp/e1m4_boss/e1m4_boss"
        )
        promoted = ctx.read_active_map_identity(gameplay)
        assert promoted["map_key"] == "e1m4_boss"
        assert promoted["gameplay_epoch"] == marker_epoch
        assert promoted["evidence_epoch"] == 101
        assert ctx.pending_map_identity is None
        assert ctx.current_map_name == "game/sp/e1m4_boss/e1m4_boss"
        assert ctx.runtime_observers_frozen is True


def test_newer_load_discards_old_pending_marker_and_process_exit_clears(monkeypatch):
    import asyncio
    monkeypatch.setattr(asyncio, "create_task", lambda *a, **kw: None)
    import bridge_client
    from bridge_client import DoomEternalContext, GameplaySaveEvidence

    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(bridge_client, "INV_DUMP_DIR", tmpdir)
        ctx = DoomEternalContext("localhost:38281", "")
        mission_path = os.path.join(tmpdir, "ap_active_map_e1m4_boss.txt")
        with open(mission_path, "w", encoding="utf-8") as f:
            f.write(
                "AP_ACTIVE_MAP_V1 map_key=e1m4_boss "
                "runtime_map=game/sp/e1m4_boss/e1m4_boss "
                "marker=AP_MAP_START_E1M4_BOSS\n"
            )
        first_epoch = os.stat(mission_path).st_mtime_ns

        class Lease:
            started_ns = first_epoch - 1
            gameplay_loaded_ns = None
            running = True
            def process_probe(self):
                return self.running
            def observe_gameplay_loaded(self, epoch):
                self.gameplay_loaded_ns = epoch

        lease = Lease()
        ctx.runtime_observation_lease = lease
        menu = GameplaySaveEvidence("menu", 100, "GAME-AUTOSAVE2", "game/hub/hub")
        assert ctx.read_active_map_identity(menu) is None
        assert ctx.pending_map_identity["map_key"] == "e1m4_boss"

        hub_path = os.path.join(tmpdir, "ap_active_map_hub.txt")
        with open(hub_path, "w", encoding="utf-8") as f:
            f.write(
                "AP_ACTIVE_MAP_V1 map_key=hub runtime_map=game/hub/hub "
                "marker=AP_MAP_START_HUB\n"
            )
        hub_epoch = max(time.time_ns(), first_epoch + 1_000_000)
        os.utime(hub_path, ns=(hub_epoch, hub_epoch))
        assert ctx.read_active_map_identity(menu) is None
        assert ctx.pending_map_identity["map_key"] == "hub"
        assert ctx.pending_map_identity["mtime_ns"] == os.stat(hub_path).st_mtime_ns
        assert ctx.current_map_name is None

        lease.running = False
        assert ctx.read_active_map_identity(menu) is None
        assert ctx.pending_map_identity is None
        assert ctx.cached_map_identity is None
        assert ctx.current_map_name is None


def test_fresh_hub_marker_replaces_mission_identity(monkeypatch):
    import asyncio
    monkeypatch.setattr(asyncio, "create_task", lambda *a, **kw: None)
    import bridge_client
    from bridge_client import DoomEternalContext, GameplaySaveEvidence

    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(bridge_client, "INV_DUMP_DIR", tmpdir)
        ctx = DoomEternalContext("localhost:38281", "")

        mission_path = os.path.join(tmpdir, "ap_active_map_e1m4_boss.txt")
        with open(mission_path, "w", encoding="utf-8") as f:
            f.write(
                "AP_ACTIVE_MAP_V1 map_key=e1m4_boss "
                "runtime_map=game/sp/e1m4_boss/e1m4_boss "
                "marker=AP_MAP_START_E1M4_BOSS\n"
            )
        mission_epoch = os.stat(mission_path).st_mtime_ns

        class MockLease:
            started_ns = mission_epoch - 1
            gameplay_loaded_ns = mission_epoch
            def process_probe(self):
                return True

        ctx.runtime_observation_lease = MockLease()
        mission_evidence = GameplaySaveEvidence(
            "gameplay", 100, "GAME-AUTOSAVE2", "game/hub/hub", True
        )
        assert ctx.read_active_map_identity(mission_evidence)["map_key"] == "e1m4_boss"

        hub_path = os.path.join(tmpdir, "ap_active_map_hub.txt")
        with open(hub_path, "w", encoding="utf-8") as f:
            f.write(
                "AP_ACTIVE_MAP_V1 map_key=hub runtime_map=game/hub/hub "
                "marker=AP_MAP_START_HUB\n"
            )
        hub_epoch = max(time.time_ns(), mission_epoch + 1_000_000)
        os.utime(hub_path, ns=(hub_epoch, hub_epoch))
        hub_epoch = os.stat(hub_path).st_mtime_ns
        ctx.runtime_observation_lease.gameplay_loaded_ns = hub_epoch

        hub_evidence = GameplaySaveEvidence(
            "gameplay", 101, "GAME-AUTOSAVE2", "game/hub/hub"
        )
        hub_marker = ctx.read_active_map_identity(hub_evidence)
        assert hub_marker is not None
        assert hub_marker["map_key"] == "hub"
        assert hub_marker["runtime_map"] == "game/hub/hub"
        assert hub_marker["gameplay_epoch"] == hub_epoch
        assert hub_marker["evidence_epoch"] == 101
