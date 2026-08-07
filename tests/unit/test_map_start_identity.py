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


def test_map_start_identity_entity_generation():
    entities = generate_system_command_entities(
        map_key="e1m4_boss", runtime_map="game/sp/e1m4_boss/e1m4_boss"
    )
    assert "entityDef ap_rpc_auto_enable" in entities
    assert "AP_ACTIVE_MAP_V1 map_key=e1m4_boss" in entities
    assert "runtime_map=game/sp/e1m4_boss/e1m4_boss" in entities
    assert "marker=AP_MAP_START_E1M4_BOSS" in entities
    assert "condump ap_active_map.txt" in entities
    assert "condump ap_telemetry_ready.txt" in entities


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

    class MockLease:
        def process_probe(self):
            return False

    ctx.runtime_observation_lease = MockLease()
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
        
        assert audit["owner"] == "gameresources"
        assert audit["challenge_count"] == 27
        assert audit["aggregate_reward_suppression"]["aggregate_count"] == 9
        
        for p in audit["written_paths"]:
            assert "gameresources" in p
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

        class MockLease:
            started_ns = now_ns - 10_000_000_000
            gameplay_loaded_ns = now_ns
            def process_probe(self):
                return True
            def validate(self, **kwargs):
                return (False, "map_mismatch")
            def validate_mission_select(self, **kwargs):
                return (True, "mission_select_lease_valid")

        ctx.runtime_observation_lease = MockLease()

        mock_evidence = GameplaySaveEvidence("gameplay", 100, "GAME-AUTOSAVE2", "game/hub/hub")
        save_candidate = PrimarySaveSelection("GAME-AUTOSAVE2", Path(tmpdir) / "save", now_ns)

        monkeypatch.setattr("bridge_client.read_gameplay_save_evidence", lambda: mock_evidence)
        monkeypatch.setattr("bridge_client.primary_save_candidates", lambda: [save_candidate])
        monkeypatch.setattr("bridge_client.primary_save_for_slot", lambda slot: save_candidate)
        monkeypatch.setattr("bridge_client.read_game_details_for_selection", lambda s: {"mapName": "game/hub/hub", "_mtime_ns": now_ns})

        marker = ctx.read_active_map_identity(evidence=mock_evidence)
        assert marker is not None
        assert marker["map_key"] == "e1m4_boss"
        assert ctx.cached_map_identity is not None

        active_save = ctx.update_save_slot_lifecycle()
        assert active_save is not None
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

        menu_evidence = GameplaySaveEvidence("menu", 100, "GAME-AUTOSAVE2", "game/hub/hub")
        monkeypatch.setattr("bridge_client.read_gameplay_save_evidence", lambda: menu_evidence)

        marker_menu = ctx.read_active_map_identity(evidence=menu_evidence)
        assert marker_menu is None
        assert ctx.cached_map_identity is None

        active_save_menu = ctx.update_save_slot_lifecycle()
        assert active_save_menu is None
        assert ctx.mission_select_observation_map is None
        assert ctx.runtime_observers_frozen is True
