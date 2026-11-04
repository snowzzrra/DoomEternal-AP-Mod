"""Bounded, allow-listed support evidence from installation and public ledger reads."""

import asyncio
import itertools
import math
from pathlib import Path

from doom_eap.runtime.ledger import Ledger, LedgerError
from doom_eap.runtime.ledger_records import RoomKey


def fields(value, allowed):
    if not isinstance(value, dict):
        return {}
    return {
        key: scalar(value[key])
        for key in allowed
        if key in value and type(value[key]) in {str, int, float, bool, type(None)}
    }


def scalar(value):
    if isinstance(value, str):
        return value[:512]
    return None if isinstance(value, float) and not math.isfinite(value) else value


def installation_evidence(summary):
    result = fields(
        summary,
        (
            "status",
            "schema",
            "transaction_id",
            "revision",
            "phase",
            "created_at",
            "updated_at",
            "resources_verified",
            "session_pending",
        ),
    )
    result["failure"] = fields(summary.get("failure"), ("phase", "code", "failure_domain", "message"))
    evidence = summary.get("evidence", {})
    result["evidence"] = fields(
        evidence, ("injector_pid", "injector_finished", "injector_state", "recovery_error", "cleanup_error")
    )
    outcome = evidence.get("outcome")
    if isinstance(outcome, dict):
        adapter = outcome.get("adapter", {})
        result["evidence"]["outcome"] = fields(adapter, ("state", "returncode", "message"))
        result["evidence"]["outcome"]["details"] = fields(
            adapter.get("details"),
            ("failure_step", "exception_type", "verification_error", "recovery_error", "rollback_error", "traceback"),
        )
    times = fields(
        summary.get("phase_times"),
        (
            "PREPARING",
            "PREPARED",
            "INJECTING",
            "AWAITING_CONFIRMATION",
            "CONFIRMED",
            "FINALIZING_RESOURCES",
            "VERIFYING",
            "PREPARING_SESSION",
            "COMPLETE",
            "ROLLING_BACK",
            "ROLLED_BACK",
            "RECOVERY_REQUIRED",
        ),
    )
    times = {phase: value for phase, value in times.items() if type(value) in {int, float}}
    result["phase_times"] = times
    ordered = sorted(times.items(), key=lambda pair: pair[1])
    result["timings"] = [
        {"phase": phase, "started_at": start, "elapsed_seconds": max(0, end - start)}
        for (phase, start), (_, end) in itertools.pairwise(ordered)
    ]
    return result


def checkpoint_evidence(session):
    result = fields(
        session,
        (
            "checkpoint",
            "checkpoint_state",
            "recovery_state",
            "resume_allowed",
            "resume_verified",
            "native_generation",
            "save_generation",
            "checkpoint_generation",
            "previous_checkpoint_generation",
        ),
    )
    admission = session.get("admission", {})
    if isinstance(admission, dict):
        result["admission"] = fields(
            admission, ("checkpoint", "native_generation", "save_generation", "checkpoint_generation")
        )
    return result


async def _timeline(path, room):
    ledger = await Ledger.open(path.resolve(), read_only=True)
    try:
        rows = await ledger.timeline(room, 129)
        return {"status": "available", "limit": 128, "truncated": len(rows) > 128, "events": rows[-128:]}
    finally:
        await ledger.close()


def ledger_timeline(metadata):
    if not metadata.get("room") or not metadata.get("path"):
        return {"status": "inactive"}
    try:
        path = Path(metadata["path"])
        if not path.is_file():
            return {"status": "missing"}
        return asyncio.run(_timeline(path, RoomKey(**metadata["room"])))
    except (LedgerError, OSError, TypeError, ValueError) as error:
        return {"status": "unavailable", "reason": type(error).__name__}


def collect(support):
    link = support.get("game_link", {})
    session = support.get("ap_session") or link.get("native_rpc", {}).get("session", {})
    checkpoint = checkpoint_evidence(session)
    checkpoint["native"] = native_generations(support.get("native_diagnostics", {}).get("records", ()))
    return {
        "installation_journal": installation_evidence(support.get("installation", {})),
        "checkpoint_generations": checkpoint,
    }


def native_generations(records):
    generations = []
    for record in records[-64:]:
        correlation = record.get("correlation", {})
        if correlation.get("build_matches") is not True or correlation.get("namespace_matches") is not True:
            continue
        row = fields(
            record.get("campaign"),
            ("checkpoint", "generation_before", "generation_after", "checkpoint_generation", "checkpoint_at_ms"),
        )
        if row and (not generations or row != generations[-1]):
            generations.append(row)
    return generations[-16:]


def safe_timeline(value):
    result = fields(value, ("status", "limit", "truncated", "reason"))
    events = value.get("events", ())
    result["events"] = [
        fields(row, ("sequence", "kind", "receipt_index", "command_id", "previous_state", "new_state", "wall_time"))
        for row in events[-128:]
    ]
    return result
