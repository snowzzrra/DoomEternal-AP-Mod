"""Actual bridge coordination using immutable canonical markers and owned epochs."""
import asyncio
from types import SimpleNamespace

import pytest

from test_cross_campaign_save_authority import _create_test_context, _active_bridge_client


def ready_context(monkeypatch, campaign):
    bridge = _active_bridge_client()
    context = _create_test_context()
    events = []
    epoch = "2:100"
    context.runtime_lifecycle.accept_marker(
        {"map_key": "e1m1_intro", "runtime_map": "game/sp/e1m1_intro/e1m1_intro",
         "gameplay_epoch": epoch, "path": "/marker.txt"}, 2,
    )
    context.level_ready.queue(epoch, None)
    context._connected_slot_data = {}
    context.read_active_map_identity = lambda **_: context.cached_map_identity
    context.snapshot_fast_travel_eligibility = lambda: None
    context.runtime_effects_ready = lambda *_: True
    context._refresh_runtime_context = lambda *_: SimpleNamespace(campaign=campaign)
    context.advance_reconciliation_epoch = lambda *_: events.append("rune_epoch") or 7
    context.reconcile_owned_runes = lambda *_: events.append("runes")
    context.advance_automap_cleanup_epoch = lambda: events.append("visual_epoch")
    context.reconcile_checked_automap_cleanup = lambda *_: events.append("visuals")
    context._context_materialize_inventory = lambda *_, **__: (events.append("materialize"), None)
    context.reconcile_fast_travel_unlock = lambda *_: events.append("travel")

    async def settle(_):
        events.append("settle")

    async def challenges():
        events.append("challenges")

    context.check_mission_challenge_locations = challenges
    monkeypatch.setattr(bridge, "asyncio", SimpleNamespace(sleep=settle))
    monkeypatch.setattr(bridge, "read_gameplay_save_evidence", lambda: SimpleNamespace(state="gameplay"))
    monkeypatch.setattr(bridge, "rpc_execution_enabled", lambda: False)
    monkeypatch.setattr(bridge, "set_rpc_execution", lambda _: events.append("arm"))
    return bridge, context, events, epoch


@pytest.mark.parametrize("campaign", ["Base", "TAG2"])
def test_level_ready_retains_effect_order_with_canonical_marker(monkeypatch, campaign):
    _, context, events, epoch = ready_context(monkeypatch, campaign)
    assert asyncio.run(context.process_level_ready())
    expected = ["arm"] + (["settle"] if campaign != "Base" else [])
    assert events == expected + ["rune_epoch", "runes", "visual_epoch", "visuals", "challenges", "materialize", "travel"]
    assert epoch in context.completed_level_ready_epochs
    events.clear()
    assert not asyncio.run(context.process_level_ready())
    assert not events


@pytest.mark.parametrize("phase", ["settle", "challenges"])
@pytest.mark.parametrize("change", ["receipt", "map", "job"])
def test_level_ready_stops_before_effects_after_changed_session_or_load(monkeypatch, phase, change):
    bridge, context, events, epoch = ready_context(monkeypatch, "TAG2")

    async def changed(*_):
        events.append(phase)
        if change == "receipt":
            context.receipt_session.begin_rebind()
        elif change == "job":
            context.level_ready.invalidate()
        else:
            context.runtime_lifecycle.accept_marker(
                {"map_key": "e1m2_war", "runtime_map": "game/sp/e1m2_battle/e1m2_battle",
                 "gameplay_epoch": "3:101"}, 3,
            )

    if phase == "settle":
        bridge.asyncio.sleep = changed
    else:
        context.check_mission_challenge_locations = changed
    assert not asyncio.run(context.process_level_ready())
    assert "materialize" not in events and "travel" not in events
    if phase == "settle":
        assert "runes" not in events
    assert epoch not in context.completed_level_ready_epochs


def test_old_finalizer_cannot_release_replacement_job_and_epoch_lifetimes_remain():
    import logging
    from doom_eap.runtime.level_ready import LevelReady
    owner = LevelReady(logging.getLogger("level-ready"))
    owner.queue("2:100", "first")
    owner.queue("2:100", "later")
    old = owner.start("2:100", None, False, None, None).job
    assert old.source_path == "first" and not owner.can_start("2:100")
    owner.invalidate()
    new = owner.start("2:100", None, False, None, None).job
    owner.finish(old)
    assert owner.is_current(new)
    owner.complete(new)
    owner.finish(new)
    owner.invalidate()
    owner.queue("2:100", "repeat")
    assert not owner.can_start("2:100")
    assert owner.completed == {"2:100"} and not owner.pending
