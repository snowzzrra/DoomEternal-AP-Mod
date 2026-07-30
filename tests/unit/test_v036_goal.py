"""Independent v0.3.6 release-boundary expectations."""

from __future__ import annotations

import json
from pathlib import Path

from content_catalog import load_content_catalog


ROOT = Path(__file__).parents[2]


def test_v036_goal_and_public_locations_are_independent() -> None:
    catalog = load_content_catalog()
    public = list(catalog.physical_locations) + list(catalog.runtime_locations)
    public_ids = [location.location_id for location in public]
    assert len(public_ids) == 339
    assert len(public_ids) == len(set(public_ids))
    assert set(range(7770340, 7770388)) <= set(public_ids)
    assert catalog.location_names[7770337] == "Taras Nabad - Mission Complete"
    assert catalog.location_names[7770387] == "Nekravol Part II - Mission Complete"

    goals = [
        publisher for publisher in catalog.publishers
        if any(effect["strategy"] == "campaign_goal" for effect in publisher.effects)
    ]
    assert len(goals) == 1
    goal = goals[0]
    assert goal.key == "nekravol_part_ii_mission_complete"
    assert goal.map_key == "e3m2_hell_b"
    assert [dict(effect) for effect in goal.effects] == [
        {"strategy": "location_check", "location_id": 7770387},
        {"strategy": "campaign_goal"},
    ]
    taras_goal = next(
        publisher for publisher in catalog.publishers
        if publisher.key == "taras_nabad_campaign_goal"
    )
    assert all(
        effect["strategy"] != "campaign_goal"
        for effect in taras_goal.effects
    )
    assert (
        "Nekravol",
        "Nekravol Part II",
        "Continue to Nekravol Part II",
    ) in catalog.route["connections"]

    contract = json.loads(
        (ROOT / "data" / "campaign_goal_contract.json").read_text(encoding="utf-8")
    )
    event = goal.triggers_for("map_event_file")[0]
    assert contract["publisher_key"] == goal.key
    assert contract["event_filename"] == event["filename"]
    assert contract["marker"] == event["marker"]
    assert contract["runtime_map"] == "game/sp/e3m2_hell_b/e3m2_hell_b"
