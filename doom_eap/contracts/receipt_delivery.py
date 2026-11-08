"""Immutable receipt planning and publication facts; no runtime discovery."""

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from doom_eap.contracts.runtime_context import RuntimeContext

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


@dataclass(frozen=True)
class MaterializationScope:
    room_seed_name: str
    team: int
    slot: int
    state_key: str
    materialization_lease: str | None
    evidence_epoch: int | None
    reason: str
    dedupe_identity: str
    manual: bool = False


SUPPORT_RUNE_CAPABILITY = "support_runes_v1"
TAG_SPECIAL_CAPABILITY = "tag_special_v1"
SUPPORT_RUNE_IDS = frozenset({7770145, 7770146, 7770147})
TAG_SPECIAL_IDS = frozenset({7770009, 7770902})
CRUCIBLE_ID = 7770007
GATE_KEY_TO_MAP = {
    7770148: "e4m1_rig",
    7770149: "e4m3_mcity",
    7770150: "e1m2_war",
    7770151: "e1m3_cult",
    7770152: "e2m1_nest",
    7770153: "e2m2_base",
    7770154: "e2m3_core",
    7770155: "e3m1_slayer",
}


def _context_supports_item(context, item_id):
    if item_id in SUPPORT_RUNE_IDS:
        return context.supports(SUPPORT_RUNE_CAPABILITY)
    if item_id in TAG_SPECIAL_IDS:
        return context.supports(TAG_SPECIAL_CAPABILITY)
    if item_id == 7770901:
        return context.supports("special_weapon_v1")
    if item_id == CRUCIBLE_ID:
        return context.supports("crucible_v1")
    return True


def context_item_ids(context: RuntimeContext, received_item_ids: Iterable[int]) -> tuple[int, ...]:
    """Select only materializable owned IDs for active context."""
    selected = []
    for item_id in received_item_ids:
        if not _context_supports_item(context, item_id):
            continue
        if item_id in GATE_KEY_TO_MAP:
            target_map = GATE_KEY_TO_MAP[item_id]
            if target_map not in context.map_keys:
                continue
        selected.append(item_id)
    return tuple(selected)


def support_rune_commands(received_item_ids: Iterable[int], context: RuntimeContext) -> tuple[int, ...]:
    if not context.supports(SUPPORT_RUNE_CAPABILITY):
        return ()
    return tuple(
        item_id
        for item_id in sorted(set(received_item_ids))
        if item_id in SUPPORT_RUNE_IDS
    )


MAP_ENTITY_SAFE = "MAP_ENTITY_SAFE"
TRANSIENT_EFFECT = "TRANSIENT_EFFECT"
CHECKED_VISUAL_HIDE = "CHECKED_VISUAL_HIDE"
FAST_TRAVEL_UNLOCK = "FAST_TRAVEL_UNLOCK"


def valid_materialization_epoch(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9]+:[0-9]+", value) is not None


def build_materialization_epoch(native_epoch, marker_mtime_ns):
    if (
        isinstance(native_epoch, bool)
        or not isinstance(native_epoch, int)
        or isinstance(marker_mtime_ns, bool)
        or not isinstance(marker_mtime_ns, int)
        or native_epoch < 0
        or marker_mtime_ns < 0
    ):
        return None
    return f"{native_epoch}:{marker_mtime_ns}"


def stable_command_key(prefix, *logical_components):
    """Return bounded ID for logical coalescing identity."""
    if not isinstance(prefix, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", prefix) is None:
        raise ValueError("Invalid command key prefix")
    canonical = json.dumps(
        logical_components,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:20]}"


def queue_session_namespace(state_key):
    """Opaque durable queue namespace derived from room identity."""
    if not isinstance(state_key, str) or not state_key:
        return None
    return hashlib.sha256(state_key.encode("utf-8")).hexdigest()[:16]


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
