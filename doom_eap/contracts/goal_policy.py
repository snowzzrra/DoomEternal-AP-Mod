"""Pure release goal objective selection from supplied authored catalog facts."""


class GoalPolicy:
    def __init__(self, locations, dlc_prefixes, capabilities, endpoint_ids, suffixes):
        self._locations = dict(locations)
        self._dlc_prefixes = frozenset(dlc_prefixes)
        self._capabilities = frozenset(capabilities)
        self._endpoint_ids = dict(endpoint_ids)
        self._suffixes = dict(suffixes)
        self._goal_names = frozenset(endpoint_ids)
        self._requirement_names = frozenset(suffixes) | {"Acquire the Unmaykr"}

    def _active_goal_location_names(self, slot_data):
        names = set(self._locations.values())
        if not slot_data.get("use_dlc_content") or not slot_data.get("include_dlc_missions", True):
            names = {
                name for name in names
                if name.split(" - ", 1)[0] not in self._dlc_prefixes
            }
        return names


    def objective_ids(self, slot_data):
        if not isinstance(slot_data, dict):
            return frozenset()
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
        if not isinstance(requirements, (list, tuple, set, frozenset)):
            return frozenset()
        requirements = set(requirements)
        if not requirements <= self._requirement_names:
            return frozenset()

        active_names = self._active_goal_location_names(slot_data)
        objective_ids = {self._endpoint_ids[goal]}
        if goal == "Complete the Full Saga":
            objective_ids.update({7770418, 7770414, 7770419})
            objective_ids.update(
                location_id
                for location_id, location_name in self._locations.items()
                if location_name in active_names
                and location_name.endswith(" - Mission Complete")
            )
        if "Acquire the Unmaykr" in requirements:
            objective_ids.add(self._endpoint_ids["Acquire the Unmaykr"])
        for requirement in requirements - {"Acquire the Unmaykr"}:
            suffix = self._suffixes[requirement]
            objective_ids.update(
                location_id
                for location_id, location_name in self._locations.items()
                if location_name in active_names and suffix in location_name
            )
        return frozenset(objective_ids)


