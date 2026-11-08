from __future__ import annotations

import asyncio
import json
import math
import re
import struct
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic
from types import MappingProxyType

from websockets.exceptions import WebSocketException

from doom_eap.content.item_classification import ITEM_CLASSIFICATION_TRAP
from doom_eap.content.killsanity_plan import resolved_rewards
from doom_eap.content.slot_data_contract import SlotData
from doom_eap.contracts.receipt_delivery import (
    FAST_TRAVEL_UNLOCK,
    MAP_ENTITY_SAFE,
    CheckObservation,
    CheckPublicationPort,
    stable_command_key,
)
from doom_eap.contracts.runtime_context import canonical_map_name, catalog_map_key
from doom_eap.runtime import sentinel_protocol_generated as codec
from doom_eap.strings import text as player_text
from tools.maps.notification_formatting import item_receipt_text

CAPABILITY = "unified_campaign_v1"
STAGES = {stage["id"]: stage for stage in json.loads(
    (Path(__file__).resolve().parents[2] / "data/campaign_stages.json").read_text(encoding="utf-8")
)}
ACCESS_IDS = {stage["access_id"]: key for key, stage in STAGES.items()}
FAST_TRAVEL_RETRY_BASE_SECONDS = 1.0
FAST_TRAVEL_RETRY_MAX_SECONDS = 8.0


class UnifiedCampaign:
    def __init__(self, slot_data, session_state, persist):
        from doom_eap.runtime.observers import admit_slot_data
        admitted = slot_data if isinstance(slot_data, SlotData) else admit_slot_data(slot_data)
        self.plan = _navigation_plan(admitted.campaign)
        self.persist = persist
        fingerprint = admitted.native_generation_fingerprint
        saved = session_state.setdefault("campaign_navigation", {
            "generation": fingerprint, "selected": "hub",
        })
        if saved.get("generation") != fingerprint:
            raise ValueError("navigation belongs to another generation")
        self.navigation = saved

    def goal_admitted(self, checked_locations, received_items):
        """Structural goal admission is independent of out-of-logic replay visibility."""
        goal = self.plan["goal_stage"]
        return (goal is None or
                all(STAGES[key]["completion_id"] in checked_locations
                    for key in self.plan["sequence"] if key != goal) or
                (self.plan["goal_as_item"] and any(item.item == STAGES[goal]["access_id"]
                                                  for item in received_items)))

    def _unlock_access_items(self, received_items, revealed, unlocked):
        plan = self.plan
        masters = plan["master_level_stage_ids"]
        for item in received_items:
            key = plan["access_items"].get(str(item.item))
            if key in masters or (key is not None and plan["mode"] == "Mission Access as Items"):
                unlocked.add(key)
                if key not in revealed:
                    revealed.append(key)

    def _reveal_completed_stages(self, sequence, completed, revealed):
        for key in sequence:
            if key in completed and key not in revealed:
                revealed.append(key)

    def _reveal_sequence(self, plan, sequence, completed, revealed, unlocked):
        if plan["mode"] == "Mission Access as Items":
            self._reveal_completed_stages(sequence, completed, revealed)
        else:
            for key in sequence:
                if key == plan["goal_stage"]:
                    break
                unlocked.add(key)
                if key not in revealed:
                    revealed.append(key)
                if key not in completed:
                    break

    def _stage_rows(self, order, revealed, completed, unlocked, goal):
        rows = []
        for key in order:
            visible = key in revealed or key in completed
            stage = STAGES[key]
            rows.append({
                "stage": key, "title": stage["name"] if visible else "???",
                "variant": stage.get("variant", "mission"),
                "map": stage["map"] if visible else "",
                "revealed": visible, "unlocked": key in unlocked,
                "completed": key in completed, "goal": key == goal,
                "details_visible": visible and key in unlocked,
            })
        return rows

    def _reveal_goal(self, plan, sequence, completed, revealed, unlocked):
        goal = plan["goal_stage"]
        if goal and all(key in completed for key in sequence if key != goal):
            unlocked.add(goal)
        if goal and goal not in revealed:
            revealed.append(goal)  # Explicit goal identity is the locked-row exception.
        return goal

    def snapshot(self, checked_locations, received_items):
        """Derive progression from server checks/items, including out-of-logic checks.

        Replays/retransmissions cannot increment a counter: completion is a set
        of existing location IDs. Local selection never creates progression.
        """
        plan = self.plan
        sequence = plan["sequence"]
        masters = plan["master_level_stage_ids"]
        completed = {key for key in [*sequence, *masters] if STAGES[key]["completion_id"] in checked_locations}
        revealed = list(plan["starting_stages"])
        unlocked = set(revealed) | completed
        self._unlock_access_items(received_items, revealed, unlocked)
        self._reveal_sequence(plan, sequence, completed, revealed, unlocked)
        goal = self._reveal_goal(plan, sequence, completed, revealed, unlocked)
        order = (revealed + [key for key in [*sequence, *masters] if key not in revealed]
                 if plan["mode"] == "Mission Access as Items" else [*sequence, *masters])
        rows = self._stage_rows(order, revealed, completed, unlocked, goal)
        ordinary = [key for key in sequence if key != goal]
        completed_count = sum(key in completed for key in ordinary)
        thresholds = plan["fortress_phase_thresholds"]
        phase = sum(completed_count >= threshold for threshold in thresholds)
        return {"rows": rows, "hub_map": plan["hub"]["map"], "fortress_phase": phase,
                "selected": self.navigation["selected"]}

    def select(self, stage, snapshot):
        if stage != "hub" and not any(row["stage"] == stage and row["unlocked"] for row in snapshot["rows"]):
            raise ValueError("native selection is unavailable in the authoritative projection")
        if self.navigation["selected"] != stage:
            self.navigation["selected"] = stage
            self.persist()


class CampaignMenu:
    def __init__(self):
        self.identity = None
        self.rows = None
        self.revision = 0

    def synchronize(self, link, snapshot):
        link.admitted_save_root()
        scope = link._run(["--pid", str(link.pid), "--native", "--json"])
        if scope.get("availability") != "enabled":
            raise RuntimeError("Native Campaign menu owner is unavailable")
        identity = (link.namespace, scope["target_pid"], scope["process_created"], scope["instance_id"])
        rows, stage_ids, summaries, rewards = _campaign_projection(snapshot, link.namespace)
        extended = "challenge_rewards" in snapshot
        serialized = json.dumps(rows, separators=(",", ":")), tuple(summaries), tuple(rewards) if extended else ()

        def exchange(operation, revision=0, index=0, row=(0, 0, 0, "", "")):
            payload = {}
            if operation == 22:
                native_row = codec.decode("sc_campaign_row", struct.pack("<III192s96s", *row[:3], row[3].encode("ascii"), row[4].encode("utf-8")))
                summary = codec.decode("sc_campaign_summary", summaries[index])
                known, count = struct.unpack_from("<II", rewards[index])
                entries = [codec.decode("campaign_reward", rewards[index][8 + n * 732:8 + (n + 1) * 732]) for n in range(count)]
                payload = dict(revision=revision, index=index, count=len(rows), row=native_row, summary=summary,
                               rewards_known=known, rewards=entries)
            elif operation == 23:
                payload = dict(revision=revision, count=len(rows), selected_id=row[0])
            elif operation == 24:
                payload = dict(revision=revision)
            result = link.request(operation, payload=payload)
            if result.get("status") != 0:
                raise RuntimeError(f"Native Campaign projection refused: {result.get('reason')}")
            return result

        result = exchange(24)
        if self.identity != identity or self.rows != serialized:
            # An interrupted upload never mutates the committed list. Use a new
            # monotonic revision for its replacement, including after reconnect.
            self.revision = max(self.revision, int(result["committed_revision"])) + 1
            for index, row in enumerate(rows):
                exchange(22, self.revision, index, row)
            selected_id = next((key for key, stage in stage_ids.items() if stage == snapshot["selected"]), 1)
            result = exchange(23, self.revision, row=(selected_id, 0, 0, "", ""))
            if int(result["committed_revision"]) != self.revision:
                raise RuntimeError("Native Campaign projection was not committed")
            self.identity, self.rows = identity, serialized
        result["selected_stage"] = stage_ids.get(result["selected_id"])
        result["loaded_stage"] = stage_ids.get(result["loaded_id"])
        return result


@dataclass(frozen=True)
class PhysicalChallenge:
    unlockable: str
    found: int
    required: int
    checked: bool


@dataclass(frozen=True)
class MissionPresentation:
    namespace: str
    generation: str
    runtime_map: str
    found: int | None
    total: int | None
    challenges: tuple[PhysicalChallenge, ...]


@dataclass(frozen=True)
class ChallengeReward:
    location_id: int
    unlockable: str
    name: str
    checked: bool
    text: str


@dataclass(frozen=True)
class ChallengePresentation:
    namespace: str
    generation: str
    runtime_map: str
    known: bool
    challenges: tuple[ChallengeReward, ...]
    aggregate: ChallengeReward | None

    @property
    def completed(self):
        return tuple(entry for entry in self.challenges if entry.checked)


class MissionPresentations:
    def __init__(self, root: Path, challenges):
        catalog = json.loads((root / "data/physical_pickups.json").read_text(encoding="utf-8"))
        if catalog["schema"] != 2:
            raise ValueError("unsupported physical presentation catalog")
        self.pickups = {key: frozenset(row["pickups"]) for key, row in catalog["stages"].items()}
        self.maps = {key: row["runtime_map"] for key, row in catalog["stages"].items()}
        self.encounters = _encounter_locations(root)
        self.authored = tuple(sorted(challenges, key=lambda entry: entry["order"]))
        self.challenges = tuple(entry for entry in challenges
                                if entry["signal"].get("kind") == "physical_event_equivalent")

    @staticmethod
    def reward(entry, facts, placements, *, mastery=False):
        """Active Location identity, exact placement and server ACK; no inventory inference."""
        location_id = entry["location_id"]
        placement = placements.get(location_id)
        text = (
            (player_text("game.menu", "mastery_unknown") if mastery else player_text("notifications", "reward_unknown"))
            if placement is None
            else item_receipt_text(
                placement["item_name"],
                local=placement["local"],
                trap=bool(placement["classification"] & ITEM_CLASSIFICATION_TRAP),
                recipient_name=placement["recipient_name"],
                uppercase=False,
            )
        )
        return ChallengeReward(location_id, entry["signal"].get("unlockable", ""),
                               entry["name"].split(" - Mission Challenge - ")[-1],
                               location_id in facts.checked, text)

    def challenge_snapshot(self, namespace, generation, stage_id, *, revealed,
                           facts, placements, aggregates):
        runtime_map = self.maps.get(stage_id, "")
        if not revealed or not facts.checked_ready:
            return ChallengePresentation(namespace, generation, runtime_map, False, (), None)
        entries, authored = self._stage_challenge_entries(stage_id, facts)
        mission_key = authored["mission_key"] if authored else None
        aggregate = next((entry for entry in aggregates if entry["mission_key"] == mission_key
                          and entry["location_id"] in facts.server_locations), None)
        return ChallengePresentation(namespace, generation, runtime_map, True,
            tuple(self.reward(entry, facts, placements) for entry in entries),
            replace(self.reward(aggregate, facts, placements),
                    unlockable=authored["signal"]["unlockable"].rsplit("/", 1)[0]) if aggregate else None)

    def mastery_snapshot(self, facts, placements, masteries):
        return tuple(replace(self.reward(entry, facts, placements, mastery=True), unlockable=entry["gameplay_perk"]) for entry in masteries
                     if entry["location_id"] in facts.server_locations)

    def encounter_snapshot(self, stage_id, *, revealed, facts, placements, pending):
        if not revealed or not facts.checked_ready:
            return ()
        return tuple(replace(self.reward(entry, facts, placements),
                     checked=entry['location_id'] in facts.checked | pending)
                     for entry in self.encounters.get(stage_id, ())
                     if entry['location_id'] in facts.server_locations)

    def snapshot(self, namespace, generation, stage_id, *, revealed, facts, pending):
        """Unknown and unrevealed fields have no denominator or challenge metadata."""
        runtime_map = self.maps.get(stage_id, "")
        if not revealed or not facts.checked_ready or stage_id not in self.pickups:
            return MissionPresentation(namespace, generation, runtime_map, None, None, ())
        active = self.pickups[stage_id] & facts.server_locations
        observed = facts.checked | pending
        challenges = []
        for entry in self.challenges:
            if entry["mission_key"] != stage_id or entry["location_id"] not in facts.server_locations:
                continue
            signal = entry["signal"]
            required = int(signal.get("required_count", 1))
            sources = frozenset(signal["physical_location_ids"]) & active
            if len(sources) < required:
                continue
            challenges.append(PhysicalChallenge(signal["unlockable"],
                                                min(required, len(sources & observed)), required,
                                                entry["location_id"] in facts.checked))
        return MissionPresentation(namespace, generation, runtime_map,
                                   len(active & observed), len(active), tuple(challenges))


    def _stage_challenge_entries(self, stage_id, facts):
        entries = tuple(entry for entry in self.authored if entry["mission_key"] == stage_id
                        and entry["location_id"] in facts.server_locations)
        authored = next((entry for entry in self.authored if entry["mission_key"] == stage_id), None)
        return entries, authored


class PresentationHints:
    def __init__(self, namespace, state, persist):
        self.namespace, self.state, self.persist = namespace, state, persist
        self.sent = {}

    def _observe_intents(self, intents, allowed):
        pending = {value for value in self.state.get("observed", [])
                   if type(value) is int and value in allowed}
        observed = {value for value in intents if type(value) is int and value in allowed}
        if observed - pending:
            pending.update(observed)
            self.state["observed"] = sorted(pending)
            self.persist()
        return pending

    async def synchronize(self, intents, allowed, confirmed, send, hints_key):
        pending = self._observe_intents(intents, allowed)
        now = time.monotonic()
        if send is None:
            return
        requested = sorted(value for value in pending - confirmed
                           if now - self.sent.get(value, -30) >= 30)
        if not requested:
            return
        await send([{"cmd": "LocationScouts", "locations": requested, "create_as_hint": 2},
                    {"cmd": "Get", "keys": [hints_key]}])
        self.sent.update((value, now) for value in requested)


def valid_fast_travel_delivery_key(value):
    if not isinstance(value, list | tuple) or len(value) != 3:
        return None
    if not all(isinstance(component, str) and component for component in value):
        return None
    if not re.fullmatch(r"[0-9]+:[0-9]+", value[2]):
        return None
    return tuple(value)


class FastTravel:
    def __init__(self, catalog_maps, map_keys, mission_ids, logger):
        self._catalog_maps = dict(catalog_maps)
        self._map_keys = frozenset(map_keys)
        self._mission_ids = dict(mission_ids)
        self._logger = logger
        self._submitted = {}
        self.invalidate()

    def invalidate(self, *, clear_submitted=False):
        self._epoch_state = None
        self._eligibility = None
        self._last_transition = None
        if clear_submitted:
            self._submitted.clear()

    def rebind(self):
        self._submitted.clear()

    @property
    def eligibility(self):
        return self._eligibility

    def _fast_travel_transition(self, event, *, reason=None, trigger=None):
        """Emit one lifecycle transition for current Fast Travel epoch."""
        state = getattr(self, "_epoch_state", None)
        if not isinstance(state, dict):
            state = {}
        signature = (
            event,
            state.get("identity"),
            state.get("map_key"),
            state.get("epoch"),
            reason,
        )
        if signature == getattr(self, "_last_transition", None):
            return
        self._last_transition = signature
        if self._epoch_state is state:
            state["status"] = event.lower()
        if reason is not None:
            state["pending_reason"] = reason
        self._logger.info(
            "[FastTravel] %s identity=%s map=%s epoch=%s completed_before_epoch=%s reason=%s trigger=%s",
            event,
            state.get("identity") or "<none>",
            state.get("map_key") or "<none>",
            state.get("epoch") or "<none>",
            state.get("completed_before_epoch", False),
            reason or "<none>",
            trigger or "<none>",
        )

    def _fast_travel_snapshot_mismatch(self, snapshot, map_identity, room_identity):
        """Reject epoch work when room, load, or accepted map identity changed."""
        identity, map_key, epoch = snapshot
        if identity != room_identity:
            return "identity_mismatch"
        accepted = map_identity.cached_marker
        if not isinstance(accepted, Mapping):
            return "map_unavailable"
        if accepted.get("gameplay_epoch") != epoch:
            return "map_epoch_mismatch"
        accepted_map_key = accepted.get("map_key")
        accepted_runtime_map = canonical_map_name(accepted.get("runtime_map", ""))
        canonical_key = catalog_map_key(self._catalog_maps, accepted_runtime_map)
        if not isinstance(accepted_map_key, str):
            accepted_map_key = canonical_key
        if accepted_map_key != canonical_key:
            return "map_identity_mismatch"
        if accepted_map_key != map_key:
            return "map_mismatch"
        return None

    async def _submit_unlock(self, trigger, state, identity, map_key, epoch, delivery_key, state_key, send):
        now = time.monotonic()
        retry_deadline = state.get("retry_deadline")
        retry_waiting = isinstance(retry_deadline, int | float) and now < retry_deadline
        if retry_waiting:
            state["status"] = "pending"
            return False
        if retry_deadline is None:
            self._fast_travel_transition("READY", trigger=trigger)
        command = "ai_ScriptCmdEnt ap_fast_travel_unlock activate"
        if not await send(
            command,
            coalesce_key=stable_command_key(
                "fast-travel", identity, map_key, epoch
            ),
            already_queued_ok=True,
            state_key=state_key,
            materialization_lease=epoch,
            execution_class=MAP_ENTITY_SAFE,
            operation=FAST_TRAVEL_UNLOCK,
        ):
            retry_attempt = state.get("retry_attempt", 0)
            if isinstance(retry_attempt, bool) or not isinstance(retry_attempt, int):
                retry_attempt = 0
            retry_attempt += 1
            backoff = min(
                FAST_TRAVEL_RETRY_MAX_SECONDS,
                FAST_TRAVEL_RETRY_BASE_SECONDS
                * (2 ** min(retry_attempt - 1, 3)),
            )
            state["retry_attempt"] = retry_attempt
            state["retry_deadline"] = now + backoff
            self._fast_travel_transition("RETRY", reason="queue_unavailable", trigger=trigger)
            return False
        state["retry_deadline"] = None
        state["retry_attempt"] = 0
        self._submitted[delivery_key] = time.time()
        self._fast_travel_transition("COMMAND_QUEUED_UNVERIFIED", trigger=trigger)
        return True

    async def reconcile(self, trigger, *, map_identity, room_identity, state_key, runtime_ready, item_ready, rpc_ready, send):
        """Activate native Fast Travel once per room/map/load epoch."""
        snapshot = getattr(self, "_eligibility", None)
        state = getattr(self, "_epoch_state", None)
        if not isinstance(state, dict):
            self._fast_travel_transition("PENDING", reason="epoch_unavailable", trigger=trigger)
            return False
        if not isinstance(snapshot, tuple) or len(snapshot) != 3:
            if state.get("completed_before_epoch"):
                self._fast_travel_transition(
                    "PENDING", reason=state.get("ineligible_reason") or "snapshot_unavailable", trigger=trigger
                )
            return False
        identity, map_key, epoch = snapshot
        delivery_key = valid_fast_travel_delivery_key((identity, map_key, epoch))
        if delivery_key is None:
            self._fast_travel_transition(
                "PENDING", reason="malformed_epoch", trigger=trigger
            )
            return False
        snapshot_mismatch = self._fast_travel_snapshot_mismatch(snapshot, map_identity, room_identity)
        if snapshot_mismatch:
            self._fast_travel_transition(
                "PENDING", reason=snapshot_mismatch, trigger=trigger
            )
            return False
        if delivery_key in self._submitted:
            self._fast_travel_transition("COMMAND_QUEUED_UNVERIFIED", trigger=trigger)
            return False
        if not self._ready_for_unlock(runtime_ready, item_ready, rpc_ready, trigger):
            return False
        return await self._submit_unlock(trigger, state, identity, map_key, epoch, delivery_key, state_key, send)

    def _ineligible_reason(self, identity, map_key, mission_id, history_available, completed_before_epoch):
        ineligible_reason = None
        if not identity:
            ineligible_reason = "room_identity_unavailable"
        elif map_key not in self._map_keys or mission_id is None:
            ineligible_reason = "map_not_supported"
        elif not history_available:
            ineligible_reason = "server_history_unavailable"
        elif not completed_before_epoch:
            ineligible_reason = "not_completed_before_epoch"
        return ineligible_reason

    def _same_capture(self, marker_data, room_identity, epoch):
        existing = getattr(self, "_epoch_state", None)
        if (
            isinstance(existing, dict)
            and existing.get("identity") == room_identity
            and existing.get("epoch") == epoch
            and existing.get("map_key") == marker_data.get("map_key")
        ):
            # Reconnect/history refresh may complete this first visit. Vanilla
            # endpoint targets own its unlock; AP early unlock waits for re-entry.
            return True
        return False

    def capture(self, map_identity, room_identity, checked, marker_data=None):
        """Freeze eligibility at epoch entry; later checks cannot create a replay."""
        if not isinstance(marker_data, Mapping):
            marker_data = (
                map_identity.cached_marker
                or map_identity.pending_marker
            )
        if not isinstance(marker_data, Mapping):
            return None
        epoch = marker_data.get("gameplay_epoch")
        if self._same_capture(marker_data, room_identity, epoch):
            return self._eligibility
        if epoch is None:
            return None

        identity = room_identity
        runtime_map = canonical_map_name(marker_data.get("runtime_map", ""))
        map_key = catalog_map_key(self._catalog_maps, runtime_map)
        if marker_data.get("map_key") != map_key:
            return None
        mission_id = self._mission_ids.get(map_key)
        history_available = isinstance(checked, set | frozenset | list | tuple)
        completed_before_epoch = bool(history_available and mission_id in checked)
        ineligible_reason = self._ineligible_reason(identity, map_key, mission_id, history_available, completed_before_epoch)

        self._epoch_state = {
            "identity": identity,
            "map_key": map_key,
            "epoch": epoch,
            "completed_before_epoch": completed_before_epoch,
            "ineligible_reason": ineligible_reason,
            "status": "epoch",
            "pending_reason": None,
            "retry_attempt": 0,
            "retry_deadline": None,
        }
        self._eligibility = (
            (identity, map_key, epoch)
            if not ineligible_reason
            else None
        )
        self._last_transition = None
        self._fast_travel_transition("EPOCH")
        if ineligible_reason:
            self._fast_travel_transition("INELIGIBLE", reason=ineligible_reason)
        return self._eligibility

    def _ready_for_unlock(self, runtime_ready, item_ready, rpc_ready, trigger):
        if not runtime_ready:
            self._fast_travel_transition("PENDING", reason="level_not_ready", trigger=trigger)
            return False
        if not item_ready:
            self._fast_travel_transition("PENDING", reason="item_state_unavailable", trigger=trigger)
            return False
        if not rpc_ready:
            self._fast_travel_transition("PENDING", reason="rpc_not_ready", trigger=trigger)
            return False
        return True


class GoalProgress:
    def __init__(self, final_map, cultist_map, logger):
        self._final_map, self._cultist_map, self._logger = final_map, cultist_map, logger
        self._state = {}
        self._state_key = None
        self._generation = 0
        self._sent = self._in_flight = False
        self._candidate = None
        self._candidate_revision = 0
        self._last_details_mtime = None
        self._completion_states = {}
        self._pending_objectives = None
        self._diagnostic = None
        self._diagnostic_time = 0
        self._diagnostic_count = 0

    @property
    def sent(self):
        return self._sent

    @property
    def candidate(self):
        return MappingProxyType(dict(self._candidate)) if self._candidate is not None else None

    @property
    def cultist_path(self):
        return self._state.get("cultist_autosave_path")

    def invalidate_publication(self):
        self._generation += 1
        self._in_flight = False
        self._diagnostic = None
        self._diagnostic_count = 0

    def bind(self, state_key, state):
        self.invalidate_publication()
        if self._state_key != state_key:
            self._sent = False
            self.clear_candidate("room_changed")
        self._state_key, self._state = state_key, state
        state.setdefault("goal_sent", False)

    def connected(self, persist):
        self.invalidate_publication()
        self._sent = False
        self._pending_objectives = None
        self._state["goal_sent"] = False
        persist()

    def candidate_token(self):
        return self._generation, self._candidate_revision

    def acknowledge_candidate(self, token):
        if token == self.candidate_token():
            self.clear_candidate("publisher_acknowledged")

    def candidate_slot_allowed(self, authoritative, slot):
        if self._candidate is not None and authoritative and slot and slot != self._candidate["slot"]:
            self.clear_candidate("different_authoritative_slot")
            return False
        return True

    def observe_candidate(self, selected, details):
        candidate = self._candidate
        if candidate is None:
            return "invalidated"
        if selected is not None and details and selected.slot_directory == candidate["slot"]:
            token = details.get("_mtime_ns", selected.mtime_ns)
            if str(details.get("_path")) == candidate["details_path"] and token is not None and int(token) > candidate["details_token_at_arm"]:
                if details.get("completed") != "1":
                    self.clear_candidate("fresh_incomplete_details")
                    return "invalidated"
                return "publish"
        return "pending"

    def _diagnose(self, source, objective_ids, facts, admitted, admission_missing, decision, threshold_ids, threshold):
        missing = objective_ids - threshold_ids - facts.checked
        count = len(threshold_ids & facts.checked)
        diagnostic = (objective_ids, missing, facts.checked_ready, facts.connected,
                      admitted, tuple(admission_missing), self._sent, self._in_flight, decision, count, threshold)
        self._diagnostic_count += 1
        now = monotonic()
        if diagnostic == self._diagnostic and now - self._diagnostic_time < 30:
            return
        self._logger.info(
            "[Goal] EVALUATION source=%s evaluations=%s checked_ready=%s connected=%s "
            "admitted=%s admission_missing=%s objective_count=%s objective_ids=%s "
            "missing_count=%s missing=%s sent=%s in_flight=%s decision=%s threshold_count=%s threshold=%s",
            str(source)[:160], self._diagnostic_count, facts.checked_ready, facts.connected,
            admitted, list(admission_missing)[:32], len(objective_ids), sorted(objective_ids)[:32],
            len(missing), sorted(missing)[:32], self._sent, self._in_flight, decision, count, threshold,
        )
        self._diagnostic, self._diagnostic_time, self._diagnostic_count = diagnostic, now, 0

    def _publication_block(self, admitted, facts):
        if not admitted:
            return "campaign_unavailable" if admitted is None else "campaign_not_admitted"
        if self._sent:
            return "already_sent"
        if self._in_flight:
            return "in_flight"
        if not facts.checked_ready:
            return "checked_not_ready"
        return None

    async def evaluate(self, source_description, objective_ids, facts: CheckObservation, publication: CheckPublicationPort,
                       *, admitted=True, admission_missing=(), threshold_ids=frozenset(), threshold=0):
        def diagnose(decision):
            self._diagnose(source_description, objective_ids, facts, admitted, admission_missing, decision, threshold_ids, threshold)

        decision = self._publication_block(admitted, facts)
        if decision:
            diagnose(decision)
            return self._sent if admitted else False
        checked_locations = facts.checked
        missing = frozenset(objective_ids - threshold_ids - checked_locations)
        count = len(threshold_ids & checked_locations)
        pending = (bool(objective_ids), missing, count, threshold)
        if pending != self._pending_objectives:
            self._pending_objectives = pending
            self._logger.info("[Goal] OBJECTIVES_OBSERVED contract_ready=%s missing=%s",
                              bool(objective_ids), sorted(missing))
        if not objective_ids or missing or count < threshold:
            diagnose("missing_objectives" if objective_ids else "objectives_unavailable")
            return False
        if not facts.connected:
            diagnose("disconnected")
            return False
        diagnose("send")
        return await self._publish_goal(source_description, objective_ids, publication)

    async def _publish_goal(self, source_description, objective_ids, publication):

        self._in_flight = True
        self._logger.info(
            "[Goal] CLIENT_GOAL_SEND source=%s objective_ids=%s",
            source_description,
            sorted(objective_ids),
        )
        generation = self._generation
        try:
            await publication.goal()
        except (OSError, RuntimeError, WebSocketException) as error:
            self._logger.warning("[Goal] CLIENT_GOAL_RESULT result=error error_type=%s current=%s",
                                 type(error).__name__, generation == self._generation)
            if generation != self._generation:
                return False
            self._in_flight = False
            raise
        if generation != self._generation:
            self._logger.info("[Goal] CLIENT_GOAL_RESULT result=stale")
            return False
        self._in_flight = False
        self._sent = True
        self._logger.info("[Goal] CLIENT_GOAL_RESULT result=returned")
        return True


    def clear_candidate(self, reason):
        candidate = self._candidate
        if candidate is not None:
            self._logger.info(
                "[Goal] FINAL_SIN_COMPLETION_CANDIDATE_CLEARED slot=%s load_epoch=%s reason=%s",
                candidate["slot"],
                candidate["load_epoch"],
                reason,
            )
        self._candidate = None
        self._candidate_revision += 1


    def arm_candidate(
        self, selected, details, runtime_map, load_epoch
    ):
        if (
            canonical_map_name(runtime_map)
            != canonical_map_name(self._final_map)
            or load_epoch is None
        ):
            return
        details_path = details.get("_path")
        details_token = details.get("_mtime_ns", selected.mtime_ns)
        if not details_path or details_token is None:
            return
        existing = self._candidate
        identity = (selected.slot_directory, load_epoch, str(details_path))
        if existing is not None and identity == (
            existing["slot"],
            existing["load_epoch"],
            existing["details_path"],
        ):
            return
        self._candidate_revision += 1
        self._candidate = {
            "slot": selected.slot_directory,
            "load_epoch": load_epoch,
            "details_path": str(details_path),
            "details_token_at_arm": int(details_token),
            "completed_at_arm": str(details.get("completed", "0")),
        }
        self._logger.info(
            "[Goal] FINAL_SIN_COMPLETION_CANDIDATE_ARMED slot=%s load_epoch=%s "
            "details_token=%s completed=%s",
            selected.slot_directory,
            load_epoch,
            details_token,
            details.get("completed", "0"),
        )


    def _observe_cultist_completion(self, details_path, persist):
        if self.cultist_path != details_path:
            self._state["cultist_autosave_path"] = details_path
            persist()
            self._logger.info(
                f"[Goal] Tracking Cultist Base completion from {details_path}."
            )
        return

    def observe_completion(self, details, persist):
        record_map = canonical_map_name(details.get("mapName", ""))
        if not record_map:
            return
        mtime = details.get("_mtime_ns")
        if mtime == self._last_details_mtime:
            return
        self._last_details_mtime = mtime

        details_path = details.get("_path")
        if not details_path:
            return

        is_completed = details.get("completed") == "1"
        key = (details_path, record_map)
        prev_status = self._completion_states.get(key)
        self._completion_states[key] = "1" if is_completed else "0"

        fresh_completion = (prev_status == "0" and is_completed)

        if record_map == self._cultist_map:
            self._observe_cultist_completion(details_path, persist)
            return

        if not fresh_completion:
            return None
        if record_map in {"e3m4_boss", "game/sp/e3m4_boss/e3m4_boss"}:
            return "final_sin_mission_complete", "Final Sin save fallback"
        if record_map in {"e1m4_boss", "game/sp/e1m4_boss/e1m4_boss"}:
            return "doom_hunter_base_mission_complete", "Doom Hunter Base save fallback"
        return None


def activity_events(previous, current, stage):
    if current is None:
        return []
    if previous is not None and (current["sequence"], current["status"]) == (previous["sequence"], previous["status"]):
        return []
    statuses = {1: "pending", 2: "done", 3: "failed"}
    status = statuses[current["status"]]
    events = []
    if status != "pending" and (previous is None or previous["sequence"] != current["sequence"]):
        events.append({"status": "pending", "stage": stage})
    events.append({"status": status, "stage": stage})
    return events


class Killsanity:
    def __init__(self, logger):
        self._logger = logger
        self._epoch = 0
        self._session = None
        self.namespace = None
        self.milestones = ()
        self._toast_range = (0, 0, 0)

    def _validate_counters(self, state):
        for key in ("total", "cursor", "source_gaps"):
            if type(state.get(key, 0)) is not int or not 0 <= state.get(key, 0) < 2**64:
                raise ValueError(f"Killsanity progress has invalid {key}")
        if (type(state.get("counter_abi", 2)) is not int or state.get("counter_abi", 2) != 2 or "history_lost" in state
                or (state.get("producer") is not None and state.get("counter_abi") != 2)):
            raise ValueError("Killsanity progress requires a new room with cumulative combat counters")

    def _validate_producer(self, state):
        producer = state.get("producer")
        if producer is not None and (
                not isinstance(producer, list) or len(producer) != 4
                or any(not isinstance(value, str) or not value for value in producer)):
            raise ValueError("Killsanity progress has invalid producer identity")
        if state.get("cursor", 0) and producer is None:
            raise ValueError("Killsanity cursor has no producer identity")

    def bind(self, session, namespace, milestones):
        self._epoch += 1
        self._session = session
        self.namespace = namespace
        self.milestones = tuple((row["threshold"], row["location_id"]) for row in milestones)
        self._toast_range = (0, 0, 0)
        state = session.get("killsanity", {})
        if not isinstance(state, dict):
            raise ValueError("Killsanity progress must be an object")
        self._validate_counters(state)
        self._validate_producer(state)

    def bind_resolved(self, session, namespace, plan):
        self.bind(session, namespace, resolved_rewards(plan))

    @property
    def enabled(self):
        return bool(self.milestones)

    @property
    def state(self):
        return self._session.get("killsanity", {}) if self._session is not None else {}

    @property
    def total(self):
        return self.state.get("total", 0)

    @property
    def cursor(self):
        return self.state.get("cursor", 0)

    def _commit_progress(self, candidate, previous, persist):
        self._session["killsanity"] = candidate
        committed = False
        try:
            persist()
            committed = True
        finally:
            if not committed:
                if previous:
                    self._session["killsanity"] = previous
                else:
                    self._session.pop("killsanity", None)

    def _next_total(self, snapshot, cursor, gaps, candidate):
        if snapshot["qualified_kills"] < cursor or snapshot["observation_gaps"] < gaps:
            raise ValueError("Combat producer counters moved backwards")
        if snapshot["observation_gaps"] > gaps:
            self._logger.warning("[Killsanity] Observation gap: source_gaps=%d; total contains observed kills only",
                                 snapshot["observation_gaps"])
        total = candidate.get("total", 0) + snapshot["qualified_kills"] - cursor
        if total >= 2**64:
            raise ValueError("Killsanity total exceeds the native counter range")
        return total

    def consume(self, snapshot, persist):
        previous = self.state
        candidate = deepcopy(previous)
        if snapshot["namespace_id"] != self.namespace:
            raise ValueError("Combat observation belongs to another room")
        if candidate.get("producer") != snapshot["producer"]:
            candidate.update(producer=snapshot["producer"], cursor=0, source_gaps=0)
        cursor, gaps = candidate.get("cursor", 0), candidate.get("source_gaps", 0)
        total = self._next_total(snapshot, cursor, gaps, candidate)
        candidate.update(counter_abi=2, total=total, cursor=snapshot["qualified_kills"],
                         source_gaps=snapshot["observation_gaps"])
        if candidate == previous:
            return ()
        self._commit_progress(candidate, previous, persist)
        crossed = [(threshold, location) for threshold, location in self.milestones
                   if previous.get("total", 0) < threshold <= self.total]
        if crossed:
            self._toast_range = (self._toast_range[0] or crossed[0][0], crossed[-1][0], self.milestones[0][0])
        return tuple(location for _, location in crossed)

    async def present(self, link, visible, run=asyncio.to_thread):
        epoch, notices = self._epoch, self._toast_range
        counter = self.counter()
        await run(link.publish_combat_progress, counter["total"], counter["target"] or 0,
                                *notices, visible and counter["target"] is not None)
        if epoch == self._epoch and notices == self._toast_range:
            self._toast_range = (0, 0, 0)

    def _due_locations(self, facts):
        due = [location for threshold, location in self.milestones
               if threshold <= self.total and location in facts.server_locations
               and location not in facts.checked and location not in facts.submitted]
        return due

    async def publish(self, facts, publication):
        if not facts.connected or not facts.checked_ready:
            return
        due = self._due_locations(facts)
        if not due:
            return
        epoch = self._epoch
        await publication.locations(due)
        if epoch == self._epoch:
            for location in due:
                publication.mark_submitted(location)

    def counter(self):
        target = next((threshold for threshold, _ in self.milestones if threshold > self.total), None)
        return {"total": self.total, "target": target}


def _navigation_plan(plan):
    access = {str(item_id): ACCESS_IDS[item_id] for item_id in plan.access_items}
    return {
        "mode": plan.mode, "sequence": list(plan.sequence), "starting_stages": list(plan.starting_stages),
        "master_level_stage_ids": list(plan.master_level_stage_ids),
        "goal_stage": plan.goal.stage_id, "goal_as_item": plan.goal_as_item, "access_items": access,
        "fixed_dash_completion_stage": plan.fixed_dash_completion_stage,
        "fortress_phase_thresholds": list(plan.fortress.phase_thresholds),
        "hub": {"id": "hub", "map": "game/hub/hub"},
    }


def _reward_wire(known, entries):
    entries = tuple(entries)
    if len(entries) > 17:
        raise ValueError("Native reward projection exceeds its authored catalog")
    wire = struct.pack("<II", known, len(entries))
    for kind, entry in entries:
        identity, name, text = entry.unlockable.encode("ascii"), entry.name.encode("utf8"), entry.text.encode("utf8")
        if len(identity) >= 80 or len(name) >= 128 or len(text) >= 512:
            raise ValueError("Native reward text exceeds its field capacity")
        wire += struct.pack("<III80s128s512s", entry.location_id, kind, entry.checked, identity, name, text)
    return wire


def _relative_difficulty_band(rating):
    base, expected, allowance = (float(rating[key]) for key in
                                  ("base_cr", "expected_player_cr", "skill_allowance"))
    if not all(math.isfinite(value) and 0 <= value <= 100
               for value in (base, expected, allowance)):
        raise ValueError("Native relative difficulty facts are invalid")
    # Relative contract uses frozen base, expected kit and allowance.
    deficit = round((base - expected - allowance) * 100)
    band = 1 + sum(deficit > cut for cut in (-1000, 0, 1000))
    return band | (round(expected * 100) + 1) << 8


def _difficulty_band(snapshot, row):
    rating = snapshot.get("ratings", {}).get(row["stage"]) if row["revealed"] else None
    band = 0
    if rating is not None and snapshot.get("ratings_version", 1) == 2:
        band = rating.get("skull_tier")
        if type(band) is not int or not 1 <= band <= 4:
            raise ValueError("Native intrinsic difficulty tier is invalid")
    elif rating is not None and "expected_player_cr" in rating:
        band = _relative_difficulty_band(rating)
    return band


def _summary_wire(snapshot, row, namespace):
    summary = snapshot.get("summaries", {}).get(row["stage"])
    band = _difficulty_band(snapshot, row)
    fields = bytes(292)
    if summary is not None:
        if summary.namespace != namespace or summary.runtime_map != row["map"]:
            raise RuntimeError("Native summary belongs to a different campaign view")
        fields = struct.pack("<IIII", 1 if summary.found is not None else 2,
                             summary.found or 0, summary.total or 0, band)
        if len(summary.challenges) > 3:
            raise ValueError("Native mission has more than three physical challenges")
        for challenge in summary.challenges:
            name = challenge.unlockable.encode("ascii")
            if len(name) >= 80:
                raise ValueError("Native challenge identity is too long")
            fields += struct.pack("<80sIII", name, challenge.found, challenge.required, challenge.checked)
        fields += bytes(92 * (3 - len(summary.challenges)))
    return fields


def _stage_rewards_wire(snapshot, row, namespace):
    reward = snapshot.get("challenge_rewards", {}).get(row["stage"])
    entries = [(1, entry) for entry in reward.challenges] if reward else []
    if reward and reward.aggregate:
        entries.append((2, reward.aggregate))
    entries.extend((4, entry) for entry in snapshot.get('encounter_rewards', {}).get(row['stage'], ()))
    if reward and (reward.namespace != namespace or reward.runtime_map != row["map"]):
        raise RuntimeError("Native rewards belong to a different campaign view")
    return _reward_wire(bool(reward and reward.known), entries)


def _campaign_projection(snapshot, namespace):
    rows = [
        (
            1,
            1 | 2 | 16,
            20 + snapshot["fortress_phase"],
            snapshot["hub_map"],
            player_text("notifications", "fortress_title"),
        )
    ]
    stage_ids = {1: "hub"}
    summaries = [bytes(292)]
    rewards = [_reward_wire(True, ((3, entry) for entry in snapshot.get("masteries", ())))]
    hub_summary = snapshot.get("hub_summary")
    if hub_summary is not None:
        summaries[0] = struct.pack("<IIII", 1 if hub_summary.found is not None else 2,
                                   hub_summary.found or 0, hub_summary.total or 0, 0) + bytes(276)
    for row in snapshot["rows"]:
        stage_id = STAGES[row["stage"]]["access_id"]
        stage_ids[stage_id] = row["stage"]
        flags = sum(bit for field, bit in (("revealed", 1), ("unlocked", 2), ("completed", 4),
                                          ("goal", 8), ("details_visible", 32), ("slayer_gate", 64),
                                          ("gate_key", 128), ("gate_complete", 256)) if row.get(field, False))
        rows.append((stage_id, flags, STAGES[row["stage"]]["native_index"], row["map"], row["title"].upper()))
        summaries.append(_summary_wire(snapshot, row, namespace))
        rewards.append(_stage_rewards_wire(snapshot, row, namespace))
    return rows, stage_ids, summaries, rewards


def _encounter_locations(root):
    names = json.loads((root / 'data/location_names.json').read_bytes())['locations']
    stages = json.loads((root / 'data/campaign_stages.json').read_bytes())
    return {stage['id']: tuple({'location_id': int(code), 'name': name,
        'signal': {'unlockable': f'encounter/{code}'}} for code, name in sorted(names.items())
        if name.startswith(stage['name'] + ' - ') and
        (' - Secret Encounter - ' in name or ' - Escalation Encounter Wave ' in name))
        for stage in stages if stage['map'].startswith(('game/dlc/', 'game/dlc2/'))}
