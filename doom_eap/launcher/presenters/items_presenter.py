"""Reduce scoped ledger pages into Items state; no Qt, disk access, or delivery decisions."""

from dataclasses import dataclass, replace
from uuid import uuid4

from doom_eap.runtime.ledger_records import (
    Authority,
    ItemAction,
    ItemReason,
    ItemRow,
    ItemStatus,
    Resolution,
    ResolutionAction,
    RoomKey,
)

from ..items import ItemResolutionRequest, ItemsQuery, ItemsSnapshot, LedgerAvailability
from ..ui.strings import text


@dataclass(frozen=True)
class ItemsModel:
    room: RoomKey | None = None
    rows: tuple[ItemRow, ...] = ()
    availability: LedgerAvailability = LedgerAvailability.INACTIVE
    online: bool = False
    can_resolve: bool = False
    can_spend: bool = False
    more: bool = False
    loading: bool = False
    message: str = ""


class ItemsPresenter:
    def __init__(self):
        self.model = ItemsModel()
        self.pending: ItemsQuery | None = None
        self.snapshot_watermark: int | None = None
        self.watermark: int | None = None
        self.oldest: int | None = None
        self.resolution: ItemResolutionRequest | None = None
        self.submitted = False
        self.authority: Authority | None = None
        self.blocked_only = False
        self.local_item_id = None
        self.local_actions = ()

    def activate(self, room: RoomKey, *, online: bool):
        if room != self.model.room:
            blocked_only = self.blocked_only
            self.__init__()
            self.blocked_only = blocked_only
            self.model = replace(self.model, room=room)
        if online != self.model.online:
            self.pending = None
            self.resolution = None
            self.submitted = False
        self.model = replace(self.model, online=online, can_resolve=False, message="")

    def query(self, *, more=False, refresh=False) -> ItemsQuery | None:
        if self.model.room is None or self.pending is not None:
            return None
        if refresh:
            self.snapshot_watermark = self.watermark = self.oldest = None
            self.model = replace(self.model, rows=(), message="")
            self.resolution = None
            self.submitted = False
        initial = self.watermark is None or more
        self.pending = ItemsQuery(
            self.model.room,
            str(uuid4()),
            before_index=self.oldest if more else None,
            since=None if initial else self.watermark,
            watermark=self.snapshot_watermark if more else None,
            blocked_only=self.blocked_only,
        )
        self.model = replace(self.model, loading=True)
        return self.pending

    def observe(self, event: ItemsSnapshot) -> ItemsQuery | None:
        if event.query != self.pending or event.query.room != self.model.room:
            return None
        self.pending = None
        self.model = replace(
            self.model, availability=event.availability, online=event.online, loading=False, message=""
        )
        if event.page is None:
            self.model = replace(self.model, can_resolve=False, rows=(), more=False)
            self.snapshot_watermark = self.watermark = self.oldest = None
            self.resolution = None
            self.submitted = False
            return None
        self.authority = event.authority
        if not event.online or event.authority is not Authority.LEDGER:
            self.model = replace(self.model, can_resolve=False)
        self._merge(event)
        if event.query.since is None:
            self.snapshot_watermark = event.page.watermark
            if self.watermark is None:
                self.watermark = event.page.watermark
            self.oldest = min((row.ap_index for row in event.page), default=self.oldest)
            self.model = replace(self.model, more=len(event.page) == event.query.limit)
        elif len(event.page) == event.query.limit:
            self.pending = replace(
                event.query,
                after_index=event.page[-1].ap_index,
                watermark=event.page.watermark,
                request_id=str(uuid4()),
            )
            self.model = replace(self.model, loading=True)
            return self.pending
        else:
            self.watermark = event.page.watermark
        return None

    def cancel_query(self):
        self.pending = None
        self.model = replace(self.model, loading=False)

    def filter_blocked(self, enabled: bool) -> ItemsQuery | None:
        self.cancel_query()
        self.blocked_only = enabled
        return self.query(refresh=True)

    def _merge(self, event):
        rows = {row.ap_index: row for row in self.model.rows}
        for row in event.page:
            if event.authority is not Authority.LEDGER:
                status = ItemStatus.BLOCKED if row.status is ItemStatus.BLOCKED else ItemStatus.RECEIVED
                code = ItemReason.ROOM_BLOCKED if status is ItemStatus.BLOCKED else ItemReason.PLANNING
                row = replace(row, status=status, actions=(), resolution=None, reason_code=code)
            rows[row.ap_index] = row
        self.model = replace(self.model, rows=tuple(rows[index] for index in sorted(rows, reverse=True)))
        if self.resolution and not self.submitted and self.resolution.action not in self.actions():
            self.resolution = None

    def capabilities(self, room, *, can_resolve: bool, can_spend: bool = False):
        if room == self.model.room:
            allowed = can_resolve and self.model.online and self.authority is Authority.LEDGER
            self.model = replace(self.model, can_resolve=allowed, can_spend=can_spend and allowed)
            if not allowed:
                self.resolution = None
                self.submitted = False

    def actions(self, index: int | None = None) -> tuple[ItemAction, ...]:
        if not self.model.can_resolve or self.model.availability is not LedgerAvailability.AVAILABLE:
            return ()
        actions = tuple(
            action for row in self.model.rows if index is None or row.ap_index == index for action in row.actions
        )
        return actions + (self.local_actions if index is None else ())

    def local_status(self, room, item_id, actions):
        if room != self.model.room:
            return
        self.local_item_id = item_id
        self.local_actions = tuple(action for action in actions if not action.command_id.startswith("ammo-operation:")
                                   or action.kind is ResolutionAction.TRY_AGAIN)
        if self.resolution and not self.submitted and self.resolution.action not in self.actions():
            self.resolution = None

    def select_action(self, action: ItemAction):
        if action in self.actions() and not self.submitted:
            self.resolution = ItemResolutionRequest(self.model.room, action, str(uuid4()))

    def confirm(self) -> ItemResolutionRequest | None:
        if self.resolution is None or self.submitted or self.resolution.action not in self.actions():
            return None
        self.submitted = True
        return self.resolution

    def resolution_result(self, room, request_id, *, ok):
        if self.resolution is None or (room, request_id) != (self.model.room, self.resolution.request_id):
            return False
        self.resolution = None
        self.submitted = False
        self.model = replace(self.model, message=text("launcher.items", "resolution_ok" if ok else "resolution_failed"))
        return True

    @staticmethod
    def status(row: ItemRow) -> str:
        resolved = row.status is ItemStatus.DELIVERED and row.resolution is Resolution.ACCEPTED_BY_PLAYER
        key = "player_resolved" if resolved else row.status.value
        return text("launcher.items", key)

    @staticmethod
    def details(row: ItemRow) -> str:
        detail = text("launcher.items", "reason_" + row.reason_code.value)
        if row.resolution is not None and row.reason_code is not ItemReason.PLAYER_RESOLVED:
            detail += "\n" + text("launcher.items", "reason_player_resolved")
        return detail
