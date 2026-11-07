"""Temporary effects expire, combine and reset through the asynchronous transport port."""
import asyncio

from doom_eap.runtime import bridge_items
from doom_eap.runtime.bridge_items import TransientEffectManager, TransientRuntime


class Publication:
    def __init__(self):
        self.commands = []
        self.ready = True

    def publish_scope(self, scope):
        self.scope = scope

    async def send(self, cvar, command, state_key, scope, **options):
        self.commands.append((command, options.get('receipt_index')))
        return self.ready


def test_duration_stacks_and_expiry_cleans_each_policy(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(bridge_items.time, 'monotonic', lambda: now[0])
    publication = Publication()
    manager = TransientEffectManager(publication, 1)
    runtime = TransientRuntime('room', ('1', '1'), True)
    async def _assert_stacked_effect_duration(manager, publication, runtime):
        assert (await manager.apply_receipt(7770156, runtime, receipt_index=0))[0]
        assert (await manager.apply_receipt(7770159, runtime, receipt_index=1))[0]
        assert publication.commands[-3] == ('g_damageScaleAllToAI 1.05', 1)


    async def check():
        assert (await manager.apply_receipt(7770156, runtime, receipt_index=0))[0]
        now[0] += 1
        await _assert_stacked_effect_duration(manager, publication, runtime)
        now[0] += 12
        assert await manager.tick(runtime)
        assert publication.commands[-3] == ('g_damageScaleAllToAI 1.50', None)
        now[0] = 121
        assert await manager.tick(runtime)
        assert publication.commands[-3:] == [('g_damageScaleAllToAI 1.00', None),
            ('g_damageScaleAllToSlayer 1.00', None), ('g_infiniteAmmo 0', None)]
        before = len(publication.commands)
        assert await manager.tick(runtime)
        assert len(publication.commands) == before
    asyncio.run(check())


def test_cleanup_retries_after_transport_failure_and_restart():
    publication = Publication()
    manager = TransientEffectManager(publication, 1)
    runtime = TransientRuntime('room', ('1', '1'), True)
    async def check():
        assert (await manager.apply_receipt(7770158, runtime, receipt_index=0))[0]
        publication.ready = False
        await manager.reset('disconnect', runtime)
        publication.ready = True
        assert await manager.tick(runtime)
        assert publication.commands[-1] == ('g_infiniteAmmo 0', None)
        restarted = TransientEffectManager(publication, 2)
        assert await restarted.tick(runtime)
        assert publication.commands[-1] == ('g_infiniteAmmo 0', None)
    asyncio.run(check())
