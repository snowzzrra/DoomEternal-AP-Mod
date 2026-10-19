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


def test_restarted_producer_replays_from_zero_then_persists_before_publication(bridge, monkeypatch):
    namespace = "a" * 64
    old = ["71", "101", "b" * 32, "build"]
    current = ["72", "102", "c" * 32, "build"]
    session = {"killsanity": {"counter_abi": 2, "total": 87, "cursor": 87, "producer": old}}
    kills = Killsanity(logging.getLogger(__name__))
    kills.bind(session, namespace, [{"threshold": 100, "location_id": 1}])
    requests, committed, sent, submitted, presented = [], [], [], set(), []
    def observe(maps):
        requests.append(maps)
        return dict(producer=current, namespace_id=namespace, qualified_kills=13, observation_gaps=0)

    async def send(messages):
        assert committed[-1] == (100, 13)
        sent.extend(messages)

    def present(*args):
        assert committed[-1] == (100, 13)
        presented.append(args)

    publication = CheckPublication(send, submitted.add, 30)
    ctx = SimpleNamespace(
        killsanity=kills, item_state_ready=True, state_key="room", session_state=session,
        native_game_link=lambda: SimpleNamespace(namespace=namespace, observe_combat=observe,
                                                publish_combat_progress=present),
        persist_session_state=lambda: committed.append((kills.total, kills.cursor)),
        check_observation=lambda: CheckObservation(frozenset(), frozenset(submitted), frozenset({1}), True, True),
        check_publication=lambda: publication,
    )

    async def run():
        await bridge.DoomEternalContext.check_killsanity(ctx, True)
        await bridge.DoomEternalContext.check_killsanity(ctx, True)
        assert len(requests) == 2 and requests[0] == requests[1]
        assert requests[0] == set(bridge.CONTEXT_BY_MAP)
        assert any("hub" in name for name in requests[0])
        assert kills.total == 100 and committed == [(100, 13)]
        assert sent == [{"cmd": "LocationChecks", "locations": [1]}]
        assert presented == [(100, 0, 100, 100, 100, False), (100, 0, 0, 0, 0, False)]

        def change_room(maps):
            ctx.state_key, ctx.session_state = "other", {}
            kills.bind(ctx.session_state, "d" * 64, [{"threshold": 100, "location_id": 1}])
            return observe(maps)

        ctx.native_game_link = lambda: SimpleNamespace(namespace=namespace, observe_combat=change_room)
        await bridge.DoomEternalContext.check_killsanity(ctx, True)
        assert kills.total == 0 and "killsanity" not in ctx.session_state
        assert len(committed) == 1 and len(sent) == 1
        assert len(presented) == 2

        ctx.killsanity_counter_visible = True
        ctx.exit_event = asyncio.Event()
        monkeypatch.setattr(bridge.sys, "stdin", io.StringIO(
            'AP_CONTROL {"type":"killsanity_counter_visibility","visible":false}\n'
            'AP_CONTROL {"type":"killsanity_counter_visibility","visible":1}\n/exit\n'))
        await bridge.launcher_control_loop(ctx)
        assert ctx.killsanity_counter_visible is False

    asyncio.run(run())


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


def test_probe_snapshot_persists_then_retries_checks_until_server_confirmation(bridge, tmp_path):
    from doom_eap.runtime.client_state_store import ClientStateStore
    from doom_eap.runtime.weapon_points import SentinelWeaponPoints

    namespace = 'a' * 64
    session = {}
    milestones = [{'threshold': number * 100, 'location_id': number} for number in range(1, 4)]
    kills = Killsanity(logging.getLogger(__name__))
    kills.bind(session, namespace, milestones)
    store = ClientStateStore(tmp_path / 'progress.json', version=2, migrate=lambda value: (value, False),
                             log_event=lambda *args, **kwargs: None, logger=logging.getLogger(__name__))
    link = SentinelWeaponPoints(tmp_path / 'probe.exe', 71, namespace)
    response = dict(result='ok', combat_abi=2, pid=71, process_created='101',
                    instance_id='b' * 32, build_id='build', availability=0, namespace_id=namespace,
                    qualified_kills='350', observation_gaps='2')
    link._run = lambda arguments, payload: response
    link.publish_combat_progress = lambda *args: None
    sent, submitted, checked = [], set(), set()
    connected, fail_send = False, True

    async def send(messages):
        nonlocal fail_send
        assert json.loads(store.path.read_text(encoding='utf-8'))['sessions']['room']['killsanity']['total'] == 350
        if fail_send:
            fail_send = False
            raise OSError('disconnected')
        sent.extend(messages)

    ctx = SimpleNamespace(
        killsanity=kills, item_state_ready=True, state_key='room', session_state=session,
        native_game_link=lambda: link,
        persist_session_state=lambda: store.commit({'version': 2, 'sessions': {'room': session}}, reason='test'),
        check_observation=lambda: CheckObservation(frozenset(checked), frozenset(submitted),
                                                  frozenset({1, 2, 3}), True, connected),
        check_publication=lambda: CheckPublication(send, submitted.add, 30),
    )

    async def run():
        nonlocal connected
        await bridge.DoomEternalContext.check_killsanity(ctx, True)
        assert kills.total == 350 and kills.state['source_gaps'] == 2 and not sent
        connected = True
        await bridge.DoomEternalContext.check_killsanity(ctx, False)
        assert not submitted
        await bridge.DoomEternalContext.check_killsanity(ctx, False)
        assert submitted == {1, 2, 3}
        checked.update(submitted)
        submitted.clear()
        await bridge.DoomEternalContext.check_killsanity(ctx, True)
        assert sent == [{'cmd': 'LocationChecks', 'locations': [1, 2, 3]}]
        assert kills.total == 350
        kills.bind(session, namespace, [])
        link.observe_combat = lambda maps: pytest.fail('disabled Killsanity queried native combat')
        await bridge.DoomEternalContext.check_killsanity(ctx, True)
        assert len(sent) == 1

    asyncio.run(run())
