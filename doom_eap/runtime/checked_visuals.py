"""Checked visual reconciliation owns per-load dedupe, local effects and retry state."""
from collections.abc import Mapping
from types import MappingProxyType
import time

from doom_eap.contracts.runtime_context import canonical_map_name
from doom_eap.contracts.command_publication import (
    stable_spool_id, valid_materialization_epoch, MAP_ENTITY_SAFE, CHECKED_VISUAL_HIDE,
)

AUTOMAP_CLEANUP_RETRY_BASE_SECONDS = 1.0
AUTOMAP_CLEANUP_RETRY_MAX_SECONDS = 8.0


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
        self._local_owned = set()
        self._submitted = set()

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
                self._local_owned.clear()
                self._submitted.clear()
            return None
        self._epoch = epoch
        if previous_epoch != epoch:
            self._retry.clear()
            self._status.clear()
            self._local_owned.clear()
            self._submitted.clear()
        return self._epoch

    def reconcile(self, trigger, *, map_identity, room_identity, state_key, checked, checked_ready, runtime_ready, rpc_ready, send):
        """Remove only isolated AP visuals for server-checked map locations."""
        marker = map_identity.cached_marker
        self.advance_epoch(marker.get("gameplay_epoch") if isinstance(marker, Mapping) else None)
        if not runtime_ready:
            return False
        marker = map_identity.cached_marker
        if not isinstance(marker, Mapping):
            return False
        epoch = marker.get("gameplay_epoch")
        if (
            not valid_materialization_epoch(epoch)
            or not valid_materialization_epoch(self._epoch)
            or epoch != self._epoch
        ):
            return False
        marker_map = canonical_map_name(marker.get("runtime_map", ""))
        current_map = canonical_map_name(map_identity.current_map or "")
        if not marker_map or not current_map or marker_map != current_map:
            return False
        map_name = marker_map
        map_key = self._catalog_map_key(map_name)
        if marker.get("map_key") != map_key:
            return False
        entries = [
            entry for entry in self._visuals.get(map_key or "", {}).values()
            if entry["classification"] == "visible_cleanup"
        ]
        if not entries:
            return False
        if not room_identity:
            self._automap_cleanup_transition(
                (epoch, "", map_name, ""),
                "PENDING",
                "room_identity_unavailable",
                trigger=trigger,
            )
            return False
        if not checked_ready or not isinstance(checked, (set, frozenset, list, tuple)):
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
        checked = set(checked)
        changed = False
        now = time.monotonic()
        for entry in sorted(entries, key=lambda item: item["location_id"]):
            location_id = entry["location_id"]
            if location_id not in checked:
                continue
            entity_name = entry["reconciliation_entity"]
            delivery_key = (room_identity, map_name, str(location_id))
            runtime_key = (epoch, *delivery_key)
            if (epoch, location_id) in self._local_owned:
                self._automap_cleanup_transition(
                    runtime_key,
                    "LOCAL_FLOW_OWNS_EFFECT",
                    "local_flow_owns_effect",
                    trigger=trigger,
                )
                continue
            if runtime_key in self._submitted:
                self._automap_cleanup_transition(
                    runtime_key,
                    "SUBMITTED",
                    "spool_already_submitted",
                    trigger=trigger,
                )
                continue
            retry = self._retry.setdefault(runtime_key, {"attempt": 0, "deadline": 0.0})
            if now < retry["deadline"]:
                self._automap_cleanup_transition(
                    runtime_key,
                    "RETRY_WAIT",
                    "queue_retry_backoff",
                    trigger=trigger,
                )
                continue
            command_id = stable_spool_id(
                "automap-cleanup",
                getattr(self, "_session", "session"),
                room_identity,
                map_name,
                location_id,
                self._epoch,
            )
            command = f"ai_ScriptCmdEnt {entity_name} activate"
            if not send(
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
                continue
            self._retry.pop(runtime_key, None)
            self._submitted.add(runtime_key)
            changed = True
            self._automap_cleanup_transition(
                runtime_key,
                "SUBMITTED",
                "spool_enqueued",
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
        self._local_owned.add((epoch, location_id))
        self._automap_cleanup_transition(
            (epoch, room_identity or "", marker.get("runtime_map", ""), str(location_id)),
            "LOCAL_FLOW_OWNS_EFFECT",
            "local_flow_owns_effect",
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
            "trigger=%s status=%s previous=%s spool_skip_reason=%s",
            delivery_key[2] if len(delivery_key) > 2 else "<unknown>",
            delivery_key[0] if delivery_key else "<unknown>",
            delivery_key[3] if len(delivery_key) > 3 else "<unknown>",
            trigger or "<none>",
            status,
            previous,
            reason if status != "COMMAND_QUEUED_UNVERIFIED" else "<none>",
        )

