"""Save candidates and native handshake values; neither implies active-slot proof."""

from pathlib import Path
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NamedTuple


class PrimarySaveSelection(NamedTuple):
    slot_directory: str
    path: Path
    mtime_ns: int

    @property
    def cache_key(self):
        return (self.slot_directory, str(self.path), self.mtime_ns)


class GameplaySaveEvidence(NamedTuple):
    state: str
    epoch: int
    slot_directory: str
    map_name: str
    provisional: bool = False
    native_safe: bool = False


@dataclass(frozen=True)
class SaveSelectionSnapshot:
    slot: str | None = None
    path: str | None = None
    token: int | None = None
    native_evidence_epoch: int | None = None


@dataclass(frozen=True)
class SaveReadinessSnapshot:
    authoritative: bool = False
    slot: str | None = None
    evidence_epoch: int | None = None
    load_epoch: int | None = None
    frozen: bool = True


@dataclass(frozen=True)
class SaveCandidateDecision:
    """Next observation step; never an accepted runtime-map transition."""

    action: str
    reason: str | None = None
    target_slot: str | None = None
    continued: PrimarySaveSelection | None = None
    reset_observation_slot: bool = False


@dataclass(frozen=True)
class MissionSelectObservation:
    map_name: str | None = None
    epoch: int | None = None


@dataclass(frozen=True)
class SaveProofDecision:
    action: str
    reason: str | None = None
    new_evidence: bool = False
    reset_observation_slot: bool = False


def expected_save_prefix_for_campaign(campaign: str | None) -> str | None:
    """Map campaign identifier to canonical save slot prefix."""
    if not campaign:
        return None
    if campaign == "Base":
        return "GAME-AUTOSAVE"
    if campaign in ("TAG1", "ARC"):
        return "DLC1-AUTOSAVE"
    if campaign in ("TAG2", "Dark Lord"):
        return "DLC2-AUTOSAVE"
    if campaign == "Horde":
        return "HORDE-AUTOSAVE"
    return None


def unlockable_record_complete(record: Mapping, signal: Mapping) -> bool:
    expected_count = signal.get("rule_0_statCount")
    return (
        int(record.get("numUnlockableRules", -1)) == signal["numUnlockableRules"]
        and record.get("rule_0_statname") == signal["rule_0_statname"]
        and (
            expected_count is None
            or int(record.get("rule_0_statCount", -1)) >= expected_count
        )
        and int(record.get("rule_0_statDuration", -1))
        == signal["rule_0_statDuration"]
        and bool(record.get("rule_0_satisfied", False))
        is signal["rule_0_satisfied"]
        and bool(record.get("unlockableIsUnlocked", False))
        is signal["unlockableIsUnlocked"]
    )
