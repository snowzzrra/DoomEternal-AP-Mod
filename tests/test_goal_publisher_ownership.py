import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from doom_eap.contracts.check_observation import CheckObservation
from doom_eap.contracts.publisher_contracts import PublisherContract
from doom_eap.contracts.save_observation import PrimarySaveSelection
from doom_eap.runtime.check_publication import CheckPublication
from doom_eap.runtime.goal_progress import GoalProgress
from doom_eap.runtime.publisher_dispatch import PublisherDispatch


def owners():
    logger = logging.getLogger("goal-publisher")
    goals = GoalProgress("game/sp/e3m4_boss/e3m4_boss", "cultist", logger)
    state = {}
    goals.bind("room-A", state)
    contract = PublisherContract("test", "map", (), (
        MappingProxyType({"strategy": "location_check", "location_id": 1}),
        MappingProxyType({"strategy": "campaign_goal"}),
    ), "room", "first_success_wins")
    publishers = PublisherDispatch((contract,), goals, logger)
    acknowledgements = {}
    publishers.bind(acknowledgements)
    facts = CheckObservation(frozenset(), frozenset(), frozenset({1}), True, True)
    publishers.observe_protocol(facts)
    return goals, publishers, contract, facts, acknowledgements


def test_submitted_check_is_not_acknowledged_and_endpoint_fact_does_not_send_goal():
    async def run():
        goals, publishers, contract, facts, acknowledgements = owners()
        messages, submitted = [], set()

        async def send(packets):
            messages.extend(packets)

        publication = CheckPublication(send, submitted.add, 30)
        assert not await publishers.execute(contract, "event", "source", publication, lambda: None)
        assert submitted == {1} and acknowledgements == {}
        assert not await publishers.execute(contract, "event", "source", publication, lambda: None)
        assert messages == [{"cmd": "LocationChecks", "locations": [1]}]
        assert not await goals.evaluate("submitted only", frozenset({1}), replace(facts, submitted=frozenset({1})), publication)
        acknowledged = replace(facts, checked=frozenset({1}), submitted=frozenset({1}))
        publishers.observe_protocol(acknowledged)
        assert await publishers.execute(contract, "event", "source", publication, lambda: None)
        assert acknowledgements["test"]["0"] == {"strategy": "location_check", "location_id": 1}
        assert not goals.sent and len(messages) == 1
        assert await goals.evaluate("confirmed endpoint", frozenset({1}), acknowledged, publication)
        assert messages[-1] == {"cmd": "StatusUpdate", "status": 30}
        assert goals.sent
        assert await goals.evaluate("duplicate", frozenset({1}), acknowledged, publication)
        assert len(messages) == 2

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["publisher", "goal"])
@pytest.mark.parametrize("failure", [False, True])
def test_stale_publication_cannot_mark_new_room_or_continue_remaining_effects(kind, failure):
    async def run():
        goals, publishers, contract, facts, _ = owners()
        entered, release = asyncio.Event(), asyncio.Event()
        submitted, packets = [], []

        async def send(messages):
            packets.extend(messages)
            entered.set()
            await release.wait()
            if failure:
                raise OSError("old transport")

        publication = CheckPublication(send, submitted.append, 30)
        if kind == "publisher":
            task = asyncio.create_task(publishers.execute(contract, "event", "source", publication, lambda: None))
        else:
            task = asyncio.create_task(goals.evaluate("source", frozenset({1}), replace(facts, checked=frozenset({1})), publication))
        await asyncio.wait_for(entered.wait(), timeout=5)
        goals.bind("room-B", {})
        new_ack = {}
        publishers.bind(new_ack)
        release.set()
        assert await task is False
        assert submitted == [] and new_ack == {} and not goals.sent
        assert len(packets) == 1

    asyncio.run(run())


def test_delayed_final_sin_candidate_requires_fresh_details_and_old_ack_cannot_clear_new_load():
    goals, _, _, _, _ = owners()
    selected = PrimarySaveSelection("GAME-AUTOSAVE0", Path("game_duration.dat"), 10)
    details = {"_path": "game.details", "_mtime_ns": 10, "completed": "1"}
    goals.arm_candidate(selected, details, "game/sp/e3m4_boss/e3m4_boss", "load-1")
    assert goals.observe_candidate(selected, details) == "pending"
    assert goals.observe_candidate(selected, {**details, "_mtime_ns": 11, "completed": "0"}) == "invalidated"
    goals.arm_candidate(selected, details, "game/sp/e3m4_boss/e3m4_boss", "load-1")
    assert goals.observe_candidate(selected, {**details, "_mtime_ns": 12}) == "publish"
    token = goals.candidate_token()
    goals.arm_candidate(selected, details, "game/sp/e3m4_boss/e3m4_boss", "load-2")
    goals.acknowledge_candidate(token)
    assert goals.candidate["load_epoch"] == "load-2"
    assert not goals.candidate_slot_allowed(True, "GAME-AUTOSAVE1")
    assert goals.candidate is None


def test_completion_fallback_preserves_false_to_true_and_cultist_persistence():
    goals, _, _, _, _ = owners()
    details = {"mapName": "game/sp/e3m4_boss/e3m4_boss", "_path": "details", "_mtime_ns": 1, "completed": "1"}
    assert goals.observe_completion(details, lambda: None) is None
    assert goals.observe_completion({**details, "_mtime_ns": 2, "completed": "0"}, lambda: None) is None
    assert goals.observe_completion({**details, "_mtime_ns": 3}, lambda: None)[0] == "final_sin_mission_complete"
    commits = []
    assert goals.observe_completion({**details, "mapName": "cultist", "_mtime_ns": 4}, lambda: commits.append(goals.cultist_path)) is None
    assert commits == ["details"]
