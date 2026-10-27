import asyncio
import importlib
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from doom_eap.contracts.check_observation import CheckObservation
from doom_eap.runtime.check_publication import CheckPublication
from doom_eap.runtime.killsanity import Killsanity


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    game, saves = tmp_path / "game/base", tmp_path / "local"
    game.mkdir(parents=True)
    saves.mkdir()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"doom_base_dir": str(game), "save_games_dir": str(saves),
                                  "client_state_file": str(tmp_path / "state.json"),
                                  "bridge_log_path": str(tmp_path / "bridge.log")}), encoding="utf-8")
    monkeypatch.setenv("DOOM_AP_CONFIG_FILE", str(config))
    monkeypatch.setenv("DOOM_AP_APPLICATION_DIR", str(tmp_path))
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "Archipelago"))
    monkeypatch.syspath_prepend(str(root / "DoomEternal-AP-Mod" / "packaging" / "standalone_runtime"))
    return importlib.import_module("doom_eap.runtime.bridge_client")




@pytest.mark.parametrize('phase', ['observe', 'present'])
@pytest.mark.parametrize('same_binding', [False, True])
def test_late_observation_or_presentation_cannot_publish_after_rebind(bridge, phase, same_binding):
    namespace = 'a' * 64
    session = {}
    kills = Killsanity(logging.getLogger(__name__))
    milestones = [{'threshold': 100, 'location_id': 1}]
    kills.bind(session, namespace, milestones)
    committed, sent = [], []

    def rebind():
        if not same_binding:
            ctx.state_key, ctx.session_state = 'other', {'killsanity': {'total': 200}}
        kills.bind(ctx.session_state, namespace if same_binding else 'b' * 64, milestones)

    def observe(maps):
        if phase == 'observe':
            rebind()
        return dict(producer=['71', '101', 'c' * 32, 'build'], namespace_id=namespace,
                    qualified_kills=100, observation_gaps=0)

    def present(*args):
        if phase == 'present':
            rebind()

    async def send(messages):
        sent.extend(messages)

    ctx = SimpleNamespace(
        killsanity=kills, item_state_ready=True, state_key='room', session_state=session,
        native_game_link=lambda: SimpleNamespace(namespace=namespace, observe_combat=observe,
                                                publish_combat_progress=present),
        persist_session_state=lambda: committed.append(kills.total),
        check_observation=lambda: CheckObservation(frozenset(), frozenset(), frozenset({1}), True, True),
        check_publication=lambda: CheckPublication(send, lambda location: None, 30),
    )
    asyncio.run(bridge.DoomEternalContext.check_killsanity(ctx, True))
    assert sent == []
    assert committed == ([100] if phase == 'present' else [])
    assert session.get('killsanity', {}).get('total', 0) == (100 if phase == 'present' else 0)
