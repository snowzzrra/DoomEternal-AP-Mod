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
    assert ctx.mission_select_observation_map is None
    assert ctx.mission_select_observation_epoch is None


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
        
        assert audit["owner"] == "gameresources_patch3"
        assert audit["challenge_count"] == 27
        assert audit["aggregate_reward_suppression"]["aggregate_count"] == 9
        
        for p in audit["written_paths"]:
            assert "gameresources_patch3" in p
            assert "gameresources_patch2" not in p

        errors = validate_overrides_from_mod_root(mod_root, registry_path)
        assert errors == [], f"Validation failed with errors: {errors}"
