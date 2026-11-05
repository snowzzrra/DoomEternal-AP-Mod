"""Public ledger exceptions, enums and frozen record dataclasses."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum


class LedgerError(Exception):
    """The ledger could not complete an operation."""


class InvalidRecord(LedgerError):
    pass


class StorageError(LedgerError):
    pass


class SchemaError(StorageError):
    pass


class WriterBusy(StorageError):
    pass


class Conflict(LedgerError):
    pass


class StaleBinding(Conflict):
    pass


class RevisionConflict(Conflict):
    pass


class HistoryConflict(Conflict):
    pass


class LedgerClosed(LedgerError):
    pass


class WorkerFull(LedgerError):
    pass


class CommandState(str, Enum):
    PLANNED = "planned"
    DEFERRED = "deferred"
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    CONFIRMED = "confirmed"
    STALE = "stale"
    REJECTED = "rejected"
    EFFECT_UNKNOWN = "effect_unknown"


class ReceiptState(str, Enum):
    RECEIVED = "received"
    DEFERRED = "deferred"
    PLANNED = "planned"
    SATISFIED = "satisfied"
    REJECTED = "rejected"


class ReplayPolicy(str, Enum):
    IDEMPOTENT = "idempotent"
    ADDITIVE = "additive"


class ItemStatus(str, Enum):
    RECEIVED = "received"
    WAITING = "waiting"
    DELIVERED = "delivered"
    BLOCKED = "blocked"


class ItemReason(str, Enum):
    PLANNING = "planning"
    EXECUTION = "execution"
    CONTEXT = "context"
    DEPENDENCY = "dependency"
    CONFIRMATION = "confirmation"
    REJECTED = "rejected"
    EFFECT_UNKNOWN = "effect_unknown"
    ROOM_BLOCKED = "room_blocked"
    PLAYER_RESOLVED = "player_resolved"
    NONE = "none"


class Authority(str, Enum):
    SHADOW = "shadow"
    LEDGER = "ledger"




class SourceKind(str, Enum):
    STARTING_GRANT = "starting_grant"
    DERIVED_FACT = "derived_fact"
    BOOTSTRAP = "bootstrap"
    TEMPORARY_CLEANUP = "temporary_cleanup"


class SaveEpochState(str, Enum):
    PREPARED = "prepared"
    APPLIED = "applied"
    ABORTED = "aborted"


class ResolutionAction(str, Enum):
    TRY_AGAIN = "try_again"
    MARK_AS_RECEIVED = "mark_as_received"


class Resolution(str, Enum):
    ACCEPTED_BY_PLAYER = "accepted_by_player"
    REPLACED = "replaced"


class AmmoKind(str, Enum):
    SPEND = "spend"
    DISCARD = "discard"


class AmmoState(str, Enum):
    PREPARED = "prepared"
    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class CheckState(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class RoomKey:
    seed: str
    team: int
    slot: int
    generation_fp: str


@dataclass(frozen=True)
class Binding:
    room: RoomKey
    writer_epoch: int
    connection_id: str


@dataclass(frozen=True)
class NetworkReceipt:
    item: int
    location: int
    player: int
    flags: int


@dataclass(frozen=True)
class Context:
    context_id: str
    binding: Binding
    pid: int
    process_created: str
    native_instance: str
    campaign: str
    map_name: str
    save_id: str
    load_id: str
    native_generation: int
    save_binding_id: str | None = None


@dataclass(frozen=True)
class EffectSource:
    source_id: str
    kind: SourceKind
    source_key: str
    evidence: bytes


@dataclass(frozen=True)
class CommandPlan:
    command_id: str
    receipt_index: int | None
    ammo_operation_id: str | None
    effect_key: str
    realization_key: str
    ordinal: int
    predecessor_id: str | None
    supersedes_id: str | None
    kind: str
    payload_revision: int
    payload: bytes
    context_id: str
    replay: ReplayPolicy
    required: bool
    retry_limit: int
    local_source_id: str | None = None


@dataclass(frozen=True)
class ManualResolution:
    command_id: str
    expected_revision: int
    action: ResolutionAction
    replacement: CommandPlan | None = None
    evidence: bytes = b""


@dataclass(frozen=True)
class ReceiptDecision:
    ap_index: int
    state: ReceiptState
    reason: str | None
    evidence: bytes
    expected_revision: int = 0


@dataclass(frozen=True)
class CommandChange:
    command_id: str
    expected_revision: int
    state: CommandState
    reason: str | None
    evidence: bytes
    next_eligible_at: int | None


@dataclass(frozen=True)
class Attempt:
    command_id: str
    number: int
    request_id: str
    binding: Binding
    context_id: str
    envelope: bytes
    deadline_at: int
    expected_revision: int = 0
    reference: bytes = b""


@dataclass(frozen=True)
class AttemptRow:
    attempt: Attempt
    envelope_fingerprint: str
    prepared_at: int
    acceptance: bytes
    result: bytes
    completed_at: int | None
    revision: int


@dataclass(frozen=True)
class AttemptChange:
    command_id: str
    number: int
    expected_revision: int
    acceptance: bytes
    result: bytes
    completed_at: int | None


@dataclass(frozen=True)
class Observation:
    run_id: str
    sequence: int
    kind: str
    command_id: str | None
    receipt_index: int | None
    evidence: bytes


@dataclass(frozen=True)
class AmmoOperation:
    operation_id: str
    storage_key: str
    request: bytes
    kind: AmmoKind
    absolute_target: int
    writer_id: str
    deadline_at: int


@dataclass(frozen=True)
class AmmoChange:
    operation_id: str
    expected_revision: int
    state: AmmoState
    evidence: bytes
    reason: str | None


@dataclass(frozen=True)
class AmmoOperationRow:
    operation: AmmoOperation
    state: AmmoState
    revision: int
    evidence: bytes
    reason: str | None


@dataclass(frozen=True)
class AmmoTotals:
    expected_revision: int
    credited: int
    consumed: int
    discarded: int
    reserved: int
    observations: bytes


@dataclass(frozen=True)
class CheckChange:
    location_id: int
    expected_revision: int | None
    state: CheckState
    observation: bytes
    send_attempts: int
    next_attempt_at: int | None
    acknowledgement: bytes
    reason: str | None


@dataclass(frozen=True)
class Commit:
    event_sequence: int
    receipt_boundary: int


@dataclass(frozen=True)
class ItemRow:
    ap_index: int
    item: int
    sender: int
    status: ItemStatus
    reason: str | None
    actions: tuple[ItemAction, ...]
    event_sequence: int
    reason_code: ItemReason = ItemReason.NONE
    revision: int = 0
    resolution: Resolution | None = None
    flags: int | None = None
    received_at: int | None = None


@dataclass(frozen=True)
class ItemAction:
    kind: ResolutionAction
    command_id: str
    expected_revision: int


@dataclass(frozen=True)
class ItemPage(Sequence[ItemRow]):
    rows: tuple[ItemRow, ...]
    watermark: int

    def __getitem__(self, index):
        return self.rows[index]

    def __len__(self):
        return len(self.rows)


@dataclass(frozen=True)
class Room:
    key: RoomKey
    compatibility_revision: int
    authority: Authority
    receipt_boundary: int
    active_binding: str | None
    active_context_id: str | None
    activation_event_sequence: int | None
    blocked_reason: str | None
    revision: int


@dataclass(frozen=True)
class SaveEpoch:
    epoch_id: str
    receipt_boundary: int
    evidence: bytes
    state: SaveEpochState = SaveEpochState.APPLIED


@dataclass(frozen=True)
class CommandRow:
    plan: CommandPlan
    state: CommandState
    revision: int
    attempt_count: int
    next_eligible_at: int | None
    result: bytes
    reason: str | None
    resolution: Resolution | None = None
    replacement_id: str | None = None
