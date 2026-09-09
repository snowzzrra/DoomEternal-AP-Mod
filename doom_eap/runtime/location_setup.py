"""Placement collection policy, independently bound to a connection generation."""
from dataclasses import dataclass
from typing import Protocol


class LocationScoutPort(Protocol):
    async def scout(self, location_ids) -> None: ...


@dataclass(frozen=True)
class ScoutResult:
    current: bool
    error: str | None = None


class LocationSetup:
    def __init__(self, logger):
        self._logger = logger
        self._generation = 0
        self.invalidate()

    def invalidate(self):
        self._generation += 1
        self._expected = None
        self._rows = {}
        self._failed = False
        self._complete = False

    @property
    def complete(self):
        return self._complete

    @property
    def received_ids(self):
        return frozenset(self._rows)

    @property
    def ready_to_resolve(self):
        return not self._failed and not self._complete and set(self._rows) == self._expected

    @staticmethod
    def _connected_ids(args, field):
        values = args.get(field)
        if not isinstance(values, (list, tuple, set, frozenset)):
            raise ValueError(f"Connected.{field} must be a location list")
        if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
            raise ValueError(f"Connected.{field} contains invalid location ID")
        return set(values)

    def begin(self, args, supported_ids):
        self.invalidate()
        missing = self._connected_ids(args, "missing_locations")
        checked = self._connected_ids(args, "checked_locations")
        if missing & checked:
            raise ValueError("Connected location sets overlap")
        active = missing | checked
        unknown = sorted(active - supported_ids)
        if unknown:
            raise ValueError(f"Connected contains unknown DOOM location IDs: {unknown}")
        self._expected = active
        return frozenset(active)

    def fail(self, message):
        if self._failed:
            return False
        self._failed = True
        self._logger.error("[Placement] SCOUT_REJECTED %s", message)
        return True

    async def scout(self, publication: LocationScoutPort):
        generation = self._generation
        try:
            await publication.scout(sorted(self._expected))
        except Exception as error:
            if generation != self._generation:
                return ScoutResult(False)
            return ScoutResult(True, f"LocationScouts failed: {error}")
        return ScoutResult(generation == self._generation)

    def consume(self, args, materialized_ids):
        if self._expected is None or self._failed:
            return None
        rows = args.get("locations") if isinstance(args, dict) else None
        if not isinstance(rows, (list, tuple)):
            return "LocationInfo.locations is not a list"
        packet_ids = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != 4:
                return "LocationInfo contains malformed placement"
            location_id = row[1]
            if not isinstance(location_id, int) or isinstance(location_id, bool):
                return "LocationInfo contains invalid location ID"
            packet_ids.append(location_id)
            if location_id not in self._expected:
                return f"LocationInfo contains unknown location ID: {location_id}"
            if location_id not in materialized_ids:
                return f"LocationInfo did not materialize location ID: {location_id}"
            previous = self._rows.get(location_id)
            if previous is not None and previous != tuple(row):
                return f"LocationInfo conflicts at location ID: {location_id}"
            self._rows[location_id] = tuple(row)
        if len(packet_ids) != len(set(packet_ids)):
            return "LocationInfo contains duplicate location IDs"
        return None

    def resolved(self):
        self._complete = True
