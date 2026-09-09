"""Physical-check disposition and submission evidence, independent of file IO."""
from typing import Protocol

from doom_eap.contracts.check_observation import CheckObservation


class CheckBatchPublicationPort(Protocol):
    async def locations(self, location_ids) -> None: ...
    def mark_submitted(self, location_id) -> None: ...


class PhysicalChecks:
    def __init__(self, logger):
        self._logger = logger
        self._generation = 0
        self._last_event = None

    @property
    def last_event(self):
        return self._last_event

    def invalidate(self):
        self._generation += 1

    @staticmethod
    def disposition(location_id, facts: CheckObservation, item_ready):
        if location_id in facts.checked:
            return "acknowledged"
        if item_ready and facts.server_locations and location_id not in facts.server_locations:
            return "quarantine"
        if location_id not in facts.server_locations:
            return "outside_slot"
        return "submitted" if location_id in facts.submitted else "submit"

    async def publish(self, location_ids, publication: CheckBatchPublicationPort):
        if not location_ids:
            return
        generation = self._generation
        self._last_event = location_ids[-1]
        try:
            await publication.locations(location_ids)
        except Exception as error:
            if generation == self._generation:
                self._logger.error("[Trigger] Failed to send AP check events; preserving files for retry: %s", error)
            return
        if generation != self._generation:
            return
        for location_id in location_ids:
            self._logger.info("[Trigger] Native AP event detected -> Queued Location %s", location_id)
            publication.mark_submitted(location_id)
