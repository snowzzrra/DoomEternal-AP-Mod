"""Current file-spool publication adapter; native claims and execution stay native."""

import os
from pathlib import Path
import re
import time
import uuid

from doom_eap.contracts.command_publication import (
    CommandEvidence, PublicationResult,
    EXECUTION_CLASS_HEADER, PLAYER_RUNTIME, MAP_ENTITY_SAFE, TRANSIENT_EFFECT,
    VALID_EXECUTION_CLASSES, VALID_MAP_ENTITY_OPERATIONS, MAP_ENTITY_OPERATION_HEADER,
    MATERIALIZATION_LEASE_HEADER, queue_session_namespace, validate_spool_id,
    valid_materialization_epoch, TRANSIENT_SCOPE_HEADER,
)


def discard_unclaimed_command(queue_dir, coalesce_key: str) -> bool:
    """Remove only producer-owned .cmd; consumer exclusively owns .processing."""
    command = queue_dir / f"{coalesce_key}.cmd"
    try:
        command.unlink()
    except FileNotFoundError:
        return False
    return True


class CommandSpool:
    def __init__(self, queue_dir, *, arm_rpc, log_delivery, logger):
        self.queue_dir = queue_dir
        self.arm_rpc = arm_rpc
        self.log_delivery = log_delivery
        self.logger = logger

    def quarantine_bootstrap(self, action_names):
        """Archive only the historical unscoped v1 files, yielding each completed move."""
        for action_name in action_names:
            command_id = f"bootstrap-v1-{action_name}"
            for suffix in (".cmd", ".processing"):
                source = Path(self.queue_dir, f"{command_id}{suffix}")
                if not source.exists():
                    continue
                try:
                    os.replace(source, source.with_suffix(".quarantined"))
                except OSError as error:
                    self.logger.error("[Bootstrap] Could not quarantine v1 spool %s: %s", source, error)
                    continue
                yield action_name
                self.logger.warning("[Bootstrap] Quarantined v1 spool: %s", source.name)

    def active_namespace(self):
        marker = Path(self.queue_dir) / "active_session_namespace"
        try:
            value = marker.read_text(encoding="ascii").strip()
        except (FileNotFoundError, OSError, UnicodeError):
            return None
        return value if re.fullmatch(r"[0-9a-f]{16}", value) else None

    def scoped_id(self, command_id, state_key=None):
        namespace = queue_session_namespace(state_key) if state_key else None
        if namespace is None:
            namespace = self.active_namespace()
        if namespace is None:
            return command_id
        prefix = f"recv-{namespace}-"
        if command_id.startswith(prefix):
            return command_id
        return f"{prefix}{command_id}"

    def exists(self, command_id, state_key=None, room_scoped=True):
        if room_scoped:
            command_id = self.scoped_id(command_id, state_key)
        validate_spool_id(command_id)
        queued_path = os.path.join(self.queue_dir, f"{command_id}.cmd")
        processing_path = os.path.join(self.queue_dir, f"{command_id}.processing")
        return os.path.exists(queued_path) or os.path.exists(processing_path)

    def publish(
        self, cmd, coalesce_key=None, arm_rpc=True, already_queued_ok=False,
        delivery_fields=None, state_key=None, room_scoped=True,
        materialization_lease=None, execution_class=PLAYER_RUNTIME, operation=None,
        diagnostic=False, transient_scope=None,
    ):
        command_id = None
        evidence = CommandEvidence.PLANNED
        try:
            if execution_class not in VALID_EXECUTION_CLASSES:
                self.logger.error("[Queue] Refusing command with invalid execution class: %r", execution_class)
                return PublicationResult(False, evidence)
            if execution_class == MAP_ENTITY_SAFE:
                if operation not in VALID_MAP_ENTITY_OPERATIONS:
                    self.logger.error("[Queue] Refusing MAP_ENTITY_SAFE command with invalid operation: %r", operation)
                    return PublicationResult(False, evidence)
            elif execution_class == TRANSIENT_EFFECT:
                if operation is not None or not isinstance(transient_scope, str) or not transient_scope:
                    self.logger.error("[Queue] Refusing transient command without scope")
                    return PublicationResult(False, evidence)
            elif operation is not None:
                self.logger.error("[Queue] Refusing PLAYER_RUNTIME command with map operation: %r", operation)
                return PublicationResult(False, evidence)
            command_id = coalesce_key or f"{time.time_ns():020d}-{uuid.uuid4().hex}"
            if room_scoped:
                command_id = self.scoped_id(command_id, state_key)
            validate_spool_id(command_id)
            os.makedirs(self.queue_dir, exist_ok=True)
            if coalesce_key:
                if self.exists(command_id, room_scoped=room_scoped):
                    evidence = CommandEvidence.SPOOL_PRESENT
                    if delivery_fields is not None:
                        self.log_delivery(
                            "QUEUE_DUPLICATE_REJECT", command_id=command_id,
                            reason="spool_exists", **delivery_fields,
                        )
                    if already_queued_ok and arm_rpc:
                        self.arm_rpc(True)
                    # Existence was observed, not a new durable publication or execution.
                    return PublicationResult(already_queued_ok, evidence, command_id)

            temporary_path = os.path.join(
                self.queue_dir, f".{command_id}-{uuid.uuid4().hex}.tmp"
            )
            command_path = os.path.join(self.queue_dir, f"{command_id}.cmd")
            if materialization_lease is not None and not valid_materialization_epoch(materialization_lease):
                self.logger.error("[Queue] Refusing command with invalid materialization lease: %r", materialization_lease)
                return PublicationResult(False, evidence, command_id)
            payload = f"{EXECUTION_CLASS_HEADER} {execution_class}\n"
            if diagnostic:
                if cmd.strip() != "condump AP_SUPPORT_FILE.txt":
                    self.logger.error("[Support] Refusing non-diagnostic condump payload")
                    return PublicationResult(False, evidence, command_id)
                payload = "AP_DIAGNOSTIC_CONDUMP_V1 AP_SUPPORT_FILE.txt\n"
            if execution_class == MAP_ENTITY_SAFE:
                payload += f"{MAP_ENTITY_OPERATION_HEADER} {operation}\n"
            if execution_class == TRANSIENT_EFFECT:
                payload += f"{TRANSIENT_SCOPE_HEADER} {transient_scope}\n"
            if materialization_lease is not None:
                payload += f"{MATERIALIZATION_LEASE_HEADER} {materialization_lease}\n"
            payload += cmd.strip() + "\n"
            with open(temporary_path, "x", encoding="utf-8", newline="\n") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            if coalesce_key:
                try:
                    os.link(temporary_path, command_path)
                    evidence = CommandEvidence.DURABLY_PUBLISHED
                except FileExistsError:
                    evidence = CommandEvidence.SPOOL_PRESENT
                    if delivery_fields is not None:
                        self.log_delivery(
                            "QUEUE_DUPLICATE_REJECT", command_id=command_id,
                            reason="cmd_exists", **delivery_fields,
                        )
                    if already_queued_ok and arm_rpc:
                        self.arm_rpc(True)
                    return PublicationResult(already_queued_ok, evidence, command_id)
                finally:
                    try:
                        os.remove(temporary_path)
                    except FileNotFoundError:
                        pass
                processing_path = os.path.join(self.queue_dir, f"{command_id}.processing")
                if os.path.exists(processing_path):
                    evidence = CommandEvidence.CLAIMED
                    try:
                        os.remove(command_path)
                    except FileNotFoundError:
                        pass
                    if delivery_fields is not None:
                        self.log_delivery(
                            "QUEUE_DUPLICATE_REJECT", command_id=command_id,
                            reason="processing_exists", **delivery_fields,
                        )
                    if already_queued_ok and arm_rpc:
                        self.arm_rpc(True)
                    return PublicationResult(already_queued_ok, evidence, command_id)
            else:
                os.replace(temporary_path, command_path)
                evidence = CommandEvidence.DURABLY_PUBLISHED
            if arm_rpc:
                self.arm_rpc(True)
            if delivery_fields is not None:
                self.log_delivery(
                    "SPOOL_CREATE", command_id=command_id,
                    path=Path(command_path).name, **delivery_fields,
                )
            return PublicationResult(True, evidence, command_id)
        except Exception as e:
            self.logger.error(f"[Error] Failed to enqueue game command: {e}")
            return PublicationResult(False, evidence, command_id)
