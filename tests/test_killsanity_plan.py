import pytest

from doom_eap.content.killsanity_plan import (
    KILLSANITY_ADDRESS_BASE, MAXIMUM_MILESTONES, MAXIMUM_THRESHOLD,
    build_campaign_kill_budget, build_killsanity_plan, milestone_names, validate_killsanity_plan,
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


def test_auto_caps_the_estimate_and_keeps_progression_inside_the_guaranteed_budget():
    options = dict(enabled=True, interval=100)
    plan = build_killsanity_plan(options, sources(200), estimated_kills=100000)
    assert plan['estimated_kills'] == 100000 and plan['finite_kills'] == 200
    assert len(plan['milestones']) == 50
    assert sum(row['finite_supported'] for row in plan['milestones']) == 2
    assert validate_killsanity_plan(options, plan) == plan
    smaller = build_killsanity_plan(options, sources(200), estimated_kills=1000)
    assert len(smaller['milestones']) == 11
    manual = build_killsanity_plan({**options, 'maximum_milestones': 75}, sources(200), estimated_kills=1000)
    assert len(manual['milestones']) == 75
    unknown = build_killsanity_plan(options, [], estimated_kills=1000)
    assert len(unknown['milestones']) == 11 and not any(row['finite_supported'] for row in unknown['milestones'])
    with pytest.raises(ValueError, match='guaranteed sources'):
        build_killsanity_plan(options, sources(200), estimated_kills=100)


def test_distinct_sources_are_summed_once_and_order_does_not_change_the_plan():
    entries = sources(250) + [{"source_id": "exultia/finite", "map_key": "e1m2_battle",
                               "count": 750, "repeatable": True}]
    first = build_killsanity_plan({"enabled": True}, entries)
    assert first["finite_kills"] == 1000
    assert first == build_killsanity_plan({"enabled": True}, entries[::-1])
    with pytest.raises(ValueError, match="twice"):
        build_killsanity_plan({"enabled": True}, entries + entries[:1])


def test_frozen_sources_preserve_their_fight_and_kit():
    entries = sources(250)
    entries[0].update(location="Exultia - Slayer Gate Complete", required_items=["Plasma Rifle", "Microwave Beam"])
    options = {"enabled": True}
    plan = build_killsanity_plan(options, entries)
    assert plan["finite_sources"][0]["location"] == entries[0]["location"]
    assert plan["finite_sources"][0]["required_items"] == ["Microwave Beam", "Plasma Rifle"]
    assert validate_killsanity_plan(options, plan) == plan
    entries[0]["required_items"].append("Plasma Rifle")
    with pytest.raises(ValueError, match="required items"):
        build_killsanity_plan(options, entries)
    entries[0]["required_items"] = []
    entries[0]["region"] = "Exultia - Hell - Ichor Expanse"
    with pytest.raises(ValueError, match="one access point"):
        build_killsanity_plan(options, entries)


def native_plan(manager, script, event, logical, physical):
    return {"kind": "native_recipes", "maps": {"e1m1_intro": {"source_sha256": "a" * 64,
        "recipes": [{"logical_count": logical, "physical_count": physical, "binding": {
            "mode": "event", "entity": manager,
            "path": f"encounterComponent.entityEvents.item[{script}].events.item[{event}].eventCall"}}]}}}


def test_campaign_budget_replaces_original_bodies_and_keeps_unchanged_combat():
    original = [{**sources(100)[0], "source_id": "arena/0/2", "region": "Hell on Earth - Subway"},
                {**sources(200)[0], "source_id": "unchanged/0/1"}]
    catalog = {"schema_version": 2, "maps": {"e1m1_intro": {
        "estimated_kills": 400, "source_sha256": "a" * 64, "sources": original}}}
    campaign = {"stage_ids": ["e1m1_intro"]}
    plan = native_plan("arena", 0, 2, 2, 1)
    result = build_campaign_kill_budget(campaign, plan, catalog=catalog)
    assert result["estimated_kills"] == 350
    assert {row["source_id"]: row["count"] for row in result["finite_sources"]} == {
        "arena/0/2": 50, "unchanged/0/1": 200}
    assert result["finite_sources"][0]["region"] == original[0]["region"]
    assert original[0]["count"] == 100
    plan["maps"]["e1m1_intro"]["source_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="different maps"):
        build_campaign_kill_budget(campaign, plan, catalog=catalog)
    catalog["maps"]["e1m1_intro"]["estimated_kills"] = None
    with pytest.raises(ValueError, match="estimate is missing"):
        build_campaign_kill_budget(campaign, None, catalog=catalog)


def test_checkpoint_alternatives_use_one_guaranteed_cohort_with_the_same_access():
    from doom_eap.content.killsanity_plan import collapse_source_alternatives

    entries = {key: {**sources(count)[0], 'source_id': key} for key, count in [('fresh', 3), ('checkpoint', 4), ('other', 7)]}
    groups = [dict(source_id='initial', branches=[['fresh'], ['checkpoint']])]
    result = collapse_source_alternatives(entries, groups)
    assert {key: row['count'] for key, row in result.items()} == {'initial': 3, 'other': 7}
    assert set(entries) == {'fresh', 'checkpoint', 'other'}
    groups[0]['branches'].append(['fresh'])
    with pytest.raises(ValueError, match='missing or repeated'):
        collapse_source_alternatives(entries, groups)
    groups[0]['branches'].pop()
    entries['checkpoint']['required_items'] = ['Dash']
    with pytest.raises(ValueError, match='different access'):
        collapse_source_alternatives(entries, groups)


def test_conditional_checkpoint_scripts_do_not_inflate_progression_or_auto():
    entries = [{**sources(200)[0], 'source_id': 'normal/0/1'},
               {**sources(500)[0], 'source_id': 'checkpoint/0/2'},
               {**sources(500)[0], 'source_id': 'checkpoint/1/2'}]
    catalog = {'schema_version': 2, 'maps': {'e1m1_intro': {
        'estimated_kills': 300, 'source_sha256': 'a' * 64,
        'sources': entries, 'conditional_managers': ['checkpoint']}}}
    campaign = {'stage_ids': ['e1m1_intro']}
    plan = native_plan("checkpoint", 1, 2, 1, 3)
    original = build_campaign_kill_budget(campaign, None, catalog=catalog)
    generated = build_campaign_kill_budget(campaign, plan, catalog=catalog)
    assert original == generated == {'estimated_kills': 300, 'finite_sources': [entries[0]]}


def test_generated_optional_fights_need_reviewed_access_before_holding_progression():
    entries = [{**sources(200)[0], 'source_id': 'normal/0/1'},
               {**sources(100)[0], 'source_id': 'optional/0/2'}]
    catalog = {'schema_version': 2, 'maps': {'e1m1_intro': {
        'estimated_kills': 400, 'source_sha256': 'a' * 64, 'sources': entries,
        'progression_source_ids': ['normal/0/1']}}}
    campaign = {'stage_ids': ['e1m1_intro']}
    plan = native_plan("optional", 0, 2, 2, 3)
    result = build_campaign_kill_budget(campaign, plan, catalog=catalog)
    assert result == {'estimated_kills': 450, 'finite_sources': [entries[0]]}
    catalog['maps']['e1m1_intro']['progression_source_ids'].append('missing')
    with pytest.raises(ValueError, match='guaranteed campaign source'):
        build_campaign_kill_budget(campaign, plan, catalog=catalog)


def test_campaign_alternatives_use_the_smaller_final_branch():
    entries = [{**sources(count)[0], 'source_id': name} for name, count in
               [('normal', 200), ('cohort', 2), ('fresh/0/0', 1), ('fresh/0/1', 1), ('checkpoint/0/0', 3)]]
    catalog = {'schema_version': 2, 'maps': {'e1m1_intro': {
        'estimated_kills': 300, 'source_sha256': 'a' * 64, 'sources': entries,
        'progression_source_ids': ['normal', 'cohort'],
        'source_alternatives': [{'source_id': 'cohort', 'branches': [['fresh/0/0', 'fresh/0/1'], ['checkpoint/0/0']]}]}}}
    campaign = {'stage_ids': ['e1m1_intro']}
    plan = native_plan("fresh", 0, 0, 1, 10)
    recipe = plan["maps"]["e1m1_intro"]["recipes"][0]
    result = build_campaign_kill_budget(campaign, plan, catalog=catalog)
    assert result['estimated_kills'] == 301
    assert {row['source_id']: row['count'] for row in result['finite_sources']} == {'normal': 200, 'cohort': 3}
    recipe['physical_count'] = 0
    result = build_campaign_kill_budget(campaign, plan, catalog=catalog)
    assert result['estimated_kills'] == 299
    assert {row['source_id']: row['count'] for row in result['finite_sources']} == {'normal': 200, 'cohort': 1}
    assert entries[1]['count'] == 2 and catalog['maps']['e1m1_intro']['source_alternatives'][0]['branches'][0] == ['fresh/0/0', 'fresh/0/1']


@pytest.mark.parametrize('physical, expected', [(0, 0), (1, 3), (2, 5), (3, 8)])
def test_native_event_scales_once_and_ignores_dynamic_banks_and_compatibility(physical, expected):
    from copy import deepcopy

    entry = {**sources(5)[0], 'source_id': 'arena/0/2'}
    catalog = {'schema_version': 2, 'maps': {'e1m1_intro': {
        'estimated_kills': 105, 'source_sha256': 'a' * 64, 'sources': [entry]}}}
    recipe = {'logical_count': 2, 'physical_count': physical, 'binding': {
        'mode': 'event', 'entity': 'arena',
        'path': 'encounterComponent.entityEvents.item[0].events.item[2].eventCall'}}
    plan = {'kind': 'native_recipes', 'maps': {'e1m1_intro': {
        'source_sha256': 'a' * 64, 'recipes': [recipe,
            {**recipe, 'runtime_only': True, 'physical_count': 100000, 'binding': None},
            {**recipe, 'physical_count': 100000, 'binding': {**recipe['binding'], 'entity': 'unreviewed'}}],
        'operations': [{'manager': {'entity': 'arena'}, 'script_index': 0, 'event_index': 2,
                        'waves': [{'instances': [{}] * 999}]}]}}}
    campaign = {'stage_ids': ['e1m1_intro']}
    original = deepcopy(catalog)
    budget = build_campaign_kill_budget(campaign, plan, catalog=catalog)
    assert budget['estimated_kills'] == 100 + expected
    assert sum(row['count'] for row in budget['finite_sources']) == expected
    assert catalog == original
    assert build_campaign_kill_budget(campaign, None, catalog=catalog)['estimated_kills'] == 105
    assert build_campaign_kill_budget(campaign, {'kind': 'native_recipes', 'maps': {}}, catalog=catalog) == {
        'estimated_kills': 105, 'finite_sources': [entry]}


def test_host_and_spirit_share_the_original_event_quantity():
    entry = {**sources(2)[0], 'source_id': 'possessed/0/2'}
    catalog = {'schema_version': 2, 'maps': {'e1m1_intro': {
        'estimated_kills': 2, 'source_sha256': 'a' * 64, 'sources': [entry]}}}
    binding = {'mode': 'event', 'entity': 'possessed',
               'path': 'encounterComponent.entityEvents.item[0].events.item[2].eventCall'}
    recipes = [{'binding': binding, 'logical_count': 1, 'physical_count': count} for count in (2, 1)]
    plan = {'kind': 'native_recipes', 'maps': {'e1m1_intro': {
        'source_sha256': 'a' * 64, 'recipes': recipes}}}
    result = build_campaign_kill_budget({'stage_ids': ['e1m1_intro']}, plan, catalog=catalog)
    assert result == {'estimated_kills': 3, 'finite_sources': [{**entry, 'count': 3}]}


@pytest.mark.parametrize('estimate, interval, expected', [
    (90, 100, 0), (91, 100, 1), (181, 100, 1), (182, 100, 2),
    (1000, 50, 22), (4545, 100, 49), (4546, 100, 50), (100000, 100, 50),
    (1, 2147483647, 0),
])
def test_auto_rounds_down_after_slack_and_interval(estimate, interval, expected):
    plan = build_killsanity_plan({'enabled': True, 'interval': interval}, [], estimated_kills=estimate)
    assert len(plan['milestones']) == expected
    assert all(not row['finite_supported'] for row in plan['milestones'])


def test_explicit_maximum_preserves_all_rewards_and_rejects_address_overflow():
    plan = build_killsanity_plan({'enabled': True, 'interval': 1, 'maximum_milestones': 65535}, [], estimated_kills=0)
    assert len(plan['milestones']) == 65535
    assert plan['milestones'][-1]['threshold'] == 65535
    assert not build_killsanity_plan({'enabled': False, 'maximum_milestones': 65535})['milestones']
    with pytest.raises(ValueError, match='final threshold'):
        build_killsanity_plan({'enabled': True, 'interval': 2147483647, 'maximum_milestones': 1}, [], estimated_kills=0)


@pytest.mark.parametrize("options, entries", [
    ({"enabled": True}, None), ({"enabled": True}, 13), ({"enabled": True}, True),
    ({"enabled": True}, sources(0)), ({"enabled": True}, sources(-1)),
    ({"enabled": True}, sources(True)),
    ({"enabled": True, "maximum_milestones": MAXIMUM_MILESTONES + 1}, sources(1)),
    ({"enabled": True, "interval": MAXIMUM_THRESHOLD + 1, "maximum_milestones": 1}, sources(1)),
    ({"enabled": True, "interval": True}, sources(100)),
    ({"enabled": True, "interval": MAXIMUM_THRESHOLD, "maximum_milestones": 2}, sources(1)),
    ({"enabled": 1}, sources(100)),
])
def test_bounds_reject_invalid_sources_and_addresses_without_truncation(options, entries):
    with pytest.raises(ValueError):
        build_killsanity_plan(options, entries)
