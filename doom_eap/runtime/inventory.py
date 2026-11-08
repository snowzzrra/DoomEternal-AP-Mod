from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import struct
import time
import uuid
from collections import Counter, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from doom_eap.contracts.foundation import (
    MASTERY_ITEM_BITS,
    DeliveryCommand,
    classify_item_definition,
)
from doom_eap.contracts.foundation import compile_item_delivery_plan as compile_item_delivery_plan
from doom_eap.contracts.inventory_domain import (
    AUTHORED_TAG_PREREQUISITES,
    MAX_CAPACITY_TIER,
    MISSING,
    OWNED,
    PERMANENT_PERK_ITEM_IDS,
    UNKNOWN,
    CompletionUpgrade,
    Currency,
    CurrencyGrant,
    CurrencyGrantResult,
    CurrencyObservation,
    CurrencyOutcome,
    CurrencyValue,
    DerivedOwnershipFact,
    EffectExpectation,
    EffectiveOwnership,
    EffectProof,
    EvidenceScope,
    InventoryEffect,
    InventoryObservation,
    InventoryReadback,
    ItemObservation,
    MasteryEffect,
    MeatHookPerk,
    MissionAccess,
    MissionAccessObservation,
    NativeEffectEvidence,
    PerkStages,
    ProgressionObservation,
    ReceiptSessionToken,
    SaveReadiness,
    SlayerKey,
    StartingMaterializationFact,
    SuitStage,
)
from doom_eap.contracts.receipt_delivery import (
    GATE_KEY_TO_MAP,
    HISTORICAL_OWNERSHIP,
    NEW_RECEIPT,
    PRESENTATION_REPAIR,
    RECONCILIATION_REPAIR,
    SUPPORT_RUNE_IDS,
    TAG_SPECIAL_CAPABILITY,
    MaterializationScope,
    context_item_ids,
    stable_command_key,
    support_rune_commands,
)
from doom_eap.contracts.runtime_context import RuntimeContext, canonical_map_name
from doom_eap.runtime import sentinel_protocol_generated as codec
from doom_eap.runtime.ledger import CommandState, NetworkReceipt, Resolution
from doom_eap.runtime.native_transport import NativeTransport, TransportError, WireError
from doom_eap.runtime.observers import CONTEXT_BY_IDENTITY

REPLAY_IDEMPOTENT = "replay_idempotent"
REPLAY_MANUAL_ONLY = "replay_manual_only"
SPECIAL_PROGRESSIVE = "special_progressive"
NEVER_REPLAY = "never_replay"
AP_RECEIPT_FEEDBACK = "ap"
NATIVE_ONLY_RECEIPT_FEEDBACK = "native_only"
DELIVERY_INTENTS = frozenset(
    {
        NEW_RECEIPT,
        HISTORICAL_OWNERSHIP,
        RECONCILIATION_REPAIR,
        PRESENTATION_REPAIR,
    }
)
CLIENT_STATE_VERSION = 2
PACKET_TIMING_RANGE_LIMIT = 256
SUPPORTED_POLICIES = frozenset(
    {REPLAY_IDEMPOTENT, REPLAY_MANUAL_ONLY, SPECIAL_PROGRESSIVE, NEVER_REPLAY}
)
SUPPORTED_RECEIPT_FEEDBACK = frozenset(
    {AP_RECEIPT_FEEDBACK, NATIVE_ONLY_RECEIPT_FEEDBACK}
)
MATERIALIZATION_EPOCH_PATTERN = re.compile(r"[0-9]+:[0-9]+")
_RUNE_COMMAND = re.compile(
    r"ai_ScriptCmdEnt player1 givePlayerPerk (?P<perk>perk/player/runes/[A-Za-z0-9_./-]+)"
)
NORMAL_RUNES = (7770085, 7770086, 7770087, 7770089, 7770090, 7770091, 7770093, 7770094, 7770095)
ITEM_ID = 7770903
AMOUNT = 3


class StartingOwnershipError(ValueError):
    pass


def load_starting_ownership(starting, item_identity):
    if not isinstance(starting, dict) or set(starting) != {"items", "weapon_item_id"}:
        raise StartingOwnershipError("starting must contain items and weapon_item_id")
    rows = starting["items"]
    if not isinstance(rows, list) or len(rows) > len(item_identity):
        raise StartingOwnershipError("starting.items must be a bounded list")
    counts = {}
    for row in rows:
        item_id, quantity = _starting_item_pair(row, item_identity)
        if item_id in counts:
            raise StartingOwnershipError("starting.items contains a repeated item")
        counts[item_id] = quantity
    weapon = starting["weapon_item_id"]
    if type(weapon) is not int:
        raise StartingOwnershipError("starting.weapon_item_id must be a starting weapon")
    _starting_weapon(weapon)
    if counts.get(weapon) != 1:
        raise StartingOwnershipError("starting weapon must have exactly one owned copy")
    return tuple(StartingMaterializationFact(item_id, count, "starting") for item_id, count in counts.items())


class ReceiptSession:
    """Process-local receipt rebind authority, independent of gameplay epochs."""

    def __init__(self, *, processed_boundary=0):
        self._generation = 0
        self._processed_boundary = processed_boundary
        self._starting_ownership = ()
        self._packet_ranges = deque(maxlen=PACKET_TIMING_RANGE_LIMIT)

    def observe_packet(self, start_index, item_count, authoritative_count, timestamp_ns):
        accepted_start = (
            start_index
            if isinstance(start_index, int) and not isinstance(start_index, bool) and start_index >= 0
            else None
        )
        if accepted_start is None:
            return max(0, authoritative_count - item_count), False
        accepted = (
            accepted_start <= authoritative_count
            and accepted_start + item_count == authoritative_count
        )
        if accepted and accepted_start == 0:
            self._packet_ranges.clear()
        if accepted and item_count:
            self._packet_ranges.append((
                accepted_start, accepted_start + item_count, timestamp_ns,
                accepted_start >= self._processed_boundary,
            ))
        return start_index, accepted

    def packet_is_live_tail(self, receipt_index):
        for start, end, _timestamp_ns, live in reversed(self._packet_ranges):
            if start <= receipt_index < end:
                return live is True
        return False

    def clear_packet_ranges(self):
        self._packet_ranges.clear()

    @property
    def starting_ownership(self):
        return self._starting_ownership

    @property
    def resolved_starting_ownership(self):
        return self._starting_ownership

    def configure_starting_ownership(self, slot_data):
        self._starting_ownership = load_starting_ownership(slot_data.get("starting"), starting_item_identity())

    def owned_count(self, item_id, received):
        starting = sum(fact.quantity for fact in self.starting_ownership if fact.item_id == item_id)
        return starting + received

    def resolved_starting_count(self, item_id):
        return sum(fact.quantity for fact in self.resolved_starting_ownership if fact.item_id == item_id)

    def ownership_item_ids(self, received, definitions):
        starting = self._starting_ownership
        return tuple(item_id for fact in starting for item_id in _persistent_start_ids(fact, definitions)) + tuple(received)

    @property
    def processed_boundary(self):
        return self._processed_boundary

    def restore_boundary(self, processed_boundary):
        self._processed_boundary = processed_boundary

    def advance(self):
        self._processed_boundary += 1

    def begin_rebind(self):
        self._generation += 1

    def capture(self, state_key):
        return ReceiptSessionToken(state_key, self._generation)

    def is_current(self, token, state_key):
        return token.state_key == state_key and token.generation == self._generation

@dataclass(frozen=True)
class ReplayPolicy:
    item_id: int
    name: str
    policy: str
    receipt_feedback: str = AP_RECEIPT_FEEDBACK


@dataclass(frozen=True)
class ReconciliationCommand:
    item_id: int
    name: str
    policy: str
    stage: int
    command_key: str
    command: str
    description: str


@dataclass(frozen=True)
class ReconciliationSelection:
    item_id: int
    name: str
    policy: str
    received_count: int
    commands: tuple[str, ...]


@dataclass(frozen=True)
class ReconciliationPlan:
    commands: tuple[ReconciliationCommand, ...]
    selections: tuple[ReconciliationSelection, ...]
    replayed: int
    special_stages: int
    skipped_never_replay: int
    skipped_unproven: int = 0
    skipped_manual_replay: int = 0
    entitlement_item_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ObservedReceipt:
    """Stable, side-effect-free view of one authoritative receipt."""

    index: int
    item_id: int
    receipt_id: str | None


@dataclass(frozen=True)
class ReceivedItemsObservation:
    """Classification of one ReceivedItems snapshot against durable progress."""

    historical: tuple[ObservedReceipt, ...]
    new: tuple[ObservedReceipt, ...]
    duplicates: tuple[ObservedReceipt, ...]
    processed_boundary: int
    highest_observed_index: int
    receipt_item_ids: tuple[int, ...]
    historical_item_ids: tuple[int, ...]
    new_item_ids: tuple[int, ...]
    receipt_ids: tuple[str, ...]

    @property
    def next_boundary(self) -> int:
        """Highest authoritative list position that can be acknowledged."""
        return self.highest_observed_index + 1

    @staticmethod
    def _deduplicated_item_ids(receipts: Iterable[ObservedReceipt]) -> tuple[int, ...]:
        return tuple(receipt.item_id for receipt in receipts)

    @property
    def authoritative_item_ids(self) -> tuple[int, ...]:
        """Item IDs from historical and currently visible receipts."""
        return self._deduplicated_item_ids((*self.historical, *self.new))

    @property
    def historical_authoritative_item_ids(self) -> tuple[int, ...]:
        """Processed ownership only, preserving authoritative receipt order."""
        return self._deduplicated_item_ids(self.historical)


def receipt_item_ids(received_items) -> tuple[int, ...]:
    """Snapshot indexed AP IDs without deduplicating legitimate occurrences."""
    return tuple(receipt.item for receipt in received_items)


def progressive_receipt_stage(item_ids, item_id, item_index, excluded_indices, definition, *, starting_count=0):
    """Resolve a stage for the caller's validated nonnegative receipt index."""
    effective_count = sum(
        1 for index, received_id in enumerate(item_ids[:item_index + 1])
        if index not in excluded_indices and received_id == item_id
    )
    stage = max(starting_count + effective_count - 1, 0)
    if (
        isinstance(definition, dict)
        and definition.get("type") in {"progressive_perk", "progressive_item"}
    ):
        perks = definition.get("perks")
        stage = min(stage, len(perks) - 1) if isinstance(perks, list) and perks else 0
    return stage


def fresh_receipt_owned_count(item_ids, item_id, item_index, boundary, excluded_indices):
    if (
        isinstance(item_index, bool)
        or not isinstance(item_index, int)
        or item_index < boundary
        or item_index >= len(item_ids)
        or item_ids[item_index] != item_id
    ):
        return None
    return sum(
        1 for index, received_id in enumerate(item_ids[boundary:item_index + 1], boundary)
        if index not in excluded_indices and received_id == item_id
    )


def receipt_item_id(receipt: Any) -> int:
    """Extract an AP item ID without depending on CommonClient classes."""
    if isinstance(receipt, int) and not isinstance(receipt, bool):
        return receipt
    value = _field(receipt, "item")
    if value is None:
        value = _field(receipt, "item_id")
    if value is None and isinstance(receipt, tuple | list) and receipt:
        value = receipt[0]
    if value is None or isinstance(value, bool):
        raise ValueError("received item ID must be an integer")
    try:
        item_id = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"received item has invalid item ID: {value!r}") from error
    return item_id


def receipt_identity(receipt: Any) -> str | None:
    """Return deterministic packet identity when receipt has AP packet fields."""
    explicit = _field(receipt, "receipt_id")
    if explicit is not None:
        value = _canonical_value(explicit)
        return "receipt:" + json.dumps(value, sort_keys=True, separators=(",", ":"))

    fields = (
        _field(receipt, "location"),
        _field(receipt, "player"),
        receipt_item_id(receipt),
        _field(receipt, "flags"),
    )
    if all(value is None for value in (fields[0], fields[1], fields[3])):
        return None
    return "network:" + json.dumps(
        _canonical_value(fields), sort_keys=True, separators=(",", ":")
    )


def observe_received_items(
    received_items: Iterable[Any],
    processed_boundary: int,
    processed_receipt_ids: Iterable[str] | Mapping[str, int] = (),
) -> ReceivedItemsObservation:
    """Classify authoritative history without delivering or mutating state.

    Positions before durable ``processed_boundary`` are historical. Positions at
    or after it are ordered new receipts unless their stable packet identity was
    already observed. Item IDs alone are intentionally not de-duplicated because
    two equal IDs can be two legitimate AP receipts.
    """
    if isinstance(processed_boundary, bool) or not isinstance(processed_boundary, int):
        raise ValueError("processed boundary must be a non-negative integer")
    if processed_boundary < 0:
        raise ValueError("processed boundary must be a non-negative integer")

    snapshot = tuple(received_items)
    known_occurrences = _known_receipt_occurrences(processed_receipt_ids)
    snapshot_occurrences: Counter[str] = Counter()
    historical: list[ObservedReceipt] = []
    new: list[ObservedReceipt] = []
    duplicates: list[ObservedReceipt] = []
    all_item_ids: list[int] = []
    historical_item_ids: list[int] = []
    new_item_ids: list[int] = []
    receipt_ids: list[str] = []

    for index, receipt in enumerate(snapshot):
        item_id = receipt_item_id(receipt)
        stable_id = receipt_identity(receipt)
        observed = ObservedReceipt(index, item_id, stable_id)
        all_item_ids.append(item_id)
        if stable_id is not None:
            receipt_ids.append(stable_id)
            snapshot_occurrences[stable_id] += 1
        if index < processed_boundary:
            historical.append(observed)
            historical_item_ids.append(item_id)
            continue
        if (
            stable_id is not None
            and snapshot_occurrences[stable_id] <= known_occurrences.get(stable_id, 0)
        ):
            duplicates.append(observed)
            continue
        new.append(observed)
        new_item_ids.append(item_id)

    return ReceivedItemsObservation(
        tuple(historical),
        tuple(new),
        tuple(duplicates),
        processed_boundary,
        len(snapshot) - 1,
        tuple(all_item_ids),
        tuple(historical_item_ids),
        tuple(new_item_ids),
        tuple(receipt_ids),
    )


def receipt_history_fingerprint(received_items: Iterable[Any]) -> str:
    """Hash authoritative receipt order and stable packet identity."""
    records = []
    for receipt in received_items:
        stable_id = receipt_identity(receipt)
        records.append(
            {
                "index": len(records),
                "item_id": receipt_item_id(receipt),
                "receipt_id": stable_id,
            }
        )
    payload = json.dumps(records, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def effective_ownership(
    processed_receipts, *, randomize_chainsaw, randomize_dash,
    checked_locations, local_checked_locations, server_checked_ready,
    hell_on_earth_locations, exultia_complete_location, slot, starting_item_ids=(),
) -> EffectiveOwnership:
    """Project vanilla facts without manufacturing authoritative AP receipts."""
    receipts = tuple(processed_receipts)
    item_ids = tuple(starting_item_ids) + tuple(receipt_item_id(receipt) for receipt in receipts)
    vanilla_chainsaw, vanilla_dash = _vanilla_ownership(
        item_ids, checked_locations, local_checked_locations, hell_on_earth_locations,
        randomize_chainsaw, randomize_dash, server_checked_ready, exultia_complete_location,
    )
    facts = []
    if vanilla_chainsaw:
        facts.append(DerivedOwnershipFact(7770010, "unrandomized_chainsaw_server_or_local_hoe_check"))
    if vanilla_dash:
        facts.append(DerivedOwnershipFact(7770015, "unrandomized_dash_server_mission_complete"))
    upgrades = tuple(
        CompletionUpgrade(location_id, perk_path, mission_name)
        for location_id, perk_path, mission_name in (
            (7770162, "perk/player/blood_punch/area_of_effect", "Doom Hunter Base"),
            (7770290, "perk/player/blood_punch/ai_charge_rate", "Sentinel Prime"),
            (7770411, "perk/player/blood_punch/max_charges", "Urdak"),
        )
        if 7770014 in item_ids and location_id in checked_locations
    )
    fingerprint = _ownership_fingerprint(receipts, starting_item_ids, vanilla_chainsaw, vanilla_dash, slot)
    return EffectiveOwnership(
        item_ids, item_ids + ((7770010,) if vanilla_chainsaw else ())
        + ((7770015,) if vanilla_dash and 7770015 not in item_ids else ()), tuple(facts),
        vanilla_dash, upgrades, fingerprint, AUTHORED_TAG_PREREQUISITES,
    )


def starting_item_identity():
    path = Path(__file__).resolve().parents[2] / "data/devinv_start_mapping.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        return {int(item_id): entry for item_id, entry in document["items"].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise StartingOwnershipError("starting item mapping is unavailable") from error


def processed_receipt_counts(session):
    """Read the occurrence ledger, retaining existing malformed-container repair."""
    history = session.setdefault("receipt_history", {})
    if not isinstance(history, dict):
        history = {}
        session["receipt_history"] = history
    receipt_counts = history.get("receipt_counts", {})
    if not isinstance(receipt_counts, dict):
        receipt_counts = {}
        history["receipt_counts"] = receipt_counts
    return {
        value: count for value, count in receipt_counts.items()
        if isinstance(value, str) and value and isinstance(count, int)
        and not isinstance(count, bool) and count > 0
    }


def validate_session_receipt_prefix(session, received_items, boundary):
    history = session.setdefault("receipt_history", {})
    return validate_receipt_history_prefix(received_items, boundary, history)


def project_receipt_history(session, received_items, boundary, observation):
    """Project a validated observation without replacing accumulated occurrences."""
    history = session.setdefault("receipt_history", {})
    owned_item_ids = sorted(set(observation.receipt_item_ids))
    ordered_receipt_ids = [
        receipt_id
        for receipt_id in (receipt_identity(receipt) for receipt in received_items[:boundary])
        if receipt_id is not None
    ]
    changed = (
        history.get("processed_boundary") != boundary
        or history.get("highest_observed_index") != observation.highest_observed_index
        or history.get("owned_item_ids") != owned_item_ids
        or history.get("receipt_item_ids") != list(observation.receipt_item_ids[:boundary])
        or history.get("receipt_ids") != ordered_receipt_ids
    )
    history["processed_boundary"] = boundary
    history["highest_observed_index"] = observation.highest_observed_index
    history["owned_item_ids"] = owned_item_ids
    history["receipt_item_ids"] = list(observation.receipt_item_ids[:boundary])
    history["receipt_ids"] = ordered_receipt_ids
    return changed


def project_receipt_boundary(session, processed_items):
    """Own the receipt fields projected at the existing session commit boundary."""
    session["processed_items"] = processed_items
    history = session.setdefault("receipt_history", {})
    if not isinstance(history, dict):
        history = {}
        session["receipt_history"] = history
    history["processed_boundary"] = processed_items


def validate_receipt_history_prefix(
    received_items: Iterable[Any],
    processed_boundary: int,
    history: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    """Validate durable receipt history against current ordered AP receipts.

    A boundary is safe only when its durable ordered prefix agrees with the
    current ``ReceivedItems`` prefix.  Item IDs are retained separately from
    packet identities so duplicate IDs remain ordered and distinguishable.
    """
    if isinstance(processed_boundary, bool) or not isinstance(processed_boundary, int):
        return False, "processed boundary must be a non-negative integer"
    if processed_boundary < 0:
        return False, "processed boundary must be a non-negative integer"
    snapshot = tuple(received_items)
    if processed_boundary > len(snapshot):
        return False, (
            f"durable boundary {processed_boundary} exceeds received history "
            f"length {len(snapshot)}"
        )
    if not isinstance(history, Mapping):
        return True, "no durable receipt prefix recorded"

    expected_item_ids = _safe_item_ids(history.get("receipt_item_ids"))
    expected_receipt_ids = _safe_receipt_ids(history.get("receipt_ids"))
    expected_receipt_counts = _safe_receipt_counts(history.get("receipt_counts"))
    actual_item_ids = tuple(receipt_item_id(item) for item in snapshot[:processed_boundary])
    actual_receipt_ids = tuple(
        identity
        for identity in (receipt_identity(item) for item in snapshot[:processed_boundary])
        if identity is not None
    )

    return _compare_receipt_prefix(processed_boundary, expected_item_ids, expected_receipt_ids,
                                   expected_receipt_counts, actual_item_ids, actual_receipt_ids)


def migrate_legacy_session_key(
    sessions: dict[str, Any],
    *,
    seed_name: Any,
    team: Any,
    slot: Any,
    generation: Any,
) -> tuple[str | None, str | None]:
    """Move a pre-generation session only when its navigation proves the generation."""
    if not _valid_legacy_identity(sessions, seed_name, team, slot, generation):
        return None, None

    state_key = f"{seed_name}:{team}:{slot}:{generation}"
    legacy_key = f"{seed_name}:{team}:{slot}"
    legacy_session = sessions.get(legacy_key)
    if (
        state_key not in sessions
        and isinstance(legacy_session, dict)
        and legacy_session.get("campaign_navigation", {}).get("generation") == generation
    ):
        sessions[state_key] = sessions.pop(legacy_key)
        return state_key, legacy_key
    return state_key, None


def normalize_session_state(session: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return safe v2 session state while preserving unrelated legacy fields."""
    if not isinstance(session, Mapping):
        session = {}
    normalized = copy.deepcopy(dict(session))
    _normalize_physical_gates(normalized)
    processed = _safe_nonnegative_int(normalized.get("processed_items"), 0)
    normalized["processed_items"] = processed
    _normalize_deferred_frag(normalized)

    _normalize_receipt_history(normalized, processed)

    resync = normalized.get("item_resync")
    normalized["item_resync"] = copy.deepcopy(dict(resync)) if isinstance(resync, Mapping) else {}

    _normalize_bootstrap(normalized)
    if not isinstance(normalized.get("save_slot_observations"), Mapping):
        normalized["save_slot_observations"] = {}
    _normalize_automap_cleanup(normalized)
    if "goal_sent" in normalized and not isinstance(normalized["goal_sent"], bool):
        normalized["goal_sent"] = False
    if "cultist_autosave_path" in normalized and not (
        normalized["cultist_autosave_path"] is None
        or isinstance(normalized["cultist_autosave_path"], str)
    ):
        normalized["cultist_autosave_path"] = None
    _normalize_replay_history(normalized)
    return normalized


def default_session_state() -> dict[str, Any]:
    return normalize_session_state({})


def migrate_client_state(raw_state: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Migrate v1 state deterministically; reject malformed top-level state."""
    if not isinstance(raw_state, Mapping):
        raise ValueError("client state must be an object")
    version = raw_state.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("unsupported client state format")
    if version not in (1, CLIENT_STATE_VERSION):
        raise ValueError("unsupported client state version")
    sessions = raw_state.get("sessions")
    if not isinstance(sessions, Mapping):
        raise ValueError("client state sessions must be an object")

    state = copy.deepcopy(dict(raw_state))
    state["version"] = CLIENT_STATE_VERSION
    state["sessions"] = {
        key: normalize_session_state(sessions[key])
        for key in sorted(sessions, key=lambda candidate: str(candidate))
        if isinstance(key, str) and key
    }
    return state, version == 1


def load_policy_registry(
    source: Path | Mapping[str, Any], definitions: Mapping[int, Any]
) -> dict[int, ReplayPolicy]:
    """Load an exact numeric registry; missing, extra and unknown policy fail."""
    raw = _read_registry(source)
    items = raw.get("items")
    if not isinstance(items, dict):
        raise ValueError("item replay policy registry items must be an object")
    parsed: dict[int, ReplayPolicy] = {}
    for raw_id, entry in items.items():
        policy = _parse_replay_policy(raw_id, entry)
        parsed[policy.item_id] = policy

    missing = sorted(set(definitions) - set(parsed))
    extra = sorted(set(parsed) - set(definitions))
    if missing:
        raise ValueError("missing policy for active item ID(s): " + ", ".join(map(str, missing)))
    if extra:
        raise ValueError("policy exists for inactive item ID(s): " + ", ".join(map(str, extra)))
    return parsed


class ReconciliationBlock(str, Enum):
    RUNTIME_NOT_READY = 'runtime effects are not level-ready'
    NATIVE_APPLICATION_PENDING = 'materialization queued; awaiting native application'

    def __str__(self):
        return self.value


@dataclass(frozen=True)
class MaterializationPlan:
    scope: MaterializationScope
    reconciliation: ReconciliationPlan
    raw_operation_count: int
    support_rune_jobs: int
    selected_special_stage: int
    special_diagnostic: tuple | None


class MaterializationPlanError(ValueError):
    def __init__(self, message, *, blocked=True):
        super().__init__(message)
        self.blocked = blocked


def authored_effects_allowed(*, evidence, campaign, epoch, lease, process_running,
                             item_ready, room_identity, connected, queue_authoritative):
    return bool(campaign not in {None, "Base"} and _authored_epoch_allowed(evidence, epoch, lease)
                and process_running and item_ready and room_identity and connected and queue_authoritative)


def reconciliation_session_block(*, item_ready, require_connection, connected, team, slot, seed):
    import re
    if not item_ready:
        return "item state is not ready"
    if require_connection and not connected:
        return "connected AP session required"
    if team is None or slot is None:
        return "current AP team/slot identity is incomplete"
    if not seed or not re.fullmatch(r"[A-Za-z0-9_.-]+", str(seed)):
        return "seed identity is missing or unsafe for deterministic command keys"
    return None


def reconciliation_runtime_block(*, evidence, marker, runtime_ready, campaign, active_slot,
                                 active_epoch, active_map, supported_maps, boundary, history_length):
    if evidence is None or evidence.state != "gameplay":
        return "confirmed gameplay epoch required; menus are not eligible"
    if marker is None:
        return "active AP map marker is unavailable"
    if not runtime_ready:
        return ReconciliationBlock.RUNTIME_NOT_READY
    if campaign in {None, "Base"}:
        if active_slot != evidence.slot_directory:
            return "gameplay evidence does not match the active save slot"
        if active_epoch != evidence.epoch:
            return "gameplay evidence does not match the active epoch"
    if active_map not in supported_maps:
        return "active map has no ap_rpc_v3 reconciliation entities"
    if boundary > history_length:
        return "authoritative received-item history is incomplete"
    return None


@dataclass(frozen=True)
class RuneNativeState:
    available_perks: frozenset[str]
    active_perks: frozenset[str]
    registered_runes: frozenset[str]
    equipped_slots: tuple[str | None, str | None, str | None]
    page_unlocked: bool | None
    save_slot: str
    evidence_epoch: int | str
    source_path: str | None = None
    source_mtime_ns: int | None = None

    @property
    def observed(self) -> bool:
        return bool(
            self.available_perks
            or self.active_perks
            or self.registered_runes
            or any(self.equipped_slots)
            or self.page_unlocked is not None
        )

    @classmethod
    def from_game_details(
        cls,
        details: Mapping[str, Any] | None,
        *,
        save_slot: str,
        evidence_epoch: int | str,
    ) -> RuneNativeState:
        source = details if isinstance(details, Mapping) else {}
        return cls(
            available_perks=_field_values(source, "availablePerkDeclName"),
            active_perks=_field_values(source, "activePerkDeclName"),
            registered_runes=_field_values(source, "runeName"),
            equipped_slots=(
                _slot_value(source, 0),
                _slot_value(source, 1),
                _slot_value(source, 2),
            ),
            page_unlocked=_optional_bool(source.get("STAT_RUNE_PAGE_UNLOCKED")),
            save_slot=save_slot,
            evidence_epoch=evidence_epoch,
            source_path=_clean_decl(source.get("_path")),
            source_mtime_ns=(
                source.get("_mtime_ns")
                if isinstance(source.get("_mtime_ns"), int)
                and not isinstance(source.get("_mtime_ns"), bool)
                else None
            ),
        )


@dataclass(frozen=True)
class RunePlanEntry:
    item_id: int
    perk: str
    disposition: str
    reason: str


@dataclass(frozen=True)
class RuneReconciliationPlan:
    entries: tuple[RunePlanEntry, ...]
    fingerprint: str
    status: str
    commands: tuple[Any, ...] = ()

    @property
    def repairs(self) -> tuple[RunePlanEntry, ...]:
        return tuple(entry for entry in self.entries if entry.disposition == "repair_candidate")

    @property
    def noops(self) -> tuple[RunePlanEntry, ...]:
        return tuple(entry for entry in self.entries if entry.disposition == "noop")


def rune_item_perk_mapping(
    definitions: Mapping[int, Any], rune_item_ids: Iterable[int]
) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for item_id in sorted(set(rune_item_ids)):
        definition = definitions.get(item_id)
        if not isinstance(definition, str):
            raise ValueError(f"Rune item {item_id} does not have a direct perk command")
        match = _RUNE_COMMAND.fullmatch(definition.strip())
        if match is None:
            raise ValueError(f"Rune item {item_id} has an unsupported perk command")
        mapping[item_id] = match.group("perk")
    return mapping


def compile_rune_reconciliation_plan(
    ap_owned_item_ids: Iterable[int],
    native: RuneNativeState,
    mapping: Mapping[int, str],
    *,
    expected_rune_item_ids: Iterable[int] | None = None,
) -> RuneReconciliationPlan:


    owned = set(ap_owned_item_ids)
    expected = set(mapping) if expected_rune_item_ids is None else set(expected_rune_item_ids)
    unknown = sorted((owned & expected) - set(mapping))
    if unknown:
        raise ValueError("AP-owned Rune has no mapping: " + ", ".join(map(str, unknown)))
    if any(isinstance(item_id, bool) or not isinstance(item_id, int) for item_id in owned):
        raise ValueError("AP Rune ownership contains a non-integer item ID")

    return compare_inventory(
        InventoryOwnership(tuple(owned)), runes=native, rune_mapping=mapping,
        expected_rune_item_ids=expected,
    ).runes


@dataclass(frozen=True)
class InventoryAcquisition:
    source_key: str
    item_id: int
    live: bool = False


@dataclass(frozen=True)
class InventoryOwnership:
    reconciliation_item_ids: tuple[int, ...]
    blood_punch_upgrades: tuple[CompletionUpgrade, ...] = ()
    acquisitions: tuple[InventoryAcquisition, ...] = ()


def ownership_from_ledger(
    ingested_prefix: Iterable[NetworkReceipt],
    local_sources: Iterable[StartingMaterializationFact] = (),
    *, blood_punch_upgrades: tuple[CompletionUpgrade, ...] = (), live_receipt_indices=frozenset(),
) -> InventoryOwnership:
    """Project a complete ingested prefix and decoded local facts without synthetic receipts."""
    acquisitions = tuple(InventoryAcquisition(f"ap:{index}", receipt.item, index in live_receipt_indices)
                         for index, receipt in enumerate(ingested_prefix))
    acquisitions += tuple(InventoryAcquisition(f"local:{fact.provenance}:{fact.item_id}:{ordinal}", fact.item_id)
                          for fact in local_sources for ordinal in range(fact.quantity))
    return InventoryOwnership(
        tuple(acquisition.item_id for acquisition in acquisitions), blood_punch_upgrades, acquisitions,
    )


@dataclass(frozen=True)
class InventoryObligation:
    item_id: int
    stage: int
    state: CommandState
    resolution: Resolution | None = None
    source_key: str | None = None


@dataclass(frozen=True)
class AcquisitionEffect:
    source_key: str
    item_id: int
    commands: tuple[DeliveryCommand, ...]
    transient: bool = False


@dataclass(frozen=True)
class InventoryDependency:
    item_id: int
    stage: int
    prerequisite_item_id: int
    prerequisite_stage: int = 0


class NativeEffectKind(Enum):
    MASTERY = "mastery"
    BLOOD_PUNCH = "blood_punch"


@dataclass(frozen=True)
class NativeInventoryEffect:
    item_id: int
    kind: NativeEffectKind
    stage: int
    mask: int


class WeaponPointsBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class InventoryPlan:
    materialization: MaterializationPlan | None = None
    reconciliation: ReconciliationPlan | None = None
    runes: RuneReconciliationPlan | None = None
    weapon_point_grants: tuple[int, ...] = ()
    unresolved_item_ids: tuple[int, ...] = ()
    weapon_points_error: WeaponPointsBlocked | None = None
    dependencies: tuple[InventoryDependency, ...] = ()
    native_effects: tuple[NativeInventoryEffect, ...] = ()
    acquisitions: tuple[AcquisitionEffect, ...] = ()


def coherent_evidence(before: EvidenceScope, after: EvidenceScope) -> bool:
    return before == after and before.readiness is SaveReadiness.READY


def effect_confirmed(expected: EffectExpectation, evidence: NativeEffectEvidence) -> bool:
    match expected.postcondition, evidence:
        case InventoryEffect(item_id, stage), InventoryReadback(before, after, observation):
            return (before == expected.scope and coherent_evidence(before, after)
                    and observation.confirms_repair(item_id, stage))
        case CurrencyGrant() as grant, CurrencyGrantResult() as result:
            return _currency_confirmed(expected, grant, result)
        case MissionAccess() as access, MissionAccessObservation() as observation:
            return _mission_confirmed(expected.scope, access, observation)
        case _, ProgressionObservation() as observation:
            if not coherent_evidence(observation.before_scope, observation.after_scope):
                return False
            return observation.after_scope == expected.scope and _progression_confirmed(
                expected.postcondition, observation)
    return False


def currency_delta(result: CurrencyGrantResult) -> tuple[int, int] | None:
    before, after = result.before, result.after
    if before.balance is None or before.gained is None or after.balance is None or after.gained is None:
        return None
    if any(not 0 <= value <= 0x7fffffff for value in (before.balance, before.gained, after.balance, after.gained)):
        return None
    return after.balance - before.balance, after.gained - before.gained


def compare_inventory(
    ownership: InventoryOwnership,
    *, definitions: Mapping[int, Any] | None = None, policies: Mapping[int, ReplayPolicy] | None = None,
    observation: InventoryObservation | None = None, context: RuntimeContext | None = None,
    scope: MaterializationScope | None = None, special_mode: str = "the_crucible",
    selected_item_ids: frozenset[int] | None = None, replay_identity: tuple[str, int] | None = None,
    include_manual_replay: bool = False, runes: RuneNativeState | None = None,
    rune_mapping: Mapping[int, str] | None = None, expected_rune_item_ids: Iterable[int] | None = None,
    weapon_points: tuple[int, int | None] | None = None, obligations: tuple[InventoryObligation, ...] = (),
    restore_persistent_only: bool = False,
) -> InventoryPlan:
    materialization, reconciliation, unresolved = _compare_persistent(
        ownership, definitions, policies, observation, context, scope, special_mode,
        selected_item_ids, replay_identity, include_manual_replay,
    )
    rune_plan = _compare_runes(ownership, selected_item_ids, runes, rune_mapping, expected_rune_item_ids, obligations)
    persistent_obligations = _persistent_obligations(obligations, restore_persistent_only)
    materialization, reconciliation = _merge_plans(materialization, reconciliation, rune_plan, persistent_obligations)
    points = None if restore_persistent_only else weapon_points
    grants, points_error = _compare_weapon_points(points, obligations)
    native_effects = _pending_native_effects(ownership, observation, selected_item_ids, obligations)
    dependencies = _dependencies(reconciliation, native_effects)
    unresolved = _all_unresolved(ownership, observation, selected_item_ids, rune_plan, unresolved)
    acquisitions = _compare_acquisitions(ownership, context, selected_item_ids, restore_persistent_only,
                                         definitions, policies, obligations)
    return InventoryPlan(materialization, reconciliation, rune_plan, grants, unresolved,
                          points_error, dependencies, native_effects, acquisitions)


@dataclass(frozen=True)
class DeliverySnapshot:
    readback: InventoryReadback
    weapon_points_gained: int | None


async def observe_delivery(transport, scope, context, room_seed_name, epoch):
    """Read one generation through v2; discard every domain if its save or load changes."""
    namespace, binding = scope.namespace.encode("ascii"), UUID(scope.save_binding).bytes
    before = await transport.hello()
    inventory = await transport.observe(25, namespace, binding)
    special = await observe_optional(transport, 37, namespace, binding)
    runes = await observe_optional(transport, 33, namespace, binding)
    points = await observe_optional(transport, 18, namespace, binding)
    progression = await observe_optional(transport, 54, namespace, binding)
    after = await transport.hello()
    admission = await transport.observe(12, b"0" * 64, bytes(16))
    if (before["scope"] != after["scope"] or before["scope"] != transport.scope
            or admission is None or bytes(admission["save_binding"]) != binding or inventory is None):
        return None
    progress = None if progression is None else progression_observation(progression)
    if progress is not None and (progress.before_scope != scope or progress.after_scope != scope):
        return None
    items = _delivery_items(inventory, special, runes, progress, context)
    observation = InventoryObservation(room_seed_name, epoch, context.identity, context.campaign, items,
                                       process_id=scope.pid, provider_namespace=scope.namespace,
                                       stage_masks=SentinelInventoryObservation._stage_masks(inventory), progression=progress)
    gained = _gained_points(points)
    return DeliverySnapshot(InventoryReadback(scope, scope, observation), gained)


async def observe_optional(transport, command, namespace, binding, **payload):
    try:
        return await transport.observe(command, namespace, binding, **payload)
    except WireError as error:
        if error.result != 2:  # capability_unavailable
            raise
        return None


def evidence_scope(value):
    process = value["process"]
    return EvidenceScope(process["pid"], process["process_created"], str(UUID(bytes=bytes(process["instance_id"]))),
                         wire_text(value["namespace_id"]), str(UUID(bytes=bytes(value["save_binding"]))),
                         process["lifecycle_generation"], wire_text(value["map_name"]),
                         SaveReadiness.READY if value["readiness"] == 1 else SaveReadiness.UNKNOWN)


def currency_observation(result):
    return CurrencyObservation(evidence_scope(result["before_scope"]), evidence_scope(result["after_scope"]),
                               {Currency(result["currency"]): _currency_value(result, "after", 2)})


def currency_grant_result(result, reference):
    return CurrencyGrantResult(reference, evidence_scope(result["before_scope"]), evidence_scope(result["after_scope"]),
                               Currency(result["currency"]), _currency_value(result, "before", 1),
                               _currency_value(result, "after", 2), bool(result["flags"] & 4),
                               result["native_exception"], CurrencyOutcome(result["outcome"]))


def progression_observation(result):
    quantity = result["slayer_key_quantity"]
    return ProgressionObservation(evidence_scope(result["before_scope"]), evidence_scope(result["after_scope"]),
                                  PerkStages(result["suit_known"], result["suit_owned"]),
                                  quantity if quantity >= 0 else None,
                                  (EffectProof.UNPROVEN, EffectProof.PRESENT, EffectProof.ABSENT)[result["meat_hook"]])


def mission_access_observation(result):
    return MissionAccessObservation(evidence_scope(result["menu_scope"]), result["revision"], result["stage_id"],
                                    wire_text(result["map_name"]), bool(result["entry_usable"]),
                                    evidence_scope(result["destination_scope"]), result["destination_revision"],
                                    result["destination_stage_id"], bool(result["destination_ready"]))


class SentinelInventoryObservation:
    def __init__(self, context, logger, log_event):
        self.context = context
        self.logger = logger
        self.log_event = log_event
        self.last_logged = None

    def _native_inventory(self, link):
        link = link or self.context.items.native_game_link()
        return link, link.observe_inventory()

    def _read_special(self, link):
        if not hasattr(link, "observe_special_inventory"):
            return None
        try:
            return link.observe_special_inventory()
        except (RuntimeError, OSError, ValueError) as error:
            self.logger.info("SPECIAL_INVENTORY_OBSERVATION_DEFERRED detail=%s", error)
            return None

    def observe_inventory(self, *, room_seed_name, epoch, context_identity, campaign, link=None,
                          progression: ProgressionObservation | None = None,
                          currencies: CurrencyObservation | None = None):
        state_key = self.context.state_key
        try:
            link, result = self._native_inventory(link)
        except (RuntimeError, OSError, ValueError) as error:
            self.logger.info("INVENTORY_OBSERVATION_DEFERRED detail=%s", error)
            return None
        context = CONTEXT_BY_IDENTITY.get(context_identity)
        if result.get("_save_recovering") or not self._matches_context(result, context, campaign):
            return None
        if not _facts_match((progression, currencies), result, link):
            return None
        items = _weapons(result) | _equipment(result) | _capacities(result) | _perks(result)
        items.update(_gate_keys(result, context, campaign))
        if progression is not None:
            items.update(_progression_items(progression, context, campaign))
        special = self._read_special(link)
        if special is not None:
            if not self._same_snapshot(result, special):
                return None
            items.update(_special_items(special))
        if self.context.state_key != state_key:
            return None
        observation = InventoryObservation(
            room_seed_name, epoch, context_identity, campaign, items,
            state_key=state_key, process_id=link.pid, provider_namespace=link.namespace,
            timestamp_ns=time.time_ns(), native_scope=result.get("_observation_scope"),
            stage_masks=self._stage_masks(result),
            progression=progression, currencies=currencies,
        )
        self._log(observation, result)
        return observation

    @staticmethod
    def _stage_masks(result):
        upgrades = result.get("upgrades_after", 0xffffffff)
        return {} if upgrades == 0xffffffff else {7770904: (upgrades >> 11) & 15}

    @staticmethod
    def _matches_context(result, context, campaign):
        if not result.get("_observation_scope"):
            return True
        if context is None or context.campaign != campaign:
            return False
        return canonical_map_name(result.get("_observation_map")) in context.runtime_maps

    @staticmethod
    def _same_snapshot(inventory, other):
        if inventory.get("_save_recovering") or other.get("_save_recovering"):
            return False
        if inventory.get("_observation_save_binding") != other.get("_observation_save_binding"):
            return False
        if inventory.get("_observation_scope") != other.get("_observation_scope"):
            return False
        if inventory.get("_observation_scope") is None:
            return True
        return canonical_map_name(inventory.get("_observation_map")) == canonical_map_name(other.get("_observation_map"))

    def _log(self, observation, result):
        signature = (observation.state_key, observation.process_id, observation.epoch,
                     observation.context_identity, observation.native_scope,
                     tuple(observation.stage_masks.items()),
                     tuple(sorted((key, value.state, value.observed_stage) for key, value in observation.items.items())))
        if signature == self.last_logged:
            return
        self.log_event(
            "ITEM_NATIVE_INVENTORY", state_key=observation.state_key,
            namespace=observation.provider_namespace, process_id=observation.process_id, epoch=observation.epoch,
            context=observation.context_identity, rocket=observation.get_state(7770002),
            bfg=observation.get_state(7770006), ballista=observation.get_state(7770004),
            frag=observation.get_state(7770011), ice=observation.get_state(7770013), dash=observation.get_state(7770015),
            weapons_mask=result["weapons_after"], ice_bomb=result["ice_bomb_after"],
            dash_owned=result.get("dash_after", 255), slayer_key_owned=result.get("slayer_key_after", 255),
            capacity_tiers=tuple(result.get(f"{family}_tier_after", 255) for family in ("health", "armor", "ammo")),
            perks_known=str(result.get("perks_known", 0)), perks_owned=str(result.get("perks_owned", 0)),
            gate_keys={str(item_id): item.state for item_id, item in observation.items.items() if item_id in GATE_KEY_TO_MAP},
        )
        self.last_logged = signature


class NativeGameplayInactive(WeaponPointsBlocked):
    pass


def namespace_id(seed: str, team: int, slot: int, fingerprint: str) -> str:
    if not isinstance(seed, str) or not seed or type(team) is not int or not 0 <= team <= 0xffffffff or type(slot) is not int or not 1 <= slot <= 0xffffffff:
        raise WeaponPointsBlocked("Incomplete AP identity")
    _validate_namespace_fingerprint(fingerprint)
    digest = hashlib.sha256(b"sentinel-ap-session-identity")
    for field in (seed, str(team), str(slot), fingerprint):
        encoded = field.encode("utf-8")
        digest.update(struct.pack(">I", len(encoded)))
        digest.update(encoded)
    return digest.hexdigest()


class SentinelWeaponPoints:
    """Keep one v2 connection on the bridge's serialized native worker."""
    def __init__(self, probe: Path, pid: int, namespace: str, diagnostic=None, *, command_prefix=(), environment=None):
        self.probe, self.pid, self.namespace = probe.resolve(), pid, namespace
        self.diagnostic = diagnostic
        self.command_prefix, self.environment = command_prefix, environment
        self._runner = None
        self._transport = None
        self._diagnostic_signatures = {}

    def close(self):
        if self._runner is not None:
            self._runner.run_until_complete(self._transport.close())
            self._runner.run_until_complete(self._runner.shutdown_asyncgens())
            self._runner.run_until_complete(self._runner.shutdown_default_executor())
            self._runner.close()
            self._runner = self._transport = None

    def _diagnose(self, evidence):
        if not self.diagnostic:
            return
        key = (evidence["stage"], evidence["operation"])
        try:
            response = json.loads(evidence.get("stdout", "{}"))
        except ValueError:
            response = evidence.get("stdout")
        signature = json.dumps((evidence.get("predicate"), evidence.get("error"), evidence.get("returncode"),
                                evidence.get("argv"), diagnostic_value(response)), sort_keys=True)
        now = time.monotonic()
        previous = self._diagnostic_signatures.get(key)
        if previous is None or previous["signature"] != signature:
            self._diagnostic_signatures[key] = diagnostic_entry(evidence, previous, now, signature)
            self.diagnostic({**evidence, "first_seen": now, "last_seen": now, "count": 1})
            return
        previous.update(last_seen=now, count=previous["count"] + 1)
        if now - previous["emitted_at"] >= 60:
            self.diagnostic({"stage": key[0], "operation": key[1], "summary": "unchanged",
                **{field: previous[field] for field in ("first_seen", "last_seen", "count")}})
            previous["emitted_at"] = now

    def request(self, command_type, tag=0, payload=None):
        if self._runner is None:
            self._runner = asyncio.new_event_loop()
            executable = str(self.probe)
            if self.command_prefix:
                from doom_eap.runtime.bridge_config import windows_path
                executable = windows_path(self.probe)
            self._transport = NativeTransport((*self.command_prefix, executable), self.pid, env=self.environment)
        try:
            result = self._runner.run_until_complete(self._request(command_type, tag, payload or {}))
        except (TransportError, OSError, TimeoutError, ValueError) as error:
            self._diagnose(dict(stage='v2', operation=str(command_type), error=str(error), returncode=1))
            self.close()
            raise WeaponPointsBlocked(f"Sentinel v2 unavailable: {error}") from error
        self._diagnose(dict(stage='v2', operation=str(command_type), stdout=json.dumps(result), returncode=0))
        return result

    async def _request(self, command_type, tag, payload):
        transport = self._transport
        await transport.connect()
        if command_type in (22, 23, 24) and not all(transport.supports(kind, 4) for kind in (22, 23, 24)):
            raise WeaponPointsBlocked('Native Campaign menu requires payload revision 4')
        command = codec.COMMANDS[command_type]
        revision = max(command['payload_revisions'])
        variant = next(v for v in command['payload']['variants'] if v['tag'] == tag)
        namespace, binding = await self._admit(command_type, tag, payload, variant)
        scope = transport.scope
        encoded = codec.encode_variant(command_type, revision, tag, payload)
        if variant['operation'] == 'read':
            result = await transport.read(dict(scope=scope, namespace_id=namespace, save_binding=binding,
                                               command_type=command_type, payload_revision=revision), encoded)
        else:
            submission = dict(reference=dict(scope=scope, namespace_id=namespace, command_id=str(uuid4()),
                                             command_type=command_type, payload_revision=revision),
                              save_binding=binding, deadline_ms=2000)
            result = await self._submit(submission, encoded)
        return self._result(command_type, result, transport.scope)

    async def _admit(self, command_type, tag, payload, variant):
        namespace, binding = self.namespace.encode('ascii'), bytes(16)
        if variant['admission'] in ('S', 'G'):
            admission = await self._transport.observe(12, b'0' * 64, bytes(16))
            if admission is None or not any(admission['save_binding']):
                raise NativeGameplayInactive('Native gameplay context is not active')
            binding = bytes(admission['save_binding'])
            if command_type == 46 and tag == 8 and payload in (
                    {'variable': 0, 'value': '1.00'}, {'variable': 1, 'value': '1.00'}, {'variable': 2, 'value': '0'}):
                namespace = wire_text(admission['observation']['namespace_id']).encode('ascii')
        elif variant['admission'] == 'P':
            namespace = b'0' * 64
        return namespace, binding

    async def _submit(self, submission, encoded):
        transport = self._transport
        prepared = await transport.prepare(submission, encoded)
        response = await transport.submit(prepared)
        deadline = time.monotonic() + 5
        while response.response['reply']['dimensions']['progress'] in (0, 1) and time.monotonic() < deadline:
            await asyncio.sleep(.01)
            response = await transport.query(prepared.reference)
        dimensions = response.response['reply']['dimensions']
        if response.response['error'] or dimensions['progress'] != 2 or response.result is None:
            raise WeaponPointsBlocked('Native feature result is uncertain')
        await transport.ack(prepared.reference, bytes(dimensions['result_token']))
        return response.result

    def _result(self, command_type, result, scope):
        value = admission_result(result) if command_type == 12 else dict(result)
        value.update(pid=scope['pid'], target_pid=scope['pid'], process_created=str(scope['process_created']),
                     instance_id=bytes(scope['instance_id']).hex(), lifecycle_generation=scope['generation'])
        if 'namespace_id' in value:
            value['namespace_id'] = wire_text(value['namespace_id'])
            value['namespace'] = value['namespace_id']
        if 'abi_version' in value:
            value[codec.COMMANDS[command_type]['name'] + '_abi'] = value['abi_version']
        if 'current_map' in value:
            value['current_map'] = wire_text(value['current_map']['bytes'])
        if command_type == 4:
            value['availability'] = 'enabled' if value['availability'] == 1 else 'unavailable'
            value['lifecycle'] = 'active' if value['lifecycle'] == 2 else 'inactive'
        value.setdefault('build_id', getattr(self, '_build_id', ''))
        if command_type == 1:
            value['build_id'] = self._build_id = wire_text(value['build_id'])
        if command_type in (22, 23, 24):
            value['hint_intents'] = value['intent_ids'][:value['intent_count']]
        value['result'] = 'ok'
        return value

    def _run(self, args):
        operations = {'--native': 4, '--inspect': 1, '--save-admission': 12, '--save': 11,
                      '--engine': 2, '--context': 3, '--save-installation': 17}
        operation = next(operations[arg] for arg in args if arg in operations)
        if operation == 4:
            self.request(1)
        return self.request(operation, payload={'after_event': 0} if operation == 4 else {})

    def admitted_save_root(self):
        facts = self._run(['--save-admission'])
        if (facts['namespace_id'] != self.namespace or not facts['accepting_requests']
                or not facts['route_retained'] or facts['state'] != 'admitted'
                or not re.fullmatch(r'ap-[0-9a-f]{40}', facts['native_root'])):
            raise WeaponPointsBlocked('AP save provider is not admitted for this identity')
        self.save_admission = facts
        return facts['native_root']

    def observe_inventory(self):
        scope = self._run(['--native'])
        result = self.request(25)
        if (result['abi_version'] != 8 or result['kind'] != 0 or result['outcome'] != 1 or result['flags'] & 3 != 3
                or result['perks_owned'] & ~result['perks_known']
                or any(result[name] not in (0, 1, 255) for name in ('ice_bomb_after', 'dash_after', 'slayer_key_after',
                                                                  'frag_grenade_after', 'flame_belch_after', 'blood_punch_after'))):
            raise WeaponPointsBlocked('Native inventory observation is malformed or unknown')
        result['_observation_scope'] = tuple(str(scope[k]) for k in ('process_created', 'instance_id', 'build_id', 'lifecycle_generation'))
        result['_observation_map'] = scope['current_map']
        return result

    def observe_special_inventory(self):
        scope = self._run(['--native'])
        result = self.request(37)
        result['_observation_scope'] = tuple(str(scope[k]) for k in ('process_created', 'instance_id', 'build_id', 'lifecycle_generation'))
        result['_observation_map'] = scope['current_map']
        return result

    def observe_combat(self, mission_maps):
        self._run(['--native'])
        if not 1 <= len(mission_maps) <= 64:
            raise WeaponPointsBlocked('Invalid combat mission maps')
        result = self.request(50, payload={'maps': [{'value': name} for name in sorted(set(mission_maps))]})
        return dict(producer=[str(self.pid), result['process_created'], result['instance_id'], result['build_id']],
                    namespace_id=result['namespace_id'], qualified_kills=result['qualified_kills'],
                    observation_gaps=result['observation_gaps'])

    def deathlink_request(self, kind, *, enabled=False, mode='soft', event_id=0, event_hash=bytes(16), ack_sequence=0):
        payloads = {0: {}, 1: dict(enabled=int(enabled), mode=int(mode == 'hardcore')),
                    2: dict(event_id=event_id, event_hash=event_hash), 3: {},
                    4: dict(sequence=ack_sequence), 5: dict(event_id=event_id)}
        return self.request(41, kind, payloads[kind])

    def select_special_weapon(self, selected):
        return self.request(37, 2, dict(selected=selected))

    def publish_ammo_refill(self, snapshot, *, connected):
        known = snapshot.get('authoritative') is True
        balance = snapshot.get('available', 0) if known else 0
        flags = int(bool(connected)) | (6 if known else 0)
        return self.request(37, 4, dict(balance=balance, flags=flags))

    def publish_checked_locations(self, checked_locations, revision):
        bits = [0] * codec.SCHEMA['limits']['automap_words']
        for location in checked_locations:
            offset = location - 7770000
            if 0 <= offset < len(bits) * 64:
                bits[offset // 64] |= 1 << (offset % 64)
        return self.request(45, 1, dict(known=1, revision=revision, checked_locations=bits))

    def publish_combat_progress(self, total, target, first_milestone=0, milestone=0, milestone_step=0, visible=True):
        scope = self._run(['--native'])
        native_scope = dict(pid=self.pid, process_created=int(scope['process_created']),
                            instance_id=bytes.fromhex(scope['instance_id']), lifecycle_generation=scope['lifecycle_generation'])
        presentation = dict(execution=dict(expected=native_scope, request_id=1, nonce=uuid4().bytes, deadline_ms=2000),
                            namespace_id=self.namespace.encode('ascii') + b'\0', total=total, target=target,
                            first_milestone=first_milestone, milestone=milestone, milestone_step=milestone_step, visible=int(visible))
        return self.request(51, payload=dict(presentation=presentation))


def wire_text(value):
    return bytes(value).split(b'\0', 1)[0].decode('utf-8')


def admission_result(result):
    value = dict(result['observation'])
    value.update(save_binding=bytes(result['save_binding']).hex(),
                 state='admitted' if value['state'] == 3 else 'unavailable',
                 accepting_requests=bool(value['flags'] & 2), route_retained=bool(value['flags'] & 1))
    value['native_root'] = wire_text(value['native_root'])
    return value


def diagnostic_value(value):
    if isinstance(value, dict):
        return {key: (item not in (0, "0", None) if key == "event_gap_count" else diagnostic_value(item))
                for key, item in value.items() if key not in {
                    "sequence", "timestamp_ms", "at_monotonic", "sampled_at_ms", "event_sequence",
                    "callback_sequence", "callback_at_ms", "context_sampled_at_ms", "history_oldest",
                    "history_overwritten", "events", "qualified_kills", "at_ms"}}
    if isinstance(value, list):
        return [diagnostic_value(item) for item in value]
    return value


def diagnostic_entry(evidence, previous, now, signature):
    row = {"signature": signature, "first_seen": now, "last_seen": now, "count": 1, "emitted_at": now}
    first_error = previous.get("first_error") if previous else None
    if first_error is None and (evidence.get("error") or evidence.get("returncode", 0)):
        first_error = {**evidence, "at_monotonic": now}
    if first_error is not None:
        row["first_error"] = first_error
    return row


class ClientStateLoadError(RuntimeError):
    pass


def atomic_write_json(path, state, *, indent=2, sort_keys=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as file:
            json.dump(state, file, indent=indent, sort_keys=sort_keys)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def client_state_metrics(state):
    sessions = state.get("sessions", {}) if isinstance(state, dict) else {}
    if not isinstance(sessions, dict):
        return {"session_count": 0, "processed_count": 0, "receipt_count": 0}
    processed_count = 0
    receipt_count = 0
    for session in sessions.values():
        if not isinstance(session, dict):
            continue
        processed = session.get("processed_items")
        if isinstance(processed, int) and not isinstance(processed, bool) and processed >= 0:
            processed_count += processed
        history = session.get("receipt_history")
        if isinstance(history, dict):
            receipt_ids = history.get("receipt_ids")
            if isinstance(receipt_ids, list):
                receipt_count += len(receipt_ids)
    return {
        "session_count": len(sessions),
        "processed_count": processed_count,
        "receipt_count": receipt_count,
    }


class ClientStateStore:
    def __init__(self, path, *, version, migrate, log_event, logger, observer=lambda *args, **kwargs: None):
        self.path = path
        self.version = version
        self.migrate = migrate
        self.log_event = log_event
        self.logger = logger
        self._load_error = None
        self._observe = observer

    def _validate_session_boundaries(self, raw_state):
        if isinstance(raw_state, dict) and isinstance(raw_state.get("sessions"), dict):
            for session in raw_state["sessions"].values():
                if not isinstance(session, dict):
                    raise ValueError("client state session must be an object")
                boundary = session.get("processed_items", 0)
                if isinstance(boundary, bool) or not isinstance(boundary, int) or boundary < 0:
                    raise ValueError("client state receipt boundary is invalid")

    def load(self):
        if self._load_error is not None:
            raise self._load_error
        empty_state = {"version": self.version, "sessions": {}}
        try:
            raw_text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.log_event(
                "ITEM_STATE_LOAD", path=str(self.path.resolve()), status="missing",
                boundary_before=0, boundary_after=0, success=True,
                **client_state_metrics(empty_state),
            )
            return empty_state
        except (OSError, UnicodeError) as error:
            self._block_load(error)
        try:
            raw_state = json.loads(raw_text)
            self._validate_session_boundaries(raw_state)
            state, migrated = self.migrate(raw_state)
            metrics = client_state_metrics(state)
            self.log_event(
                "ITEM_STATE_LOAD", path=str(self.path.resolve()),
                status="migrated" if migrated else "loaded", version=state.get("version"),
                boundary_before=0, boundary_after=metrics["processed_count"], success=True,
                **metrics,
            )
            if migrated:
                self.log_event(
                    "ITEM_STATE_MIGRATION", path=str(self.path.resolve()),
                    reason="state_schema_migration", boundary_before=0,
                    boundary_after=metrics["processed_count"], success=True, **metrics,
                )
                self.logger.info(
                    "[State] STATE_MIGRATED from=1 to=%s sessions=%s",
                    self.version, len(state["sessions"]),
                )
                self.commit(state, reason="state_migration")
            return state
        except (OSError, ValueError, TypeError) as error:
            self._block_load(error)

    def _block_load(self, error):
        quarantine = self.path.with_name(f"{self.path.name}.corrupt-{time.time_ns()}")
        try:
            shutil.copy2(self.path, quarantine)
            preservation = f"Original retained; quarantine copy: {quarantine}"
        except OSError as copy_error:
            preservation = f"Original retained at {self.path}; quarantine copy failed: {copy_error}"
        message = (f"Item delivery blocked: cannot load client state {self.path}: {error}. "
                   f"{preservation}. Restore readable state and restart the bridge.")
        self._load_error = ClientStateLoadError(message)
        self.logger.error("[State] %s", message)
        self.log_event("ITEM_STATE_LOAD", path=str(self.path), status="blocked",
                       reason=str(error), success=False, quarantine=str(quarantine))
        raise self._load_error from error

    def _state_boundary(self, state):
        boundaries = []
        sessions = state.get("sessions", {}) if isinstance(state, dict) else {}
        if isinstance(sessions, dict):
            for session in sessions.values():
                candidate = session.get("processed_items") if isinstance(session, dict) else None
                if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
                    boundaries.append(candidate)
        return max(boundaries, default=0)

    def commit(self, state, *, reason="state_update", boundary=None, boundary_before=None):
        if self._load_error is not None:
            raise self._load_error
        if isinstance(boundary, bool) or not isinstance(boundary, int) or boundary < 0:
            boundary = self._state_boundary(state)
        if (
            isinstance(boundary_before, bool)
            or not isinstance(boundary_before, int)
            or boundary_before < 0
        ):
            boundary_before = boundary
        metrics = client_state_metrics(state)
        try:
            atomic_write_json(self.path, state)
        except (OSError, TypeError, ValueError) as error:
            self._observe("legacy_commit", state=state, reason=reason, boundary=boundary, success=False)
            self.log_event(
                "ITEM_STATE_SAVE", path=str(self.path.resolve()), reason=reason,
                boundary_before=boundary_before, boundary_after=boundary,
                success=False, error=str(error), **metrics,
            )
            raise
        self._observe("legacy_commit", state=state, reason=reason, boundary=boundary, success=True)
        self.log_event(
            "ITEM_STATE_SAVE", path=str(self.path.resolve()), reason=reason,
            boundary_before=boundary_before, boundary_after=boundary, success=True,
            **metrics,
        )

    def commit_session(self, document, session, *, processed_items, cultist_autosave_path,
                       received_deathlink_event_ids, save_slot_observations):
        original_session = session
        session = dict(session)
        session["receipt_history"] = copy.deepcopy(session.get("receipt_history"))
        project_receipt_boundary(session, processed_items)
        session["cultist_autosave_path"] = cultist_autosave_path
        session.pop("deathlinked", None)
        session["received_deathlink_event_ids"] = sorted(received_deathlink_event_ids)[-64:]
        session["save_slot_observations"] = save_slot_observations
        session.pop("automap_cleanup", None)
        session.pop("fast_travel_delivered", None)
        session.pop("sticky_mastery_observed", None)
        session.pop("weapon_masteries_observed", None)
        candidate = dict(document, sessions={
            key: session if value is original_session else value
            for key, value in document["sessions"].items()
        })
        self.commit(candidate, reason="session_persist", boundary=processed_items)
        for key in original_session.keys() - session.keys():
            original_session.pop(key)
        original_session.update(session)


def _starting_weapon(item_id):
    if item_id not in {7770000, 7770001, 7770002, 7770003, 7770004, 7770005, 7770900}:
        raise StartingOwnershipError("starting.weapon_item_id must be a starting weapon")


def _starting_item_pair(row, item_identity):
    if not isinstance(row, list) or len(row) != 2:
        raise StartingOwnershipError("starting.items entries must be item and quantity pairs")
    item_id, quantity = row
    if type(item_id) is not int or item_id not in item_identity:
        raise StartingOwnershipError("starting.items contains an unknown item")
    if type(quantity) is not int or not 1 <= quantity <= 0x7fffffff:
        raise StartingOwnershipError("starting quantity must be a positive integer")
    return item_id, quantity


def _persistent_start_ids(fact, definitions):
    definition = definitions.get(fact.item_id)
    if fact.item_id == 7770024 or definition is None:
        return ()
    if isinstance(definition, dict):
        if definition.get("type") in {"currency", "native_weapon_upgrade_points"}:
            return ()
        return (fact.item_id,) * min(fact.quantity, len(definition.get("perks", [None])))
    return (fact.item_id,)


def _field(receipt: Any, name: str) -> Any:
    if isinstance(receipt, Mapping):
        return receipt.get(name)
    return getattr(receipt, name, None)


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(value[key])
            for key in sorted(value, key=lambda candidate: str(candidate))
        }
    if isinstance(value, tuple | list):
        return [_canonical_value(entry) for entry in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _known_receipt_occurrences(processed_receipt_ids):
    if isinstance(processed_receipt_ids, Mapping):
        return _safe_receipt_counts(processed_receipt_ids)
    return Counter(value for value in processed_receipt_ids if isinstance(value, str) and value)


def _vanilla_ownership(item_ids, checked_locations, local_checked_locations, hell_on_earth_locations, randomize_chainsaw, randomize_dash, server_checked_ready, exultia_complete_location):
    checked = set(checked_locations) | set(local_checked_locations)
    has_hoe_check = any(
        loc in hell_on_earth_locations
        or (isinstance(loc, str) and loc.isdigit() and int(loc) in hell_on_earth_locations)
        for loc in checked
    )
    vanilla_chainsaw = not randomize_chainsaw and has_hoe_check and 7770010 not in item_ids
    vanilla_dash = bool(
        not randomize_dash and server_checked_ready
        and exultia_complete_location in checked_locations
    )
    return vanilla_chainsaw, vanilla_dash


def _ownership_fingerprint(receipts, starting_item_ids, vanilla_chainsaw, vanilla_dash, slot):
    records = [
        {"index": index, "item_id": receipt_item_id(receipt), "receipt_id": receipt_identity(receipt)}
        for index, receipt in enumerate(receipts)
    ]
    records.extend(_starting_records(starting_item_ids))
    if vanilla_chainsaw:
        records.append({
            "index": len(records), "item_id": 7770010,
            "receipt_id": "network:" + json.dumps([0, slot, 7770010, None], separators=(",", ":")),
        })
    if vanilla_dash:
        records.append({"derived_item_id": 7770015, "provenance": "unrandomized_dash_server_mission_complete"})
    fingerprint = hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return fingerprint


def _starting_records(item_ids):
    return [{"starting_item_id": item_id} for item_id in item_ids]


def _safe_nonnegative_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _safe_receipt_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for entry in value:
        if isinstance(entry, str) and entry:
            result.append(entry)
    return result


def _safe_item_ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    return [
        entry
        for entry in value
        if isinstance(entry, int) and not isinstance(entry, bool)
    ]


def _compare_receipt_prefix(processed_boundary, expected_item_ids, expected_receipt_ids, expected_receipt_counts, actual_item_ids, actual_receipt_ids):
    if expected_item_ids:
        if len(expected_item_ids) != processed_boundary:
            return False, (
                "durable receipt item prefix length does not match boundary "
                f"({len(expected_item_ids)} != {processed_boundary})"
            )
        if tuple(expected_item_ids) != actual_item_ids:
            return False, "durable receipt item prefix differs from ReceivedItems"
    elif expected_receipt_ids:
        if len(expected_receipt_ids) != len(actual_receipt_ids):
            return False, "durable receipt identity prefix is incomplete"
        if tuple(expected_receipt_ids) != actual_receipt_ids:
            return False, "durable receipt identity prefix differs from ReceivedItems"
    elif expected_receipt_counts:
        if Counter(actual_receipt_ids) != expected_receipt_counts:
            return False, "durable receipt identity counts differ from ReceivedItems"
    return True, "compatible"


def _safe_receipt_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: count
        for key, count in value.items()
        if isinstance(key, str)
        and key
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count > 0
    }


def _valid_session_coordinates(team, slot):
    return (isinstance(team, int) and not isinstance(team, bool) and team >= 0
            and isinstance(slot, int) and not isinstance(slot, bool) and slot >= 0)


def _valid_legacy_identity(sessions, seed_name, team, slot, generation):
    if (
        not isinstance(sessions, dict)
        or not isinstance(seed_name, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+", seed_name) is None
                or not _valid_session_coordinates(team, slot)
        or not isinstance(generation, str)
        or re.fullmatch(r"[0-9a-f]{64}", generation) is None
    ):
        return False
    return True


def _normalize_physical_gates(normalized):
    if "physical_slayer_gates" in normalized:
        gates = normalized["physical_slayer_gates"]
        normalized["physical_slayer_gates"] = (
            sorted({gate for gate in gates if type(gate) is int}) if isinstance(gates, list) else []
        )


def _normalize_deferred_frag(normalized):
    frag = normalized.get("deferred_frag_items")
    normalized["deferred_frag_items"] = {
        key: item_id for key, item_id in (frag.items() if isinstance(frag, Mapping) else ())
        if isinstance(key, str) and key.isdecimal() and isinstance(item_id, int)
        and item_id in {7770011, 7770111, 7770112, 7770113, 7770114}
    }


def _normalize_owned_item_ids(history):
    owned_ids = history.get("owned_item_ids")
    if not isinstance(owned_ids, list):
        owned_ids = []
    history["owned_item_ids"] = sorted(
        {
            entry
            for entry in owned_ids
            if isinstance(entry, int) and not isinstance(entry, bool)
        }
    )


def _normalize_receipt_history(normalized, processed):
    history = normalized.get("receipt_history")
    if not isinstance(history, Mapping):
        history = {}
    history = copy.deepcopy(dict(history))
    history["processed_boundary"] = processed
    highest = history.get("highest_observed_index", max(processed - 1, -1))
    if isinstance(highest, bool) or not isinstance(highest, int) or highest < -1:
        highest = max(processed - 1, -1)
    history["highest_observed_index"] = highest
    legacy_receipt_ids = _safe_receipt_ids(history.get("receipt_ids"))
    receipt_counts = _safe_receipt_counts(history.get("receipt_counts"))
    if not receipt_counts:
        receipt_counts = dict(Counter(legacy_receipt_ids))
    history["receipt_counts"] = receipt_counts
    history["receipt_ids"] = legacy_receipt_ids
    history["receipt_item_ids"] = _safe_item_ids(history.get("receipt_item_ids"))
    _normalize_owned_item_ids(history)
    normalized["receipt_history"] = history


def _normalize_bootstrap(normalized):
    bootstrap = normalized.get("bootstrap")
    if not isinstance(bootstrap, Mapping):
        bootstrap = {"actions": {}}
    else:
        bootstrap = copy.deepcopy(dict(bootstrap))
        revision = bootstrap.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            bootstrap.pop("revision", None)
        if not isinstance(bootstrap.get("actions"), Mapping):
            bootstrap["actions"] = {}
    normalized["bootstrap"] = bootstrap


def _normalize_automap_cleanup(normalized):
    cleanup = normalized.get("automap_cleanup")
    if not isinstance(cleanup, Mapping):
        cleanup = {}
    normalized["automap_cleanup"] = {
        str(key): value
        for key, value in cleanup.items()
        if (
            str(key)
            and isinstance(value, str)
            and MATERIALIZATION_EPOCH_PATTERN.fullmatch(value) is not None
        )
    }


def _normalize_replay_history(normalized):
    # Keep valid history and make malformed values inert rather than executable.
    for key in ("never_replay_history", "never_replay_items", "never_replayed", "never_replay"):
        if key not in normalized:
            continue
        value = normalized[key]
        if not isinstance(value, list):
            normalized[key] = copy.deepcopy(dict(value)) if isinstance(value, Mapping) else []
        else:
            normalized[key] = [
                entry
                for entry in value
                if (isinstance(entry, str) and entry)
                or (isinstance(entry, int) and not isinstance(entry, bool))
            ]


def _read_registry(source: Path | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(source, Mapping):
        return source
    with Path(source).open("r", encoding="utf-8") as file:
        loaded = json.load(file)
    if not isinstance(loaded, dict):
        raise ValueError("item replay policy registry must be an object")
    return loaded


def _policy_item_id(raw_id):
    try:
        item_id = int(raw_id)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid numeric item policy ID: {raw_id!r}") from error
    if str(item_id) != str(raw_id):
        raise ValueError(f"item policy ID is not canonical decimal: {raw_id!r}")
    return item_id


def _parse_replay_policy(raw_id, entry):
    item_id = _policy_item_id(raw_id)
    if not isinstance(entry, dict):
        raise ValueError(f"item policy {item_id} must be an object")
    if not {"name", "policy"} <= set(entry) or set(entry) - {
        "name", "policy", "receipt_feedback"
    }:
        raise ValueError(
            f"item policy {item_id} must contain name, policy, and optional receipt_feedback"
        )
    name = entry["name"]
    policy = entry["policy"]
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"item policy {item_id} has invalid name")
    if policy not in SUPPORTED_POLICIES:
        raise ValueError(f"unsupported policy for item {item_id}: {policy!r}")
    receipt_feedback = entry.get("receipt_feedback", AP_RECEIPT_FEEDBACK)
    if receipt_feedback not in SUPPORTED_RECEIPT_FEEDBACK:
        raise ValueError(
            f"unsupported receipt feedback for item {item_id}: {receipt_feedback!r}"
        )
    return ReplayPolicy(item_id, name, policy, receipt_feedback)


def _authored_epoch_allowed(evidence, epoch, lease):
    return (evidence is not None and evidence.state == "gameplay" and evidence.native_safe
            and isinstance(epoch, str) and lease == epoch)


def _clean_decl(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().strip('"')
    if not cleaned or cleaned.lower() in {"none", "null", "<none>"}:
        return None
    return cleaned


def _field_values(details: Mapping[str, Any], prefix: str) -> frozenset[str]:
    values = set()
    for key, value in details.items():
        if not isinstance(key, str):
            continue
        if key == prefix or re.fullmatch(re.escape(prefix) + r"(?:_|\[)?\d+\]?", key):
            cleaned = _clean_decl(value)
            if cleaned is not None:
                values.add(cleaned)
    return frozenset(values)


def _slot_value(details: Mapping[str, Any], index: int) -> str | None:
    for key in (f"runeSlotName_{index}", f"runeSlotName[{index}]"):
        if key in details:
            return _clean_decl(details[key])
    return None


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return None


def _currency_confirmed(expected, grant, result):
    if not coherent_evidence(result.before_scope, result.after_scope):
        return False
    if result.reference != expected.reference or result.before_scope != expected.scope:
        return False
    if grant.currency is not Currency.SENTINEL_BATTERY or grant.amount not in (1, 2):
        return False
    if (result.currency, result.writer_entered, result.native_exception, result.outcome) != (
            grant.currency, True, 0, CurrencyOutcome.GRANTED):
        return False
    return result.before.gained == grant.expected_gained and currency_delta(result) == (grant.amount, grant.amount)


def _progression_confirmed(postcondition, observation):
    match postcondition:
        case SuitStage(stage):
            return 0 <= stage < 6 and bool(observation.suit.known & observation.suit.owned & (1 << stage))
        case SlayerKey(map_name):
            quantity = observation.slayer_key_quantity
            return observation.after_scope.map_name == map_name and quantity is not None and quantity > 0
        case MeatHookPerk():
            return observation.meat_hook is EffectProof.PRESENT
        case MasteryEffect(item_id, host, mod):
            return _mastery_confirmed(observation, item_id, host, mod)
    return False


def _mastery_confirmed(observation, item_id, host, mod):
    slot = observation.masteries.get(item_id)
    return bool(slot and slot.host == host and slot.mod == mod and _slot_proof(slot) is EffectProof.PRESENT)


def _mission_confirmed(scope, access, observation):
    if scope.readiness is not SaveReadiness.READY or observation.menu_scope != scope:
        return False
    if (observation.revision, observation.stage_id, observation.map_name) != (
            access.revision, access.stage_id, access.map_name):
        return False
    destination = observation.destination_scope
    if not observation.entry_usable or not observation.destination_ready or destination is None:
        return False
    return _destination_matches(scope, access, observation)


def _destination_matches(scope, access, observation):
    destination = observation.destination_scope
    identity = ("pid", "process_created", "core_instance", "namespace", "save_binding")
    if any(getattr(scope, field) != getattr(destination, field) for field in identity):
        return False
    return (destination.readiness is SaveReadiness.READY and destination.generation > scope.generation
            and destination.map_name == access.map_name and observation.destination_revision == access.revision
            and observation.destination_stage_id == access.stage_id)


def _compare_persistent(ownership, definitions, policies, observation, context, scope,
                        special_mode, selection, replay_identity, include_manual_replay):
    if context is not None:
        materialization = _materialize(ownership, context, scope, definitions, policies, special_mode,
                                       observation=observation, selected_item_ids=selection)
        unresolved = _unresolved_items(ownership, context, scope, policies, special_mode, selection, observation)
        return materialization, materialization.reconciliation, unresolved
    if replay_identity is None:
        return None, None, ()
    reconciliation = _replay(ownership.reconciliation_item_ids, definitions, policies, *replay_identity,
                              include_manual_replay=include_manual_replay)
    return None, reconciliation, ()


def _compare_runes(ownership, selection, native, mapping, expected, obligations):
    if native is None:
        return None
    ids = ownership.reconciliation_item_ids
    if selection is not None:
        ids = tuple(i for i in ids if i in selection)
    plan = _runes(ids, native, mapping, expected_rune_item_ids=expected)
    return _pending_runes(plan, obligations)


def _merge_plans(materialization, reconciliation, rune_plan, obligations):
    if reconciliation is None:
        return materialization, reconciliation
    reconciliation = _pending_effects(reconciliation, obligations)
    if rune_plan is not None:
        rune_ids = {entry.item_id for entry in rune_plan.entries}
        reconciliation = replace(reconciliation, commands=tuple(c for c in reconciliation.commands
                                                                 if c.item_id not in rune_ids))
    if materialization is not None:
        materialization = replace(materialization, reconciliation=reconciliation)
    return materialization, reconciliation


def _pending_native_effects(ownership, observation, selection, obligations):
    blocked = _blocked_effects(obligations)
    effects = _native_effects(ownership, observation, selection) + _acquired_masteries(ownership, selection, obligations)
    return tuple({(effect.item_id, effect.stage): effect for effect in effects
                  if (effect.item_id, effect.stage) not in blocked}.values())


def _acquired_masteries(ownership, selection, obligations):
    recorded = {effect.item_id for effect in obligations}
    ids = {a.item_id for a in ownership.acquisitions} & MASTERY_ITEM_BITS.keys() - recorded
    if selection is not None:
        ids &= selection
    return tuple(NativeInventoryEffect(i, NativeEffectKind.MASTERY, 0, MASTERY_ITEM_BITS[i]) for i in sorted(ids))


def _compare_acquisitions(ownership, context, selection, restore_persistent_only, definitions, policies, obligations):
    if context is None or selection is not None or restore_persistent_only:
        return ()
    return _acquisitions(ownership.acquisitions, definitions, policies, obligations)


def _acquisitions(acquisitions, definitions, policies, obligations):
    blocked = {(effect.source_key, effect.stage) for effect in obligations
               if effect.resolution != Resolution.REPLACED and effect.state != CommandState.STALE}
    effects = []
    for acquisition in acquisitions:
        item_id = acquisition.item_id
        if policies[item_id].policy != NEVER_REPLAY:
            continue
        family = classify_item_definition(definitions[item_id])
        if not _acquisition_allowed(family, acquisition.live):
            continue
        effect = _acquisition_effect(acquisition, family, definitions, blocked)
        if effect is not None:
            effects.append(effect)
    return tuple(effects)


def _acquisition_effect(acquisition, family, definitions, blocked):
    if family == "transient_effect":
        if (acquisition.source_key, 0) in blocked:
            return None
        return AcquisitionEffect(acquisition.source_key, acquisition.item_id, (), transient=True)
    plan = compile_item_delivery_plan(acquisition.item_id, definitions)
    commands = tuple(command for command in plan.commands if (acquisition.source_key, command.index) not in blocked)
    return AcquisitionEffect(acquisition.source_key, acquisition.item_id, commands) if commands else None


def _acquisition_allowed(family, live):
    if family in {"native_weapon_upgrade_points", "no_op"}:
        return False
    return live or family not in {"transient_effect", "trap_spawn"}


def _unresolved_items(ownership, context, scope, policies, special_mode, selection, observation):
    selected = _selected_ids(ownership, context, scope, policies, _special_ids(special_mode), selection)
    return tuple(item_id for item_id in selected if observation.get_state(item_id) == UNKNOWN)


def _dependencies(plan, native_effects):
    commands = () if plan is None else plan.commands
    frag = tuple(InventoryDependency(command.item_id, command.stage, 7770011)
                 for command in commands if command.item_id in {7770111, 7770112, 7770113, 7770114})
    blood_punch = tuple(InventoryDependency(effect.item_id, effect.stage, 7770904)
                        for effect in native_effects if effect.kind == NativeEffectKind.BLOOD_PUNCH and effect.stage > 0)
    return frag + blood_punch


def _native_effects(ownership, observation, selection):
    if observation is None:
        return ()
    ids = set(ownership.reconciliation_item_ids)
    if selection is not None:
        ids &= selection
    effects = [NativeInventoryEffect(i, NativeEffectKind.MASTERY, 0, MASTERY_ITEM_BITS[i])
               for i in sorted(ids & MASTERY_ITEM_BITS.keys()) if _mastery_proof(observation, i) is EffectProof.ABSENT]
    effects.extend(_progressive_blood_punch(ownership, observation, ids))
    return tuple(effects)


def _all_unresolved(ownership, observation, selection, rune_plan, unresolved):
    ids = set(ownership.reconciliation_item_ids)
    if selection is not None:
        ids &= selection
    unknown = set(unresolved)
    if observation is not None:
        unknown.update(i for i in ids & MASTERY_ITEM_BITS.keys()
                       if _mastery_proof(observation, i) is EffectProof.UNPROVEN)
        if 7770904 in ids and 7770904 not in observation.stage_masks:
            unknown.add(7770904)
    if rune_plan is not None:
        unknown.update(entry.item_id for entry in rune_plan.entries if entry.disposition == "deferred")
    return tuple(sorted(unknown))


def _mastery_proof(observation, item_id):
    progression = observation.progression
    if progression is None or not coherent_evidence(progression.before_scope, progression.after_scope):
        return EffectProof.UNPROVEN
    slot = progression.masteries.get(item_id)
    return _slot_proof(slot) if slot else EffectProof.UNPROVEN


def _slot_proof(slot):
    if not slot.selected or slot.component_before != slot.component_after:
        return EffectProof.UNPROVEN
    return slot.proof


def _progressive_blood_punch(ownership, observation, ids):
    observed = observation.stage_masks.get(7770904)
    if 7770904 not in ids or observed is None:
        return ()
    count = min(4, ownership.reconciliation_item_ids.count(7770904))
    return tuple(NativeInventoryEffect(7770904, NativeEffectKind.BLOOD_PUNCH, stage, 1 << stage)
                 for stage in range(count) if not observed & (1 << stage))


def _pending_effects(plan, obligations):
    blocked = _blocked_effects(obligations)
    blocked.update((effect.item_id, effect.stage) for effect in obligations if effect.item_id in GATE_KEY_TO_MAP
                   and effect.state is CommandState.CONFIRMED)
    return replace(plan, commands=tuple(command for command in plan.commands
                                       if (command.item_id, command.stage) not in blocked))


def _persistent_obligations(obligations, restore_persistent_only):
    if not restore_persistent_only:
        return obligations
    return tuple(effect for effect in obligations if not (
        effect.item_id in GATE_KEY_TO_MAP and effect.state is CommandState.CONFIRMED))


def _blocked_effects(obligations):
    return {(effect.item_id, effect.stage) for effect in obligations
               if effect.state in {CommandState.PLANNED, CommandState.DEFERRED,
                                   CommandState.QUEUED, CommandState.DISPATCHED,
                                   CommandState.EFFECT_UNKNOWN, CommandState.REJECTED}
               and effect.resolution != Resolution.REPLACED}


def _pending_runes(plan, obligations):
    blocked = _blocked_effects(obligations)
    entries = tuple(replace(entry, disposition="deferred", reason="existing_obligation")
                    if (entry.item_id, 0) in blocked else entry for entry in plan.entries)
    return replace(plan, entries=entries, status=_rune_status(entries))


def _weapon_point_grants(total, gained):
    if gained is None:
        raise WeaponPointsBlocked("Native earned WUP observation is unknown")
    if gained < 0 or gained > total or gained % 3:
        raise WeaponPointsBlocked("Native earned WUP disagrees with receipt ownership")
    return tuple(range(gained, total, 3))


def _compare_weapon_points(points, obligations):
    if points is None:
        return (), None
    try:
        blocked = _blocked_effects(obligations)
        grants = _weapon_point_grants(*points)
        first_pending = min((stage for item_id, stage in blocked if item_id == 7770903), default=points[0] // 3)
        return tuple(gained for gained in grants if gained // 3 < first_pending), None
    except WeaponPointsBlocked as error:
        return (), error


def _special_ids(mode):
    selected = {
        "progressive_special_weapon": {7770901}, "Progressive Special Weapon": {7770901},
        "progressive_sentinel_hammer": {7770902}, "Progressive Sentinel Hammer": {7770902},
        "the_crucible": {7770007}, "The Crucible": {7770007},
    }.get(mode)
    if selected is None:
        raise MaterializationPlanError(f"unsupported special weapon option: {mode!r}", blocked=False)
    return selected


def _replay_allowed(item_id, policies, lease):
    if item_id in SUPPORT_RUNE_IDS | {7770007, 7770009, 7770901, 7770902}:
        return True
    if item_id == 7770021:
        return True
    if item_id in {7770017, 7770088, 7770092} and lease is not None:
        return True
    policy = policies.get(item_id)
    return policy is not None and policy.policy in {"replay_idempotent", "replay_manual_only"}


def _selected_ids(ownership, context, scope, policies, special_ids, selection):
    received = set(ownership.reconciliation_item_ids)
    candidates = context_item_ids(context, received)
    excluded = MASTERY_ITEM_BITS.keys() | ({7770007, 7770009, 7770901, 7770902} - special_ids)
    if 7770011 not in received:
        excluded |= {7770111, 7770112, 7770113, 7770114}
    candidates = set(candidates) - excluded
    if selection is not None:
        candidates &= selection
    return tuple(sorted(item_id for item_id in candidates
                        if _replay_allowed(item_id, policies, scope.materialization_lease)))


def _command_missing(command, observation, counts):
    item_id = command.item_id
    if item_id == 7770021:
        return _suit_stage_missing(observation.progression, command.stage)
    if item_id in {7770017, 7770088, 7770092}:
        if observation.get_state(item_id) == UNKNOWN:
            return False
        observed = observation.get_observed_stage(item_id) if observation.is_owned(item_id) else 0
        return observed <= command.stage < min(MAX_CAPACITY_TIER, counts[item_id])
    return observation.is_proven_missing(item_id) and (item_id != 7770006 or command.stage == 0)


def _suit_stage_missing(progression, stage):
    if progression is None or not coherent_evidence(progression.before_scope, progression.after_scope):
        return False
    bit = 1 << stage
    return bool(progression.suit.known & bit and not progression.suit.owned & bit)


def _persistent_replay(ownership, selected_ids, special_ids, definitions, policies, context, scope, observation):
    ids = tuple(item_id for item_id in ownership.reconciliation_item_ids
                if item_id in selected_ids and item_id not in SUPPORT_RUNE_IDS | special_ids)
    identity = stable_command_key("context", scope.room_seed_name, scope.team, scope.slot, context.identity)
    plan = _replay(ids, {i: definitions[i] for i in set(ids)}, {i: policies[i] for i in set(ids)},
                   identity, scope.evidence_epoch, include_manual_replay=True)
    counts = Counter(ownership.reconciliation_item_ids)
    return replace(plan, commands=tuple(c for c in plan.commands if _command_missing(c, observation, counts)))


def _special_candidate(item_id, count, definitions):
    if item_id in {7770007, 7770009}:
        return (1 if item_id == 7770007 else 2), item_id, compile_item_delivery_plan(item_id, definitions)
    stage = min(count, len(definitions[item_id].get("perks", []))) - 1
    return stage + 1, item_id, compile_item_delivery_plan(item_id, definitions, stage=stage)


def _hammer_commands(context, item_id, stage):
    has_hammer = item_id == 7770009 or (item_id == 7770901 and stage >= 2) or item_id == 7770902
    if not has_hammer or context.campaign not in {"TAG2", "Dark Lord"}:
        return ()
    if not context.supports(TAG_SPECIAL_CAPABILITY):
        return ()
    return tuple((key, f"ai_ScriptCmdEnt player1 givePlayerPerk perk/player/weapons/hammer/{key}")
                 for key in ("ammo_drops_upgraded", "armor_and_health_drops_upgraded", "debuff_duration_increase"))


def _special_command(item_id, policy, stage, scope, context, index, command, description):
    return ReconciliationCommand(
        item_id, policy.name, policy.policy, stage,
        stable_command_key("reconcile", scope.room_seed_name, scope.team, scope.slot,
                        context.identity, "special", stage, index), command, description,
    )


def _special_commands(ownership, selected_ids, special_ids, definitions, policies, context, scope, observation):
    counts = Counter(ownership.reconciliation_item_ids)
    candidates = [_special_candidate(i, counts[i], definitions) for i in selected_ids if i in special_ids]
    if not candidates:
        return (), 0, None
    stage, item_id, delivery = max(candidates, key=lambda value: (value[0], value[1]))
    policy = policies[item_id]
    commands = [_special_command(item_id, policy, stage, scope, context, c.index, c.command, delivery.description)
                for c in delivery.commands]
    existing = {c.command for c in commands}
    commands.extend(_special_command(item_id, policy, stage, scope, context, f"hammer-upgrade-{key}",
                                     command, f"Sentinel Hammer upgrade: {key}")
                    for key, command in _hammer_commands(context, item_id, stage) if command not in existing)
    if not _special_missing(observation, item_id, stage):
        commands = []
    diagnostic = (item_id, counts[item_id], stage, context.identity, len(commands))
    return tuple(commands), stage, diagnostic


def _special_missing(observation, item_id, stage):
    return observation.is_proven_missing(item_id) or (observation.is_owned(item_id)
                                                      and observation.get_observed_stage(item_id) < stage)


def _support_commands(received, context, scope, definitions, policies, observation):
    commands = []
    for item_id in support_rune_commands(received, context):
        if not observation.is_proven_missing(item_id):
            continue
        plan = compile_item_delivery_plan(item_id, {item_id: definitions[item_id]})
        if len(plan.commands) != 1:
            raise MaterializationPlanError(f"invalid support rune plan {item_id}")
        policy = policies[item_id]
        commands.append(ReconciliationCommand(
            item_id, policy.name, policy.policy, 0,
            stable_command_key("reconcile", scope.room_seed_name, scope.team, scope.slot,
                            context.identity, "support-rune", item_id),
            plan.commands[0].command, plan.description,
        ))
    return tuple(commands)


def _blood_punch_commands(ownership, context, scope, observation, selected_ids):
    if context.campaign == "Base" or 7770014 not in selected_ids:
        return ()
    upgrades = _missing_completion_upgrades(ownership.blood_punch_upgrades, observation)
    return tuple(ReconciliationCommand(
        7770014, "Blood Punch", "replay_idempotent", upgrade.location_id,
        stable_command_key("reconcile", scope.room_seed_name, scope.team, scope.slot,
                        context.identity, "blood-punch-upgrade", upgrade.location_id),
        f"ai_ScriptCmdEnt player1 givePlayerPerk {upgrade.perk_path}",
        f"Blood Punch upgrade from {upgrade.mission_name}",
    ) for upgrade in upgrades)


def _missing_completion_upgrades(upgrades, observation):
    mask = observation.stage_masks.get(7770904)
    if mask is None:
        return upgrades if observation.is_proven_missing(7770014) else ()
    bits = {7770162: 2, 7770290: 4, 7770411: 8}
    return tuple(upgrade for upgrade in upgrades if not mask & bits[upgrade.location_id])


def _materialize(ownership, context, scope, item_definitions, replay_policies, special_mode,
                 *, observation: InventoryObservation | None = None, selected_item_ids=None):
    if observation is None or observation.is_stale_for(
        room_seed_name=scope.room_seed_name, epoch=scope.evidence_epoch,
        context_identity=context.identity, campaign=context.campaign,
    ):
        raise MaterializationPlanError("fresh native observation required; UNKNOWN is not missing")
    special_ids = _special_ids(special_mode)
    selected_ids = _selected_ids(ownership, context, scope, replay_policies, special_ids, selected_item_ids)
    plan = _persistent_replay(ownership, selected_ids, special_ids, item_definitions,
                              replay_policies, context, scope, observation)
    special, stage, diagnostic = _special_commands(ownership, selected_ids, special_ids, item_definitions,
                                                  replay_policies, context, scope, observation)
    support = _support_commands(selected_ids, context, scope, item_definitions, replay_policies, observation)
    blood_punch = _blood_punch_commands(ownership, context, scope, observation, selected_ids)
    raw = plan.commands + special + support + blood_punch
    commands = {}
    for command in raw:
        commands.setdefault((command.item_id, command.stage, command.command), command)
    plan = replace(plan, commands=tuple(commands.values()), special_stages=int(bool(special)))
    return MaterializationPlan(scope, plan, len(raw), len(support), stage, diagnostic)


def _replay_commands(item_id, count, definition_map, policy, identity, epoch):
    if policy.policy == SPECIAL_PROGRESSIVE:
        stages = range(min(count, len(definition_map[item_id]["perks"])))
        deliveries = [(stage, _progressive_delivery(item_id, definition_map, stage)) for stage in stages]
    else:
        deliveries = [(None, compile_item_delivery_plan(item_id, definition_map))]
        if not deliveries[0][1].commands:
            raise ValueError(f"replay-safe item {item_id} compiled no commands")
    return tuple(_replay_command(item_id, policy, delivery.index if stage is None else stage,
                                 identity, epoch, delivery.command, plan.description)
                 for stage, plan in deliveries for delivery in plan.commands)


def _replay_command(item_id, policy, stage, identity, epoch, command, description):
    return ReconciliationCommand(item_id, policy.name, policy.policy, stage,
        f"reconcile-{identity}-e{epoch}-item{item_id}-stage{stage}", command, description)


def _progressive_delivery(item_id, definitions, stage):
    plan = compile_item_delivery_plan(item_id, definitions, stage=stage)
    if len(plan.commands) != 1:
        raise ValueError(f"progressive item {item_id} stage {stage} must compile one command")
    return plan


def _replay(received_item_ids, definitions, registry, slot_identity, epoch, *, include_manual_replay=False):
    commands, selections = [], []
    replayed = special_stages = skipped_never = skipped_manual = 0
    for item_id, count in sorted(Counter(received_item_ids).items()):
        if item_id == 7770904:
            continue
        policy = registry[item_id]
        selected = ()
        if policy.policy == NEVER_REPLAY:
            skipped_never += 1
        elif policy.policy == REPLAY_MANUAL_ONLY and not include_manual_replay:
            skipped_manual += 1
        else:
            selected = _replay_commands(item_id, count, definitions, policy, slot_identity, epoch)
            replayed += policy.policy != SPECIAL_PROGRESSIVE
            special_stages += len(selected) if policy.policy == SPECIAL_PROGRESSIVE else 0
        commands.extend(selected)
        selections.append(ReconciliationSelection(item_id, policy.name, policy.policy, count,
                                                   tuple(command.command for command in selected)))
    return ReconciliationPlan(tuple(commands), tuple(selections), replayed, special_stages,
                               skipped_never, skipped_manual_replay=skipped_manual)


def _rune_entry(item_id, perk, native):
    registered = perk in native.registered_runes
    available = perk in native.available_perks
    if registered and available:
        reason = "equipped_player_choice_preserved" if perk in native.equipped_slots else "registered_and_available"
        return RunePlanEntry(item_id, perk, "noop", reason)
    if not native.observed:
        return RunePlanEntry(item_id, perk, "deferred", "native_snapshot_unavailable")
    elif not registered and (available or perk in native.active_perks or perk in native.equipped_slots):
        reason = "manager_registration_missing"
    elif registered:
        reason = "perk_availability_missing"
    else:
        reason = "perk_and_manager_registration_missing"
    return RunePlanEntry(item_id, perk, "repair_candidate", reason)


def _runes(ap_owned_item_ids, native, mapping, *, expected_rune_item_ids=None):
    expected = set(mapping) if expected_rune_item_ids is None else set(expected_rune_item_ids)
    owned = set(ap_owned_item_ids) & expected
    entries = tuple(_rune_entry(item_id, mapping[item_id], native) for item_id in sorted(owned))
    payload = {
        "owned": sorted(owned), "available": sorted(native.available_perks),
        "active": sorted(native.active_perks), "registered": sorted(native.registered_runes),
        "slots": list(native.equipped_slots), "page": native.page_unlocked,
        "save_slot": native.save_slot, "evidence_epoch": native.evidence_epoch,
    }
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    status = _rune_status(entries)
    return RuneReconciliationPlan(entries, fingerprint, status)


def _rune_status(entries):
    if any(entry.disposition == "repair_candidate" for entry in entries):
        return "repair_needed"
    return "deferred" if any(entry.disposition == "deferred" for entry in entries) else "noop"


def _gained_points(points):
    if points is None or points["native_exception"] or points["outcome"] not in (1, 2) or not points["flags"] & 16:
        return None
    return points["gained_after"]


def _delivery_items(inventory, special, runes, progress, context):
    items = _weapons(inventory) | _equipment(inventory) | _capacities(inventory) | _perks(inventory)
    items.update(_gate_keys(inventory, context, context.campaign))
    if progress is not None:
        items.update(_progression_items(progress, context, context.campaign))
    if special is not None:
        items.update(_special_items(special))
    if runes is not None:
        items.update(_rune_items(runes))
    return items


def _rune_items(result):
    from doom_eap.contracts.receipt_delivery import SUPPORT_RUNE_IDS

    items = {}
    for ids, field in ((NORMAL_RUNES, "owned_normal_after"), (sorted(SUPPORT_RUNE_IDS), "owned_support_after")):
        mask = result[field]
        if mask != 0xffffffff:
            items.update({item_id: _item(item_id, mask & (1 << bit)) for bit, item_id in enumerate(ids)})
    return items


def _currency_value(result, suffix, valid):
    if not result["flags"] & valid:
        return CurrencyValue()
    return CurrencyValue(result[f"balance_{suffix}"], result[f"gained_{suffix}"])


def _item(item_id, value, *, stage=0, source="sentinel_inventory"):
    return ItemObservation(item_id, OWNED if value else MISSING, observed_stage=stage, source=source)


def _weapons(result):
    mask = result["weapons_after"]
    if mask == 0xffffffff:
        return {}
    ids = (7770900, 7770003, 7770000, 7770005, 7770001,
           7770004, 7770002, 7770006, 7770010, 7770008)
    return {item_id: _item(item_id, mask & (1 << bit)) for bit, item_id in enumerate(ids)}


def _equipment(result):
    fields = ((7770011, "frag_grenade_after"), (7770013, "ice_bomb_after"),
              (7770015, "dash_after"), (7770012, "flame_belch_after"), (7770014, "blood_punch_after"))
    return {item_id: _item(item_id, result[field]) for item_id, field in fields if result.get(field, 255) in (0, 1)}


def _capacities(result):
    fields = ((7770017, "health"), (7770088, "armor"), (7770092, "ammo"))
    items = {}
    for item_id, family in fields:
        tier = result.get(f"{family}_tier_after", 255)
        if type(tier) is int and 0 <= tier <= 4:
            items[item_id] = _item(item_id, tier, stage=tier)
    return items


def _perks(result):
    known, owned = result.get("perks_known", 0), result.get("perks_owned", 0)
    items = {}
    for item_id in PERMANENT_PERK_ITEM_IDS | {7770145, 7770146, 7770147}:
        index = item_id - 7770145 + 61 if item_id >= 7770145 else item_id - 7770058
        if known & (1 << index):
            items[item_id] = _item(item_id, owned & (1 << index), source="sentinel_perk_availability")
    return items


def _gate_keys(result, context, campaign):
    key = result.get("slayer_key_after", 255)
    if key not in (0, 1) or context is None or context.campaign != campaign:
        return {}
    return {item_id: _item(item_id, key) for item_id, map_key in GATE_KEY_TO_MAP.items() if map_key in context.map_keys}


def _special_items(special):
    known = special.get("native_state_known", 0)
    items = {}
    for item_id, field, bit in ((7770007, "native_crucible", 1), (7770009, "native_hammer", 2)):
        if known & bit:
            items[item_id] = _item(item_id, special[field], stage=special[field], source="sentinel_special")
    if known & 7 == 7:
        hammer_stage = 2 if special["native_hammer_perks"] == 3 else 1
        stage = hammer_stage + 1 if special["native_hammer"] else int(bool(special["native_crucible"]))
        items[7770901] = _item(7770901, stage, stage=stage, source="sentinel_special")
    if known & 6 == 6:
        stage = _hammer_stage(special)
        items[7770902] = _item(7770902, stage, stage=stage, source="sentinel_special")
    return items


def _hammer_stage(special):
    if not special["native_hammer"]:
        return 0
    return 2 if special["native_hammer_perks"] == 3 else 1


def _facts_match(facts, result, link):
    if not all(_fact_matches(fact, result, link) for fact in facts if fact is not None):
        return False
    scopes = [fact.after_scope for fact in facts if fact is not None]
    return not scopes or all(scope == scopes[0] for scope in scopes)


def _progression_items(progression, context, campaign):
    items = {}
    quantity = progression.slayer_key_quantity
    if quantity is not None and context is not None and context.campaign == campaign:
        items.update({item_id: _item(item_id, quantity > 0, source="slayer_key_quantity")
                      for item_id, map_key in GATE_KEY_TO_MAP.items() if map_key in context.map_keys})
    if progression.meat_hook is not EffectProof.UNPROVEN:
        items[7770083] = _item(7770083, progression.meat_hook is EffectProof.PRESENT, source="sentinel_progression")
    return items


def _fact_matches(fact, result, link):
    scope = fact.after_scope
    if fact.before_scope != scope or scope.readiness is not SaveReadiness.READY:
        return False
    if (scope.pid, scope.namespace, scope.save_binding) != (
            link.pid, link.namespace, result.get("_observation_save_binding")):
        return False
    if canonical_map_name(scope.map_name) != canonical_map_name(result.get("_observation_map")):
        return False
    identity = result.get("_observation_scope")
    return bool(identity and (str(scope.process_created), scope.core_instance, str(scope.generation)) == (
        identity[0], identity[1], identity[3]))


def _validate_namespace_fingerprint(fingerprint):
    if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
        raise WeaponPointsBlocked("Missing generated native save fingerprint")
