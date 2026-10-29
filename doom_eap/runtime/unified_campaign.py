"""AP campaign policy; Sentinel Core owns native navigation and loading facts."""
from __future__ import annotations

import json
from pathlib import Path


CAPABILITY = "unified_campaign_v1"
STAGES = {stage["id"]: stage for stage in json.loads(
    (Path(__file__).resolve().parents[2] / "data/campaign_stages.json").read_text(encoding="utf-8")
)}
ACCESS_IDS = {stage["access_id"]: key for key, stage in STAGES.items()}


def validate_plan(slot_data):
    from .context_registry import admit_slot_data

    admit_slot_data(slot_data)
    return slot_data["campaign"]


def _navigation_plan(plan):
    access = {str(item_id): ACCESS_IDS[item_id] for item_id in plan["access_items"]}
    goal = plan["goal"]["stage_id"]
    return {
        "mode": plan["mode"], "sequence": plan["sequence"], "starting_stages": plan["starting_stages"],
        "goal_stage": goal, "goal_as_item": plan["goal_as_item"], "access_items": access,
        "fixed_dash_completion_stage": plan["fixed_dash_completion_stage"],
        "fortress_phase_thresholds": plan["fortress"]["phase_thresholds"],
        "hub": {"id": "hub", "map": "game/hub/hub"},
    }


class UnifiedCampaign:
    def __init__(self, slot_data, session_state, persist):
        self.plan = _navigation_plan(validate_plan(slot_data))
        self.persist = persist
        fingerprint = slot_data["native_generation_fingerprint"]
        saved = session_state.setdefault("campaign_navigation", {
            "generation": fingerprint, "selected": "hub",
        })
        if saved.get("generation") != fingerprint:
            raise ValueError("navigation belongs to another generation")
        self.navigation = saved

    def goal_admitted(self, checked_locations, received_items):
        """Structural goal admission is independent of out-of-logic replay visibility."""
        goal = self.plan["goal_stage"]
        return (goal is None or
                all(STAGES[key]["completion_id"] in checked_locations
                    for key in self.plan["sequence"] if key != goal) or
                (self.plan["goal_as_item"] and any(item.item == STAGES[goal]["access_id"]
                                                  for item in received_items)))

    def snapshot(self, checked_locations, received_items):
        """Derive progression from server checks/items, including out-of-logic checks.

        Replays/retransmissions cannot increment a counter: completion is a set
        of existing location IDs. Local selection never creates progression.
        """
        plan = self.plan
        sequence = plan["sequence"]
        completed = {key for key in sequence if STAGES[key]["completion_id"] in checked_locations}
        revealed = list(plan["starting_stages"])
        unlocked = set(revealed) | completed
        if plan["mode"] == "Mission Access as Items":
            for item in received_items:
                key = plan["access_items"].get(str(item.item))
                if key is not None:
                    unlocked.add(key)
                    if key not in revealed:
                        revealed.append(key)
            for key in sequence:
                if key in completed and key not in revealed:
                    revealed.append(key)
        else:
            for key in sequence:
                if key == plan["goal_stage"]:
                    break
                unlocked.add(key)
                if key not in revealed:
                    revealed.append(key)
                if key not in completed:
                    break
        goal = plan["goal_stage"]
        if goal and all(key in completed for key in sequence if key != goal):
            unlocked.add(goal)
        if goal and goal not in revealed:
            revealed.append(goal)  # Explicit goal identity is the locked-row exception.
        order = (revealed + [key for key in sequence if key not in revealed]
                 if plan["mode"] == "Mission Access as Items" else sequence)
        rows = []
        for key in order:
            visible = key in revealed or key in completed
            stage = STAGES[key]
            rows.append({
                "stage": key, "title": stage["name"] if visible else "???",
                "map": stage["map"] if visible else "",
                "revealed": visible, "unlocked": key in unlocked,
                "completed": key in completed, "goal": key == goal,
                "details_visible": visible and key in unlocked,
            })
        ordinary = [key for key in sequence if key != goal]
        completed_count = sum(key in completed for key in ordinary)
        thresholds = plan["fortress_phase_thresholds"]
        phase = sum(completed_count >= threshold for threshold in thresholds)
        return {"rows": rows, "hub_map": plan["hub"]["map"], "fortress_phase": phase,
                "selected": self.navigation["selected"]}

    def select(self, stage, snapshot):
        if stage != "hub" and not any(row["stage"] == stage and row["unlocked"] for row in snapshot["rows"]):
            raise ValueError("native selection is unavailable in the authoritative projection")
        if self.navigation["selected"] != stage:
            self.navigation["selected"] = stage
            self.persist()
