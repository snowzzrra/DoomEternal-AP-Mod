"""AP campaign policy; Sentinel Core owns native navigation and loading facts."""
from __future__ import annotations

import json
from pathlib import Path

CAPABILITY = "unified_campaign_v1"
STAGES = {stage["id"]: stage for stage in json.loads(
    (Path(__file__).resolve().parents[2] / "data/campaign_stages.json").read_text(encoding="utf-8")
)}
ACCESS_IDS = {stage["access_id"]: key for key, stage in STAGES.items()}
MODES = {"Vanilla Order", "Random Mission Order", "Mission Access as Items"}


def validate_plan(slot_data):
    if CAPABILITY not in slot_data.get("required_capabilities", ()):
        raise ValueError("unified_campaign_v1 is required; generate a Phase6 seed")
    plan = slot_data.get("campaign_plan")
    if not isinstance(plan, dict) or plan.get("schema") != 1 or plan.get("mode") not in MODES:
        raise ValueError("unsupported unified campaign plan")
    if type(plan.get("difficulty")) is not int or not 0 <= plan["difficulty"] <= 3:
        raise ValueError("campaign difficulty must be fixed by the generated room")
    sequence, starts, goal = plan.get("sequence"), plan.get("starting_stages"), plan.get("goal_stage")
    enabled = {key for key, stage in STAGES.items()
               if slot_data["include_dlc_missions"] or stage["source"] == "base"}
    if (not isinstance(sequence, list) or any(not isinstance(key, str) for key in sequence)
            or len(sequence) != len(enabled) or set(sequence) != enabled):
        raise ValueError("sequence must contain each enabled stage exactly once")
    expected_goal = {"Kill the Icon of Sin": "e3m4_boss", "Kill the Dark Lord": "e5m4_boss"}.get(slot_data["goal"])
    if slot_data["goal"] == "Complete the Full Saga":
        if goal not in {"e3m4_boss", "e5m4_boss"}:
            raise ValueError("Full Saga requires a concrete final boss")
    elif goal != expected_goal:
        raise ValueError("final stage contradicts the selected Goal")
    if goal is not None and (goal not in enabled or sequence[-1] != goal):
        raise ValueError("selected goal stage must be final")
    access_mode = plan["mode"] == "Mission Access as Items"
    if (not isinstance(starts, list) or not 1 <= len(starts) <= (3 if access_mode else 1)
            or starts != sequence[:len(starts)] or goal in starts):
        raise ValueError("invalid Starting Stages")
    bootstrap = {"Dash": 1} if (not slot_data["randomize_dash"] and
                 any(key not in {"e1m1_intro", "e1m2_war"} for key in starts)) else {}
    if plan.get("bootstrap_inventory") != bootstrap:
        raise ValueError("Dash bootstrap contradicts randomization/Starting Stages")
    if any(slot_data.get("starting_inventory", {}).get(name) != count for name, count in bootstrap.items()):
        raise ValueError("Dash bootstrap is absent from initial materialization")
    if type(plan.get("goal_as_item")) is not bool or (plan["goal_as_item"] and not (access_mode and goal)):
        raise ValueError("Goal Mission as Item requires Access mode and a boss stage")
    expected_access = {str(STAGES[key]["access_id"]): key for key in sequence
                       if access_mode and key not in starts and (key != goal or plan["goal_as_item"])}
    if plan.get("access_items") != expected_access:
        raise ValueError("Access pool contradicts starting/goal decisions")
    if plan.get("stages") != [stage for key, stage in STAGES.items() if key in enabled]:
        raise ValueError("stage catalog differs from compiled content")
    if plan.get("hub") != {"id": "hub", "map": "game/hub/hub"}:
        raise ValueError("universal Fortress contract is invalid")
    if plan["mode"] == "Vanilla Order":
        chronological = [key for key in STAGES if key in enabled and key != goal] + ([goal] if goal else [])
        if sequence != chronological:
            raise ValueError("Vanilla Order is not chronological")
        if slot_data["goal"] == "Complete the Full Saga" and goal != "e5m4_boss":
            raise ValueError("Vanilla Full Saga must end at Davoth")
    return plan


class UnifiedCampaign:
    def __init__(self, slot_data, session_state, persist):
        self.plan = validate_plan(slot_data)
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
        phase = sum(completed_count >= (count * len(ordinary) + 12) // 13
                    for count in (1, 2, 4, 5, 6, 8, 9))
        return {"rows": rows, "hub_map": plan["hub"]["map"], "fortress_phase": phase,
                "selected": self.navigation["selected"]}

    def select(self, stage, snapshot):
        if stage != "hub" and not any(row["stage"] == stage and row["unlocked"] for row in snapshot["rows"]):
            raise ValueError("native selection is unavailable in the authoritative projection")
        if self.navigation["selected"] != stage:
            self.navigation["selected"] = stage
            self.persist()
