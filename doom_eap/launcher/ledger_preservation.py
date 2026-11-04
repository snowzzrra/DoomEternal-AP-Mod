"""Preserve SQLite with save copies and record explicit save restarts without erasing delivery history."""

import asyncio
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from doom_eap.runtime.ledger import Authority, Ledger, RoomKey, StorageError

from .ui.strings import text


def room_key(snapshot):
    return RoomKey(
        snapshot.seed_name, snapshot.team, snapshot.slot, snapshot.slot_data["native_generation_fingerprint"]
    )


async def _read(config, room, destination=None):
    path = config.get("delivery_ledger_file")
    if not path:
        return None
    path = Path(path)
    if not path.is_file():
        if config.get("delivery_ledger_expected") or path.with_suffix(".expected").exists():
            raise StorageError(text("launcher.items", "missing"))
        return None
    ledger = await Ledger.open(path, read_only=True)
    try:
        stored = await ledger.room(room)
        if stored is not None and destination is not None:
            await ledger.backup(destination)
        return stored
    finally:
        await ledger.close()


@contextmanager
def delivery_backup(config, room, directory):
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (uuid4().hex + ".sqlite3")
    try:
        stored = asyncio.run(_read(config, room, destination))
        yield destination if stored is not None else None
    finally:
        destination.unlink(missing_ok=True)


@contextmanager
def delivery_restart(config, room):
    stored = asyncio.run(_read(config, room))
    ledger = None
    try:
        if stored is not None and stored.authority is Authority.LEDGER:
            ledger = asyncio.run(Ledger.open(Path(config["delivery_ledger_file"])))
        yield ledger
    finally:
        if ledger is not None:
            asyncio.run(ledger.close())


async def audit_new_save(ledger, room, epoch_id, archive):
    if ledger is None:
        return
    stored = await ledger.room(room)
    binding = await ledger.bind(room, str(uuid4()), compatibility_revision=stored.compatibility_revision,
                                authority=Authority.LEDGER, expected_revision=stored.revision)
    await ledger.new_save_epoch(binding, epoch_id, str(archive.resolve()).encode("utf-8"), pending=True)
    return binding


def finish_new_save(ledger, binding, epoch_id, *, applied):
    if ledger is not None and binding is not None:
        asyncio.run(ledger.finish_save_epoch(binding, epoch_id, applied=applied))
