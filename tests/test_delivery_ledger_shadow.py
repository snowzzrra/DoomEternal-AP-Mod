"""Shadow evidence cannot authorize delivery or turn publication into confirmation."""

import asyncio
import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from doom_eap.contracts.receipt_delivery import ReceiptPlan, ReceiptPublicationScope
from doom_eap.runtime import ledger_shadow as shadow_module
from doom_eap.runtime import client_state_store as state_module
from doom_eap.runtime.check_publication import CheckPublication
from doom_eap.runtime.client_state_store import ClientStateStore
from doom_eap.runtime.command_spool import CommandSpool
from doom_eap.runtime.ledger import StorageError
from doom_eap.runtime.ledger_shadow import ShadowObserver
from doom_eap.runtime.receipt_delivery import ReceiptDelivery
from doom_eap.runtime.receipt_publication import ReceiptPublication, receipt_command_id


def context(key="room-a", seed="RoomA", boundary=0):
    return SimpleNamespace(state_key=key, team=0, slot=1, items_processed=boundary,
                           items_received=[], seed=seed)


def bind(observer, ctx):
    observer.bind(ctx, ctx.seed, "a" * 64)


def history(observer, ctx, item=7770024):
    ctx.items_received = [SimpleNamespace(item=item, location=12, player=1, flags=1)]
    observer.history(ctx, 0)


def store(path, observer):
    return ClientStateStore(path, version=2, migrate=lambda state: (state, False),
                            log_event=lambda *args, **kwargs: None, logger=logging.getLogger("shadow-test"),
                            observer=observer.commit)


def run_delivery(path, observer):
    ctx = context()
    bind(observer, ctx)
    history(observer, ctx, 7770025)
    state = {"processed_items": 0}
    document = {"version": 2, "sessions": {ctx.state_key: state}}
    persistence = store(path / "state.json", observer)
    spool = CommandSpool(path / "queue", arm_rpc=lambda *_: True, log_delivery=lambda *args, **kwargs: None,
                         logger=logging.getLogger("shadow-test"), observer=observer.capture)
    delivery = ReceiptDelivery(logging.getLogger("shadow-test"), observer=observer.capture)
    delivery.bind(state)
    scope = ReceiptPublicationScope(ctx.state_key, "map", "slot", "revision", 3, materialization_lease="1:1")
    plan = ReceiptPlan(("give health", "give armor"), "delivery")
    observer.capture("compiled_plan", state_key=ctx.state_key, commands=plan.commands,
                     item_id=7770025, item_index=0, lease="1:1")
    publication = ReceiptPublication(lambda cmd, **options: spool.publish(cmd, **options).accepted,
                                     observer=observer.capture)
    result = delivery.spool(plan, 7770025, 0, "Health", scope, publication,
                            lambda reason: persistence.commit(document, reason=reason, boundary=0))
    state["processed_items"] = 1
    persistence.commit(document, reason="session_persist", boundary=1)
    observer.capture("cursor", boundary=1)
    return result, json.loads((path / "state.json").read_text()), sorted(p.read_bytes() for p in (path / "queue").glob("*.cmd"))


@pytest.mark.parametrize("failure", ["capture", "write", "logger"])
def test_observer_failure_leaves_delivery_decisions_unchanged(tmp_path, monkeypatch, failure):
    async def exercise():
        baseline = ShadowObserver(False, tmp_path / "off.sqlite3", logging.getLogger("shadow-test"))
        expected = run_delivery(tmp_path / "off", baseline)
        observer = ShadowObserver(True, tmp_path / "on.sqlite3", logging.getLogger("shadow-test"))
        if failure == "capture":
            monkeypatch.setattr(shadow_module, "snapshot", lambda _: (_ for _ in ()).throw(InvalidEvidence()))
        elif failure == "write":
            async def broken():
                raise StorageError("unavailable")
            monkeypatch.setattr(observer, "_open", broken)
        else:
            monkeypatch.setattr(observer.logger, "info", lambda *args: (_ for _ in ()).throw(InvalidEvidence()))
        try:
            assert run_delivery(tmp_path / "on", observer) == expected
            await observer.flush()
            assert observer.dropped > 0 and observer.incomplete
        finally:
            await observer.close()
    asyncio.run(exercise())


class InvalidEvidence(ValueError):
    pass


def test_publication_is_never_confirmed_and_commits_compare_at_one_watermark(tmp_path):
    async def exercise():
        observer = ShadowObserver(True, tmp_path / "shadow.sqlite3", logging.getLogger("shadow-test"))
        try:
            assert run_delivery(tmp_path / "delivery", observer)[0][0]
            await observer.flush()
            summary = observer.last_summary
            assert summary["status"] == "incomplete"
            assert summary["legacy_safety_gaps"] == 1
            assert summary["mismatches"] == {}
            assert summary["watermark"] == summary["observed_watermark"]
            with sqlite3.connect(observer.path) as database:
                assert database.execute("SELECT count(*) FROM commands WHERE state='confirmed'").fetchone()[0] == 0
                rows = database.execute("SELECT kind, evidence FROM events WHERE observer_run=?", (observer.run,)).fetchall()
            commits = [json.loads(evidence) for kind, evidence in rows if kind == "legacy_commit"]
            assert commits[-1]["sessions"]["room-a"]["boundary"] == summary["legacy_boundary"] == 1
            assert any(kind == "comparison" for kind, _ in rows)
        finally:
            await observer.close()
    asyncio.run(exercise())


def test_successful_credit_commit_matches_receipts_and_ownership(tmp_path):
    async def exercise():
        observer = ShadowObserver(True, tmp_path / "shadow.sqlite3", logging.getLogger("shadow-test"))
        ctx = context()
        try:
            bind(observer, ctx)
            history(observer, ctx)
            ctx.items_received.append(ctx.items_received[0])
            observer.history(ctx, 1)
            observer.projection(ctx, SimpleNamespace(receipt_item_ids=(7770024, 7770024), processed_boundary=0))
            document = {"sessions": {ctx.state_key: {"processed_items": 2}}}
            store(tmp_path / "state.json", observer).commit(document, boundary=2)
            observer.capture("cursor", boundary=2)
            await observer.flush()
            assert observer.last_summary["status"] == "match"
            assert observer.last_summary["legacy_boundary"] == observer.last_summary["receipt_boundary"] == 2
            assert len(await observer.ledger.items(observer.keys[ctx.state_key], -1, 10)) == 2
        finally:
            await observer.close()
    asyncio.run(exercise())


def test_temporary_lag_is_not_divergence(tmp_path, caplog):
    async def exercise():
        observer = ShadowObserver(True, tmp_path / "shadow.sqlite3", logging.getLogger("shadow-test"))
        try:
            bind(observer, context())
            history(observer, context())
            await observer.flush()
            summaries = [json.loads(record.message.split(" ", 1)[1]) for record in caplog.records
                         if record.message.startswith("DELIVERY_LEDGER_COMPARE ")]
            assert "lagging" in {entry["status"] for entry in summaries}
            assert observer.last_summary["status"] == "match"
            assert all(entry["status"] != "divergent" for entry in summaries)
        finally:
            await observer.close()
    with caplog.at_level(logging.INFO, logger="shadow-test"):
        asyncio.run(exercise())


def test_same_watermark_payload_contradiction_is_divergent(tmp_path):
    async def exercise():
        observer = ShadowObserver(True, tmp_path / "shadow.sqlite3", logging.getLogger("shadow-test"))
        try:
            bind(observer, context())
            observer.capture("compiled_plan", commands=("give health",), item_id=1, item_index=0, state_key="room-a")
            command_id = receipt_command_id("room-a", 1, 0, 0, "give health")
            for _ in range(12):
                observer.capture("receipt_publication", command_id=command_id, payload="give armor", item_index=0, accepted=True)
            await observer.flush()
            assert observer.last_summary["status"] == "divergent"
            assert observer.last_summary["mismatches"] == {"payload": 12}
            assert len(observer.last_summary["samples"]) == 8
        finally:
            await observer.close()
    asyncio.run(exercise())


def test_late_storage_and_check_replies_keep_original_room(tmp_path):
    async def exercise():
        observer = ShadowObserver(True, tmp_path / "shadow.sqlite3", logging.getLogger("shadow-test"))
        entered, released = asyncio.Event(), asyncio.Event()
        async def send(_):
            entered.set()
            await released.wait()
        try:
            old = context()
            bind(observer, old)
            old_key = f"doom_eap:{old.state_key}:ammo_refill_consumed"
            publication = CheckPublication(send, lambda _: None, 30, observer=observer.scope(old.state_key))
            task = asyncio.create_task(publication.location(123))
            await entered.wait()
            new = context("room-b", "RoomB")
            bind(observer, new)
            observer.package(new, "SetReply", {"key": old_key, "value": 1, "password": "do-not-record"})
            released.set()
            await task
            await observer.flush()
            with sqlite3.connect(observer.path) as database:
                rows = database.execute("SELECT seed, kind, evidence FROM events WHERE kind IN ('storage_reply','check_sent')").fetchall()
            assert {row[0] for row in rows} == {"RoomA"}
            assert {row[1] for row in rows} == {"storage_reply", "check_sent"}
            assert all(b"do-not-record" not in row[2] for row in rows)
            assert observer.rooms[new.state_key]["checks"] == {}
            assert observer.rooms[old.state_key]["checks"] == {123: "sent"}
            observer.capture("invalid", state_key=old.state_key, value=object())
            assert old.state_key in observer.incomplete
            assert new.state_key not in observer.incomplete
        finally:
            await observer.close()
    asyncio.run(exercise())


def test_failed_commit_is_evidence_and_cannot_advance_legacy_boundary(tmp_path, monkeypatch):
    async def exercise():
        observer = ShadowObserver(True, tmp_path / "shadow.sqlite3", logging.getLogger("shadow-test"))
        try:
            bind(observer, context())
            history(observer, context())
            monkeypatch.setattr(state_module, "atomic_write_json", lambda *args: (_ for _ in ()).throw(OSError("refused")))
            with pytest.raises(OSError, match="refused"):
                store(tmp_path / "state.json", observer).commit({"sessions": {"room-a": {"processed_items": 1}}}, boundary=1)
            await observer.flush()
            assert observer.last_summary["legacy_boundary"] == 0
            with sqlite3.connect(observer.path) as database:
                row = database.execute("SELECT evidence FROM events WHERE kind='legacy_commit'").fetchone()
            assert json.loads(row[0])["success"] is False
        finally:
            await observer.close()
    asyncio.run(exercise())


def test_disabled_shadow_creates_nothing(tmp_path):
    observer = ShadowObserver(False, tmp_path / "disabled.sqlite3", logging.getLogger("shadow-test"))
    observer.bind(None, None, None)
    observer.capture("anything", password="never")
    asyncio.run(observer.close())
    assert list(tmp_path.iterdir()) == []


def test_restart_retains_receipts_and_reports_incomplete_crash_coverage(tmp_path):
    async def exercise():
        path = tmp_path / "shadow.sqlite3"
        first = ShadowObserver(True, path, logging.getLogger("shadow-test"))
        bind(first, context())
        history(first, context())
        await first.close()
        second = ShadowObserver(True, path, logging.getLogger("shadow-test"))
        try:
            bind(second, context())
            history(second, context())
            await second.flush()
            assert second.dropped == 0
            assert second.last_summary["status"] == "incomplete"
            assert second.last_summary["mismatches"] == {}
            assert second.last_summary["receipt_boundary"] == 1
        finally:
            await second.close()
    asyncio.run(exercise())


def test_shorter_replayed_history_does_not_truncate_or_diverge(tmp_path):
    async def exercise():
        path = tmp_path / "shadow.sqlite3"
        first = ShadowObserver(True, path, logging.getLogger("shadow-test"))
        ctx = context()
        bind(first, ctx)
        history(first, ctx)
        ctx.items_received.append(ctx.items_received[0])
        first.history(ctx, 1)
        await first.close()
        second = ShadowObserver(True, path, logging.getLogger("shadow-test"))
        try:
            bind(second, ctx)
            history(second, ctx)
            await second.flush()
            assert second.last_summary["status"] == "incomplete"
            assert second.last_summary["receipt_boundary"] == 2
            assert second.last_summary["mismatches"] == {}
        finally:
            await second.close()
    asyncio.run(exercise())


def test_actual_envelope_and_ordinal_are_compared(tmp_path):
    async def exercise():
        observer = ShadowObserver(True, tmp_path / "shadow.sqlite3", logging.getLogger("shadow-test"))
        try:
            bind(observer, context())
            observer.capture("compiled_plan", commands=("give health",), item_id=1, item_index=0, lease="1:1")
            command_id = receipt_command_id("room-a", 1, 0, 0, "give health")
            observer.capture("spool_payload", command_id=command_id,
                             payload="AP_EXECUTION_CLASS_V1 PLAYER_RUNTIME\nAP_MATERIALIZATION_LEASE_V1 2:2\ngive health\n")
            observer.capture("receipt_publication", command_id=command_id, payload="give health",
                             item_index=0, ordinal=1, scope={"materialization_lease": "2:2"}, accepted=True)
            observer.capture("spool_publication", requested_id=command_id, command_id="different-id",
                             payload="give health", accepted=True, evidence="durably_published")
            await observer.flush()
            assert observer.last_summary["status"] == "divergent"
            assert observer.last_summary["mismatches"] == {"envelope": 1, "ordinal": 1, "lease": 1, "command_id": 1}
        finally:
            await observer.close()
    asyncio.run(exercise())


def test_optional_notification_does_not_claim_or_require_effect_confirmation(tmp_path):
    async def exercise():
        observer = ShadowObserver(True, tmp_path / "shadow.sqlite3", logging.getLogger("shadow-test"))
        try:
            bind(observer, context())
            history(observer, context())
            command = "ai_ScriptCmdEnt ap_notify_item_7770024_a activate"
            observer.capture("compiled_plan", commands=(command,), item_id=7770024, item_index=0)
            command_id = receipt_command_id("room-a", 7770024, 0, 0, command)
            observer.capture("receipt_publication", command_id=command_id, payload=command, item_index=0, accepted=True)
            store(tmp_path / "state.json", observer).commit({"sessions": {"room-a": {"processed_items": 1}}}, boundary=1)
            await observer.flush()
            assert observer.last_summary["status"] == "match"
            assert observer.last_summary["legacy_safety_gaps"] == 0
        finally:
            await observer.close()
    asyncio.run(exercise())
