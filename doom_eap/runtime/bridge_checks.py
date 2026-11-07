from __future__ import annotations

import asyncio
import copy
import glob
import hashlib
import json
import os
import re
import shutil
import time
import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from websockets.exceptions import WebSocketException

from doom_eap.content.automap_visual_registry import (
    checked_reconciliation_entity,
    index_automap_visual_registry,
    load_automap_visual_registry,
)
from doom_eap.content.item_classification import ITEM_CLASSIFICATION_TRAP
from doom_eap.content.publisher_loader import load_publisher_contracts
from doom_eap.contracts.check_observation import CheckObservation
from doom_eap.contracts.command_publication import (
    CHECKED_VISUAL_HIDE,
    MAP_ENTITY_SAFE,
    stable_command_key,
    valid_materialization_epoch,
)
from doom_eap.contracts.foundation import load_foundation_contracts
from doom_eap.contracts.goal_policy import GoalPolicy
from doom_eap.contracts.publisher_contracts import PublisherEngine
from doom_eap.contracts.runtime_context import canonical_map_name
from doom_eap.runtime import bridge_config, bridge_saves
from doom_eap.runtime.campaign import STAGES
from doom_eap.runtime.deathlink import DeathLinkPublication
from doom_eap.runtime.inventory import ReconciliationBlock
from doom_eap.runtime.native_transport import StaleNativeResult
from doom_eap.runtime.observers import (
    CONTEXT_BY_MAP,
    GOAL_CAPABILITIES,
    RuntimeLifecycle,
    ignore,
    quarantine_malformed_event,
    read_map_event,
)

RUNTIME_LOCATIONS_FILE = bridge_config.REPO_ROOT / "data" / "runtime_locations.json"


class CheckPublication:
    def __init__(self, send, mark_submitted, goal_status, observer=ignore):
        self._send, self._mark_submitted, self._goal_status = send, mark_submitted, goal_status
        self._observe = observer

    async def location(self, location_id):
        self._observe("check_send_prepare", locations=(location_id,))
        try:
            await self._send([{"cmd": "LocationChecks", "locations": [location_id]}])
        except (OSError, RuntimeError, WebSocketException):
            self._observe("check_send_failed", locations=(location_id,))
            raise
        self._observe("check_sent", locations=(location_id,))

    async def locations(self, location_ids):
        self._observe("check_send_prepare", locations=location_ids)
        try:
            await self._send([{"cmd": "LocationChecks", "locations": list(location_ids)}])
        except (OSError, RuntimeError, WebSocketException):
            self._observe("check_send_failed", locations=location_ids)
            raise
        self._observe("check_sent", locations=location_ids)

    async def scout(self, location_ids):
        await self._send([{"cmd": "LocationScouts", "locations": list(location_ids), "create_as_hint": 0}])

    async def goal(self):
        await self._send([{"cmd": "StatusUpdate", "status": self._goal_status}])

    def mark_submitted(self, location_id):
        self._mark_submitted(location_id)


def telemetry_dump_files():
    files = set()
    for prefix in (bridge_config.TELEMETRY_DUMP_PREFIX, bridge_config.LEGACY_TELEMETRY_DUMP_PREFIX):
        files.update(
            glob.glob(os.path.join(bridge_config.INV_DUMP_DIR, f"{prefix}*.txt"))
        )
    return sorted(files)


def ammo_refill_request_files(directory=None):
    """Return only DoomEAP refill request files in safe creation order."""
    root = directory if directory is not None else globals().get("SAVE_GAMES_DIR")
    if not root:
        return []
    candidates = glob.glob(os.path.join(str(root), "AP_REFILL_REQUEST*.txt"))
    valid_files = []
    for path in set(candidates):
        match = bridge_config.AMMO_REFILL_REQUEST_RE.fullmatch(os.path.basename(path))
        if match is None:
            continue
        try:
            mtime_ns = os.stat(path).st_mtime_ns
        except OSError:
            continue
        suffix = int(match.group(1) or 0)
        valid_files.append((mtime_ns, suffix, os.path.basename(path), path))
    valid_files.sort()
    return [path for _, _, _, path in valid_files]


def cleanup_ammo_refill_request_files(directory=None):
    """Remove only stale DoomEAP Ammo Refill request files."""
    removed = 0
    for path in ammo_refill_request_files(directory):
        try:
            os.remove(path)
        except FileNotFoundError:
            continue
        except OSError as error:
            bridge_config.logger.warning("[Ammo Refill] Could not remove stale request %s: %s", path, error)
        else:
            removed += 1
    return removed


def check_event_files():
    return sorted(glob.glob(os.path.join(bridge_config.INV_DUMP_DIR, f"{bridge_config.CHECK_EVENT_PREFIX}*.txt")))


def goal_event_files():
    return sorted(glob.glob(os.path.join(bridge_config.DOOM_BASE_DIR, f"{bridge_config.GOAL_EVENT_PREFIX}*.evt")))


def extract_location_id_from_event(path):
    basename = os.path.basename(path)
    filename_match = re.match(
        rf"^{bridge_config.CHECK_EVENT_PREFIX}(\d+)(?:_.*)?\.txt$",
        basename,
    )
    if filename_match:
        return int(filename_match.group(1))

    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            contents = f.read()
    except OSError:
        return None

    content_match = re.search(r"AP_CHECK_EVENT_(\d+)", contents)
    if content_match:
        return int(content_match.group(1))
    return None


def quarantine_event_file(path, old_state_key=None, new_state_key=None, reason="session_changed"):
    path = Path(path)
    if not path.exists():
        return
    quarantine_base = Path(bridge_config.INV_DUMP_DIR) / "ap_event_quarantine"
    timestamp_folder = time.strftime("%Y%m%d_%H%M%S")
    dest_dir = quarantine_base / timestamp_folder
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_path = dest_dir / path.name
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    location_id = extract_location_id_from_event(path)

    meta = {
        "filename": path.name,
        "parsed_location_id": location_id,
        "mtime_ns": mtime_ns,
        "old_state_key": old_state_key,
        "new_state_key": new_state_key,
        "reason": reason,
        "quarantined_at": time.time(),
    }

    try:
        shutil.move(str(path), str(dest_path))
        meta_path = dest_dir / f"{path.name}.meta.json"
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        bridge_config.logger.warning(
            "[Quarantine] Quarantined event file %s -> %s (reason: %s, location_id: %s)",
            path.name, dest_path, reason, location_id,
        )
    except OSError as err:
        bridge_config.logger.error("[Quarantine] Failed to quarantine event file %s: %s", path.name, err)


def discover_active_map_markers():
    """Discover all suffixed map start identity files in INV_DUMP_DIR, ordered by mtime."""
    patterns = [
        os.path.join(bridge_config.INV_DUMP_DIR, f"{ACTIVE_MAP_MARKER_PREFIX}*.txt"),
        os.path.join(bridge_config.INV_DUMP_DIR, f"{bridge_config.TELEMETRY_DUMP_PREFIX}*.txt"),
    ]
    valid_files = []
    seen_paths = set()
    for pattern in patterns:
        for path in glob.glob(pattern):
            if path in seen_paths:
                continue
            seen_paths.add(path)
            basename = os.path.basename(path)
            if (
                re.match(rf"^{ACTIVE_MAP_MARKER_PREFIX}(?:_.*)?\.txt$", basename)
                or re.match(rf"^{bridge_config.TELEMETRY_DUMP_PREFIX}(?:_.*)?\.txt$", basename)
            ):
                try:
                    st = os.stat(path)
                    valid_files.append((st.st_mtime_ns, path))
                except OSError:
                    pass
    valid_files.sort(key=lambda item: item[0])
    return valid_files


def parse_active_map_marker(path, mtime_ns):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except OSError:
        return None

    return RuntimeLifecycle.parse_authored_marker(
        content, path, mtime_ns, MATERIALIZATION_MAPS, CONTEXT_BY_MAP,
    )


def cleanup_active_map_marker_file(path):
    """Remove a consumed or stale active-map condump file."""
    if not path:
        return False
    basename = os.path.basename(path)
    if not re.match(rf"^{ACTIVE_MAP_MARKER_PREFIX}(?:_.*)?\.txt$", basename):
        return False
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError as error:
        bridge_config.logger.warning(
            f"[MAP] Could not remove active map marker {basename}: {error}"
        )
        return False


def cleanup_active_map_markers(preserve_path=None):
    """Remove consumed or stale active-map condump files to prevent progressive suffixed names."""
    pattern = os.path.join(bridge_config.INV_DUMP_DIR, f"{ACTIVE_MAP_MARKER_PREFIX}*.txt")
    for path in glob.glob(pattern):
        if preserve_path and os.path.abspath(path) == os.path.abspath(preserve_path):
            continue
        basename = os.path.basename(path)
        if not re.match(rf"^{ACTIVE_MAP_MARKER_PREFIX}(?:_.*)?\.txt$", basename):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                content = f.read(256)
            if "AP_ACTIVE_MAP_V1" not in content and "echo AP_ACTIVE_MAP_V1" not in content:
                continue
        except OSError:
            continue
        cleanup_active_map_marker_file(path)


def discover_telemetry_markers():
    """Discover all suffixed telemetry marker files in INV_DUMP_DIR, ordered by mtime."""
    pattern = os.path.join(bridge_config.INV_DUMP_DIR, f"{bridge_config.TELEMETRY_DUMP_PREFIX}*.txt")
    valid_files = []
    for path in glob.glob(pattern):
        basename = os.path.basename(path)
        if re.match(rf"^{bridge_config.TELEMETRY_DUMP_PREFIX}(?:_.*)?\.txt$", basename):
            try:
                st = os.stat(path)
                valid_files.append((st.st_mtime_ns, path))
            except OSError:
                pass
    valid_files.sort(key=lambda item: item[0])
    return valid_files


def parse_goal_transition_event(path, include_raw=False):
    data = {}
    with open(path, encoding="utf-8", errors="ignore") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key] = value

    if not data.get("from_map") or not data.get("to_map"):
        return None
    raw_from_map = data["from_map"]
    raw_to_map = data["to_map"]
    data["from_map"] = canonical_map_name(data["from_map"])
    data["to_map"] = canonical_map_name(data["to_map"])
    if include_raw:
        data["raw_from_map"] = raw_from_map
        data["raw_to_map"] = raw_to_map
    return data


def cleanup_telemetry_dumps():
    """Remove completed telemetry files before DOOM chooses a suffixed name."""
    removed_all = True
    for path in telemetry_dump_files():
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as error:
            removed_all = False
            bridge_config.logger.warning(
                f"[Telemetry] Could not remove {os.path.basename(path)} yet: "
                f"{error}"
            )
    return removed_all


class CheckRuntime:
    def __init__(self, context):
        self.ctx = context

    async def request_support_condump(self):
        link = await self.ctx.items._native_game_link_async()
        result = await self.ctx.items._native_call(link, "request", 52)
        return result.get("outcome") == 1

    async def queue_received_deathlink(self):
        try:
            ready = (not self.ctx.save_observer.readiness.frozen and self.ctx.saves.has_authoritative_save_proof()
                     and (await self.ctx.items.runtime_effects_ready()))
            if not self.ctx.deathlink.receiving:
                return
            identity = await self.ctx.items._query_process_identity()
            link = self.ctx.items._native_game_link_for_identity(identity) if identity else None
            session = copy.copy(self.ctx.deathlink)
            before = copy.deepcopy(self.ctx.deathlink._receiver.__dict__)
            session._receiver = copy.deepcopy(self.ctx.deathlink._receiver)
            def require_link():
                if link is None:
                    raise RuntimeError("Sentinel requires a qualified Windows game process")
                return link
            publication = DeathLinkPublication(require_link, session.active_event, session.mode)
            await self.ctx.items._blocking_native_worker().call(
                session.advance, ready, publication, process=identity)
            if self.ctx.deathlink._receiver.__dict__ == before:
                self.ctx.deathlink._receiver.__dict__.update(session._receiver.__dict__)
        except RuntimeError as error:
            bridge_config.logger.info("[DeathLink] Native receive pending: %s", error)

    async def check_game_duration_death(self):
        selected = (await self.ctx.saves.update_save_slot_lifecycle())
        if not selected:
            # The durable-save path is available but deliberately frozen until
            # native gameplay evidence promotes a slot. Do not fall back to a
            # newest-mtime game.details reader while in menus.
            return True
        path = selected.path
        if self.ctx.save_observer.duration_is_current(selected):
            return True

        receipt_token = self.ctx.receipt_session.capture(self.ctx.state_key)
        observation_token = self.ctx.save_observer.capture_observation()

        def observation_current():
            return (
                self.ctx.receipt_session.is_current(receipt_token, self.ctx.state_key)
                and self.ctx.save_observer.observation_is_current(observation_token)
            )

        try:
            snapshot = await asyncio.to_thread(bridge_saves.probe_game_duration, path)
        except (OSError, RuntimeError, ValueError, WebSocketException) as error:
            if not observation_current():
                return True
            self.ctx.death_observer.probe_failed(error)
            return False

        current = await self.ctx.saves.admitted_campaign_save()
        if not observation_current() or current != selected:
            return True
        self.ctx.death_observer.probe_succeeded()
        self.ctx.save_observer.accept_duration(selected)
        self.ctx.saves.observe_weapon_masteries(snapshot["mastery_records"], selected)
        self.ctx.saves.observe_mission_challenges(
            snapshot["mission_challenge_records"], selected
        )
        marker = (await self.read_active_map_identity())
        event_identity = self.ctx.death_observer.observe_checkpoint(
            snapshot, selected, self.ctx.save_observer.readiness.load_epoch,
            marker["runtime_map"] if marker else "unknown",
        )
        if event_identity is None:
            return True
        self.ctx.items.reset_transient_effects("death_boundary")
        bridge_config.logger.info(
            "[DeathLink] LOCAL_DEATH_OBSERVED event_identity=%s",
            event_identity,
        )
        if not self.ctx.deathlink.enabled:
            bridge_config.logger.info(
                "[DeathLink] DEATHLINK_OUTBOUND_SUPPRESSED reason=death_link_disabled"
            )
            return True

        await self.report_local_death()
        return True

    async def check_game_details_death(self):
        details = (await self.ctx.saves.observe_active_game_details())
        if self.ctx.death_observer.observe_details(details):
            bridge_config.logger.info("[DeathLink] LOCAL_DEATH_OBSERVED source=game.details path=%s", details.get("_path"))
            await self.report_local_death()

    async def report_local_death(self):
        from types import SimpleNamespace
        event, mode = self.ctx.deathlink.active_event, self.ctx.deathlink.mode
        cancellations = []
        publication = SimpleNamespace(cancel=cancellations.append)
        await self.ctx.deathlink.report_local_death(self.ctx.auth, self.ctx.send_death, publication,
            grace=bridge_config.load_config().get("death_link_grace", 0), persist=self.ctx.persist_session_state)
        for state_key in cancellations:
            try:
                link = await self.ctx.items._native_game_link_async()
                publication = DeathLinkPublication(lambda link=link: link, event, mode)
                await self.ctx.items._blocking_native_worker().call(
                    publication.cancel, state_key, process=link._process_identity)
            except (RuntimeError, OSError) as error:
                bridge_config.logger.warning("[DeathLink] Native cancellation unavailable: %s", error)

    def check_observation(self):
        return CheckObservation(
            frozenset(getattr(self.ctx, "checked_locations", None) or ()),
            frozenset(self.ctx.locations_checked), frozenset(self.ctx.server_locations),
            bool(getattr(self.ctx, "server_checked_locations_ready", False)),
            bool(self.ctx.server and self.ctx.server.socket and not self.ctx.server.socket.closed),
        )

    def check_publication(self):
        from NetUtils import ClientStatus
        return CheckPublication(self.ctx.send_msgs, self.ctx.locations_checked.add, ClientStatus.CLIENT_GOAL)

    async def evaluate_campaign_goal(self, source_description):
        campaign = getattr(self.ctx, "unified_campaign", None)
        admitted = campaign.goal_admitted(self.ctx.checked_locations, self.ctx.items_received) if campaign else None
        admission_missing = tuple(
            key for key in campaign.plan["sequence"]
            if key != campaign.plan["goal_stage"] and STAGES[key]["completion_id"] not in self.ctx.checked_locations
        ) if campaign else ()
        observation = self.check_observation()
        slot_data = getattr(self.ctx, "_connected_slot_data", {})
        threshold_ids, threshold = GOAL_POLICY.threshold(slot_data)
        return await self.ctx.goals.evaluate(
            source_description,
            GOAL_POLICY.objective_ids(slot_data, observation.server_locations),
            observation, self.check_publication(),
            admitted=admitted, admission_missing=admission_missing,
            threshold_ids=threshold_ids, threshold=threshold,
        )

    async def execute_publisher(self, publisher, trigger_strategy, source_description):
        self.ctx.publisher_dispatch.observe_protocol(self.check_observation())
        return await self.ctx.publisher_dispatch.execute(
            publisher, trigger_strategy, source_description,
            self.check_publication(), self.ctx.persist_session_state,
        )

    async def send_mission_complete(self, location_id, source_description, report_goal=False):
        if report_goal:
            return await self.evaluate_campaign_goal(source_description
        )
        self.ctx.publisher_dispatch.observe_protocol(self.check_observation())
        return await self.ctx.publisher_dispatch.mission(
            location_id, source_description, self.check_publication(), self.ctx.persist_session_state,
        )

    async def check_campaign_goal_event(self):
        """Consume independent map files and native transition triggers."""
        if not (await self.ctx.items.check_and_update_event_session()):
            return False
        receipt_token = self.ctx.receipt_session.capture(self.ctx.state_key)
        observed = False
        quarantine_root = Path(bridge_config.INV_DUMP_DIR) / "quarantine"
        for trigger_key, publishers in PUBLISHER_ENGINE.publishers_by_trigger.items():
            observed = await self._consume_publisher_map_event(trigger_key, publishers, receipt_token, quarantine_root) or observed
            if not self.ctx.receipt_session.is_current(receipt_token, self.ctx.state_key):
                return observed

        for path in goal_event_files():
            observed = True
            await self._consume_transition_event(path, receipt_token, quarantine_root)
            if not self.ctx.receipt_session.is_current(receipt_token, self.ctx.state_key):
                return observed
        return observed

    async def _publish_final_sin_candidate(self):
        token = self.ctx.goals.candidate_token()
        publisher = next(item for item in PUBLISHERS if item.key == "final_sin_mission_complete")
        published = await self.execute_publisher(publisher, "save_fallback", "Final Sin Mission Select completed edge",
        )
        if published:
            self.ctx.goals.acknowledge_candidate(token)
        return published

    async def evaluate_final_sin_completion_candidate(self):
        candidate = self.ctx.goals.candidate
        if candidate is None:
            return False
        if not self.ctx.goals.candidate_slot_allowed(self.ctx.save_observer.readiness.authoritative, self.ctx.save_observer.readiness.slot):
            return False
        selected = (await self.ctx.saves.admitted_campaign_save())
        if selected and selected.slot_directory != candidate["slot"]:
            selected = None
        details = bridge_saves.read_game_details_for_selection(selected, bridge_config.STEAM_ID3, bridge_config.logger) if selected else None
        decision = self.ctx.goals.observe_candidate(selected, details)
        if decision == "publish":
            return await self._publish_final_sin_candidate()
        if decision == "pending":
            lease = getattr(self.ctx, "runtime_observation_lease", None)
            if lease is not None and not lease.process_probe():
                self.ctx.goals.clear_candidate("game_process_ended")
        return False

    async def check_campaign_goal_save_fallback(self):
        receipt_token = self.ctx.receipt_session.capture(self.ctx.state_key)
        if await self.evaluate_final_sin_completion_candidate():
            return
        if not self.ctx.receipt_session.is_current(receipt_token, self.ctx.state_key):
            return
        marker = (await self.read_active_map_identity(evidence=bridge_saves.read_gameplay_save_evidence()))
        active_map = canonical_map_name(marker["runtime_map"]) if marker else ""
        if not active_map:
            return
        details = (await self.ctx.saves.observe_active_game_details())
        if not details:
            return
        intent = self.ctx.goals.observe_completion(details, self.ctx.persist_session_state)
        if intent is not None:
            key, source = intent
            for publisher in (item for item in PUBLISHERS if item.key == key):
                await self.execute_publisher(publisher, "save_fallback", source)

    async def check_campaign_goal(self):
        receipt_token = self.ctx.receipt_session.capture(self.ctx.state_key)
        if not self.ctx.item_state_ready:
            await self.check_campaign_goal_event()
            return

        await self.check_campaign_goal_event()
        if not self.ctx.receipt_session.is_current(receipt_token, self.ctx.state_key):
            return
        await self.check_campaign_goal_save_fallback()
        if not self.ctx.receipt_session.is_current(receipt_token, self.ctx.state_key):
            return
        await self.evaluate_campaign_goal("central objective evaluator")

    async def check_rpc_autopause(self):
        evidence = bridge_saves.read_gameplay_save_evidence()
        marker = (await self.read_active_map_identity(evidence=evidence))

        if marker is None:
            self.ctx.bootstrap.observe_supported_map(None)
            self.ctx.runtime_lifecycle.project_current_map(None)
            return
        map_name = marker["runtime_map"]

        self.ctx.runtime_lifecycle.project_current_map(marker)
        if getattr(evidence, "state", None) != "gameplay":
            return
        self.snapshot_fast_travel_eligibility()
        (await self.reconcile_fast_travel_unlock("map_ready"))
        if self.ctx.bootstrap.observe_supported_map(map_name):
            await self.ctx.items.onboard_bootstrap("on_supported_map_load")

    async def _present_killsanity(self, tracker, link):
        try:
            await tracker.present(link, getattr(self.ctx, "killsanity_counter_visible", True),
                                  run=lambda method, *args: self.ctx.items._native_call(link, "publish_combat_progress", *args))
            self.ctx._killsanity_presentation_error = None
        except StaleNativeResult:
            return
        except (OSError, RuntimeError, ValueError, WebSocketException) as error:
            signature = str(error)
            if signature != getattr(self.ctx, "_killsanity_presentation_error", None):
                bridge_config.logger.warning("[Killsanity] Presentation pending: %s", error)
                self.ctx._killsanity_presentation_error = signature
        return True

    async def _observe_killsanity(self, game_running, tracker, identity):
        link = None
        if game_running:
            link = (await self.ctx.items._native_game_link_async())
            if link.namespace != tracker.namespace:
                return link, False
            snapshot = await self.ctx.items._native_call(link, "observe_combat", set(CONTEXT_BY_MAP))
            if identity != (self.ctx.state_key, id(self.ctx.session_state), tracker.namespace, tracker._epoch):
                return link, False
            tracker.consume(snapshot, self.ctx.persist_session_state)
        return link, True

    async def _publish_killsanity(self, game_running, link, tracker, identity):
        if game_running and link is not None:
            if not await self._present_killsanity(tracker, link):
                return
        if identity != (self.ctx.state_key, id(self.ctx.session_state), tracker.namespace, tracker._epoch):
            return
        try:
            await tracker.publish(self.check_observation(), self.check_publication())
        except (OSError, RuntimeError, ValueError, WebSocketException) as error:
            bridge_config.logger.warning("[Killsanity] Milestone publication pending: %s", error)

    async def check_killsanity(self, game_running):
        tracker = self.ctx.killsanity
        if not tracker.enabled or not self.ctx.item_state_ready:
            return
        identity = (self.ctx.state_key, id(self.ctx.session_state), tracker.namespace, tracker._epoch)
        link = None
        try:
            link, current = await self._observe_killsanity(game_running, tracker, identity)
            if not current:
                return
            self.ctx._killsanity_observation_error = None
        except StaleNativeResult:
            return
        except (OSError, RuntimeError, ValueError, WebSocketException) as error:
            signature = str(error)
            if signature != getattr(self.ctx, "_killsanity_observation_error", None):
                bridge_config.logger.warning("[Killsanity] Observation pending: %s", error)
                self.ctx._killsanity_observation_error = signature
        if identity != (self.ctx.state_key, id(self.ctx.session_state), tracker.namespace, tracker._epoch):
            return
        await self._publish_killsanity(game_running, link, tracker, identity)

    async def prime_death_baseline(self):
        if not getattr(self.ctx, '_death_baseline_pending', False):
            return
        token = self.ctx.receipt_session.capture(self.ctx.state_key)
        try:
            root = await self.ctx.saves._admitted_campaign_root()
            if root is None or not self.ctx.receipt_session.is_current(token, self.ctx.state_key):
                return
            path = bridge_config.STEAM_REMOTE_DIR / root / 'GAME-AUTOSAVE0' / 'game_duration.dat'
            try:
                path.stat()
            except FileNotFoundError:
                self.ctx.death_observer.start_empty_slot('GAME-AUTOSAVE0')
                self.ctx._death_baseline_pending = False
                return
            selected = bridge_saves.admitted_ap_save(bridge_config.STEAM_REMOTE_DIR, root)
            if selected is None:
                return
            snapshot = await asyncio.to_thread(bridge_saves.probe_game_duration, selected.path)
            current = await self.ctx.saves.admitted_campaign_save()
            if (current != selected
                    or not self.ctx.receipt_session.is_current(token, self.ctx.state_key)):
                return
            # this read establishes history; live save proof still controls death publication.
            self.ctx.death_observer.observe_checkpoint(snapshot, selected, 0, 'session_baseline')
            self.ctx._death_baseline_pending = False
        except (OSError, RuntimeError, ValueError, WebSocketException) as error:
            if str(error) != getattr(self.ctx, '_death_baseline_error', None):
                bridge_config.logger.warning('[DeathLink] Initial save baseline pending: %s', error)
                self.ctx._death_baseline_error = str(error)

    def _map_event_trigger(self, publishers, filename):
        trigger = next(
            item
            for publisher in publishers
            for item in publisher.triggers_for("map_event_file")
            if item["filename"] == filename
        )
        return trigger

    async def _consume_publisher_map_event(self, trigger_key, publishers, receipt_token, quarantine_root):
        observed = False
        if trigger_key[0] != "map_event_file":
            return observed
        filename = trigger_key[1]
        trigger = self._map_event_trigger(publishers, filename)
        path = Path(bridge_config.INV_DUMP_DIR) / filename
        if not path.exists():
            return observed
        observed = True
        try:
            valid, contents, digest = read_map_event(path, trigger["marker"])
        except OSError as error:
            bridge_config.logger.warning("[PUBLISHER] EVENT_READ_RETRY filename=%s error=%s", filename, error)
            return observed
        if not valid:
            quarantine_malformed_event(
                path,
                key=",".join(publisher.key for publisher in publishers),
                contents=contents,
                sha256=digest,
                quarantine_root=quarantine_root,
            )
            bridge_config.logger.warning(
                "[PUBLISHER] EVENT_MALFORMED key=%s filename=%s sha256=%s",
                ",".join(publisher.key for publisher in publishers),
                filename,
                digest,
            )
            return observed
        completed = []
        for publisher in publishers:
            completed.append(await self.execute_publisher(publisher,
                "map_event_file",
                f"map event {filename}",
            ))
            if not self.ctx.receipt_session.is_current(receipt_token, self.ctx.state_key):
                return observed
        if all(completed):
            self._cleanup_map_event(path, publishers, filename)
        return observed

    async def _consume_transition_event(self, path, receipt_token, quarantine_root):
        try:
            event = parse_goal_transition_event(path, include_raw=True)
        except OSError as error:
            bridge_config.logger.warning("[PUBLISHER] EVENT_READ_RETRY filename=%s error=%s", os.path.basename(path), error)
            return
        if event is None:
            self._quarantine_transition(path, quarantine_root)
            return
        matching = PUBLISHER_ENGINE.observe("native_transition", event)
        if not matching:
            bridge_config.logger.info(
                "[PUBLISHER] TRANSITION_IGNORED from=%s to=%s reason=no_contract",
                event["from_map"],
                event["to_map"],
            )
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            return
        completed = []
        for publisher in matching:
            completed.append(
                await self.execute_publisher(publisher,
                    "native_transition",
                    f"native transition {event['from_map']} -> {event['to_map']}",
                )
            )
            if not self.ctx.receipt_session.is_current(receipt_token, self.ctx.state_key):
                return
        if all(completed):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as error:
                bridge_config.logger.warning(
                    "[PUBLISHER] EVENT_CLEANUP_RETRY key=native_transition "
                    "filename=%s error=%s",
                    os.path.basename(path),
                    error,
                )

    def _cleanup_map_event(self, path, publishers, filename):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            bridge_config.logger.warning(
                "[PUBLISHER] EVENT_CLEANUP_RETRY key=%s filename=%s error=%s",
                ",".join(publisher.key for publisher in publishers),
                filename,
                error,
            )

    def _quarantine_transition(self, path, quarantine_root):
        try:
            raw = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            bridge_config.logger.warning("[PUBLISHER] EVENT_READ_RETRY filename=%s error=%s", os.path.basename(path), error)
            return
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        quarantine_malformed_event(
            Path(path),
            key="native_transition",
            contents=raw,
            sha256=digest,
            quarantine_root=quarantine_root,
        )
        bridge_config.logger.warning(
            "[PUBLISHER] EVENT_MALFORMED key=native_transition filename=%s sha256=%s",
            os.path.basename(path),
            digest,
        )
        return

    async def death_monitor_loop(self):
        while not self.ctx.exit_event.is_set():
            if not self.ctx.item_state_ready:
                await asyncio.sleep(0.25)
                continue
            await self.prime_death_baseline()
            (await self.check_rpc_autopause())
            (await self.queue_received_deathlink())
            game_running = bool(await self.ctx.items._query_process_identity())
            await self.check_killsanity(game_running)
            if not game_running:
                await asyncio.sleep(1.0)
                continue
            await self._poll_death_and_save_checks()

    def _physical_event_paths(self):
        event_paths_by_location = {}
        unknown_event_paths = []
        for path in check_event_files():
            if os.path.basename(path) in PUBLISHER_MAP_EVENT_FILENAMES:
                continue
            location_id = extract_location_id_from_event(path)
            if location_id is None:
                unknown_event_paths.append(path)
                continue
            event_paths_by_location.setdefault(location_id, []).append(path)
        return event_paths_by_location, unknown_event_paths

    async def flush_check_event_files(self):
        get_key = getattr(self.ctx, "get_ap_state_key", None)
        state_key = get_key() if get_key else None
        if not state_key or not (await self.ctx.items.check_and_update_event_session()):
            return

        event_paths_by_location, unknown_event_paths = self._physical_event_paths()

        for path in unknown_event_paths:
            bridge_config.logger.warning(
                "[Trigger] Could not identify AP event location from "
                f"{os.path.basename(path)}; leaving file in place."
            )

        facts = self.check_observation()
        if not _record_physical_gates(self.ctx, event_paths_by_location, facts):
            return
        pending_locations = []
        for location_id, paths in event_paths_by_location.items():
            if self._observe_physical_event(location_id, paths, facts, state_key):
                pending_locations.append(location_id)

        await self.ctx.physical_checks.publish(pending_locations, self.check_publication())

    async def physical_check_loop(self):
        while not self.ctx.exit_event.is_set():
            try:
                if self.ctx.item_state_ready and self.ctx.server and self.ctx.server.socket and not self.ctx.server.socket.closed:
                    await self.flush_check_event_files()
            except StaleNativeResult:
                continue
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError, ValueError, WebSocketException):
                bridge_config.logger.exception("[Trigger] Check publication interrupted; retrying")
                await asyncio.sleep(1.0)
            await asyncio.sleep(0.1)

    async def _ingest_tracker_markers(self, evidence, markers):
        newest_path = None
        if markers:
            _, newest_path = markers[-1]
            try:
                (await self.ingest_visible_runtime_lifecycle(
                    evidence=evidence,
                    lifecycle_markers=markers,
                ))
            except StaleNativeResult:
                return False, newest_path
            except (OSError, RuntimeError, ValueError, WebSocketException) as exc:
                bridge_config.logger.error(
                    "[RPC] Auto-RPC failed to consume telemetry ready file %s: %s",
                    newest_path,
                    exc,
                )
            for _mtime, path in markers:
                try:
                    os.remove(path)
                except OSError:
                    pass
        return True, newest_path

    async def tracker_loop(self):
        bridge_config.logger.info(
            "[Tracking] Starting Doom Eternal runtime tracking loop "
            "(polling every 4 seconds)."
        )
        bridge_config.logger.info(
            "[RPC] Auto-RPC waits for telemetry-ready, then the native memory "
            "gate permits execution only in safe gameplay. Check delivery prefers "
            "native ap_event files over telemetry polls."
        )
        self.ctx.last_heartbeat_timestamp = time.time()
        self.ctx.heartbeat_iteration_count = 0

        while not self.ctx.exit_event.is_set():
            self.ctx.last_heartbeat_timestamp = time.time()
            if not self.ctx.item_state_ready:
                await asyncio.sleep(0.25)
                continue
            if not (await self.ctx.items._query_process_identity()):
                self.invalidate_map_identity("game_not_running")
                await asyncio.sleep(4.0)
                continue
            self.ctx.heartbeat_iteration_count += 1
            if self.ctx.heartbeat_iteration_count % 15 == 0:
                bridge_config.logger.info(
                    "[Tracking] TRACKER_HEARTBEAT active_slot=%s map=%s items_processed=%d/%d",
                    self.ctx.save_observer.selection.slot or "<none>",
                    self.ctx.runtime_lifecycle.map_identity.current_map or "<none>",
                    self.ctx.items.items_processed,
                    len(self.ctx.items_received),
                )

                (await self.ctx.items._publish_owner_ammo_refill())
            if self.ctx.server and self.ctx.server.socket and not self.ctx.server.socket.closed:
                await self._poll_connected_tracker()

            await asyncio.sleep(4.0)

    async def death_monitor_supervisor(self):
        while not self.ctx.exit_event.is_set():
            try:
                await self.death_monitor_loop()
                return
            except StaleNativeResult:
                continue
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError, ValueError, WebSocketException):
                bridge_config.logger.exception("[DeathLink] Monitor interrupted; retrying")
                await asyncio.sleep(1.0)

    async def tracker_supervisor(self):
        bridge_config.logger.info("[Supervisor] TRACKER_STARTED")
        self.ctx.tracker_alive = True
        self.ctx.tracker_restart_count = getattr(self.ctx, "tracker_restart_count", 0)
        self.ctx.last_tracker_error = getattr(self.ctx, "last_tracker_error", None)
        self.ctx.last_heartbeat_timestamp = time.time()
        self.ctx.tracker_backoff = getattr(self.ctx, "tracker_backoff", 1.0)
        self.ctx.last_error_fingerprint = getattr(self.ctx, "last_error_fingerprint", None)
        self.ctx.consecutive_same_error_count = getattr(self.ctx, "consecutive_same_error_count", 0)
        self.ctx.tracker_degraded = getattr(self.ctx, "tracker_degraded", False)

        while not self.ctx.exit_event.is_set():
            try:
                await self.tracker_loop()
                break
            except StaleNativeResult:
                continue
            except asyncio.CancelledError:
                bridge_config.logger.info("[Supervisor] TRACKER_STOPPED (clean shutdown)")
                self.ctx.tracker_alive = False
                raise
            except (OSError, RuntimeError, ValueError, WebSocketException) as exc:
                self.ctx.tracker_restart_count += 1
                tb = traceback.format_exc()
                fingerprint = _tracker_error_fingerprint(exc)
                if fingerprint == self.ctx.last_error_fingerprint:
                    self.ctx.consecutive_same_error_count += 1
                    self.ctx.tracker_backoff = min(30.0, self.ctx.tracker_backoff * 2.0)
                else:
                    self.ctx.last_error_fingerprint = fingerprint
                    self.ctx.consecutive_same_error_count = 1
                    self.ctx.tracker_backoff = 1.0

                self.ctx.last_tracker_error = f"{type(exc).__name__}: {exc}"
                self.ctx.tracker_degraded = True
                if self.ctx.consecutive_same_error_count <= 2:
                    bridge_config.logger.error(
                        "[Supervisor] TRACKER_CRASH type=%s msg=%s ap_state_key=%s "
                        "current_map=%s active_save_slot=%s last_processed_event=%s "
                        "last_heartbeat_age=%.1fs traceback:\n%s",
                        type(exc).__name__,
                        str(exc),
                        self.ctx.get_ap_state_key(),
                        self.ctx.runtime_lifecycle.map_identity.current_map,
                        self.ctx.save_observer.selection.slot,
                        getattr(self.ctx.physical_checks, "last_event", None),
                        time.time() - (self.ctx.last_heartbeat_timestamp or time.time()),
                        tb,
                    )
                bridge_config.logger.info(
                    "[Supervisor] TRACKER_RESTART count=%d backoff=%.1fs consecutive_errors=%d fingerprint=%s",
                    self.ctx.tracker_restart_count,
                    self.ctx.tracker_backoff,
                    self.ctx.consecutive_same_error_count,
                    fingerprint,
                )
                await asyncio.sleep(self.ctx.tracker_backoff)
        self.ctx.tracker_alive = False

    async def _poll_death_and_save_checks(self):
        work_token = self.ctx.runtime_lifecycle.capture_work()
        used_duration = False
        if not bridge_config.DEATH_PROBE.is_file() and not getattr(self.ctx, "_save_probe_missing_logged", False):
            bridge_config.logger.warning("[Challenge] SAVE_PROBE_UNAVAILABLE reason=helper_missing path=%s", bridge_config.DEATH_PROBE)
            self.ctx._save_probe_missing_logged = True
        if bridge_saves.death_probe_available():
            used_duration = await self.check_game_duration_death()
        if not self._tracking_work_current(work_token):
            return
        if not used_duration:
            await self.check_game_details_death()
        if not self._tracking_work_current(work_token):
            return
        await self.ctx.saves.check_weapon_mastery_locations()
        if not self._tracking_work_current(work_token):
            return
        await self.ctx.saves.check_mission_challenge_locations()
        if not self._tracking_work_current(work_token):
            return
        self.ctx.items._consume_ammo_refill_request_file()
        sleep_duration = 0.05 if self.ctx.deathlink.receiving else 1.0
        await asyncio.sleep(sleep_duration)

    def _tracking_work_current(self, token):
        return self.ctx.runtime_lifecycle.work_is_current(token) and not self.ctx.exit_event.is_set()

    def _observe_physical_event(self, location_id, paths, facts, state_key):
        disposition = self.ctx.physical_checks.disposition(location_id, facts, self.ctx.item_state_ready)
        if disposition == "acknowledged":
            for path in paths:
                _remove_acknowledged_event(path)
            return False
        if disposition == "quarantine":
            bridge_config.logger.warning(
                "[Trigger] AP event location %s not in connected slot; quarantining.",
                location_id,
            )
            for path in paths:
                quarantine_event_file(
                    path,
                    old_state_key=state_key,
                    new_state_key=state_key,
                    reason="location_not_in_connected_slot",
                )
            return False
        if disposition == "outside_slot":
            bridge_config.logger.warning(
                "[Trigger] AP event location %s is not part of the connected slot; leaving file in place.",
                location_id,
            )
            return False
        if disposition == "submit":
            self.record_local_automap_cleanup_ownership(location_id, paths)
            return True
        return False

    async def _track_level_progress(self, newest_path):
        work_token = self.ctx.runtime_lifecycle.capture_work()
        level_ready = await self.process_level_ready(newest_path)
        if not self._tracking_work_current(work_token):
            return
        if not level_ready:
            await self.ctx.saves.check_mission_challenge_locations()
            if not self._tracking_work_current(work_token):
                return
            (await self.reconcile_fast_travel_unlock("readiness"))
        await self.ctx.items.process_pending_item_receipts("tracker")
        if not self._tracking_work_current(work_token):
            return

        await self.check_campaign_goal()
        await self.ctx.saves.synchronize_campaign_menu()

    async def _poll_connected_tracker(self):
        newest_path = None
        try:
            await self.ctx.saves.checkpoint_recovery_ready()
            evidence = bridge_saves.read_gameplay_save_evidence()
            if getattr(evidence, "state", None) == "not_running":
                self.ctx.items.reset_transient_effects("game_exit")
            else:
                await self.ctx.transient_effect_manager.tick(self.ctx.items._observe_transient_runtime())
            markers = discover_telemetry_markers()
            ingested, newest_path = await self._ingest_tracker_markers(evidence, markers)
            if not ingested:
                return
            self.ctx.items._refresh_runtime_context(
                getattr(self.ctx, "_connected_slot_data", {}), evidence
            )
            if getattr(evidence, "state", None) == "gameplay":
                await self.ctx.items.onboard_bootstrap("on_reconnect")
                (await self.reconcile_checked_automap_cleanup("connect_or_reconnect"))
        except StaleNativeResult:
            return
        except (OSError, RuntimeError, ValueError, WebSocketException) as exc:
            bridge_config.logger.warning("[Tracking] Error during reconnection reconciliation: %s", exc)

        await self._track_level_progress(newest_path)

    def advance_automap_cleanup_epoch(self):
        marker = self.ctx.runtime_lifecycle.map_identity.cached_marker
        return self.ctx.checked_visuals.advance_epoch(marker.get("gameplay_epoch") if marker else None)

    async def reconcile_fast_travel_unlock(self, trigger):
        return await self.ctx.fast_travel.reconcile(
            trigger, map_identity=self.ctx.runtime_lifecycle.map_identity,
            room_identity=self.ctx.get_ap_state_key(), state_key=self.ctx.state_key,
            runtime_ready=(await self.ctx.items.runtime_effects_ready()), item_ready=self.ctx.item_state_ready,
            rpc_ready=True, send=self.ctx.items._send_runtime_command,
        )

    def snapshot_fast_travel_eligibility(self, marker_data=None, *, refresh=False):
        return self.ctx.fast_travel.capture(
            self.ctx.runtime_lifecycle.map_identity, self.ctx.get_ap_state_key(),
            getattr(self.ctx, "checked_locations", None), marker_data, refresh=refresh,
        )

    async def _prepare_level_inventory(self, job, receipt_token):
        if self.ctx.level_ready.needs_preparation(job, "cleanup_epoch"):
            self.advance_automap_cleanup_epoch()
            self.ctx.level_ready.prepared(job, "cleanup_epoch")
        if self.ctx.level_ready.needs_preparation(job, "cleanup"):
            (await self.reconcile_checked_automap_cleanup("level_ready"))
            self.ctx.level_ready.prepared(job, "cleanup")
        await self.ctx.saves.check_mission_challenge_locations()
        if not self.ctx.receipt_session.is_current(receipt_token, self.ctx.state_key) or not self.ctx.level_ready.is_current(job):
            return False
        evidence = bridge_saves.read_gameplay_save_evidence()
        (await self.read_active_map_identity(evidence=evidence))
        if not self.ctx.level_ready.observe_resume(job, self.ctx.runtime_lifecycle.map_identity, (await self.ctx.items.runtime_effects_ready(evidence))):
            return False
        _, context_error = (await self.ctx.items._owner_reconcile(evidence))
        if context_error is ReconciliationBlock.NATIVE_APPLICATION_PENDING:
            self.ctx.level_ready.wait(job, "native_materialization_pending")
            return False
        if context_error:
            self.ctx.level_ready.wait(job, context_error)
            return False
        (await self.reconcile_fast_travel_unlock("level_ready"))
        self.ctx.level_ready.complete(job)
        return True

    async def process_level_ready(self, newest_path=None):
        """Adapt native observations and apply the ordered effects of an accepted job."""
        evidence = bridge_saves.read_gameplay_save_evidence()
        (await self.read_active_map_identity(evidence=evidence))
        self.snapshot_fast_travel_eligibility()
        marker = self.ctx.runtime_lifecycle.map_identity.cached_marker
        epoch = marker.get("gameplay_epoch") if isinstance(marker, Mapping) else None
        if not self.ctx.level_ready.can_start(epoch):
            return False
        if not self.ctx.level_ready.observe_readiness(epoch, (await self.ctx.items.runtime_effects_ready(evidence)), getattr(evidence, "state", None)):
            return False
        slot_data = getattr(self.ctx, "_connected_slot_data", {})
        active_context = self.ctx.items._refresh_runtime_context(slot_data)
        use_dlc = slot_data.get("runtime", slot_data).get("use_dlc_content")
        admission = self.ctx.level_ready.start(
            epoch, active_context, use_dlc, self.ctx.dlc_evidence if use_dlc else None,
            newest_path or marker.get("path"),
        )
        if admission.job is None:
            bridge_config.logger.info("MATERIALIZATION_BLOCKED reason=%s", admission.block)
            return False
        job = admission.job
        receipt_token = self.ctx.receipt_session.capture(self.ctx.state_key)
        try:
            if job.settle_inventory and not await self._settle_level_inventory(job, epoch, receipt_token):
                return False
            return await self._prepare_level_inventory(job, receipt_token)
        finally:
            self.ctx.level_ready.finish(job)

    async def reconcile_checked_automap_cleanup(self, trigger):
        if self.ctx.server_checked_locations_ready:
            checked = frozenset(getattr(self.ctx, "checked_locations", ()))
            try:
                link = (await self.ctx.items._native_game_link_async())
                result = (await self.ctx.items._native_call(link, 'publish_checked_locations', checked, time.time_ns()))
                publication = (result.get("pid"), result.get("process_created"),
                               result.get("lifecycle_generation"), checked)
                if publication != self.ctx._native_automap_published:
                    bridge_config.log_item_event(
                        "ITEM_AUTOMAP_SNAPSHOT_ACCEPTED", trigger=trigger, decision="accepted_publication",
                        snapshot_revision=result.get("revision"), lifecycle_generation=result.get("lifecycle_generation"),
                        checked_count=len(checked), native_fault=result.get("native_fault"),
                        namespace=result.get("namespace"), process_id=result.get("pid"),
                        process_created=result.get("process_created"), request_id=result.get("request_id"),
                        native_build=result.get("build_id"),
                        state_key=getattr(self.ctx, "state_key", None),
                    )
                    self.ctx._native_automap_published = publication
            except (RuntimeError, OSError, ValueError) as error:
                bridge_config.log_item_event(
                    "ITEM_AUTOMAP_SNAPSHOT_DEFERRED", trigger=trigger, decision="publication_unconfirmed",
                    reason="native_snapshot_not_accepted", detail=str(error), checked_count=len(checked),
                    state_key=getattr(self.ctx, "state_key", None),
                )
        return await self.ctx.checked_visuals.reconcile(
            trigger, map_identity=self.ctx.runtime_lifecycle.map_identity,
            room_identity=self.ctx.get_ap_state_key(), state_key=self.ctx.state_key,
            checked=getattr(self.ctx, "checked_locations", None),
            checked_ready=self.ctx.server_checked_locations_ready,
            runtime_ready=(await self.ctx.items.runtime_effects_ready()), rpc_ready=True, send=self.ctx.items._send_runtime_command,
        )

    def record_local_automap_cleanup_ownership(self, location_id, event_paths):
        try:
            event_mtime = max(Path(path).stat().st_mtime_ns for path in event_paths)
        except (OSError, ValueError):
            return False
        return self.ctx.checked_visuals.record_local_ownership(
            location_id, map_identity=self.ctx.runtime_lifecycle.map_identity,
            room_identity=self.ctx.get_ap_state_key(), event_mtime=event_mtime,
        )

    async def _settle_level_inventory(self, job, epoch, receipt_token):
        bridge_config.logger.info("[Context] TAG_INVENTORY_SETTLE epoch=%s delay_ms=%s",
                    epoch, int(bridge_config.TAG_INVENTORY_SETTLE_SECONDS * 1000))
        await asyncio.sleep(bridge_config.TAG_INVENTORY_SETTLE_SECONDS)
        if not self.ctx.receipt_session.is_current(receipt_token, self.ctx.state_key) or not self.ctx.level_ready.is_current(job):
            return False
        evidence = bridge_saves.read_gameplay_save_evidence()
        (await self.read_active_map_identity(evidence=evidence))
        if not self.ctx.level_ready.observe_resume(
            job, self.ctx.runtime_lifecycle.map_identity, (await self.ctx.items.runtime_effects_ready(evidence)), settled=True,
        ):
            return False
        return True

    def _accept_authored_marker(self, lease, newest_mtime, marker_data, evidence_epoch, markers):
        if lease is not None:
            lease.observe_gameplay_loaded(newest_mtime)
        self.accept_map_identity(marker_data, evidence_epoch)
        self.snapshot_fast_travel_eligibility(marker_data=marker_data)
        for _, marker_path in markers:
            cleanup_active_map_marker_file(marker_path)
        return True

    async def ingest_visible_runtime_lifecycle(self, evidence=None, lifecycle_markers=None):
        """Accept fresh FirstThink markers as live-map and load-epoch authority."""
        lease = getattr(self.ctx, "runtime_observation_lease", None)
        evidence_epoch = getattr(evidence, "epoch", None)
        if evidence_epoch is not None:
            self.ctx.runtime_lifecycle.record_native_epoch(evidence_epoch)
        if lease is not None and not lease.process_probe():
            return False

        markers = (
            lifecycle_markers
            if lifecycle_markers is not None
            else discover_active_map_markers()
        )
        if not markers:
            return (await self.advance_known_map_materialization(evidence))
        newest_mtime, newest_path = markers[-1]
        started = getattr(lease, "started_ns", None) if lease else None
        action = self.ctx.runtime_lifecycle.authored_timestamp_action(newest_mtime, started)
        if action == "ignore":
            return (await self.advance_known_map_materialization(evidence))
        if action == "native_fallback":
            return (await self.advance_known_map_materialization(evidence))
        marker_data = parse_active_map_marker(newest_path, newest_mtime)
        if marker_data is None:
            self.invalidate_map_identity("malformed_marker")
            return False
        self.ctx.saves.invalidate_active_save_proof()
        self.ctx.fast_travel.invalidate(clear_submitted=True)
        self.ctx.save_observer.clear_mission_select()
        self.ctx.runtime_lifecycle.clear_map()
        self.ctx.runtime_lifecycle.clear_pending_marker()
        marker_data = self.ctx.runtime_lifecycle.authored_marker_proposal(
            marker_data, newest_mtime, bridge_config.gameplay_evidence_mtime_ns(), evidence_epoch,
            evidence_state=getattr(evidence, "state", None),
        )
        return self._accept_authored_marker(lease, newest_mtime, marker_data, evidence_epoch, markers)

    def _log_native_materialization(self, decision, marker_data, epoch, evidence):
        if decision.action == "reload":
            bridge_config.logger.info(
                "[MAP] MATERIALIZATION_EPOCH_SECONDARY map=%s epoch=%s "
                "source=native_same_map_load_edge evidence_provisional=%s",
                marker_data.get("map_key", "<unknown>"), epoch,
                bool(getattr(evidence, "provisional", False)),
            )
        else:
            event = "MAP_INITIALIZE_EVIDENCE" if decision.action == "initialize" else "MAP_TRANSITION_EVIDENCE"
            bridge_config.logger.info(
                "[MAP] %s map=%s epoch=%s runtime_map=%s",
                event, decision.map_key, epoch, decision.runtime_map,
            )
        return True

    async def _recover_cached_materialization(self, cached, evidence):
        if cached is None or cached.get("native_scope") or cached.get("materialization_suspended"):
            evidence = evidence or bridge_saves.read_gameplay_save_evidence()
            if (await self.ctx.items._recover_native_materialization(evidence)):
                return True, evidence
            if cached and cached.get("native_scope"):
                return False, evidence
        return None, evidence

    async def advance_known_map_materialization(self, evidence):
        """Advance epoch from a fresh native load edge or transition."""
        cached = getattr(self.ctx.runtime_lifecycle.map_identity, "cached_marker", None)
        recovered, evidence = await self._recover_cached_materialization(cached, evidence)
        if recovered is not None:
            return recovered
        decision = self.ctx.runtime_lifecycle.classify_native_load(evidence, MATERIALIZATION_MAPS)
        if decision is None:
            return False
        evidence_epoch = decision.evidence_epoch
        if decision.action == "bind":
            self._bind_native_materialization(evidence_epoch, cached, evidence)
            return False
        if decision.action == "suspend":
            return self.suspend_map_materialization(
                cached, evidence_epoch, "provisional_same_map_load_edge"
            )
        evidence_mtime = bridge_config.gameplay_evidence_mtime_ns()
        marker_data = self.ctx.runtime_lifecycle.native_marker_proposal(decision, evidence_mtime)
        if marker_data is None:
            return False
        epoch = marker_data["gameplay_epoch"]

        lease = getattr(self.ctx, "runtime_observation_lease", None)
        if lease is not None:
            lease.observe_gameplay_loaded(evidence_mtime)
        self.ctx.saves.invalidate_active_save_proof()
        self.ctx.fast_travel.invalidate(clear_submitted=True)
        if decision.action != "reload":
            self.ctx.save_observer.clear_mission_select()
        self.accept_map_identity(marker_data, evidence_epoch)
        self.snapshot_fast_travel_eligibility(marker_data=marker_data)
        self.advance_automap_cleanup_epoch()
        self._log_native_materialization(decision, marker_data, epoch, evidence)
        return True

    def suspend_map_materialization(self, cached, evidence_epoch, reason):
        """Suspend map authority until a fresh authored marker reacquires context."""
        if cached.get("materialization_suspended"):
            return False
        self.ctx.saves.invalidate_active_save_proof()
        self.ctx.runtime_lifecycle.suspend_marker(evidence_epoch, reason)
        self.ctx.runtime_lifecycle.record_lease_publication(None, False)
        self.ctx.runtime_lifecycle.clear_context()
        bridge_config.logger.info(
            "[MAP] MATERIALIZATION_SUSPENDED map=%s evidence_epoch=%s reason=%s",
            cached.get("map_key", "<unknown>"), evidence_epoch, reason,
        )
        return True

    def invalidate_map_identity(self, reason, *, clear_pending=True):
        self.ctx.items.reset_transient_effects(reason)
        if getattr(self.ctx, "last_marker_reject_reason", None) != reason:
            bridge_config.logger.info("[MAP] MAP_IDENTITY_MARKER_REJECTED reason=%s", reason)
            self.ctx.last_marker_reject_reason = reason
        self.ctx.runtime_lifecycle.clear_map()
        self.ctx.runtime_lifecycle.record_lease_publication(None, False)
        if reason == "game_not_running":
            self.ctx.runtime_lifecycle.clear_context(clear_log=True)
        if clear_pending:
            self.ctx.runtime_lifecycle.clear_pending_marker()
        self.ctx.save_observer.clear_mission_select()
        self.ctx.fast_travel.invalidate(clear_submitted=False)
        return None

    def store_pending_map_identity(self, marker_data):
        self.ctx.runtime_lifecycle.stage_marker(marker_data)
        self.ctx.save_observer.set_frozen(True)
        if getattr(self.ctx, "last_pending_marker_mtime", None) != marker_data["mtime_ns"]:
            self.ctx.last_pending_marker_mtime = marker_data["mtime_ns"]
            bridge_config.logger.info(
                "[MAP] MAP_IDENTITY_MARKER_PENDING map=%s runtime_map=%s "
                "source=map_start_event epoch=%s",
                marker_data["map_key"],
                marker_data["runtime_map"],
                marker_data["gameplay_epoch"],
            )
        return self.ctx.runtime_lifecycle.map_identity.pending_marker

    def accept_map_identity(self, marker_data, evidence_epoch=None):
        previous = getattr(self.ctx.runtime_lifecycle.map_identity, "cached_marker", None)
        if isinstance(previous, Mapping) and any(
            previous.get(key) != marker_data.get(key)
            for key in ("map_key", "runtime_map", "gameplay_epoch")
        ):
            self.ctx.items.reset_transient_effects("map_transition")
            self.ctx.fast_travel.invalidate(clear_submitted=True)
        marker_data = self.ctx.runtime_lifecycle.accept_marker(marker_data, evidence_epoch)
        materialized_epoch = marker_data.get("gameplay_epoch")
        published = True
        self.ctx.runtime_lifecycle.record_lease_publication(materialized_epoch, published)
        self.ctx.level_ready.queue(materialized_epoch, marker_data.get("path"))
        marker_mtime = marker_data.get("mtime_ns", 0)
        if self.ctx.runtime_lifecycle.observe_marker_timestamp(marker_mtime, evidence_epoch):
            self.ctx.last_marker_reject_reason = None
            bridge_config.logger.info(
                "[MAP] MAP_IDENTITY_MARKER_ACCEPTED map=%s runtime_map=%s "
                "source=map_start_event epoch=%s",
                marker_data["map_key"],
                marker_data["runtime_map"],
                marker_data["gameplay_epoch"],
            )
        return marker_data

    def _missing_map_identity(self, evidence):
        if evidence is not None and getattr(evidence, "state", None) != "gameplay":
            self.ctx.saves.invalidate_active_save_proof()
            return self.invalidate_map_identity("menu")
        return None

    def _marker_predates_process(self, lease, marker_mtime):
        return lease is not None and lease.started_ns and marker_mtime < lease.started_ns

    async def read_active_map_identity(self, evidence=None):
        lease = getattr(self.ctx, "runtime_observation_lease", None)
        (await self.ingest_visible_runtime_lifecycle(evidence=evidence))
        if lease is not None and not lease.process_probe():
            self.ctx.saves.invalidate_active_save_proof()
            return self.invalidate_map_identity("game_not_running")

        cached = self.ctx.runtime_lifecycle.map_identity.cached_marker
        if not isinstance(cached, Mapping):
            return self._missing_map_identity(evidence)
        if cached.get("materialization_suspended"):
            return None
        if cached.get("native_scope"):
            return cached if (await self.ctx.items.runtime_effects_ready(evidence)) else None
        marker_mtime = cached.get("mtime_ns", 0)
        if self._marker_predates_process(lease, marker_mtime):
            return self.invalidate_map_identity("stale_marker")
        if evidence is not None and getattr(evidence, "state", None) != "gameplay":
            return self._hold_map_identity(cached, marker_mtime, evidence)
        self.ctx.last_map_identity_hold_signature = None
        return self.accept_map_identity(cached, getattr(evidence, "epoch", None))

    def _bind_native_materialization(self, evidence_epoch, cached, evidence):
        self.ctx.runtime_lifecycle.bind_materialization_evidence(evidence_epoch)
        bridge_config.logger.info(
            "[MAP] MATERIALIZATION_GENERATION_BOUND map=%s evidence_epoch=%s "
            "source=first_same_map_evidence provisional=%s",
            cached.get("map_key", "<unknown>"),
            evidence_epoch,
            str(bool(getattr(evidence, "provisional", False))).lower(),
        )

    def _hold_map_identity(self, cached, marker_mtime, evidence):
        self.ctx.saves.invalidate_active_save_proof()
        hold_signature = (
            marker_mtime,
            getattr(evidence, "state", None),
            bool(getattr(evidence, "provisional", False)),
        )
        if hold_signature != getattr(self.ctx, "last_map_identity_hold_signature", None):
            self.ctx.last_map_identity_hold_signature = hold_signature
            bridge_config.logger.info(
                "[MAP] MAP_IDENTITY_HELD map=%s epoch=%s evidence_state=%s",
                cached.get("map_key", "<unknown>"),
                cached.get("gameplay_epoch", "<unknown>"),
                getattr(evidence, "state", "<unknown>"),
            )
        return self.accept_map_identity(cached, getattr(evidence, "epoch", None))


class CheckBatchPublicationPort(Protocol):
    async def locations(self, location_ids) -> None: ...
    def mark_submitted(self, location_id) -> None: ...


class PhysicalChecks:
    def __init__(self, logger):
        self._logger = logger
        self._generation = 0
        self._last_event = None
        self._pending = set()

    @property
    def last_event(self):
        return self._last_event

    def invalidate(self):
        self._generation += 1
        self._pending.clear()

    def pending(self, facts):
        """Qualified observations retained until ACK, within this publication epoch."""
        self._pending.difference_update(facts.checked)
        return frozenset(self._pending & facts.server_locations)

    def disposition(self, location_id, facts: CheckObservation, item_ready):
        if location_id in facts.checked:
            return "acknowledged"
        if item_ready and facts.server_locations and location_id not in facts.server_locations:
            return "quarantine"
        if location_id not in facts.server_locations:
            return "outside_slot"
        self._pending.add(location_id)
        return "submitted" if location_id in facts.submitted else "submit"

    async def publish(self, location_ids, publication: CheckBatchPublicationPort):
        if not location_ids:
            return
        generation = self._generation
        self._pending.update(location_ids)
        self._last_event = location_ids[-1]
        try:
            await publication.locations(location_ids)
        except (OSError, RuntimeError, WebSocketException) as error:
            if generation == self._generation:
                self._logger.error("[Trigger] Failed to send AP check events; preserving files for retry: %s", error)
            return
        if generation != self._generation:
            return
        for location_id in location_ids:
            self._logger.info("[Trigger] Native AP event detected -> Queued Location %s", location_id)
            publication.mark_submitted(location_id)


class LocationScoutPort(Protocol):
    async def scout(self, location_ids) -> None: ...


@dataclass(frozen=True)
class ScoutResult:
    current: bool
    error: str | None = None


class LocationSetup:
    def __init__(self, logger):
        self._logger = logger
        self._generation = 0
        self.invalidate()

    def invalidate(self):
        self._generation += 1
        self._expected = None
        self._rows = {}
        self._failed = False
        self._complete = False

    @property
    def complete(self):
        return self._complete

    @property
    def received_ids(self):
        return frozenset(self._rows)

    @property
    def ready_to_resolve(self):
        return not self._failed and not self._complete and set(self._rows) == self._expected

    @staticmethod
    def _connected_ids(args, field):
        values = args.get(field)
        if not isinstance(values, list | tuple | set | frozenset):
            raise ValueError(f"Connected.{field} must be a location list")
        if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
            raise ValueError(f"Connected.{field} contains invalid location ID")
        return set(values)

    def begin(self, args, supported_ids, room_ids=frozenset()):
        self.invalidate()
        missing = self._connected_ids(args, "missing_locations")
        checked = self._connected_ids(args, "checked_locations")
        if missing & checked:
            raise ValueError("Connected location sets overlap")
        active = missing | checked
        unknown = sorted(active - supported_ids - room_ids)
        if unknown:
            raise ValueError(f"Connected contains unknown DOOM location IDs: {unknown}")
        self._expected = active
        return frozenset(self._expected)

    def fail(self, message):
        if self._failed:
            return False
        self._failed = True
        self._logger.error("[Placement] SCOUT_REJECTED %s", message)
        return True

    async def scout(self, publication: LocationScoutPort):
        generation = self._generation
        try:
            await publication.scout(sorted(self._expected))
        except (OSError, RuntimeError, WebSocketException) as error:
            if generation != self._generation:
                return ScoutResult(False)
            return ScoutResult(True, f"LocationScouts failed: {error}")
        return ScoutResult(generation == self._generation)

    def consume(self, args, materialized_ids):
        if self._expected is None or self._failed:
            return None
        rows = args.get("locations") if isinstance(args, dict) else None
        if not isinstance(rows, list | tuple):
            return "LocationInfo.locations is not a list"
        packet_ids = []
        for row in rows:
            error = self._consume_placement(row, materialized_ids)
            if error:
                return error
            packet_ids.append(row[1])
        if len(packet_ids) != len(set(packet_ids)):
            return "LocationInfo contains duplicate location IDs"
        return None

    def resolved(self):
        self._complete = True

    def _consume_placement(self, row, materialized_ids):
        if not isinstance(row, list | tuple) or len(row) != 4:
            return "LocationInfo contains malformed placement"
        location_id = row[1]
        if not isinstance(location_id, int) or isinstance(location_id, bool):
            return "LocationInfo contains invalid location ID"
        if location_id not in self._expected:
            return f"LocationInfo contains unknown location ID: {location_id}"
        if location_id not in materialized_ids:
            return f"LocationInfo did not materialize location ID: {location_id}"
        previous = self._rows.get(location_id)
        if previous is not None and previous != tuple(row):
            return f"LocationInfo conflicts at location ID: {location_id}"
        self._rows[location_id] = tuple(row)
        return None


def resolve_placement_records(location_ids, network_items, slot_info_by_id, names, local_slot):
    records = []
    for location_id in sorted(location_ids):
        network_item = network_items[location_id]
        recipient_slot = network_item.player
        slot_info = slot_info_by_id.get(recipient_slot)
        if slot_info is None:
            raise ValueError(f"recipient slot is unknown: {recipient_slot}")
        # Other games supply display text; only its length is constrained.
        location_name = str(names.location_names.lookup_in_slot(location_id, local_slot))[:NAME_LIMIT]
        item_name = str(names.item_names.lookup_in_slot(network_item.item, recipient_slot))[:NAME_LIMIT]
        classification = network_item.flags
        if not isinstance(classification, int) or isinstance(classification, bool):
            raise ValueError(f"invalid item classification at location {location_id}")
        if classification < 0:
            raise ValueError(f"invalid item classification at location {location_id}")
        local = recipient_slot == local_slot
        recipient_name = names.player_names.get(recipient_slot, str(recipient_slot))
        if not isinstance(recipient_name, str) or not recipient_name.strip():
            raise ValueError(f"recipient name is unavailable: {recipient_slot}")
        records.append({
            "location_id": location_id,
            "location_name": location_name,
            "item_id": network_item.item,
            "item_name": item_name,
            "recipient_slot": recipient_slot,
            "recipient_name": recipient_name,
            "classification": classification,
            "trap": bool(classification & ITEM_CLASSIFICATION_TRAP),
            "local": local,
        })
    return records


@dataclass(frozen=True)
class SaveCheckBinding:
    identity: str
    team: int
    slot: int
    state_key: str | None
    registry_revision: str
    checked: frozenset[int]
    item_ready: bool
    persist_enabled: bool


class SaveCheckObservations:
    def __init__(self, observer, scope, persist, logger):
        self._observer, self._scope, self._persist, self._logger = observer, scope, persist, logger

    def observe_record(self, kind, slot, unlockable, record):
        return self._observer.observe_record(kind, slot, unlockable, record)

    def observe_edges(self, observer_key, records, entries, slot_directory,
                      first_sample_pending=frozenset()):
        if not self._scope.item_ready:
            return set()
        acknowledged = {
            key
            for key, entry in entries.items()
            if entry["location_id"] in self._scope.checked
        }
        pending, created, new_edges = self._observer.observe_edges(
            session_identity=self._scope.identity,
            team=self._scope.team,
            slot=self._scope.slot,
            doom_save_slot=slot_directory,
            registry_revision=self._scope.registry_revision,
            observer_key=observer_key,
            records=records,
            acknowledged_records=acknowledged,
            first_sample_pending=first_sample_pending,
        )
        if self._scope.persist_enabled:
            self._persist()
        if created:
            self._logger.info(
                "[OBSERVER] BASELINE_CREATED session=%s save_slot=%s records=%s",
                self._scope.state_key,
                slot_directory,
                sum(records.values()),
            )
        for key in sorted(new_edges):
            self._logger.info("[OBSERVER] %s key=%s", "BASELINE_CATCHUP" if created else "EDGE_COMPLETE", key)
        return pending


class CheckedVisuals:
    def __init__(self, catalog_maps, visuals, session, logger):
        self._catalog_maps = dict(catalog_maps)
        self._visuals = visuals
        self._session = session
        self._logger = logger
        self._retry = {}
        self._status = {}
        self.rebind()

    def rebind(self):
        self._epoch = None
        self._local_observed = set()

    @property
    def epoch(self):
        return self._epoch

    @property
    def status(self):
        return MappingProxyType(dict(self._status))

    def _catalog_map_key(self, runtime_map):
        normalized = canonical_map_name(runtime_map)
        matches = [key for key, value in self._catalog_maps.items() if value == normalized]
        return matches[0] if len(matches) == 1 else None

    def advance_epoch(self, epoch):
        """Start map-safe checked-visual reconciliation for the current level epoch."""
        previous_epoch = self._epoch
        if not valid_materialization_epoch(epoch):
            self._epoch = None
            if previous_epoch != self._epoch:
                self._retry.clear()
                self._status.clear()
                self._local_observed.clear()
            return None
        self._epoch = epoch
        if previous_epoch != epoch:
            self._retry.clear()
            self._status.clear()
            self._local_observed.clear()
        return self._epoch

    def _publication_ready(self, room_identity, checked_ready, checked, rpc_ready, epoch, map_name, trigger):
        if not room_identity:
            self._automap_cleanup_transition(
                (epoch, "", map_name, ""),
                "PENDING",
                "room_identity_unavailable",
                trigger=trigger,
            )
            return False
        if not checked_ready or not isinstance(checked, set | frozenset | list | tuple):
            self._automap_cleanup_transition(
                (epoch, room_identity, map_name, ""),
                "PENDING",
                "checked_locations_unavailable",
                trigger=trigger,
            )
            return False
        if not rpc_ready:
            self._automap_cleanup_transition(
                (epoch, room_identity, map_name, ""),
                "PENDING",
                "rpc_not_ready",
                trigger=trigger,
            )
            return False
        return True

    async def reconcile(self, trigger, *, map_identity, room_identity, state_key, checked, checked_ready, runtime_ready, rpc_ready, send):
        """Suppress confirmed AP entities for the current map."""
        matched = self._matching_marker(map_identity, runtime_ready)
        if matched is None:
            return False
        epoch, map_name, map_key = matched
        entries = [
            entry for entry in self._visuals.get(map_key or "", {}).values()
            if checked_reconciliation_entity(entry)
        ]
        if not entries:
            return False
        if not self._publication_ready(room_identity, checked_ready, checked, rpc_ready, epoch, map_name, trigger):
            return False
        checked = set(checked)
        changed = False
        now = time.monotonic()
        for entry in sorted(entries, key=lambda item: item["location_id"]):
            changed = await self._reconcile_entry(
                entry, checked, room_identity, map_name, epoch, now, trigger, state_key, send,
            ) or changed
        return changed

    def record_local_ownership(self, location_id, *, map_identity, room_identity, event_mtime):
        """Bind local pickup cleanup ownership to current materialization epoch."""
        marker = map_identity.cached_marker
        if not isinstance(marker, Mapping):
            return False
        epoch = marker.get("gameplay_epoch")
        map_key = marker.get("map_key")
        if map_key != self._catalog_map_key(marker.get("runtime_map", "")):
            return False
        entry = self._visuals.get(map_key or "", {}).get(location_id)
        if not valid_materialization_epoch(epoch) or not entry:
            return False
        if entry.get("classification") != "visible_cleanup":
            return False
        if event_mtime < marker.get("mtime_ns", 0):
            return False
        self._local_observed.add((epoch, location_id))
        self._automap_cleanup_transition(
            (epoch, room_identity or "", marker.get("runtime_map", ""), str(location_id)),
            "LOCAL_CHECK_OBSERVED",
            "server_confirmation_and_repair_required",
            trigger="native_ap_check_event",
        )
        return True

    def _automap_cleanup_transition(self, delivery_key, status, reason, *, trigger=None):
        previous = self._status.get(delivery_key)
        if previous == status:
            return
        self._status[delivery_key] = status
        self._logger.info(
            "[Automap] cleanup lifecycle map=%s epoch=%s location=%s "
            "trigger=%s status=%s previous=%s skip_reason=%s",
            delivery_key[2] if len(delivery_key) > 2 else "<unknown>",
            delivery_key[0] if delivery_key else "<unknown>",
            delivery_key[3] if len(delivery_key) > 3 else "<unknown>",
            trigger or "<none>",
            status,
            previous,
            reason if status != "COMMAND_QUEUED_UNVERIFIED" else "<none>",
        )

    async def _reconcile_entry(self, entry, checked, room_identity, map_name, epoch, now, trigger, state_key, send):
        location_id = entry["location_id"]
        if location_id not in checked:
            return False
        entity_name = checked_reconciliation_entity(entry)
        delivery_key = (room_identity, map_name, str(location_id))
        runtime_key = (epoch, *delivery_key)
        retry = self._retry.setdefault(runtime_key, {"attempt": 0, "deadline": 0.0})
        if now < retry["deadline"]:
            self._automap_cleanup_transition(
                runtime_key,
                "RETRY_WAIT",
                "queue_retry_backoff",
                trigger=trigger,
            )
            return False
        command_id = stable_command_key(
            "automap-cleanup",
            getattr(self, "_session", "session"),
            room_identity,
            map_name,
            location_id,
            self._epoch,
            retry["attempt"],
        )
        command = f"ai_ScriptCmdEnt {entity_name} activate"
        if not await send(
            command,
            coalesce_key=command_id,
            already_queued_ok=True,
            state_key=state_key,
            materialization_lease=epoch,
            execution_class=MAP_ENTITY_SAFE,
            operation=CHECKED_VISUAL_HIDE,
        ):
            retry["attempt"] += 1
            retry["deadline"] = now + min(
                AUTOMAP_CLEANUP_RETRY_MAX_SECONDS,
                AUTOMAP_CLEANUP_RETRY_BASE_SECONDS * (2 ** min(retry["attempt"] - 1, 3)),
            )
            self._automap_cleanup_transition(
                runtime_key,
                "RETRY",
                "queue_unavailable",
                trigger=trigger,
            )
            return False
        retry["attempt"] += 1
        retry["deadline"] = now + AUTOMAP_CLEANUP_RETRY_MAX_SECONDS
        self._automap_cleanup_transition(
            runtime_key,
            "REPAIR_QUEUED",
            "model_presence_unverified",
            trigger=trigger,
        )
        self._logger.info(
            "[Automap] Checked-state cleanup queued location=%s map=%s "
            "epoch=%s trigger=%s target=%s",
            location_id,
            map_name,
            self._epoch,
            trigger,
            entity_name,
        )
        return True

    def _matching_marker(self, map_identity, runtime_ready):
        marker = map_identity.cached_marker
        self.advance_epoch(marker.get("gameplay_epoch") if isinstance(marker, Mapping) else None)
        if not runtime_ready:
            return None
        marker = map_identity.cached_marker
        if not isinstance(marker, Mapping):
            return None
        epoch = marker.get("gameplay_epoch")
        if (
            not valid_materialization_epoch(epoch)
            or epoch != self._epoch
        ):
            return None
        marker_map = canonical_map_name(marker.get("runtime_map", ""))
        current_map = canonical_map_name(map_identity.current_map or "")
        if not marker_map or marker_map != current_map:
            return None
        map_name = marker_map
        map_key = self._catalog_map_key(map_name)
        if marker.get("map_key") != map_key:
            return None
        return epoch, map_name, map_key


def _validated_catalog_map(raw_map_key, raw_runtime_map):
    if not isinstance(raw_map_key, str) or not raw_map_key.strip():
        raise ValueError("foundation active_maps contains an invalid map key")
    if raw_map_key != raw_map_key.strip():
        raise ValueError(f"foundation active_maps map key is not canonical: {raw_map_key!r}")
    if not isinstance(raw_runtime_map, str) or not raw_runtime_map.strip():
        raise ValueError(f"foundation active_maps[{raw_map_key!r}] has an invalid runtime map")
    runtime_map = canonical_map_name(raw_runtime_map)
    if not runtime_map:
        raise ValueError(f"foundation active_maps[{raw_map_key!r}] has an empty canonical runtime map")
    return runtime_map


def _validated_catalog_maps(active_maps):
    """Validate and canonicalize runtime map identity from packaged contracts."""
    if not isinstance(active_maps, dict) or not active_maps:
        raise ValueError("foundation active_maps must be a non-empty object")
    validated = {}
    seen_runtime_maps = set()
    for raw_map_key, raw_runtime_map in active_maps.items():
        runtime_map = _validated_catalog_map(raw_map_key, raw_runtime_map)
        if runtime_map in seen_runtime_maps:
            raise ValueError(f"foundation active_maps contains duplicate runtime map: {runtime_map}")
        validated[raw_map_key] = runtime_map
        seen_runtime_maps.add(runtime_map)
    return validated


def _materialization_map_key(runtime_map):
    runtime_map = canonical_map_name(runtime_map)
    matches = [
        map_key
        for map_key, catalog_runtime_map in MATERIALIZATION_MAPS.items()
        if catalog_runtime_map == runtime_map
    ]
    return matches[0] if len(matches) == 1 else None


def _remove_acknowledged_event(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        bridge_config.logger.warning(
            "[Trigger] Could not remove acknowledged AP event "
            f"{os.path.basename(path)} yet: {error}"
        )


def _record_physical_gates(context, event_paths_by_location, facts):
    gate_completions = set(context.session_state.get("physical_slayer_gates", ()))
    observed_gates = {
        location_id for location_id in event_paths_by_location
        if location_id in facts.server_locations
        and bridge_config.DOOM_LOCATION_NAMES.get(location_id, "").endswith(" - Slayer Gate Complete")
    }
    if observed_gates - gate_completions:
        if not context.item_state_ready:
            return False
        context.session_state["physical_slayer_gates"] = sorted(gate_completions | observed_gates)
        committed = False
        try:
            context.persist_session_state()
            committed = True
        finally:
            if not committed:
                context.session_state["physical_slayer_gates"] = sorted(gate_completions)
    return True


def _tracker_error_fingerprint(error):
    lineno = error.__traceback__.tb_lineno if error.__traceback__ else 0
    return f"{type(error).__name__}:{error}:{lineno}"


with open(RUNTIME_LOCATIONS_FILE, encoding="utf-8") as f:
    RUNTIME_LOCATIONS = json.load(f)


EXULTIA_COMPLETE_LOCATION = RUNTIME_LOCATIONS[
    "Exultia - Mission Complete"
]
CULTIST_BASE_COMPLETE_LOCATION = RUNTIME_LOCATIONS[
    "Cultist Base - Mission Complete"
]
DOOM_HUNTER_BASE_COMPLETE_LOCATION = RUNTIME_LOCATIONS[
    "Doom Hunter Base - Mission Complete"
]
KNOWN_CATALOG_MAPS = _validated_catalog_maps(
    load_foundation_contracts().get("active_maps")
)
MATERIALIZATION_MAPS = _validated_catalog_maps({
    **{context.map_keys[0]: runtime_map for runtime_map, context in CONTEXT_BY_MAP.items()
       if len(context.map_keys) == 1},
    **KNOWN_CATALOG_MAPS,
})
FAST_TRAVEL_MAP_KEYS = frozenset(
    map_key for map_key, runtime_map in KNOWN_CATALOG_MAPS.items()
    if map_key != "hub" and runtime_map in {
        "game/sp/e1m1_intro/e1m1_intro",
        "game/sp/e1m2_battle/e1m2_battle",
        "game/sp/e1m3_cult/e1m3_cult",
        "game/sp/e1m4_boss/e1m4_boss",
        "game/sp/e2m1_nest/e2m1_nest",
        "game/sp/e2m2_base/e2m2_base",
        "game/sp/e2m3_core/e2m3_core",
        "game/sp/e2m4_boss/e2m4_boss",
        "game/sp/e3m1_slayer/e3m1_slayer",
        "game/sp/e3m2_hell/e3m2_hell",
        "game/sp/e3m2_hell_b/e3m2_hell_b",
        "game/sp/e3m3_maykr/e3m3_maykr",
        "game/sp/e3m4_boss/e3m4_boss",
        "game/dlc/e4m1_rig/e4m1_rig",
        "game/dlc/e4m2_swamp/e4m2_swamp",
        "game/dlc/e4m3_mcity/e4m3_mcity",
    }
)
MISSION_CHALLENGE_RUNTIME_MAPS = frozenset(
    (set(bridge_saves.MISSION_CHALLENGE_RUNTIME_MAP_BY_UNLOCKABLE.values()) | set(KNOWN_CATALOG_MAPS.values()))
    - {"game/hub/hub", ""}
)
PUBLISHERS = load_publisher_contracts()
PUBLISHER_ENGINE = PublisherEngine(PUBLISHERS)
PUBLISHER_MAP_EVENT_FILENAMES = frozenset(
    trigger["filename"]
    for publisher in PUBLISHERS
    for trigger in publisher.triggers_for("map_event_file")
)
DECL_TO_LOCATION = {}
MANIFESTS_DIR = bridge_config.REPO_ROOT / "manifests"


if os.path.exists(MANIFESTS_DIR):
    for filename in os.listdir(MANIFESTS_DIR):
        if filename.endswith(".json"):
            with open(os.path.join(MANIFESTS_DIR, filename), encoding="utf-8") as f:
                manifest_data = json.load(f)
                DECL_TO_LOCATION.update(manifest_data)


AUTOMAP_VISUAL_REGISTRY = load_automap_visual_registry()
AUTOMAP_VISUALS_BY_MAP = index_automap_visual_registry(AUTOMAP_VISUAL_REGISTRY)
GOAL_POLICY = GoalPolicy(bridge_config.DOOM_LOCATION_NAMES, bridge_config.DLC_MISSION_PREFIXES, GOAL_CAPABILITIES,
                        bridge_config.GOAL_ENDPOINT_LOCATION_IDS, bridge_config.GOAL_REQUIREMENT_SUFFIXES,
                        (stage for stage in STAGES.values() if stage["kind"] == "master"))
ACTIVE_MAP_MARKER_PREFIX = "ap_active_map"
NAME_LIMIT = 128
AUTOMAP_CLEANUP_RETRY_BASE_SECONDS = 1.0
AUTOMAP_CLEANUP_RETRY_MAX_SECONDS = 8.0
