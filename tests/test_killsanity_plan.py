import pytest

from doom_eap.content.killsanity_plan import (
    KILLSANITY_ADDRESS_BASE, MAXIMUM_MILESTONES, MAXIMUM_THRESHOLD,
    build_killsanity_plan, milestone_names, validate_killsanity_plan,
)


def test_campaign_sized_rewards_keep_stable_addresses_with_unlimited_deaths():
    first = build_killsanity_plan({"enabled": True}, 13)
    assert len(first["milestones"]) == 13
    assert first["milestones"][-1] == {"threshold": 1300, "location_id": KILLSANITY_ADDRESS_BASE + 1300}
    second = build_killsanity_plan({"enabled": True, "interval": 50}, 13)
    assert first["milestones"][0] == second["milestones"][1]
    assert milestone_names(first)["Killed 100 Enemies"] == KILLSANITY_ADDRESS_BASE + 100
    assert not build_killsanity_plan({}, None)["milestones"]
    assert len(build_killsanity_plan({"enabled": True}, 20)["milestones"]) == 20
    options = {"enabled": True, "maximum_milestones": 3}
    plan = build_killsanity_plan(options)
    assert len(plan["milestones"]) == 3
    assert validate_killsanity_plan(options, plan) == plan
    plan["milestones"][0]["threshold"] += 1
    with pytest.raises(ValueError, match="room's settings"):
        validate_killsanity_plan(options, plan)


def test_bounds_reject_invalid_campaigns_and_addresses_without_truncation():
    for options, count in (({"enabled": True}, None),
                           ({"enabled": True}, 21),
                           ({"enabled": True}, True),
                           ({"enabled": True, "maximum_milestones": MAXIMUM_MILESTONES + 1}, 1),
                           ({"enabled": True, "interval": MAXIMUM_THRESHOLD + 1, "maximum_milestones": 1}, 1),
                           ({"enabled": True, "interval": True}, 100),
                           ({"enabled": True, "interval": MAXIMUM_THRESHOLD, "maximum_milestones": 2}, 1),
                           ({"enabled": 1}, 100),
                           ({"enabled": True}, -1)):
        with pytest.raises(ValueError):
            build_killsanity_plan(options, count)
