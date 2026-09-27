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
        self._pending = set()

    @property
    def last_event(self):
        return self._last_event

    def invalidate(self):
        self._generation += 1
        self._pending.clear()

    def pending(self, facts):
        """Qualified observations retained until ACK, within this publication epoch."""
        self._pending.difference_update(facts.checked)
        return frozenset(self._pending & facts.server_locations)

    def disposition(self, location_id, facts: CheckObservation, item_ready):
        if location_id in facts.checked:
            return "acknowledged"
        if item_ready and facts.server_locations and location_id not in facts.server_locations:
            return "quarantine"
        if location_id not in facts.server_locations:
            return "outside_slot"
        self._pending.add(location_id)
        return "submitted" if location_id in facts.submitted else "submit"

    async def publish(self, location_ids, publication: CheckBatchPublicationPort):
        if not location_ids:
            return
        generation = self._generation
        self._pending.update(location_ids)
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
