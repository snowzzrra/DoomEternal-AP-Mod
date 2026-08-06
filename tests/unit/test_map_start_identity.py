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
)
from tools.decls.mission_challenge_decl_builder import build_mission_challenge_overrides


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


def test_parse_active_map_marker():
    sample_content = (
        "AP_ACTIVE_MAP_V1 map_key=e1m4_boss "
        "runtime_map=game/sp/e1m4_boss/e1m4_boss marker=AP_MAP_START_E1M4_BOSS\n"
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


def test_mission_challenge_overrides_owner_is_patch3():
    with tempfile.TemporaryDirectory() as tmpdir:
        mod_root = Path(tmpdir) / "mod"
        audit_output = Path(tmpdir) / "audit.json"
        audit = build_mission_challenge_overrides(mod_root)
        
        assert audit["owner"] == "gameresources_patch3"
        assert audit["challenge_count"] == 27
        assert audit["aggregate_reward_suppression"]["aggregate_count"] == 9
        
        # Verify written path owner
        for p in audit["written_paths"]:
            assert "gameresources_patch3" in p
            assert "gameresources_patch2" not in p
