from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import pickle
import re
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from copy import deepcopy
from ctypes import wintypes
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar

from websockets.exceptions import WebSocketException

from doom_eap.content import slot_data_contract
from doom_eap.contracts.check_observation import CheckObservation, CheckPublicationPort
from doom_eap.contracts.command_publication import build_materialization_epoch, valid_materialization_epoch
from doom_eap.contracts.core_distribution import version_key
from doom_eap.contracts.publisher_contracts import publisher_acknowledged
from doom_eap.contracts.runtime_context import (
    ContextResolution,
    DlcEvidence,
    MapEpochSnapshot,
    MapIdentitySnapshot,
    NativeLoadDecision,
    RuntimeContext,
    RuntimeContextSnapshot,
    canonical_map_name,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
OBSERVER_CONTRACT_PATH = REPO_ROOT / "data" / "observer_contracts.json"
logger = logging.getLogger(__name__)
TH32CS_SNAPPROCESS = 0x00000002
ERROR_NO_MORE_FILES = 18
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH = 260
_windows_process_probe_warning_emitted = False


def same_executable_path(actual, expected):
    try:
        return (os.path.normcase(os.path.abspath(actual)) == os.path.normcase(os.path.abspath(expected))
                or os.path.samefile(actual, expected))
    except (OSError, ValueError):
        return False


class PROCESSENTRY32W(ctypes.Structure):
    _fields_: ClassVar = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


def windows_game_processes() -> tuple[dict[str, object], ...] | None:
    """Return exact Windows game identities; None means observation failed."""
    api = _load_kernel32()
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    api.GetProcessTimes.restype = wintypes.BOOL
    api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.QueryFullProcessImageNameW.restype = wintypes.BOOL
    snapshot = api.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, INVALID_HANDLE_VALUE):
        return None
    rows = []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        if not api.Process32FirstW(snapshot, ctypes.byref(entry)):
            return () if ctypes.get_last_error() == ERROR_NO_MORE_FILES else None
        while True:
            if entry.szExeFile.casefold() == "doometernalx64vk.exe":
                row = _windows_game_identity(api, entry)
                if row is None:
                    return None
                rows.append(row)
            if not api.Process32NextW(snapshot, ctypes.byref(entry)):
                return tuple(rows) if ctypes.get_last_error() == ERROR_NO_MORE_FILES else None
    finally:
        api.CloseHandle(snapshot)


@dataclass(frozen=True)
class EvidencePolicy:
    requires_live_runtime: bool
    requires_authoritative_save: bool
    baseline_policy: str
    binding_scope: str
    completion_policy: str


def load_observer_contracts(path: Path = OBSERVER_CONTRACT_PATH) -> dict:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise ValueError("observer contract schema_version must be 1")
    policies = document.get("policies", {})
    for key, value in policies.items():
        try:
            EvidencePolicy(**value)
        except TypeError as error:
            raise ValueError(f"{key}: invalid evidence policy") from error
    for key, observer in document.get("observers", {}).items():
        if observer.get("policy") not in policies or not observer.get("strategies"):
            raise ValueError(f"{key}: invalid observer policy binding")
    return document


def doom_process_running() -> bool:
    executable = "doometernalx64vk.exe"
    if os.name == "nt":
        return _windows_process_running(executable)
    proc = Path("/proc")
    try:
        process_dirs = tuple(proc.iterdir())
    except OSError:
        return False
    for directory in process_dirs:
        if not directory.name.isdigit():
            continue
        for filename in ("comm", "cmdline"):
            try:
                value = (directory / filename).read_bytes().decode(
                    "utf-8", errors="ignore"
                )
            except OSError:
                continue
            if executable in value.lower():
                return True
    return False


class RuntimeObservationLease:
    """Proof that save evidence belongs to live gameplay in this bridge run."""

    def __init__(
        self,
        *,
        process_probe: Callable[[], bool] = doom_process_running,
        started_ns: int | None = None,
    ):
        self.process_probe = process_probe
        self.started_ns = int(started_ns if started_ns is not None else time.time_ns())
        self.gameplay_loaded_ns: int | None = None
        self.last_block_reason: str | None = None

    def observe_gameplay_loaded(self, proof_mtime_ns: int | None = None) -> None:
        observed = int(proof_mtime_ns if proof_mtime_ns is not None else time.time_ns())
        if observed >= self.started_ns:
            self.gameplay_loaded_ns = observed

    def validate(
        self,
        *,
        evidence_mtime_ns: int,
        evidence_state: str,
        current_map: str,
    ) -> tuple[bool, str]:
        if not self.process_probe():
            return False, "game_not_running"
        if self.gameplay_loaded_ns is None:
            return False, "gameplay_not_loaded"
        if evidence_state != "gameplay":
            return False, "gameplay_not_loaded"
        if not current_map:
            return False, "map_unavailable"
        if evidence_mtime_ns < self.started_ns:
            return False, "stale_runtime_proof"
        if not str(current_map or "").replace("\\", "/").rstrip("/"):
            return False, "map_mismatch"
        return True, "live"

    def validate_mission_select(
        self,
        *,
        evidence_mtime_ns: int,
        evidence_state: str,
        current_map: str,
        mission_map: str,
        save_mtime_ns: int,
    ) -> tuple[bool, str]:
        """Validate a replay without trusting stale campaign game.details."""
        if not self.process_probe():
            return False, "game_not_running"
        if self.gameplay_loaded_ns is None:
            return False, "gameplay_not_loaded"
        if evidence_state != "gameplay":
            return False, "gameplay_not_loaded"
        if evidence_mtime_ns < self.started_ns:
            return False, "stale_runtime_proof"
        if save_mtime_ns and save_mtime_ns < self.started_ns:
            return False, "mission_select_save_not_fresh"
        def canonical(value):
            return str(value or "").replace("\\", "/").rstrip("/")
        if not canonical(current_map) or canonical(current_map) != canonical(mission_map):
            return False, "mission_select_map_mismatch"
        return True, "mission_select_live"


def observer_registry_revision(registry_path: Path | Mapping) -> str:
    document = (
        registry_path
        if isinstance(registry_path, Mapping)
        else json.loads(registry_path.read_text(encoding="utf-8"))
    )
    evidence = {
        "schema_version": document.get("schema_version"),
        "weapon_masteries": document.get("weapon_masteries", []),
        "mission_challenges": document.get("mission_challenges", []),
        "all_mission_challenges": document.get("all_mission_challenges", []),
        "policy": SAVE_OBSERVER_POLICY.__dict__,
        "observer_contracts": OBSERVER_CONTRACTS,
    }
    encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def ignore(*args, **kwargs):
    pass


@dataclass(frozen=True)
class LevelReadyJob:
    epoch: str
    generation: int
    source_path: object
    settle_inventory: bool


@dataclass(frozen=True)
class LevelReadyAdmission:
    job: LevelReadyJob | None = None
    block: str | None = None


class LevelReady:
    def __init__(self, logger):
        self._logger = logger
        self._generation = 0
        self._pending = {}
        self._completed = set()
        self._settled = set()
        self._active = {}
        self._prepared = {}
        self._wait_reasons = {}
        self._pending_signature = None

    @property
    def pending(self):
        return MappingProxyType(dict(self._pending))

    @property
    def completed(self):
        return frozenset(self._completed)

    def invalidate(self):
        self._generation += 1
        self._active.clear()
        self._prepared.clear()
        self._wait_reasons.clear()

    def queue(self, epoch, path):
        if valid_materialization_epoch(epoch) and epoch not in self._completed:
            self._pending.setdefault(epoch, path)

    def can_start(self, epoch):
        return isinstance(epoch, str) and epoch in self._pending and epoch not in self._active

    def observe_readiness(self, epoch, runtime_ready, evidence_state):
        if runtime_ready:
            return True
        reason = "native_gameplay_unsafe" if evidence_state == "gameplay" else "evidence_not_gameplay"
        signature = (epoch, reason, evidence_state)
        if signature != self._pending_signature:
            self._pending_signature = signature
            self._logger.info("[RPC] LEVEL_READY_PENDING epoch=%s reason=%s state=%s",
                              epoch, reason, evidence_state or "unavailable")
            self._logger.info("[MAP] RUNTIME_EFFECTS_PENDING reason=%s state=%s",
                              reason, evidence_state or "unavailable")
        return False

    def start(self, epoch, context, use_dlc, dlc_evidence, fallback_path):
        if use_dlc and context is not None and context.campaign != "Base" and dlc_evidence.blocks_enabled:
            self._logger.info("[Context] LEVEL_READY_PENDING reason=dlc_missing evidence=%s", dlc_evidence.report())
            return LevelReadyAdmission(block=dlc_evidence.reason)
        job = LevelReadyJob(epoch, self._generation, self._pending.get(epoch) or fallback_path,
                            context is not None and context.campaign != "Base" and epoch not in self._settled)
        self._active[epoch] = job
        self._pending_signature = None
        if epoch not in self._prepared:
            self._logger.info("[RPC] LEVEL_READY_EXECUTE epoch=%s", epoch)
        return LevelReadyAdmission(job=job)

    def needs_preparation(self, job, step):
        return self.is_current(job) and step not in self._prepared.get(job.epoch, ())

    def prepared(self, job, step):
        if self.is_current(job):
            self._prepared.setdefault(job.epoch, set()).add(step)
            self._wait_reasons.get(job.epoch, {}).pop(step, None)
            self._logger.info("[RPC] LEVEL_READY_ADVANCE epoch=%s domain=%s", job.epoch, step)

    def wait(self, job, reason, *, domain="inventory"):
        if self.is_current(job):
            reasons = self._wait_reasons.setdefault(job.epoch, {})
            if reasons.get(domain) != reason:
                reasons[domain] = reason
                self._logger.info("[Context] LEVEL_READY_PENDING epoch=%s domain=%s reason=%s", job.epoch, domain, reason)

    def is_current(self, job):
        return job.generation == self._generation and self._active.get(job.epoch) is job

    def observe_resume(self, job, map_identity, runtime_ready, *, settled=False):
        if not self.is_current(job):
            return False
        marker = map_identity.cached_marker
        if not runtime_ready or marker is None or marker.get("gameplay_epoch") != job.epoch:
            reason = "inventory_settle_invalidated" if settled else "level_ready_invalidated"
            self._logger.info("[Context] LEVEL_READY_PENDING reason=%s epoch=%s", reason, job.epoch)
            return False
        if settled:
            self._settled.add(job.epoch)
        return True

    def complete(self, job):
        self._pending.pop(job.epoch, None)
        self._completed.add(job.epoch)
        self._prepared.pop(job.epoch, None)
        self._wait_reasons.pop(job.epoch, None)
        self._logger.info("[RPC] LEVEL_READY_COMPLETE epoch=%s", job.epoch)

    def finish(self, job):
        if self.is_current(job):
            self._active.pop(job.epoch)


class PublisherDispatch:
    def __init__(self, publishers, goals, logger, observer=lambda: ignore):
        self._publishers, self._goals, self._logger = tuple(publishers), goals, logger
        self._observe = observer
        self._acknowledgements = {}
        self._in_flight = set()
        self._generation = 0
        self._facts = CheckObservation(frozenset(), frozenset(), frozenset(), False, False)

    def observe_protocol(self, facts):
        self._facts = facts

    def invalidate(self):
        self._generation += 1
        self._in_flight.clear()

    def bind(self, acknowledgements):
        self.invalidate()
        self._acknowledgements = acknowledgements

    def record_ack(self, publisher_key, effect_index, effect, persist):
        state = self._acknowledgements
        publisher_state = state.setdefault(publisher_key, {})
        publisher_state[str(effect_index)] = {
            "strategy": effect["strategy"],
            "location_id": effect.get("location_id"),
        }
        persist()


    async def _send_location_effect(self, publisher, effect_index, effect, effect_key, strategy, location_id, source_description, publication, persist, observe):

        if not self._facts.connected:
            return False
        self._in_flight.add(effect_key)
        self._logger.info(
            "[PUBLISHER] EFFECT_SEND key=%s effect=%s location_id=%s source=%s",
            publisher.key,
            strategy,
            effect.get("location_id", ""),
            source_description,
        )
        generation = self._generation
        try:
            await publication.location(location_id)
            observe("publisher_sent", locations=(location_id,))
        except (OSError, RuntimeError, ValueError, WebSocketException):
            if generation != self._generation:
                return False
            self._in_flight.discard(effect_key)
            raise
        if generation != self._generation:
            return False
        if strategy == "location_check":
            publication.mark_submitted(effect["location_id"])
            self._facts = replace(self._facts, submitted=self._facts.submitted | {effect["location_id"]})
            if effect["location_id"] in self._facts.checked:
                self._in_flight.discard(effect_key)
                self.record_ack(publisher.key, effect_index, effect, persist)
                self._logger.info(
                    "[PUBLISHER] EFFECT_ACK key=%s effect=location_check location_id=%s",
                    publisher.key,
                    effect["location_id"],
                )
                return True
            return False
        raise RuntimeError(f"unsupported publisher effect strategy: {strategy}")

    async def send_publisher_effect(
        self, publisher, effect_index, effect, source_description, publication, persist
    ):
        observe = self._observe()
        observe("publisher_effect", effect=effect, checked=self._facts.checked)
        strategy = effect["strategy"]
        effect_key = (publisher.key, effect_index)
        checked_locations = self._facts.checked
        if strategy == "preserved_native_target":
            return True
        if strategy == "location_check":
            location_id = effect["location_id"]
            if location_id in checked_locations:
                self._in_flight.discard(effect_key)
                self.record_ack(publisher.key, effect_index, effect, persist)
                self._logger.info(
                    "[PUBLISHER] EFFECT_ACK key=%s effect=location_check location_id=%s",
                    publisher.key,
                    location_id,
                )
                return True
            if location_id in self._facts.submitted or effect_key in self._in_flight:
                self._logger.info(
                    "[PUBLISHER] FALLBACK_SUPPRESSED key=%s reason=already_dispatched",
                    publisher.key,
                )
                return False
            if location_id not in self._facts.server_locations:
                self._logger.warning(
                    "[PUBLISHER] EFFECT_BLOCKED key=%s effect=location_check "
                    "location_id=%s reason=slot_absent",
                    publisher.key,
                    location_id,
                )
                return False
        elif strategy == "campaign_goal":
            return self._record_goal_effect(publisher, effect_index, effect, effect_key, persist)
        else:
            raise ValueError(f"unsupported publisher effect strategy: {strategy}")

        return await self._send_location_effect(publisher, effect_index, effect, effect_key, strategy, location_id, source_description, publication, persist, observe)


    async def execute(self, publisher, trigger_strategy, source_description, publication: CheckPublicationPort, persist):

        if publisher_acknowledged(
            publisher,
            self._facts.checked,
            self._goals.sent,
        ):
            self._logger.info(
                "[PUBLISHER] FALLBACK_SUPPRESSED key=%s reason=already_acknowledged",
                publisher.key,
            )
            return True
        self._logger.info(
            "[PUBLISHER] TRIGGER_OBSERVED key=%s strategy=%s",
            publisher.key,
            trigger_strategy,
        )
        generation = self._generation
        results = []
        for index, effect in enumerate(publisher.effects):
            try:
                results.append(
                    await self.send_publisher_effect(publisher, index, effect, source_description, publication, persist)
                )
            except (OSError, RuntimeError, ValueError, WebSocketException) as error:
                if generation != self._generation:
                    return False
                self._logger.error(
                    "[PUBLISHER] EFFECT_RETRY key=%s effect=%s error=%s",
                    publisher.key,
                    effect["strategy"],
                    error,
                )
                results.append(False)
            if generation != self._generation:
                return False
        return all(results)


    async def mission(self, location_id, source_description, publication, persist):
        matching = next(
            (
                publisher
                for publisher in self._publishers
                if any(
                    effect["strategy"] == "location_check"
                    and effect["location_id"] == location_id
                    for effect in publisher.effects
                )
            ),
            None,
        )
        if matching is None:
            return False
        generation = self._generation
        results = []
        for index, effect in enumerate(matching.effects):
            if effect["strategy"] == "preserved_native_target":
                continue
            results.append(
                await self.send_publisher_effect(matching, index, effect, source_description, publication, persist)
            )
            if generation != self._generation:
                return False
        return bool(results) and all(results)


    def _record_goal_effect(self, publisher, effect_index, effect, effect_key, persist):
        if self._goals.sent:
            self._in_flight.discard(effect_key)
            self.record_ack(publisher.key, effect_index, effect, persist)
            self._logger.info(
                "[PUBLISHER] EFFECT_ACK key=%s effect=campaign_goal",
                publisher.key,
            )
            return True
        self._in_flight.discard(effect_key)
        self._logger.info(
            "[PUBLISHER] EFFECT_FACT_ONLY key=%s effect=campaign_goal",
            publisher.key,
        )
        return True


def read_map_event(path: Path, marker: str) -> tuple[bool, str, str]:
    contents = path.read_text(encoding="utf-8", errors="replace")
    digest = hashlib.sha256(contents.encode("utf-8")).hexdigest()
    return marker in contents, contents, digest


def quarantine_malformed_event(
    path: Path,
    *,
    key: str,
    contents: str,
    sha256: str,
    quarantine_root: Path,
) -> tuple[Path, Path]:
    failed = quarantine_root / "failed"
    failed.mkdir(parents=True, exist_ok=True)
    stem = f"{int(time.time_ns())}_{sha256[:12]}_{path.name}"
    quarantined = failed / stem
    metadata = failed / f"{stem}.json"
    try:
        os.replace(path, quarantined)
    except OSError:
        quarantined.write_text(contents, encoding="utf-8", newline="")
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    metadata.write_text(
        json.dumps(
            {
                "publisher_key": key,
                "filename": path.name,
                "content": contents,
                "sha256": sha256,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return quarantined, metadata


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

    def _classify_map_transition(self, evidence, catalog_maps, runtime_map, cached, evidence_epoch):
        matches = [key for key, value in catalog_maps.items() if value == runtime_map]
        if len(matches) != 1 or getattr(evidence, "provisional", False):
            return None
        return NativeLoadDecision(
            "initialize" if cached is None else "transition",
            evidence_epoch, runtime_map, matches[0],
        )

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
            return self._classify_map_transition(evidence, catalog_maps, runtime_map, cached, evidence_epoch)
        bound_epoch = cached.get("materialization_evidence_epoch")
        if bound_epoch is None:
            action = "bind"
        elif evidence_epoch <= bound_epoch:
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
    def authored_marker_proposal(marker, newest_mtime, evidence_mtime, evidence_epoch, *, evidence_state="gameplay"):
        return MappingProxyType({
            **marker,
            "native_gameplay_epoch": newest_mtime,
            "gameplay_epoch": build_materialization_epoch(newest_mtime, newest_mtime),
            "evidence_mtime_ns": evidence_mtime,
            "evidence_epoch": evidence_epoch,
            "materialization_evidence_epoch": (
                evidence_epoch
                if evidence_state == "gameplay" and isinstance(evidence_epoch, int) and not isinstance(evidence_epoch, bool)
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


class SlotAdmissionError(ValueError):
    failure_domain = "room_package"

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def slot_schema_version() -> int:
    return _SLOT_IDENTITY["slot_data_schema_version"]


def load_slot_catalog() -> dict:
    directory = REPO_ROOT / "doom_eap/content"
    paths = (directory / "slot_catalog.json", directory / "projection_manifest.json",
             *(slot_data_contract.catalog_dependency_path(directory, name)
               for name in slot_data_contract.CATALOG_DEPENDENCIES))
    signature = tuple(_catalog_file_identity(path) for path in paths)
    return pickle.loads(_slot_catalog_snapshot(signature))


def admit_slot_data(slot_data, *, placements=None):
    """Select the packaged schema before any room preparation or state changes."""
    if not isinstance(slot_data, Mapping):
        raise SlotAdmissionError("slot_structure", "Room data must be an object.")
    expected = slot_schema_version()
    identity = slot_data.get("identity")
    if not isinstance(identity, dict) or type(identity.get("schema")) is not int or identity["schema"] != expected:
        version = _SLOT_IDENTITY["release_version"]
        apworld = _SLOT_IDENTITY["apworld_revision"]
        raise SlotAdmissionError("slot_schema", f"This client requires a DoomEAP {version} room "
                                 f"(slot_data schema {expected}). Generate a compatible room with APWorld {apworld}.")
    try:
        resolved = slot_data_contract.validate_slot_data(
            slot_data, load_slot_catalog(), placements=placements,
            supported_capabilities=SUPPORTED_SLOT_CAPABILITIES,
        )
    except slot_data_contract.ContractError as error:
        codes = {slot_data_contract.StructureError: "slot_structure", slot_data_contract.ReferenceError: "slot_reference",
                 slot_data_contract.DigestError: "slot_digest", slot_data_contract.ExpansionError: "slot_expansion",
                 slot_data_contract.PayloadSizeError: "slot_size"}
        raise SlotAdmissionError(codes.get(type(error), "slot_contract"), str(error)) from error
    _validate_resolved_compatibility(resolved.compatibility)
    return resolved


def classify_runtime_context(runtime_map: str, *, base_maps: Iterable[str] = ()) -> RuntimeContext | None:
    """Classify exact technical map identities; never infer from map catalogs."""
    if not isinstance(runtime_map, str):
        return None
    context = CONTEXT_BY_MAP.get(runtime_map)
    if context is not None:
        return context
    return None


def resolve_context_evidence(marker_map, evidence_map, *, materialization_suspended, base_maps=()):
    """Supply authored context data to the lifecycle selection policy."""
    return RuntimeLifecycle.resolve_context(
        marker_map, evidence_map, materialization_suspended=materialization_suspended,
        contexts_by_map=CONTEXT_BY_MAP,
    )


def evaluate_dlc_availability(base_dir: str | Path | None) -> DlcEvidence:
    """Use only filesystem proof; unknown never blocks DLC-enabled readiness."""
    if base_dir is None:
        return DlcEvidence("unknown", "Doom base path is unavailable", ())
    root = Path(base_dir).expanduser().resolve()
    base = root if root.name.lower() == "base" else root / "base"
    game_root = base.parent
    paths = (
        base / "game" / "dlc" / "e4m1_rig",
        base / "game" / "dlc2" / "e5m1_spear",
    )
    rendered = tuple(path.as_posix() for path in paths)
    authoritative = _installation_proven(base, game_root)
    if not authoritative:
        return DlcEvidence(
            "unknown",
            "DOOM Eternal installation root is not proven",
            rendered,
        )
    missing = tuple(
        path for path in paths
        if not path.is_dir()
        or not any(resource.is_file() for resource in path.glob("*.resources"))
    )
    if missing:
        return DlcEvidence("missing", "required DLC runtime directory is absent", rendered)
    return DlcEvidence("present", "required TAG runtime resources are present", rendered)


def validate_slot_contract(slot_data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the generated room identity and exact option contract."""
    admit_slot_data(slot_data)
    return dict(slot_data)


def dlc_contexts() -> tuple[RuntimeContext, ...]:
    return tuple(context for context in CONTEXTS if context.campaign != "Base")


def received_any_suit_upgrade(received_item_ids: Collection[int]) -> bool:
    """Whether persisted AP receipt makes the parent Suit tab eligible."""
    return not SUIT_PAGE_UNLOCKING_ITEM_IDS.isdisjoint(received_item_ids)


@dataclass(frozen=True)
class BootstrapOwnership:
    item_ids: frozenset[int]
    has_rune: bool


class Bootstrap:
    def __init__(self, logger):
        self._logger = logger
        self._last_rpc_map = None
        self.bind({"revision": BOOTSTRAP_REVISION, "actions": {}})

    def observe_supported_map(self, map_name):
        previous = self._last_rpc_map
        self._last_rpc_map = map_name
        if map_name is None or previous == map_name:
            return False
        if previous is not None:
            self._logger.info(
                f"[RPC] Map transition observed: {previous} -> {map_name}. "
                "The native gameplay gate controls safe execution."
            )
        return True

    def bind(self, state):
        self._state = state
        actions = state.setdefault("actions", {})
        state["revision"] = BOOTSTRAP_REVISION
        self._actions = actions

    @property
    def actions(self):
        return MappingProxyType(deepcopy(self._actions))

    def action_state(self, action_name, revision=None):
        revision = BOOTSTRAP_REVISION if revision is None else revision
        key = f"v{revision}:{action_name}"
        return MappingProxyType(deepcopy(self._actions.get(key, {
            "revision": revision, "action": action_name, "trigger": None, "status": "pending",
            "last_map": None, "timestamp": None, "reapply_on_map_load": False,
        })))

    def _action_state(self, action_name, revision=None):
        revision = BOOTSTRAP_REVISION if revision is None else revision
        state_key = f"v{revision}:{action_name}"
        state = self._actions.setdefault(state_key, {
            "revision": revision,
            "action": action_name, "trigger": None, "status": "pending",
            "last_map": None, "timestamp": None,
            "reapply_on_map_load": False,
        })
        return state

    def eligible(self, action_name, ownership):
        action = BOOTSTRAP_ACTIONS[action_name]
        if action["required_ap_ownership"] == "at_least_one_rune":
            return ownership.has_rune
        if action["required_ap_ownership"] == "at_least_one_suit_page_unlocker":
            return received_any_suit_upgrade(ownership.item_ids)
        if action["required_ap_ownership"] == "frag_grenade":
            return 7770011 in ownership.item_ids
        if action["required_ap_ownership"] == "ice_bomb":
            return 7770013 in ownership.item_ids
        return False

    def ineligibility_reason(self, action_name, ownership):
        if self.eligible(action_name, ownership):
            return "eligible"
        return {
            "rune_page": "needs AP Rune",
            "suit_page": "needs AP Suit Upgrade",
            "frag_acquired": "needs AP Frag Grenade",
            "ice_acquired": "needs AP Ice Bomb",
        }.get(action_name, "ownership predicate unmet")

    def command_id(self, action_name):
        action = BOOTSTRAP_ACTIONS[action_name]
        return f"bootstrap-v{action['revision']}-{action_name}"

    async def enqueue(self, action_name, trigger, *, ownership, current_map, state_key, send, persist):
        """Record dispatch only after the delivery backend accepts the action."""
        action = BOOTSTRAP_ACTIONS[action_name]
        state = self._action_state(action_name)
        non_replayable = {
            "queued",
            "delivered_effect_unknown",
            "delivered_effect_unknown_legacy",
            "confirmed",
            "skipped",
        }
        if state["status"] in non_replayable or not self.eligible(action_name, ownership):
            return False
        if canonical_map_name(current_map) not in {
            canonical_map_name(name) for name in action["maps_supported"]
        }:
            state.update(status="pending", trigger=trigger, timestamp=time.time())
            persist()
            return False
        command_id = self.command_id(action_name)
        if not await send(f"ai_ScriptCmdEnt {action['entity_name']} activate", coalesce_key=command_id,
                          already_queued_ok=True, state_key=state_key):
            state.update(status="retryable_failure", trigger=trigger, timestamp=time.time())
            persist()
            return False
        state.update(status="queued", trigger=trigger, last_map=current_map,
                     timestamp=time.time(), revision=action["revision"])
        persist()
        self._logger.info(
            "[Bootstrap] v%s entity=%s primitive_class=%s inherit=%s map=%s command=%s trigger=%s",
            action["revision"], action["entity_name"],
            BOOTSTRAP_STAT_PRIMITIVE["class"],
            BOOTSTRAP_STAT_PRIMITIVE["inherit"] or "<none>",
            current_map, command_id, trigger,
        )
        return True

    async def _enqueue_automatic_actions(self, trigger, ownership, map_identity, state_key, send, persist):
        for action_name, action in BOOTSTRAP_ACTIONS.items():
            if not action["automatic_enabled"]:
                continue
            if trigger in action["trigger_policy"]:
                if trigger == "on_supported_map_load" and self.action_state(action_name)["status"] != "pending":
                    continue
                await self.enqueue(action_name, trigger, ownership=ownership, current_map=map_identity.current_map,
                                   state_key=state_key, send=send, persist=persist)

    async def onboard(self, trigger, *, ownership, map_identity, state_key, item_ready, rpc_ready, send, persist):
        # Only explicitly enabled, ownership-gated acquisition actions run automatically.
        if not any(action["automatic_enabled"] and self.eligible(name, ownership)
                   for name, action in BOOTSTRAP_ACTIONS.items()):
            return
        if not item_ready or not rpc_ready:
            return
        await self._enqueue_automatic_actions(trigger, ownership, map_identity, state_key, send, persist)


def _load_kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(PROCESSENTRY32W),
    ]
    kernel32.Process32FirstW.restype = ctypes.c_int
    kernel32.Process32NextW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(PROCESSENTRY32W),
    ]
    kernel32.Process32NextW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    return kernel32


def _warn_windows_process_probe_once(message: str) -> None:
    global _windows_process_probe_warning_emitted
    if _windows_process_probe_warning_emitted:
        return
    _windows_process_probe_warning_emitted = True
    logger.warning("Windows process detection failed: %s", message)


def _windows_game_identity(api, entry):
    handle = api.OpenProcess(0x1000, False, entry.th32ProcessID)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        path = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(path))
        if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)) or not api.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)):
            return None
        return {"pid": int(entry.th32ProcessID), "path": path.value,
                     "created": (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime}
    finally:
        api.CloseHandle(handle)


def _windows_process_running(executable: str) -> bool:
    try:
        kernel32 = _load_kernel32()
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    except (AttributeError, OSError) as error:
        _warn_windows_process_probe_once(str(error))
        return False

    if snapshot in (None, INVALID_HANDLE_VALUE):
        _warn_windows_process_probe_once("CreateToolhelp32Snapshot")
        return False

    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    found = False
    try:
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            _warn_windows_process_probe_once("Process32FirstW")
        else:
            found = _find_windows_executable(kernel32, snapshot, entry, executable)
    except (AttributeError, OSError) as error:
        _warn_windows_process_probe_once(str(error))
        found = False
    finally:
        try:
            if not kernel32.CloseHandle(snapshot):
                _warn_windows_process_probe_once("CloseHandle")
                found = False
        except (AttributeError, OSError) as error:
            _warn_windows_process_probe_once(str(error))
            found = False
    return found


def _find_windows_executable(kernel32, snapshot, entry, executable):
    found = False
    expected = executable.casefold()
    while True:
        if entry.szExeFile.casefold() == expected:
            found = True
            break
        if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
            error = ctypes.get_last_error()
            if error not in (0, ERROR_NO_MORE_FILES):
                _warn_windows_process_probe_once(
                    f"Process32NextW error={error}"
                )
            break
    return found


def _catalog_file_identity(path):
    try:
        stat = path.stat()
        return path, stat.st_size, stat.st_mtime_ns
    except OSError:
        return path, None, None


@lru_cache(maxsize=1)
def _slot_catalog_snapshot(signature):
    catalog = slot_data_contract.load_slot_catalog(path=signature[0][0])
    # in-memory snapshots isolate mutable tables without rehashing assets.
    return pickle.dumps(catalog, protocol=pickle.HIGHEST_PROTOCOL)


def _validate_resolved_compatibility(compatibility):
    identity = _SLOT_IDENTITY
    expected = (identity["bridge_protocol_version"], identity["manifest_schema_version"],
                identity["session_mod_contract_revision"])
    actual = (compatibility.bridge_protocol, compatibility.manifest_schema_version, compatibility.mod_contract_revision)
    if actual != expected:
        raise SlotAdmissionError("slot_compatibility", "The room requires a different client contract.")
    try:
        supported = version_key(identity["release_version"]) >= version_key(compatibility.minimum_client_version)
        version_key(compatibility.apworld_version)
    except ValueError as error:
        raise SlotAdmissionError("slot_compatibility", "The room has an invalid compatibility version.") from error
    if not supported:
        raise SlotAdmissionError("slot_compatibility", "The room requires a newer client.")


def _load_registry() -> tuple[RuntimeContext, ...]:
    document = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    contexts = []
    for raw in document["contexts"]:
        contexts.append(RuntimeContext(
            identity=raw["identity"],
            campaign=raw["campaign"],
            runtime_maps=tuple(raw["runtime_maps"]),
            map_keys=tuple(raw["map_keys"]),
            capabilities=frozenset(raw["capabilities"]),
        ))
    return tuple(contexts)


def _installation_proven(base, game_root):
    authoritative = (
        base.is_dir()
        and (base / "game").is_dir()
        and (game_root / "DOOMEternalx64vk.exe").is_file()
    )
    return authoritative


def _action(action, ownership, description, triggers, *, automatic_enabled=False):
    stat = BOOTSTRAP_STAT_ALLOWLIST[action]
    return {
        "action": action,
        "revision": BOOTSTRAP_REVISION,
        "entity_name": f"{BOOTSTRAP_ENTITY_PREFIX}{action}",
        "maps_supported": SUPPORTED_MAPS,
        "trigger_policy": triggers,
        "reapply_policy": "once_per_slot_revision",
        "required_ap_ownership": ownership,
        "stat": stat,
        "effects": ((stat, 1),),
        "forbidden_effects": FORBIDDEN_EFFECT_TERMS,
        "status": "experimental",
        "automatic_enabled": automatic_enabled,
        "description": description,
    }


def _validate_action_identity(key, action, entity_names):
    if key not in BOOTSTRAP_STAT_ALLOWLIST:
        raise ValueError(f"Bootstrap action {key} is not allowlisted")
    if action.get("action") != key:
        raise ValueError("Bootstrap action IDs must be unique and canonical")
    if action.get("revision") != BOOTSTRAP_REVISION:
        raise ValueError(f"Bootstrap action {key} has an unexpected revision")
    if action.get("status") != "experimental" or action.get("automatic_enabled") is not (key == "ice_acquired"):
        raise ValueError(f"Bootstrap action {key} has an invalid runtime status")
    if action.get("entity_name") != f"{BOOTSTRAP_ENTITY_PREFIX}{key}":
        raise ValueError(f"Bootstrap action {key} has an unsafe entity name")
    if action["entity_name"] in entity_names:
        raise ValueError("Bootstrap entity names must be unique")


def _validate_acquisition_audit(key, expected_stat):
    audit = EQUIPMENT_ACQUISITION_STAT_AUDIT.get(key)
    if audit and (audit["stat"] != expected_stat or not audit["ui_loadout_only"]
                  or audit["progression_reader_found"] or audit["force_stat_required"]):
        raise ValueError(f"Bootstrap action {key} lacks a safe acquisition-stat audit")


def _validate_action_effect(key, action):
    if any(trigger not in TRIGGER_POLICIES for trigger in action.get("trigger_policy", ())):
        raise ValueError(f"Bootstrap action {key} has an unsupported trigger")
    if action.get("reapply_policy") not in REAPPLY_POLICIES:
        raise ValueError(f"Bootstrap action {key} has an unsupported reapply policy")
    expected_stat = BOOTSTRAP_STAT_ALLOWLIST[key]
    if action.get("stat") != expected_stat or action.get("effects") != ((expected_stat, 1),):
        raise ValueError(f"Bootstrap action {key} must contain exactly one allowlisted effect")
    _validate_acquisition_audit(key, expected_stat)


OBSERVER_CONTRACTS = load_observer_contracts()
SAVE_OBSERVER_POLICY = EvidencePolicy(
    **OBSERVER_CONTRACTS["policies"]["save_based"]
)
REGISTRY_PATH = REPO_ROOT / "data" / "runtime_contexts.json"
_SLOT_IDENTITY = json.loads(
    (REPO_ROOT / "data" / "content_identity.json").read_text(encoding="utf-8")
)
TAG_CAPABILITY = "tag_context_v1"
GOAL_CAPABILITIES = frozenset({"goal_events_v1", "goal_endpoint_events_v1"})
SUPPORTED_SLOT_CAPABILITIES = frozenset({
    "room_mod_v2", "slot_data_v6", "slot_data_v7", "unified_campaign_v1",
    "fortress_economy_v1", "progressive_blood_punch_v1", "enemy_recipe_seed_v1",
    "dlc_missions_v1", "goal_events_v1", "goal_endpoint_events_v1", "placement_scouts_v1",
    "randomize_dash_v1", "starting_inventory_v1", "starting_weapon_v1",
    "special_weapon_progression_v1", "ammo_refill_v1", "physical_options_v1",
    "room_options_v1", "cross_campaign_materialization_v1", "deathlink_mode_v1",
    slot_data_contract.ULTRA_NIGHTMARE_CAPABILITY,
    slot_data_contract.MASTER_LEVELS_CAPABILITY,
})
CONTEXTS = _load_registry()
CONTEXT_BY_IDENTITY = {context.identity: context for context in CONTEXTS}
CONTEXT_BY_MAP = {
    runtime_map: context
    for context in CONTEXTS
    for runtime_map in context.runtime_maps
}
BOOTSTRAP_REVISION = 2
BOOTSTRAP_ENTITY_PREFIX = "ap_bootstrap_v2_"
LEGACY_BOOTSTRAP_ENTITY_PREFIXES = ("ap_bootstrap_v1_",)
BOOTSTRAP_ENTITY_PREFIXES = (*LEGACY_BOOTSTRAP_ENTITY_PREFIXES, BOOTSTRAP_ENTITY_PREFIX)
TRIGGER_POLICIES = (
    "on_connect", "on_reconnect", "on_item_received", "on_supported_map_load",
    "manual_diagnostic",
)
REAPPLY_POLICIES = ("once_per_slot_revision", "once_per_map_load", "manual_only")
SUPPORTED_MAPS = (
    "game/sp/e1m1_intro/e1m1_intro",
    "game/hub/hub",
    "game/sp/e1m2_battle/e1m2_battle",
    "game/sp/e1m3_cult/e1m3_cult",
)
FORBIDDEN_EFFECT_TERMS = (
    "give ", "giveplayerperk", "chrispy ", "g_giveextralives", "currency",
    "ap_check_", "ap_event_", "objective", "progress", "portal", "gate",
    "relay", "tutorial", "codex", "checkpoint", "activatelayer", "forcestat",
)


# PlayerStatModifier shape proven by local map evidence.
BOOTSTRAP_STAT_PRIMITIVE = {
    "source": "KaiserCampaign 2.31 (Kaiser_will), research reference: e3m2_hell_patch2/maps/game/sp/e3m2_hell/e3m2_hell.entities:8213",
    "class": "idTarget_PlayerStatModifier",
    "inherit": None,
    "fields": ("gameStat", "value"),
    "value": 1,
}
INVALID_BOOTSTRAP_INHERITS = frozenset({"target/player_stat_modifier"})


# mirror the suit unlock ids from apworld and check them in validate_data; crystals, frag and ice unlock suit tabs, flame belch has no suit perk group
SUIT_PAGE_UNLOCKING_ITEM_IDS = frozenset({
    7770011, 7770013, 7770017, 7770088, 7770092,
    7770097, 7770098, 7770099, 7770100, 7770101, 7770102, 7770103, 7770104,
    7770106, 7770107, 7770108, 7770109, 7770110, 7770111, 7770112, 7770113,
    7770114, 7770115, 7770116, 7770117, 7770118,
})


# Suit-page unlock IDs include parent-tab unlockers and independent perks.
SUIT_UPGRADE_ITEM_IDS = SUIT_PAGE_UNLOCKING_ITEM_IDS
BOOTSTRAP_STAT_ALLOWLIST = {
    "rune_page": "STAT_RUNE_PAGE_UNLOCKED",
    "frag_acquired": "STAT_FRAG_GAINED",
    "ice_acquired": "STAT_ICE_BOMB_GAINED",
}
EQUIPMENT_ACQUISITION_STAT_AUDIT = {
    "frag_acquired": {
        "stat": "STAT_FRAG_GAINED",
        "readers": ("perkgroups/suit: Frag equipment family preReqStat",),
        "vanilla_writer": "throwable/player/frag_grenade.gameStat",
        "ui_loadout_only": True,
        "progression_reader_found": False,
        "force_stat_required": False,
    },
    "ice_acquired": {
        "stat": "STAT_ICE_BOMB_GAINED",
        "readers": ("perkgroups/suit: Ice equipment family preReqStat",),
        "vanilla_writer": "throwable/player/ice_bomb.gameStat",
        "ui_loadout_only": True,
        "progression_reader_found": False,
        "force_stat_required": False,
    },
}
BOOTSTRAP_ACTIONS = {
    "rune_page": _action(
        "rune_page", "at_least_one_rune",
        "Set only the Rune dossier page stat after AP Rune receipt.",
        ("on_connect", "on_reconnect", "on_item_received", "manual_diagnostic"),
    ),
    "frag_acquired": _action(
        "frag_acquired", "frag_grenade",
        "Set only the Frag acquisition stat after AP Frag receipt.",
        ("on_connect", "on_reconnect", "on_item_received", "on_supported_map_load"),
    ),
    "ice_acquired": _action(
        "ice_acquired", "ice_bomb",
        "Set only the Ice acquisition stat after AP Ice receipt.",
        ("on_connect", "on_reconnect", "on_item_received", "on_supported_map_load"),
        automatic_enabled=True,
    ),
}
REJECTED_BOOTSTRAP_HISTORY = {
    "suit_page_v2": {
        "revision": 2,
        "stat": "STAT_SUIT_PAGE_UNLOCKED",
        "status": "runtime_rejected",
        "reason": "stat-only candidate did not open the Suit page in runtime",
    }
}


def validate_bootstrap_catalogue(catalogue=BOOTSTRAP_ACTIONS):
    """Reject non-evidenced or gameplay-changing bootstrap entities."""
    entity_names = set()
    primitive = BOOTSTRAP_STAT_PRIMITIVE
    if primitive["inherit"] in INVALID_BOOTSTRAP_INHERITS:
        raise ValueError("Bootstrap primitive inherits the runtime-invalid entityDef")
    if primitive["class"] != "idTarget_PlayerStatModifier" or primitive["fields"] != ("gameStat", "value"):
        raise ValueError("Bootstrap primitive is not locally evidenced")
    for key, action in catalogue.items():
        _validate_action_identity(key, action, entity_names)
        entity_names.add(action["entity_name"])
        _validate_action_effect(key, action)
    return catalogue


validate_bootstrap_catalogue()
