import pytest

from doom_eap.content.killsanity_plan import (
    KILLSANITY_ADDRESS_BASE, MAXIMUM_MILESTONES, MAXIMUM_THRESHOLD,
    build_killsanity_plan, milestone_names, validate_killsanity_plan,
)


def sources(count):
    return [{"source_id": "intro/finite", "map_key": "e1m1_intro", "count": count, "repeatable": True}]


def test_auto_uses_finite_kills_and_fixed_slack_with_stable_addresses():
    first = build_killsanity_plan({"enabled": True}, sources(1000))
    assert len(first["milestones"]) == 11
    assert first["finite_kills"] == 1000
    assert first["milestones"][-1] == {"threshold": 1100, "location_id": KILLSANITY_ADDRESS_BASE + 1100,
                                        "finite_supported": False}
    assert all(row["finite_supported"] for row in first["milestones"][:-1])
    second = build_killsanity_plan({"enabled": True, "interval": 50}, sources(1000))
    assert len(second["milestones"]) == 22
    assert first["milestones"][0] == second["milestones"][1]
    assert milestone_names(first)["Killed 100 Enemies"] == KILLSANITY_ADDRESS_BASE + 100
    assert not build_killsanity_plan({})["milestones"]
    assert not build_killsanity_plan({"enabled": True}, sources(90))["milestones"]
    assert len(build_killsanity_plan({"enabled": True}, sources(91))["milestones"]) == 1


def test_manual_maximum_and_frozen_budget_validation():
    options = {"enabled": True, "maximum_milestones": 3}
    plan = build_killsanity_plan(options, sources(200))
    assert len(plan["milestones"]) == 3
    assert [row["finite_supported"] for row in plan["milestones"]] == [True, True, False]
    assert validate_killsanity_plan(options, plan) == plan
    plan["milestones"][0]["threshold"] += 1
    with pytest.raises(ValueError, match="room's settings"):
        validate_killsanity_plan(options, plan)


def test_distinct_sources_are_summed_once_and_order_does_not_change_the_plan():
    entries = sources(250) + [{"source_id": "exultia/finite", "map_key": "e1m2_battle",
                               "count": 750, "repeatable": True}]
    first = build_killsanity_plan({"enabled": True}, entries)
    assert first["finite_kills"] == 1000
    assert first == build_killsanity_plan({"enabled": True}, entries[::-1])
    with pytest.raises(ValueError, match="twice"):
        build_killsanity_plan({"enabled": True}, entries + entries[:1])


@pytest.mark.parametrize("options, entries", [
    ({"enabled": True}, None), ({"enabled": True}, 13), ({"enabled": True}, True),
    ({"enabled": True}, sources(0)), ({"enabled": True}, sources(-1)),
    ({"enabled": True}, sources(True)),
    ({"enabled": True, "maximum_milestones": MAXIMUM_MILESTONES + 1}, sources(1)),
    ({"enabled": True, "interval": MAXIMUM_THRESHOLD + 1, "maximum_milestones": 1}, sources(1)),
    ({"enabled": True, "interval": True}, sources(100)),
    ({"enabled": True, "interval": MAXIMUM_THRESHOLD, "maximum_milestones": 2}, sources(1)),
    ({"enabled": 1}, sources(100)),
    ({"enabled": True, "interval": 1}, sources(MAXIMUM_MILESTONES * 100)),
])
def test_bounds_reject_invalid_sources_and_addresses_without_truncation(options, entries):
    with pytest.raises(ValueError):
        build_killsanity_plan(options, entries)
