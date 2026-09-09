"""Fast Travel visit eligibility and publication state, consuming canonical map facts."""
from collections.abc import Mapping
from types import MappingProxyType
import re
import time

from doom_eap.contracts.runtime_context import canonical_map_name
from doom_eap.contracts.command_publication import stable_spool_id, MAP_ENTITY_SAFE, FAST_TRAVEL_UNLOCK

FAST_TRAVEL_RETRY_BASE_SECONDS = 1.0
FAST_TRAVEL_RETRY_MAX_SECONDS = 8.0


def valid_fast_travel_delivery_key(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
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
    def epoch_state(self):
        return MappingProxyType(dict(self._epoch_state)) if self._epoch_state is not None else None

    @property
    def eligibility(self):
        return self._eligibility

    def _catalog_map_key(self, runtime_map):
        normalized = canonical_map_name(runtime_map)
        matches = [key for key, value in self._catalog_maps.items() if value == normalized]
        return matches[0] if len(matches) == 1 else None

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
        canonical_key = self._catalog_map_key(accepted_runtime_map)
        if not isinstance(accepted_map_key, str):
            accepted_map_key = canonical_key
        if accepted_map_key != canonical_key:
            return "map_identity_mismatch"
        if accepted_map_key != map_key:
            return "map_mismatch"
        return None

    def reconcile(self, trigger, *, map_identity, room_identity, state_key, runtime_ready, item_ready, rpc_ready, send):
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
        if not runtime_ready:
            self._fast_travel_transition("PENDING", reason="level_not_ready", trigger=trigger)
            return False
        if not item_ready:
            self._fast_travel_transition("PENDING", reason="item_state_unavailable", trigger=trigger)
            return False
        if not rpc_ready:
            self._fast_travel_transition("PENDING", reason="rpc_not_ready", trigger=trigger)
            return False
        now = time.monotonic()
        retry_deadline = state.get("retry_deadline")
        retry_waiting = isinstance(retry_deadline, (int, float)) and now < retry_deadline
        if retry_waiting:
            state["status"] = "pending"
            return False
        if retry_deadline is None:
            self._fast_travel_transition("READY", trigger=trigger)
        command = "ai_ScriptCmdEnt ap_fast_travel_unlock activate"
        if not send(
            command,
            coalesce_key=stable_spool_id(
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

    def capture(self, map_identity, room_identity, checked, marker_data=None, *, refresh=False):
        """Capture server-history eligibility for current gameplay epoch."""
        if not isinstance(marker_data, Mapping):
            marker_data = (
                map_identity.cached_marker
                or map_identity.pending_marker
            )
        if not isinstance(marker_data, Mapping):
            return None
        epoch = marker_data.get("gameplay_epoch")
        existing = getattr(self, "_epoch_state", None)
        if (
            isinstance(existing, dict)
            and existing.get("epoch") == epoch
            and existing.get("map_key") == marker_data.get("map_key")
            and not refresh
        ):
            return getattr(self, "_eligibility", None)
        if epoch is None:
            return None

        identity = room_identity
        runtime_map = canonical_map_name(marker_data.get("runtime_map", ""))
        map_key = self._catalog_map_key(runtime_map)
        if marker_data.get("map_key") != map_key:
            return None
        mission_id = self._mission_ids.get(map_key)
        history_available = isinstance(checked, (set, frozenset, list, tuple))
        completed_before_epoch = bool(history_available and mission_id in checked)
        ineligible_reason = None
        if not identity:
            ineligible_reason = "room_identity_unavailable"
        elif map_key not in self._map_keys or mission_id is None:
            ineligible_reason = "map_not_supported"
        elif not history_available:
            ineligible_reason = "server_history_unavailable"
        elif not completed_before_epoch:
            ineligible_reason = "not_completed_before_epoch"

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

