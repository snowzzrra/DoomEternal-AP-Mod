"""Preserve receipt spool identities, dispatch metadata and timing at the adapter."""
from doom_eap.contracts.receipt_delivery import ReceiptPublicationScope
import time
from doom_eap.contracts.command_publication import queue_session_namespace
from doom_eap.contracts.receipt_delivery import NEW_RECEIPT




def receipt_command_id(state_key, item_id, item_index, command_index, command):
    suffix = "notify" if command.startswith("ai_ScriptCmdEnt ap_notify_item_") else f"effect-{command_index:02d}"
    namespace = queue_session_namespace(state_key)
    if namespace is None:
        raise RuntimeError("cannot create receipt command without active AP identity")
    return f"recv-{namespace}-{item_index:06d}-item-{item_id}-{suffix}"


class ReceiptPublication:
    def __init__(self, send):
        self._send = send

    def publish(self, command, scope, item_id, item_index, ordinal, item_name, *, deferred=False):
        command_id = receipt_command_id(scope.state_key, item_id, item_index, ordinal, command)
        if deferred:
            fields = {"item_id": item_id, "item_name": item_name, "stage": 0,
                      "source": "deferred_receipt_notification", "intent": NEW_RECEIPT}
            options = {}
        else:
            fields = {"receipt_index": item_index, "item_id": item_id, "item_name": item_name,
                      "command_ordinal": ordinal, "packet_received_monotonic_ns": scope.packet_received_ns,
                      "packet_to_spool_ms": ((time.monotonic_ns() - scope.packet_received_ns) / 1_000_000
                                             if scope.packet_received_ns is not None else None),
                      "source": "cmd", "active_map": scope.current_map, "slot": scope.save_slot,
                      "bridge_revision": scope.bridge_revision, "protocol_version": scope.protocol_version,
                      **({"context_identity": scope.context_identity} if scope.context_identity is not None else {})}
            options = {"materialization_lease": scope.materialization_lease}
        accepted = self._send(command, coalesce_key=command_id, already_queued_ok=True,
                              state_key=scope.state_key, delivery_fields=fields, **options)
        return accepted, command_id
