from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from websockets.exceptions import WebSocketException

from doom_eap.contracts.challenge_registry import aggregate_ready, load_challenge_registry
from doom_eap.contracts.receipt_delivery import GATE_KEY_TO_MAP, build_materialization_epoch
from doom_eap.contracts.runtime_context import canonical_map_name
from doom_eap.contracts.save_observation import (
    CheckpointRecovery,
    CheckpointRecoveryState,
    GameplaySaveEvidence,
    MissionSelectObservation,
    PrimarySaveSelection,
    SaveCandidateDecision,
    SaveProofDecision,
    SaveReadinessSnapshot,
    SaveSelectionSnapshot,
    unlockable_record_complete,
)
from doom_eap.runtime import bridge_config
from doom_eap.runtime.campaign import STAGES
from doom_eap.runtime.connection_diagnostics import timed_sync_call
from doom_eap.runtime.observers import classify_runtime_context, observer_registry_revision
from doom_eap.strings import text as player_text

#!/usr/bin/env python3


STEAM_ID64_BASE = 76561197960265728


def discover_default_remote() -> Path | None:
    home = Path.home()
    homes = [home]
    if os.name != "nt" and home.is_absolute():
        var_home = Path("/var") / home.relative_to("/")
        if var_home != home:
            homes.append(var_home)

    candidates = [
        path
        for candidate_home in homes
        for path in candidate_home.joinpath(
            ".local/share/Steam/userdata"
        ).glob("*/782330/remote")
    ]
    program_files = os.environ.get("PROGRAMFILES(X86)")
    if program_files:
        candidates.extend(
            Path(program_files).joinpath("Steam/userdata").glob("*/782330/remote")
        )
    return next((path for path in candidates if path.is_dir()), None)


def default_steam_id3() -> int:
    if DEFAULT_REMOTE is not None:
        try:
            return int(DEFAULT_REMOTE.parents[1].name)
        except (IndexError, ValueError):
            pass
    return 0


def steam_id64(steam_id3: int) -> int:
    return STEAM_ID64_BASE + steam_id3


def decrypt(data: bytes, aad_text: str) -> bytes:
    from doom_eap.launcher.build_manifest import _aes_gcm_decrypt

    if len(data) < 28:
        raise ValueError("Encrypted save is too short to contain nonce and GCM tag")
    aad = aad_text.encode("utf-8")
    key = hashlib.sha256(aad).digest()[:16]
    nonce = data[:12]
    ciphertext = data[12:-16]
    tag = data[-16:]
    return _aes_gcm_decrypt(key, nonce, ciphertext, tag, aad)


def decrypt_file(source: Path, destination: Path, identifier: str) -> None:
    aad = f"{identifier}MANCUBUS{source.name}"
    plaintext = decrypt(source.read_bytes(), aad)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(plaintext)
    print(f"{source} -> {destination} ({len(plaintext)} bytes)")


def decrypt_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", nargs="?", type=Path, default=DEFAULT_REMOTE)
    parser.add_argument("--output", type=Path, default=Path("/tmp/doom-eternal-decrypted"))
    parser.add_argument("--steam-id3", type=int, default=default_steam_id3())
    args = parser.parse_args()

    if args.source is None:
        parser.error("Steam remote directory was not found; provide source explicitly")
    if not args.steam_id3:
        parser.error("Steam account ID was not found; provide --steam-id3")

    identifier = str(steam_id64(args.steam_id3))
    sources = [args.source] if args.source.is_file() else sorted(args.source.glob("*/*"))
    for source in sources:
        if not source.is_file() or source.name.endswith("-BACKUP"):
            continue
        relative = source.name if args.source.is_file() else source.relative_to(args.source)
        try:
            decrypt_file(source, args.output / relative, identifier)
        except (OSError, RuntimeError, ValueError) as error:
            print(f"SKIP {source}: {error}")


def read_checkpoint_recovery(config, process_identity, namespace):
    blocked = CheckpointRecoveryState.BLOCKED
    try:
        return _checkpoint_recovery(_read_startup_record(config, process_identity, namespace), process_identity)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return CheckpointRecovery(blocked, f"checkpoint_diagnostics_unavailable: {error}")


def read_stage_restart(config, process_identity, namespace):
    try:
        campaign = _read_startup_record(config, process_identity, namespace)["campaign"]
        sequence, status = campaign["restart_sequence"], campaign["restart_status"]
        if type(sequence) is not int or sequence < 0 or type(status) is not int or status not in (1, 2, 3):
            return None
        return {"sequence": sequence, "status": status, "map": campaign["restart_map"],
                "stage_id": campaign["restart_stage_id"], "reason": campaign["restart_reason"]}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def primary_save_candidates(remote_directory, steam_id, filename="game_duration.dat", slot_prefix=None):
    """Enumerate candidates, without asserting which slot is active."""
    if remote_directory is None or steam_id <= 0 or not remote_directory.is_dir():
        return []
    candidates = []
    glob_pattern = f"{slot_prefix}*/{filename}" if slot_prefix else f"*-AUTOSAVE*/{filename}"
    for path in remote_directory.glob(glob_pattern):
        if not re.fullmatch(r"(?:GAME|DLC[12]|HORDE)-AUTOSAVE\d+", path.parent.name):
            continue
        if slot_prefix and not path.parent.name.startswith(slot_prefix):
            continue
        selected = _primary_save_candidate(path)
        if selected is not None:
            candidates.append(selected)

    def _slot_sort_key(selected):
        digits = re.search(r"\d+$", selected.slot_directory)
        slot_num = int(digits.group(0)) if digits else 0
        return (selected.mtime_ns, slot_num)

    return sorted(candidates, key=_slot_sort_key, reverse=True)


def admitted_ap_save(remote_directory, native_root, filename="game_duration.dat"):
    """Read only the single physical campaign under an already admitted provider."""
    if remote_directory is None:
        return None
    directory = remote_directory / native_root / "GAME-AUTOSAVE0"
    path = directory / filename
    try:
        # Provider-relative selection must not escape through filesystem links.
        if directory.resolve() != remote_directory.resolve() / native_root / "GAME-AUTOSAVE0":
            return None
        info = path.stat()
        if not path.is_file() or info.st_size <= 0 or path.resolve().parent != directory.resolve():
            return None
        return PrimarySaveSelection("GAME-AUTOSAVE0", path.resolve(), info.st_mtime_ns)
    except OSError:
        return None


def read_gameplay_save_evidence(path=None):
    """Read native evidence; active-slot proof belongs to the observer."""

    path = Path(path or bridge_config.GAMEPLAY_SAVE_EVIDENCE_PATH)
    try:
        values = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        state = values.get("state", "")
        epoch = int(values.get("epoch", "-1"))
        slot_directory = values.get("slot", "")
        map_name = canonical_map_name(values.get("map_name", "")) or ""
        pid = int(values.get("pid", "0"))
    except (OSError, UnicodeError, ValueError):
        return None
    if state == "menu":
        return GameplaySaveEvidence(state, epoch, "", "")
    if (
        state != "gameplay"
        or epoch < 0
        or not re.fullmatch(r"(?:GAME|DLC[12]|HORDE)-AUTOSAVE\d+", slot_directory)
    ):
        return None
    return GameplaySaveEvidence(
        state, epoch, slot_directory, map_name,
        values.get("provisional", "false").lower() == "true",
        values.get("native_safe", "false").lower() == "true",
        values.get("namespace", ""), values.get("native_root", ""), pid,
        values.get("process_created", ""), values.get("instance_id", ""),
        values.get("source_file", ""),
    )


@timed_sync_call
def unpack_game_duration(path, *, steam_id, runtime_directory, probe_path, oodle_path,
                         compat_data, proton_path, steam_install, host_exec):
    """Return checkpoint-death and native unlockable records from one save."""
    runtime_directory.mkdir(parents=True, exist_ok=True)
    runtime_probe = runtime_directory / probe_path.name
    runtime_oodle = runtime_directory / oodle_path.name
    if not runtime_probe.exists():
        shutil.copy2(probe_path, runtime_probe)
    if not runtime_oodle.exists():
        shutil.copy2(oodle_path, runtime_oodle)

    encrypted = path.read_bytes()
    aad = f"{steam_id64(steam_id)}MANCUBUS{path.name}"
    runtime_save = runtime_directory / "game_duration.dat"
    runtime_save.write_bytes(decrypt(encrypted, aad))

    runtime_unpacked = runtime_directory / "game_duration.full.bin"
    if os.name == "nt":
        command = [
            str(runtime_probe), runtime_oodle.name, runtime_save.name,
            runtime_unpacked.name,
        ]
        environment = None
    else:
        compat_data.mkdir(parents=True, exist_ok=True)
        proton_command = [
            str(proton_path),
            "run",
            runtime_probe.name,
            runtime_oodle.name,
            runtime_save.name,
            runtime_unpacked.name,
        ]
        if host_exec:
            command = [
                host_exec,
                "env",
                f"STEAM_COMPAT_DATA_PATH={compat_data}",
                f"STEAM_COMPAT_CLIENT_INSTALL_PATH={steam_install}",
                *proton_command,
            ]
            environment = None
        else:
            command = proton_command
            environment = os.environ.copy()
            environment["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
            environment["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_install)

    result = subprocess.run(
        command,
        cwd=runtime_directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode in {0, 20}:
        return runtime_unpacked.read_bytes(), result.returncode, result.stdout

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    raise RuntimeError(
        "save_death_probe exited with code "
        f"{result.returncode}; stdout={stdout!r}; stderr={stderr!r}"
    )


def read_game_details_for_selection(selected, steam_id, logger):
    if selected is None:
        return None
    path = selected.path.parent / "game.details"
    if not path.is_file():
        return None

    aad = f"{steam_id64(steam_id)}MANCUBUS{path.name}"
    try:
        plaintext = decrypt(path.read_bytes(), aad).decode("utf-8")
        values = {}
        for line in plaintext.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        values["_path"] = str(path)
        values["_mtime_ns"] = path.stat().st_mtime_ns
        if "mapName" in values:
            values["mapName"] = canonical_map_name(values["mapName"])
        return values
    except (OSError, RuntimeError, ValueError) as error:
        logger.error(f"[Save] Failed to decrypt {path}: {error}")
        return None


class SaveCheckObservationPort(Protocol):
    def observe_edges(self, observer_key, records, entries, slot_directory,
                      first_sample_pending=frozenset()): ...
    def observe_record(self, kind, slot, unlockable, record): ...


@dataclass(frozen=True)
class SaveCheckReadiness:
    item_ready: bool
    authoritative: bool
    slot: str | None


class SaveChecks:
    def __init__(self, masteries, challenges, challenge_maps, aggregates, logger):
        self._mastery_entries, self._challenge_entries = dict(masteries), dict(challenges)
        self._challenge_maps, self._aggregates = dict(challenge_maps), tuple(aggregates)
        self._logger = logger
        self._masteries, self._challenges = {}, {}
        self._mastery_warnings, self._challenge_warnings = set(), set()
        self._generation = 0

    def invalidate(self):
        self._generation += 1

    def bind(self, masteries=None):
        if masteries is not None:
            self._mastery_entries = dict(masteries)
        self.invalidate()
        self.reset_observations(populate=False)

    def reset_observations(self, *, populate=True):
        self._masteries = {key: False for key in self._mastery_entries} if populate else {}
        self._challenges = {key: False for key in self._challenge_entries} if populate else {}

    def observe_masteries(self, records, path, slot_directory, observations: SaveCheckObservationPort, *, authoritative):
        if not authoritative:
            return
        completion_states = {
            unlockable: (
                unlockable in records
                and unlockable_record_complete(records[unlockable], entry["signal"])
            )
            for unlockable, entry in self._mastery_entries.items()
        }
        pending_edges = observations.observe_edges(
            "weapon_masteries",
            completion_states,
            self._mastery_entries,
            slot_directory,
        )
        for unlockable in self._mastery_entries:
            self._masteries.setdefault(unlockable, False)
        for unlockable, record in records.items():
            entry = self._mastery_entries.get(unlockable)
            if entry is None:
                continue
            observed_record = (
                int(record["numUnlockableRules"]),
                record["rule_0_statname"],
                int(record["rule_0_statCount"]),
                int(record["rule_0_statDuration"]),
                bool(record["rule_0_satisfied"]),
                bool(record["unlockableIsUnlocked"]),
            )
            if observations.observe_record("mastery", slot_directory, unlockable, observed_record):
                self._logger.info(
                    "[Mastery] RECORD unlockable=%s rules=%s stat=%s count=%s "
                    "duration=%s satisfied=%s unlocked=%s save_slot=%s source=%s",
                    unlockable,
                    *observed_record,
                    slot_directory,
                    path,
                )

            if unlockable not in pending_edges:
                continue
            self._masteries[unlockable] = True


    def _observe_challenge_record(self, unlockable, record, signal, observations, slot_directory, path, pending_edges):
        observed_record = (
            int(record["numUnlockableRules"]),
            record["rule_0_statname"],
            int(record["rule_0_statCount"]),
            int(record["rule_0_statDuration"]),
            bool(record["rule_0_satisfied"]),
            bool(record["unlockableIsUnlocked"]),
        )
        if observations.observe_record("challenge", slot_directory, unlockable, observed_record):
            self._logger.info(
                "[Challenge] RECORD unlockable=%s rules=%s stat=%s count=%s "
                "duration=%s satisfied=%s unlocked=%s save_slot=%s source=%s",
                unlockable,
                *observed_record,
                slot_directory,
                path,
            )

        self._log_challenge_shape(unlockable, observed_record, signal)
        if unlockable not in pending_edges:
            return
        self._challenges[unlockable] = True

    def _save_challenge_entries(self, mission_select_map):
        save_entries = {
            unlockable: entry
            for unlockable, entry in self._challenge_entries.items()
            if entry["signal"]["kind"] in {"unlockable_record", "stat_threshold"}
            and (
                not mission_select_map
                or self._challenge_maps.get(unlockable)
                == mission_select_map
            )
        }
        return save_entries

    def observe_challenges(self, records, path, slot_directory, mission_select_map, mission_select_epoch, observations: SaveCheckObservationPort, *, authoritative, first_sample_locations=frozenset()):
        if not authoritative:
            return
        save_entries = self._save_challenge_entries(mission_select_map)
        completion_states = {
            unlockable: (
                unlockable in records
                and unlockable_record_complete(records[unlockable], entry["signal"])
            )
            for unlockable, entry in save_entries.items()
        }
        observer_key = "mission_challenges"
        if mission_select_map:
            observer_key = (
                f"mission_challenges:mission_select:"
                f"{mission_select_epoch}:"
                f"{mission_select_map}"
            )
        pending_edges = observations.observe_edges(
            observer_key,
            completion_states,
            save_entries,
            slot_directory,
            first_sample_pending=frozenset(
                key for key, entry in save_entries.items()
                if entry["location_id"] in first_sample_locations
            ),
        )
        for unlockable, record in records.items():
            entry = self._challenge_entries.get(unlockable)
            if entry is None:
                continue
            signal = entry["signal"]
            if signal["kind"] not in {"unlockable_record", "stat_threshold"}:
                continue
            self._observe_challenge_record(unlockable, record, signal, observations, slot_directory, path, pending_edges)


    async def _publish_mastery(self, location_id, unlockable, publication):

        generation = self._generation
        try:
            self._logger.info(
                "[Mastery] LOCATION_CHECK_SEND id=%s unlockable=%s "
                "source=vanilla_save_predicate",
                location_id,
                unlockable,
            )
            await publication.location(location_id)
        except (OSError, RuntimeError, WebSocketException) as error:
            if generation != self._generation:
                return
            self._logger.error(
                "[Mastery] LOCATION_CHECK_RETRY id=%s unlockable=%s error=%s",
                location_id,
                unlockable,
                error,
            )
            return
        if generation != self._generation:
            return
        publication.mark_submitted(location_id)
        self._logger.info("[Mastery] LOCATION_CHECK_ACK id=%s", location_id)

    async def check_mastery(self, entry, readiness, facts, publication):
        if not readiness.item_ready or not readiness.authoritative:
            return
        unlockable = entry["signal"]["unlockable"]
        if not self._masteries.get(unlockable):
            return
        location_id = entry["location_id"]
        if location_id in facts.checked or location_id in facts.submitted:
            return
        if location_id not in facts.server_locations:
            self._warn_absent_location(location_id, unlockable, "Mastery", self._mastery_warnings)
            return
        if not facts.connected:
            return
        await self._publish_mastery(location_id, unlockable, publication)


    async def _publish_challenge(self, location_id, unlockable, readiness, is_physical, publication):

        source_name = "physical_event_equivalent" if is_physical else "vanilla_save_predicate"
        generation = self._generation
        try:
            self._logger.info(
                "[Challenge] LOCATION_CHECK_SEND id=%s unlockable=%s "
                "source=%s save_slot=%s",
                location_id,
                unlockable,
                source_name,
                readiness.slot or "<synthetic>",
            )
            await publication.location(location_id)
        except (OSError, RuntimeError, WebSocketException) as error:
            if generation != self._generation:
                return
            self._logger.error(
                "[Challenge] LOCATION_CHECK_RETRY id=%s unlockable=%s error=%s",
                location_id,
                unlockable,
                error,
            )
            return
        if generation != self._generation:
            return
        self._logger.info("[Challenge] LOCATION_CHECK_QUEUED id=%s awaiting=server_ack", location_id)

    async def check_challenge(self, entry, readiness, facts, publication):
        if not readiness.item_ready:
            return
        is_physical = entry["signal"].get("kind") == "physical_event_equivalent"
        if not is_physical and not readiness.authoritative:
            return
        unlockable = entry["signal"]["unlockable"]
        if not self._challenge_complete(entry, is_physical, unlockable, facts):
            return
        location_id = entry["location_id"]
        if location_id in facts.checked:
            return
        if location_id not in facts.server_locations:
            self._warn_absent_location(location_id, unlockable, "Challenge", self._challenge_warnings)
            return
        if not facts.connected:
            return
        await self._publish_challenge(location_id, unlockable, readiness, is_physical, publication)


    async def check_aggregates(self, readiness, facts, publication):
        """Publish aggregates only from server-authoritative checked children."""
        if not readiness.item_ready:
            return
        generation = self._generation
        checked = set(facts.checked)
        for aggregate in self._aggregates:
            await self._check_aggregate(aggregate, facts, publication, generation, checked)
            if generation != self._generation:
                return

    def _log_challenge_shape(self, unlockable, observed_record, signal):
        if observed_record[0] != signal["numUnlockableRules"]:
            self._logger.warning(
                "[Challenge] REGISTRY_MISMATCH unlockable=%s field=numUnlockableRules expected=%s observed=%s",
                unlockable, signal["numUnlockableRules"], observed_record[0],
            )
        if observed_record[1] != signal["rule_0_statname"]:
            self._logger.warning(
                "[Challenge] REGISTRY_MISMATCH unlockable=%s field=rule_0_statname expected=%s observed=%s",
                unlockable, signal["rule_0_statname"], observed_record[1],
            )
        if observed_record[3] != signal["rule_0_statDuration"]:
            self._logger.warning(
                "[Challenge] REGISTRY_MISMATCH unlockable=%s field=rule_0_statDuration expected=%s observed=%s",
                unlockable, signal["rule_0_statDuration"], observed_record[3],
            )
        expected_count = signal.get("rule_0_statCount")
        if expected_count is not None and observed_record[2] < expected_count:
            self._logger.warning(
                "[Challenge] REGISTRY_MISMATCH unlockable=%s "
                "field=rule_0_statCount expected_at_least=%s observed=%s",
                unlockable, expected_count, observed_record[2],
            )


    def _warn_absent_location(self, location_id, unlockable, label, warnings):
        if location_id not in warnings:
            self._logger.warning("[%s] LOCATION id=%s unlockable=%s slot=absent", label, location_id, unlockable)
            warnings.add(location_id)

    def _challenge_complete(self, entry, is_physical, unlockable, facts):
        if is_physical:
            physical_ids = set(entry["signal"].get("physical_location_ids", ()))
            required_count = int(entry["signal"].get("required_count", 1))
            if len(physical_ids.intersection(facts.checked)) < required_count:
                return False
        elif not self._challenges.get(unlockable):
            return False
        return True

    async def _check_aggregate(self, aggregate, facts, publication, generation, checked):

        signal = aggregate["signal"]
        children = set(signal["children"])
        if not aggregate_ready(signal, checked, set(facts.server_locations)):
            return
        location_id = aggregate["location_id"]
        if location_id in checked:
            return
        if location_id not in facts.server_locations:
            if location_id not in self._challenge_warnings:
                self._logger.warning(
                    "[Challenge] ALL_LOCATION id=%s slot=absent", location_id
                )
                self._challenge_warnings.add(location_id)
            return
        if not facts.connected:
            return
        self._logger.info(
            "[Challenge] ALL_LOCATION_CHECK_SEND id=%s authority=server_checked_locations "
            "children=%s",
            location_id,
            sorted(children),
        )
        try:
            await publication.location(location_id)
        except (OSError, RuntimeError, WebSocketException) as error:
            if generation != self._generation:
                return
            self._logger.error(
                "[Challenge] ALL_LOCATION_CHECK_RETRY id=%s error=%s",
                location_id,
                error,
            )
            return
        if generation != self._generation:
            return
        self._logger.info(
            "[Challenge] ALL_LOCATION_CHECK_QUEUED id=%s awaiting=server_ack",
            location_id,
        )


def read_unlockable_record(payload, entry):
    """Decode one exact native unlockable record; global stats are ignored."""
    signal = entry["signal"]
    unlockable = signal["unlockable"].encode("ascii")
    manager_type_offset = _mastery_manager_type_offset(payload)
    record_prefix = (
        bytes([len(unlockable) * 2]) + unlockable
        + b"\x0e\x0c$numUnlockableRules"
    )
    record_offset = payload.find(record_prefix, manager_type_offset)
    if (
        record_offset < manager_type_offset
        or payload.find(record_prefix, record_offset + 1) >= 0
    ):
        if record_offset < 0:
            return None
        raise ValueError(f"{signal['unlockable']}: native record is ambiguous")

    cursor = record_offset + len(record_prefix)
    rule_count, cursor = _read_serialized_uint(payload, cursor)
    satisfied, cursor = _read_structured_bool(payload, cursor, b" rule_0_satisfied")
    if not payload.startswith(b" rule_0_statCount", cursor):
        raise ValueError(f"{signal['unlockable']}: missing rule_0_statCount")
    stat_count, cursor = _read_serialized_uint(
        payload, cursor + len(b" rule_0_statCount")
    )
    if not payload.startswith(b"&rule_0_statDuration", cursor):
        raise ValueError(f"{signal['unlockable']}: missing rule_0_statDuration")
    stat_duration, cursor = _read_serialized_uint(
        payload, cursor + len(b"&rule_0_statDuration")
    )
    stat_prefix = b"\x1erule_0_statname\x0a"
    if not payload.startswith(stat_prefix, cursor):
        raise ValueError(f"{signal['unlockable']}: missing rule_0_statname")
    cursor += len(stat_prefix)
    stat_len = payload[cursor] // 2
    cursor += 1
    stat_bytes = payload[cursor:cursor + stat_len]
    cursor += stat_len
    unlocked, cursor = _read_structured_bool(
        payload, cursor, b"(unlockableIsUnlocked"
    )
    return {
        "numUnlockableRules": rule_count,
        "rule_0_statname": stat_bytes.decode("ascii", errors="ignore"),
        "rule_0_statCount": stat_count,
        "rule_0_statDuration": stat_duration,
        "rule_0_satisfied": satisfied,
        "unlockableIsUnlocked": unlocked,
    }


def active_primary_save(filename="game_duration.dat"):
    """Compatibility view of the newest candidate, not an active-slot proof."""

    candidates = primary_save_candidates(bridge_config.STEAM_REMOTE_DIR, bridge_config.STEAM_ID3, filename)
    return candidates[0] if candidates else None


def mastery_save_selection():
    return active_primary_save("game_duration.dat")


def mastery_save_file():
    """Compatibility path view of the dynamically selected primary save."""
    selected = mastery_save_selection()
    return selected.path if selected else None


def active_slot_file(filename):
    """Return a companion file from the newest game_duration primary slot."""
    selected = mastery_save_selection()
    if selected is None:
        return None
    path = selected.path.parent / filename
    return path if path.is_file() else None


def death_probe_available():

    if (
        bridge_config.STEAM_REMOTE_DIR is None
        or bridge_config.STEAM_ID3 <= 0
        or not bridge_config.STEAM_REMOTE_DIR.is_dir()
        or not bridge_config.DEATH_PROBE.is_file()
        or not bridge_config.OODLE_DLL.is_file()
    ):
        return False

    if os.name == "nt":
        return True

    return (
        bridge_config.PROTON_PATH.is_file()
        and bridge_config.STEAM_COMPAT_DATA.is_dir()
        and bridge_config.STEAM_INSTALL.is_dir()
    )


def read_weapon_mastery_records(payload):
    """Return only structured records that exist in the fixed vanilla manager."""

    records = {}
    for entry in WEAPON_MASTERY_ENTRIES:
        try:
            record = read_unlockable_record(payload, entry)
            if record is not None:
                records[entry["signal"]["unlockable"]] = record
        except (ValueError, struct.error) as error:
            bridge_config.logger.warning(
                "[Mastery] RECORD_PARSE_ERROR unlockable=%s error=%s",
                entry["signal"]["unlockable"], error,
            )
    return records


def read_mission_challenge_records(payload):
    """Return exact challenge records from the native manager."""

    records = {}
    for entry in MISSION_CHALLENGE_ENTRIES:
        try:
            record = read_unlockable_record(payload, entry)
            if record is not None:
                records[entry["signal"]["unlockable"]] = record
        except (ValueError, struct.error) as error:
            bridge_config.logger.warning(
                "[Challenge] RECORD_PARSE_ERROR unlockable=%s error=%s",
                entry["signal"]["unlockable"], error,
            )
    return records


def read_checkpoint_deaths(unpacked: bytes) -> int | None:
    marker = b"numCheckpointDeaths\x01"
    idx = unpacked.find(marker)
    if idx == -1 or idx + len(marker) >= len(unpacked):
        return None
    return int(unpacked[idx + len(marker)])


def probe_game_duration(path):

    unpacked, result_code, stdout = unpack_game_duration(
        path, steam_id=bridge_config.STEAM_ID3, runtime_directory=bridge_config.DEATH_PROBE_RUNTIME,
        probe_path=bridge_config.DEATH_PROBE, oodle_path=bridge_config.OODLE_DLL, compat_data=bridge_config.DEATH_PROBE_COMPAT_DATA,
        proton_path=bridge_config.PROTON_PATH, steam_install=bridge_config.STEAM_INSTALL, host_exec=bridge_config.DISTROBOX_HOST_EXEC,
    )
    mastery_records = read_weapon_mastery_records(unpacked)
    raw_deaths = read_checkpoint_deaths(unpacked)
    if raw_deaths is None and stdout:
        m = re.search(r"numCheckpointDeaths=(\d+)", stdout)
        if m:
            raw_deaths = int(m.group(1))
    snapshot = {
        "mastery_records": mastery_records,
        "mission_challenge_records": read_mission_challenge_records(unpacked),
        "raw_num_checkpoint_deaths": raw_deaths if raw_deaths is not None else (1 if result_code == 20 else 0),
    }
    sticky_record = mastery_records.get(STICKY_UNLOCKABLE.decode("ascii"))
    if sticky_record is not None:
        snapshot.update({
            key: sticky_record[key]
            for key in (
                "rule_0_statname", "rule_0_statCount", "rule_0_satisfied",
                "unlockableIsUnlocked",
            )
        })
    snapshot["checkpoint_death"] = snapshot["raw_num_checkpoint_deaths"] > 0
    return snapshot


class SaveRuntime:
    def __init__(self, context):
        self.ctx = context

    def save_check_observations(self):
        from doom_eap.runtime import bridge_checks

        scope = bridge_checks.SaveCheckBinding(
            str(self.ctx.room_seed_name or getattr(self.ctx, "seed_name", None) or self.ctx.state_key or "unknown"),
            int(getattr(self.ctx, "team", 0) or 0), int(getattr(self.ctx, "slot", 0) or 0), self.ctx.state_key,
            OBSERVER_REGISTRY_REVISION, frozenset(getattr(self.ctx, "checked_locations", set())),
            self.ctx.item_state_ready, self.ctx.client_state.get("sessions", {}).get(self.ctx.state_key) is self.ctx.session_state,
        )
        return bridge_checks.SaveCheckObservations(self.ctx.save_observer, scope, self.ctx.persist_session_state, bridge_config.logger)

    def observe_weapon_masteries(self, records, path):
        slot_directory = self.observation_slot_for_source(path)
        self.select_save_observation_slot(slot_directory)
        self.ctx.save_checks.observe_masteries(records, path, slot_directory, self.save_check_observations(),
            authoritative=self.has_authoritative_save_proof())

    def observe_mission_challenges(self, records, path):
        slot_directory = self.observation_slot_for_source(path)
        self.select_save_observation_slot(slot_directory)
        if not self.ctx.server_checked_locations_ready:
            return  # Do not consume the first complete baseline before checked Locations arrive.
        self.ctx.save_checks.observe_challenges(records, path, slot_directory,
            self.ctx.save_observer.mission_select.map_name, self.ctx.save_observer.mission_select.epoch, self.save_check_observations(),
            authoritative=self.has_authoritative_save_proof(),
            first_sample_locations=frozenset(self.ctx.server_locations) - frozenset(self.ctx.checked_locations))

    def save_check_readiness(self):
        return SaveCheckReadiness(self.ctx.item_state_ready, self.has_authoritative_save_proof(), self.ctx.save_observer.selection.slot)

    async def check_weapon_mastery_location(self, entry):
        await self.ctx.save_checks.check_mastery(entry, self.save_check_readiness(),
            self.ctx.checks.check_observation(), self.ctx.checks.check_publication())

    async def check_weapon_mastery_locations(self):
        token = self.ctx.receipt_session.capture(self.ctx.state_key)
        for entry in WEAPON_MASTERY_ENTRIES:
            await self.check_weapon_mastery_location(entry)
            if not self.ctx.receipt_session.is_current(token, self.ctx.state_key):
                return

    async def check_mission_challenge_location(self, entry):
        await self.ctx.save_checks.check_challenge(entry, self.save_check_readiness(),
            self.ctx.checks.check_observation(), self.ctx.checks.check_publication())

    async def check_mission_challenge_locations(self):
        (await self.ctx.checks.ingest_visible_runtime_lifecycle())
        token = self.ctx.receipt_session.capture(self.ctx.state_key)
        for entry in MISSION_CHALLENGE_ENTRIES:
            await self.check_mission_challenge_location(entry)
            if not self.ctx.receipt_session.is_current(token, self.ctx.state_key):
                return
        await self.check_all_mission_challenges_location()

    async def check_all_mission_challenges_location(self):
        await self.ctx.save_checks.check_aggregates(self.save_check_readiness(),
            self.ctx.checks.check_observation(), self.ctx.checks.check_publication())

    def observation_slot_for_source(self, source):
        if isinstance(source, PrimarySaveSelection):
            return source.slot_directory
        try:
            parent = Path(source).parent.name
        except (TypeError, ValueError):
            parent = ""
        if re.fullmatch(r"(?:GAME|DLC[12]|HORDE)-AUTOSAVE\d+", parent):
            return parent
        return self.ctx.save_observer.selection.slot or "<synthetic>"

    def select_save_observation_slot(self, slot_directory):
        """Select a slot without trusting legacy observed=true persistence."""
        if not self.ctx.save_observer.select_observation_slot(slot_directory):
            return
        self.ctx.save_checks.reset_observations()

    def has_authoritative_save_proof(self):
        lease = getattr(self.ctx, "runtime_observation_lease", None)
        return self.ctx.save_observer.has_authoritative_proof(
            lease_present=lease is not None,
            gameplay_loaded_ns=getattr(lease, "gameplay_loaded_ns", None),
        )

    def invalidate_active_save_proof(self):
        """Clear proof authority at a gameplay/process lifecycle boundary."""
        self.ctx.save_observer.invalidate_proof()

    def activate_save_selection(self, selected):

        old_slot = self.ctx.save_observer.selection.slot
        path_changed = str(selected.path) != self.ctx.save_observer.selection.path
        if old_slot != selected.slot_directory or path_changed:
            bridge_config.logger.info(
                "SAVE_SLOT_ACTIVE old=%s new=%s path=%s",
                old_slot or "<none>",
                selected.slot_directory,
                selected.path,
            )
        self.ctx.save_observer.update_selection(
            slot=selected.slot_directory, path=str(selected.path), token=selected.mtime_ns,
        )
        self.ctx.save_observer.activate_slot(selected.slot_directory)
        self.select_save_observation_slot(selected.slot_directory)

    def invalidate_save_observation_slot(self, slot_directory):
        """Discard local authority when a slot directory has been recreated."""
        self.ctx.save_observer.invalidate_observation_slot(slot_directory)
        self.select_save_observation_slot(slot_directory)

    def log_save_proof_rejected(
        self, reason, evidence_slot=None, marker_map=None, candidate_slot=None,
        candidate_mtime=None, active_slot=None,
    ):

        rejection = (
            reason,
            evidence_slot or "<none>",
            marker_map or "<none>",
            candidate_slot or "<none>",
            candidate_mtime or 0,
            active_slot or self.ctx.save_observer.selection.slot or "<none>",
        )
        if rejection == getattr(self.ctx, "last_save_proof_rejection", None):
            return
        self.ctx.last_save_proof_rejection = rejection
        bridge_config.logger.info(
            "SAVE_PROOF_REJECTED reason=%s evidence_slot=%s marker_map=%s "
            "candidate_slot=%s candidate_mtime=%s active_slot=%s",
            *rejection,
        )
        bridge_config.logger.info(
            "SAVE_SLOT_REJECTED slot=%s reason=%s path=%s",
            candidate_slot or "<none>",
            reason,
            candidate_slot or "<none>",
        )

    def log_save_proof_accepted(
        self, slot, map_name, epoch, duration_token, details_token, proof="non_provisional_fresh_map_match"
    ):

        bridge_config.logger.info(
            "SAVE_PROOF_ACCEPTED slot=%s map=%s epoch=%s game_duration_token=%s "
            "game_details_token=%s proof=%s",
            slot,
            map_name,
            epoch,
            duration_token,
            details_token,
            proof,
        )

    async def checkpoint_recovery_ready(self, link=None):

        try:
            link = link or await self.ctx.items._native_game_link_async()
            await self.synchronize_stage_restart(link)
            recovery = await self.ctx.items._blocking_native_worker().call(
                read_checkpoint_recovery, bridge_config.config, link._process_identity, link.namespace,
                process=link._process_identity)
        except (RuntimeError, OSError, ValueError) as error:
            recovery = CheckpointRecovery(CheckpointRecoveryState.BLOCKED, str(error))
        previous = getattr(self.ctx, "_checkpoint_recovery", None)
        if recovery != previous:
            bridge_config.logger.info("[Campaign] Checkpoint recovery state=%s reason=%s", recovery.state.value, recovery.reason)
            if recovery.token is not None and recovery.token != getattr(previous, "token", None):
                self.ctx.death_observer.reset()
                self.ctx._death_baseline_pending = True
                self.ctx.save_observer.invalidate_proof()
        self.ctx._checkpoint_recovery = recovery
        if recovery.state != CheckpointRecoveryState.READY:
            self.ctx._death_baseline_pending = True
            self.ctx.save_observer.invalidate_proof()
            return False
        return True

    async def synchronize_stage_restart(self, link):

        data = getattr(self.ctx, "_connected_slot_data", None) or {}
        if data.get("runtime", {}).get("difficulty", {}).get("death_policy") != "restart_stage":
            return
        from doom_eap.runtime.campaign import activity_events

        current = await self.ctx.items._blocking_native_worker().call(
            read_stage_restart, bridge_config.config, link._process_identity, link.namespace,
            process=link._process_identity)
        identity = (link._process_identity, link.namespace)
        prior_identity, previous = getattr(self.ctx, "_stage_restart_activity", (None, None))
        previous = previous if prior_identity == identity else None
        stage = next((row["title"] for row in self.campaign_projection()["rows"]
                      if current and STAGES[row["stage"]]["access_id"] == current["stage_id"]), player_text("stage_restart", "stage"))
        for event in activity_events(previous, current, stage):
            bridge_config.logger.info("[Campaign] Restart %s: %s", event["status"], stage)
            bridge_config.emit_launcher_event("stage_restart", **event)
        if current is not None:
            self.ctx._stage_restart_activity = identity, current

    async def _admitted_campaign_root(self):

        try:
            link = (await self.ctx.items._native_game_link_async())
            if not await self.checkpoint_recovery_ready(link):
                return None
            native_root = (await self.ctx.items._native_call(link, 'admitted_save_root'))
            self.ctx.save_observer.bind_ap_provider(link.namespace, native_root)
            return native_root
        except (RuntimeError, OSError, ValueError) as error:
            reason = str(error)
            if getattr(self.ctx, "_last_ap_save_refusal", None) != reason:
                bridge_config.logger.info("[Campaign] Save observation unavailable: %s", reason)
                self.ctx._last_ap_save_refusal = reason
            return None

    async def admitted_campaign_save(self, filename="game_duration.dat"):

        native_root = await self._admitted_campaign_root()
        return admitted_ap_save(bridge_config.STEAM_REMOTE_DIR, native_root, filename) if native_root else None

    def campaign_projection(self):
        return self.ctx.unified_campaign.snapshot(self.ctx.checked_locations, self.ctx.items_received)

    def _project_slayer_gates(self, projection):

        owned = self.ctx.items.received_item_ids()
        gate_checks = {name: int(location) for location, name in bridge_config.DOOM_LOCATION_NAMES.items()
                       if name.endswith(" - Slayer Gate Complete")}
        for row in projection["rows"]:
            gate_key = next((item for item, map_key in GATE_KEY_TO_MAP.items()
                             if row["stage"] == map_key), None)
            row["slayer_gate"] = gate_key is not None and row["revealed"]
            row["gate_key"] = row["slayer_gate"] and gate_key in owned
            row["gate_complete"] = row["slayer_gate"] and gate_checks.get(
                row["title"] + " - Slayer Gate Complete") in self.ctx.session_state.get("physical_slayer_gates", ())

    def _project_stage_ratings(self, projection):
        ratings = {key: {"skull_tier": row["skulls"]} for key, row in
                   self.ctx._connected_slot_data.get("stage_profiles", {}).items()}
        projection["ratings_version"] = 2
        projection["ratings"] = {row["stage"]: ratings[row["stage"]]
                                 for row in projection["rows"]
                                 if row["revealed"] and row["stage"] in ratings}

    async def _observe_campaign_menu(self, result, link, generation, facts, projection):
        from doom_eap.runtime.campaign import PresentationHints
        hints = getattr(self.ctx, "_presentation_hints", None)
        if hints is None or hints.namespace != link.namespace:
            state = self.ctx.session_state.setdefault("presentation_hints", {}).setdefault(link.namespace, {})
            self.ctx._presentation_hints = hints = PresentationHints(link.namespace, state, self.ctx.persist_session_state)
        hint_scope = (self.ctx.room_seed_name, self.ctx.team, self.ctx.slot, generation)
        confirmed_scope, confirmed = getattr(self.ctx, "_presentation_hints_confirmed", (None, set()))
        allowed = {entry["location_id"] for entry in
                   (*MISSION_CHALLENGE_ENTRIES, *ALL_MISSION_CHALLENGES_ENTRIES, *WEAPON_MASTERY_ENTRIES)}
        await hints.synchronize(result.get("hint_intents", []), allowed & facts.server_locations,
                                confirmed if confirmed_scope == hint_scope else set(),
        self.ctx.send_msgs if self.ctx.server and self.ctx.server.socket.open and not self.ctx.server.socket.closed else None,
        self.ctx._launcher_hints_key())
        selected = result["selected_stage"]
        if selected:
            self.ctx.unified_campaign.select(selected, projection)
        status = (result["committed_revision"], result["rendered_revision"], selected, result["loaded_stage"])
        if getattr(self.ctx, "_campaign_menu_status", None) != status:
            bridge_config.logger.info("[Campaign] Native menu committed=%s rendered=%s selected=%s loaded=%s", *status)
            self.ctx._campaign_menu_status = status
        self.ctx._campaign_menu_refusal = None

    def _project_mission_summaries(self, projection, link, generation, facts, pending):
        from doom_eap.runtime import bridge_checks

        projection["summaries"] = {
            row["stage"]: self.ctx._mission_presentations.snapshot(
                link.namespace, generation, row["stage"], revealed=row["revealed"], facts=facts, pending=pending)
            for row in projection["rows"]
        }
        placements = {entry["location_id"]: entry for entry in bridge_checks.resolve_placement_records(
            self.ctx.location_setup.received_ids, self.ctx.locations_info, self.ctx.slot_info, self.ctx.protocol_names(), self.ctx.slot
        )}
        projection["hub_summary"] = self.ctx._mission_presentations.snapshot(
            link.namespace, generation, "hub", revealed=True, facts=facts, pending=pending)
        projection["challenge_rewards"] = {
            row["stage"]: self.ctx._mission_presentations.challenge_snapshot(
                link.namespace, generation, row["stage"], revealed=row["revealed"], facts=facts,
                placements=placements, aggregates=ALL_MISSION_CHALLENGES_ENTRIES)
            for row in projection["rows"]
        }
        projection["masteries"] = self.ctx._mission_presentations.mastery_snapshot(
            facts, placements, WEAPON_MASTERY_ENTRIES)
        projection['encounter_rewards'] = {row['stage']: self.ctx._mission_presentations.encounter_snapshot(
            row['stage'], revealed=row['revealed'], facts=facts, placements=placements, pending=pending)
            for row in projection['rows']}

    async def synchronize_campaign_menu(self):

        if not getattr(self.ctx, "server_checked_locations_ready", False) or not hasattr(self.ctx, "unified_campaign"):
            return
        from doom_eap.runtime.campaign import CampaignMenu
        if not hasattr(self.ctx, "_campaign_menu"):
            self.ctx._campaign_menu = CampaignMenu()
        try:
            projection = self.campaign_projection()
            self._project_slayer_gates(projection)
            (await self.synchronize_fortress_phase(projection))
            from doom_eap.runtime.campaign import MissionPresentations
            if not hasattr(self.ctx, "_mission_presentations"):
                self.ctx._mission_presentations = MissionPresentations(bridge_config.REPO_ROOT,
                                                                    MISSION_CHALLENGE_ENTRIES)
            link = (await self.ctx.items._native_game_link_async())
            facts = self.ctx.checks.check_observation()
            pending = self.ctx.physical_checks.pending(facts)
            generation = self.ctx._connected_slot_data["native_generation_fingerprint"]
            self._project_mission_summaries(projection, link, generation, facts, pending)
            self._project_stage_ratings(projection)
            result = await self.ctx.items._blocking_native_worker().call(
                self.ctx._campaign_menu.synchronize, link, projection, process=link._process_identity)
            await self._observe_campaign_menu(result, link, generation, facts, projection)
        except (RuntimeError, OSError, ValueError, KeyError) as error:
            reason = str(error)
            if getattr(self.ctx, "_campaign_menu_refusal", None) != reason:
                bridge_config.logger.info("[Campaign] Native menu pending: %s", reason)
                self.ctx._campaign_menu_refusal = reason

    async def synchronize_fortress_phase(self, projection):
        """Project server chronology in the current Fortress load."""
        evidence = read_gameplay_save_evidence()
        if canonical_map_name(self.ctx.runtime_lifecycle.map_identity.current_map) != "game/hub/hub" or not (await self.ctx.items.runtime_effects_ready(evidence)):
            return
        lease = (await self.ctx.items._active_materialization_lease())
        if lease is None:
            return
        phase = projection["fortress_phase"]
        await self.ctx.items._send_runtime_command(f"ai_ScriptCmdEnt ap_fortress_phase_{phase} activate",
                                         coalesce_key=f"fortress-{lease}-{phase}", materialization_lease=lease)

    async def _provider_evidence_matches(self, evidence, admitted):
        if evidence is not None and evidence.state == "gameplay":
            from doom_eap.contracts.save_observation import admitted_evidence_matches
            link = (await self.ctx.items._native_game_link_async())
            facts = getattr(link, "save_admission", {})
            if not admitted_evidence_matches(evidence, admitted, link.namespace, link.pid, facts):
                self.ctx.save_observer.set_frozen(True)
                self.log_save_proof_rejected("provider_process_evidence_mismatch",
                    evidence_slot=evidence.slot_directory, candidate_slot=admitted.slot_directory,
                    candidate_mtime=admitted.mtime_ns, active_slot=self.ctx.save_observer.selection.slot, marker_map=evidence.map_name)
                return False
        return True

    def _save_evidence_context(self, evidence):
        evidence_context = None
        if (
            evidence
            and evidence.state == "gameplay"
            and getattr(evidence, "native_safe", False)
            and getattr(evidence, "map_name", None)
        ):
            evidence_context = classify_runtime_context(evidence.map_name)
        return evidence_context

    def _compare_save_context(self, marker_map, evidence, marker_context, evidence_context):
        if marker_map and evidence and getattr(evidence, "map_name", None):
            if (
                marker_context is not None
                and evidence_context is not None
                and marker_context.identity != evidence_context.identity
            ):
                self.ctx.items._record_context_evidence_rejection(
                    "mission_select_save_details",
                    marker_context.identity,
                    evidence_context.identity,
                )

    def _save_evidence_identity(self, evidence, expected_prefix):
        evidence_slot = evidence.slot_directory if (evidence and getattr(evidence, "slot_directory", None)) else None
        if expected_prefix and evidence_slot and not evidence_slot.startswith(expected_prefix):
            evidence_slot = None
        evidence_epoch = evidence.epoch if (evidence and getattr(evidence, "epoch", None) is not None) else None
        return evidence_slot, evidence_epoch

    async def update_save_slot_lifecycle(self):
        """Keep an authoritative slot through transient samples; prove switches."""

        evidence = read_gameplay_save_evidence()
        admitted = (await self.admitted_campaign_save())
        if admitted is None:
            return None
        if not await self._provider_evidence_matches(evidence, admitted):
            return None
        (await self.ctx.checks.ingest_visible_runtime_lifecycle(evidence=evidence))
        marker = (await self.ctx.checks.read_active_map_identity(evidence=evidence))
        marker_map = marker["runtime_map"] if marker else None
        marker_context = classify_runtime_context(marker_map)
        evidence_context = self._save_evidence_context(evidence)
        expected_prefix = "GAME-"  # physical ap save family is independent of content

        active_family_mismatch, prior_evidence_epoch = self.ctx.save_observer.observe_expected_family(expected_prefix)

        self._compare_save_context(marker_map, evidence, marker_context, evidence_context)

        evidence_slot, evidence_epoch = self._save_evidence_identity(evidence, expected_prefix)

        if self.ctx.save_observer.observe_candidate(admitted):
            bridge_config.logger.info(
                "SAVE_SLOT_CANDIDATE slot=%s path=%s mtime_ns=%s",
                admitted.slot_directory, admitted.path, admitted.mtime_ns,
            )

        lease = getattr(self.ctx, "runtime_observation_lease", None)
        lease_epoch = getattr(lease, "gameplay_loaded_ns", None) or None

        proof_evidence_epoch = (
            evidence_epoch
            if evidence_epoch is not None
            else getattr(self.ctx.save_observer.readiness, "evidence_epoch", None)
        )
        proof_load_epoch = lease_epoch

        newest = admitted
        active = admitted if admitted.slot_directory == self.ctx.save_observer.selection.slot else None
        candidate_slot = newest.slot_directory
        candidate_mtime = newest.mtime_ns

        provisional_family_switch = self.ctx.save_observer.permits_provisional_family_switch(
            evidence, marker_absent=marker is None, context_known=evidence_context is not None,
            expected_prefix=expected_prefix, active_family_mismatch=active_family_mismatch,
            newest=newest, active=active, evidence_epoch=evidence_epoch,
            prior_evidence_epoch=prior_evidence_epoch,
        )

        def fail_proof(reason):
            self.ctx.save_observer.set_frozen(True)
            self.ctx.save_observer.clear_mission_select()
            self.log_save_proof_rejected(
                reason,
                evidence_slot=evidence_slot,
                marker_map=marker_map,
                candidate_slot=candidate_slot,
                candidate_mtime=candidate_mtime,
                active_slot=self.ctx.save_observer.selection.slot,
            )
            return None

        decision = self.ctx.save_observer.select_candidate(
            evidence=evidence, marker_present=marker is not None,
            evidence_slot=evidence_slot, expected_prefix=expected_prefix,
            newest=newest, active=active,
            process_running=lease is None or lease.process_probe(),
            load_epoch=proof_load_epoch,
            mission_select_map=self.ctx.save_observer.mission_select.map_name,
            provisional_family_switch=provisional_family_switch,
        )
        if decision.action != "read_details":
            return await self._continue_save_candidate(decision, fail_proof)

        return await self._read_candidate_proof(
            admitted, decision, marker_map, marker, evidence, provisional_family_switch,
            lease, proof_evidence_epoch, proof_load_epoch, evidence_epoch, fail_proof,
        )

    async def observe_active_game_details(self):

        selected = (await self.update_save_slot_lifecycle())
        return read_game_details_for_selection(selected, bridge_config.STEAM_ID3, bridge_config.logger) if selected else None

    def _mission_select_required(self, details, active_map):
        from doom_eap.runtime import bridge_checks

        continue_target_map = canonical_map_name(details.get("mapName", ""))
        marker_context = classify_runtime_context(active_map)
        details_context = classify_runtime_context(continue_target_map)
        cross_campaign_details_lag = bool(
            marker_context is not None
            and details_context is not None
            and marker_context.campaign != details_context.campaign
        )
        if cross_campaign_details_lag:
            self.ctx.items._record_context_evidence_rejection(
                "cross_campaign_save_details_lag",
                marker_context.identity,
                details_context.identity,
            )
        return self.ctx.save_observer.requires_mission_select(
            active_map, continue_target_map,
            cross_campaign_details_lag=cross_campaign_details_lag,
            challenge_maps=bridge_checks.MISSION_CHALLENGE_RUNTIME_MAPS,
        )

    def _accept_mission_select_lease(self, active_map, lease, selected):

        if self.ctx.save_observer.accept_mission_select(active_map, lease.gameplay_loaded_ns):
            bridge_config.logger.info(
                "[OBSERVER] MISSION_SELECT_LEASE_ACCEPTED slot=%s map=%s "
                "load_epoch=%s save_mtime_ns=%s",
                selected.slot_directory,
                active_map,
                lease.gameplay_loaded_ns,
                selected.mtime_ns,
            )

    def _validate_save_lease(self, lease, marker, evidence, active_map, selected, mission_select_required):

        try:
            evidence_mtime_ns = Path(bridge_config.GAMEPLAY_SAVE_EVIDENCE_PATH).stat().st_mtime_ns
        except OSError:
            evidence_mtime_ns = marker["mtime_ns"] if marker else 0
        live, reason = (False, "mission_select_required") if mission_select_required else lease.validate(
            evidence_mtime_ns=evidence_mtime_ns,
            evidence_state=evidence.state if evidence else "gameplay",
            current_map=active_map,
        )
        mission_select_live = False
        if not live:
            mission_select_live, reason = self._validate_mission_select_lease(
                lease, marker, evidence, active_map, selected, mission_select_required, evidence_mtime_ns, reason,
            )
        elif live:
            self.ctx.save_observer.clear_mission_select()
        if not live and not mission_select_live:
            if reason != self.ctx.last_observer_lease_block:
                bridge_config.logger.info("[OBSERVER] LIVE_LEASE_BLOCKED reason=%s", reason)
                self.ctx.last_observer_lease_block = reason
            return reason
        self.ctx.last_observer_lease_block = None

        return None

    def _accept_provisional_map(self, evidence_epoch, lease, active_map):
        from doom_eap.runtime import bridge_checks

        evidence_mtime = bridge_config.gameplay_evidence_mtime_ns()
        materialization_epoch = build_materialization_epoch(
            evidence_epoch, evidence_mtime
        )
        if lease is not None:
            lease.observe_gameplay_loaded(evidence_mtime)
        marker_data = self.ctx.runtime_lifecycle.new_map_proposal(
            bridge_checks._materialization_map_key(active_map), active_map, evidence_epoch,
            evidence_mtime, materialization_epoch,
        )
        self.ctx.checks.accept_map_identity(marker_data, evidence_epoch)
        self.ctx.checks.snapshot_fast_travel_eligibility(marker_data=marker_data)
        self.ctx.checks.advance_automap_cleanup_epoch()
        bridge_config.logger.info(
            "[MAP] MAP_TRANSITION_EVIDENCE map=%s epoch=%s runtime_map=%s "
            "source=provisional_cross_campaign_load_edge",
            marker_data["map_key"], materialization_epoch, active_map,
        )

    async def _accept_save_proof(self, proof, selected, active_map, proof_evidence_epoch, proof_load_epoch, evidence_epoch, details, details_token, provisional_family_switch, lease):
        if proof.action == "activate":

            # save_proof_accepted must precede save_slot_active
            self.log_save_proof_accepted(
                selected.slot_directory,
                active_map,
                proof_evidence_epoch,
                selected.mtime_ns,
                details_token,
                proof=(
                    "provisional_cross_campaign_load_edge"
                    if provisional_family_switch
                    else "non_provisional_fresh_map_match"
                ),
            )
            self.activate_save_selection(selected)
            self.ctx.save_observer.update_selection(native_evidence_epoch=proof_evidence_epoch)
            self.ctx.save_observer.accept_proof(selected.slot_directory, proof_evidence_epoch, proof_load_epoch)
            if provisional_family_switch:
                self._accept_provisional_map(evidence_epoch, lease, active_map)
            self.ctx.goals.arm_candidate(
                selected, details, active_map, proof_load_epoch
            )
            (await self.ctx.checks.reconcile_fast_travel_unlock("save_proof"))
            return selected
        else:
            if not self.ctx.save_observer.mission_select.map_name:
                self.ctx.save_observer.clear_mission_select()
            if proof.new_evidence:
                self.log_save_proof_accepted(
                    selected.slot_directory,
                    active_map,
                    evidence_epoch,
                    selected.mtime_ns,
                    details_token,
                    proof="non_provisional_fresh_map_match",
                )
                if proof.reset_observation_slot:
                    self.invalidate_save_observation_slot(selected.slot_directory)
                self.ctx.save_observer.update_selection(native_evidence_epoch=evidence_epoch, token=selected.mtime_ns)

            self.ctx.save_observer.accept_proof(selected.slot_directory, proof_evidence_epoch, proof_load_epoch)
            self.ctx.goals.arm_candidate(
                selected, details, active_map, proof_load_epoch
            )
            (await self.ctx.checks.reconcile_fast_travel_unlock("save_proof"))
            return selected

    def _proof_active_map(self, marker_map, evidence, provisional_family_switch):
        return marker_map or (canonical_map_name(evidence.map_name) if provisional_family_switch and evidence else None)

    async def _read_candidate_proof(self, admitted, decision, marker_map, marker, evidence, provisional_family_switch, lease, proof_evidence_epoch, proof_load_epoch, evidence_epoch, fail_proof):

        selected = admitted if admitted.slot_directory == decision.target_slot else None
        if selected is None:
            return fail_proof("no_gameplay_evidence")

        details = read_game_details_for_selection(selected, bridge_config.STEAM_ID3, bridge_config.logger)
        if not details:
            return fail_proof("no_game_details")

        active_map = self._proof_active_map(marker_map, evidence, provisional_family_switch)
        if not active_map:
            return fail_proof("map_marker_unavailable")
        mission_select_required = self._mission_select_required(details, active_map)
        if lease is not None:
            lease_error = self._validate_save_lease(lease, marker, evidence, active_map, selected, mission_select_required)
            if lease_error is not None:
                return fail_proof(lease_error)

        proof = self.ctx.save_observer.plan_proof(
            selected, proof_evidence_epoch=proof_evidence_epoch, evidence_epoch=evidence_epoch,
        )

        details_token = details.get("_mtime_ns", selected.mtime_ns)

        if proof.action == "reject":
            return fail_proof(proof.reason)

        return await self._accept_save_proof(
            proof, selected, active_map, proof_evidence_epoch, proof_load_epoch, evidence_epoch,
            details, details_token, provisional_family_switch, lease,
        )

    async def _continue_save_candidate(self, decision, fail_proof):

        if decision.action == "reject":
            if decision.reason == "game_not_running" and self.ctx.last_observer_lease_block != "game_not_running":
                bridge_config.logger.info("[OBSERVER] LIVE_LEASE_BLOCKED reason=game_not_running")
                self.ctx.last_observer_lease_block = "game_not_running"
            return fail_proof(decision.reason)
        if decision.action == "continue":
            self.ctx.save_observer.update_selection(path=str(decision.continued.path))
            if decision.reset_observation_slot:
                self.invalidate_save_observation_slot(decision.continued.slot_directory)
            self.ctx.save_observer.continue_selection(decision.continued)
            (await self.ctx.checks.reconcile_fast_travel_unlock("save_proof"))
            return decision.continued

    def _validate_mission_select_lease(self, lease, marker, evidence, active_map, selected, mission_select_required, evidence_mtime_ns, reason):
        mission_select_live = False
        if mission_select_required:
            mission_select_live, reason = lease.validate_mission_select(
                evidence_mtime_ns=evidence_mtime_ns or (marker["mtime_ns"] if marker else 0),
                evidence_state=evidence.state if evidence else "gameplay",
                current_map=active_map,
                mission_map=active_map,
                save_mtime_ns=selected.mtime_ns,
            )
        if mission_select_live:
            self._accept_mission_select_lease(active_map, lease, selected)
        else:
            self.ctx.save_observer.clear_mission_select()
        return mission_select_live, reason


class SaveObserver:
    def __init__(self, readiness=None, *, baselines=None):
        self._readiness = readiness if readiness is not None else SaveReadinessSnapshot()
        self._selection = SaveSelectionSnapshot()
        self._candidate_tokens = {}
        self._slot_observations = {}
        self._observation_slot = None
        self._duration_cache_key = None
        self._records = {}
        self._proof_revision = 0
        self._baselines = baselines if baselines is not None else SaveObserverBaselineStore({})
        self._mission_select = MissionSelectObservation()
        self._last_mission_select_epoch = None

    @property
    def mission_select(self):
        return self._mission_select

    def clear_mission_select(self):
        self._mission_select = MissionSelectObservation()

    def accept_mission_select(self, map_name, epoch):
        self._mission_select = MissionSelectObservation(map_name, epoch)
        changed = self._last_mission_select_epoch != epoch
        self._last_mission_select_epoch = epoch
        return changed

    def plan_proof(self, selected, *, proof_evidence_epoch, evidence_epoch):
        same_slot = self._selection.slot == selected.slot_directory and self._selection.path == str(selected.path)
        if not same_slot:
            if self._selection.native_evidence_epoch == proof_evidence_epoch and self._selection.slot is not None:
                return SaveProofDecision("reject", reason="unproven_epoch")
            return SaveProofDecision("activate", new_evidence=True)
        new_evidence = evidence_epoch is not None and self._selection.native_evidence_epoch != evidence_epoch
        return SaveProofDecision(
            "refresh", new_evidence=new_evidence,
            reset_observation_slot=new_evidence and self._selection.token != selected.mtime_ns,
        )

    @staticmethod
    def requires_mission_select(active_map, continue_target_map, *, cross_campaign_details_lag, challenge_maps):
        if cross_campaign_details_lag:
            continue_target_map = active_map
        return bool(active_map in challenge_maps and continue_target_map and continue_target_map != active_map)

    def bind_baselines(self, store):
        self._baselines = store

    def bind_ap_provider(self, namespace, root):
        self._ap_provider = namespace + ":" + root

    def capture_observation(self):
        return self._proof_revision, self._selection, self._readiness, self._mission_select

    def observation_is_current(self, token):
        return token == self.capture_observation()

    def duration_is_current(self, selected):
        return selected.cache_key == self._duration_cache_key

    def accept_duration(self, selected):
        self._duration_cache_key = selected.cache_key

    def observe_record(self, kind, slot, unlockable, record):
        """Remember a decoded record and report whether its diagnostic changed."""
        key = (kind, slot, unlockable)
        if self._records.get(key) == record:
            return False
        self._records[key] = record
        return True

    def observe_edges(self, *, session_identity, team, slot, registry_revision,
                      doom_save_slot, observer_key, records, acknowledged_records,
                      first_sample_pending=frozenset()):
        """Select the durable baseline independently of feature publication."""
        binding_key = self._baselines.binding_key(
            session_identity=session_identity + ":" + getattr(self, "_ap_provider", "unbound"), team=team, slot=slot,
            doom_save_slot=doom_save_slot, registry_revision=registry_revision,
        )
        return self._baselines.observe(
            binding_key=binding_key, observer_key=observer_key,
            records=records, acknowledged_records=acknowledged_records,
            first_sample_pending=first_sample_pending,
        )

    @property
    def observation_slot(self):
        return self._observation_slot

    @property
    def observation_document(self):
        """Live persistence projection; slot entries are mutated only by this owner."""
        return self._slot_observations

    def restore_slot_observations(self, raw):
        if not isinstance(raw, dict):
            raw = {}
        self._slot_observations = {
            slot: state for slot, state in raw.items()
            if re.fullmatch(r"(?:GAME|DLC[12]|HORDE)-AUTOSAVE\d+", str(slot))
            and isinstance(state, dict)
        }
        self._observation_slot = None
        return self._slot_observations

    def select_observation_slot(self, slot):
        if self._observation_slot == slot:
            return False
        self._observation_slot = slot
        state = self._slot_observations.setdefault(slot, {})
        state.pop("weapon_masteries", None)
        state.pop("mission_challenges", None)
        return True

    def invalidate_observation_slot(self, slot):
        self._slot_observations[slot] = {}
        self._observation_slot = None

    def observe_candidate(self, selected):
        token = (str(selected.path), selected.mtime_ns)
        if self._candidate_tokens.get(selected.slot_directory) == token:
            return False
        self._candidate_tokens[selected.slot_directory] = token
        return True

    @property
    def selection(self):
        return self._selection

    def update_selection(self, **changes):
        self._selection = replace(self._selection, **changes)

    @property
    def readiness(self):
        return self._readiness

    def invalidate_proof(self):
        self._proof_revision += 1
        self._duration_cache_key = None
        self._readiness = SaveReadinessSnapshot()

    def activate_slot(self, slot):
        self._readiness = replace(self._readiness, authoritative=True, slot=slot)

    def accept_proof(self, slot, evidence_epoch, load_epoch):
        self._readiness = SaveReadinessSnapshot(True, slot, evidence_epoch, load_epoch, False)

    def set_frozen(self, frozen):
        self._readiness = replace(self._readiness, frozen=frozen)

    def observe_expected_family(self, expected_prefix):
        mismatch = bool(
            expected_prefix and self._selection.slot
            and not self._selection.slot.startswith(expected_prefix)
        )
        prior_epoch = self._readiness.evidence_epoch
        if mismatch:
            self.invalidate_proof()
            self.update_selection(native_evidence_epoch=None)
        return mismatch, prior_epoch

    def _provisional_sample(self, evidence, marker_present, provisional_family_switch):
        return evidence and evidence.provisional and not marker_present and not provisional_family_switch

    def select_candidate(self, *, evidence, marker_present, evidence_slot,
                         expected_prefix, newest, active, process_running,
                         load_epoch, mission_select_map, provisional_family_switch):
        """Resolve observation admission from discovered facts, without IO/effects."""
        newer_unproven = bool(
            newest and active and newest.slot_directory != active.slot_directory
            and newest.mtime_ns > active.mtime_ns
        )

        def reject(reason):
            self.set_frozen(True)
            return SaveCandidateDecision("reject", reason=reason)

        if not process_running:
            self.invalidate_proof()
            return reject("game_not_running")
        if evidence is None and not marker_present:
            return self._continue_candidate("no_gameplay_evidence", mission_select_map, load_epoch, active, newer_unproven)
        if evidence and evidence.state != "gameplay":
            self.invalidate_proof()
            return reject("menu")
        if self._provisional_sample(evidence, marker_present, provisional_family_switch):
            return self._continue_candidate("provisional", mission_select_map, load_epoch, active, newer_unproven)
        return self._read_details_candidate(newest, active, evidence_slot, expected_prefix)

    def continue_selection(self, selected):
        self.update_selection(path=str(selected.path), token=selected.mtime_ns)
        self.set_frozen(False)

    @staticmethod
    def permits_provisional_family_switch(
        evidence, *, marker_absent, context_known, expected_prefix, active_family_mismatch,
        newest, active, evidence_epoch, prior_evidence_epoch,
    ):
        if not (evidence and evidence.state == "gameplay" and evidence.provisional and evidence.native_safe
                and marker_absent and context_known and expected_prefix and active_family_mismatch):
            return False
        return SaveObserver._newer_family_save(newest, active, expected_prefix, evidence_epoch, prior_evidence_epoch)

    def has_authoritative_proof(self, *, lease_present, gameplay_loaded_ns):
        proof = self._readiness
        if proof.frozen:
            return False
        if proof.authoritative is False:
            return False
        if lease_present and proof.load_epoch != gameplay_loaded_ns:
            return False
        if self._selection.slot is not None:
            return bool(proof.slot == self._selection.slot)
        return True


    def _continue_candidate(self, reason, mission_select_map, load_epoch, active, newer_unproven):
        proof = self._readiness
        if (
            mission_select_map or not proof.authoritative
            or proof.slot != self._selection.slot or proof.load_epoch != load_epoch
            or active is None or newer_unproven
        ):
            self.set_frozen(True)
            return SaveCandidateDecision("reject", reason=reason)
        reset = self._selection.token != active.mtime_ns
        # Coordinator applies any feature-view reset before committing the token.
        return SaveCandidateDecision(
            "continue", continued=active, reset_observation_slot=reset,
        )


    @staticmethod
    def _newer_family_save(newest, active, expected_prefix, evidence_epoch, prior_evidence_epoch):
        return bool(newest and newest.slot_directory.startswith(expected_prefix) and active
                    and newest.mtime_ns > active.mtime_ns and evidence_epoch is not None
                    and evidence_epoch != prior_evidence_epoch)


    def _read_details_candidate(self, newest, active, evidence_slot, expected_prefix):
        candidate_slot = newest.slot_directory if newest else None
        target = evidence_slot or (active.slot_directory if active else None) or candidate_slot
        if expected_prefix and target and not target.startswith(expected_prefix):
            target = candidate_slot
        if not target or not re.match(r"^(?:GAME|DLC[12]|HORDE)-AUTOSAVE[0-9]+$", target):
            self.set_frozen(True)
            return SaveCandidateDecision("reject", reason="invalid_evidence_slot")
        return SaveCandidateDecision("read_details", target_slot=target)


class SaveObserverBaselineStore:
    """Persistent false-to-true edges bound to AP identity and Doom save slot."""

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

    def _initial_observer(self, observers, observer_key, first_sample_pending, records, acknowledged_records):
        recovered = {
            key for key in first_sample_pending
            if records.get(key) and key not in acknowledged_records
        }
        observers[observer_key] = {
            "baseline_preexisting": sorted(
                key for key, complete in records.items() if complete
            ),
            "last_observed": {
                key: bool(complete) for key, complete in records.items()
            },
            "pending_edges": sorted(recovered),
        }
        return recovered, True, recovered

    def observe(
        self,
        *,
        binding_key: str,
        observer_key: str,
        records: Mapping[str, bool],
        acknowledged_records: set[str],
        first_sample_pending: frozenset[str] = frozenset(),
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
            return self._initial_observer(observers, observer_key, first_sample_pending, records, acknowledged_records)

        previous = observer.setdefault("last_observed", {})
        pending = set(observer.setdefault("pending_edges", []))
        pending.update(key for key in first_sample_pending if records.get(key) and key not in acknowledged_records)
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




















def _read_startup_record(config, process_identity, namespace):
    from doom_eap.launcher.sentinel_diagnostics import diagnostics_directory

    _, pid, created = process_identity.split(":")
    directory = diagnostics_directory(config)
    if directory is None:
        raise ValueError("native diagnostics directory unavailable")
    with (directory / f"{int(pid)}-{int(created)}.latest.json").open("rb") as stream:
        raw = stream.read(131073)
    if len(raw) > 131072:
        raise ValueError("checkpoint diagnostics too large")
    record = json.loads(raw)
    if (record["schema"] != "sentinel-startup-v1" or record["pid"] != int(pid)
            or record["process_created"] != created
            or record["admission"]["namespace_id"] != namespace):
        raise ValueError("checkpoint diagnostics identity mismatch")
    return record


def _checkpoint_recovery(record, process_identity):
    recovery = record["b_diagnostics"]["stages"]["checkpoint_recovery"]
    if recovery is None:
        return CheckpointRecovery(CheckpointRecoveryState.READY, "none")
    status, reason = recovery["status"], recovery["predicate"]
    token = process_identity, recovery["sequence"]
    if status in (1, 2):
        return CheckpointRecovery(CheckpointRecoveryState.RECOVERING, reason, token)
    if status != 3:
        return CheckpointRecovery(CheckpointRecoveryState.BLOCKED, reason, token)
    campaign = record["campaign"]
    verified = (campaign["resumed"] is True and campaign["source_verified"] is True
                and campaign["parser_completed"] is True and campaign["parser_result"] == 0)
    state = CheckpointRecoveryState.READY if verified else CheckpointRecoveryState.RESUME_PENDING
    return CheckpointRecovery(state, reason, token)


def _primary_save_candidate(path):
    try:
        stat = path.stat()
        full_path = path.resolve()
    except OSError:
        return None
    if not path.is_file() or stat.st_size <= 0:
        return None
    return PrimarySaveSelection(path.parent.name, full_path, stat.st_mtime_ns)


def _read_serialized_uint(payload, offset):
    """Read one width-prefixed little-endian unsigned value."""
    if offset >= len(payload):
        raise ValueError("metric value width is missing")
    width = payload[offset]
    if width < 1 or width > 8 or offset + 1 + width > len(payload):
        raise ValueError(f"invalid metric value width {width}")
    return (
        int.from_bytes(payload[offset + 1:offset + 1 + width], "little"),
        offset + 1 + width,
    )


def _read_structured_bool(payload, offset, field):
    if not payload.startswith(field, offset):
        raise ValueError(f"unlockable record missing {field.decode('ascii').strip()}")
    value_offset = offset + len(field)
    try:
        value = {0x0B: False, 0x0C: True}[payload[value_offset]]
    except (IndexError, KeyError) as error:
        raise ValueError(f"unlockable record has invalid {field.decode('ascii').strip()}") from error
    return value, value_offset + 1


def _mastery_manager_type_offset(payload):
    manager_offset = payload.find(MASTERY_MANAGER)
    if manager_offset < 0 or payload.find(MASTERY_MANAGER, manager_offset + 1) >= 0:
        raise ValueError("native unlockable manager is missing or ambiguous")
    manager_type_offset = payload.find(MASTERY_MANAGER_TYPE, manager_offset)
    if (
        manager_type_offset < 0
        or payload.find(MASTERY_MANAGER_TYPE, manager_type_offset + 1) >= 0
    ):
        raise ValueError("native unlockable manager type is missing or ambiguous")
    return manager_type_offset


DEFAULT_REMOTE = discover_default_remote()
MASTERY_MANAGER = b"UnlockableManager_0_1_2"
MASTERY_MANAGER_TYPE = b"idUnlockableManager_2"
CHALLENGE_LOCATION_REGISTRY = load_challenge_registry()
OBSERVER_REGISTRY_REVISION = observer_registry_revision(CHALLENGE_LOCATION_REGISTRY)
WEAPON_MASTERY_ENTRIES = tuple(CHALLENGE_LOCATION_REGISTRY["weapon_masteries"])
WEAPON_MASTERY_BY_UNLOCKABLE = {
    entry["signal"]["unlockable"]: entry
    for entry in WEAPON_MASTERY_ENTRIES
}
MISSION_CHALLENGE_ENTRIES = tuple(
    CHALLENGE_LOCATION_REGISTRY["mission_challenges"]
)
MISSION_CHALLENGE_BY_UNLOCKABLE = {
    entry["signal"]["unlockable"]: entry
    for entry in MISSION_CHALLENGE_ENTRIES
}
MISSION_CHALLENGE_RUNTIME_MAP_BY_UNLOCKABLE = {
    entry["signal"]["unlockable"]: canonical_map_name(entry["runtime_map"])
    for entry in MISSION_CHALLENGE_ENTRIES
}
ALL_MISSION_CHALLENGES_ENTRIES = list(
    CHALLENGE_LOCATION_REGISTRY.get("all_mission_challenges", [])
)
STICKY_MASTERY_ENTRY = WEAPON_MASTERY_BY_UNLOCKABLE[
    "weapon_mastery/shotgun/sticky_bomb"
]
STICKY_MASTERY_LOCATION = STICKY_MASTERY_ENTRY["location_id"]
STICKY_UNLOCKABLE = b"weapon_mastery/shotgun/sticky_bomb"


if __name__ == "__main__":
    decrypt_main()
