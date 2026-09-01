"""Reusable live-runtime lease and persistent save-observer baseline contracts."""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


REPO_ROOT = Path(__file__).resolve().parents[2]
OBSERVER_CONTRACT_PATH = REPO_ROOT / "data" / "observer_contracts.json"
logger = logging.getLogger(__name__)

TH32CS_SNAPPROCESS = 0x00000002
ERROR_NO_MORE_FILES = 18
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH = 260
_windows_process_probe_warning_emitted = False


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
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


OBSERVER_CONTRACTS = load_observer_contracts()
SAVE_OBSERVER_POLICY = EvidencePolicy(
    **OBSERVER_CONTRACTS["policies"]["save_based"]
)


def unlockable_record_complete(record: Mapping, signal: Mapping) -> bool:
    expected_count = signal.get("rule_0_statCount")
    return (
        int(record.get("numUnlockableRules", -1)) == signal["numUnlockableRules"]
        and record.get("rule_0_statname") == signal["rule_0_statname"]
        and (
            expected_count is None
            or int(record.get("rule_0_statCount", -1)) >= expected_count
        )
        and int(record.get("rule_0_statDuration", -1))
        == signal["rule_0_statDuration"]
        and bool(record.get("rule_0_satisfied", False))
        is signal["rule_0_satisfied"]
        and bool(record.get("unlockableIsUnlocked", False))
        is signal["unlockableIsUnlocked"]
    )


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
        canonical = lambda value: str(value or "").replace("\\", "/").rstrip("/")
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


class SaveObserverBaselineStore:
    """Persistent false→true edges bound to AP identity and Doom save slot."""

    def __init__(self, state: dict):
        self.state = state.setdefault("observer_baselines", {})

    @staticmethod
    def binding_key(
        *,
        session_identity: str,
        team: int,
        slot: int,
        doom_save_slot: str,
        registry_revision: str,
    ) -> str:
        return "|".join(
            (
                session_identity,
                str(team),
                str(slot),
                doom_save_slot,
                registry_revision,
            )
        )

    def observe(
        self,
        *,
        binding_key: str,
        observer_key: str,
        records: Mapping[str, bool],
        acknowledged_records: set[str],
    ) -> tuple[set[str], bool, set[str]]:
        binding = self.state.get(binding_key)
        created = binding is None
        if binding is None:
            binding = self.state[binding_key] = {
                "observers": {},
                "registry_revision": binding_key.rsplit("|", 1)[-1],
            }
        observers = binding["observers"]
        observer = observers.get(observer_key)
        if observer is None:
            observer = observers[observer_key] = {
                "baseline_preexisting": sorted(
                    key for key, complete in records.items() if complete
                ),
                "last_observed": {
                    key: bool(complete) for key, complete in records.items()
                },
                "pending_edges": [],
            }
            return set(), True, set()

        previous = observer.setdefault("last_observed", {})
        pending = set(observer.setdefault("pending_edges", []))
        pending.difference_update(acknowledged_records)
        new_edges: set[str] = set()
        for key, complete in records.items():
            current = bool(complete)
            if key not in previous:
                previous[key] = current
                continue
            if current and not bool(previous[key]):
                pending.add(key)
                new_edges.add(key)
            previous[key] = current
        observer["pending_edges"] = sorted(pending)
        return pending, created, new_edges
