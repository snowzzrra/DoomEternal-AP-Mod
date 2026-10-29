from doom_eap.content.killsanity_plan import build_campaign_kill_budget, build_killsanity_plan


def sources(count):
    return [{"source_id": "intro/finite", "map_key": "e1m1_intro", "count": count, "repeatable": True}]


def test_producer_reward_addresses_use_ordinals():
    first = build_killsanity_plan({"enabled": True, "interval": 100}, sources(200))
    second = build_killsanity_plan({"enabled": True, "interval": 10, "maximum_milestones": 2}, sources(200))
    assert [row["location_id"] for row in first["milestones"]] == [2_200_000_001, 2_200_000_002]
    assert [row["location_id"] for row in second["milestones"]] == [2_200_000_001, 2_200_000_002]


def native_plan(manager, script, event, logical, physical):
    return {"kind": "native_recipes", "maps": {"e1m1_intro": {"source_sha256": "a" * 64,
        "recipes": [{"logical_count": logical, "physical_count": physical, "binding": {
            "mode": "event", "entity": manager,
            "path": f"encounterComponent.entityEvents.item[{script}].events.item[{event}].eventCall"}}]}}}


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
