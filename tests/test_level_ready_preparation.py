import asyncio
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_boss_without_item_checks_can_recover_materialization(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv('DOOM_AP_APPLICATION_DIR', str(tmp_path))
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'doom_base_dir': str(tmp_path / 'base'),
        'save_games_dir': str(tmp_path / 'saves'), 'client_state_file': str(tmp_path / 'state.json'),
        'bridge_log_path': str(tmp_path / 'bridge.log')}))
    monkeypatch.setenv('DOOM_AP_CONFIG_FILE', str(config))
    env = dict(os.environ, SKIP_REQUIREMENTS_UPDATE='1', PYTHONPATH=os.pathsep.join(map(str, (
        root, Path(os.environ.get('ARCHIPELAGO_SOURCE', root.parent / 'Archipelago')), root / 'packaging/standalone_runtime'))))
    script = '''
from pathlib import Path
from types import SimpleNamespace
from doom_eap.runtime import bridge_client as bridge
from doom_eap.runtime.lifecycle import RuntimeLifecycle
runtime = 'game/dlc2/e5m4_boss/e5m4_boss'
evidence = SimpleNamespace(state='gameplay', epoch=9, map_name=runtime, provisional=False)
lifecycle = RuntimeLifecycle()
decision = lifecycle.classify_native_load(evidence, bridge.MATERIALIZATION_MAPS)
assert decision is not None and decision.map_key == 'e5m4_boss'
marker = Path('marker.txt')
marker.write_text(f'AP_ACTIVE_MAP_V1 map_key=e5m4_boss runtime_map={runtime} marker=AP_MAP_START_E5M4_BOSS')
assert bridge.parse_active_map_marker(marker, 100)['runtime_map'] == runtime
evidence.map_name = 'game/dlc2/unrecognized/unrecognized'
assert lifecycle.classify_native_load(evidence, bridge.MATERIALIZATION_MAPS) is None
'''
    subprocess.run([sys.executable, '-c', script], cwd=tmp_path, env=env, check=True, timeout=30)


def test_tracker_passes_newest_telemetry_path_to_level_ready(tmp_path):
    from delivery_scenario_support import environment, prepare

    prepare(tmp_path)
    script = '''
import asyncio
from pathlib import Path
from types import SimpleNamespace
import Utils
Utils.user_path.cached_path = str(Path.cwd())
from doom_eap.runtime import bridge_client as bridge

class Context(bridge.DoomEternalContext):
    async def process_level_ready(self, newest_path=None):
        self.observed_path = newest_path
        self.exit_event.set()
        return True

async def exercise():
    context = Context(None, None)
    context.item_state_ready = True
    context.server = SimpleNamespace(socket=SimpleNamespace(closed=False))
    context.reconnect_resync_attempted = True
    bridge.doom_process_identity = lambda: 'fixture'
    bridge.read_gameplay_save_evidence = lambda: SimpleNamespace(state='not_running')
    paths = [Path('older.txt'), Path('newest.txt')]
    for path in paths:
        path.write_text('AP_ACTIVE_MAP_V1 map_key=e1m1_intro runtime_map=game/sp/e1m1_intro/e1m1_intro marker=AP_MAP_START_E1M1_INTRO')
    bridge.discover_telemetry_markers = lambda: [(index + 1, str(path)) for index, path in enumerate(paths)]
    try:
        await asyncio.wait_for(context.tracker_loop(), 5)
        assert context.observed_path == str(paths[-1])
        assert all(not path.exists() for path in paths)
    finally:
        context._blocking_native_worker().close()

asyncio.run(exercise())
'''
    subprocess.run([sys.executable, '-B', '-c', script], cwd=tmp_path,
                   env=environment(tmp_path), check=True, timeout=30)


def test_pending_observation_does_not_repeat_preparation(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    if not (Path(os.environ.get("ARCHIPELAGO_SOURCE", root.parent / "Archipelago")) / "CommonClient.py").is_file():
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
    monkeypatch.syspath_prepend(str(Path(os.environ.get("ARCHIPELAGO_SOURCE", root.parent / "Archipelago"))))
    monkeypatch.syspath_prepend(str(root / "packaging/standalone_runtime"))
    bridge = importlib.import_module("doom_eap.runtime.bridge_client")
    from doom_eap.contracts.runtime_context import RuntimeContext

    async def exercise():
        context = bridge.DoomEternalContext(None, None)
        context.state_key = "room"
        marker = {"gameplay_epoch": "42:1", "path": "marker", "runtime_map": "game/sp/e1m1_intro/e1m1_intro"}
        context.runtime_lifecycle.accept_marker(marker, 1)
        evidence = SimpleNamespace(state="GAMEPLAY_READY")
        monkeypatch.setattr(bridge, "read_gameplay_save_evidence", lambda: evidence)
        monkeypatch.setattr(context, "read_active_map_identity", AsyncMock(return_value=marker))
        monkeypatch.setattr(context, "snapshot_fast_travel_eligibility", lambda: None)
        monkeypatch.setattr(context, "runtime_effects_ready", AsyncMock(return_value=True))
        runtime = RuntimeContext("base/e1m1_intro", "Base", ("game/sp/e1m1_intro/e1m1_intro",), ("e1m1_intro",), frozenset())
        monkeypatch.setattr(context, "_refresh_runtime_context", lambda _: runtime)
        published = []
        def publish(name):
            published.append(name)
        monkeypatch.setattr(context, "advance_automap_cleanup_epoch", lambda: published.append("cleanup_epoch"))
        monkeypatch.setattr(context, "reconcile_checked_automap_cleanup", AsyncMock(side_effect=lambda _: publish("cleanup")))
        monkeypatch.setattr(context, "reconcile_fast_travel_unlock", AsyncMock(side_effect=lambda _: publish("fast_travel")))
        async def challenges():
            pass
        monkeypatch.setattr(context, "check_mission_challenge_locations", challenges)
        observation = ["empty plan has unresolved native observations"]
        monkeypatch.setattr(context, "_context_materialize_inventory", AsyncMock(side_effect=lambda *a, **kw: (None, observation[0])))
        context.level_ready.queue("42:1", None)
        for _ in range(5):
            assert not await context.process_level_ready()
        assert published == ["cleanup_epoch", "cleanup"]
        observation[0] = None
        assert await context.process_level_ready()
        assert published == ["cleanup_epoch", "cleanup", "fast_travel"]
        assert not await context.process_level_ready()

    asyncio.run(exercise())
