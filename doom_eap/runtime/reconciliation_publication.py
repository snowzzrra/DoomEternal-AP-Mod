"""Reconciliation command publication through the existing command-spool port."""
from doom_eap.contracts.receipt_delivery import RECONCILIATION_REPAIR


class ReconciliationPublisher:
    def __init__(self, send, exists, logger):
        self._send = send
        self._exists = exists
        self._logger = logger

    def exists(self, command_id, state_key):
        return self._exists(command_id, state_key=state_key)

    def publish(self, plan, *, state_key, intent=RECONCILIATION_REPAIR, reason="manual",
                materialization_lease=None, context_identity=None):
        if intent != RECONCILIATION_REPAIR:
            return False, f"unsupported reconciliation intent: {intent!r}"
        for command in plan.commands:
            self._logger.info(
                "RESYNC_QUEUE reason=%s spool=%s item=%s stage=%s policy=%s",
                reason,
                command.spool_id,
                command.item_id,
                command.stage,
                command.policy,
            )
            if not self._send(
                command.command,
                coalesce_key=command.spool_id,
                already_queued_ok=True,
                state_key=state_key,
                delivery_fields={
                    "item_id": command.item_id,
                    "item_name": command.name,
                    "stage": command.stage,
                    "source": "reconciliation",
                    "intent": intent,
                    **({"context_identity": context_identity} if context_identity is not None else {}),
                },
                materialization_lease=materialization_lease,
            ):
                return False, f"failed to spool {command.spool_id}; rerun is safe"
        return True, None

