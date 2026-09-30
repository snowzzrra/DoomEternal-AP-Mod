"""Room-scoped physical facts for native summaries; never a check authority."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from tools.maps.notification_formatting import item_receipt_text
from doom_eap.content.item_classification import ITEM_CLASSIFICATION_TRAP


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


@dataclass(frozen=True)
class ChallengeReward:
    location_id: int
    unlockable: str
    name: str
    checked: bool
    text: str


@dataclass(frozen=True)
class ChallengePresentation:
    namespace: str
    generation: str
    runtime_map: str
    known: bool
    challenges: tuple[ChallengeReward, ...]
    aggregate: ChallengeReward | None

    @property
    def completed(self):
        return tuple(entry for entry in self.challenges if entry.checked)


class MissionPresentations:
    def __init__(self, root: Path, challenges):
        catalog = json.loads((root / "data/physical_pickups.json").read_text(encoding="utf-8"))
        if catalog["schema"] != 1:
            raise ValueError("unsupported physical presentation catalog")
        self.pickups = {key: frozenset(ids) for key, ids in catalog["maps"].items()}
        self.authored = tuple(sorted(challenges, key=lambda entry: entry["order"]))
        self.challenges = tuple(entry for entry in challenges
                                if entry["signal"].get("kind") == "physical_event_equivalent")

    @staticmethod
    def reward(entry, facts, placements):
        """Active Location identity, exact placement and server ACK; no inventory inference."""
        location_id = entry["location_id"]
        placement = placements.get(location_id)
        text = "REWARD UNKNOWN" if placement is None else item_receipt_text(
            placement["item_name"], local=placement["local"],
            trap=bool(placement["classification"] & ITEM_CLASSIFICATION_TRAP),
            recipient_name=placement["recipient_name"], uppercase=False)
        return ChallengeReward(location_id, entry["signal"].get("unlockable", ""),
                               entry["name"].split(" - Mission Challenge - ")[-1],
                               location_id in facts.checked, text)

    def challenge_snapshot(self, namespace, generation, runtime_map, *, revealed,
                           facts, placements, aggregates):
        if not revealed or not facts.checked_ready:
            return ChallengePresentation(namespace, generation, runtime_map, False, (), None)
        entries = tuple(entry for entry in self.authored if entry["runtime_map"] == runtime_map
                        and entry["location_id"] in facts.server_locations)
        authored = next((entry for entry in self.authored if entry["runtime_map"] == runtime_map), None)
        mission_key = authored["mission_key"] if authored else None
        aggregate = next((entry for entry in aggregates if entry["mission_key"] == mission_key
                          and entry["location_id"] in facts.server_locations), None)
        return ChallengePresentation(namespace, generation, runtime_map, True,
            tuple(self.reward(entry, facts, placements) for entry in entries),
            replace(self.reward(aggregate, facts, placements),
                    unlockable=authored["signal"]["unlockable"].rsplit("/", 1)[0]) if aggregate else None)

    def mastery_snapshot(self, facts, placements, masteries):
        return tuple(replace(self.reward(entry, facts, placements), unlockable=entry["gameplay_perk"]) for entry in masteries
                     if entry["location_id"] in facts.server_locations)

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
