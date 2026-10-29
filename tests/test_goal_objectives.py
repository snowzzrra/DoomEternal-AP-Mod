"""Victory requirements use the connected world's real location set."""
from doom_eap.contracts.goal_policy import GoalPolicy


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
