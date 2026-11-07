import asyncio
import logging
from types import SimpleNamespace

import pytest

from doom_eap.contracts.check_observation import CheckObservation
from doom_eap.runtime.bridge_checks import CheckPublication
from doom_eap.runtime.campaign import GoalProgress


@pytest.mark.parametrize("admitted,ready,connected,objectives,checked", [
    (None, True, True, {1}, {1}),
    (False, True, True, {1}, {1}),
    (True, False, True, {1}, {1}),
    (True, True, True, set(), set()),
    (True, True, True, {1, 2}, {1}),
    (True, True, False, {1}, {1}),
])
def test_unadmitted_or_unready_goal_cannot_publish(admitted, ready, connected, objectives, checked):
    goals = GoalProgress("final", "cultist", logging.getLogger("goal-diagnostics"))
    facts = CheckObservation(frozenset(checked), frozenset(), frozenset(objectives), ready, connected)

    async def send(packets):
        pytest.fail(f"blocked goal published {packets}")

    assert not asyncio.run(goals.evaluate(
        "server checked update", frozenset(objectives), facts, CheckPublication(send, set().add, 30),
        admitted=admitted, admission_missing=("e1m2_war",)))


def test_goal_send_error_is_diagnosed_and_can_retry(caplog):
    goals = GoalProgress("final", "cultist", logging.getLogger("goal-diagnostics"))
    facts = CheckObservation(frozenset({1}), frozenset(), frozenset({1}), True, True)
    attempts = []

    async def send(messages):
        attempts.append(messages)
        if len(attempts) == 1:
            raise OSError("private transport details")

    async def run():
        publication = CheckPublication(send, set().add, 30)
        with pytest.raises(OSError):
            await goals.evaluate("poll", frozenset({1}), facts, publication)
        assert not goals.sent
        assert await goals.evaluate("retry", frozenset({1}), facts, publication)

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    assert len(attempts) == 2
    assert any("result=error error_type=OSError current=True" in message for message in caplog.messages)
    assert "private transport details" not in caplog.text


def test_campaign_goal_reports_admission_before_publishing_server_progress(bridge, caplog):
    from doom_eap.runtime import bridge_checks, bridge_client

    campaign = bridge_client.UnifiedCampaign.__new__(bridge_client.UnifiedCampaign)
    campaign.plan = {"sequence": ["e1m2_war", "e5m4_boss"], "goal_stage": "e5m4_boss", "goal_as_item": False}
    objective = bridge_checks.STAGES["e5m4_boss"]["completion_id"]
    packets = []

    async def send(messages):
        packets.extend(messages)

    ctx = SimpleNamespace(
        state_key="goal-room",
        _connected_slot_data={"campaign": {"goal": {
            "kind": "Kill the Dark Lord", "stage_id": "e5m4_boss",
            "endpoint_event": "Internal Goal Endpoint: Kill the Dark Lord", "additional_requirements": [],
        }}},
        checked_locations={objective},
        items_received=[],
        server_checked_locations_ready=True,
        locations_checked=set(),
        server_locations={objective},
        server=SimpleNamespace(socket=SimpleNamespace(closed=False)),
        send_msgs=send,
        goals=GoalProgress("final", "cultist", logging.getLogger("goal-diagnostics")),
        items=None,
        checks=None,
        saves=None
    )
    ctx.items = bridge.bridge_items.ItemRuntime(ctx)
    ctx.checks = bridge.bridge_checks.CheckRuntime(ctx)
    ctx.saves = bridge.bridge_saves.SaveRuntime(ctx)




    async def run():
        assert not await ctx.checks.evaluate_campaign_goal("connected")
        ctx.unified_campaign = campaign
        assert not await ctx.checks.evaluate_campaign_goal("server checked update")
        assert packets == []
        ctx.checked_locations.add(bridge_checks.STAGES["e1m2_war"]["completion_id"])
        assert await ctx.checks.evaluate_campaign_goal("server checked update")
        assert await ctx.checks.evaluate_campaign_goal("physical defeat")

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    from NetUtils import ClientStatus

    assert packets == [{"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}]
    assert any("decision=campaign_unavailable" in message for message in caplog.messages)
    assert any("admission_missing=['e1m2_war']" in message for message in caplog.messages)
