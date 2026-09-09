"""Immutable CommonClient check/connection facts; submission is not acknowledgement."""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CheckObservation:
    checked: frozenset[int]
    submitted: frozenset[int]
    server_locations: frozenset[int]
    checked_ready: bool
    connected: bool


class CheckPublicationPort(Protocol):
    async def location(self, location_id: int): ...
    async def goal(self): ...
    def mark_submitted(self, location_id: int): ...
