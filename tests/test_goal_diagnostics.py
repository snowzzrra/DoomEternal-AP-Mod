import asyncio
import importlib
import json
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from doom_eap.contracts.check_observation import CheckObservation
from doom_eap.runtime.check_publication import CheckPublication
from doom_eap.runtime.goal_progress import GoalProgress


@pytest.mark.parametrize("admitted,ready,connected,objectives,checked,decision", [
    (None, True, True, {1}, {1}, "campaign_unavailable"),
    (False, True, True, {1}, {1}, "campaign_not_admitted"),
    (True, False, True, {1}, {1}, "checked_not_ready"),
    (True, True, True, set(), set(), "objectives_unavailable"),
    (True, True, True, {1, 2}, {1}, "missing_objectives"),
    (True, True, False, {1}, {1}, "disconnected"),
])
def test_goal_block_diagnostics_include_inputs_and_do_not_publish(
        caplog, admitted, ready, connected, objectives, checked, decision):
    goals = GoalProgress("final", "cultist", logging.getLogger("goal-diagnostics"))
    facts = CheckObservation(frozenset(checked), frozenset(), frozenset(objectives), ready, connected)

    async def send(packets):
        pytest.fail(f"blocked goal published {packets}")

    with caplog.at_level(logging.INFO):
        assert not asyncio.run(goals.evaluate(
            "server checked update", frozenset(objectives), facts, CheckPublication(send, set().add, 30),
            admitted=admitted, admission_missing=("e1m2_war",)))
    record = next(message for message in caplog.messages if "EVALUATION" in message)
    assert f"decision={decision}" in record
    assert f"admitted={admitted}" in record
    assert f"checked_ready={ready} connected={connected}" in record
    assert "admission_missing=['e1m2_war']" in record
    assert f"objective_count={len(objectives)}" in record
    assert f"missing_count={len(objectives - checked)}" in record


def test_goal_diagnostics_coalesce_repeated_evaluations_and_bound_lists(caplog, monkeypatch):
    goals = GoalProgress("final", "cultist", logging.getLogger("goal-diagnostics"))
    objectives = frozenset(range(100))
    facts = CheckObservation(frozenset(), frozenset(), objectives, True, True)
    clock = [100.0]
    monkeypatch.setattr("doom_eap.runtime.goal_progress.monotonic", lambda: clock[0])

    async def run():
        for _ in range(50):
            assert not await goals.evaluate("poll", objectives, facts, None)
        clock[0] += 30
        assert not await goals.evaluate("poll", objectives, facts, None)
        assert not await goals.evaluate("changed", objectives, replace(facts, connected=False), None)

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    records = [message for message in caplog.messages if "EVALUATION" in message]
    assert len(records) == 3
    assert "evaluations=50" in records[1]
    assert "connected=False" in records[2]
    assert "missing_count=100" in records[0]
    assert "31]" in records[0] and "32," not in records[0]


def test_collected_objective_and_duplicate_physical_event_publish_one_goal(caplog):
    goals = GoalProgress("final", "cultist", logging.getLogger("goal-diagnostics"))
    facts = CheckObservation(frozenset({1}), frozenset(), frozenset({1}), True, True)
    packets = []

    async def send(messages):
        packets.extend(messages)

    async def run():
        publication = CheckPublication(send, set().add, 30)
        assert await goals.evaluate("server checked update", frozenset({1}), facts, publication)
        assert await goals.evaluate("physical defeat", frozenset({1}), facts, publication)
        assert await goals.evaluate("poll", frozenset({1}), facts, publication)

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    assert packets == [{"cmd": "StatusUpdate", "status": 30}]
    assert any("CLIENT_GOAL_RESULT result=returned" in message for message in caplog.messages)
    assert any("decision=already_sent" in message for message in caplog.messages)


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


def test_campaign_goal_reports_admission_before_publishing_server_progress(tmp_path, monkeypatch, caplog):
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "doom_base_dir": str(tmp_path / "base"), "save_games_dir": str(tmp_path),
        "client_state_file": str(tmp_path / "state.json"), "bridge_log_path": str(tmp_path / "bridge.log"),
    }))
    monkeypatch.setenv("DOOM_AP_CONFIG_FILE", str(config))
    monkeypatch.setenv("DOOM_AP_APPLICATION_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(root.parent / "Archipelago"))
    monkeypatch.syspath_prepend(str(root / "packaging/standalone_runtime"))
    bridge = importlib.import_module("doom_eap.runtime.bridge_client")
    campaign = bridge.UnifiedCampaign.__new__(bridge.UnifiedCampaign)
    campaign.plan = {"sequence": ["e1m2_war", "e5m4_boss"], "goal_stage": "e5m4_boss", "goal_as_item": False}
    objective = bridge.STAGES["e5m4_boss"]["completion_id"]
    monkeypatch.setattr(bridge, "GOAL_POLICY", SimpleNamespace(objective_ids=lambda *_: frozenset({objective})))
    packets = []

    async def send(messages):
        packets.extend(messages)

    ctx = SimpleNamespace(
        checked_locations={objective}, items_received=[], server_checked_locations_ready=True,
        locations_checked=set(), server_locations={objective},
        server=SimpleNamespace(socket=SimpleNamespace(closed=False)), send_msgs=send,
        goals=GoalProgress("final", "cultist", logging.getLogger("goal-diagnostics")),
    )

    async def run():
        assert not await bridge.DoomEternalContext.evaluate_campaign_goal(ctx, "connected")
        ctx.unified_campaign = campaign
        assert not await bridge.DoomEternalContext.evaluate_campaign_goal(ctx, "server checked update")
        assert packets == []
        ctx.checked_locations.add(bridge.STAGES["e1m2_war"]["completion_id"])
        assert await bridge.DoomEternalContext.evaluate_campaign_goal(ctx, "server checked update")
        assert await bridge.DoomEternalContext.evaluate_campaign_goal(ctx, "physical defeat")

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    assert packets == [{"cmd": "StatusUpdate", "status": bridge.ClientStatus.CLIENT_GOAL}]
    assert any("decision=campaign_unavailable" in message for message in caplog.messages)
    assert any("admission_missing=['e1m2_war']" in message for message in caplog.messages)
