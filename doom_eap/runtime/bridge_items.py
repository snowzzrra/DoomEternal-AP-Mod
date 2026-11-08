from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from doom_eap.content.item_classification import (
    load_item_classification_identity,
)
from doom_eap.content.mastery_requirements import mastery_entries_for_counts
from doom_eap.contracts.challenge_registry import canonical_map_name
from doom_eap.contracts.receipt_delivery import build_materialization_epoch, stable_command_key
from doom_eap.contracts.foundation import compile_item_delivery_plan
from doom_eap.contracts.receipt_delivery import SUPPORT_RUNE_IDS
from doom_eap.contracts.receipt_delivery import (
    HISTORICAL_OWNERSHIP,
    NEW_RECEIPT,
    PRESENTATION_REPAIR,
    RECONCILIATION_REPAIR,
    ReceiptFeedbackFacts,
    ReceiptIntent,
    ReceiptPlan,
)
from doom_eap.contracts.save_observation import CheckpointRecoveryState
from doom_eap.runtime import bridge_checks, bridge_config, bridge_saves
from doom_eap.runtime.ammo import AMMO_REFILL_ITEM_ID, AmmoCommandScope, active_crucible
from doom_eap.runtime.bridge_saves import SaveObserverBaselineStore
from doom_eap.runtime.campaign import STAGES
from doom_eap.runtime.delivery import RoomDelivery
from doom_eap.runtime.inventory import (
    AP_RECEIPT_FEEDBACK,
    ClientStateLoadError,
    RuneNativeState,
    authored_effects_allowed,
    default_session_state,
    effective_ownership,
    fresh_receipt_owned_count,
    load_policy_registry,
    migrate_legacy_session_key,
    normalize_session_state,
    observe_received_items,
    processed_receipt_counts,
    progressive_receipt_stage,
    project_receipt_history,
    receipt_history_fingerprint,
    receipt_item_ids,
    validate_receipt_history_prefix,
    validate_session_receipt_prefix,
)
from doom_eap.runtime.native_transport import StaleNativeResult
from doom_eap.runtime.observers import (
    BOOTSTRAP_ACTIONS,
    BOOTSTRAP_REVISION,
    CONTEXT_BY_IDENTITY,
    BootstrapOwnership,
    classify_runtime_context,
    evaluate_dlc_availability,
    resolve_context_evidence,
)
from doom_eap.strings import text as player_text

ITEMS_FILE = bridge_config.REPO_ROOT / "data" / "items.json"


def requires_progressive_observation(request, definitions):
    definition = definitions.get(request.item_id)
    return (request.intent in {NEW_RECEIPT, RECONCILIATION_REPAIR, PRESENTATION_REPAIR}
            and isinstance(definition, dict) and definition.get("type") in {"progressive_perk", "progressive_item"}
            and isinstance(request.item_index, int) and not isinstance(request.item_index, bool))


def suppress_local_toast(request, facts):
    if request.intent != NEW_RECEIPT or isinstance(request.item_index, bool) or not isinstance(request.item_index, int):
        return False
    if request.item_index < facts.processed_boundary:
        return False
    return _local_placement_checked(facts)


def compile_receipt_plan(request: ReceiptIntent, facts: ReceiptFeedbackFacts, stage, definitions, policies, classifications) -> ReceiptPlan:
    if request.intent not in {NEW_RECEIPT, HISTORICAL_OWNERSHIP, RECONCILIATION_REPAIR, PRESENTATION_REPAIR}:
        return ReceiptPlan(None, f"unsupported item delivery intent: {request.intent!r}")
    if request.intent == HISTORICAL_OWNERSHIP:
        return ReceiptPlan((), "historical ownership observed")
    receipt = _receipt_feedback_allowed(request, facts, policies)
    classification = request.classification
    if receipt and classification is None:
        classification = classifications.get(request.item_id)
    try:
        plan = compile_item_delivery_plan(request.item_id, definitions, stage=stage, receipt=receipt,
            classification=classification, notification_slot=facts.notification_slot if receipt else None)
    except ValueError as error:
        return ReceiptPlan(None, str(error))
    commands = [command.command for command in plan.commands]
    return ReceiptPlan(tuple(commands), plan.description)


class ItemRuntime:
    def __init__(self, context):
        self.ctx = context

    async def _owner_reconcile(self, evidence, *, selected=None):
        from doom_eap.runtime.ledger import LedgerError
        from doom_eap.runtime.native_transport import TransportError
        if self.ctx.room_delivery is None:
            return None, 'Return to DOOM and unpause within 30 seconds. Inventory resync is waiting for safe gameplay.'
        try:
            return await self.ctx.room_delivery.reconcile(evidence, selected=selected)
        except (LedgerError, TransportError, OSError, TimeoutError) as error:
            bridge_config.logger.error("DELIVERY_BLOCKED error=%s", error)
            bridge_config.emit_launcher_event("item_history_status", status="blocked", message=player_text("launcher.items", "unavailable"))
            return None, player_text("launcher.items", "unavailable")

    async def _send_runtime_command(self, command, *, coalesce_key, **options):
        from doom_eap.runtime.inventory import WeaponPointsBlocked
        from doom_eap.runtime.ledger import LedgerError
        from doom_eap.runtime.native_transport import TransportError
        try:
            if command.startswith(("g_damageScaleAllToAI ", "g_damageScaleAllToSlayer ", "g_infiniteAmmo ")) and options.get("receipt_index") is None:
                from doom_eap.runtime.delivery import console_payload
                tag, payload = console_payload(command)
                link = await self._native_game_link_async()
                result = await self._native_call(link, "request", 46, tag, payload)
                return result.get("outcome") == 1
            if self.ctx.room_delivery is None:
                return False
            return await self.ctx.room_delivery.local_command(coalesce_key, command, bridge_saves.read_gameplay_save_evidence(),
                                                          lease=options.get("materialization_lease"), receipt_index=options.get("receipt_index"))
        except (LedgerError, TransportError, WeaponPointsBlocked, OSError, TimeoutError) as error:
            bridge_config.logger.error("RUNTIME_EFFECT_BLOCKED key=%s error=%s", coalesce_key, error)
            return False

    def _bind_delivery_backend(self):
        self.ctx.session_state["delivery_backend"] = "ledger"
        self.ctx.room_delivery = RoomDelivery(self.ctx, {**bridge_config.config, "client_state_file": str(bridge_config.CLIENT_STATE_FILE)},
                                          ITEM_ID_TO_COMMAND, ITEM_REPLAY_POLICIES, bridge_config.emit_launcher_event)

    def _receive_owner_storage(self, cmd, args):
        owner = getattr(self.ctx, "room_delivery", None)
        if owner is not None and owner.ammo is not None:
            owner.storage.receive(cmd, args)

    async def close_room_delivery(self):
        if self.ctx.room_delivery is not None:
            await self.ctx.room_delivery.close()

    def effective_delivery_ownership(self, receipts):
        slot_data = self.ctx._connected_slot_data
        return effective_ownership(
            receipts,
            randomize_chainsaw=bool(slot_data.get("runtime", slot_data).get("randomize_chainsaw", False)),
            randomize_dash=bool(slot_data.get("runtime", slot_data).get("randomize_dash", False)),
            checked_locations=frozenset(getattr(self.ctx, "checked_locations", ())),
            local_checked_locations=frozenset(getattr(self.ctx, "locations_checked", ())),
            server_checked_ready=getattr(self.ctx, "server_checked_locations_ready", False),
            hell_on_earth_locations=bridge_config.HELL_ON_EARTH_LOCATION_IDS,
            exultia_complete_location=(
                STAGES[slot_data.get("campaign", slot_data.get("campaign_plan", {}))["fixed_dash_completion_stage"]]["completion_id"]
                if slot_data.get("campaign", slot_data.get("campaign_plan", {})).get("fixed_dash_completion_stage") else bridge_checks.EXULTIA_COMPLETE_LOCATION
            ), slot=self.ctx.slot,
            starting_item_ids=self.ctx.receipt_session.ownership_item_ids((), ITEM_ID_TO_COMMAND),
        )

    async def queue_dev_commands(self, commands, action):
        """Send isolated dev commands without changing receipt/bootstrap state."""
        self.ctx.dev_counter += 1
        correlation = f"devtest-{self.ctx.dev_session_id}-{self.ctx.dev_counter:04d}"
        for index, command in enumerate(commands):
            if not re.fullmatch(r"ai_ScriptCmdEnt [A-Za-z0-9_]+ activate", command):
                raise ValueError("Directed tests accept only map-side entity activation")
            command_id = f"{correlation}-cmd-{index:02d}"
            if not await self._send_runtime_command(
                command,
                coalesce_key=command_id,
                state_key=self.ctx.state_key,
            ):
                return None
            bridge_config.logger.info(
                "[Test] correlation=%s action=%s map=%s command_id=%s command=%s effect=unknown",
                correlation, action, self.ctx.runtime_lifecycle.map_identity.current_map, command_id, command,
            )
        self.ctx.dev_last_action = action
        self.ctx.dev_last_correlation = correlation
        return correlation

    async def queue_dev_plan(self, plan, action):
        return await self.queue_dev_commands(
            [command.command for command in plan.commands],
            f"{action}:{plan.item_id}:{plan.family}:{plan.primitive_id}",
        )

    async def _publish_owner_ammo_refill(self):
        owner = self.ctx.room_delivery
        if owner.coordinator is None:
            return None
        try:
            link = await self._native_game_link_async()
            return await self._native_call(link, "publish_ammo_refill", owner.balance,
                                           connected=owner.can_spend and self.ctx._room_session_authoritative and owner.current())
        except (RuntimeError, OSError, ValueError):
            return None

    def _ammo_refill_balance_payload(self):
        return self.ctx.room_delivery.balance if self.ctx.room_delivery else {"available": 0, "consumed": 0, "authoritative": False}

    async def request_ammo_refill(self):
        _, error = await self._owner_reconcile(bridge_saves.read_gameplay_save_evidence())
        owner = self.ctx.room_delivery
        if error or not owner.can_spend:
            return False
        scope = AmmoCommandScope(self.ctx.state_key, self.ctx.runtime_lifecycle.map_identity.current_map, self.ctx.save_observer.selection.slot,
                                active_crucible(self.ctx._connected_slot_data["runtime"].get("special_weapon"), self.received_item_ids(processed_only=True)))
        operation = await owner.ammo.request(scope, (None, {}), sum(item.item == AMMO_REFILL_ITEM_ID for item in self.ctx.items_received[:self.items_processed])
                                             + self.ctx.receipt_session.resolved_starting_count(AMMO_REFILL_ITEM_ID))
        if operation:
            self._schedule_item_delivery("ammo")
        return operation is not None

    def _consume_ammo_refill_request_file(self):
        return self.ctx.ammo_requests.consume(bridge_checks.ammo_refill_request_files())

    def _schedule_item_delivery(self, trigger):
        """Coalesce packet wakeups into one event-loop delivery runner."""
        exit_event = getattr(self.ctx, "exit_event", None)
        if exit_event is not None and exit_event.is_set():
            return
        self.ctx._item_delivery_wakeup = True
        task = getattr(self.ctx, "_item_delivery_task", None)
        if task is None or task.done():
            self.ctx._item_delivery_task = asyncio.get_running_loop().create_task(
                self._run_scheduled_item_delivery()
            )

    async def _run_scheduled_item_delivery(self):
        try:
            while self.ctx._item_delivery_wakeup:
                self.ctx._item_delivery_wakeup = False
                await self.process_pending_item_receipts("packet")
                if self.ctx._item_delivery_waiting_for_state:
                    return
        except StaleNativeResult:
            return
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, ValueError, TypeError):
            bridge_config.logger.exception("[Tracking] ITEM_DELIVERY_RUNNER_CRASH")

    async def process_pending_item_receipts(self, trigger):
        _, error = await self._owner_reconcile(bridge_saves.read_gameplay_save_evidence())
        return error is None

    def _compatible_revision_sessions(self, candidates, sessions):
        compatible = []
        for candidate in candidates:
            session = sessions[candidate]
            history = session.get("receipt_history")
            processed = session.get("processed_items", 0)
            ok, _ = validate_receipt_history_prefix(
                self.ctx.items_received, processed, history
            )
            if ok:
                compatible.append(candidate)
        return compatible

    def migrate_revision_bound_session(self, sessions, state_key):
        """Adopt one pre-stable revision-bound session without trusting it blindly."""
        candidates = sorted(
            key for key in sessions
            if isinstance(key, str)
            and key.startswith(f"{state_key}:")
            and isinstance(sessions.get(key), dict)
        )
        if not candidates:
            return None

        selected = candidates[-1]
        if len(candidates) > 1 and self.ctx.items_received:
            compatible = self._compatible_revision_sessions(candidates, sessions)
            if len(compatible) == 1:
                selected = compatible[0]
            else:
                bridge_config.log_item_event(
                    "ITEM_STATE_INCOMPATIBLE",
                    reason="multiple_revision_sessions",
                    state_key=state_key,
                    candidates=candidates,
                    compatible=compatible,
                    boundary_before=0,
                    boundary_after=0,
                    success=False,
                    bridge_revision=bridge_config.BRIDGE_REVISION,
                )

        sessions[state_key] = sessions.pop(selected)
        bridge_config.log_item_event(
            "ITEM_STATE_MIGRATION",
            reason="bridge_revision_removed_from_session_identity",
            from_state_key=selected,
            state_key=state_key,
            boundary_before=sessions[state_key].get("processed_items", 0),
            boundary_after=sessions[state_key].get("processed_items", 0),
            session_count=1,
            success=True,
            bridge_revision=bridge_config.BRIDGE_REVISION,
        )
        return selected

    async def check_and_update_event_session(self):
        current_key = self.ctx.get_ap_state_key()
        if not current_key:
            return False
        try:
            identity = await self._query_process_identity()
        except StaleNativeResult:
            return False
        if not identity or current_key != self.ctx.get_ap_state_key():
            return False

        session_file = Path(bridge_config.INV_DUMP_DIR) / "ap_event_session.json"
        old_key = None
        if session_file.exists():
            try:
                data = json.loads(session_file.read_text(encoding="utf-8"))
                old_key = data.get("ap_state_key")
            except (OSError, RuntimeError, ValueError, TypeError):
                pass

        if old_key != current_key:
            quarantine_reason = "session_changed" if old_key else "unbound_preexisting"
            self.quarantine_unbound_physical_events(
                old_state_key=old_key,
                new_state_key=current_key,
                reason=quarantine_reason,
            )
            tmp = session_file.with_name(f".ap_event_session.{uuid.uuid4().hex}.tmp")
            tmp.write_text(
                json.dumps({"ap_state_key": current_key, "updated_at": time.time()}, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, session_file)
            bridge_config.logger.info("[Session] Persisted new physical event state key: %s (old: %s)", current_key, old_key)
        return True

    def quarantine_unbound_physical_events(
        self, old_state_key=None, new_state_key=None, reason="session_changed"
    ):
        paths = set(bridge_checks.check_event_files()) | {
            os.path.join(bridge_config.INV_DUMP_DIR, filename) for filename in bridge_checks.PUBLISHER_MAP_EVENT_FILENAMES}
        for path in sorted(paths):
            bridge_checks.quarantine_event_file(
                path,
                old_state_key=old_state_key,
                new_state_key=new_state_key,
                reason=reason,
            )
            if os.path.exists(path):
                raise RuntimeError("Unbound event could not be quarantined: " + path)

    def _unresolved_item_session(self, initialization_boundary_before):
        bridge_config.logger.warning(
            "[State] Slot identity incomplete or unsafe; refusing session state reuse."
        )
        self.ctx.state_key = ""
        self.ctx.session_state = default_session_state()
        self.ctx.receipt_session.restore_boundary(0)
        self.ctx.item_state_ready = False
        self.ctx.receipt_session.clear_packet_ranges()
        bridge_config.log_item_event(
            "ITEM_STATE_SESSION_INIT",
            state_key="unresolved",
            state_key_status="unresolved",
            reason="invalid_slot_identity",
            boundary_before=initialization_boundary_before,
            boundary_after=0,
            received_count=len(self.ctx.items_received),
            processed_count=0,
            success=False,
        )
        return

    def _restore_delivery_session(self, sessions):
        existing_session = sessions.get(self.ctx.state_key)
        if not isinstance(existing_session, dict):
            existing_session = default_session_state()
        self.ctx.session_state = normalize_session_state(existing_session)
        self._bind_delivery_backend()

    def initialize_item_state(self, slot_data):
        self.ctx.reset_room_session_authority("initialize_item_state")
        if getattr(self.ctx, "_item_state_error", None):
            self._block_item_state(self.ctx._item_state_error)
            return
        self.ctx.item_state_ready = False
        initialization_boundary_before = getattr(self, "items_processed", 0)
        previous_state_key = self.ctx.state_key
        try:
            self.ctx.client_state = bridge_config.load_client_state()
        except ClientStateLoadError as error:
            self._block_item_state(error)
            return
        self.ctx.receipt_session.begin_rebind()
        effective_seed_name = self.ctx.room_seed_name or self.ctx.seed_name
        generation = slot_data.get("native_generation_fingerprint")
        sessions = self.ctx.client_state["sessions"]
        self.ctx.state_key, migrated_from = migrate_legacy_session_key(
            sessions, seed_name=effective_seed_name, team=self.ctx.team, slot=self.ctx.slot, generation=generation,
        )
        if self.ctx.state_key is None:
            self._unresolved_item_session(initialization_boundary_before)
            return
        if previous_state_key != self.ctx.state_key:
            self.ctx.receipt_session.clear_packet_ranges()
        if self.ctx.state_key not in sessions:
            self.migrate_revision_bound_session(sessions, self.ctx.state_key)
        self._log_session_migration(migrated_from, sessions, initialization_boundary_before)
        self._restore_delivery_session(sessions)
        from doom_eap.runtime.inventory import namespace_id
        self.ctx.killsanity.bind_resolved(
            self.ctx.session_state, namespace_id(effective_seed_name, self.ctx.team, self.ctx.slot, generation),
            slot_data["killsanity"],
        )
        self.ctx.goals.bind(self.ctx.state_key, self.ctx.session_state)
        masteries = mastery_entries_for_counts(bridge_saves.WEAPON_MASTERY_ENTRIES, slot_data["runtime"]["mastery_requirement_counts"])
        self.ctx.save_checks.bind({entry["signal"]["unlockable"]: entry for entry in masteries})
        self.ctx.publisher_dispatch.bind(self.ctx.session_state.setdefault("publisher_acknowledgements", {}))
        self.ctx.save_observer.bind_baselines(SaveObserverBaselineStore(self.ctx.session_state))
        self.ctx.session_state.setdefault("cultist_autosave_path", None)
        self.ctx.session_state.setdefault("save_slot_observations", {})
        self.ctx.checked_visuals.rebind()
        self.ctx.fast_travel.rebind()
        self.ctx.session_state.pop("automap_cleanup", None)
        self.ctx.session_state.pop("fast_travel_delivered", None)
        sessions[self.ctx.state_key] = self.ctx.session_state
        self.ctx.receipt_session.restore_boundary(0)
        self.ctx.session_state.pop("deathlinked", None)
        self.ctx.deathlink.bind(self.ctx.state_key, self.ctx.session_state)
        if self.ctx.death_observer.bind(namespace_id(effective_seed_name, self.ctx.team, self.ctx.slot, generation)):
            self.ctx._death_baseline_pending = True
        raw_save_observations = self.ctx.session_state.get("save_slot_observations", {})
        self.ctx.session_state["save_slot_observations"] = self.ctx.save_observer.restore_slot_observations(raw_save_observations)
        self.ctx.session_state.pop("sticky_mastery_observed", None)
        self.ctx.session_state.pop("weapon_masteries_observed", None)
        self.ctx.item_state_ready = True
        self.ctx.reconnect_resync_attempted = False
        self.ctx.bootstrap.bind(self.ctx.session_state["bootstrap"])
        try:
            bridge_config.save_client_state(
                self.ctx.client_state,
                reason="item_state_initialize",
                boundary=self.items_processed,
                boundary_before=initialization_boundary_before,
            )
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            self._block_item_state(error)
            bridge_config.log_item_event(
                "ITEM_STATE_SESSION_INIT",
                state_key=self.ctx.state_key,
                state_key_status="resolved",
                reason="state_save_failed",
                detail=str(error),
                boundary_before=initialization_boundary_before,
                boundary_after=self.items_processed,
                received_count=len(self.ctx.items_received),
                processed_count=self.items_processed,
                success=False,
            )
            return
        bridge_config.log_item_event(
            "ITEM_STATE_SESSION_INIT",
            state_key=self.ctx.state_key,
            state_key_status="resolved",
            reason="initialized",
            boundary_before=initialization_boundary_before,
            boundary_after=self.items_processed,
            received_count=len(self.ctx.items_received),
            processed_count=self.items_processed,
            success=True,
        )
        bridge_config.logger.info(
            f"[State] Loaded {self.items_processed} processed items for "
            f"{self.ctx.state_key}."
        )
        self.ctx._room_session_authoritative = True
        self.ctx.session_tasks.start(self.check_and_update_event_session)

    def native_game_link(self):
        identity = bridge_config.doom_process_identity()
        return self._native_game_link_for_identity(identity)

    def _native_game_link_for_identity(self, identity):
        from doom_eap.runtime.inventory import SentinelWeaponPoints, namespace_id
        if not identity or not identity.startswith("windows:"):
            raise RuntimeError("Sentinel requires a qualified Windows game process")
        namespace = (namespace_id(self.ctx.room_seed_name, self.ctx.team, self.ctx.slot, self.ctx._connected_slot_data["native_generation_fingerprint"])
                     if self.ctx.room_seed_name and self.ctx._connected_slot_data else "0" * 64)
        probe = Path(os.environ.get("SENTINEL_TRANSPORT_RELAY", bridge_config.DEATH_PROBE))
        key = (identity, namespace, str(probe))
        if getattr(self.ctx, "_native_link_key", None) != key:
            if getattr(self.ctx, "_native_link", None) is not None:
                self.ctx._native_link.close()
            command, environment = (), None
            if os.name != "nt":
                from doom_eap.runtime.bridge_config import runtime
                command, environment = runtime(bridge_config.config)
            self.ctx._native_link = SentinelWeaponPoints(
                probe, int(identity.split(":")[1]), namespace,
                command_prefix=command, environment=environment,
                diagnostic=lambda evidence: bridge_config.log_item_event("ITEM_NATIVE_PROBE_RESPONSE", **evidence))
            self.ctx._native_link_key = key
            self.ctx._native_link._process_identity = identity
        return self.ctx._native_link

    def _native_work_identity(self):
        return (self.ctx.state_key, self.ctx.room_seed_name, self.ctx.team, self.ctx.slot, id(self.ctx.session_state),
                self.ctx.runtime_lifecycle.capture_work(), self.ctx.receipt_session.capture(self.ctx.state_key),
                self.ctx._connected_slot_data.get("native_generation_fingerprint"), self.ctx.exit_event.is_set())

    def _blocking_native_worker(self):
        from doom_eap.runtime.native_transport import NativeWorker
        if not hasattr(self.ctx, "_native_worker"):
            self.ctx._native_worker = NativeWorker(self._native_work_identity, bridge_config.doom_process_identity)
        return self.ctx._native_worker

    async def _query_process_identity(self):
        def query():
            identity = bridge_config.doom_process_identity()
            return identity if identity and bridge_config.doom_process_identity() == identity else None

        return await self._blocking_native_worker().call(query)

    async def _native_game_link_async(self):
        identity = await self._query_process_identity()
        return await self._blocking_native_worker().call(self._native_game_link_for_identity, identity, process=identity)

    async def _native_call(self, link, method, *args, **kwargs):
        return await self._blocking_native_worker().call(
            getattr(link, method), *args, process=getattr(link, "_process_identity", None), **kwargs)

    def _block_item_state(self, error):
        self.ctx._item_state_error = str(error)
        self.ctx.item_state_ready = False
        self.ctx.item_delivery_blocked = True
        message = f'Item delivery blocked by local client state: {error!s}. Check the state file and restart the bridge.'
        self.ctx.item_delivery_blocked_info = {"reason": "client_state_error", "message": message}
        bridge_config.logger.error("[State] %s", message)
        bridge_config.emit_launcher_event("item_history_status", status="blocked", **self.ctx.item_delivery_blocked_info)
        self.ctx.reset_room_session_authority("client_state_error")

    @property
    def items_processed(self):
        return self.ctx.receipt_session.processed_boundary

    def received_rune_count(self):
        return sum(item_id in bridge_config.REVISION_ONE_RUNE_IDS for item_id in self.ctx.receipt_session.ownership_item_ids(receipt_item_ids(self.ctx.items_received), ITEM_ID_TO_COMMAND))

    def received_item_ids(self, processed_only=False):
        items = self.ctx.items_received[: self.items_processed] if processed_only else self.ctx.items_received
        return frozenset(self.ctx.receipt_session.ownership_item_ids(receipt_item_ids(items), ITEM_ID_TO_COMMAND))

    def validate_item_history_prefix(self):
        if not getattr(self.ctx, "_item_history_received", True):
            raise ValueError('waiting for the full server item history')
        compatible, detail = validate_session_receipt_prefix(
            self.ctx.session_state,
            self.ctx.items_received,
            self.items_processed,
        )
        bridge_config.log_item_event(
            "ITEM_HISTORY_COMPATIBILITY",
            status="compatible" if compatible else "incompatible",
            reason=detail,
            boundary=self.items_processed,
            received_count=len(self.ctx.items_received),
            history_fingerprint=receipt_history_fingerprint(self.ctx.items_received),
            state_key=getattr(self.ctx, "state_key", None),
            bridge_revision=bridge_config.BRIDGE_REVISION,
            mapping_revision=bridge_config.ITEM_MAPPING_REVISION,
        )
        if not compatible:
            blocked = {
                "reason": "history_incompatible",
                "boundary": self.items_processed,
                "received_count": len(self.ctx.items_received),
                "state_key": getattr(self.ctx, "state_key", None),
                "message": (player_text("activity", "history_incompatible")),
            }
            if getattr(self.ctx, "item_delivery_blocked_info", None) != blocked:
                self.ctx.item_delivery_blocked = True
                self.ctx.item_delivery_blocked_info = blocked
                bridge_config.emit_launcher_event("item_history_status", status="blocked", **blocked)
            raise ValueError(f'received-item history is incompatible: {detail!s}')
        if (getattr(self.ctx, "item_delivery_blocked_info", None) or {}).get("reason") == "history_incompatible":
            self.ctx.item_delivery_blocked = False
            self.ctx.item_delivery_blocked_info = None
            bridge_config.emit_launcher_event("item_history_status", status="ready", message=player_text("activity", "history_ready"))
        return True

    def observe_received_item_history(self):
        """Record authoritative ownership summary without delivering effects."""
        self.validate_item_history_prefix()
        observation = observe_received_items(
            self.ctx.items_received,
            self.items_processed,
            processed_receipt_counts(self.ctx.session_state),
        )
        history = copy.deepcopy(self.ctx.session_state.get("receipt_history"))
        changed = project_receipt_history(
            self.ctx.session_state, self.ctx.items_received, self.items_processed, observation,
        )
        if changed:
            committed = False
            try:
                self.ctx.persist_session_state()
                committed = True
            finally:
                if not committed:
                    if history is None:
                        self.ctx.session_state.pop("receipt_history", None)
                    else:
                        self.ctx.session_state["receipt_history"] = history
        return observation

    async def manual_reconcile_inventory(self):
        return await self._owner_reconcile(bridge_saves.read_gameplay_save_evidence())

    async def manual_reconcile_inventory_async(self, *, domain="all", item_id=None):
        from doom_eap.contracts.inventory_domain import selected_persistent_items
        return await self._owner_reconcile(bridge_saves.read_gameplay_save_evidence(), selected=selected_persistent_items(domain, item_id))

    async def observe_rune_native_state(self):
        """Read distinct Rune surfaces only from lifecycle-proven active save."""
        if not self.ctx.saves.has_authoritative_save_proof() or not self.ctx.save_observer.selection.slot:
            return None, 'authoritative active-save proof required'
        lease = getattr(self.ctx, "runtime_observation_lease", None)
        evidence_epoch = getattr(lease, "gameplay_loaded_ns", None)
        if evidence_epoch is None:
            evidence_epoch = self.ctx.save_observer.selection.native_evidence_epoch or "unknown"
        return RuneNativeState.from_game_details(
            (await self.ctx.saves.observe_active_game_details()),
            save_slot=self.ctx.save_observer.selection.slot,
            evidence_epoch=evidence_epoch,
        ), None

    async def observe_owned_rune_plan(self):
        if not self.ctx.item_state_ready:
            return None, 'item state is not ready'
        native, error = (await self.observe_rune_native_state())
        if error:
            return None, error
        try:
            from doom_eap.runtime.inventory import compile_rune_reconciliation_plan, rune_item_perk_mapping
            plan = compile_rune_reconciliation_plan(
                self.received_item_ids(), native, rune_item_perk_mapping(ITEM_ID_TO_COMMAND, SUPPORT_RUNE_IDS),
                expected_rune_item_ids=SUPPORT_RUNE_IDS,
            )
        except ValueError as error:
            return None, str(error)
        return plan, None

    async def _rune_diagnostic_state(self, native, error):
        authority = "authoritative"
        if error:
            candidate = (await self.ctx.saves.observe_active_game_details())
            if not isinstance(candidate, dict):
                return None, "observational"
            native = RuneNativeState.from_game_details(
                candidate,
                save_slot=self.ctx.save_observer.selection.slot or getattr(self.ctx.save_observer, "observation_slot", None) or "candidate",
                evidence_epoch=getattr(self.ctx.save_observer.selection, "native_evidence_epoch", None) or "unknown",
            )
            authority = "observational"
        return native, authority

    def _format_rune_diagnostic(self, native, authority, plan_error, error, owned_perks):
        slots = (*native.equipped_slots, None, None, None)
        lines = [
            f"Rune authority={authority} repair_allowed={'yes' if authority == 'authoritative' else 'no'} "
            f"reason={plan_error or error or 'active-save proof'} slot={native.save_slot} "
            f"epoch={native.evidence_epoch} map={self.ctx.runtime_lifecycle.map_identity.current_map or '-'}",
            f"AP-owned Rune perks: {owned_perks} | "
            f"available: {', '.join(sorted(native.available_perks)) or '-'} | "
            f"active: {', '.join(sorted(native.active_perks)) or '-'} | "
            f"registered: {', '.join(sorted(native.registered_runes)) or '-'}",
            self._rune_slot_text(native, slots),
        ]
        return lines

    async def observe_rune_diagnostic_lines(self):
        native, error = (await self.observe_rune_native_state())
        authority = "authoritative"
        native, authority = await self._rune_diagnostic_state(native, error)
        if native is None:
            return ["Rune diagnostic unavailable: "
                    f"authority=observational repair_allowed=no reason={error}"]
        plan, plan_error = (await self.observe_owned_rune_plan())
        owned_perks = (
            ", ".join(sorted(entry.perk for entry in plan.entries)) or "-"
            if plan is not None
            else f"unavailable ({plan_error})"
        )
        lines = self._format_rune_diagnostic(native, authority, plan_error, error, owned_perks)
        if plan_error:
            lines.append(f"Plan: blocked ({plan_error})")
        else:
            lines.append(
                f"Plan: {plan.status}; noops={len(plan.noops)} "
                f"repair_candidates={len(plan.repairs)}"
            )
        return lines

    def _bootstrap_ownership(self):
        return BootstrapOwnership(frozenset(self.received_item_ids()), self.received_rune_count() > 0)

    def bootstrap_eligible(self, action_name):
        return self.ctx.bootstrap.eligible(action_name, self._bootstrap_ownership())

    def bootstrap_ineligibility_reason(self, action_name):
        return self.ctx.bootstrap.ineligibility_reason(action_name, self._bootstrap_ownership())

    async def onboard_bootstrap(self, trigger):
        await self.ctx.bootstrap.onboard(
            trigger, ownership=self._bootstrap_ownership(), map_identity=self.ctx.runtime_lifecycle.map_identity,
            state_key=self.ctx.state_key, item_ready=self.ctx.item_state_ready, rpc_ready=True,
            send=self._send_runtime_command,
            persist=self.ctx.persist_session_state,
        )

    def onboarding_status_lines(self):
        lines = [
            f"Bootstrap revision: {BOOTSTRAP_REVISION}",
            f"Current map: {self.ctx.runtime_lifecycle.map_identity.current_map or 'unknown'}",
        ]
        for action_name in BOOTSTRAP_ACTIONS:
            state = self.ctx.bootstrap.action_state(action_name)
            status = state.get("status", "pending")
            eligible = self.bootstrap_eligible(action_name)
            reason = "eligible" if eligible else self.bootstrap_ineligibility_reason(action_name)
            lines.append(
                f"v2 {action_name}: eligible={'yes' if eligible else 'no'}, "
                f"state={status}, trigger={state.get('trigger') or '-'}, "
                f"map={state.get('last_map') or '-'}, reason={reason}"
            )
        lines.append(f"Technical log: {bridge_config.BRIDGE_LOG_DIR}")
        return lines

    def progressive_stage(self, item_id, item_index, *, excluded_receipt_indices=None):
        """Resolve finite progressive stage from effective authoritative ownership."""
        if (
            isinstance(item_index, bool)
            or not isinstance(item_index, int)
            or item_index < 0
        ):
            return 0
        if excluded_receipt_indices is None:
            if self.ctx.items_received:
                observation = self.observe_received_item_history()
                excluded_receipt_indices = {
                    receipt.index for receipt in observation.duplicates
                }
            else:
                excluded_receipt_indices = set()
        return progressive_receipt_stage(
            receipt_item_ids(self.ctx.items_received), item_id, item_index,
            frozenset(excluded_receipt_indices), ITEM_ID_TO_COMMAND.get(item_id),
            starting_count=self.ctx.receipt_session.resolved_starting_count(item_id),
        )

    def _fresh_receipt_owned_count(
        self, item_id, item_index, boundary=None, excluded_receipt_indices=None
    ):
        """Count only current authoritative receipts eligible for receipt feedback."""
        boundary = (
            getattr(self, "items_processed", 0)
            if boundary is None
            else boundary
        )
        if excluded_receipt_indices is None:
            excluded_receipt_indices = set()
        return fresh_receipt_owned_count(
            receipt_item_ids(self.ctx.items_received), item_id, item_index, boundary,
            frozenset(excluded_receipt_indices),
        )

    def item_activation_commands(self, item_id, item_index, *, intent=HISTORICAL_OWNERSHIP,
            include_notification=None, classification=None, fresh_receipt_boundary=None,
            excluded_receipt_indices=None, suppress_local_toast=True):
        request = ReceiptIntent(item_id, item_index, intent, include_notification, classification, suppress_local_toast)
        network_item = (self.ctx.items_received[item_index] if isinstance(item_index, int) and not isinstance(item_index, bool)
                        and 0 <= item_index < len(self.ctx.items_received) else None)
        facts = ReceiptFeedbackFacts(
            self._fresh_receipt_owned_count(item_id, item_index, fresh_receipt_boundary, excluded_receipt_indices),
            self.items_processed if fresh_receipt_boundary is None else fresh_receipt_boundary,
            getattr(network_item, "location", None), getattr(self.ctx, "server_checked_locations_ready", False),
            frozenset(getattr(self.ctx, "checked_locations", ()) or ()),
            frozenset(getattr(self.ctx, "locations_info", {}) or {}), self.ctx.location_setup.received_ids & bridge_config.DOOM_LOCATION_NAMES.keys(),
        )
        stage = (self.progressive_stage(item_id, item_index, excluded_receipt_indices=excluded_receipt_indices)
                 if requires_progressive_observation(request, ITEM_ID_TO_COMMAND) else None)
        plan = compile_receipt_plan(request, facts, stage, ITEM_ID_TO_COMMAND, ITEM_REPLAY_POLICIES, ITEM_CLASSIFICATIONS)
        return (list(plan.commands) if plan.commands is not None else None), plan.description

    def delivery_notification_commands(self, item_id, item_index):
        if not bridge_config.ENABLE_ITEM_NOTIFICATIONS:
            return ()
        commands, _ = self.item_activation_commands(item_id, item_index, intent=NEW_RECEIPT, include_notification=True,
                                                     fresh_receipt_boundary=item_index)
        return tuple(command for command in commands or () if command.startswith("ai_ScriptCmdEnt ap_notify_item_"))

    def _log_session_migration(self, migrated_from, sessions, initialization_boundary_before):
        if migrated_from is not None:
            migrated_session = sessions.get(self.ctx.state_key)
            migrated_boundary = (
                migrated_session.get("processed_items", 0)
                if isinstance(migrated_session, dict)
                else 0
            )
            bridge_config.log_item_event(
                "ITEM_STATE_MIGRATION",
                from_state_key=migrated_from,
                state_key=self.ctx.state_key,
                reason="legacy_session_identity",
                boundary_before=initialization_boundary_before,
                boundary_after=migrated_boundary,
                session_count=len(sessions),
                processed_count=migrated_boundary,
                success=True,
            )
            bridge_config.logger.info(
                "[State] STATE_MIGRATED from=%s to=%s reason=legacy_session",
                migrated_from,
                self.ctx.state_key,
            )

    def _rune_slot_text(self, native, slots):
        return (
            f"Rune slots: 0={slots[0] or '-'} | 1={slots[1] or '-'} | "
            f"2={slots[2] or '-'} | "
            f"page={native.page_unlocked if native.page_unlocked is not None else 'unknown'} | "
            f"active_save={native.save_slot} | epoch={native.evidence_epoch}"
        )

    def _refresh_runtime_context(self, slot_data, evidence=None):
        """Bind exact runtime context without consulting location projections."""
        use_dlc = bool(slot_data.get("runtime", slot_data).get("use_dlc_content"))
        self.ctx.dlc_evidence = evaluate_dlc_availability(bridge_config.DOOM_BASE_DIR if use_dlc else None)
        marker = getattr(self.ctx.runtime_lifecycle.map_identity, "cached_marker", None) or {}
        evidence_map = getattr(evidence, "map_name", "")
        resolution = resolve_context_evidence(
            marker.get("runtime_map", ""), evidence_map,
            materialization_suspended=bool(marker.get("materialization_suspended")),
        )
        context = resolution.context
        source = resolution.source
        if resolution.rejection is not None:
            self._record_context_evidence_rejection(*resolution.rejection)
        if context is None:
            return CONTEXT_BY_IDENTITY.get(getattr(self.ctx.runtime_lifecycle.snapshot, "identity", "unknown"))

        transition = self.ctx.runtime_lifecycle.bind_context(context)
        if transition is not None:
            self.reset_transient_effects("context_transition")
            if self.ctx.runtime_lifecycle.record_transition(transition, context.identity):
                bridge_config.logger.info(
                    "CONTEXT_TRANSITION previous=%s current=%s identity=%s source=%s status=detected",
                    transition[0], transition[1], context.identity, source,
                )
        return context

    def _record_context_evidence_rejection(self, reason, marker_identity, evidence_identity):
        result = self.ctx.runtime_lifecycle.record_evidence_rejection(reason, marker_identity, evidence_identity)
        if result is not None:
            bridge_config.logger.warning("CONTEXT_EVIDENCE_REJECTED reason=%s marker=%s evidence=%s count=%s", *result)

    async def _active_materialization_lease(self, context=None):
        if not await self.ctx.saves.checkpoint_recovery_ready():
            return None
        marker = self.ctx.runtime_lifecycle.map_identity.cached_marker or {}
        if marker.get("native_scope") and not (await self.runtime_effects_ready()):
            return None
        marker_context = classify_runtime_context(marker.get("runtime_map", ""))
        return self.ctx.runtime_lifecycle.materialization_lease(
            getattr(marker_context, "campaign", None), getattr(context, "campaign", None),
        )

    async def authored_map_runtime_ready(self, evidence=None):
        if not await self.ctx.saves.checkpoint_recovery_ready():
            return False
        evidence = evidence or bridge_saves.read_gameplay_save_evidence()
        marker = self.ctx.runtime_lifecycle.map_identity.cached_marker or {}
        context = classify_runtime_context(marker.get("runtime_map", ""))
        lease = (await self._active_materialization_lease(context))
        socket = getattr(getattr(self.ctx, "server", None), "socket", None)
        observation = self.ctx.runtime_observation_lease
        return authored_effects_allowed(
            evidence=evidence, campaign=getattr(context, "campaign", None),
            epoch=marker.get("gameplay_epoch"), lease=lease,
            process_running=bool(lease and observation.process_probe()),
            item_ready=self.ctx.item_state_ready, room_identity=self.ctx.get_ap_state_key(),
            connected=socket is not None and not socket.closed,
            queue_authoritative=self.ctx._room_session_authoritative,
        )

    def _native_scope_identity(self, scope, link, admission):
        identity = (str(admission.get("process_created")), admission.get("instance_id"), admission.get("build_id"))
        if (scope.get("target_pid") != link.pid or scope.get("server_pid") != link.pid
                or (str(scope.get("process_created")), scope.get("instance_id"), scope.get("build_id")) != identity
                or scope.get("availability") != "enabled" or scope.get("lifecycle") != "active"
                or scope.get("context_reason") != "none"):
            return None
        return identity

    def _validated_native_scope(self, scope, link, admission):
        identity = self._native_scope_identity(scope, link, admission)
        if identity is None:
            return None
        generation = int(scope["lifecycle_generation"])
        if generation <= 0 or int(scope["context_generation"]) != generation:
            return None
        runtime_map = canonical_map_name(scope.get("current_map"))
        if len([key for key, value in bridge_checks.MATERIALIZATION_MAPS.items() if value == runtime_map]) != 1:
            return None
        return (link.namespace, admission["native_root"], link.pid, *identity, generation, runtime_map)

    async def _native_materialization_scope(self, evidence, admitted=None, link=None):
        from doom_eap.contracts.save_observation import admitted_evidence_matches
        try:
            link = link or (await self._native_game_link_async())
            admitted = admitted or (await self.ctx.saves.admitted_campaign_save())
            admission = getattr(link, "save_admission", {})
            if (not getattr(evidence, "native_safe", False)
                    or not admitted_evidence_matches(evidence, admitted, link.namespace, link.pid, admission)):
                return None
            scope = (await self._native_call(link, '_run', ["--pid", str(link.pid), "--native", "--json"]))
            return self._validated_native_scope(scope, link, admission)
        except (RuntimeError, OSError, ValueError, KeyError, TypeError):
            return None

    async def _recover_native_materialization(self, evidence):
        scope = (await self._native_materialization_scope(evidence))
        marker = self.ctx.runtime_lifecycle.map_identity.cached_marker or {}
        if scope is None:
            if marker.get("native_scope") and self.ctx.runtime_lifecycle.map_epochs.published_materialization_lease is not None:
                self.ctx.runtime_lifecycle.record_lease_publication(None, False)
            return False
        if marker.get("native_scope") == scope:
            if self.ctx.runtime_lifecycle.map_epochs.published_materialization_lease != marker["gameplay_epoch"]:
                self.ctx.checks.accept_map_identity(marker, evidence.epoch)
            return True
        # process creation and Core load generation keep the epoch stable across polls
        epoch = build_materialization_epoch(scope[-2], int(scope[3]))
        if epoch is None:
            return False
        marker = {**self.ctx.runtime_lifecycle.new_map_proposal(
            bridge_checks._materialization_map_key(scope[-1]), scope[-1], evidence.epoch, time.time_ns(), epoch),
            "native_scope": scope}
        self.ctx.saves.invalidate_active_save_proof()
        self.ctx.checks.accept_map_identity(marker, evidence.epoch)
        self.ctx.checks.snapshot_fast_travel_eligibility(marker_data=marker)
        bridge_config.logger.info("[MAP] Native Core context recovered map=%s epoch=%s", scope[-1], epoch)
        return True

    async def runtime_effects_ready(self, evidence=None):
        """Canonical admission predicate for map-scoped runtime effects."""
        evidence = evidence or bridge_saves.read_gameplay_save_evidence()
        from doom_eap.contracts.save_observation import admitted_evidence_matches
        admitted = (await self.ctx.saves.admitted_campaign_save())
        if admitted is None:
            return False
        link = (await self._native_game_link_async())
        if not admitted_evidence_matches(evidence, admitted, link.namespace, link.pid,
                                        getattr(link, "save_admission", {})):
            return False
        return await self._authored_effects_ready(marker=self.ctx.runtime_lifecycle.map_identity.cached_marker, evidence=evidence, admitted=admitted, link=link)

    def _observe_transient_runtime(self):
        evidence = bridge_saves.read_gameplay_save_evidence()
        return read_transient_runtime(
            self.ctx.base_directory, self.ctx.state_key,
            evidence is not None and evidence.state == "gameplay" and evidence.native_safe
            and not self.ctx.save_observer.readiness.frozen
            and getattr(getattr(self.ctx, "_checkpoint_recovery", None), "state", None) == CheckpointRecoveryState.READY,
        )

    def reset_transient_effects(self, reason):
        self.ctx.session_tasks.start(lambda: self.ctx.transient_effect_manager.reset(reason, self._observe_transient_runtime()))

    @property
    def context_materialization_status(self):
        return "ready" if self.ctx.room_delivery and self.ctx.room_delivery.coordinator else "waiting"

    @property
    def context_materialization_mode(self):
        return "ledger"

    @property
    def context_materialization_block(self):
        return None

    def context_support_report(self):
        marker = getattr(self.ctx.runtime_lifecycle.map_identity, "cached_marker", None) or {}
        materialization = self.ctx.session_state.get("context_materialization", {})
        return {
            "context_identity": self.ctx.runtime_lifecycle.snapshot.identity,
            "campaign": self.ctx.runtime_lifecycle.snapshot.campaign,
            "active_map_key": marker.get("map_key"),
            "active_runtime_map": marker.get("runtime_map"),
            "capabilities": sorted(self.ctx.runtime_lifecycle.snapshot.capabilities),
            "use_dlc_content": bool(getattr(self.ctx, "_connected_slot_data", {}).get("runtime", getattr(self.ctx, "_connected_slot_data", {})).get("use_dlc_content")),
            "dlc": self.ctx.dlc_evidence.report(),
            "materialization": self.context_materialization_status,
            "materialization_mode": self.context_materialization_mode,
            "materialization_block": self.context_materialization_block,
            "checkpoint_recovery": {
                "state": self.ctx._checkpoint_recovery.state.value,
                "reason": self.ctx._checkpoint_recovery.reason,
            } if hasattr(self.ctx, "_checkpoint_recovery") else None,
            "native_probe_events": {"/".join(key): {field: row[field] for field in
                ("first_seen", "last_seen", "count", "first_error") if field in row}
                for key, row in getattr(getattr(self.ctx, "_weapon_points_link", None), "_diagnostic_signatures", {}).items()},
            "special_stage": materialization.get("special_stage", 0),
        }

    async def _authored_effects_ready(self, *, marker, evidence, admitted, link):
        if marker and marker.get("native_scope"):
            return marker["native_scope"] == (await self._native_materialization_scope(evidence, admitted, link))
        context = classify_runtime_context(
            marker.get("runtime_map", "") if isinstance(marker, Mapping) else ""
        )
        if context is not None and context.campaign != "Base":
            return (await self.authored_map_runtime_ready(evidence))
        return bool(
            evidence is not None
            and evidence.state == "gameplay"
            and evidence.native_safe
            and self.ctx.saves.has_authoritative_save_proof()
        )


@dataclass(frozen=True)
class TransientRuntime:
    state_key: str
    binding: tuple[str, str] | None
    ready: bool


class TransientPublicationPort(Protocol):
    async def send(self, cvar: str, command: str, state_key: str, scope: str, *, receipt_index=None) -> bool: ...


class TransientEffectManager:
    """Owns only five named effects and their monotonic expirations."""

    def __init__(self, publication: TransientPublicationPort, process_id: int):
        self._publication = publication
        self._process_id = process_id
        self._active: dict[str, tuple[float, float, str]] = {}
        self._receipts = set()
        self._scope_generation = 0
        self._bound_baseline: tuple[str, str] | None = None
        self._reset_pending = True

    def _scope(self, runtime) -> str | None:
        state_key = runtime.state_key
        if not state_key:
            return None
        binding = runtime.binding
        if binding is None:
            return None
        if self._bound_baseline != binding:
            if self._bound_baseline is not None:
                self._active.clear()
                self._reset_pending = False
            self._bound_baseline = binding
        session_namespace = hashlib.sha256(str(state_key).encode("utf-8")).hexdigest()[:16]
        identity = "|".join(
            (
                str(state_key),
                str(session_namespace),
                str(self._process_id),
                binding[0],
                binding[1],
                str(self._scope_generation),
            )
        )
        return f"effectscope-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"

    def _commands(self, now: float) -> list[tuple[str, str]]:
        factors = {"g_damageScaleAllToAI": 1.0, "g_damageScaleAllToSlayer": 1.0}
        infinite_ammo = 0
        for _name, (expiry, factor, cvar) in self._active.items():
            if expiry > now:
                if cvar == "g_infiniteAmmo":
                    infinite_ammo = 1
                else:
                    factors[cvar] *= factor
        return [
            ("g_damageScaleAllToAI", f"g_damageScaleAllToAI {factors['g_damageScaleAllToAI']:.2f}"),
            ("g_damageScaleAllToSlayer", f"g_damageScaleAllToSlayer {factors['g_damageScaleAllToSlayer']:.2f}"),
            ("g_infiniteAmmo", f"g_infiniteAmmo {infinite_ammo}"),
        ]

    async def _emit(self, now: float, runtime, *, receipt_index=None) -> bool:
        scope = self._scope(runtime)
        if scope is None or not runtime.ready:
            return False
        for cvar, command in self._commands(now):
            if not await self._publication.send(cvar, command, runtime.state_key, scope, receipt_index=receipt_index):
                return False
        return True

    async def apply_receipt(self, item_id: int, runtime: TransientRuntime, *, receipt_index=None) -> tuple[bool, str, bool]:
        effect = TRANSIENT_EFFECTS.get(item_id)
        if effect is None:
            return False, "not a transient effect", False
        now = time.monotonic()
        await self.tick(runtime, now, emit=False)
        if not runtime.ready:
            return False, "transient baseline or safe gameplay unavailable", False
        identity = (runtime.state_key, receipt_index)
        if receipt_index is not None and identity in self._receipts:
            return True, effect["name"], False
        if receipt_index is not None:
            self._receipts.add(identity)
        current = self._active.get(effect["name"])
        expiry = max(now, current[0] if current is not None else now)
        self._active[effect["name"]] = (
            expiry + effect["duration"], effect["factor"], effect["cvar"]
        )
        self._reset_pending = True
        if not await self._emit(now, runtime, receipt_index=receipt_index):
            return False, "transient command rejected", True
        self._reset_pending = False
        return True, effect["name"], False

    async def tick(self, runtime: TransientRuntime, now: float | None = None, *, emit: bool = True) -> bool:
        now = time.monotonic() if now is None else now
        expired = [name for name, (expiry, _factor, _cvar) in self._active.items() if expiry <= now]
        for name in expired:
            self._active.pop(name, None)
        if expired:
            self._reset_pending = True
        if not emit:
            return not expired
        if expired or self._reset_pending:
            emitted = await self._emit(now, runtime)
            if emitted:
                self._reset_pending = False
            return emitted
        return True

    async def reset(self, reason: str, runtime: TransientRuntime) -> None:
        self._active.clear()
        self._scope_generation += 1
        self._reset_pending = True
        if runtime.ready:
            if await self._emit(time.monotonic(), runtime):
                self._reset_pending = False


def transient_baseline_ready(data, health) -> bool:
    if data.get("state") != "ready" or not data.get("attachment_epoch", "").isdigit():
        return False
    if data.get("pid") != health.get("pid") or health.get("state") != "ready":
        return False
    try:
        timestamp = int(data["timestamp_ms"])
        freshness = int(data["freshness_ms"])
    except (KeyError, ValueError):
        return False
    return timestamp <= int(time.time() * 1000) <= timestamp + freshness


def read_transient_runtime(base_directory, state_key, native_ready):
    base = Path(base_directory)
    data = _read_key_value_file(base / TRANSIENT_EFFECT_BASELINE_FILE)
    health = _read_key_value_file(base / "ap_rpc_health.state")
    return TransientRuntime(state_key, _baseline_binding(data, health),
                            bool(native_ready and transient_baseline_ready(data, health)))


class TransientPublication:
    def __init__(self, send):
        self._send = send

    async def send(self, cvar, command, state_key, scope, *, receipt_index=None):
        command_id = (f"temporary-{receipt_index}-{cvar}" if receipt_index is not None
                      else stable_command_key("effect", state_key, scope, cvar, command))
        return await self._send(command, coalesce_key=command_id,
                                receipt_index=receipt_index)



def _local_placement_checked(facts):
    location = facts.source_location
    if isinstance(location, bool) or not isinstance(location, int) or location <= 0 or not facts.checked_ready:
        return False
    return location in facts.checked and location in facts.local_locations and location in facts.placements


def _receipt_feedback_allowed(request, facts, policies):
    receipt = request.intent == NEW_RECEIPT and (True if request.include_notification is None else bool(request.include_notification))
    receipt = receipt and policies[request.item_id].receipt_feedback == AP_RECEIPT_FEEDBACK
    if request.suppress_local_toast and suppress_local_toast(request, facts):
        receipt = False
    if receipt and facts.notification_slot is None:
        receipt = False
    return receipt


def _read_key_value_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return {}
    result = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator:
            result[key] = value
    return result


def _baseline_binding(data, health) -> tuple[str, str] | None:
    if data.get("state") != "ready" or health.get("state") != "ready":
        return None
    pid = data.get("pid")
    epoch = data.get("attachment_epoch")
    if not pid or not epoch or pid != health.get("pid"):
        return None
    return pid, epoch


with open(ITEMS_FILE, encoding="utf-8") as f:
    # keys in json are strings, convert them to ints
    _raw_items = json.load(f)
    ITEM_ID_TO_COMMAND = {int(k): v for k, v in _raw_items.items()}


ITEM_REPLAY_POLICIES_FILE = bridge_config.REPO_ROOT / "data" / "item_replay_policies.json"
ITEM_REPLAY_POLICIES = load_policy_registry(
    Path(ITEM_REPLAY_POLICIES_FILE), ITEM_ID_TO_COMMAND
)
ITEM_CLASSIFICATIONS_FILE = bridge_config.REPO_ROOT / "data" / "item_classifications.json"
_item_classification_document = json.loads(
    Path(ITEM_CLASSIFICATIONS_FILE).read_text(encoding="utf-8")
)


if (
    _item_classification_document.get("item_mapping_revision")
    != bridge_config.ITEM_MAPPING_REVISION
):
    raise RuntimeError(
        "Packaged item classification revision diverges from item mapping"
    )


ITEM_CLASSIFICATION_IDENTITY = load_item_classification_identity(
    Path(ITEM_CLASSIFICATIONS_FILE)
)
ITEM_CLASSIFICATIONS = {
    item_id: entry["classification"]
    for item_id, entry in ITEM_CLASSIFICATION_IDENTITY.items()
}


if set(ITEM_CLASSIFICATIONS) != set(ITEM_ID_TO_COMMAND):
    raise RuntimeError(
        "Packaged item classifications diverge from the item command mapping"
    )


TRANSIENT_EFFECTS = {
    7770156: {"name": "damage_boost", "cvar": "g_damageScaleAllToAI", "factor": 1.50, "duration": 20.0},
    7770157: {"name": "damage_resistance", "cvar": "g_damageScaleAllToSlayer", "factor": 0.65, "duration": 20.0},
    7770158: {"name": "infinite_ammo", "cvar": "g_infiniteAmmo", "factor": 1, "duration": 10.0},
    7770159: {"name": "weakness_trap", "cvar": "g_damageScaleAllToAI", "factor": 0.70, "duration": 12.0},
    7770160: {"name": "vulnerability_trap", "cvar": "g_damageScaleAllToSlayer", "factor": 1.35, "duration": 12.0},
}


"""Explicit native baseline/health reads and transient command publication."""


TRANSIENT_EFFECT_BASELINE_FILE = "ap_effect_baseline.state"
