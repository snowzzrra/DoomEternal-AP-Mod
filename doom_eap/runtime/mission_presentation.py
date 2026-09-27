"""Room-scoped physical facts for native summaries; never a check authority."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PhysicalChallenge:
    unlockable: str
    found: int
    required: int
    checked: bool


@dataclass(frozen=True)
class MissionPresentation:
    namespace: str
    generation: str
    runtime_map: str
    found: int | None
    total: int | None
    challenges: tuple[PhysicalChallenge, ...]


class MissionPresentations:
    def __init__(self, root: Path, challenges):
        catalog = json.loads((root / "data/physical_pickups.json").read_text(encoding="utf-8"))
        if catalog["schema"] != 1:
            raise ValueError("unsupported physical presentation catalog")
        self.pickups = {key: frozenset(ids) for key, ids in catalog["maps"].items()}
        self.challenges = tuple(entry for entry in challenges
                                if entry["signal"].get("kind") == "physical_event_equivalent")

    def snapshot(self, namespace, generation, runtime_map, *, revealed, facts, pending):
        """Unknown and unrevealed fields have no denominator or challenge metadata."""
        if not revealed or not facts.checked_ready or runtime_map not in self.pickups:
            return MissionPresentation(namespace, generation, runtime_map, None, None, ())
        active = self.pickups[runtime_map] & facts.server_locations
        observed = facts.checked | pending
        challenges = []
        for entry in self.challenges:
            if entry["runtime_map"] != runtime_map or entry["location_id"] not in facts.server_locations:
                continue
            signal = entry["signal"]
            required = int(signal.get("required_count", 1))
            sources = frozenset(signal["physical_location_ids"]) & active
            if len(sources) < required:
                continue
            challenges.append(PhysicalChallenge(signal["unlockable"],
                                                min(required, len(sources & observed)), required,
                                                entry["location_id"] in facts.checked))
        return MissionPresentation(namespace, generation, runtime_map,
                                   len(active & observed), len(active), tuple(challenges))
