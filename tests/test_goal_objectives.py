"""Victory requirements use the connected world's real location set."""
import asyncio
import logging
from unittest.mock import AsyncMock

from doom_eap.contracts.check_observation import CheckObservation
from doom_eap.contracts.goal_policy import GoalPolicy
from doom_eap.runtime.campaign import GoalProgress


def test_resolved_goal_uses_active_dlc_missions_without_dlc_equipment():
    catalog = {1: 'The Dark Lord - Mission Complete', 2: 'The World Spear - Mission Complete'}
    policy = GoalPolicy(catalog, {'The Dark Lord', 'The World Spear'}, {'goal'},
                        {'Kill the Dark Lord': 1}, {'Complete All Included Missions': ' - Mission Complete'})
    room = {'campaign': {'goal': {'kind': 'Kill the Dark Lord', 'stage_id': 'e5m4_boss',
            'endpoint_event': 'Internal Goal Endpoint: Kill the Dark Lord',
            'additional_requirements': ['Complete All Included Missions']}},
            'runtime': {'use_dlc_content': False}}
    assert policy.objective_ids(room, set(catalog)) == {1, 2}


def test_short_world_requirements_exclude_unselected_missions():
    catalog = {
        1: 'Final Sin - Mission Complete',
        2: 'Mars Core - Mission Complete',
        3: 'Mars Core - Slayer Gate Complete',
        4: 'The World Spear - Escalation Encounter Wave 2',
        5: 'Exultia - Mission Complete',
        6: 'Exultia - Slayer Gate Complete',
        7: 'Reclaimed Earth - Escalation Encounter Wave 2',
    }
    suffixes = {
        'Complete All Included Missions': ' - Mission Complete',
        'Complete All Slayer Gates': ' - Slayer Gate Complete',
        'Complete All Escalation Encounters': ' - Escalation Encounter Wave ',
    }
    policy = GoalPolicy(catalog, {'The World Spear', 'Reclaimed Earth'}, {'goal'},
                        {'Kill the Icon of Sin': 1}, suffixes)
    slot = dict(goal='Kill the Icon of Sin',
                goal_endpoint_event='Internal Goal Endpoint: Kill the Icon of Sin',
                goal_endpoint_available=True, required_capabilities=['goal'],
                use_dlc_content=True, include_dlc_missions=True,
                additional_victory_requirements=list(suffixes))
    assert policy.objective_ids(slot, {1, 2, 3, 4}) == {1, 2, 3, 4}
    assert policy.objective_ids(slot, set(catalog)) == set(catalog)
    assert policy.objective_ids(slot, set()) == {1}
    slot['goal_endpoint_available'] = False
    assert not policy.objective_ids(slot, set(catalog))


def test_master_level_threshold_counts_distinct_checked_completions():
    stages = [{"id": f"ml_{key}", "completion_id": code} for key, code in (("a", 11), ("b", 12), ("c", 13))]
    policy = GoalPolicy({11: "A - Mission Complete", 12: "B - Mission Complete", 13: "C - Mission Complete",
                         21: "Fortress - Unmaykr Acquired"}, (), {"goal"}, {"Acquire the Unmaykr": 21}, (), stages)
    room = {"campaign": {"master_level_stage_ids": [stage["id"] for stage in stages],
            "goal": {"kind": "Complete N Master Levels", "count": 2, "stage_id": None,
                     "endpoint_event": "Internal Goal Endpoint: Complete N Master Levels",
                     "additional_requirements": ["Acquire the Unmaykr"]}}}
    objectives = policy.objective_ids(room, {11, 12, 13, 21})
    candidates, count = policy.threshold(room)
    progress = GoalProgress("", "", logging.getLogger(__name__))
    publication = AsyncMock()

    async def evaluate(checked, submitted=()):
        facts = CheckObservation(frozenset(checked), frozenset(submitted), objectives, True, True)
        return await progress.evaluate("room update", objectives, facts, publication,
                                       threshold_ids=candidates, threshold=count)

    assert not asyncio.run(evaluate([11, 11, 21], [12]))
    assert not asyncio.run(evaluate([11, 12]))
    assert asyncio.run(evaluate([11, 12, 21]))
    assert asyncio.run(evaluate([11, 12, 21]))
    publication.goal.assert_awaited_once()
    room["campaign"]["goal"]["additional_requirements"] = ["Complete all Master Levels"]
    assert policy.threshold(room) == (candidates, 3)
    room["campaign"]["goal"].update(kind="Acquire the Unmaykr", endpoint_event="Internal Goal Endpoint: Acquire the Unmaykr")
    assert policy.objective_ids(room, {11, 12, 13, 21}) == {11, 12, 13, 21}
    assert policy.threshold(room) == (frozenset(), 0)
