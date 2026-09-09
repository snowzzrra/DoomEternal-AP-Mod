import asyncio

from doom_eap.runtime.task_supervision import SessionTasks
from doom_eap.runtime.lifecycle import RuntimeLifecycle
from test_cross_campaign_save_authority import _create_test_context, _active_bridge_client


def test_session_jobs_cancel_before_entry_and_during_wait_then_close():
    async def run():
        jobs = SessionTasks()
        effects = []
        entered = asyncio.Event()

        async def work():
            effects.append("entered")
            entered.set()
            await asyncio.Event().wait()
            effects.append("late")

        retired = jobs.start(work)
        jobs.invalidate()
        await asyncio.gather(retired, return_exceptions=True)
        assert effects == []
        active = jobs.start(work)
        await asyncio.wait_for(entered.wait(), 1)
        await jobs.close()
        assert active.cancelled() and effects == ["entered"]

    asyncio.run(run())


def test_work_tokens_distinguish_disconnect_and_same_map_reload():
    lifecycle = RuntimeLifecycle()
    marker = {"runtime_map": "map", "gameplay_epoch": "1:100"}
    lifecycle.accept_marker(marker, 1)
    token = lifecycle.capture_work()
    lifecycle.accept_marker({**marker, "gameplay_epoch": "2:100"}, 2)
    assert not lifecycle.work_is_current(token)
    token = lifecycle.capture_work()
    lifecycle.invalidate_work()
    assert not lifecycle.work_is_current(token)


def test_death_monitor_does_not_continue_old_iteration_after_rebind(monkeypatch):
    bridge = _active_bridge_client()
    context = _create_test_context()
    context.exit_event = asyncio.Event()
    calls = []
    context.check_rpc_autopause = lambda: None
    context.queue_received_deathlink = lambda: None
    monkeypatch.setattr(bridge, "death_probe_available", lambda: True)

    async def probe():
        calls.append("probe")
        if len(calls) == 1:
            context.runtime_lifecycle.invalidate_work()
        else:
            context.exit_event.set()
        return False

    async def forbidden():
        raise AssertionError("old monitor iteration continued after retirement")

    context.check_game_duration_death = probe
    context.check_game_details_death = forbidden
    context.check_weapon_mastery_locations = forbidden
    context.check_mission_challenge_locations = forbidden
    context.check_campaign_goal = forbidden
    asyncio.run(context.death_monitor_loop())
    assert calls == ["probe", "probe"]
