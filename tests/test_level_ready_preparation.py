import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_pending_observation_does_not_repeat_preparation(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    if not (root.parent / "Archipelago" / "CommonClient.py").is_file():
        pytest.skip("Archipelago source required for bridge integration")
    for directory in (tmp_path / "game/base", tmp_path / "local", tmp_path / "Steam/userdata/1/782330/remote"):
        directory.mkdir(parents=True)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "doom_base_dir": str(tmp_path / "game/base"), "save_games_dir": str(tmp_path / "local"),
        "steam_remote_dir": str(tmp_path / "Steam/userdata/1/782330/remote"),
        "client_state_file": str(tmp_path / "state.json"), "bridge_log_path": str(tmp_path / "bridge.log"),
    }))
    monkeypatch.setenv("DOOM_AP_CONFIG_FILE", str(config))
    monkeypatch.setenv("DOOM_AP_APPLICATION_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(root.parent / "Archipelago"))
    monkeypatch.syspath_prepend(str(root / "packaging/standalone_runtime"))
    bridge = importlib.import_module("doom_eap.runtime.bridge_client")
    monkeypatch.setattr(bridge, "QUEUE_DIR", str(tmp_path / "game/base/ap_queue"))
    from doom_eap.contracts.runtime_context import RuntimeContext

    async def exercise():
        context = bridge.DoomEternalContext(None, None)
        context.state_key = "room"
        marker = {"gameplay_epoch": "42:1", "path": "marker", "runtime_map": "game/sp/e1m1_intro/e1m1_intro"}
        context.runtime_lifecycle.accept_marker(marker, 1)
        evidence = SimpleNamespace(state="GAMEPLAY_READY")
        monkeypatch.setattr(bridge, "read_gameplay_save_evidence", lambda: evidence)
        monkeypatch.setattr(bridge, "rpc_execution_enabled", lambda: True)
        monkeypatch.setattr(context, "read_active_map_identity", lambda **kw: marker)
        monkeypatch.setattr(context, "snapshot_fast_travel_eligibility", lambda: None)
        monkeypatch.setattr(context, "runtime_effects_ready", lambda _: True)
        runtime = RuntimeContext("base/e1m1_intro", "Base", ("game/sp/e1m1_intro/e1m1_intro",), ("e1m1_intro",), frozenset())
        monkeypatch.setattr(context, "_refresh_runtime_context", lambda _: runtime)
        published = []
        def publish(name):
            result = bridge.command_spool().publish("ai_ScriptCmdEnt ap_fixture activate",
                coalesce_key=name, room_scoped=False, arm_rpc=False)
            assert result.accepted
            published.append(name)
        monkeypatch.setattr(context, "advance_reconciliation_epoch", lambda _: published.append("epoch") or 1)
        monkeypatch.setattr(context, "reconcile_owned_runes", lambda _: (None, None))
        monkeypatch.setattr(context, "advance_automap_cleanup_epoch", lambda: published.append("cleanup_epoch"))
        monkeypatch.setattr(context, "reconcile_checked_automap_cleanup", lambda _: publish("cleanup"))
        monkeypatch.setattr(context, "reconcile_fast_travel_unlock", lambda _: publish("fast_travel"))
        async def challenges():
            pass
        monkeypatch.setattr(context, "check_mission_challenge_locations", challenges)
        observation = ["empty plan has unresolved native observations"]
        monkeypatch.setattr(context, "_context_materialize_inventory", lambda *a, **kw: (None, observation[0]))
        context.level_ready.queue("42:1", None)
        for _ in range(5):
            assert not await context.process_level_ready()
        assert published == ["epoch", "cleanup_epoch", "cleanup"]
        observation[0] = None
        assert await context.process_level_ready()
        assert published == ["epoch", "cleanup_epoch", "cleanup", "fast_travel"]
        assert not await context.process_level_ready()

    asyncio.run(exercise())
