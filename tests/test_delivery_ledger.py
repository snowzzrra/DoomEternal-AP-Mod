import asyncio
import sqlite3
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import pytest

from doom_eap.runtime import ledger_schema as storage
from doom_eap.runtime.ledger import (
    AmmoKind,
    AmmoOperation,
    Authority,
    CommandChange,
    CommandPlan,
    CommandState,
    Conflict,
    Context,
    EffectSource,
    HistoryConflict,
    ItemStatus,
    Ledger,
    ManualResolution,
    NetworkReceipt,
    ReceiptDecision,
    ReceiptState,
    ReplayPolicy,
    ResolutionAction,
    RevisionConflict,
    RoomKey,
    SchemaError,
    SourceKind,
    StaleBinding,
    StorageError,
)


def uid():
    return str(uuid4())


def room(seed="seed", team=0, slot=1, generation_fp="generation"):
    return RoomKey(seed, team, slot, generation_fp)


def receipt(item=100, location=-1, player=2, flags=1):
    return NetworkReceipt(item, location, player, flags)


def context(binding):
    return Context(uid(), binding, 123, "process-start", "native-instance", "base", "map", "save", uid(), 1)


def plan(ctx, index=0, *, required=True, effect="grant", ammo_id=None):
    return CommandPlan(
        uid(),
        index,
        ammo_id,
        effect,
        uid(),
        0,
        None,
        None,
        "grant",
        1,
        b"exact payload\x00",
        ctx.context_id,
        ReplayPolicy.ADDITIVE,
        required,
        2,
    )


def change(command, state, revision=0, reason=None):
    return CommandChange(command.command_id, revision, state, reason, b"evidence", None)


def decision(index, state, revision=0, reason=None):
    return ReceiptDecision(index, state, reason, b"proof", revision)


async def bind(ledger, key=None, **kwargs):
    return await ledger.bind(key or room(), uid(), compatibility_revision=1, **kwargs)


@pytest.fixture
def run(tmp_path):
    path = tmp_path / "delivery.sqlite3"

    def execute(scenario):
        async def managed():
            ledger = await Ledger.create(path)
            try:
                await scenario(ledger, path)
            finally:
                await ledger.close()

        asyncio.run(managed())

    return execute


def rows(path, sql, parameters=()):
    connection = sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        return connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()


def execute_sql(path, sql, parameters=()):
    connection = sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute(sql, parameters)
        connection.commit()
    finally:
        connection.close()


def test_same_index_with_different_contents_blocks_room(run):
    async def scenario(ledger, path):
        binding = await bind(ledger)
        await ledger.ingest(binding, 0, (receipt(),))
        with pytest.raises(HistoryConflict):
            await ledger.ingest(binding, 0, (receipt(999), receipt(101)))
        stored = await ledger.room(binding.room)
        assert stored.receipt_boundary == 1
        assert stored.blocked_reason == "receipt index has different contents"
        assert rows(path, "SELECT item FROM receipts") == [(100,)]
        assert rows(path, "SELECT kind FROM events ORDER BY sequence DESC LIMIT 1") == [("history_conflict",)]
        assert (await ledger.items(binding.room, -1, 10))[0].status is ItemStatus.BLOCKED

    run(scenario)


def test_identical_items_at_different_indexes_stay_separate(run):
    async def scenario(ledger, path):
        binding = await bind(ledger)
        await ledger.ingest(binding, 0, (receipt(),) * 3)
        page = await ledger.items(binding.room, -1, 2)
        assert [row.ap_index for row in page] == [0, 1]
        assert [row.ap_index for row in await ledger.items(binding.room, 1, 2)] == [2]
        assert rows(path, "SELECT count(*) FROM receipts") == [(3,)]

    run(scenario)


def test_failed_transaction_leaves_no_partial_rows(run):
    async def scenario(ledger, path):
        binding = await bind(ledger)
        execute_sql(
            path,
            """CREATE TRIGGER fail_second_receipt BEFORE INSERT ON receipts
            WHEN NEW.ap_index=1 BEGIN SELECT RAISE(ABORT,'disk refused write'); END""",
        )
        before = rows(path, "SELECT count(*) FROM events")
        with pytest.raises(Conflict):
            await ledger.ingest(binding, 0, (receipt(), receipt(101)))
        assert (await ledger.room(binding.room)).receipt_boundary == 0
        assert rows(path, "SELECT count(*) FROM receipts") == [(0,)]
        assert rows(path, "SELECT count(*) FROM events") == before

    run(scenario)


@pytest.mark.parametrize("contents", [None, b"broken database", b""])
def test_missing_or_corrupt_established_database_refuses_to_open(tmp_path, contents):
    path = tmp_path / "missing.sqlite3"
    if contents is not None:
        path.write_bytes(contents)
    with pytest.raises(StorageError):
        asyncio.run(Ledger.open(path))
    assert path.exists() == (contents is not None)


def test_newer_schema_is_refused(run):
    async def scenario(ledger, path):
        await ledger.close()
        execute_sql(path, "PRAGMA user_version=999")
        with pytest.raises(SchemaError, match="newer"):
            await Ledger.open(path)

    run(scenario)


def test_migration_rollback_keeps_old_version_and_backup(run, monkeypatch):
    def broken(connection):
        connection.execute("CREATE TABLE should_roll_back(value TEXT)")
        connection.execute("PRAGMA user_version=4")
        connection.execute("INSERT INTO no_such_table VALUES(1)")

    async def scenario(ledger, path):
        binding = await bind(ledger)
        await ledger.ingest(binding, 0, (receipt(),))
        await ledger.close()
        monkeypatch.setitem(storage._MIGRATIONS, 4, broken)
        with pytest.raises(StorageError):
            await Ledger.open(path)
        assert rows(path, "PRAGMA user_version") == [(3,)]
        assert rows(path, "SELECT name FROM sqlite_schema WHERE name='should_roll_back'") == []
        backups = list(path.parent.glob("*.backup"))
        assert len(backups) == 1
        assert rows(backups[0], "SELECT item FROM receipts") == [(100,)]

    run(scenario)


def test_successful_numbered_migration_and_explicit_creation(run, monkeypatch):
    def upgrade(connection):
        connection.execute("CREATE TABLE extra(value TEXT)")

    async def scenario(ledger, path):
        await ledger.close()
        with pytest.raises(StorageError):
            await Ledger.create(path)
        monkeypatch.setitem(storage._MIGRATIONS, 4, upgrade)
        reopened = await Ledger.open(path)
        await reopened.close()
        assert rows(path, "PRAGMA user_version") == [(4,)]
        assert rows(path, "SELECT name FROM sqlite_schema WHERE name='extra'") == [("extra",)]

    run(scenario)


def test_reusing_command_id_with_different_contents_fails(run):
    async def scenario(ledger, path):
        binding = await bind(ledger)
        await ledger.ingest(binding, 0, (receipt(),))
        ctx = context(binding)
        await ledger.context(binding, ctx, ())
        command = plan(ctx)
        first = await ledger.plan(binding, (command,), ())
        assert await ledger.plan(binding, (command,), ()) == first
        with pytest.raises(Conflict, match="different contents"):
            await ledger.plan(binding, (replace(command, payload=b"other"),), ())
        assert (await ledger.command(binding.room, command.command_id)).plan == command
        with pytest.raises(Conflict):
            await ledger.plan(binding, (replace(command, command_id=uid()),), ())
        assert rows(path, "SELECT count(*) FROM commands") == [(1,)]

    run(scenario)


@pytest.mark.parametrize("sql", ["UPDATE events SET evidence=X'01'", "DELETE FROM events"])
def test_events_cannot_be_updated_or_deleted(run, sql):
    async def scenario(ledger, path):
        await bind(ledger)
        with pytest.raises(sqlite3.IntegrityError, match="append only"):
            execute_sql(path, sql)
        assert rows(path, "SELECT count(*) FROM events") == [(1,)]

    run(scenario)


def test_expected_revisions_roll_back_a_whole_batch(run):
    async def scenario(ledger, path):
        binding = await bind(ledger)
        await ledger.ingest(binding, 0, (receipt(),))
        ctx = context(binding)
        await ledger.context(binding, ctx, ())
        command = plan(ctx)
        await ledger.plan(binding, (command,), ())
        before = rows(path, "SELECT count(*) FROM events")
        with pytest.raises(RevisionConflict):
            await ledger.apply(
                binding,
                (change(command, CommandState.CONFIRMED),),
                (decision(0, ReceiptState.SATISFIED, revision=5),),
                (),
            )
        assert (await ledger.command(binding.room, command.command_id)).revision == 0
        assert rows(path, "SELECT count(*) FROM events") == before
        await ledger.apply(binding, (), (decision(0, ReceiptState.SATISFIED),), ())
        with pytest.raises(RevisionConflict):
            await ledger.apply(binding, (), (decision(0, ReceiptState.RECEIVED),), ())

    run(scenario)


def test_authority_cannot_be_switched_for_an_existing_room(run):
    async def scenario(ledger, path):
        binding = await bind(ledger)
        with pytest.raises(Conflict):
            await bind(ledger, authority=Authority.LEDGER, expected_revision=0)
        assert (await ledger.room(binding.room)).authority is Authority.SHADOW

    run(scenario)


def test_open_verifies_connection_settings_and_rejects_other_database_formats(tmp_path, monkeypatch):
    original = storage._configure
    captured = []

    def configure(connection, read_only):
        original(connection, read_only)
        names = ("journal_mode", "synchronous", "foreign_keys", "busy_timeout", "locking_mode", "wal_autocheckpoint")
        captured.append(tuple(connection.execute(f"PRAGMA {name}").fetchone()[0] for name in names))

    async def scenario():
        path = tmp_path / "settings.sqlite3"
        ledger = await Ledger.create(path)
        await ledger.close()
        assert captured and all(settings == ("wal", 2, 1, 5000, "normal", 1000) for settings in captured)
        captured.clear()
        execute_sql(path, "PRAGMA application_id=123")
        with pytest.raises(SchemaError):
            await Ledger.open(path)
        assert not captured

    monkeypatch.setattr(storage, "_configure", configure)
    asyncio.run(scenario())


def operation(kind=AmmoKind.SPEND):
    identifier = uid()
    return AmmoOperation(identifier, "ammo/ops/" + identifier, b"immutable request", kind, 1, "writer", 2**62)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE receipts SET item=999, revision=revision+1",
        "UPDATE receipts SET state='nonsense', revision=revision+1",
        "UPDATE receipts SET flags=-1, revision=revision+1",
        "UPDATE receipts SET state='planned'",
        "UPDATE commands SET payload=X'00', revision=revision+1",
        "UPDATE commands SET required=2, revision=revision+1",
    ],
)
def test_sql_rejects_immutable_fields_bad_enums_counts_and_revisions(run, statement):
    async def scenario(ledger, path):
        binding = await bind(ledger)
        await ledger.ingest(binding, 0, (receipt(),))
        ctx = context(binding)
        await ledger.context(binding, ctx, ())
        await ledger.plan(binding, (plan(ctx),), ())
        with pytest.raises(sqlite3.IntegrityError):
            execute_sql(path, statement)

    run(scenario)


def test_cross_room_sources_predecessors_and_replacement_forks_are_rejected(run):
    async def scenario(ledger, path):
        first = await bind(ledger)
        second = await bind(ledger, room("other"))
        await ledger.ingest(first, 0, (receipt(),))
        ctx_a, ctx_b = context(first), context(second)
        await ledger.context(first, ctx_a, ())
        await ledger.context(second, ctx_b, ())
        original = plan(ctx_a)
        await ledger.plan(first, (original,), ())
        with pytest.raises(Conflict):
            await ledger.plan(second, (plan(ctx_b),), ())
        await ledger.ingest(second, 0, (receipt(),))
        with pytest.raises(Conflict):
            await ledger.plan(second, (replace(plan(ctx_b), predecessor_id=original.command_id),), ())
        replacement = replace(plan(ctx_a), supersedes_id=original.command_id)
        await ledger.plan(first, (replacement,), ())
        with pytest.raises(Conflict):
            await ledger.plan(first, (replace(plan(ctx_a), supersedes_id=original.command_id),), ())
        op = operation()
        await ledger.ammo(first, (op,), ())
        with pytest.raises(Conflict):
            await ledger.plan(second, (plan(ctx_b, None, ammo_id=op.operation_id),), ())

    run(scenario)


async def uncertain_command(ledger, source="receipt"):
    binding = await bind(ledger)
    ctx = context(binding)
    await ledger.context(binding, ctx, ())
    local_sources = ()
    if source == "receipt":
        await ledger.ingest(binding, 0, (receipt(),))
        command = plan(ctx)
    elif source == "ammo":
        op = operation()
        await ledger.ammo(binding, (op,), ())
        command = plan(ctx, None, ammo_id=op.operation_id)
    else:
        local = EffectSource(uid(), SourceKind.STARTING_GRANT, "grant", b"starting inventory")
        local_sources = (local,)
        command = replace(plan(ctx, None), local_source_id=local.source_id)
    await ledger.plan(binding, (command,), (), sources=local_sources)
    await ledger.apply(binding, (change(command, CommandState.EFFECT_UNKNOWN, reason="Core lost"),), (), ())
    return binding, ctx, command


async def create_v1_database(path, monkeypatch):
    with monkeypatch.context() as patches:
        patches.delitem(storage._MIGRATIONS, 2)
        patches.delitem(storage._MIGRATIONS, 3)
        ledger = await Ledger.create(path)
        try:
            binding = await bind(ledger, authority=Authority.LEDGER)
            await ledger.ingest(binding, 0, (receipt(),))
        finally:
            await ledger.close()
    ctx = context(binding)
    command = plan(ctx)
    room_values = dict(zip(("seed", "team", "slot", "generation_fp"), vars(binding.room).values(), strict=True))
    context_values = vars(ctx).copy()
    del context_values["binding"], context_values["save_binding_id"]
    command_values = vars(command).copy()
    del command_values["local_source_id"]
    fixtures = {
        "contexts": context_values | {"connection_id": binding.connection_id, "writer_epoch": binding.writer_epoch,
                                      "started_at": 1},
        "commands": command_values | {"state": "effect_unknown", "revision": 2, "attempt_count": 1,
                                      "payload_hash": sha256(command.payload).hexdigest(), "result": b"old evidence"},
        "attempts": {"command_id": command.command_id, "attempt_no": 1, "request_id": uid(),
                     "connection_id": binding.connection_id, "writer_epoch": binding.writer_epoch,
                     "context_id": ctx.context_id, "envelope": b"old envelope", "prepared_at": 1,
                     "deadline_at": 2**62, "acceptance": b"old acceptance", "result": b"", "revision": 0},
        "events": {"kind": "attempt_prepared", "command_id": command.command_id, "attempt_no": 1,
                   "receipt_index": 0, "context_id": ctx.context_id, "evidence": b"old event", "wall_time": 1},
    }
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for table, values in fixtures.items():
            values = room_values | values
            names = ",".join(values)
            placeholders = ",".join("?" for _ in values)
            connection.execute(f"INSERT INTO {table} ({names}) VALUES ({placeholders})", tuple(values.values()))
    return binding.room, command


def test_v1_migration_preserves_commands_attempts_events_and_backup(tmp_path, monkeypatch):
    def _assert_migrated_attempt_evidence(before_events, path, stored):
        assert stored.attempt.envelope == b"old envelope" and stored.attempt.reference == b""
        assert stored.envelope_fingerprint == sha256(b"old envelope").hexdigest()
        assert stored.acceptance == b"old acceptance" and stored.revision == 0
        assert rows(path, "SELECT * FROM events ORDER BY sequence") == before_events
        assert rows(path, "PRAGMA foreign_key_check") == []


    async def scenario():
        path = tmp_path / "old.sqlite3"
        key, command = await create_v1_database(path, monkeypatch)
        before_events = rows(path, "SELECT * FROM events ORDER BY sequence")
        with pytest.raises(SchemaError, match="writer"):
            await Ledger.open(path, read_only=True)
        ledger = await Ledger.open(path)
        try:
            assert (await ledger.command(key, command.command_id)).plan == command
            assert (await ledger.command(key, command.command_id)).revision == 2
            stored, = await ledger.attempts(key, command.command_id, 0, 1)
            _assert_migrated_attempt_evidence(before_events, path, stored)
            backups = list(tmp_path.glob("*.backup"))
            assert len(backups) == 1 and rows(backups[0], "PRAGMA user_version") == [(1,)]
            assert rows(backups[0], "SELECT result FROM commands") == [(b"old evidence",)]
        finally:
            await ledger.close()
        reader = await Ledger.open(path, read_only=True)
        await reader.close()

    asyncio.run(scenario())


def test_v1_migration_failure_rolls_back_rebuild_and_keeps_backup(tmp_path, monkeypatch):
    upgrade = storage._MIGRATIONS[2]

    def interrupted(connection):
        upgrade(connection)
        connection.execute("INSERT INTO missing_table VALUES(1)")

    async def scenario():
        path = tmp_path / "old.sqlite3"
        key, command = await create_v1_database(path, monkeypatch)
        before = rows(path, "SELECT * FROM commands")
        with monkeypatch.context() as patches:
            patches.setitem(storage._MIGRATIONS, 2, interrupted)
            with pytest.raises(StorageError):
                await Ledger.open(path)
        assert rows(path, "PRAGMA user_version") == [(1,)]
        assert rows(path, "SELECT * FROM commands") == before
        assert rows(path, "SELECT name FROM sqlite_schema WHERE name='effect_sources'") == []
        assert rows(path, "PRAGMA foreign_key_check") == []
        assert len(list(tmp_path.glob("*.backup"))) == 1
        reopened = await Ledger.open(path)
        assert (await reopened.command(key, command.command_id)).plan == command
        await reopened.close()

    asyncio.run(scenario())


def test_resolution_survives_writer_restart_without_a_second_replacement(run):
    async def scenario(ledger, path):
        binding, _, command = await uncertain_command(ledger)
        replacement = replace(command, command_id=uid(), realization_key=uid(), supersedes_id=command.command_id)
        resolution = ManualResolution(command.command_id, 1, ResolutionAction.TRY_AGAIN, replacement)
        await ledger.resolve(binding, resolution)
        await ledger.close()
        reopened = await Ledger.open(path)
        try:
            current = await bind(reopened, expected_revision=(await reopened.room(binding.room)).revision)
            with pytest.raises(StaleBinding):
                await reopened.resolve(binding, resolution)
            await reopened.resolve(current, resolution)
            assert (await reopened.command(binding.room, command.command_id)).replacement_id == replacement.command_id
            assert rows(path, "SELECT count(*) FROM commands") == [(2,)]
            assert rows(path, "SELECT count(*) FROM events WHERE kind='command_resolved'") == [(1,)]
        finally:
            await reopened.close()

    run(scenario)
