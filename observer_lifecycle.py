"""Reusable live-runtime lease and persistent save-observer baseline contracts."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


ROOT = Path(__file__).resolve().parent
OBSERVER_CONTRACT_PATH = ROOT / "data" / "observer_contracts.json"


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
        try:
            output = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq DOOMEternalx64vk.exe"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return executable in output.lower()
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
        evidence_map: str,
        current_map: str,
        details_map: str,
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
        canonical = lambda value: str(value or "").replace("\\", "/").rstrip("/")
        maps = {canonical(evidence_map), canonical(current_map), canonical(details_map)}
        if "" in maps or len(maps) != 1:
            return False, "map_mismatch"
        return True, "live"


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
            if complete and not bool(previous.get(key, False)):
                pending.add(key)
                new_edges.add(key)
            previous[key] = bool(complete)
        observer["pending_edges"] = sorted(pending)
        return pending, created, new_edges
