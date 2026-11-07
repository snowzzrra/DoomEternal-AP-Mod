"""Pure release goal objective selection from supplied authored catalog facts."""


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
        if not slot_data.get("use_dlc_content") or not slot_data.get("include_dlc_missions", True):
            names = {
                name for name in names
                if name.split(" - ", 1)[0] not in self._dlc_prefixes
            }
        return names


    def _objective_ids_goal(self, server_locations, slot_data):
        goal = slot_data.get("goal")
        endpoint_event = slot_data.get("goal_endpoint_event")
        required_capabilities = slot_data.get("required_capabilities")
        if (
            goal not in self._goal_names
            or endpoint_event != f"Internal Goal Endpoint: {goal}"
            or slot_data.get("goal_endpoint_available") is not True
        ):
            return frozenset()
        if (
            not isinstance(required_capabilities, list)
            or any(not isinstance(value, str) for value in required_capabilities)
            or not self._capabilities <= set(required_capabilities)
        ):
            return frozenset()
        requirements = slot_data.get("additional_victory_requirements", ())
        if not isinstance(requirements, list | tuple | set | frozenset):
            return frozenset()
        requirements = set(requirements)
        if not requirements <= self._requirement_names:
            return frozenset()

        active_names = self._active_goal_location_names(slot_data, server_locations)
        return self._objectives(slot_data, goal, requirements, active_names)


    def objective_ids(self, slot_data, server_locations):
        slot_data = self._goal_options(slot_data)
        if not isinstance(slot_data, dict):
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
                "goal_endpoint_available": True, "required_capabilities": list(self._capabilities),
                "additional_victory_requirements": goal["additional_requirements"],
                "master_level_completion_ids": [self._master_levels[key]
                    for key in slot_data["campaign"].get("master_level_stage_ids", ())],
                "campaign_plan": {"goal_stage": goal["stage_id"]},
                "use_dlc_content": True, "include_dlc_missions": True}

