"""Canonical lifecycle state and evidence policy with explicitly supplied inputs."""

from dataclasses import replace
import re
from types import MappingProxyType

from doom_eap.contracts.command_publication import build_materialization_epoch, valid_materialization_epoch
from doom_eap.contracts.runtime_context import (
    ContextResolution,
    MapEpochSnapshot, MapIdentitySnapshot, NativeLoadDecision, RuntimeContextSnapshot,
    canonical_map_name,
)


class RuntimeLifecycle:
    """Own accepted context state independently of discovery and feature effects."""

    def __init__(self, snapshot=None, *, map_identity=None):
        self._snapshot = snapshot if snapshot is not None else RuntimeContextSnapshot()
        self._last_transition_log = None
        self._map_epochs = MapEpochSnapshot()
        self._map_identity = map_identity if map_identity is not None else MapIdentitySnapshot()
        self._work_generation = 0
        self._evidence_rejections = {}

    def invalidate_work(self):
        self._work_generation += 1

    def capture_work(self):
        marker = self._map_identity.cached_marker or {}
        return self._work_generation, marker.get("gameplay_epoch"), marker.get("runtime_map")

    def work_is_current(self, token):
        return token == self.capture_work()

    def materialization_lease(self, marker_campaign, context_campaign=None):
        marker = self._map_identity.cached_marker
        if marker is None or marker.get("materialization_suspended"):
            return None
        lease = marker.get("gameplay_epoch")
        if not valid_materialization_epoch(lease) or lease != self._map_epochs.published_materialization_lease:
            return None
        if context_campaign is not None and marker_campaign != context_campaign:
            return None
        return lease

    def record_evidence_rejection(self, reason, marker_identity, evidence_identity):
        key = (str(reason), str(marker_identity), str(evidence_identity))
        if key not in self._evidence_rejections and len(self._evidence_rejections) >= 32:
            key = ("overflow", "<bounded>", "<bounded>")
        count = self._evidence_rejections.get(key, 0) + 1
        self._evidence_rejections[key] = count
        return (*key, count) if count in {1, 2, 4, 8, 16, 32} else None

    @property
    def map_identity(self):
        return self._map_identity

    def accept_marker(self, marker, evidence_epoch):
        self._map_identity = MapIdentitySnapshot(
            cached_marker={**marker, "evidence_epoch": evidence_epoch,
                           "materialization_suspended": False},
            current_map=marker["runtime_map"],
        )
        return self._map_identity.cached_marker

    def bind_materialization_evidence(self, evidence_epoch):
        self._map_identity = replace(
            self._map_identity,
            cached_marker={**self._map_identity.cached_marker,
                           "materialization_evidence_epoch": evidence_epoch,
                           "evidence_epoch": evidence_epoch},
        )

    def suspend_marker(self, evidence_epoch, reason):
        self._map_identity = replace(
            self._map_identity, current_map=None,
            cached_marker={**self._map_identity.cached_marker,
                           "materialization_suspended": True,
                           "suspended_evidence_epoch": evidence_epoch,
                           "suspension_reason": reason},
        )

    def stage_marker(self, marker):
        self._map_identity = MapIdentitySnapshot(
            pending_marker={**marker, "evidence_epoch": None},
        )

    def clear_map(self):
        self._map_identity = replace(self._map_identity, cached_marker=None, current_map=None)

    def clear_pending_marker(self):
        self._map_identity = replace(self._map_identity, pending_marker=None)

    def project_current_map(self, marker):
        self._map_identity = replace(
            self._map_identity, current_map=None if marker is None else marker["runtime_map"],
        )

    @staticmethod
    def resolve_context(marker_map, evidence_map, *, materialization_suspended, contexts_by_map):
        marker_context = (
            None if materialization_suspended or not isinstance(marker_map, str)
            else contexts_by_map.get(marker_map)
        )
        evidence_context = contexts_by_map.get(evidence_map) if isinstance(evidence_map, str) else None
        if marker_context is None:
            return ContextResolution(evidence_context, "save")
        rejection = None
        if evidence_context is not None and evidence_context.identity != marker_context.identity:
            rejection = (
                "accepted_marker_authority", marker_context.identity, evidence_context.identity,
            )
        elif evidence_map and evidence_context is None:
            rejection = (
                "accepted_marker_over_unrecognized_save", marker_context.identity, evidence_map,
            )
        return ContextResolution(marker_context, "map_marker", rejection)

    def classify_native_load(self, evidence, catalog_maps):
        """Decide whether supplied native evidence needs timestamp proof or binding."""
        if evidence is None or getattr(evidence, "state", None) != "gameplay":
            return None
        evidence_epoch = getattr(evidence, "epoch", None)
        if isinstance(evidence_epoch, bool) or not isinstance(evidence_epoch, int):
            return None
        runtime_map = canonical_map_name(getattr(evidence, "map_name", ""))
        cached = self._map_identity.cached_marker
        if cached is None or runtime_map != canonical_map_name(cached.get("runtime_map", "")):
            matches = [key for key, value in catalog_maps.items() if value == runtime_map]
            if len(matches) != 1 or getattr(evidence, "provisional", False):
                return None
            return NativeLoadDecision(
                "initialize" if cached is None else "transition",
                evidence_epoch, runtime_map, matches[0],
            )
        bound_epoch = cached.get("materialization_evidence_epoch")
        if bound_epoch is None:
            action = "bind"
        elif evidence_epoch == bound_epoch or evidence_epoch < bound_epoch:
            return None
        elif getattr(evidence, "provisional", False):
            action = "suspend"
        else:
            action = "reload"
        return NativeLoadDecision(action, evidence_epoch, runtime_map, cached.get("map_key"))

    def authored_timestamp_action(self, newest_mtime, process_started):
        if process_started is not None and newest_mtime < process_started:
            return "ignore"
        known_mtime = max(
            self._map_epochs.accepted_marker_mtime or 0,
            (self._map_identity.pending_marker or {}).get("mtime_ns", 0),
            (self._map_identity.cached_marker or {}).get("mtime_ns", 0),
        )
        return "native_fallback" if newest_mtime <= known_mtime else "parse"

    @staticmethod
    def parse_authored_marker(content, path, mtime_ns, catalog_maps, contexts_by_map):
        matches = list(re.finditer(
            r"AP_ACTIVE_MAP_V1\s+map_key=(\S+)\s+runtime_map=(\S+)\s+marker=(\S+)", content,
        ))
        if not matches:
            return None
        last_match = matches[-1]
        map_key = last_match.group(1).rstrip(";")
        runtime_map = canonical_map_name(last_match.group(2).rstrip(";"))
        marker = last_match.group(3).rstrip(";")
        if not runtime_map:
            return None
        context = contexts_by_map.get(runtime_map)
        matches = [key for key, value in catalog_maps.items() if value == runtime_map]
        if context is None or map_key not in context.map_keys or matches != [map_key]:
            return None
        if marker != f"AP_MAP_START_{map_key.upper()}":
            return None
        return {
            "map_key": map_key, "runtime_map": runtime_map, "marker": marker,
            "mtime_ns": mtime_ns, "path": path,
        }

    @staticmethod
    def authored_marker_proposal(marker, newest_mtime, evidence_mtime, evidence_epoch):
        return MappingProxyType({
            **marker,
            "native_gameplay_epoch": newest_mtime,
            "gameplay_epoch": build_materialization_epoch(newest_mtime, newest_mtime),
            "evidence_mtime_ns": evidence_mtime,
            "evidence_epoch": evidence_epoch,
            "materialization_evidence_epoch": (
                evidence_epoch
                if isinstance(evidence_epoch, int) and not isinstance(evidence_epoch, bool)
                else None
            ),
        })

    def native_marker_proposal(self, decision, evidence_mtime):
        """Build a marker only after explicit native timestamp proof; do not accept it."""
        epoch = build_materialization_epoch(decision.evidence_epoch, evidence_mtime)
        if epoch is None:
            return None
        cached = self._map_identity.cached_marker
        if decision.action == "reload":
            if epoch == cached.get("gameplay_epoch"):
                return None
            marker = {**cached, "secondary_materialization": True}
        else:
            return self.new_map_proposal(
                decision.map_key, decision.runtime_map, decision.evidence_epoch,
                evidence_mtime, epoch,
            )
        marker.update(
            native_gameplay_epoch=decision.evidence_epoch, gameplay_epoch=epoch,
            evidence_mtime_ns=evidence_mtime, evidence_epoch=decision.evidence_epoch,
            materialization_evidence_epoch=decision.evidence_epoch,
        )
        return MappingProxyType(marker)

    @staticmethod
    def new_map_proposal(map_key, runtime_map, evidence_epoch, evidence_mtime, materialization_epoch):
        """Construct an unaccepted map proposal after the caller's evidence gate."""
        return MappingProxyType({
            "map_key": map_key,
            "runtime_map": runtime_map,
            "marker": f"AP_MAP_START_{map_key.upper()}",
            "mtime_ns": evidence_mtime,
            "path": None,
            "native_gameplay_epoch": evidence_epoch,
            "gameplay_epoch": materialization_epoch,
            "evidence_mtime_ns": evidence_mtime,
            "evidence_epoch": evidence_epoch,
            "materialization_evidence_epoch": evidence_epoch,
            "secondary_materialization": False,
        })

    @property
    def map_epochs(self):
        return self._map_epochs

    def record_native_epoch(self, epoch):
        self._map_epochs = replace(self._map_epochs, native_gameplay_epoch=epoch)

    def record_lease_publication(self, epoch, published):
        self._map_epochs = replace(
            self._map_epochs, published_materialization_lease=epoch if published else None,
        )

    def observe_marker_timestamp(self, timestamp, evidence_epoch):
        if self._map_epochs.accepted_marker_mtime == timestamp:
            return False
        self._map_epochs = replace(
            self._map_epochs, accepted_marker_mtime=timestamp,
            accepted_marker_evidence_epoch=evidence_epoch,
        )
        return True

    @property
    def snapshot(self):
        return self._snapshot

    def bind_context(self, context):
        previous_campaign = self._snapshot.campaign
        self._snapshot = replace(
            self._snapshot, identity=context.identity, campaign=context.campaign,
            capabilities=context.capabilities,
        )
        if previous_campaign == "Unknown":
            return ("Unknown", context.campaign)
        if previous_campaign not in {"Unknown", context.campaign}:
            return (previous_campaign, context.campaign)
        return None

    def record_transition(self, transition, identity):
        self._snapshot = replace(self._snapshot, pending_transition=transition)
        signature = (*transition, identity)
        if signature == self._last_transition_log:
            return False
        self._last_transition_log = signature
        return True

    def complete_transition(self):
        self._snapshot = replace(self._snapshot, pending_transition=None)

    def clear_context(self, *, clear_log=False):
        self._snapshot = RuntimeContextSnapshot()
        if clear_log:
            self._last_transition_log = None
