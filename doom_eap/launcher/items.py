"""Typed requests and read results for Items; room and delivery records belong to the ledger."""

from dataclasses import dataclass
from enum import Enum

from doom_eap.runtime.ledger import Authority, ItemAction, ItemPage, RoomKey

from .events import LauncherEvent


class LedgerAvailability(str, Enum):
    AVAILABLE = "available"
    INACTIVE = "inactive"
    MISSING = "missing"
    BUSY = "busy"
    INCOMPATIBLE = "incompatible"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ItemsQuery:
    room: RoomKey
    request_id: str
    after_index: int = -1
    before_index: int | None = None
    limit: int = 64
    since: int | None = None
    watermark: int | None = None
    blocked_only: bool = False


@dataclass(frozen=True, eq=False)
class ItemsSnapshot(LauncherEvent):
    query: ItemsQuery
    availability: LedgerAvailability
    page: ItemPage | None
    authority: Authority | None
    online: bool
    attention_count: int | None = None


@dataclass(frozen=True, eq=False)
class ItemsChanged(ItemsSnapshot):
    pass


@dataclass(frozen=True)
class ItemResolutionRequest:
    room: RoomKey
    action: ItemAction
    request_id: str
