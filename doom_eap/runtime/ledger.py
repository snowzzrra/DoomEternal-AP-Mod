"""Asynchronous Ledger operations and read-model helpers.
Re-exports the public ledger record types.
"""

from __future__ import annotations

import asyncio
import os
import queue
import sqlite3
import threading
from concurrent.futures import Future
from dataclasses import fields
from hashlib import sha256
from pathlib import Path

from .ledger_records import (
    AmmoChange,
    AmmoOperation,
    AmmoState,
    AmmoTotals,
    Attempt,
    AttemptChange,
    Authority,
    Binding,
    CheckChange,
    CommandChange,
    CommandPlan,
    CommandRow,
    CommandState,
    Commit,
    Conflict,
    Context,
    HistoryConflict,
    InvalidRecord,
    ItemRow,
    ItemStatus,
    LedgerClosed,
    LedgerError,
    NetworkReceipt,
    Observation,
    ReceiptDecision,
    ReceiptState,
    ReplayPolicy,
    RevisionConflict,
    Room,
    RoomKey,
    StaleBinding,
    StorageError,
    WorkerFull,
)
from .ledger_records import AmmoKind as AmmoKind
from .ledger_records import CheckState as CheckState
from .ledger_records import SchemaError as SchemaError
from .ledger_records import WriterBusy as WriterBusy
from .ledger_schema import (
    _COMMAND_FIELDS,
    _ROOM_COLUMNS,
    _ROOM_WHERE,
    _configure,
    _migrate,
    _now,
    _room_values,
    _schema_version,
    _validate,
    _writer_lock,
)
from .ledger_schema import (
    _MIGRATIONS as _MIGRATIONS,
)


class Ledger:
    def __init__(self, path: Path, create: bool, read_only: bool, capacity: int) -> None:
        self._path = path
        self._create = create
        self._read_only = read_only
        self._queue = queue.Queue(maxsize=capacity)
        self._ready = Future()
        self._closed = False
        self._close_future = None
        self._thread = threading.Thread(target=self._worker, name="delivery-ledger", daemon=True)

    @classmethod
    async def create(cls, path: Path, *, capacity: int = 64) -> Ledger:
        return await cls._start(path, True, False, capacity)

    @classmethod
    async def open(cls, path: Path, *, read_only: bool = False, capacity: int = 64) -> Ledger:
        return await cls._start(path, False, read_only, capacity)

    @classmethod
    async def _start(cls, path: Path, create: bool, read_only: bool, capacity: int) -> Ledger:
        if not isinstance(path, Path) or not path.is_absolute():
            raise InvalidRecord("database path must be an absolute Path")
        _validate(read_only, bool, "read_only")
        _validate(capacity, int, "capacity")
        if capacity < 1:
            raise InvalidRecord("worker capacity must be positive")
        ledger = cls(path.resolve(), create, read_only, capacity)
        ledger._thread.start()
        try:
            await asyncio.shield(asyncio.wrap_future(ledger._ready))
        except asyncio.CancelledError:
            await ledger.close()
            raise
        return ledger

    def _connect(self) -> sqlite3.Connection:
        if self._create:
            descriptor = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        mode = "ro" if self._read_only else "rw"
        connection = sqlite3.connect(f"{self._path.as_uri()}?mode={mode}", uri=True, isolation_level=None)
        self._connection = connection
        connection.row_factory = sqlite3.Row
        version = _schema_version(connection, self._create)
        _configure(connection, self._read_only)
        _migrate(connection, self._path, self._create, self._read_only, version)
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise StorageError("database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StorageError("database references are damaged")
        self._epoch = connection.execute("SELECT writer_epoch FROM profile WHERE singleton=1").fetchone()[0]
        if not self._read_only:
            self._claim_writer()
        return connection

    def _claim_writer(self) -> None:
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("UPDATE profile SET writer_epoch=writer_epoch+1 WHERE singleton=1")
            self._epoch = connection.execute("SELECT writer_epoch FROM profile WHERE singleton=1").fetchone()[0]
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def _worker(self) -> None:
        lock = None
        failure = None
        self._connection = None
        try:
            if not self._read_only:
                lock = _writer_lock(self._path)
            self._connect()
            self._ready.set_result(None)
            self._work_loop()
        except Exception as error:
            failure = self._error(error)
        finally:
            if self._connection is not None:
                self._connection.close()
            if lock is not None:
                lock.close()
            if failure is not None and not self._ready.done():
                self._ready.set_exception(failure)
            if self._close_future is not None:
                self._close_future.set_result(None)

    def _work_loop(self) -> None:
        while not self._closed or not self._queue.empty():
            try:
                request = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            operation, arguments, future, mutate = request
            try:
                result = self._execute(operation, arguments, mutate)
            except Exception as error:
                future.set_exception(self._error(error))
            else:
                future.set_result(result)

    @staticmethod
    def _error(error: Exception) -> LedgerError:
        if isinstance(error, LedgerError):
            return error
        if isinstance(error, sqlite3.IntegrityError):
            return Conflict(str(error))
        return StorageError(str(error))

    def _execute(self, operation, arguments: tuple, mutate: bool):
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE" if mutate else "BEGIN")
        try:
            result = operation(*arguments)
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise

    async def _submit(self, operation, arguments: tuple, mutate: bool = True):
        if self._closed:
            raise LedgerClosed("ledger is closed")
        if mutate and self._read_only:
            raise StorageError("ledger is read only")
        future = Future()
        try:
            self._queue.put_nowait((operation, arguments, future, mutate))
        except queue.Full as error:
            raise WorkerFull("ledger worker is full") from error
        return await asyncio.shield(asyncio.wrap_future(future))

    async def close(self) -> None:
        if self._close_future is None:
            self._close_future = Future()
            self._closed = True
            if not self._thread.is_alive():
                self._close_future.set_result(None)
        await asyncio.shield(asyncio.wrap_future(self._close_future))

    async def bind(
        self,
        room: RoomKey,
        connection_id: str,
        *,
        compatibility_revision: int,
        authority: Authority = Authority.SHADOW,
        expected_revision: int | None = None,
    ) -> Binding:
        _validate(room, RoomKey)
        _validate(connection_id, str, "connection_id")
        _validate(compatibility_revision, int, "compatibility_revision")
        _validate(authority, Authority)
        _validate(expected_revision, int | None, "expected_revision")
        return await self._submit(
            self._bind, (room, connection_id, compatibility_revision, authority, expected_revision)
        )

    def _bind(self, room, connection_id, compatibility_revision, authority, expected_revision):
        row = self._room(room)
        if row is None:
            if expected_revision is not None:
                raise RevisionConflict("room does not exist")
            activation = None
            if authority is Authority.LEDGER:
                activation = self._connection.execute("SELECT coalesce(max(sequence),0)+1 FROM events").fetchone()[0]
            self._insert(
                "rooms",
                self._room_dict(room),
                compatibility_revision=compatibility_revision,
                authority=authority,
                active_binding=connection_id,
                activation_event_sequence=activation,
            )
            self._insert("ammo_totals", self._room_dict(room))
            self._event(room, "room_created", new_state=authority)
        else:
            self._check_room_identity(row, compatibility_revision, authority)
            self._update("rooms", room, {"active_binding": connection_id}, expected_revision)
            self._event(room, "binding_changed")
        return Binding(room, self._epoch, connection_id)

    @staticmethod
    def _check_room_identity(row, compatibility_revision, authority):
        if (row["compatibility_revision"], row["authority"]) != (compatibility_revision, authority):
            raise Conflict("room compatibility and authority are fixed at creation")

    def _room(self, room: RoomKey):
        return self._connection.execute(f"SELECT * FROM rooms WHERE {_ROOM_WHERE}", _room_values(room)).fetchone()

    def _binding(self, binding: Binding):
        row = self._room(binding.room)
        epoch = self._connection.execute("SELECT writer_epoch FROM profile WHERE singleton=1").fetchone()[0]
        if row is None or binding.writer_epoch != epoch or row["active_binding"] != binding.connection_id:
            raise StaleBinding("writer or connection binding has changed")
        return row

    def _insert(self, table: str, values: dict, **extra):
        values = values | extra
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        return self._connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values())
        )

    def _room_dict(self, room: RoomKey) -> dict:
        return dict(zip(_ROOM_COLUMNS.split(", "), _room_values(room), strict=True))

    def _update(self, table: str, room: RoomKey, values: dict, revision: int, **key) -> None:
        assignments = ", ".join(f"{name}=?" for name in values)
        where = " AND ".join(f"{name}=?" for name in key)
        clause = f" AND {where}" if key else ""
        parameters = (*values.values(), *_room_values(room), *key.values(), revision)
        cursor = self._connection.execute(
            f"UPDATE {table} SET {assignments}, revision=revision+1 WHERE {_ROOM_WHERE}{clause} AND revision=?",
            parameters,
        )
        if cursor.rowcount != 1:
            raise RevisionConflict(f"{table} row or revision has changed")

    def _event(self, room: RoomKey, kind: str, **details) -> int:
        values = self._room_dict(room) | {"kind": kind, "evidence": b"", "wall_time": _now()} | details
        cursor = self._insert("events", values)
        return cursor.lastrowid

    def _commit(self, room: RoomKey) -> Commit:
        sequence = self._connection.execute(
            f"SELECT coalesce(max(sequence),0) FROM events WHERE {_ROOM_WHERE}",
            _room_values(room),
        ).fetchone()[0]
        return Commit(sequence, self._room(room)["receipt_boundary"])

    async def ingest(self, binding: Binding, start: int, items: tuple[NetworkReceipt, ...]) -> Commit:
        _validate(binding, Binding)
        _validate(start, int, "start")
        _validate(items, tuple[NetworkReceipt, ...])
        if len(items) > 256:
            raise InvalidRecord("receipt batch cannot exceed 256 items")
        result = await self._submit(self._ingest, (binding, start, items))
        if isinstance(result, HistoryConflict):
            raise result
        return result

    def _ingest(self, binding, start, items):
        row = self._binding(binding)
        boundary = row["receipt_boundary"]
        if start > boundary:
            raise Conflict("receipt history has a gap")
        room = binding.room
        old = self._connection.execute(
            f"SELECT * FROM receipts WHERE {_ROOM_WHERE} AND ap_index>=? AND ap_index<? ORDER BY ap_index",
            (*_room_values(room), start, min(start + len(items), boundary)),
        ).fetchall()
        if not self._matching_receipts(old, items):
            return self._block_history(room, row)
        timestamp = _now()
        for offset in range(len(old), len(items)):
            index = start + offset
            receipt = items[offset]
            self._insert(
                "receipts",
                self._room_dict(room),
                ap_index=index,
                **vars(receipt),
                received_at=timestamp,
                state=ReceiptState.RECEIVED,
                evidence=b"",
            )
            self._event(room, "receipt_received", receipt_index=index, new_state=ReceiptState.RECEIVED)
        new_boundary = max(boundary, start + len(items))
        if new_boundary != boundary:
            self._update("rooms", room, {"receipt_boundary": new_boundary}, row["revision"])
        return self._commit(room)

    @staticmethod
    def _matching_receipts(old, items):
        return all(
            tuple(row[name] for name in ("item", "location", "player", "flags"))
            == (receipt.item, receipt.location, receipt.player, receipt.flags)
            for row, receipt in zip(old, items, strict=False)
        )

    def _block_history(self, room, row):
        reason = "receipt index has different contents"
        self._update("rooms", room, {"blocked_reason": reason}, row["revision"])
        self._event(room, "history_conflict", reason=reason)
        return HistoryConflict(reason)

    async def context(self, binding: Binding, context: Context, changes: tuple[CommandChange, ...]) -> Commit:
        _validate(binding, Binding)
        _validate(context, Context)
        _validate(changes, tuple[CommandChange, ...])
        if context.binding != binding:
            raise InvalidRecord("context belongs to another binding")
        return await self._submit(self._context, (binding, context, changes))

    def _context(self, binding, context, changes):
        row = self._binding(binding)
        values = self._room_dict(binding.room) | vars(context)
        del values["binding"]
        values.update(connection_id=binding.connection_id, writer_epoch=binding.writer_epoch)
        existing = self._connection.execute(
            "SELECT * FROM contexts WHERE context_id=?", (context.context_id,)
        ).fetchone()
        if existing is not None:
            self._same(existing, values, "context")
        else:
            self._insert("contexts", values, started_at=_now())
        if row["active_context_id"] != context.context_id:
            self._end_context(row["active_context_id"])
            self._update("rooms", binding.room, {"active_context_id": context.context_id}, row["revision"])
            self._event(binding.room, "context_started", context_id=context.context_id)
        self._changes(binding.room, changes, ())
        return self._commit(binding.room)

    def _end_context(self, context_id):
        if context_id is not None:
            self._connection.execute(
                "UPDATE contexts SET ended_at=? WHERE context_id=? AND ended_at IS NULL", (_now(), context_id)
            )

    @staticmethod
    def _same(row, values, entity):
        if any(row[name] != value for name, value in values.items()):
            raise Conflict(f"{entity} identity has different contents")

    async def plan(
        self, binding: Binding, plans: tuple[CommandPlan, ...], decisions: tuple[ReceiptDecision, ...]
    ) -> Commit:
        _validate(binding, Binding)
        _validate(plans, tuple[CommandPlan, ...])
        _validate(decisions, tuple[ReceiptDecision, ...])
        for plan in plans:
            if (plan.receipt_index is None) == (plan.ammo_operation_id is None):
                raise InvalidRecord("command needs exactly one source")
        return await self._submit(self._plan, (binding, plans, decisions))

    def _plan(self, binding, plans, decisions):
        self._binding(binding)
        for plan in plans:
            self._command_plan(binding.room, plan)
        self._changes(binding.room, (), decisions)
        return self._commit(binding.room)

    def _command_plan(self, room, plan):
        values = self._room_dict(room) | vars(plan) | {"payload_hash": sha256(plan.payload).hexdigest()}
        row = self._connection.execute("SELECT * FROM commands WHERE command_id=?", (plan.command_id,)).fetchone()
        if row is not None:
            self._same(row, values, "command")
            return
        self._insert("commands", values, state=CommandState.PLANNED, result=b"")
        self._event(
            room,
            "command_planned",
            command_id=plan.command_id,
            receipt_index=plan.receipt_index,
            operation_id=plan.ammo_operation_id,
            new_state=CommandState.PLANNED,
        )

    async def prepare(self, binding: Binding, attempt: Attempt) -> Commit:
        _validate(binding, Binding)
        _validate(attempt, Attempt)
        if attempt.binding != binding or attempt.number < 1:
            raise InvalidRecord("attempt needs this binding and a positive number")
        return await self._submit(self._prepare, (binding, attempt))

    def _prepare(self, binding, attempt):
        self._binding(binding)
        command = self._command(binding.room, attempt.command_id)
        if command is None or command["context_id"] != attempt.context_id:
            raise Conflict("attempt context differs from command context")
        if attempt.number != command["attempt_count"] + 1:
            raise Conflict("attempt number must follow the stored count")
        self._insert(
            "attempts",
            self._room_dict(binding.room),
            command_id=attempt.command_id,
            attempt_no=attempt.number,
            request_id=attempt.request_id,
            connection_id=binding.connection_id,
            writer_epoch=binding.writer_epoch,
            context_id=attempt.context_id,
            envelope=attempt.envelope,
            prepared_at=_now(),
            deadline_at=attempt.deadline_at,
            acceptance=b"",
            result=b"",
        )
        self._update(
            "commands",
            binding.room,
            {"attempt_count": attempt.number},
            attempt.expected_revision,
            command_id=attempt.command_id,
        )
        self._event(
            binding.room,
            "attempt_prepared",
            command_id=attempt.command_id,
            context_id=attempt.context_id,
            attempt_no=attempt.number,
        )
        return self._commit(binding.room)

    async def apply(
        self,
        binding: Binding,
        changes: tuple[CommandChange, ...],
        decisions: tuple[ReceiptDecision, ...],
        observations: tuple[Observation, ...],
        *,
        attempts: tuple[AttemptChange, ...] = (),
    ) -> Commit:
        _validate(binding, Binding)
        _validate(changes, tuple[CommandChange, ...])
        _validate(decisions, tuple[ReceiptDecision, ...])
        _validate(observations, tuple[Observation, ...])
        _validate(attempts, tuple[AttemptChange, ...])
        return await self._submit(self._apply, (binding, changes, decisions, observations, attempts))

    def _apply(self, binding, changes, decisions, observations, attempts):
        self._binding(binding)
        self._changes(binding.room, changes, decisions)
        for attempt in attempts:
            self._attempt_change(binding.room, attempt)
        self._observations(binding.room, observations)
        return self._commit(binding.room)

    def _changes(self, room, changes, decisions):
        for change in changes:
            self._state_change(
                "commands",
                room,
                {"command_id": change.command_id},
                change,
                result=change.evidence,
                next_eligible_at=change.next_eligible_at,
            )
        for decision in decisions:
            self._state_change("receipts", room, {"ap_index": decision.ap_index}, decision, evidence=decision.evidence)

    def _state_change(self, table, room, key, change, **extra):
        clause = " AND ".join(f"{name}=?" for name in key)
        row = self._connection.execute(
            f"SELECT state FROM {table} WHERE {_ROOM_WHERE} AND {clause}",
            (*_room_values(room), *key.values()),
        ).fetchone()
        previous = row["state"] if row is not None else None
        self._update(
            table, room, {"state": change.state, "reason": change.reason, **extra}, change.expected_revision, **key
        )
        event_key = {"ap_index": "receipt_index", "command_id": "command_id", "operation_id": "operation_id"}
        references = {event_key[name]: value for name, value in key.items()}
        self._event(
            room,
            f"{table}_changed",
            previous_state=previous,
            new_state=change.state,
            reason=change.reason,
            evidence=change.evidence,
            **references,
        )

    def _attempt_change(self, room, change):
        self._update(
            "attempts",
            room,
            {"acceptance": change.acceptance, "result": change.result, "completed_at": change.completed_at},
            change.expected_revision,
            command_id=change.command_id,
            attempt_no=change.number,
        )
        self._event(
            room, "attempt_changed", command_id=change.command_id, attempt_no=change.number, evidence=change.result
        )
        if change.acceptance:
            self._event(
                room,
                "attempt_acceptance",
                command_id=change.command_id,
                attempt_no=change.number,
                evidence=change.acceptance,
            )

    async def observe(self, binding: Binding, observations: tuple[Observation, ...]) -> Commit:
        _validate(binding, Binding)
        _validate(observations, tuple[Observation, ...])
        return await self._submit(self._apply, (binding, (), (), observations, ()))

    async def observations(self, room: RoomKey, run_id: str, after_sequence: int, limit: int) -> tuple[Observation, ...]:
        _validate(room, RoomKey)
        _validate(run_id, str, "run_id")
        _validate(after_sequence, int, "after_sequence")
        _validate(limit, int, "limit")
        if not 1 <= limit <= 256:
            raise InvalidRecord("observation limit must be between 1 and 256")
        return await self._submit(self._read_observations, (room, run_id, after_sequence, limit), False)

    def _read_observations(self, room, run_id, after_sequence, limit):
        rows = self._connection.execute(
            f"SELECT * FROM events WHERE {_ROOM_WHERE} AND observer_run=? AND observer_sequence>? "
            "ORDER BY observer_sequence LIMIT ?", (*_room_values(room), run_id, after_sequence, limit),
        ).fetchall()
        return tuple(Observation(row["observer_run"], row["observer_sequence"], row["kind"],
                                 row["command_id"], row["receipt_index"], row["evidence"]) for row in rows)

    def _observations(self, room, observations):
        for observation in observations:
            values = self._room_dict(room) | {
                "observer_run": observation.run_id,
                "observer_sequence": observation.sequence,
                "kind": observation.kind,
                "command_id": observation.command_id,
                "receipt_index": observation.receipt_index,
                "evidence": observation.evidence,
            }
            existing = self._connection.execute(
                "SELECT * FROM events WHERE observer_run=? AND observer_sequence=?",
                (observation.run_id, observation.sequence),
            ).fetchone()
            if existing is None:
                self._insert("events", values, wall_time=_now())
            else:
                self._same(existing, values, "observation")

    async def ammo(
        self,
        binding: Binding,
        operations: tuple[AmmoOperation, ...],
        changes: tuple[AmmoChange, ...],
        totals: AmmoTotals | None = None,
        commands: tuple[CommandChange, ...] = (),
    ) -> Commit:
        _validate(binding, Binding)
        _validate(operations, tuple[AmmoOperation, ...])
        _validate(changes, tuple[AmmoChange, ...])
        _validate(totals, AmmoTotals | None)
        _validate(commands, tuple[CommandChange, ...])
        return await self._submit(self._ammo, (binding, operations, changes, totals, commands))

    def _ammo(self, binding, operations, changes, totals, commands):
        self._binding(binding)
        room = binding.room
        for operation in operations:
            self._ammo_operation(room, operation)
        for change in changes:
            self._state_change(
                "ammo_operations", room, {"operation_id": change.operation_id}, change, evidence=change.evidence
            )
        if totals is not None:
            values = {
                field.name: getattr(totals, field.name) for field in fields(totals) if field.name != "expected_revision"
            }
            self._update("ammo_totals", room, values, totals.expected_revision)
            self._event(room, "ammo_totals_changed", evidence=totals.observations)
        self._changes(room, commands, ())
        return self._commit(room)

    def _ammo_operation(self, room, operation):
        values = self._room_dict(room) | vars(operation) | {"request_hash": sha256(operation.request).hexdigest()}
        row = self._connection.execute(
            "SELECT * FROM ammo_operations WHERE operation_id=?", (operation.operation_id,)
        ).fetchone()
        if row is not None:
            self._same(row, values, "ammo operation")
            return
        self._insert("ammo_operations", values, state=AmmoState.PREPARED, evidence=b"")
        self._event(room, "ammo_prepared", operation_id=operation.operation_id, new_state=AmmoState.PREPARED)

    async def checks(self, binding: Binding, changes: tuple[CheckChange, ...]) -> Commit:
        _validate(binding, Binding)
        _validate(changes, tuple[CheckChange, ...])
        return await self._submit(self._checks, (binding, changes))

    def _checks(self, binding, changes):
        self._binding(binding)
        for change in changes:
            self._check_change(binding.room, change)
        return self._commit(binding.room)

    def _check_change(self, room, change):
        values = vars(change).copy()
        del values["expected_revision"]
        if change.expected_revision is None:
            self._insert("checks_outbox", self._room_dict(room), **values)
        else:
            self._update("checks_outbox", room, values, change.expected_revision, location_id=change.location_id)
        self._event(
            room,
            "check_changed",
            location_id=change.location_id,
            new_state=change.state,
            reason=change.reason,
            evidence=change.acknowledgement,
        )

    async def room(self, room: RoomKey) -> Room | None:
        _validate(room, RoomKey)
        return await self._submit(self._read_room, (room,), False)

    def _read_room(self, room):
        row = self._room(room)
        if row is None:
            return None
        return Room(
            room,
            row["compatibility_revision"],
            Authority(row["authority"]),
            row["receipt_boundary"],
            row["active_binding"],
            row["active_context_id"],
            row["activation_event_sequence"],
            row["blocked_reason"],
            row["revision"],
        )

    def _command(self, room, command_id):
        return self._connection.execute(
            f"SELECT * FROM commands WHERE {_ROOM_WHERE} AND command_id=?",
            (*_room_values(room), command_id),
        ).fetchone()

    async def command(self, room: RoomKey, command_id: str) -> CommandRow | None:
        _validate(room, RoomKey)
        _validate(command_id, str, "command_id")
        return await self._submit(self._read_command, (room, command_id), False)

    def _read_command(self, room, command_id):
        row = self._command(room, command_id)
        if row is None:
            return None
        values = {name: row[name] for name in _COMMAND_FIELDS}
        values.update(replay=ReplayPolicy(row["replay"]), required=bool(row["required"]))
        return CommandRow(
            CommandPlan(**values),
            CommandState(row["state"]),
            row["revision"],
            row["attempt_count"],
            row["next_eligible_at"],
            row["result"],
            row["reason"],
        )

    async def items(self, room: RoomKey, after_index: int, limit: int) -> tuple[ItemRow, ...]:
        _validate(room, RoomKey)
        _validate(after_index, int, "after_index")
        _validate(limit, int, "limit")
        if after_index < -1 or not 1 <= limit <= 256:
            raise InvalidRecord("items needs after_index >= -1 and limit between 1 and 256")
        return await self._submit(self._items, (room, after_index, limit), False)

    def _items(self, room, after_index, limit):
        stored_room = self._room(room)
        if stored_room is None:
            return ()
        watermark = self._commit(room).event_sequence
        receipts = self._connection.execute(
            f"SELECT * FROM receipts WHERE {_ROOM_WHERE} AND ap_index>? ORDER BY ap_index LIMIT ?",
            (*_room_values(room), after_index, limit),
        ).fetchall()
        return tuple(self._item(room, receipt, stored_room["blocked_reason"], watermark) for receipt in receipts)

    def _item(self, room, receipt, blocked_reason, watermark):
        commands = self._connection.execute(
            f"""SELECT c.* FROM commands c WHERE {_ROOM_WHERE} AND receipt_index=? AND required=1
            AND NOT (state IN ('stale','rejected') AND EXISTS
                (SELECT 1 FROM commands replacement WHERE replacement.supersedes_id=c.command_id))
            ORDER BY ordinal, command_id""",
            (*_room_values(room), receipt["ap_index"]),
        ).fetchall()
        status, reason, actions = _item_status(receipt, commands, blocked_reason)
        return ItemRow(receipt["ap_index"], receipt["item"], receipt["player"], status, reason, actions, watermark)


def _item_status(receipt, commands, blocked_reason):
    if blocked_reason is not None:
        return ItemStatus.BLOCKED, blocked_reason, ()
    blocked = next((command for command in commands if command["state"] in ("rejected", "effect_unknown")), None)
    if blocked is not None:
        actions = ("Try again", "Mark as received") if blocked["state"] == "effect_unknown" else ()
        reason = blocked["reason"] or blocked["state"].replace("_", " ")
        if blocked["state"] == "effect_unknown":
            reason += "; trying again may duplicate the effect"
        return ItemStatus.BLOCKED, reason, actions
    if receipt["state"] == "rejected":
        return ItemStatus.BLOCKED, receipt["reason"] or "receipt rejected", ()
    return _item_progress(receipt, commands)


def _item_progress(receipt, commands):
    pending = next((command for command in commands if command["state"] != "confirmed"), None)
    if pending is not None:
        return ItemStatus.WAITING, pending["reason"] or pending["state"], ()
    if receipt["state"] == "satisfied" or commands:
        return ItemStatus.DELIVERED, receipt["reason"], ()
    if receipt["state"] in ("deferred", "planned"):
        return ItemStatus.WAITING, receipt["reason"] or "waiting for a plan", ()
    return ItemStatus.RECEIVED, receipt["reason"], ()
