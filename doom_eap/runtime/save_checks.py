"""Save-backed check policy; SaveObserver retains baseline and diagnostic cache ownership."""
from dataclasses import dataclass
from typing import Protocol
from doom_eap.contracts.save_observation import unlockable_record_complete
from doom_eap.contracts.challenge_registry import aggregate_ready


class SaveCheckObservationPort(Protocol):
    def observe_edges(self, observer_key, records, entries, slot_directory): ...
    def observe_record(self, kind, slot, unlockable, record): ...


@dataclass(frozen=True)
class SaveCheckReadiness:
    item_ready: bool
    authoritative: bool
    slot: str | None


class SaveChecks:
    def __init__(self, masteries, challenges, challenge_maps, aggregates, logger):
        self._mastery_entries, self._challenge_entries = dict(masteries), dict(challenges)
        self._challenge_maps, self._aggregates = dict(challenge_maps), tuple(aggregates)
        self._logger = logger
        self._masteries, self._challenges = {}, {}
        self._mastery_warnings, self._challenge_warnings = set(), set()
        self._generation = 0

    def invalidate(self):
        self._generation += 1

    def bind(self):
        self.invalidate()
        self.reset_observations(populate=False)

    def reset_observations(self, *, populate=True):
        self._masteries = {key: False for key in self._mastery_entries} if populate else {}
        self._challenges = {key: False for key in self._challenge_entries} if populate else {}

    def observe_masteries(self, records, path, slot_directory, observations: SaveCheckObservationPort, *, authoritative):
        if not authoritative:
            return
        completion_states = {
            unlockable: (
                unlockable in records
                and unlockable_record_complete(records[unlockable], entry["signal"])
            )
            for unlockable, entry in self._mastery_entries.items()
        }
        pending_edges = observations.observe_edges(
            "weapon_masteries",
            completion_states,
            self._mastery_entries,
            slot_directory,
        )
        for unlockable in self._mastery_entries:
            self._masteries.setdefault(unlockable, False)
        for unlockable, record in records.items():
            entry = self._mastery_entries.get(unlockable)
            if entry is None:
                continue
            observed_record = (
                int(record["numUnlockableRules"]),
                record["rule_0_statname"],
                int(record["rule_0_statCount"]),
                int(record["rule_0_statDuration"]),
                bool(record["rule_0_satisfied"]),
                bool(record["unlockableIsUnlocked"]),
            )
            if observations.observe_record("mastery", slot_directory, unlockable, observed_record):
                self._logger.info(
                    "[Mastery] RECORD unlockable=%s rules=%s stat=%s count=%s "
                    "duration=%s satisfied=%s unlocked=%s save_slot=%s source=%s",
                    unlockable,
                    *observed_record,
                    slot_directory,
                    path,
                )

            if unlockable not in pending_edges:
                continue
            self._masteries[unlockable] = True


    def observe_challenges(self, records, path, slot_directory, mission_select_map, mission_select_epoch, observations: SaveCheckObservationPort, *, authoritative):
        if not authoritative:
            return
        save_entries = {
            unlockable: entry
            for unlockable, entry in self._challenge_entries.items()
            if entry["signal"]["kind"] in {"unlockable_record", "stat_threshold"}
            and (
                not mission_select_map
                or self._challenge_maps.get(unlockable)
                == mission_select_map
            )
        }
        completion_states = {
            unlockable: (
                unlockable in records
                and unlockable_record_complete(records[unlockable], entry["signal"])
            )
            for unlockable, entry in save_entries.items()
        }
        observer_key = "mission_challenges"
        if mission_select_map:
            observer_key = (
                f"mission_challenges:mission_select:"
                f"{mission_select_epoch}:"
                f"{mission_select_map}"
            )
        pending_edges = observations.observe_edges(
            observer_key,
            completion_states,
            save_entries,
            slot_directory,
        )
        for unlockable, record in records.items():
            entry = self._challenge_entries.get(unlockable)
            if entry is None:
                continue
            signal = entry["signal"]
            if signal["kind"] not in {"unlockable_record", "stat_threshold"}:
                continue
            observed_record = (
                int(record["numUnlockableRules"]),
                record["rule_0_statname"],
                int(record["rule_0_statCount"]),
                int(record["rule_0_statDuration"]),
                bool(record["rule_0_satisfied"]),
                bool(record["unlockableIsUnlocked"]),
            )
            if observations.observe_record("challenge", slot_directory, unlockable, observed_record):
                self._logger.info(
                    "[Challenge] RECORD unlockable=%s rules=%s stat=%s count=%s "
                    "duration=%s satisfied=%s unlocked=%s save_slot=%s source=%s",
                    unlockable,
                    *observed_record,
                    slot_directory,
                    path,
                )

            if observed_record[0] != signal["numUnlockableRules"]:
                self._logger.warning(
                    "[Challenge] REGISTRY_MISMATCH unlockable=%s field=numUnlockableRules expected=%s observed=%s",
                    unlockable, signal["numUnlockableRules"], observed_record[0],
                )
            if observed_record[1] != signal["rule_0_statname"]:
                self._logger.warning(
                    "[Challenge] REGISTRY_MISMATCH unlockable=%s field=rule_0_statname expected=%s observed=%s",
                    unlockable, signal["rule_0_statname"], observed_record[1],
                )
            if observed_record[3] != signal["rule_0_statDuration"]:
                self._logger.warning(
                    "[Challenge] REGISTRY_MISMATCH unlockable=%s field=rule_0_statDuration expected=%s observed=%s",
                    unlockable, signal["rule_0_statDuration"], observed_record[3],
                )
            expected_count = signal.get("rule_0_statCount")
            if expected_count is not None and observed_record[2] < expected_count:
                self._logger.warning(
                    "[Challenge] REGISTRY_MISMATCH unlockable=%s "
                    "field=rule_0_statCount expected_at_least=%s observed=%s",
                    unlockable, expected_count, observed_record[2],
                )

            if unlockable not in pending_edges:
                continue
            self._challenges[unlockable] = True


    async def check_mastery(self, entry, readiness, facts, publication):
        if not readiness.item_ready or not readiness.authoritative:
            return
        unlockable = entry["signal"]["unlockable"]
        if not self._masteries.get(unlockable):
            return
        location_id = entry["location_id"]
        if location_id in facts.checked or location_id in facts.submitted:
            return
        if location_id not in facts.server_locations:
            if location_id not in self._mastery_warnings:
                self._logger.warning(
                    "[Mastery] LOCATION id=%s unlockable=%s slot=absent",
                    location_id,
                    unlockable,
                )
                self._mastery_warnings.add(location_id)
            return
        if not facts.connected:
            return
        generation = self._generation
        try:
            self._logger.info(
                "[Mastery] LOCATION_CHECK_SEND id=%s unlockable=%s "
                "source=vanilla_save_predicate",
                location_id,
                unlockable,
            )
            await publication.location(location_id)
        except Exception as error:
            if generation != self._generation:
                return
            self._logger.error(
                "[Mastery] LOCATION_CHECK_RETRY id=%s unlockable=%s error=%s",
                location_id,
                unlockable,
                error,
            )
            return
        if generation != self._generation:
            return
        publication.mark_submitted(location_id)
        self._logger.info("[Mastery] LOCATION_CHECK_ACK id=%s", location_id)


    async def check_challenge(self, entry, readiness, facts, publication):
        if not readiness.item_ready:
            return
        is_physical = entry["signal"].get("kind") == "physical_event_equivalent"
        if not is_physical and not readiness.authoritative:
            return
        unlockable = entry["signal"]["unlockable"]
        if is_physical:
            physical_ids = set(entry["signal"].get("physical_location_ids", ()))
            required_count = int(entry["signal"].get("required_count", 1))
            if len(physical_ids.intersection(facts.checked)) < required_count:
                return
        elif not self._challenges.get(unlockable):
            return
        location_id = entry["location_id"]
        if location_id in facts.checked:
            return
        if location_id not in facts.server_locations:
            if location_id not in self._challenge_warnings:
                self._logger.warning(
                    "[Challenge] LOCATION id=%s unlockable=%s slot=absent",
                    location_id,
                    unlockable,
                )
                self._challenge_warnings.add(location_id)
            return
        if not facts.connected:
            return
        source_name = "physical_event_equivalent" if is_physical else "vanilla_save_predicate"
        generation = self._generation
        try:
            self._logger.info(
                "[Challenge] LOCATION_CHECK_SEND id=%s unlockable=%s "
                "source=%s save_slot=%s",
                location_id,
                unlockable,
                source_name,
                readiness.slot or "<synthetic>",
            )
            await publication.location(location_id)
        except Exception as error:
            if generation != self._generation:
                return
            self._logger.error(
                "[Challenge] LOCATION_CHECK_RETRY id=%s unlockable=%s error=%s",
                location_id,
                unlockable,
                error,
            )
            return
        if generation != self._generation:
            return
        self._logger.info("[Challenge] LOCATION_CHECK_QUEUED id=%s awaiting=server_ack", location_id)


    async def check_aggregates(self, readiness, facts, publication):
        """Publish aggregates only from server-authoritative checked children."""
        if not readiness.item_ready:
            return
        generation = self._generation
        checked = set(facts.checked)
        for aggregate in self._aggregates:
            signal = aggregate["signal"]
            children = set(signal["children"])
            if not aggregate_ready(signal, checked):
                continue
            location_id = aggregate["location_id"]
            if location_id in checked:
                continue
            if location_id not in facts.server_locations:
                if location_id not in self._challenge_warnings:
                    self._logger.warning(
                        "[Challenge] ALL_LOCATION id=%s slot=absent", location_id
                    )
                    self._challenge_warnings.add(location_id)
                continue
            if not facts.connected:
                continue
            self._logger.info(
                "[Challenge] ALL_LOCATION_CHECK_SEND id=%s authority=server_checked_locations "
                "children=%s",
                location_id,
                sorted(children),
            )
            try:
                await publication.location(location_id)
            except Exception as error:
                if generation != self._generation:
                    return
                self._logger.error(
                    "[Challenge] ALL_LOCATION_CHECK_RETRY id=%s error=%s",
                    location_id,
                    error,
                )
                continue
            if generation != self._generation:
                return
            self._logger.info(
                "[Challenge] ALL_LOCATION_CHECK_QUEUED id=%s awaiting=server_ack",
                location_id,
            )

