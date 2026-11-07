import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from doom_eap.contracts.check_observation import CheckObservation
from doom_eap.runtime.bridge_checks import CheckPublication
from doom_eap.runtime.campaign import Killsanity


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
        killsanity=kills,
        item_state_ready=True,
        state_key='room',
        session_state=session,
        persist_session_state=lambda: committed.append(kills.total),
        items=None,
        checks=None,
        saves=None
    )
    ctx.items = bridge.bridge_items.ItemRuntime(ctx)
    ctx.items._native_game_link_async = AsyncMock(return_value=SimpleNamespace(
            namespace=namespace, observe_combat=observe, publish_combat_progress=present))
    ctx.checks = bridge.bridge_checks.CheckRuntime(ctx)
    ctx.checks.check_observation = lambda: CheckObservation(frozenset(), frozenset(), frozenset({1}), True, True)
    ctx.checks.check_publication = lambda: CheckPublication(send, lambda location: None, 30)
    ctx.saves = bridge.bridge_saves.SaveRuntime(ctx)



    async def native_call(link, method, *args):
        return await asyncio.to_thread(getattr(link, method), *args)
    ctx.items._native_call = native_call
    asyncio.run(ctx.checks.check_killsanity(True))
    assert sent == []
    assert committed == ([100] if phase == 'present' else [])
    assert session.get('killsanity', {}).get('total', 0) == (100 if phase == 'present' else 0)
