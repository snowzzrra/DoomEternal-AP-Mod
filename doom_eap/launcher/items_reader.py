"""Read the profile ledger through its public API; never create or mutate delivery storage."""

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from doom_eap.runtime.ledger import InvalidRecord, Ledger, SchemaError, StorageError, WorkerFull, WriterBusy

from .events import EventContext, EventOrigin
from .items import ItemsChanged, ItemsQuery, ItemsSnapshot, LedgerAvailability


class LedgerReadAdapter:
    def __init__(self, path: Path, *, expected=True):
        self.path = path.resolve()
        self.expected = expected

    async def read(self, query: ItemsQuery, *, online: bool = False) -> ItemsSnapshot:
        if not self.path.is_file():
            expected = self.expected or self.path.with_suffix(".expected").is_file()
            state = LedgerAvailability.MISSING if expected else LedgerAvailability.INACTIVE
            return self._event(query, state, online=online)
        self.expected = True
        try:
            return await self._read(query, online)
        except SchemaError:
            return self._event(query, LedgerAvailability.INCOMPATIBLE, online=online)
        except (WriterBusy, WorkerFull):
            return self._event(query, LedgerAvailability.BUSY, online=online)
        except (StorageError, InvalidRecord, OSError):
            return self._event(query, LedgerAvailability.UNAVAILABLE, online=online)

    async def _read(self, query, online):
        ledger = await Ledger.open(self.path, read_only=True)
        try:
            room = await ledger.room(query.room)
            if room is None:
                return self._event(query, LedgerAvailability.INACTIVE, online=online)
            page = await ledger.items(
                query.room,
                query.after_index,
                query.limit,
                since=query.since,
                watermark=query.watermark,
                before_index=query.before_index,
                newest_first=query.since is None,
                blocked_only=query.blocked_only,
            )
            # the read adapter cannot authorize mutations, even when the database belongs to an owner
            page = replace(page, rows=tuple(replace(row, actions=()) for row in page)) if not online else page
            attention_count = await self._attention_count(ledger, query.room, page.watermark)
            return self._event(query, LedgerAvailability.AVAILABLE, page, room.authority, online, attention_count)
        finally:
            await ledger.close()

    @staticmethod
    async def _attention_count(ledger, room, watermark):
        count, before = 0, None
        while True:
            page = await ledger.items(room, -1, 256, watermark=watermark, before_index=before,
                                      newest_first=True, blocked_only=True)
            count += len(page)
            if len(page) < 256:
                return count
            before = page[-1].ap_index

    @staticmethod
    def _event(query, availability, page=None, authority=None, online=False, attention_count=None):
        changed = query.since is not None
        kind = "items_changed" if changed else "items_snapshot"
        context = EventContext(EventOrigin.APPLICATION, room=tuple(vars(query.room).values()))
        event = ItemsChanged if changed else ItemsSnapshot
        return event(context, kind, MappingProxyType({"type": kind}), query, availability, page, authority, online, attention_count)
