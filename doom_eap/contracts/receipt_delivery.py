"""Immutable receipt planning and publication facts; no runtime discovery."""
from dataclasses import dataclass

NEW_RECEIPT = "new_receipt"
HISTORICAL_OWNERSHIP = "historical_ownership"
RECONCILIATION_REPAIR = "reconciliation_repair"
PRESENTATION_REPAIR = "presentation_repair"


@dataclass(frozen=True)
class ReceiptIntent:
    item_id: int
    item_index: int | None
    intent: str = HISTORICAL_OWNERSHIP
    include_notification: bool | None = None
    classification: int | None = None
    suppress_local_toast: bool = True



@dataclass(frozen=True)
class ReceiptFeedbackFacts:
    owned_count: int | None
    processed_boundary: int
    source_location: object
    checked_ready: bool
    checked: frozenset[int]
    local_locations: frozenset[int]
    placements: frozenset[int]

    @property
    def notification_slot(self):
        return ("a", "b")[(self.owned_count - 1) % 2] if self.owned_count else None



@dataclass(frozen=True)
class ReceiptPlan:
    commands: tuple[str, ...] | None
    description: str



@dataclass(frozen=True)
class MappingRepair:
    action: str
    item_index: int | None = None
    item_id: int | None = None



@dataclass(frozen=True)
class ReceiptPublicationScope:
    state_key: str | None
    current_map: str | None
    save_slot: str | None
    bridge_revision: str
    protocol_version: int
    packet_received_ns: int | None = None
    materialization_lease: str | None = None
    context_identity: str | None = None

