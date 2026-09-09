"""Materialization publication ledger and completion evidence; no global lifecycle."""
from dataclasses import dataclass
import time
from typing import Callable, Protocol

from doom_eap.contracts.command_publication import stable_spool_id
from doom_eap.contracts.materialization import GATE_KEY_TO_MAP, MaterializationScope
from doom_eap.runtime.item_reconciliation import ReconciliationPlan, compile_reconciliation_plan
from doom_eap.runtime.materialization import MaterializationPlanError, compile_materialization_plan


@dataclass(frozen=True)
class MaterializationOutcome:
    plan: ReconciliationPlan | None = None
    error: str | None = None
    complete_transition: bool = False


@dataclass(frozen=True)
class MaterializationStatus:
    status: str
    mode: str
    block: str | None
    triggers: frozenset[str]


class MaterializationPublicationPort(Protocol):
    """Spool admission/remaining-file evidence; never verified native success."""

    def publish(self, plan: ReconciliationPlan, *, state_key, reason,
                materialization_lease, context_identity) -> tuple[bool, str | None]: ...

    def exists(self, command_id: str, state_key: str) -> bool: ...


class MaterializationCoordinator:
    def __init__(self, logger):
        self._logger = logger
        self._state = {}
        self._status = "uninitialized"
        self._mode = "none"
        self._block = None
        self._triggers = set()
        self._completion = None
        self._resync = {}
        self._noop_signature = None
        self._noop_logged_at = None

    @property
    def snapshot(self):
        return MaterializationStatus(self._status, self._mode, self._block, frozenset(self._triggers))

    def bind(self, state, resync=None):
        self._state = state
        self._resync = resync if resync is not None else {}
        self._triggers.clear()
        self._completion = None

    def reset_automatic(self):
        self._resync.clear()

    def automatic_already_applied(self, epoch, fingerprint):
        return (
            self._resync.get("runtime_epoch") == epoch
            and self._resync.get("history_fingerprint") == fingerprint
            and self._resync.get("status") in {"complete", "noop"}
        )

    def record_automatic_failure(self, reason, epoch, fingerprint):
        self._resync.update(runtime_epoch=epoch, history_fingerprint=fingerprint, status="blocked", reason=reason)

    def record_automatic_success(self, plan, reason, epoch, fingerprint, boundary):
        status = "noop" if not plan.commands else "complete"
        self._resync.update(
            runtime_epoch=epoch, history_fingerprint=fingerprint, status=status, reason=reason,
            processed_boundary=boundary, timestamp=time.time(),
        )
        return status

    def log_automatic_noop(self, reason, detail, epoch, history_fingerprint):
        signature = (reason, str(detail), epoch, history_fingerprint)
        now = time.monotonic()
        if self._noop_signature == signature and self._noop_logged_at is not None and now - self._noop_logged_at < 300.0:
            return False
        self._noop_signature, self._noop_logged_at = signature, now
        self._logger.info("RESYNC_NOOP reason=%s detail=%s epoch=%s fingerprint=%s",
                          reason, detail, epoch, history_fingerprint)
        return True

    def trigger(self, reason):
        if reason is not None:
            self._triggers.add(str(reason))

    def block(self, reason):
        self._status = "blocked"
        self._block = reason

    def admit_context(self, context, *, use_dlc_content, dlc_block):
        if context is None or context.identity == "unknown":
            self.block("runtime context is unrecognized")
            return False, self._block
        if not use_dlc_content and context.campaign != "Base":
            self._mode = "none"
            self._status = "dlc_disabled_context_ignored"
            self._block = None
            return False, None
        if use_dlc_content and context.campaign != "Base" and dlc_block:
            self.block(dlc_block)
            return False, self._block
        return True, None

    def invalidate_completed_ownership(self):
        self._state.pop("completed_key", None)
        self._state.pop("completed_persistent_key", None)

    def poll_completion(self, publisher: MaterializationPublicationPort):
        completion = self._completion
        if completion is None:
            return False
        command_ids = completion["command_ids"]
        if any(publisher.exists(command_id, completion["state_key"]) for command_id in command_ids):
            return False
        duration_ms = max(0, int((time.monotonic() - completion["started_at"]) * 1000))
        self._logger.info(
            "MATERIALIZATION_DISPATCH_COMPLETE context=%s lease=%s queued_ops=%s "
            "duration_to_last_ack_ms=%s semantic_state=command_consumed_unverified",
            completion["context"], completion["lease"], len(command_ids), duration_ms,
        )
        self._completion = None
        return True

    def reconcile(self, scope: MaterializationScope, context, ownership, transition, item_definitions,
                  replay_policies, special_mode, publisher: MaterializationPublicationPort,
                  persist: Callable[[], None]):
        state = self._state
        materialization_lease = scope.materialization_lease
        manual = scope.manual
        previous = transition[0] if transition is not None else context.campaign
        target_campaign = transition[1] if transition is not None else context.campaign
        complete_transition = False
        ownership_fingerprint = ownership.materialization_fingerprint
        materialization_key = ":".join((
            scope.dedupe_identity,
            context.identity,
            str(materialization_lease or "deferred"),
            ownership_fingerprint,
        ))
        # TAG DevInv clears physical inventory on every load. Persistent
        # ownership therefore needs one reconciliation per accepted lease.
        persistent_reconciliation_key = materialization_key
        if not manual and state.get("completed_key") == materialization_key:
            complete_transition = True
            self._triggers.clear()
            self._mode = "none"
            self._status = "completed_noop"
            return MaterializationOutcome(None, None, complete_transition)
        received = set(ownership.reconciliation_item_ids)
        active_gate_keys = [
            item_id for item_id, map_key in GATE_KEY_TO_MAP.items()
            if map_key in context.map_keys and item_id in received
        ]
        if not manual and state.get("completed_persistent_key") == persistent_reconciliation_key:
            if (
                active_gate_keys
                and materialization_lease is not None
                and state.get("completed_gate_key_lease") != materialization_lease
            ):
                gate_key_plan = compile_reconciliation_plan(
                    active_gate_keys,
                    {k: item_definitions[k] for k in active_gate_keys},
                    {k: replay_policies[k] for k in active_gate_keys},
                    stable_spool_id("context", scope.room_seed_name, scope.team, scope.slot, context.identity),
                    scope.evidence_epoch,
                    include_manual_replay=True,
                )
                publisher.publish(
                    gate_key_plan, state_key=scope.state_key,
                    reason="gate_key_epoch_rematerialization",
                    materialization_lease=materialization_lease,
                    context_identity=context.identity,
                )
                state["completed_gate_key_lease"] = materialization_lease
                state["completed_key"] = materialization_key
                persist()
                complete_transition = True
                self._triggers.clear()
                self._mode = "same_context"
                self._status = "gate_key_rematerialized"
                self._logger.info(
                    "GATE_KEY_REMATERIALIZE context=%s lease=%s keys=%s",
                    context.identity, materialization_lease, active_gate_keys,
                )
                return MaterializationOutcome(gate_key_plan, None, complete_transition)

            complete_transition = True
            self._triggers.clear()
            self._mode = "none"
            self._status = "completed_noop"
            state["completed_key"] = materialization_key
            persist()
            self._logger.info(
                "MATERIALIZATION_SKIP reason=same_context_same_ownership_reload context=%s lease=%s",
                context.identity, materialization_lease,
            )
            return MaterializationOutcome(None, None, complete_transition)
        if materialization_lease is None:
            deferred_key = f"{previous}:{target_campaign}:{context.identity}"
            self._mode = "cross_context"
            self._status = "deferred"
            self._block = "active context has no materialization lease"
            state.update(
                context_identity=context.identity,
                campaign=context.campaign,
                deferred_key=deferred_key,
                status="deferred",
            )
            persist()
            if state.get("logged_deferred_key") != deferred_key:
                state["logged_deferred_key"] = deferred_key
                persist()
                self._logger.info(
                    "CONTEXT_TRANSITION previous=%s current=%s identity=%s status=deferred reason=no_materialization_lease",
                    previous, target_campaign, context.identity,
                )
            return MaterializationOutcome(None, self._block)
        cross_context = transition is not None and previous != target_campaign
        if cross_context:
            self._triggers.add("context")
        self._mode = "cross_context" if cross_context else "same_context"
        try:
            planned = compile_materialization_plan(
                ownership, context, scope, item_definitions, replay_policies,
                special_mode,
            )
        except MaterializationPlanError as error:
            if error.blocked:
                self._status = "blocked"
                self._block = str(error)
            return MaterializationOutcome(None, str(error))
        plan = planned.reconciliation
        commands = plan.commands
        if planned.special_diagnostic is not None:
            self._logger.info(
                "SPECIAL_WEAPON_PLAN item=%s owned_count=%s resolved_stage=%s context=%s ops=%s",
                *planned.special_diagnostic,
            )
        triggers = tuple(sorted(self._triggers))
        self._logger.info(
            "MATERIALIZATION_PLAN context=%s lease=%s triggers=%s ownership_fingerprint=%s "
            "raw_ops=%s deduped_ops=%s queued_ops=%s includes_manual_replay=%s",
            context.identity,
            materialization_lease,
            ",".join(triggers),
            ownership_fingerprint,
            planned.raw_operation_count,
            len(commands),
            len(commands),
            "true",
        )
        queued, error = publisher.publish(
            plan, state_key=scope.state_key,
            reason=f"context:{context.identity}",
            materialization_lease=materialization_lease,
            context_identity=context.identity,
        )
        if not queued:
            self._status = "blocked"
            self._block = error
            return MaterializationOutcome(None, error)
        gate_key_reload = bool(
            active_gate_keys
            and state.get("completed_gate_key_lease") not in (None, materialization_lease)
        )
        state.update(
            context_identity=context.identity,
            campaign=context.campaign,
            epoch=scope.evidence_epoch,
            mode="cross_context" if cross_context else "same_context",
            pending_key=materialization_key,
            pending_plan=[command.__dict__ for command in commands],
            support_rune_jobs=planned.support_rune_jobs,
            queued_command_count=len(commands),
            completion_criterion="durable_spool_publication",
            special_stage=planned.selected_special_stage,
            status="complete",
        )
        persist()
        state["completed_key"] = materialization_key
        state["completed_persistent_key"] = persistent_reconciliation_key
        state["completed_gate_key_lease"] = materialization_lease
        state.pop("pending_key", None)
        state.pop("pending_plan", None)
        persist()
        complete_transition = True
        self._triggers.clear()
        self._completion = {
            "context": context.identity,
            "lease": materialization_lease,
            "command_ids": tuple(command.spool_id for command in commands),
            "started_at": time.monotonic(),
            "state_key": scope.state_key,
        } if commands else None
        self._status = (
            "gate_key_rematerialized"
            if gate_key_reload
            else "complete" if commands else "noop"
        )
        self._block = None
        self._logger.info("CONTEXT_MATERIALIZATION context=%s previous=%s commands=%s", context.identity, previous, len(commands))
        return MaterializationOutcome(plan, None, complete_transition)
