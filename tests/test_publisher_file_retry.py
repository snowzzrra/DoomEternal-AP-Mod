import asyncio
import builtins
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("kind", ["map_event_file", "native_transition"])
def test_unreadable_completion_waits_for_retry_and_rejects_malformed_content(tmp_path, monkeypatch, kind):
    game, saves = tmp_path / "game/base", tmp_path / "saves"
    game.mkdir(parents=True)
    saves.mkdir()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"doom_base_dir": str(game), "save_games_dir": str(saves),
        "client_state_file": str(tmp_path / "state.json"), "bridge_log_path": str(tmp_path / "bridge.log")}),
        encoding="utf-8")
    monkeypatch.setenv("DOOM_AP_CONFIG_FILE", str(config))
    monkeypatch.setenv("DOOM_AP_APPLICATION_DIR", str(tmp_path))
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "Archipelago"))
    monkeypatch.syspath_prepend(str(root / "DoomEternal-AP-Mod/packaging/standalone_runtime"))
    bridge = importlib.import_module("doom_eap.runtime.bridge_client")
    monkeypatch.setattr(bridge, "INV_DUMP_DIR", str(game))
    publisher = next(p for p in bridge.PUBLISHERS if p.triggers_for(kind) and
                     (kind == "native_transition" or any(e.get("location_id") == 7770337 for e in p.effects)))
    trigger = publisher.triggers_for(kind)[0]
    event = game / (trigger["filename"] if kind == "map_event_file" else "transition.txt")
    contents = (trigger["marker"] if kind == "map_event_file" else
                f"from_map={trigger['from_map']}\nto_map={trigger['to_map']}\n")
    event.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(bridge, "goal_event_files", lambda: [str(event)] if kind == "native_transition" and event.exists() else [])
    ctx = SimpleNamespace(state_key="room", check_and_update_event_session=lambda: True, receipt_session=SimpleNamespace(
        capture=lambda state_key: state_key, is_current=lambda token, state_key: token == state_key))
    executed = []

    async def execute(self, selected, *args):
        executed.append(selected.key)
        return True

    monkeypatch.setattr(bridge.DoomEternalContext, "execute_publisher", execute)
    original = Path.read_text if kind == "map_event_file" else builtins.open

    def unreadable(path, *args, **kwargs):
        if Path(path) == event:
            raise PermissionError("writer is holding the event")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as locked:
        locked.setattr(Path if kind == "map_event_file" else builtins,
                       "read_text" if kind == "map_event_file" else "open", unreadable)
        assert asyncio.run(bridge.DoomEternalContext.check_campaign_goal_event(ctx))
    assert not executed and event.read_text(encoding="utf-8") == contents
    assert not (game / "quarantine").exists()
    event.write_text("unrelated console output\n", encoding="utf-8")
    assert asyncio.run(bridge.DoomEternalContext.check_campaign_goal_event(ctx))
    assert not executed and not event.exists()
    assert list((game / "quarantine/failed").glob("*.json"))
    event.write_text(contents, encoding="utf-8")
    assert asyncio.run(bridge.DoomEternalContext.check_campaign_goal_event(ctx))
    assert publisher.key in executed and not event.exists()
