"""Protect durable ownership, room isolation and atomic delivery records."""

import asyncio
import sqlite3
import threading
import time
from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest

from doom_eap.runtime import ledger as storage
from doom_eap.runtime.ledger import (
    AmmoChange,
    AmmoKind,
    AmmoOperation,
    AmmoState,
    AmmoTotals,
    Attempt,
    AttemptChange,
    Authority,
    CheckChange,
    CheckState,
    CommandChange,
    CommandPlan,
    CommandState,
    Conflict,
    Context,
    HistoryConflict,
    InvalidRecord,
    ItemStatus,
    Ledger,
    LedgerClosed,
    NetworkReceipt,
    Observation,
    ReceiptDecision,
    ReceiptState,
    ReplayPolicy,
    RevisionConflict,
    RoomKey,
    SchemaError,
    StaleBinding,
    StorageError,
    WorkerFull,
    WriterBusy,
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


def test_atomic_receipt_ingest_advances_boundary_and_appends_events_together(run):
    async def scenario(ledger, path):
        binding = await bind(ledger)
        first = await ledger.ingest(binding, 0, (receipt(), receipt(101)))
        assert first.receipt_boundary == 2
        assert rows(path, "SELECT ap_index, item FROM receipts ORDER BY ap_index") == [(0, 100), (1, 101)]
        assert rows(path, "SELECT receipt_index FROM events WHERE kind='receipt_received' ORDER BY sequence") == [
            (0,),
            (1,),
        ]
        repeated = await ledger.ingest(binding, 0, (receipt(), receipt(101)))
        assert repeated == first
        suffix = await ledger.ingest(binding, 1, (receipt(101), receipt(102)))
        assert suffix.receipt_boundary == 3
        assert suffix.event_sequence == first.event_sequence + 1

    run(scenario)


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


@pytest.mark.parametrize("other", [room("other"), room(team=1), room(slot=2), room(generation_fp="other")])
def test_rooms_are_isolated(run, other):
    async def scenario(ledger, path):
        first = await bind(ledger)
        second = await bind(ledger, other)
        await ledger.ingest(first, 0, (receipt(100),))
        await ledger.ingest(second, 0, (receipt(200),))
        first_context = context(first)
        second_context = context(second)
        await ledger.context(first, first_context, ())
        await ledger.context(second, second_context, ())
        with pytest.raises(Conflict):
            await ledger.plan(second, (plan(first_context),), ())
        assert [row.item for row in await ledger.items(first.room, -1, 10)] == [100]
        assert [row.item for row in await ledger.items(second.room, -1, 10)] == [200]
        assert rows(path, "SELECT count(*) FROM commands") == [(0,)]

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
        connection.execute("PRAGMA user_version=2")
        connection.execute("INSERT INTO no_such_table VALUES(1)")

    async def scenario(ledger, path):
        binding = await bind(ledger)
        await ledger.ingest(binding, 0, (receipt(),))
        await ledger.close()
        monkeypatch.setitem(storage._MIGRATIONS, 2, broken)
        with pytest.raises(StorageError):
            await Ledger.open(path)
        assert rows(path, "PRAGMA user_version") == [(1,)]
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
        monkeypatch.setitem(storage._MIGRATIONS, 2, upgrade)
        reopened = await Ledger.open(path)
        await reopened.close()
        assert rows(path, "PRAGMA user_version") == [(2,)]
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


def test_items_view_reports_received_waiting_delivered_blocked_with_reason(run):
    async def scenario(ledger, path):
        binding = await bind(ledger)
        await ledger.ingest(binding, 0, (receipt(),) * 7)
        ctx = context(binding)
        await ledger.context(binding, ctx, ())
        commands = tuple(plan(ctx, index) for index in range(1, 6))
        optional = plan(ctx, 3, required=False, effect="toast")
        await ledger.plan(
            binding,
            (*commands, optional),
            (
                decision(1, ReceiptState.DEFERRED, reason="waiting for save"),
                decision(2, ReceiptState.PLANNED),
                decision(3, ReceiptState.PLANNED),
                decision(4, ReceiptState.PLANNED),
                decision(5, ReceiptState.PLANNED),
                decision(6, ReceiptState.SATISFIED, reason="charge credited"),
            ),
        )
        changes = (
            change(commands[0], CommandState.DEFERRED, reason="waiting for save"),
            change(commands[1], CommandState.QUEUED, reason="waiting for predecessor"),
            change(commands[2], CommandState.CONFIRMED),
            change(commands[3], CommandState.EFFECT_UNKNOWN, reason="reply lost"),
            change(commands[4], CommandState.REJECTED, reason="unsupported effect"),
            change(optional, CommandState.REJECTED, reason="toast failed"),
        )
        commit = await ledger.apply(binding, changes, (), ())
        result = await ledger.items(binding.room, -1, 10)
        assert [row.status for row in result] == [
            ItemStatus.RECEIVED,
            ItemStatus.WAITING,
            ItemStatus.WAITING,
            ItemStatus.DELIVERED,
            ItemStatus.BLOCKED,
            ItemStatus.BLOCKED,
            ItemStatus.DELIVERED,
        ]
        assert [result[index].reason for index in (1, 2, 5, 6)] == [
            "waiting for save",
            "waiting for predecessor",
            "unsupported effect",
            "charge credited",
        ]
        assert result[4].actions == ("Try again", "Mark as received")
        assert "duplicate" in result[4].reason
        assert {row.event_sequence for row in result} == {commit.event_sequence}

    run(scenario)


def test_required_effects_all_finish_and_current_work_keeps_uncertainty_visible(run):
    async def scenario(ledger, path):
        binding = await bind(ledger)
        await ledger.ingest(binding, 0, (receipt(),))
        ctx = context(binding)
        await ledger.context(binding, ctx, ())
        first, second = plan(ctx), plan(ctx, effect="second")
        await ledger.plan(binding, (first, second), (decision(0, ReceiptState.PLANNED),))
        await ledger.apply(binding, (change(first, CommandState.CONFIRMED),), (), ())
        assert (await ledger.items(binding.room, -1, 10))[0].status is ItemStatus.WAITING
        await ledger.apply(binding, (change(second, CommandState.CONFIRMED),), (), ())
        assert (await ledger.items(binding.room, -1, 10))[0].status is ItemStatus.DELIVERED
        rematerialized = plan(ctx, effect="second")
        await ledger.plan(binding, (rematerialized,), ())
        assert (await ledger.items(binding.room, -1, 10))[0].status is ItemStatus.WAITING
        await ledger.apply(binding, (change(rematerialized, CommandState.EFFECT_UNKNOWN),), (), ())
        assert (await ledger.items(binding.room, -1, 10))[0].status is ItemStatus.BLOCKED

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


def test_writer_lock_binding_fences_and_offline_reader(run):
    async def scenario(ledger, path):
        old = await bind(ledger, authority=Authority.LEDGER)
        with pytest.raises(WriterBusy):
            await Ledger.open(path)
        stored = await ledger.room(old.room)
        assert stored.activation_event_sequence is not None
        current = await bind(ledger, authority=Authority.LEDGER, expected_revision=stored.revision)
        with pytest.raises(StaleBinding):
            await ledger.ingest(old, 0, (receipt(),))
        await ledger.ingest(current, 0, (receipt(),))
        reader = await Ledger.open(path, read_only=True)
        try:
            assert len(await reader.items(old.room, -1, 10)) == 1
            with pytest.raises(StorageError, match="read only"):
                await reader.ingest(current, 1, (receipt(),))
        finally:
            await reader.close()
        await ledger.close()
        reopened = await Ledger.open(path)
        try:
            with pytest.raises(StaleBinding):
                await reopened.ingest(current, 1, (receipt(),))
        finally:
            await reopened.close()

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
        assert captured == [("wal", 2, 1, 5000, "normal", 1000)]
        execute_sql(path, "PRAGMA application_id=123")
        with pytest.raises(SchemaError):
            await Ledger.open(path)
        assert len(captured) == 1

    monkeypatch.setattr(storage, "_configure", configure)
    asyncio.run(scenario())


def test_attempt_preparation_and_correlated_evidence_are_atomic(run):
    async def scenario(ledger, path):
        binding = await bind(ledger)
        await ledger.ingest(binding, 0, (receipt(),))
        ctx = context(binding)
        await ledger.context(binding, ctx, ())
        command = plan(ctx)
        await ledger.plan(binding, (command,), ())
        await ledger.apply(binding, (change(command, CommandState.QUEUED),), (), ())
        attempt = Attempt(command.command_id, 1, uid(), binding, ctx.context_id, b"envelope", 2**62, 1)
        await ledger.prepare(binding, attempt)
        stored = await ledger.command(binding.room, command.command_id)
        assert (stored.state, stored.revision, stored.attempt_count) == (CommandState.QUEUED, 2, 1)
        with pytest.raises(Conflict):
            await ledger.prepare(binding, replace(attempt, number=2, request_id=uid(), expected_revision=2))
        completion = AttemptChange(command.command_id, 1, 0, b"accepted", b"done", time.time_ns() // 1000)
        await ledger.apply(
            binding,
            (change(command, CommandState.CONFIRMED, 2),),
            (decision(0, ReceiptState.SATISFIED),),
            (),
            attempts=(completion,),
        )
        assert rows(path, "SELECT acceptance, result, revision FROM attempts") == [(b"accepted", b"done", 1)]

    run(scenario)


def test_context_change_closes_previous_context_and_batches_changes(run):
    async def scenario(ledger, path):
        binding = await bind(ledger)
        await ledger.ingest(binding, 0, (receipt(),))
        first = context(binding)
        await ledger.context(binding, first, ())
        command = plan(first)
        await ledger.plan(binding, (command,), ())
        second = context(binding)
        with pytest.raises(RevisionConflict):
            await ledger.context(binding, second, (change(command, CommandState.STALE, 5),))
        assert rows(path, "SELECT count(*) FROM contexts") == [(1,)]
        assert rows(path, "SELECT ended_at FROM contexts") == [(None,)]
        await ledger.context(binding, second, (change(command, CommandState.STALE),))
        assert rows(path, "SELECT count(*) FROM contexts WHERE ended_at IS NOT NULL") == [(1,)]
        assert (await ledger.room(binding.room)).active_context_id == second.context_id

    run(scenario)


def operation(kind=AmmoKind.SPEND):
    identifier = uid()
    return AmmoOperation(identifier, "ammo/ops/" + identifier, b"immutable request", kind, 1, "writer", 2**62)


def test_ammo_reservations_confirmation_and_command_eligibility_are_atomic(run):
    async def scenario(ledger, path):
        binding = await bind(ledger)
        ctx = context(binding)
        await ledger.context(binding, ctx, ())
        op = operation()
        await ledger.ammo(binding, (op,), (), AmmoTotals(0, 1, 0, 0, 1, b"credit"))
        with pytest.raises(Conflict):
            await ledger.ammo(binding, (operation(),), ())
        with pytest.raises(Conflict):
            await ledger.ammo(binding, (replace(op, request=b"other"),), ())
        command = plan(ctx, None, ammo_id=op.operation_id)
        await ledger.plan(binding, (command,), ())
        confirmed = AmmoChange(op.operation_id, 0, AmmoState.CONFIRMED, b"matching reply", None)
        with pytest.raises(RevisionConflict):
            await ledger.ammo(
                binding,
                (),
                (confirmed,),
                AmmoTotals(99, 1, 1, 0, 0, b"storage"),
                (change(command, CommandState.QUEUED),),
            )
        assert rows(path, "SELECT state FROM ammo_operations") == [("prepared",)]
        await ledger.ammo(
            binding, (), (confirmed,), AmmoTotals(1, 1, 1, 0, 0, b"storage"), (change(command, CommandState.QUEUED),)
        )
        assert rows(path, "SELECT consumed, reserved FROM ammo_totals") == [(1, 0)]
        assert (await ledger.command(binding.room, command.command_id)).state is CommandState.QUEUED
        await ledger.ammo(binding, (operation(), operation(AmmoKind.DISCARD)), ())

    run(scenario)


def test_check_outbox_preserves_first_evidence_and_revisions(run):
    async def scenario(ledger, path):
        binding = await bind(ledger)
        pending = CheckChange(100, None, CheckState.PENDING, b"first evidence", 0, None, b"", None)
        await ledger.checks(binding, (pending,))
        sent = replace(pending, expected_revision=0, state=CheckState.SENT, send_attempts=1)
        await ledger.checks(binding, (sent,))
        with pytest.raises(RevisionConflict):
            await ledger.checks(binding, (sent,))
        with pytest.raises(Conflict):
            await ledger.checks(binding, (replace(sent, expected_revision=1, observation=b"changed"),))
        ack = replace(sent, expected_revision=1, state=CheckState.ACKNOWLEDGED, acknowledgement=b"server set")
        await ledger.checks(binding, (ack,))
        assert rows(path, "SELECT state, observation, acknowledgement, revision FROM checks_outbox") == [
            ("acknowledged", b"first evidence", b"server set", 2),
        ]

    run(scenario)


def test_observation_identity_is_idempotent_and_room_scoped(run):
    async def scenario(ledger, path):
        first = await bind(ledger)
        second = await bind(ledger, room("other"))
        observation = Observation(uid(), 0, "comparison", None, None, b"canonical bytes")
        commit = await ledger.observe(first, (observation,))
        assert await ledger.observe(first, (observation,)) == commit
        with pytest.raises(Conflict):
            await ledger.observe(first, (replace(observation, evidence=b"different"),))
        with pytest.raises(Conflict):
            await ledger.observe(second, (observation,))

    run(scenario)


@pytest.mark.parametrize("bad", [True, 1.0, "1", -1, 2**63])
def test_boundary_rejects_invalid_integers_before_sql(run, bad):
    async def scenario(ledger, path):
        binding = await bind(ledger)
        before = rows(path, "SELECT count(*) FROM events")
        with pytest.raises(InvalidRecord):
            await ledger.ingest(binding, 0, (replace(receipt(), flags=bad),))
        assert rows(path, "SELECT count(*) FROM events") == before

    run(scenario)


def test_boundary_checks_nested_types_payloads_enums_and_page_limits(run):
    async def scenario(ledger, path):
        binding = await bind(ledger)
        with pytest.raises(InvalidRecord):
            await ledger.ingest(binding, 0, [receipt()])
        with pytest.raises(InvalidRecord):
            await ledger.ingest(binding, 0, (receipt(),) * 257)
        with pytest.raises(InvalidRecord):
            await ledger.ingest(replace(binding, room="wrong"), 0, ())
        with pytest.raises(InvalidRecord):
            await ledger.apply(binding, (), (replace(decision(0, ReceiptState.RECEIVED), state="received"),), ())
        ctx = context(binding)
        with pytest.raises(InvalidRecord):
            await ledger.plan(binding, (replace(plan(ctx), payload=bytearray(b"mutable")),), ())
        with pytest.raises(InvalidRecord):
            await ledger.items(binding.room, -2, 10)
        with pytest.raises(InvalidRecord):
            await ledger.items(binding.room, -1, 257)
        with pytest.raises(FrozenInstanceError):
            binding.writer_epoch = 2

    run(scenario)


def test_stale_obligation_needs_a_replacement_and_unknown_effect_stays_blocked(run):
    async def scenario(ledger, path):
        binding = await bind(ledger)
        await ledger.ingest(binding, 0, (receipt(),))
        ctx = context(binding)
        await ledger.context(binding, ctx, ())
        completed, stale = plan(ctx), plan(ctx, effect="second")
        await ledger.plan(binding, (completed, stale), ())
        await ledger.apply(
            binding, (change(completed, CommandState.CONFIRMED), change(stale, CommandState.STALE)), (), ()
        )
        assert (await ledger.items(binding.room, -1, 10))[0].status is ItemStatus.WAITING
        replacement = replace(plan(ctx, effect="second"), supersedes_id=stale.command_id)
        await ledger.plan(binding, (replacement,), ())
        await ledger.apply(binding, (change(replacement, CommandState.CONFIRMED),), (), ())
        assert (await ledger.items(binding.room, -1, 10))[0].status is ItemStatus.DELIVERED
        await ledger.apply(binding, (change(stale, CommandState.EFFECT_UNKNOWN, 1),), (), ())
        assert (await ledger.items(binding.room, -1, 10))[0].status is ItemStatus.BLOCKED

    run(scenario)


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


def test_bounded_worker_does_not_block_asyncio_or_lose_cancelled_commits(run, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = storage._now

    def delayed():
        entered.set()
        assert release.wait(10)
        return original()

    async def scenario(ledger, path):
        binding = await bind(ledger)
        monkeypatch.setattr(storage, "_now", delayed)
        task = asyncio.create_task(ledger.ingest(binding, 0, (receipt(),)))
        try:
            await wait_until(entered.is_set)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            queued = [asyncio.create_task(ledger.items(binding.room, -1, 10)) for _ in range(65)]
            await asyncio.sleep(0)
            with pytest.raises(WorkerFull):
                await queued[-1]
        finally:
            release.set()
        results = await asyncio.gather(*queued[:-1])
        assert all(len(result) == 1 for result in results)
        assert (await ledger.room(binding.room)).receipt_boundary == 1
        await ledger.close()
        with pytest.raises(LedgerClosed):
            await ledger.items(binding.room, -1, 10)

    run(scenario)


async def wait_until(predicate):
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0.001)
    pytest.fail("worker did not start")


async def measure_backlog(path, count, work):
    ledger = await Ledger.create(path)
    binding = await bind(ledger)
    ticks = []
    stopped = asyncio.Event()
    observer = asyncio.create_task(heartbeat(stopped, ticks))
    started = time.perf_counter()
    try:
        for start in range(0, count, 250):
            batch = (receipt(),) * min(250, count - start)
            await ledger.ingest(binding, start, batch)
        elapsed = time.perf_counter() - started
        steps = work[path]
        await check_backlog_pages(ledger, binding.room, count)
        assert (await ledger.room(binding.room)).receipt_boundary == count
    finally:
        stopped.set()
        await observer
        await ledger.close()
    return elapsed, len(ticks), steps


async def heartbeat(stopped, ticks):
    while not stopped.is_set():
        ticks.append(time.perf_counter())
        await asyncio.sleep(0.001)


async def check_backlog_pages(ledger, key, count):
    seen = 0
    after = -1
    while page := await ledger.items(key, after, 256):
        seen += len(page)
        after = page[-1].ap_index
    assert seen == count


def test_ingesting_5000_receipts_in_batches_scales_roughly_linearly(tmp_path, capsys, monkeypatch):
    work = {}
    original = Ledger._connect

    def connect(ledger):
        connection = original(ledger)
        work[ledger._path] = 0

        def progress():
            work[ledger._path] += 100
            return 0

        connection.set_progress_handler(progress, 100)
        return connection

    async def scenario():
        small, small_ticks, small_steps = await measure_backlog(tmp_path / "2500.sqlite3", 2500, work)
        large, large_ticks, large_steps = await measure_backlog(tmp_path / "5000.sqlite3", 5000, work)
        assert large <= small * 4 + 0.5
        assert large_steps <= small_steps * 2.5
        assert min(small_ticks, large_ticks) >= 1
        assert rows(tmp_path / "5000.sqlite3", "SELECT count(*) FROM receipts") == [(5000,)]
        print(f"receipt batches: 2500={small:.3f}s; 5000={large:.3f}s; ratio={large / small:.2f}")
        print(f"event loop ticks: {small_ticks}, {large_ticks}")
        print(f"SQLite work: {small_steps}, {large_steps} steps")

    monkeypatch.setattr(Ledger, "_connect", connect)
    asyncio.run(scenario())
    with capsys.disabled():
        print(capsys.readouterr().out, end="")
