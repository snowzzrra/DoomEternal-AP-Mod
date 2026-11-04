"""Decode checkpoint plans and process-bound recovery diagnostics; no save writes."""

from dataclasses import dataclass
from enum import Enum, IntEnum

from .sentinel_diagnostics import collect


class CheckpointState(str, Enum):
    MISSING = "missing"
    CONFIRMED = "confirmed"
    INTERRUPTED = "save_interrupted"
    RECEIPT_READY = "receipt_ready"
    CONFLICT = "conflict"
    INVALID = "invalid"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNSUPPORTED = "unsupported"


class RecoveryState(str, Enum):
    NONE = "none"
    RECOVERING = "recovering"
    RESTORED = "previous_checkpoint_restored"
    RECOVERED = "interrupted_save_recovered"
    BLOCKED = "blocked"


class DiagnosticStatus(IntEnum):
    ENTERED = 1
    PENDING = 2
    SUCCEEDED = 3
    REFUSED = 4
    BLOCKED = 5


@dataclass(frozen=True)
class CheckpointPlan:
    state: CheckpointState
    resume_allowed: bool
    preservation_required: bool
    recovery: RecoveryState
    recovery_id: str | None
    checkpoint: int

    @classmethod
    def decode(cls, value):
        field_types = {"schema": int, "resume_allowed": bool, "preservation_required": bool, "checkpoint": int}
        valid_types = all(type(value.get(key)) is kind for key, kind in field_types.items())
        if (not valid_types or value["schema"] != 1 or value["checkpoint"] < 0 or "recovery_id" not in value
                or type(value["recovery_id"]) not in {str, type(None)}):
            raise ValueError("checkpoint_plan_invalid")
        plan = cls(CheckpointState(value.get("checkpoint_state")), value["resume_allowed"],
                   value["preservation_required"], RecoveryState(value.get("recovery_state")),
                   value["recovery_id"], value["checkpoint"])
        if plan.resume_allowed and (plan.state is not CheckpointState.CONFIRMED or plan.recovery is RecoveryState.RECOVERING):
            raise ValueError("checkpoint_plan_inconsistent")
        return plan

    @property
    def recoverable(self):
        return (self.state in {CheckpointState.INTERRUPTED, CheckpointState.RECEIPT_READY, CheckpointState.CONFIRMED}
                and (self.checkpoint > 0 or self.state is CheckpointState.RECEIPT_READY)
                and not self.resume_allowed)

    def projection(self):
        return {"checkpoint_state": self.state.value, "resume_allowed": self.resume_allowed,
                "preservation_required": self.preservation_required, "recovery_state": self.recovery.value,
                "recovery_id": self.recovery_id, "checkpoint": self.checkpoint}


def recovery_observation(config, session):
    records = collect(config, session).get("records", [])
    records = [record for record in records if all(record["correlation"].values())]
    if not records:
        return RecoveryState.RECOVERING, False, ""
    try:
        return decode_recovery(records[-1])
    except (KeyError, TypeError, ValueError):
        return RecoveryState.RECOVERING, False, ""


def decode_recovery(record):
    trace, campaign = record.get("b_diagnostics"), record.get("campaign")
    if not isinstance(trace, dict) or not isinstance(campaign, dict) or not isinstance(trace.get("stages"), dict):
        return RecoveryState.RECOVERING, False, ""
    stages = trace["stages"]
    recovery = stages.get("checkpoint_recovery")
    if recovery is None:
        return recovery_result(None, "", campaign)
    if not isinstance(recovery, dict):
        return RecoveryState.RECOVERING, False, ""
    status = DiagnosticStatus(recovery["status"])
    reason = recovery.get("predicate", "")
    if not isinstance(reason, str):
        return RecoveryState.RECOVERING, False, ""
    return recovery_result(status, reason, campaign)


def recovery_result(status, reason, campaign):
    if status in {DiagnosticStatus.REFUSED, DiagnosticStatus.BLOCKED}:
        return RecoveryState.BLOCKED, False, reason
    state = RecoveryState.NONE if status in {None, DiagnosticStatus.SUCCEEDED} else RecoveryState.RECOVERING
    if status is DiagnosticStatus.SUCCEEDED and reason in {RecoveryState.RESTORED.value, RecoveryState.RECOVERED.value}:
        state = RecoveryState(reason)
    verified = (campaign.get("source_verified") is True and campaign.get("parser_completed") is True
                and type(campaign.get("parser_result")) is int and campaign["parser_result"] == 0)
    return state, verified, reason
