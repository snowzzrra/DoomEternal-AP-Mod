"""Victory requirements use the connected world's real location set."""
from doom_eap.contracts.goal_policy import GoalPolicy


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
