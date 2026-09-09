"""Adapt bound SaveObserver baselines and existing persistence/log effects."""
from dataclasses import dataclass


@dataclass(frozen=True)
class SaveCheckBinding:
    identity: str
    team: int
    slot: int
    state_key: str | None
    registry_revision: str
    checked: frozenset[int]
    item_ready: bool
    persist_enabled: bool


class SaveCheckObservations:
    def __init__(self, observer, scope, persist, logger):
        self._observer, self._scope, self._persist, self._logger = observer, scope, persist, logger

    def observe_record(self, kind, slot, unlockable, record):
        return self._observer.observe_record(kind, slot, unlockable, record)

    def observe_edges(self, observer_key, records, entries, slot_directory):
        if not self._scope.item_ready:
            return set()
        acknowledged = {
            key
            for key, entry in entries.items()
            if entry["location_id"] in self._scope.checked
        }
        pending, created, new_edges = self._observer.observe_edges(
            session_identity=self._scope.identity,
            team=self._scope.team,
            slot=self._scope.slot,
            doom_save_slot=slot_directory,
            registry_revision=self._scope.registry_revision,
            observer_key=observer_key,
            records=records,
            acknowledged_records=acknowledged,
        )
        if self._scope.persist_enabled:
            self._persist()
        if created:
            self._logger.info(
                "[OBSERVER] BASELINE_CREATED session=%s save_slot=%s records=%s",
                self._scope.state_key,
                slot_directory,
                sum(records.values()),
            )
        for key in sorted(new_edges):
            self._logger.info("[OBSERVER] EDGE_COMPLETE key=%s", key)
        return pending


