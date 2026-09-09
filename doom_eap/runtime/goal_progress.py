"""Goal publication and delayed save completion; no discovery or lifecycle authority."""
from types import MappingProxyType
from doom_eap.contracts.check_observation import CheckObservation, CheckPublicationPort
from doom_eap.contracts.publisher_contracts import canonical_map_name


class GoalProgress:
    def __init__(self, final_map, cultist_map, logger):
        self._final_map, self._cultist_map, self._logger = final_map, cultist_map, logger
        self._state = {}
        self._state_key = None
        self._generation = 0
        self._sent = self._in_flight = False
        self._candidate = None
        self._candidate_revision = 0
        self._last_details_mtime = None
        self._completion_states = {}

    @property
    def sent(self):
        return self._sent

    @property
    def candidate(self):
        return MappingProxyType(dict(self._candidate)) if self._candidate is not None else None

    @property
    def cultist_path(self):
        return self._state.get("cultist_autosave_path")

    def invalidate_publication(self):
        self._generation += 1
        self._in_flight = False

    def bind(self, state_key, state):
        self.invalidate_publication()
        if self._state_key != state_key:
            self._sent = False
            self.clear_candidate("room_changed")
        self._state_key, self._state = state_key, state
        state.setdefault("goal_sent", False)

    def connected(self, persist):
        self.invalidate_publication()
        self._sent = False
        self._state["goal_sent"] = False
        persist()

    def candidate_token(self):
        return self._generation, self._candidate_revision

    def acknowledge_candidate(self, token):
        if token == self.candidate_token():
            self.clear_candidate("publisher_acknowledged")

    def candidate_slot_allowed(self, authoritative, slot):
        if self._candidate is not None and authoritative and slot and slot != self._candidate["slot"]:
            self.clear_candidate("different_authoritative_slot")
            return False
        return True

    def observe_candidate(self, selected, details):
        candidate = self._candidate
        if candidate is None:
            return "invalidated"
        if selected is not None and details and selected.slot_directory == candidate["slot"]:
            token = details.get("_mtime_ns", selected.mtime_ns)
            if str(details.get("_path")) == candidate["details_path"] and token is not None and int(token) > candidate["details_token_at_arm"]:
                if details.get("completed") != "1":
                    self.clear_candidate("fresh_incomplete_details")
                    return "invalidated"
                return "publish"
        return "pending"

    async def evaluate(self, source_description, objective_ids, facts: CheckObservation, publication: CheckPublicationPort):
        if self._sent:
            return True
        if self._in_flight:
            return False
        if not facts.checked_ready:
            return False
        checked_locations = facts.checked
        if not objective_ids or not objective_ids <= set(checked_locations):
            return False
        if not facts.connected:
            return False
        self._in_flight = True
        self._logger.info(
            "[Goal] CLIENT_GOAL_SEND source=%s objective_ids=%s",
            source_description,
            sorted(objective_ids),
        )
        generation = self._generation
        try:
            await publication.goal()
        except Exception:
            if generation != self._generation:
                return False
            self._in_flight = False
            raise
        if generation != self._generation:
            return False
        self._in_flight = False
        self._sent = True
        return True


    def clear_candidate(self, reason):
        candidate = self._candidate
        if candidate is not None:
            self._logger.info(
                "[Goal] FINAL_SIN_COMPLETION_CANDIDATE_CLEARED slot=%s load_epoch=%s reason=%s",
                candidate["slot"],
                candidate["load_epoch"],
                reason,
            )
        self._candidate = None
        self._candidate_revision += 1


    def arm_candidate(
        self, selected, details, runtime_map, load_epoch
    ):
        if (
            canonical_map_name(runtime_map)
            != canonical_map_name(self._final_map)
            or load_epoch is None
        ):
            return
        details_path = details.get("_path")
        details_token = details.get("_mtime_ns", selected.mtime_ns)
        if not details_path or details_token is None:
            return
        existing = self._candidate
        identity = (selected.slot_directory, load_epoch, str(details_path))
        if existing is not None and identity == (
            existing["slot"],
            existing["load_epoch"],
            existing["details_path"],
        ):
            return
        self._candidate_revision += 1
        self._candidate = {
            "slot": selected.slot_directory,
            "load_epoch": load_epoch,
            "details_path": str(details_path),
            "details_token_at_arm": int(details_token),
            "completed_at_arm": str(details.get("completed", "0")),
        }
        self._logger.info(
            "[Goal] FINAL_SIN_COMPLETION_CANDIDATE_ARMED slot=%s load_epoch=%s "
            "details_token=%s completed=%s",
            selected.slot_directory,
            load_epoch,
            details_token,
            details.get("completed", "0"),
        )


    def observe_completion(self, details, persist):
        record_map = canonical_map_name(details.get("mapName", ""))
        if not record_map:
            return
        mtime = details.get("_mtime_ns")
        if mtime == self._last_details_mtime:
            return
        self._last_details_mtime = mtime

        details_path = details.get("_path")
        if not details_path:
            return

        is_completed = details.get("completed") == "1"
        key = (details_path, record_map)
        prev_status = self._completion_states.get(key)
        self._completion_states[key] = "1" if is_completed else "0"

        fresh_completion = (prev_status == "0" and is_completed)

        if record_map == self._cultist_map:
            if self.cultist_path != details_path:
                self._state["cultist_autosave_path"] = details_path
                persist()
                self._logger.info(
                    f"[Goal] Tracking Cultist Base completion from {details_path}."
                )
            return

        if fresh_completion and record_map in {"e3m4_boss", "game/sp/e3m4_boss/e3m4_boss"}:
            return "final_sin_mission_complete", "Final Sin save fallback"
        if fresh_completion and record_map in {"e1m4_boss", "game/sp/e1m4_boss/e1m4_boss"}:
            return "doom_hunter_base_mission_complete", "Doom Hunter Base save fallback"
        return None
