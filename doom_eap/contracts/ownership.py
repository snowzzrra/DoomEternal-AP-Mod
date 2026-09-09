"""Immutable AP and derived ownership facts, without transport or game state."""

from dataclasses import dataclass

from doom_eap.contracts.tag_prerequisites import AuthoredTagPrerequisites


@dataclass(frozen=True)
class ReceiptSessionToken:
    state_key: str
    generation: int


@dataclass(frozen=True)
class StartingMaterializationFact:
    item_id: int
    quantity: int
    provenance: str


@dataclass(frozen=True)
class DerivedOwnershipFact:
    item_id: int
    provenance: str


@dataclass(frozen=True)
class CompletionUpgrade:
    location_id: int
    perk_path: str
    mission_name: str


@dataclass(frozen=True)
class EffectiveOwnership:
    ap_item_ids: tuple[int, ...]
    reconciliation_item_ids: tuple[int, ...]
    derived_facts: tuple[DerivedOwnershipFact, ...]
    vanilla_dash: bool
    blood_punch_upgrades: tuple[CompletionUpgrade, ...]
    materialization_fingerprint: str
    authored_tag_prerequisites: AuthoredTagPrerequisites
