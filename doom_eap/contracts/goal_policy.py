"""Pure release goal objective selection from supplied authored catalog facts."""

import json
from pathlib import Path


class GoalPolicy:
    def __init__(self, locations, dlc_prefixes, capabilities, endpoint_ids, suffixes, master_levels=()):
        self._locations = dict(locations)
        self._dlc_prefixes = frozenset(dlc_prefixes)
        self._capabilities = frozenset(capabilities)
        self._endpoint_ids = dict(endpoint_ids)
        self._suffixes = dict(suffixes)
        self._master_levels = {stage["id"]: stage["completion_id"] for stage in master_levels}
        self._goal_names = frozenset(endpoint_ids) | {"Complete N Master Levels"}
        self._requirement_names = frozenset(suffixes) | {"Acquire the Unmaykr", "Complete all Master Levels"}

    def threshold(self, slot_data):
        campaign = slot_data.get("campaign", {})
        goal = campaign.get("goal", {})
        if goal.get("kind") != "Complete N Master Levels":
            return frozenset(), 0
        candidates = frozenset(self._master_levels[key] for key in campaign["master_level_stage_ids"])
        count = len(candidates) if "Complete all Master Levels" in goal["additional_requirements"] else goal["count"]
        return candidates, count

    def _active_goal_location_names(self, slot_data, server_locations):
        names = {name for identity, name in self._locations.items() if identity in server_locations}
        if "campaign" not in slot_data and (not slot_data.get("use_dlc_content") or not slot_data.get("include_dlc_missions", True)):
            names = {
                name for name in names
                if name.split(" - ", 1)[0] not in self._dlc_prefixes
            }
        return names


    def _objective_ids_goal(self, server_locations, slot_data):
        active_names = self._active_goal_location_names(slot_data, server_locations)
        slot_data = self._goal_options(slot_data)
        goal = slot_data.get("goal")
        endpoint_event = slot_data.get("goal_endpoint_event")
        if (
            goal not in self._goal_names
            or endpoint_event != f"Internal Goal Endpoint: {goal}"
        ):
            return frozenset()
        requirements = slot_data.get("additional_victory_requirements", ())
        if not isinstance(requirements, list | tuple | set | frozenset):
            return frozenset()
        requirements = set(requirements)
        if not requirements <= self._requirement_names:
            return frozenset()

        return self._objectives(slot_data, goal, requirements, active_names)


    def objective_ids(self, slot_data, server_locations):
        if not isinstance(slot_data, dict):
            return frozenset()
        if "campaign" not in slot_data:
            required_capabilities = slot_data.get("required_capabilities")
            if (
                slot_data.get("goal_endpoint_available") is not True
                or not isinstance(required_capabilities, list)
                or any(not isinstance(value, str) for value in required_capabilities)
                or not self._capabilities <= set(required_capabilities)
            ):
                return frozenset()
        return self._objective_ids_goal(server_locations, slot_data)

    def _objectives(self, slot_data, goal, requirements, active_names):
        masters = set(slot_data.get("master_level_completion_ids", ()))
        objective_ids = masters if goal == "Complete N Master Levels" else {self._endpoint_ids[goal]}
        if goal == "Complete the Full Saga":
            final = slot_data['campaign_plan']['goal_stage']
            objective_ids = {{'e3m4_boss': 7770414, 'e5m4_boss': 7770419}[final]}
        if "Acquire the Unmaykr" in requirements:
            objective_ids.add(self._endpoint_ids["Acquire the Unmaykr"])
        if "Complete all Master Levels" in requirements:
            objective_ids.update(masters)
        for requirement in requirements - {"Acquire the Unmaykr", "Complete all Master Levels"}:
            suffix = self._suffixes[requirement]
            objective_ids.update(
                location_id
                for location_id, location_name in self._locations.items()
                if location_name in active_names and suffix in location_name and location_id not in masters
            )
        return frozenset(objective_ids)

    def _goal_options(self, slot_data):
        if not isinstance(slot_data, dict) or "campaign" not in slot_data:
            return slot_data
        goal = slot_data["campaign"]["goal"]
        return {"goal": goal["kind"], "goal_endpoint_event": goal["endpoint_event"],
                "additional_victory_requirements": goal["additional_requirements"],
                "master_level_completion_ids": [self._master_levels[key]
                    for key in slot_data["campaign"].get("master_level_stage_ids", ())],
                "campaign_plan": {"goal_stage": goal["stage_id"]}}


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPO_ROOT / "data" / "campaign_goal_contract.json"
REQUIRED_FIELDS = {
    "schema_version",
    "release",
    "publisher_key",
    "event_filename",
    "marker",
    "event_target",
    "owner",
    "source",
    "runtime_map",
    "destination_map",
}


def load_campaign_goal_contract(path: Path = CONTRACT_PATH) -> dict:
    contract = json.loads(path.read_text(encoding="utf-8"))
    if set(contract) != REQUIRED_FIELDS:
        raise ValueError(
            "campaign goal contract fields drift: "
            f"expected {sorted(REQUIRED_FIELDS)}, got {sorted(contract)}"
        )
    if contract["schema_version"] != 1:
        raise ValueError("unsupported campaign goal contract schema")
    for key in REQUIRED_FIELDS - {"schema_version"}:
        if not isinstance(contract[key], str) or not contract[key].strip():
            raise ValueError(f"campaign goal contract has invalid {key}")
    if not contract["event_filename"].endswith(".txt"):
        raise ValueError("campaign goal event_filename must be a .txt file")
    if not contract["marker"].startswith(("AP_GOAL_EVENT_", "AP_MISSION_EVENT_")):
        raise ValueError("campaign goal marker must identify a publisher event")
    if contract["event_target"] != "ap_campaign_goal_event":
        raise ValueError("campaign goal event_target drift")
    if contract["runtime_map"] == contract["destination_map"]:
        raise ValueError("campaign goal transition cannot target its source map")
    return contract


CAMPAIGN_GOAL_CONTRACT = load_campaign_goal_contract()
