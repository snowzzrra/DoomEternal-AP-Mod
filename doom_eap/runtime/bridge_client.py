from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

import colorama
from websockets.exceptions import WebSocketException

from doom_eap.content.killsanity_plan import milestone_ids
from doom_eap.contracts.goal_policy import CAMPAIGN_GOAL_CONTRACT
from doom_eap.contracts.challenge_registry import canonical_map_name
from doom_eap.contracts.foundation import compile_item_delivery_plan, load_foundation_contracts, load_primitive_registry
from doom_eap.contracts.inventory_domain import InventoryObservationPort
from doom_eap.contracts.inventory_domain import DEFAULT_DEATH_LINK_MODE
from doom_eap.runtime import bridge_checks, bridge_config, bridge_items, bridge_saves
from doom_eap.runtime.ammo import AmmoRequestPump
from doom_eap.runtime.bridge_checks import CheckedVisuals, LocationSetup, PhysicalChecks, resolve_placement_records
from doom_eap.runtime.bridge_items import TransientEffectManager, TransientPublication
from doom_eap.runtime.bridge_saves import SaveChecks, SaveObserver
from doom_eap.runtime.campaign import STAGES, FastTravel, GoalProgress, Killsanity, UnifiedCampaign
from doom_eap.runtime.connection_diagnostics import ConnectionDiagnostics
from doom_eap.runtime.deathlink import DEATHLINK_MESSAGES, DeathLinkReceiver, DeathLinkSession, DeathObservation
from doom_eap.runtime.delivery import handle_delivery_control
from doom_eap.runtime.inventory import CLIENT_STATE_VERSION, ReceiptSession, SentinelInventoryObservation
from doom_eap.runtime.native_transport import (
    ARCHIPELAGO_EVENT_PLAIN_LIMIT,
    ProtocolFeed,
    ProtocolNames,
    StaleNativeResult,
    _bounded_event_text,
    emit_hints,
    format_archipelago_event,
    hints_key,
)
from doom_eap.runtime.observers import (
    BOOTSTRAP_ACTIONS,
    Bootstrap,
    LevelReady,
    PublisherDispatch,
    RuntimeLifecycle,
    RuntimeObservationLease,
    admit_slot_data,
    evaluate_dlc_availability,
)
from doom_eap.strings import text as player_text

# config prepares the Archipelago source path before CommonClient imports
# isort: split

import CommonClient as APCommonClient
import Utils
from CommonClient import ClientCommandProcessor, CommonContext, get_base_parser, gui_enabled


class SessionTasks:
    def __init__(self):
        self._tasks = set()
        self._generation = 0

    def start(self, operation):
        generation = self._generation

        async def run():
            if generation == self._generation:
                await operation()

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def invalidate(self):
        self._generation += 1
        for task in tuple(self._tasks):
            task.cancel()

    async def close(self):
        tasks = tuple(self._tasks)
        self.invalidate()
        await asyncio.gather(*tasks, return_exceptions=True)


class DoomCommandProcessor(ClientCommandProcessor):
    def _cmd_resync(self):
        self.ctx.session_tasks.start(lambda: _launcher_inventory_resync(self.ctx, {"domain": "all"}))

    def _cmd_ap_reconcile(self):
        """Manually restore replay-safe AP inventory after Mission Reset."""
        self.ctx.session_tasks.start(self._reconcile)

    async def _reconcile(self):
        plan, error = await self.ctx.items.manual_reconcile_inventory()
        if error:
            self.output(f'AP reconcile rejected: {error!s}')
            return
        self.output(
            f'replayed={plan.replayed!s} special_stages={plan.special_stages!s} skipped_never_replay={plan.skipped_never_replay!s} skipped_unproven={plan.skipped_unproven!s}'
        )

    def _cmd_doom_rune_diag(self):
        """Show AP Rune ownership and distinct native Rune state surfaces."""
        self.ctx.session_tasks.start(self._rune_diag)

    async def _rune_diag(self):
        for line in await self.ctx.items.observe_rune_diagnostic_lines():
            self.output(line)

    def _cmd_doom_context_diag(self):
        """Show bounded campaign-context and DLC evidence."""
        for key, value in self.ctx.items.context_support_report().items():
            self.output(f"{key}={json.dumps(value, sort_keys=True)}")


    def _cmd_doom_items_reset(self, confirmation: str = ""):
        """Preserve the receipt ledger when native reconstruction is unqualified."""
        self.output('AP reset refused: native reconstruction and spent consumables require verified backup evidence. Receipt history is preserved.')

    def _cmd_doom_status(self):
        """Show user-facing integration and tracker status."""
        ctx = getattr(self, "ctx", None)
        alive = getattr(ctx, "tracker_alive", False)
        degraded = getattr(ctx, "tracker_degraded", False) or getattr(ctx, "item_delivery_blocked", False)
        status_str = (
            'DEGRADED'
            if degraded
            else ('running' if alive else 'stopped')
        )
        hb_ts = getattr(ctx, "last_heartbeat_timestamp", None)
        hb_age = f"{time.time() - hb_ts:.1f}s" if hb_ts else 'never'
        restarts = getattr(ctx, "tracker_restart_count", 0)
        last_err = getattr(ctx, "last_tracker_error", "none")
        backoff = getattr(ctx, "tracker_backoff", 1.0)
        consec_err = getattr(ctx, "consecutive_same_error_count", 0)
        blocked_info = getattr(ctx, "item_delivery_blocked_info", None)

        self.output(f'DOOM integration status: {status_str!s}')
        self.output(f'Tracker alive: {alive!s}')
        self.output(f'Last heartbeat age: {hb_age!s}')
        self.output(f'Restart count: {restarts!s}')
        self.output(f'Consecutive error count: {consec_err!s}')
        self.output('Current backoff: {seconds}s'.format(seconds=f'{backoff:.1f}'))
        self.output(f'Last error summary: {last_err!s}')
        if blocked_info:
            self.output(
                'Blocked item: index={index} id={item_id} name={name}'.format(index=str(blocked_info.get("index")), item_id=str(blocked_info.get("item_id")), name=str(blocked_info.get("item_name")))
            )
        self.output(f'Detailed diagnostics: {bridge_config.BRIDGE_LOG_DIR!s}')

    def _cmd_doom_deathlink_diag(self):
        """Show bounded DeathLink receive evidence and active policy."""
        receiver = self.ctx.deathlink
        self.output(
            f"DeathLink mode={self.ctx.deathlink.mode} enabled={self.ctx.deathlink.enabled} "
            f"policy={'single_dispatch' if receiver.mode == 'soft' else 'retry_until_confirmed'}"
        )
        for entry in receiver.instrumentation()[-8:]:
            self.output(
                "DeathLink event={event_id} state={state} detail={detail} "
                "attempts={attempts} deliveries={deliveries}".format(**entry)
            )

    def _cmd_doom_onboarding_status(self):
        """Show the compact, safe bootstrap onboarding state."""
        for line in self.ctx.items.onboarding_status_lines():
            self.output(line)

    def _cmd_doom_test_plan(
        self, item_id: str = "", stage_flag: str = "", stage_value: str = ""
    ):
        """Compile and display an item plan without executing it."""
        if item_id == "location":
            self._show_location_plan(stage_flag)
            return
        try:
            parsed_id = int(item_id)
            stage = None
            if stage_flag:
                if stage_flag != "--stage":
                    raise ValueError("expected --stage <index>")
                stage = int(stage_value)
            plan = compile_item_delivery_plan(
                parsed_id, bridge_items.ITEM_ID_TO_COMMAND, stage=stage
            )
        except (ValueError, TypeError) as error:
            self.output(f"Usage: /doom_test_plan <item id> [--stage N] ({error})")
            return
        record = load_primitive_registry()["primitives"][plan.primitive_id]
        map_supported = canonical_map_name(self.ctx.runtime_lifecycle.map_identity.current_map) in {
            canonical_map_name(name)
            for name in load_foundation_contracts()["active_maps"].values()
        }
        self.output(
            f"item={plan.item_id} family={plan.family} primitive={plan.primitive_id} "
            f"evidence={record['status']} map={self.ctx.runtime_lifecycle.map_identity.current_map or 'unknown'} "
            f"entities_expected={'yes' if map_supported else 'unknown'}"
        )
        for command in plan.commands:
            self.output(
                f"{command.index}: entity={command.entity} command={command.command}"
            )
        if not plan.commands:
            self.output("No gameplay command: runtime-only/no-op item.")

    async def _cmd_doom_test_item(
        self, item_id: str = "", stage_flag: str = "", stage_value: str = ""
    ):
        """Execute the canonical item plan without simulating a NetworkItem."""
        try:
            parsed_id = int(item_id)
            stage = None
            if stage_flag:
                if stage_flag != "--stage":
                    raise ValueError("expected --stage <index>")
                stage = int(stage_value)
            plan = compile_item_delivery_plan(
                parsed_id, bridge_items.ITEM_ID_TO_COMMAND, stage=stage
            )
        except (ValueError, TypeError) as error:
            self.output(f"Usage: /doom_test_item <item id> [--stage N] ({error})")
            return
        correlation = await self.ctx.items.queue_dev_plan(plan, "item")
        if correlation:
            self.output(
                f"Queued {len(plan.commands)} map-side command(s): {correlation}; effect unconfirmed."
            )

    async def _cmd_doom_test_entity(self, entity: str = "", confirmation: str = ""):
        """Activate one allowlisted AP/test entity."""
        allowed = bool(re.fullmatch(r"ap_rpc_v3_[0-9]+(?:_[0-9]+)?", entity))
        allowed = allowed or entity.startswith("ap_test_")
        allowed = allowed or bool(re.fullmatch(r"ap_bootstrap_v[12]_[a-z_]+", entity))
        allowed = allowed or entity in set(bridge_checks.DECL_TO_LOCATION)
        allowed = allowed or entity == "ap_independent_rocket_launcher_7770056"
        if not allowed:
            self.output("Entity rejected by the directed-test allowlist.")
            return
        correlation = await self.ctx.items.queue_dev_commands(
            [f"ai_ScriptCmdEnt {entity} activate"], f"entity:{entity}"
        )
        self.output(f"Queued allowlisted entity: {correlation}; effect unconfirmed.")

    async def _cmd_doom_test_bootstrap(self, action_name: str = ""):
        """Record a diagnostic command without simulating receipts or changing bootstrap state."""
        if action_name == "suit_page":
            self.output("No active Suit Page bootstrap candidate.")
            self.output("The v2 stat-only candidate failed runtime validation.")
            return
        contracts = load_foundation_contracts()
        entity = contracts["bootstrap_test_entrypoints"].get(action_name)
        if not entity or action_name not in BOOTSTRAP_ACTIONS:
            self.output("Usage: /doom_test_bootstrap rune_page|frag_acquired|ice_acquired")
            return
        before = json.dumps(self.ctx.session_state.get("bootstrap", {}), sort_keys=True)
        correlation = await self.ctx.items.queue_dev_commands(
            [f"ai_ScriptCmdEnt {entity} activate"], f"bootstrap:{action_name}"
        )
        after = json.dumps(self.ctx.session_state.get("bootstrap", {}), sort_keys=True)
        if before != after:
            raise RuntimeError("Dev bootstrap mutated production bootstrap state")
        self.output(
            f"Queued experimental {action_name}: {correlation}. Record menu state manually."
        )

    async def _cmd_doom_test_location(
        self, location_id: str = "", confirmation: str = ""
    ):
        """Activate a registered map entrypoint."""
        try:
            parsed_id = int(location_id)
        except ValueError:
            parsed_id = -1
        entry = load_foundation_contracts()["location_entrypoints"].get(str(parsed_id))
        if not entry:
            self.output("No registered directed-test entrypoint for that location.")
            return
        if confirmation != "--confirm":
            self.output(
                f"This can change the save/check. Re-run /doom_test_location {parsed_id} --confirm"
            )
            return
        if canonical_map_name(self.ctx.runtime_lifecycle.map_identity.current_map) != canonical_map_name(entry["map"]):
            self.output(f"Wrong map: requires {entry['map']}, current={self.ctx.runtime_lifecycle.map_identity.current_map}")
            return
        correlation = await self.ctx.items.queue_dev_commands(
            [f"ai_ScriptCmdEnt {entry['entity']} activate"],
            f"location:{parsed_id}",
        )
        self.output(
            f"Queued map-side location entrypoint: {correlation}; check/objective remain runtime evidence."
        )

    def _cmd_doom_test_status(self):
        """Show isolated directed-test state."""
        self.output(f"map={self.ctx.runtime_lifecycle.map_identity.current_map or 'unknown'}")
        self.output(f"last_action={self.ctx.dev_last_action or '-'}")
        self.output(f"last_correlation={self.ctx.dev_last_correlation or '-'}")
        self.output("primitive_registry=foundation.py (embedded registry)")
        self.output(f"logs={bridge_config.BRIDGE_LOG_DIR}")

    def _show_location_plan(self, stage_flag):
        entry = load_foundation_contracts()["location_entrypoints"].get(stage_flag)
        if not entry:
            self.output("Usage: /doom_test_plan location <registered location id>")
            return
        record = load_primitive_registry()["primitives"][entry["primitive_id"]]
        self.output(
            f"location={stage_flag} primitive={entry['primitive_id']} "
            f"evidence={record['status']} entity={entry['entity']} map={entry['map']} "
            f"current_map={self.ctx.runtime_lifecycle.map_identity.current_map or 'unknown'} destructive=yes"
        )
        return


class DoomEternalContext(CommonContext):
    def get_ap_state_key(self):
        if not getattr(self, "server", None) or not getattr(self, "auth", None) or not getattr(self, "state_key", None):
            return None
        return self.state_key

    def persist_session_state(self):
        if getattr(self, "_item_state_error", None):
            raise RuntimeError(self._item_state_error)
        if not self.item_state_ready:
            return
        try:
            bridge_config.client_state_store().commit_session(
                self.client_state, self.session_state, processed_items=self.items.items_processed,
                cultist_autosave_path=self.goals.cultist_path,
                received_deathlink_event_ids=self.deathlink.seen_events,
                save_slot_observations=self.save_observer.observation_document,
            )
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            self.items._block_item_state(error)
            raise

    def on_deathlink(self, data: dict):
        super().on_deathlink(data)
        event_id = self.deathlink.receive(data, self.persist_session_state)
        if event_id is None:
            return
        source = _bounded_event_text(str(data.get("source") or player_text("deathlink", "source_fallback")), 128)
        cause = _bounded_event_text(str(data.get("cause") or ""), 512)
        bridge_config.emit_launcher_event(
            "deathlink",
            direction="received",
            event_id=event_id,
            source=source,
            cause=cause,
            message=cause or player_text("deathlink", "received", source=str(source)),
        )

    def on_print_json(self, args: dict):
        try:
            super().on_print_json(args)
        except Exception:
            bridge_config.logger.exception("[Bridge] Archipelago PrintJSON logging failed")
        try:
            event = format_archipelago_event(self.protocol_names(), args)
            if not self.protocol_feed.consume_echo(event):
                bridge_config.emit_launcher_event("archipelago", **event)
        except Exception:
            bridge_config.logger.exception("[Bridge] Archipelago PrintJSON event formatting failed")

    async def send_launcher_chat(self, text: str) -> None:
        """Send launcher text through CommonClient's canonical Say path."""
        if text.strip().casefold() == "/resync":
            self.session_tasks.start(lambda: _launcher_inventory_resync(self, {"domain": "all"}))
            return
        if not self.server or not self.server.socket.open or self.server.socket.closed:
            raise RuntimeError('Archipelago connection is unavailable')
        accepted = self.on_user_say(text)
        if accepted is None:
            raise RuntimeError('Archipelago rejected message')
        self.protocol_feed.record_echo(accepted)
        await self.send_msgs([{"cmd": "Say", "text": accepted}])

    def _launcher_hints_key(self):
        return hints_key(self.team, self.slot)

    def protocol_names(self):
        return ProtocolNames(self.item_names, self.location_names, dict(self.player_names), self.slot_concerns_self)

    def delivery_actions_ready(self):
        task = getattr(self, "input_task", None)
        return bridge_config.LAUNCHER_EVENTS_ENABLED and task is not None and not task.done()

    def _emit_launcher_hints(self, update_kind="DATA_RECEIVED"):
        key = self._launcher_hints_key()
        if key is not None:
            records = emit_hints(key, self.stored_data.get(key, []), self.protocol_names(), bridge_config.emit_launcher_event, bridge_config.logger, update_kind)
            self._presentation_hints_confirmed = (
                (self.room_seed_name, self.team, self.slot,
                 self._connected_slot_data.get("native_generation_fingerprint", "")),
                {record["location"] for record in records if record["finding_player"] == self.slot})

    def reset_room_session_authority(self, reason):
        task = getattr(self, "_slot_validation_task", None)
        if task is not None:
            task.cancel()
        self._slot_validation_task = None
        self._pending_slot_packages = []
        self._ammo_projection_active = False
        self.runtime_lifecycle.invalidate_work()
        self.session_tasks.invalidate()
        self.physical_checks.invalidate()
        self.level_ready.invalidate()
        self.location_setup.invalidate()
        self.protocol_feed.invalidate()
        self.ammo_requests.reset()
        self.session_tasks.start(lambda: self.items._publish_owner_ammo_refill())
        self.deathlink.invalidate_outbound()
        self.goals.invalidate_publication()
        self.publisher_dispatch.invalidate()
        self.save_checks.invalidate()
        self._room_session_authoritative = False
        manager = getattr(self, "transient_effect_manager", None)
        if manager is not None:
            self.items.reset_transient_effects(reason)

    def _report_launcher_connection_failure(
        self, message, *, code="connection_failed", reason_codes=None,
        technical_message=None,
    ):
        if (
            not bridge_config.LAUNCHER_EVENTS_ENABLED
            or self._launcher_connection_failure_reported
        ):
            return
        self._launcher_connection_failure_reported = True
        bridge_config.emit_launcher_event(
            "error",
            code=code,
            message=message,
            technical_message=technical_message or message,
            reason_codes=list(reason_codes or ()),
        )
        self.disconnected_intentionally = True
        self.cancel_autoreconnect()
        self.exit_event.set()

    def handle_connection_loss(self, msg: str) -> None:
        self.items.reset_transient_effects("connection_loss")
        self.reset_room_session_authority("connection_loss")
        self.deathlink.abandon(time.monotonic(), "disconnect")
        super().handle_connection_loss(msg)
        if self._room_session_established:
            if bridge_config.LAUNCHER_EVENTS_ENABLED and not self._launcher_connection_loss_reported:
                self._launcher_connection_loss_reported = True
                bridge_config.emit_launcher_event(
                    "connection_lost",
                    code="connection_closed",
                    message='The room connection was interrupted; reconnecting automatically.',
                    technical_message=msg,
                )
        else:
            self._report_launcher_connection_failure(
                msg, code=getattr(self, "_launcher_transport_code", "connection_failed"), technical_message=msg,
            )

    async def connection_closed(self):
        diagnostics = getattr(self, "connection_diagnostics", None)
        if diagnostics is not None:
            diagnostics.disconnected()
        self.items.reset_transient_effects("connection_closed")
        self.reset_room_session_authority("connection_closed")
        self.deathlink.abandon(time.monotonic(), "disconnect")
        unexpected_launcher_close = (
            bridge_config.LAUNCHER_EVENTS_ENABLED
            and self.server is not None
            and not self.disconnected_intentionally
            and not self.exit_event.is_set()
        )
        await super().connection_closed()
        if unexpected_launcher_close and not self._room_session_established:
            self._report_launcher_connection_failure('Disconnected from the Archipelago server', code="connection_closed")
        elif unexpected_launcher_close and not self._launcher_connection_loss_reported:
            self._launcher_connection_loss_reported = True
            bridge_config.emit_launcher_event(
                "connection_lost",
                code="connection_closed",
                message='The room connection was interrupted; reconnecting automatically.',
                technical_message="Disconnected from the Archipelago server",
            )

    async def server_auth(self, password_requested: bool = False):
        if password_requested and bridge_config.LAUNCHER_EVENTS_ENABLED and not self.password:
            self._report_launcher_connection_failure(
                'This room requires a password.',
                code="password_required",
                reason_codes=[],
                technical_message="Archipelago requested a password and none was supplied",
            )
            return
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.get_username()
        await self.send_connect()

    def _reject_slot_contract(self, error):
        message = str(error)
        bridge_config.logger.error("[Contract] Connected slot rejected: %s", error)
        self._report_launcher_connection_failure(message, code=getattr(error, "code", "room_incompatible"), technical_message=message)

    async def _validate_connected_package(self, args):
        slot_data = args.get("slot_data", {})
        started = time.perf_counter()
        try:
            admission = await asyncio.to_thread(admit_slot_data, slot_data if isinstance(slot_data, dict) else {})
        except ValueError as error:
            self._slot_validation_task = None
            self._pending_slot_packages = []
            self._reject_slot_contract(error)
            return
        bridge_config.logger.info("ROOM_SLOT_VALIDATION elapsed_ms=%d", round((time.perf_counter() - started) * 1000))
        previous = self.room_delivery
        if previous is not None:
            await previous.close()
            self.room_delivery = None
            self.delivery_coordinator = None
        pending, self._pending_slot_packages = self._pending_slot_packages, []
        self._slot_validation_task = None
        self._handle_connected(args, dict(slot_data), admission)
        if self.item_state_ready:
            for cmd, packet, received_count in pending:
                self._handle_package(cmd, packet, received_count=received_count)

    def on_package(self, cmd: str, args: dict):
        if cmd == "Connected":
            task = self._slot_validation_task
            if task is not None:
                task.cancel()
            self._pending_slot_packages = []
            self.item_state_ready = False
            self._slot_validation_task = asyncio.create_task(self._validate_connected_package(args))
        elif self._slot_validation_task is not None and cmd not in {"RoomInfo", "ConnectionRefused"}:
            if len(self._pending_slot_packages) >= 256:
                self.reset_room_session_authority("pending_packets_limit")
                self._reject_slot_contract(ValueError('Too many packets received before room validation completed'))
            else:
                received_count = len(self.items_received) if cmd == "ReceivedItems" else None
                self._pending_slot_packages.append((cmd, args, received_count))
        else:
            self._handle_package(cmd, args)

    def _handle_checked_locations(self, args):
        self.server_checked_locations_ready = isinstance(args.get("checked_locations"), list | tuple | set | frozenset)
        if self.server_checked_locations_ready:
            self.checked_locations.update(args["checked_locations"])
            if any(stage["completion_id"] in args["checked_locations"] for stage in STAGES.values()):
                self.items._schedule_item_delivery("mission_complete")
            self.checks.snapshot_fast_travel_eligibility()
        self.publisher_dispatch.observe_protocol(self.checks.check_observation())
        self.session_tasks.start(lambda: self._reconcile_server_checks("server_checked_update"))
        self.session_tasks.start(self.saves.check_mission_challenge_locations)
        self.session_tasks.start(lambda: self.checks.evaluate_campaign_goal("server checked update"))

    def _handle_package(self, cmd: str, args: dict, *, received_count=None):
        self.items._receive_owner_storage(cmd, args)
        if cmd == "RoomInfo":
            self.items.reset_transient_effects("room_info")
            # Runtime effects require a matching
            # Connected identity for this room.
            self.reset_room_session_authority("room_info")
            self.room_seed_name = args.get("seed_name")
        elif cmd == "ReceivedItems":
            self._on_received_items_packet(args, authoritative_count=received_count)
        elif cmd == "LocationInfo":
            self._consume_location_info(args)
        elif cmd in {"Retrieved", "SetReply"}:
            self._handle_hint_update(cmd, args)
        elif cmd == "ConnectionRefused":
            self._connection_refused(args)
        elif cmd == "RoomUpdate" and "checked_locations" in args:
            self._handle_checked_locations(args)
        elif cmd == "Bounced" and "DeathLink" in args.get("tags", []):
            data = args.get("data", {})
            self.deathlink.observe_echo(data.get("time"), self.last_death_link)

    def _fail_placement_scout(self, message):
        if not self.location_setup.fail(message):
            return
        self.server_checked_locations_ready = False
        self._report_launcher_connection_failure(
            message, code="malformed_server_data", technical_message=message
        )

    async def _scout_active_locations(self):
        result = await self.location_setup.scout(self.checks.check_publication())
        if not result.current:
            return
        if result.error:
            self._fail_placement_scout(result.error)
            return
        self._try_complete_location_scouts()

    def _consume_location_info(self, args):
        error = self.location_setup.consume(args, frozenset(self.locations_info))
        if error:
            self._fail_placement_scout(error)
            return
        self._try_complete_location_scouts()

    def _try_complete_location_scouts(self):
        if not self.location_setup.ready_to_resolve:
            return
        try:
            placements = resolve_placement_records(
                self.location_setup.received_ids, self.locations_info, self.slot_info, self.protocol_names(), self.slot
            )
        except (ValueError, KeyError) as error:
            self._fail_placement_scout(f"Placement resolution failed: {error}")
            return
        except Exception as error:
            bridge_config.logger.exception("Unexpected placement resolution failure")
            self._fail_placement_scout(f"Unexpected placement resolution failure: {type(error).__name__}: {error}")
            return
        self.location_setup.resolved()
        self._complete_connected(placements)

    def _complete_connected(self, placements):
        self.server_checked_locations_ready = True
        self.session_tasks.start(lambda: self.items.onboard_bootstrap("on_connect"))
        self.session_tasks.start(lambda: self._reconcile_server_checks("server_connected"))
        self.session_tasks.start(self.saves.check_mission_challenge_locations)
        self.session_tasks.start(lambda: self.checks.evaluate_campaign_goal("server connected"))
        if self._item_delivery_wakeup:
            self.items._schedule_item_delivery("connected")
        balance = self.items._ammo_refill_balance_payload()
        bridge_config.emit_launcher_event(
            "connected",
            seed_name=self.room_seed_name,
            endpoint=str(getattr(self, "server_address", "") or ""),
            team=self.team,
            slot=self.slot,
            player_names=dict(self.player_names),
            item_names={item: self.item_names.lookup_in_game(item, self.game) for item in bridge_items.ITEM_ID_TO_COMMAND},
            slot_data=self._connected_slot_data,
            missing_locations=sorted(self.missing_locations),
            checked_locations=sorted(self.checked_locations),
            placements=placements,
            placement_scouts_complete=True,
            ammo_refills_available=balance["available"],
            ammo_refills_consumed=balance["consumed"],
            ammo_refills_authoritative=balance["authoritative"],
        )

    def _on_received_items_packet(self, args, *, authoritative_count=None):
        """Record packet metadata and wake delivery without doing delivery work."""
        packet_received_ns = time.monotonic_ns()
        packet_items = args.get("items", ()) if isinstance(args, dict) else ()
        try:
            packet_item_count = len(packet_items)
        except TypeError:
            packet_item_count = 0
        if authoritative_count is None:
            authoritative_count = len(self.items_received)
        packet_start_index, packet_accepted = self.receipt_session.observe_packet(
            args.get("index") if isinstance(args, dict) else None,
            packet_item_count, authoritative_count, packet_received_ns,
        )
        if packet_accepted and packet_start_index == 0:
            self._item_history_received = True
        bridge_config.log_item_event(
            "ITEM_PACKET_OBSERVATION",
            packet_start_index=packet_start_index,
            packet_count=packet_item_count,
            authoritative_count=authoritative_count,
            boundary_before=getattr(self.items, "items_processed", 0),
            boundary_after=getattr(self.items, "items_processed", 0),
            received_count=authoritative_count,
            processed_count=getattr(self.items, "items_processed", 0),
            state_key=getattr(self, "state_key", None) or "unresolved",
            success=packet_accepted,
            packet_received_monotonic_ns=packet_received_ns,
        )
        self.items._schedule_item_delivery("packet")

    async def _reconcile_server_checks(self, trigger):
        try:
            await self.checks.reconcile_checked_automap_cleanup(trigger)
            await self.checks.reconcile_fast_travel_unlock(trigger)
        except StaleNativeResult:
            return

    def _abandon_previous_room(self, previous_state_key):
        if previous_state_key and previous_state_key != self.state_key:
            abandoned = self.deathlink.abandon(time.monotonic(), "room_changed")
            if abandoned:
                bridge_config.logger.warning(
                    "[DeathLink] Cleared room-bound receive events after slot change: %s.",
                    ", ".join(event_id[:12] for event_id in abandoned),
                )

    def _start_location_scouts(self, args, slot_data):
        self.server_checked_locations_ready = False
        try:
            active = self.location_setup.begin(args, bridge_config._doom_location_ids(), milestone_ids(slot_data["killsanity"]))
        except ValueError as error:
            self._fail_placement_scout(str(error))
            return
        self.locations_info.clear()
        if not active:
            self._try_complete_location_scouts()
        else:
            self.session_tasks.start(self._scout_active_locations)
        self.ammo_requests.reset()

    def _handle_connected(self, args, slot_data, admission):
        self.items._receive_owner_storage("Connected", args)
        self._item_history_received = False
        self._room_session_established = True
        self._launcher_connection_loss_reported = False
        self._connected_slot_data = slot_data
        previous_state_key = self.state_key
        self.items.initialize_item_state(slot_data)
        if self._item_state_error:
            return
        self._abandon_previous_room(previous_state_key)
        try:
            self.unified_campaign = UnifiedCampaign(admission, self.session_state, self.persist_session_state)
        except ValueError as error:
            message = f'Estado local incompatÃƒÂ­vel com a campanha: {error!s}'
            bridge_config.logger.error("[State] Connected campaign rejected: %s", error)
            self._report_launcher_connection_failure(
                message, code="campaign_state_incompatible", technical_message=message,
            )
            return
        self.goals.connected(self.persist_session_state)
        self.items._refresh_runtime_context(slot_data)
        self.deathlink.configure(slot_data["runtime"]["death_link"]["enabled"], slot_data["runtime"]["death_link"]["mode"])
        bridge_config.logger.info(
            "[DeathLink] enabled=%s receive_policy=native mode=%s",
            self.deathlink.enabled,
            self.deathlink.mode,
        )
        self.receipt_session.configure_starting_ownership(slot_data)
        self._death_link_task = self.session_tasks.start(
            lambda: self.update_death_link(self.deathlink.enabled)
        )
        self._start_location_scouts(args, slot_data)

    def _connection_refused(self, args):
        self.reset_room_session_authority("connection_refused")
        reasons = args.get("errors", [])
        if not isinstance(reasons, list | tuple):
            reasons = [reasons]
        self._report_launcher_connection_failure(
            'Archipelago rejected the room login.',
            code="connection_refused",
            reason_codes=reasons,
            technical_message="ConnectionRefused: " + ", ".join(map(str, reasons)),
        )

    def _handle_hint_update(self, cmd, args):
        key = self._launcher_hints_key()
        if (cmd == "Retrieved" and key in args.get("keys", {})) or (
            cmd == "SetReply" and args.get("key") == key
        ):
            self._emit_launcher_hints("DATA_RECEIVED" if cmd == "Retrieved" else "UPDATED")

    command_processor: type = DoomCommandProcessor

    game = bridge_config.GAME_NAME

    items_handling = 0b011 if bridge_config._CONTENT_IDENTITY["slot_data_schema_version"] == 7 else 0b111

    max_size = 64 * 1024 * 1024

    def __init__(self, server_address, password):
        super().__init__(server_address, password)
        self.checks = bridge_checks.CheckRuntime(self)
        self.items = bridge_items.ItemRuntime(self)
        self.saves = bridge_saves.SaveRuntime(self)
        self.dev_session_id = uuid.uuid4().hex[:8]
        self.dev_counter = 0
        self.dev_last_action = None
        self.dev_last_correlation = None
        self.tracking_task = None
        self._item_delivery_lock = asyncio.Lock()
        self._item_delivery_task = None
        self._item_delivery_wakeup = False
        self._item_delivery_waiting_for_state = False
        self.receipt_session = ReceiptSession()
        self.room_delivery = None
        self._room_session_authoritative = False
        self._ammo_projection_active = False
        self._ammo_projection_confirmed = None
        self.ammo_requests = AmmoRequestPump(self.items.request_ammo_refill, self.exit_event, bridge_config.logger)
        self.tracker_alive = False
        self.tracker_restart_count = 0
        self.last_tracker_error = None
        self.last_heartbeat_timestamp = None
        self.item_state_ready = False
        self._slot_validation_task = None
        self._pending_slot_packages = []
        self.reconnect_resync_attempted = False
        self.client_state = {"version": CLIENT_STATE_VERSION, "sessions": {}}
        self.state_key = ""
        self.base_directory = bridge_config.DOOM_BASE_DIR
        self.transient_effect_manager = TransientEffectManager(TransientPublication(self.items._send_runtime_command), os.getpid())
        self.session_state = {}
        self.goals = GoalProgress(CAMPAIGN_GOAL_CONTRACT["runtime_map"], bridge_config.CULTIST_BASE_MAP, bridge_config.logger)
        self.publisher_dispatch = PublisherDispatch(bridge_checks.PUBLISHERS, self.goals, bridge_config.logger)
        self.physical_checks = PhysicalChecks(bridge_config.logger)
        self.level_ready = LevelReady(bridge_config.logger)
        self.session_tasks = SessionTasks()
        self.location_setup = LocationSetup(bridge_config.logger)
        self.protocol_feed = ProtocolFeed()
        self.save_checks = SaveChecks(bridge_saves.WEAPON_MASTERY_BY_UNLOCKABLE, bridge_saves.MISSION_CHALLENGE_BY_UNLOCKABLE, bridge_saves.MISSION_CHALLENGE_RUNTIME_MAP_BY_UNLOCKABLE, bridge_saves.ALL_MISSION_CHALLENGES_ENTRIES, bridge_config.logger)
        self.death_observer = DeathObservation(bridge_config.logger)
        self.killsanity = Killsanity(bridge_config.logger)
        self.killsanity_counter_visible = bridge_config.config.get("killsanity_counter_visible", True) is True
        self.save_observer = SaveObserver()
        self.inventory_observation_port: InventoryObservationPort = SentinelInventoryObservation(self, bridge_config.logger, bridge_config.log_item_event)
        self.runtime_observation_lease = RuntimeObservationLease()
        self.save_observer.clear_mission_select()
        self.last_observer_lease_block = None
        # Pending lethal commands stay process-local. Seen event identities persist per
        # room so reconnect/transport replay cannot create a second logical event.
        self.deathlink = DeathLinkSession(DeathLinkReceiver(
            wait_timeout=bridge_config.DEATHLINK_RECEIVE_TIMEOUT,
            confirm_timeout=bridge_config.DEATHLINK_CONFIRM_TIMEOUT,
            retry_interval=bridge_config.DEATHLINK_KILL_INTERVAL,
            total_timeout=bridge_config.DEATHLINK_TOTAL_TIMEOUT,
            late_suppression_grace=bridge_config.DEATHLINK_LATE_SUPPRESSION_GRACE,
            max_attempts=bridge_config.DEATHLINK_MAX_ATTEMPTS,
            mode=DEFAULT_DEATH_LINK_MODE,
        ), DEATHLINK_MESSAGES, bridge_config.emit_launcher_event, bridge_config.logger)
        self.room_seed_name = None
        self._connected_slot_data = {}
        self.runtime_lifecycle = RuntimeLifecycle()
        self.dlc_evidence = evaluate_dlc_availability(None)
        self.bootstrap = Bootstrap(bridge_config.logger)
        self.checked_visuals = CheckedVisuals(bridge_checks.KNOWN_CATALOG_MAPS, bridge_checks.AUTOMAP_VISUALS_BY_MAP, uuid.uuid4().hex[:8], bridge_config.logger)
        self._native_automap_published = None
        self.server_checked_locations_ready = False
        self.fast_travel = FastTravel(bridge_checks.KNOWN_CATALOG_MAPS, bridge_checks.FAST_TRAVEL_MAP_KEYS, bridge_config.FAST_TRAVEL_MISSION_COMPLETE_IDS, bridge_config.logger)
        self._launcher_connection_failure_reported = False
        self._room_session_established = False
        self._launcher_connection_loss_reported = False


def log_mission_bridge_identity():
    from doom_eap.runtime import inventory

    executable = Path(sys.executable).resolve()
    try:
        executable_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
    except OSError as error:
        executable_sha256 = f"unavailable:{type(error).__name__}"
    bridge_config.logger.info("BRIDGE_REVISION=%s", bridge_config.BRIDGE_REVISION)
    bridge_config.logger.info("BRIDGE_FILE=%s", bridge_config.BRIDGE_FILE)
    bridge_config.logger.info("BRIDGE_SHA256=%s", bridge_config.BRIDGE_SHA256)
    bridge_config.logger.info("BRIDGE_EXECUTABLE_SHA256=%s", executable_sha256)
    bridge_config.logger.info("BRIDGE_PROTOCOL=%s", bridge_config.BRIDGE_PROTOCOL)
    bridge_config.logger.info("GAME_NAME=%s", bridge_config.GAME_NAME)
    bridge_config.logger.info("TRANSITION_HANDLER=%s", bridge_config.TRANSITION_HANDLER)
    bridge_config.log_item_event(
        "ITEM_WORKER_RUNTIME_IDENTITY",
        frozen=bool(getattr(sys, "frozen", False)),
        executable=str(executable),
        executable_sha256=executable_sha256,
        application_dir=str(bridge_config.APPLICATION_DIR),
        modules={
            __name__: {
                "origin": __spec__.origin if __spec__ else None,
                "loader": type(__loader__).__name__,
                "methods": {
                    name: hashlib.sha256(getattr(bridge_items.ItemRuntime, name).__code__.co_code).hexdigest()
                    for name in (
                        "process_pending_item_receipts",
                        "native_game_link",
                    )
                },
            },
            inventory.__name__: {
                "origin": inventory.__spec__.origin if inventory.__spec__ else None,
                "loader": type(inventory.__loader__).__name__,
                "methods": {
                    "SentinelWeaponPoints._run": hashlib.sha256(
                        inventory.SentinelWeaponPoints._run.__code__.co_code
                    ).hexdigest(),
                },
            },
        },
    )


async def amain(launch_args=None):
    bridge_config.start_bridge_logger()
    bridge_config.log_effective_runtime_paths()
    Utils.init_logging("DoomEternalClient")
    parser = get_base_parser()
    parser.add_argument('--name', default=None, help="Player name no Archipelago")
    args = parser.parse_args(launch_args)
    args.password = args.password or os.environ.get("DOOM_AP_PASSWORD")

    ctx = DoomEternalContext(args.connect, args.password)
    ctx.auth = args.name
    ctx.connection_diagnostics = ConnectionDiagnostics(APCommonClient.websockets, ctx)
    APCommonClient.websockets = ctx.connection_diagnostics
    loop_monitor = asyncio.create_task(ctx.connection_diagnostics.monitor_loop(), name="bridge loop diagnostics")
    if await ctx.items._query_process_identity():
        bridge_checks.cleanup_active_map_markers()
        bridge_checks.cleanup_ammo_refill_request_files()
        bridge_checks.cleanup_telemetry_dumps()
    ctx.tracking_task = asyncio.create_task(ctx.checks.tracker_supervisor())
    ctx.death_task = asyncio.create_task(ctx.checks.death_monitor_supervisor())
    ctx.physical_check_task = asyncio.create_task(ctx.checks.physical_check_loop())

    log_mission_bridge_identity()
    bridge_config.emit_launcher_event("client_started")
    bridge_config.logger.info("=== DOOM ETERNAL ARCHIPELAGO CLIENT ===")
    if not args.connect or not args.name:
        bridge_config.logger.info(
            "Use the GUI connection fields, or pass --connect and --name "
            "on the command line."
        )
    else:
        bridge_config.logger.info(f"Auto-connecting to {args.connect} as {args.name}...")
        bridge_config.emit_launcher_event("connecting")

    if bridge_config.LAUNCHER_EVENTS_ENABLED:
        original_process_server_cmd = APCommonClient.process_server_cmd

        async def launcher_process_server_cmd(client_ctx, package):
            # CommonClient handles ConnectionRefused before calling on_package,
            # and raises away its machine-readable reason. The launcher owns the
            # retry UX, so preserve that packet before CommonClient can flatten it.
            if package.get("cmd") == "ConnectionRefused":
                client_ctx.on_package("ConnectionRefused", package)
                return
            await original_process_server_cmd(client_ctx, package)

        APCommonClient.process_server_cmd = launcher_process_server_cmd

    APCommonClient.server_loop = server_loop
    ctx.server_task = asyncio.create_task(server_loop(ctx), name="server loop")
    ctx.server_task.add_done_callback(lambda task: report_server_stop(ctx, task))

    if gui_enabled:
        raise RuntimeError("DOOM Eternal bridge worker requires --nogui")
    ctx.input_task = asyncio.create_task(launcher_control_loop(ctx), name="Launcher control")

    await ctx.exit_event.wait()
    bridge_config.emit_launcher_event("client_stopping")
    ctx.reset_room_session_authority("bridge_shutdown")
    tasks = [ctx.tracking_task, ctx.death_task, ctx.physical_check_task, ctx.input_task, loop_monitor]
    tasks.extend(_pending_delivery_tasks(ctx))
    for task in tasks:
        task.cancel()
    await ctx.session_tasks.close()
    await asyncio.gather(*tasks, return_exceptions=True)
    await ctx.items.close_room_delivery()
    await ctx.shutdown()
    await ctx.items._publish_owner_ammo_refill()
    await ctx.session_tasks.close()
    if hasattr(ctx, "_native_worker"):
        if getattr(ctx, "_native_link", None) is not None:
            await ctx._native_worker.call(ctx._native_link.close)
        ctx._native_worker.close()
    await ctx.connection_diagnostics.close()
    APCommonClient.websockets = ctx.connection_diagnostics.websockets


def launch(*launch_args):
    colorama.init()
    asyncio.run(amain(launch_args))
    colorama.deinit()


async def launcher_control_loop(ctx):
    """Receive launcher IPC without invoking CommonClient's console parser."""
    while not ctx.exit_event.is_set():
        try:
            raw_line = await asyncio.to_thread(sys.stdin.readline)
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError) as error:
            bridge_config.logger.warning("[Launcher] Control input closed: %s", error)
            ctx.exit_event.set()
            return

        if raw_line == "":
            ctx.exit_event.set()
            return

        line = raw_line.rstrip("\r\n")
        if line == "/exit":
            ctx.exit_event.set()
            return
        if not line.startswith("AP_CONTROL "):
            continue

        try:
            control = json.loads(line[len("AP_CONTROL "):])
        except json.JSONDecodeError:
            bridge_config.emit_launcher_event("chat_send_failed", message='Invalid launcher control message')
            continue
        if not isinstance(control, dict):
            continue
        await _dispatch_launcher_control(ctx, control)


def report_server_stop(ctx, task):
    try:
        error = task.exception()
    except asyncio.CancelledError:
        error = None
    if ctx.exit_event.is_set():
        ctx.reset_room_session_authority("server_loop_stopped")
        return
    ctx.reset_room_session_authority("server_loop_stopped")
    if ctx._room_session_established and not ctx.disconnected_intentionally:
        # CommonClient owns reconnect and outage reporting after an established session.
        return
    if error is not None:
        bridge_config.emit_launcher_event(
            "error",
            code="server_loop_failed",
            message=f"{type(error).__name__}: {error}",
        )
    else:
        bridge_config.emit_launcher_event("disconnected")


async def server_loop(ctx, address=None):
    if ctx.server and ctx.server.socket:
        APCommonClient.logger.error("Already connected")
        return
    address = ctx.server_address if address is None else address
    if not address:
        APCommonClient.logger.info("Please connect to an Archipelago server.")
        return
    ctx.cancel_autoreconnect()
    address, server_url = _server_url(ctx, address)

    def reconnect_hint():
        return ", type /connect to reconnect" if ctx.server_address else ""

    try:
        address, socket = await _connect_socket(ctx, address, server_url)
        await _receive_server(ctx, address, server_url, reconnect_hint, socket)
    except (WebSocketException, OSError, ValueError, RuntimeError) as error:
        APCommonClient.logger.debug("Archipelago connection failed", exc_info=True)
        ctx.handle_connection_loss(_connection_loss_message(error, reconnect_hint))
    finally:
        await ctx.connection_closed()
        _schedule_reconnect(ctx)


def _pending_delivery_tasks(ctx):
    tasks = []
    item_delivery_task = ctx._item_delivery_task
    if item_delivery_task is not None:
        tasks.append(item_delivery_task)
    inventory_resync_task = getattr(ctx, "_inventory_resync_task", None)
    if inventory_resync_task is not None:
        tasks.append(inventory_resync_task)
    return tasks


async def _launcher_inventory_resync(ctx, control):
    correlation = _resync_correlation(ctx)
    try:
        plan, error = await ctx.items.manual_reconcile_inventory_async(
            domain=control.get("domain", "all"), item_id=control.get("item_id"))
    except (OSError, RuntimeError, ValueError, WebSocketException) as error:
        bridge_config.logger.warning("[Resync] Manual inventory resync failed: %s", error)
        bridge_config.emit_launcher_event("inventory_resync", status="error",
            message=_bounded_event_text(str(error), ARCHIPELAGO_EVENT_PLAIN_LIMIT), **correlation)
        return
    if error or plan is None:
        bridge_config.emit_launcher_event(
            "inventory_resync",
            status="error",
            message=_bounded_event_text(str(error or 'manual inventory resync returned no plan'), ARCHIPELAGO_EVENT_PLAIN_LIMIT),
            **_resync_correlation(ctx),
        )
    else:
        bridge_config.emit_launcher_event("inventory_resync", status="observed" if plan.commands else "noop",
                            command_count=len(plan.commands), **_resync_correlation(ctx))


def _resync_correlation(ctx):
    from dataclasses import asdict
    if ctx.room_delivery is not None:
        return {"room": asdict(ctx.room_delivery.key), "command_ids": list(ctx.room_delivery.planned_ids)}
    return {}


async def _handle_delivery_control(ctx, control):
    if await handle_delivery_control(getattr(ctx, "delivery_coordinator", None), control, bridge_config.emit_launcher_event):
        ctx.items._schedule_item_delivery("resolution")


async def _dispatch_launcher_control(ctx, control):
    await _handle_delivery_control(ctx, control)
    match control.get("type"):
        case "killsanity_counter_visibility":
            if type(control.get("visible")) is bool:
                ctx.killsanity_counter_visible = control["visible"]
        case "core_runtime_reload":
            _reload_core_runtime(ctx, control)
        case "support_condump":
            await _support_condump(ctx, control)
        case "inventory_resync":
            task = getattr(ctx, "_inventory_resync_task", None)
            if task is None or task.done():
                ctx._inventory_resync_task = asyncio.create_task(_launcher_inventory_resync(ctx, control))
        case "ammo_refill":
            await _launcher_refill(ctx, control)
        case "say":
            await _launcher_say(ctx, control)


def _reload_core_runtime(ctx, control):
    updated = bridge_config.load_config()
    for key in ("core_runtime_manifest", "selected_core_runtime_manifest", "proton_executable", "proton_compat_data_dir"):
        if key in updated:
            bridge_config.config[key] = updated[key]
        else:
            bridge_config.config.pop(key, None)
    ctx._native_link_key = None
    bridge_config.refresh_death_probe_proton()
    bridge_config.emit_launcher_event("core_runtime_bound", path=bridge_config.config.get("core_runtime_manifest") or bridge_config.config.get("selected_core_runtime_manifest"))


async def _support_condump(ctx, control):
    try:
        queued = await ctx.checks.request_support_condump()
    except (OSError, RuntimeError, ValueError, WebSocketException) as error:
        bridge_config.logger.warning("[Support] Diagnostic condump request failed: %s", error)
        bridge_config.emit_launcher_event("support_condump", status="error", message=str(error))
    else:
        status = "queued" if queued else "unavailable"
        bridge_config.emit_launcher_event("support_condump", status=status)


async def _launcher_refill(ctx, control):
    try:
        await ctx.items.request_ammo_refill()
    except (OSError, RuntimeError, ValueError, WebSocketException) as error:
        bridge_config.logger.warning("[Ammo Refill] Launcher request failed: %s", error)
        bridge_config.emit_launcher_event(
            "ammo_refill",
            status="error",
            message=_bounded_event_text(str(error), ARCHIPELAGO_EVENT_PLAIN_LIMIT),
        )


async def _launcher_say(ctx, control):
    text = control.get("text")
    if not isinstance(text, str) or not text.strip():
        return
    try:
        await ctx.send_launcher_chat(text)
    except (OSError, RuntimeError, ValueError, WebSocketException) as error:
        bridge_config.logger.warning("[Launcher] Chat send failed: %s", error)
        bridge_config.emit_launcher_event("chat_send_failed", message=str(error))
    else:
        bridge_config.emit_launcher_event("chat_sent", text=text)


def _server_url(ctx, address):
    if ctx._messagebox_connection_loss:
        ctx._messagebox_connection_loss.dismiss()
        ctx._messagebox_connection_loss = None
    address = f"ws://{address}" if "://" not in address else address.replace("archipelago://", "ws://")
    server_url = urllib.parse.urlparse(address)
    if server_url.username:
        ctx.username = urllib.parse.unquote(server_url.username)
    if server_url.password:
        ctx.password = urllib.parse.unquote(server_url.password)
    return address, server_url


async def _receive_server(ctx, address, server_url, reconnect_hint, socket):
    if ctx.ui is not None:
        ctx.ui.update_address_bar(server_url.netloc)
    ctx.server = APCommonClient.Endpoint(socket)
    APCommonClient.logger.info("Connected")
    ctx.server_address = address
    ctx.current_reconnect_delay = ctx.starting_reconnect_delay
    ctx.disconnected_intentionally = False
    async for data in ctx.server.socket:
        for msg in APCommonClient.decode(data):
            await APCommonClient.process_server_cmd(ctx, msg)
    APCommonClient.logger.warning("Disconnected from multiworld server%s", reconnect_hint())


async def _connect_socket(ctx, address, server_url):
    while True:
        APCommonClient.logger.info("Connecting to Archipelago server at %s", address)
        try:
            socket = await APCommonClient.websockets.connect(
                address, port=server_url.port or 38281, ping_timeout=None, ping_interval=None,
                ssl=APCommonClient.get_ssl_context() if address.startswith("wss://") else None,
                max_size=ctx.max_size)
            return address, socket
        except APCommonClient.websockets.InvalidMessage:
            if not address.startswith("ws://"):
                raise
            address = "wss://" + address[len("ws://"):]


def _schedule_reconnect(ctx):
    if (ctx.server_address and ctx.username and not ctx.disconnected_intentionally
            and not ctx.exit_event.is_set()):
        APCommonClient.logger.info("... automatically reconnecting in %s seconds", ctx.current_reconnect_delay)
        assert ctx.autoreconnect_task is None
        ctx.autoreconnect_task = asyncio.create_task(APCommonClient.server_autoreconnect(ctx), name="server auto reconnect")
    ctx.current_reconnect_delay *= 2


def _connection_loss_message(error, reconnect_hint):
    if isinstance(error, APCommonClient.websockets.InvalidMessage):
        return "Lost connection to the multiworld server due to InvalidMessage" + reconnect_hint()
    if isinstance(error, ConnectionRefusedError):
        return "Connection refused by the server. May not be running Archipelago on that address or port."
    if isinstance(error, APCommonClient.websockets.InvalidURI):
        return "Failed to connect to the multiworld server (invalid URI)"
    if isinstance(error, asyncio.TimeoutError):
        return "Failed to connect to the multiworld server. Connection timed out."
    if isinstance(error, OSError):
        return "Failed to connect to the multiworld server"
    return "Lost connection to the multiworld server" + reconnect_hint()


if __name__ == '__main__':
    launch(*sys.argv[1:])
