"""Room-scoped physical facts for native summaries; never a check authority."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from doom_eap.content.item_classification import ITEM_CLASSIFICATION_TRAP
from doom_eap.strings import text as player_text
from tools.maps.notification_formatting import item_receipt_text


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
        if catalog["schema"] != 2:
            raise ValueError("unsupported physical presentation catalog")
        self.pickups = {key: frozenset(row["pickups"]) for key, row in catalog["stages"].items()}
        self.maps = {key: row["runtime_map"] for key, row in catalog["stages"].items()}
        self.encounters = _encounter_locations(root)
        self.authored = tuple(sorted(challenges, key=lambda entry: entry["order"]))
        self.challenges = tuple(entry for entry in challenges
                                if entry["signal"].get("kind") == "physical_event_equivalent")

    @staticmethod
    def reward(entry, facts, placements, *, mastery=False):
        """Active Location identity, exact placement and server ACK; no inventory inference."""
        location_id = entry["location_id"]
        placement = placements.get(location_id)
        text = (
            (player_text("game.menu", "mastery_unknown") if mastery else player_text("notifications", "reward_unknown"))
            if placement is None
            else item_receipt_text(
                placement["item_name"],
                local=placement["local"],
                trap=bool(placement["classification"] & ITEM_CLASSIFICATION_TRAP),
                recipient_name=placement["recipient_name"],
                uppercase=False,
            )
        )
        return ChallengeReward(location_id, entry["signal"].get("unlockable", ""),
                               entry["name"].split(" - Mission Challenge - ")[-1],
                               location_id in facts.checked, text)

    def challenge_snapshot(self, namespace, generation, stage_id, *, revealed,
                           facts, placements, aggregates):
        runtime_map = self.maps.get(stage_id, "")
        if not revealed or not facts.checked_ready:
            return ChallengePresentation(namespace, generation, runtime_map, False, (), None)
        entries = tuple(entry for entry in self.authored if entry["mission_key"] == stage_id
                        and entry["location_id"] in facts.server_locations)
        authored = next((entry for entry in self.authored if entry["mission_key"] == stage_id), None)
        mission_key = authored["mission_key"] if authored else None
        aggregate = next((entry for entry in aggregates if entry["mission_key"] == mission_key
                          and entry["location_id"] in facts.server_locations), None)
        return ChallengePresentation(namespace, generation, runtime_map, True,
            tuple(self.reward(entry, facts, placements) for entry in entries),
            replace(self.reward(aggregate, facts, placements),
                    unlockable=authored["signal"]["unlockable"].rsplit("/", 1)[0]) if aggregate else None)

    def mastery_snapshot(self, facts, placements, masteries):
        return tuple(replace(self.reward(entry, facts, placements, mastery=True), unlockable=entry["gameplay_perk"]) for entry in masteries
                     if entry["location_id"] in facts.server_locations)

    def encounter_snapshot(self, stage_id, *, revealed, facts, placements, pending):
        if not revealed or not facts.checked_ready:
            return ()
        return tuple(replace(self.reward(entry, facts, placements),
                     checked=entry['location_id'] in facts.checked | pending)
                     for entry in self.encounters.get(stage_id, ())
                     if entry['location_id'] in facts.server_locations)

    def snapshot(self, namespace, generation, stage_id, *, revealed, facts, pending):
        """Unknown and unrevealed fields have no denominator or challenge metadata."""
        runtime_map = self.maps.get(stage_id, "")
        if not revealed or not facts.checked_ready or stage_id not in self.pickups:
            return MissionPresentation(namespace, generation, runtime_map, None, None, ())
        active = self.pickups[stage_id] & facts.server_locations
        observed = facts.checked | pending
        challenges = []
        for entry in self.challenges:
            if entry["mission_key"] != stage_id or entry["location_id"] not in facts.server_locations:
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


def _encounter_locations(root):
    names = json.loads((root / 'data/location_names.json').read_bytes())['locations']
    stages = json.loads((root / 'data/campaign_stages.json').read_bytes())
    return {stage['id']: tuple({'location_id': int(code), 'name': name,
        'signal': {'unlockable': f'encounter/{code}'}} for code, name in sorted(names.items())
        if name.startswith(stage['name'] + ' - ') and
        (' - Secret Encounter - ' in name or ' - Escalation Encounter Wave ' in name))
        for stage in stages if stage['map'].startswith(('game/dlc/', 'game/dlc2/'))}
