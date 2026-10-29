"""Ledger schema, migrations, connection configuration, writer lock and backups.
Record validation and storage value helpers live here.
"""

from __future__ import annotations

import os
import sqlite3
import time
import types
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Union, get_args, get_origin, get_type_hints
from uuid import uuid4

from .ledger_records import (
    AmmoChange,
    AmmoKind,
    AmmoOperation,
    AmmoState,
    AmmoTotals,
    Attempt,
    AttemptChange,
    Authority,
    Binding,
    CheckChange,
    CheckState,
    CommandChange,
    CommandPlan,
    CommandState,
    Context,
    InvalidRecord,
    NetworkReceipt,
    Observation,
    ReceiptDecision,
    ReceiptState,
    ReplayPolicy,
    RoomKey,
    SchemaError,
    StorageError,
    WriterBusy,
)

_APPLICATION_ID = 0x44454150
_ROOM_COLUMNS = "seed, team, slot, generation_fp"
_ROOM_WHERE = "seed=? AND team=? AND slot=? AND generation_fp=?"
_ROOM_FIELDS = "seed TEXT NOT NULL, team INTEGER NOT NULL CHECK(team>=0), slot INTEGER NOT NULL CHECK(slot>=0), generation_fp TEXT NOT NULL"
_ROOM_FK = f"FOREIGN KEY ({_ROOM_COLUMNS}) REFERENCES rooms ({_ROOM_COLUMNS}) ON DELETE RESTRICT"
_COMMAND_FIELDS = (
    "command_id",
    "receipt_index",
    "ammo_operation_id",
    "effect_key",
    "realization_key",
    "ordinal",
    "predecessor_id",
    "supersedes_id",
    "kind",
    "payload_revision",
    "payload",
    "context_id",
    "replay",
    "required",
    "retry_limit",
)


def _enum_check(column: str, enum: type[Enum]) -> str:
    values = ",".join(f"'{entry.value}'" for entry in enum)
    return f"CHECK({column} IN ({values}))"


def _reference(column: str, table: str, target: str) -> str:
    return f"FOREIGN KEY ({_ROOM_COLUMNS}, {column}) REFERENCES {table} ({_ROOM_COLUMNS}, {target}) ON DELETE RESTRICT"


def _immutable(connection: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> None:
    condition = " OR ".join(f"NEW.{name} IS NOT OLD.{name}" for name in columns)
    connection.execute(f"""CREATE TRIGGER {table}_immutable BEFORE UPDATE ON {table}
        WHEN {condition} BEGIN SELECT RAISE(ABORT, 'immutable {table} fields'); END""")


def _create_schema(connection: sqlite3.Connection) -> None:
    for statement in _SCHEMA:
        connection.execute(statement)
    _create_indexes(connection)
    _create_triggers(connection)


_SCHEMA = (
    """CREATE TABLE profile (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), database_id TEXT NOT NULL UNIQUE,
        writer_epoch INTEGER NOT NULL CHECK(writer_epoch>=0), created_at INTEGER NOT NULL CHECK(created_at>=0)
    ) STRICT""",
    f"""CREATE TABLE rooms (
        {_ROOM_FIELDS}, compatibility_revision INTEGER NOT NULL CHECK(compatibility_revision>=0),
        authority TEXT NOT NULL {_enum_check("authority", Authority)},
        receipt_boundary INTEGER NOT NULL DEFAULT 0 CHECK(receipt_boundary>=0), active_binding TEXT,
        active_context_id TEXT, activation_event_sequence INTEGER, blocked_reason TEXT,
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0), PRIMARY KEY ({_ROOM_COLUMNS}),
        CHECK((authority='ledger')=(activation_event_sequence IS NOT NULL)),
        {_reference("active_context_id", "contexts", "context_id")},
        {_reference("activation_event_sequence", "events", "sequence")} DEFERRABLE INITIALLY DEFERRED
    ) STRICT""",
    f"""CREATE TABLE receipts (
        {_ROOM_FIELDS}, ap_index INTEGER NOT NULL CHECK(ap_index>=0), item INTEGER NOT NULL,
        location INTEGER NOT NULL, player INTEGER NOT NULL CHECK(player>=0), flags INTEGER NOT NULL CHECK(flags>=0),
        received_at INTEGER NOT NULL CHECK(received_at>=0),
        state TEXT NOT NULL {_enum_check("state", ReceiptState)}, reason TEXT, evidence BLOB NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0), PRIMARY KEY ({_ROOM_COLUMNS}, ap_index), {_ROOM_FK}
    ) STRICT""",
    f"""CREATE TABLE contexts (
        context_id TEXT PRIMARY KEY NOT NULL, {_ROOM_FIELDS}, connection_id TEXT NOT NULL,
        writer_epoch INTEGER NOT NULL CHECK(writer_epoch>=0), pid INTEGER NOT NULL CHECK(pid>=0),
        process_created TEXT NOT NULL, native_instance TEXT NOT NULL, campaign TEXT NOT NULL,
        map_name TEXT NOT NULL, save_id TEXT NOT NULL, load_id TEXT NOT NULL,
        native_generation INTEGER NOT NULL CHECK(native_generation>=0),
        started_at INTEGER NOT NULL CHECK(started_at>=0), ended_at INTEGER CHECK(ended_at>=started_at),
        UNIQUE ({_ROOM_COLUMNS}, context_id), {_ROOM_FK}
    ) STRICT""",
    f"""CREATE TABLE ammo_operations (
        operation_id TEXT PRIMARY KEY NOT NULL, {_ROOM_FIELDS}, storage_key TEXT NOT NULL UNIQUE,
        request BLOB NOT NULL, request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
        kind TEXT NOT NULL {_enum_check("kind", AmmoKind)}, absolute_target INTEGER NOT NULL CHECK(absolute_target>=0),
        writer_id TEXT NOT NULL, state TEXT NOT NULL {_enum_check("state", AmmoState)},
        deadline_at INTEGER NOT NULL CHECK(deadline_at>=0), evidence BLOB NOT NULL, reason TEXT,
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
        UNIQUE ({_ROOM_COLUMNS}, operation_id), {_ROOM_FK}
    ) STRICT""",
    f"""CREATE TABLE commands (
        command_id TEXT PRIMARY KEY NOT NULL, {_ROOM_FIELDS}, receipt_index INTEGER CHECK(receipt_index>=0),
        ammo_operation_id TEXT, effect_key TEXT NOT NULL, realization_key TEXT NOT NULL,
        source_key TEXT GENERATED ALWAYS AS
            (CASE WHEN receipt_index IS NOT NULL THEN 'receipt:'||receipt_index ELSE 'ammo:'||ammo_operation_id END) VIRTUAL,
        ordinal INTEGER NOT NULL CHECK(ordinal>=0), predecessor_id TEXT CHECK(predecessor_id<>command_id),
        supersedes_id TEXT CHECK(supersedes_id<>command_id), kind TEXT NOT NULL,
        payload_revision INTEGER NOT NULL CHECK(payload_revision>=0), payload BLOB NOT NULL,
        payload_hash TEXT NOT NULL CHECK(length(payload_hash)=64), context_id TEXT NOT NULL,
        replay TEXT NOT NULL {_enum_check("replay", ReplayPolicy)}, required INTEGER NOT NULL CHECK(required IN (0,1)),
        state TEXT NOT NULL {_enum_check("state", CommandState)}, revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
        retry_limit INTEGER NOT NULL CHECK(retry_limit>=0), next_eligible_at INTEGER CHECK(next_eligible_at>=0),
        result BLOB NOT NULL, reason TEXT, CHECK((receipt_index IS NULL)<>(ammo_operation_id IS NULL)),
        UNIQUE ({_ROOM_COLUMNS}, command_id),
        UNIQUE ({_ROOM_COLUMNS}, source_key, effect_key, realization_key),
        {_ROOM_FK}, {_reference("receipt_index", "receipts", "ap_index")},
        {_reference("ammo_operation_id", "ammo_operations", "operation_id")},
        {_reference("context_id", "contexts", "context_id")},
        {_reference("predecessor_id", "commands", "command_id")},
        {_reference("supersedes_id", "commands", "command_id")}
    ) STRICT""",
    f"""CREATE TABLE attempts (
        command_id TEXT NOT NULL, attempt_no INTEGER NOT NULL CHECK(attempt_no>0), {_ROOM_FIELDS},
        request_id TEXT NOT NULL UNIQUE, connection_id TEXT NOT NULL,
        writer_epoch INTEGER NOT NULL CHECK(writer_epoch>=0), context_id TEXT NOT NULL,
        envelope BLOB NOT NULL, prepared_at INTEGER NOT NULL CHECK(prepared_at>=0),
        deadline_at INTEGER NOT NULL CHECK(deadline_at>=0), acceptance BLOB NOT NULL, result BLOB NOT NULL,
        completed_at INTEGER CHECK(completed_at>=prepared_at), revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
        PRIMARY KEY(command_id, attempt_no), UNIQUE ({_ROOM_COLUMNS}, command_id, attempt_no), {_ROOM_FK},
        {_reference("command_id", "commands", "command_id")}, {_reference("context_id", "contexts", "context_id")}
    ) STRICT""",
    f"""CREATE TABLE ammo_totals (
        {_ROOM_FIELDS}, credited INTEGER NOT NULL DEFAULT 0 CHECK(credited>=0),
        consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed>=0), discarded INTEGER NOT NULL DEFAULT 0 CHECK(discarded>=0),
        reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved>=0), observations BLOB NOT NULL DEFAULT X'',
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0), PRIMARY KEY ({_ROOM_COLUMNS}), {_ROOM_FK}
    ) STRICT""",
    f"""CREATE TABLE checks_outbox (
        {_ROOM_FIELDS}, location_id INTEGER NOT NULL CHECK(location_id>=0), observation BLOB NOT NULL,
        state TEXT NOT NULL {_enum_check("state", CheckState)}, send_attempts INTEGER NOT NULL CHECK(send_attempts>=0),
        next_attempt_at INTEGER CHECK(next_attempt_at>=0), acknowledgement BLOB NOT NULL, reason TEXT,
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0), PRIMARY KEY ({_ROOM_COLUMNS}, location_id), {_ROOM_FK}
    ) STRICT""",
    f"""CREATE TABLE events (
        sequence INTEGER PRIMARY KEY, {_ROOM_FIELDS}, observer_run TEXT,
        observer_sequence INTEGER CHECK(observer_sequence>=0), command_id TEXT, receipt_index INTEGER CHECK(receipt_index>=0),
        context_id TEXT, operation_id TEXT, location_id INTEGER CHECK(location_id>=0),
        attempt_no INTEGER CHECK(attempt_no>0), kind TEXT NOT NULL,
        previous_state TEXT, new_state TEXT, reason TEXT, evidence BLOB NOT NULL,
        wall_time INTEGER NOT NULL CHECK(wall_time>=0),
        CHECK((observer_run IS NULL)=(observer_sequence IS NULL)), UNIQUE(observer_run, observer_sequence),
        CHECK(attempt_no IS NULL OR command_id IS NOT NULL),
        UNIQUE ({_ROOM_COLUMNS}, sequence),
        {_ROOM_FK}, {_reference("command_id", "commands", "command_id")},
        {_reference("receipt_index", "receipts", "ap_index")}, {_reference("context_id", "contexts", "context_id")},
        {_reference("operation_id", "ammo_operations", "operation_id")},
        {_reference("location_id", "checks_outbox", "location_id")},
        FOREIGN KEY ({_ROOM_COLUMNS}, command_id, attempt_no)
            REFERENCES attempts ({_ROOM_COLUMNS}, command_id, attempt_no) ON DELETE RESTRICT
    ) STRICT""",
)


def _create_indexes(connection: sqlite3.Connection) -> None:
    indexes = (
        f"CREATE INDEX commands_runnable ON commands ({_ROOM_COLUMNS}, next_eligible_at, command_id) WHERE state='queued'",
        f"CREATE INDEX commands_receipt ON commands ({_ROOM_COLUMNS}, receipt_index)",
        f"CREATE INDEX commands_context ON commands ({_ROOM_COLUMNS}, context_id)",
        f"CREATE INDEX commands_predecessor ON commands ({_ROOM_COLUMNS}, predecessor_id)",
        f"CREATE INDEX commands_ammo ON commands ({_ROOM_COLUMNS}, ammo_operation_id)",
        "CREATE UNIQUE INDEX commands_replacement ON commands(supersedes_id) WHERE supersedes_id IS NOT NULL",
        "CREATE INDEX attempts_unresolved ON attempts(command_id, attempt_no) WHERE completed_at IS NULL",
        "CREATE UNIQUE INDEX attempts_one_unresolved ON attempts(command_id) WHERE completed_at IS NULL",
        f"CREATE INDEX checks_pending ON checks_outbox ({_ROOM_COLUMNS}, next_attempt_at, location_id) WHERE state IN ('pending','sent')",
        f"CREATE INDEX events_room ON events ({_ROOM_COLUMNS}, sequence)",
        "CREATE INDEX events_command ON events(command_id, sequence)",
        f"CREATE UNIQUE INDEX ammo_unresolved ON ammo_operations ({_ROOM_COLUMNS}, kind) WHERE state IN ('prepared','requested','unknown')",
    )
    for statement in indexes:
        connection.execute(statement)


def _create_triggers(connection: sqlite3.Connection) -> None:
    room_fields = tuple(_ROOM_COLUMNS.split(", "))
    identities = {
        "profile": ("singleton", "database_id", "created_at"),
        "rooms": (*room_fields, "compatibility_revision", "authority", "activation_event_sequence"),
        "receipts": (*room_fields, "ap_index", "item", "location", "player", "flags", "received_at"),
        "commands": (*room_fields, *_COMMAND_FIELDS, "payload_hash"),
        "contexts": (
            *room_fields,
            "context_id",
            "connection_id",
            "writer_epoch",
            "pid",
            "process_created",
            "native_instance",
            "campaign",
            "map_name",
            "save_id",
            "load_id",
            "native_generation",
            "started_at",
        ),
        "attempts": (
            *room_fields,
            "command_id",
            "attempt_no",
            "request_id",
            "connection_id",
            "writer_epoch",
            "context_id",
            "envelope",
            "prepared_at",
            "deadline_at",
        ),
        "ammo_operations": (
            *room_fields,
            "operation_id",
            "storage_key",
            "request",
            "request_hash",
            "kind",
            "absolute_target",
            "writer_id",
            "deadline_at",
        ),
        "checks_outbox": (*room_fields, "location_id", "observation"),
        "ammo_totals": room_fields,
    }
    for table, columns in identities.items():
        _immutable(connection, table, columns)
    for action in ("UPDATE", "DELETE"):
        connection.execute(f"""CREATE TRIGGER events_no_{action.lower()} BEFORE {action} ON events
            BEGIN SELECT RAISE(ABORT, 'events are append only'); END""")
    for table in ("rooms", "receipts", "commands", "attempts", "ammo_operations", "ammo_totals", "checks_outbox"):
        connection.execute(f"""CREATE TRIGGER {table}_revision BEFORE UPDATE ON {table}
            WHEN NEW.revision<>OLD.revision+1 BEGIN SELECT RAISE(ABORT, 'revision must advance once'); END""")
    connection.execute("""CREATE TRIGGER profile_epoch BEFORE UPDATE ON profile
        WHEN NEW.writer_epoch<>OLD.writer_epoch+1 BEGIN SELECT RAISE(ABORT, 'writer epoch must advance once'); END""")


_MIGRATIONS = {1: _create_schema}


def _now() -> int:
    return time.time_ns() // 1_000


def _room_values(room: RoomKey) -> tuple:
    return room.seed, room.team, room.slot, room.generation_fp


def _values(record: object, names: tuple[str, ...]) -> tuple:
    return tuple(getattr(record, name) for name in names)


@cache
def _record_types(record_type: type) -> dict:
    return get_type_hints(record_type)


def _validate(value: object, expected: object, name: str = "record") -> None:
    origin = get_origin(expected)
    if origin in (types.UnionType, Union):
        _validate_union(value, expected, name)
    elif origin is tuple:
        _validate_tuple(value, get_args(expected)[0], name)
    elif expected in _record_classes:
        _validate_record(value, expected, name)
    else:
        _validate_scalar(value, expected, name)


def _validate_union(value: object, expected: object, name: str) -> None:
    if value is None:
        if type(None) not in get_args(expected):
            raise InvalidRecord(f"{name} cannot be absent")
        return
    alternatives = tuple(entry for entry in get_args(expected) if entry is not type(None))
    _validate(value, alternatives[0], name)


def _validate_tuple(value: object, entry_type: object, name: str) -> None:
    if type(value) is not tuple:
        raise InvalidRecord(f"{name} must be a tuple")
    for entry in value:
        _validate(entry, entry_type, name)


def _validate_record(value: object, expected: type, name: str) -> None:
    if type(value) is not expected:
        raise InvalidRecord(f"{name} must be {expected.__name__}")
    for field, field_type in _record_types(expected).items():
        _validate(getattr(value, field), field_type, field)


def _validate_scalar(value: object, expected: type, name: str) -> None:
    if type(value) is not expected:
        raise InvalidRecord(f"{name} must be {expected.__name__}")
    if expected is int:
        _validate_integer(value, name)
    if expected is str and not value:
        raise InvalidRecord(f"{name} cannot be empty")


def _validate_integer(value: int, name: str) -> None:
    if not -(2**63) <= value < 2**63:
        raise InvalidRecord(f"{name} is outside the database integer range")
    if name not in ("item", "location", "after_index") and value < 0:
        raise InvalidRecord(f"{name} must be nonnegative")


_record_classes = {
    RoomKey,
    Binding,
    NetworkReceipt,
    Context,
    CommandPlan,
    ReceiptDecision,
    CommandChange,
    Attempt,
    AttemptChange,
    Observation,
    AmmoOperation,
    AmmoChange,
    AmmoTotals,
    CheckChange,
}


def _writer_lock(path: Path):
    handle = path.with_name(path.name + ".writer.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        handle.close()
        raise WriterBusy("another writer has this database open") from error
    return handle


def _configure(connection: sqlite3.Connection, read_only: bool) -> None:
    mode = connection.execute("PRAGMA journal_mode" if read_only else "PRAGMA journal_mode=WAL").fetchone()[0]
    if mode.lower() != "wal":
        raise StorageError("database requires WAL journal mode")
    settings = {
        "synchronous": 2,
        "foreign_keys": 1,
        "busy_timeout": 5000,
        "locking_mode": "normal",
        "wal_autocheckpoint": 1000,
    }
    for name, value in settings.items():
        connection.execute(f"PRAGMA {name}={value}")
        actual = connection.execute(f"PRAGMA {name}").fetchone()[0]
        if actual != value:
            raise StorageError(f"could not set database {name}")
    if read_only:
        connection.execute("PRAGMA query_only=ON")


def _schema_version(connection: sqlite3.Connection, create: bool) -> int:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    application = connection.execute("PRAGMA application_id").fetchone()[0]
    latest = max(_MIGRATIONS)
    if version > latest:
        raise SchemaError("database schema is newer than this reader")
    if not create and (application != _APPLICATION_ID or version < 1):
        raise SchemaError("database is not an initialized delivery ledger")
    return version


def _migrate(connection: sqlite3.Connection, path: Path, create: bool, read_only: bool, version: int) -> None:
    latest = max(_MIGRATIONS)
    if version == latest:
        return
    if read_only:
        raise SchemaError("database needs a writer to upgrade it")
    if not create:
        _backup(connection, path, version)
    _run_migrations(connection, version, latest, create)


def _backup(connection: sqlite3.Connection, path: Path, version: int) -> None:
    target = path.with_name(f"{path.name}.v{version}.{uuid4().hex}.backup")
    backup = sqlite3.connect(target)
    try:
        connection.backup(backup)
    finally:
        backup.close()


def _run_migrations(connection: sqlite3.Connection, version: int, latest: int, create: bool) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        for number in range(version + 1, latest + 1):
            _MIGRATIONS[number](connection)
            connection.execute(f"PRAGMA user_version={number}")
        if create:
            connection.execute("INSERT INTO profile VALUES(1, ?, 0, ?)", (str(uuid4()), _now()))
        connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
