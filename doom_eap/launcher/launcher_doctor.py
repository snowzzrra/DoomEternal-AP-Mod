"""Bounded launcher diagnostics and sanitized support bundle generation."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from doom_eap.launcher.ui.strings import text as player_text

from .launcher_integration import UNINSTALL_OWNED_STATES
from .launcher_native_health import NativeHealthReader, doom_base_dir_from_config
from .launcher_platform import (
    AMMO_HOTKEY_STATE_FILENAME,
    AMMO_REFILL_BIND_COMMAND,
    LINUX_MOD_INJECTOR,
    WINDOWS_INJECTOR_REQUIRED_MEMBERS,
    WINDOWS_MOD_INJECTOR,
    DependencyManager,
    PrerequisiteStatus,
    ProcessInspectionError,
    SavedGamesSelection,
    detect_doom_processes,
    doom_saved_games_base,
    idfile_decompressor_spec,
    installation_error_details,
    probe_meathook,
    publish_file,
    redact_secrets,
    resolve_doom_config_path,
    saved_games_shape_verdict,
    select_saved_games_dir,
    validate_game_root,
    verify_linux_mod_installation,
)
from .sentinel_diagnostics import diagnostics_directory

SUPPORT_LOG_TAIL_BYTES = 256 * 1024
SUPPORT_LOG_MAX_BYTES = 1024 * 1024
SUPPORT_DIAGNOSTIC_MAX_BYTES = 256 * 1024
SUPPORT_SPAWN_DIAGNOSTIC_MAX_BYTES = 16 * 1024 * 1024
SUPPORT_DIAGNOSTIC_MAX_ITEMS = 24
SUPPORT_BUNDLE_MAX_BYTES = 10 * 1024 * 1024
_CREDENTIAL_FIELDS = frozenset({
    "password", "passwd", "passphrase", "authorization", "token", "secret",
    "access_token", "api_token", "auth_token", "refresh_token", "id_token",
    "bearer_token", "oauth_token", "oauth_token_secret", "session_token",
    "client_secret", "consumer_secret", "webhook_secret", "secret_key",
    "api_key", "private_key", "signing_key",
})


@dataclass(frozen=True)
class Diagnostic:
    key: str
    status: str
    message: str
    details: Mapping[str, object] | None = None


@dataclass(frozen=True)
class RepairAction:
    """Small, previewable repair limited to launcher-owned state."""

    action_id: str
    title: str
    changes: tuple[str, ...]
    requires_confirmation: bool = False
    rollback: str = ""

    @property
    def key(self) -> str:
        return self.action_id


@dataclass(frozen=True)
class DoctorReport:
    version: str
    diagnostics: tuple[Diagnostic, ...]
    summary_status: str = "ready"
    failure_domain: str = ""
    recovery_action: str = ""
    summary_message: str = ""
    support_diagnostics: Mapping[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.summary_status == "ready" and all(item.status not in {"error", "invalid", "missing", "incompatible", "failed", "attention"} for item in self.diagnostics)

    def document(self) -> dict[str, object]:
        return {
            "version": self.version,
            "ok": self.ok,
            "status": self.summary_status,
            "summary_status": self.summary_status,
            "failure_domain": self.failure_domain,
            "recovery_action": self.recovery_action,
            "summary_message": self.summary_message,
            "diagnostics": [asdict(item) for item in self.diagnostics],
            "support_diagnostics": dict(self.support_diagnostics),
        }


def _safe_path(value: object) -> str:
    text = str(value)
    # profile paths can belong to a different Windows account
    text = re.sub(r"(?i)([a-z]:[\\/]+Users[\\/]+)[^\\/\r\n\"<>]+", r"\1[USER]", text)
    text = re.sub(r"(/(?:home|Users)/)[^/\r\n\"<>]+", r"\1[USER]", text)
    return text


def _sanitize_support_text(text: str) -> str:
    decoder = json.JSONDecoder()
    pieces = []
    offset = 0
    while (start := text.find("{", offset)) != -1:
        pieces.append(text[offset:start])
        try:
            value, length = decoder.raw_decode(text[start:])
        except ValueError:
            pieces.append("{")
            offset = start + 1
        else:
            pieces.append(json.dumps(sanitize_support_value(value), ensure_ascii=False))
            offset = start + length
    text = "".join(pieces) + text[offset:]
    private_field = re.compile(r'''(?i)["']?\b[\w-]*private["']?\s*[=:]\s*''')
    while match := private_field.search(text):
        start = match.end()
        try:
            _, length = decoder.raw_decode(text[start:])
            end = start + length
        except ValueError:
            end = len(text) if text[start:start + 1] in {"{", "["} else text.find("\n", start)
            if end == -1:
                end = len(text)
        text = text[:match.start()] + player_text('launcher.doctor', 'redacted') + text[end:]
    fields = "|".join(sorted(_CREDENTIAL_FIELDS, key=len, reverse=True))
    text = re.sub(
        rf"""(?ix)(["']?\b(?:[\w-]+_)?(?:{fields})["']?\s*[=:]\s*)
        (?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|(?:Bearer|Basic)\s+\S+|[^\s&,;}}]+)""",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)(\b(?:wss?|https?)://)[^/\s@]+@", r"\1[REDACTED]@", text)
    return redact_secrets(_safe_path(text))


def sanitize_support_value(value: object, *, key: str = "") -> object:
    lowered = key.casefold().replace("-", "_")
    if lowered == "gameplay_authorization" and value == "unvalidated":
        return value
    if lowered in _CREDENTIAL_FIELDS or any(lowered.endswith("_" + field) for field in _CREDENTIAL_FIELDS):
        return player_text('launcher.doctor', 'redacted')
    if isinstance(value, Mapping):
        return {_sanitize_support_text(str(name)): sanitize_support_value(item, key=str(name))
                for name, item in value.items() if not str(name).casefold().endswith("private")}
    if isinstance(value, list | tuple):
        return [sanitize_support_value(item, key=key) for item in value]
    if isinstance(value, Path | str):
        return _sanitize_support_text(str(value))
    return value


def sanitize_support_bundle(document: Mapping[str, object]) -> dict[str, object]:
    return sanitize_support_value(dict(document))  # type: ignore[return-value]


def _configured_paths(value: object) -> tuple[Path, ...]:
    if isinstance(value, str | Path):
        values = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        values = tuple(value)
    else:
        return ()
    paths: list[Path] = []
    for item in values:
        try:
            paths.append(Path(str(item)).expanduser())
        except (OSError, TypeError, ValueError):
            continue
    return tuple(paths)


def _bridge_log_candidates(
    config: Mapping[str, object] | None,
    paths: object | None,
    application_dir: Path | None = None,
) -> tuple[Path, ...]:
    config = config or {}
    candidates: list[Path] = []
    for key in ("bridge_log_path", "bridge_log_file"):
        for path in _configured_paths(config.get(key)):
            candidates.extend((path, path.with_name("bridge.previous.log")))

    directories: list[Path] = []
    directories.extend(_configured_paths(config.get("bridge_log_dir")))
    for name in ("config_dir", "state_dir", "data_dir"):
        value = getattr(paths, name, None)
        for root in _configured_paths(value):
            directories.extend((root, root / "logs"))
    if application_dir is not None:
        directories.append(application_dir / "logs")

    if os.name == "nt":
        user_root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        user_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    directories.extend((user_root / "doom-eternal-ap" / "logs", user_root / "doom-eternal-archipelago" / "logs"))
    for directory in directories:
        candidates.extend((directory / "bridge.log", directory / "bridge.previous.log"))
    return tuple(dict.fromkeys(candidates))


def _native_log_candidates(config: Mapping[str, object] | None) -> tuple[Path, ...]:
    config = config or {}
    configured_path = config.get("native_log_path") or config.get("native_log_file")
    if configured_path:
        paths: list[Path] = []
        for path in _configured_paths(configured_path):
            paths.extend((path, path.with_name("ap_client.previous.log")))
        return tuple(dict.fromkeys(paths))
    configured = config.get("game_root") or config.get("doom_base_dir")
    if not configured:
        return ()
    try:
        root = Path(str(configured)).expanduser()
        if root.name.casefold() == "base":
            root = root.parent
    except (OSError, TypeError, ValueError):
        return ()
    base = root / "base"
    return (base / "ap_client.log", base / "ap_client.previous.log")


def _launcher_log_candidates(
    config: Mapping[str, object] | None,
    paths: object | None,
    application_dir: Path | None = None,
) -> tuple[Path, ...]:
    """Find launcher session logs without manufacturing a path or file."""
    config = config or {}
    candidates: list[Path] = []
    for key in ("launcher_log_path", "launcher_log_file"):
        for path in _configured_paths(config.get(key)):
            candidates.extend((path, path.with_name("launcher.previous.log")))
    directories: list[Path] = []
    for key in ("launcher_log_dir",):
        directories.extend(_configured_paths(config.get(key)))
    for name in ("config_dir", "state_dir", "data_dir"):
        for root in _configured_paths(getattr(paths, name, None)):
            directories.extend((root, root / "logs"))
    if application_dir is not None:
        directories.append(application_dir / "logs")
    for directory in directories:
        candidates.extend((directory / "launcher.log", directory / "launcher.previous.log"))
    return tuple(dict.fromkeys(candidates))


def _read_log_tail(path: Path, limit: int = SUPPORT_LOG_TAIL_BYTES) -> str | None:
    try:
        with path.open("rb") as source:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            if size <= 0:
                return None
            start = max(0, size - limit)
            source.seek(start)
            payload = source.read(limit)
        if start:
            line_start = payload.find(b"\n")
            if line_start < 0:
                return None
            payload = payload[line_start + 1 :]
        text = _sanitize_support_text(payload.decode("utf-8", errors="replace"))
        encoded = text.encode("utf-8")
        if len(encoded) > limit:
            encoded = encoded[-limit:]
            line_start = encoded.find(b"\n")
            if line_start < 0:
                return None
            text = encoded[line_start + 1 :].decode("utf-8", errors="replace")
        return text
    except (OSError, UnicodeError):
        return None


def _native_lifecycle_summary(
    config: Mapping[str, object],
    processes: Sequence[Mapping[str, object]],
    health: Mapping[str, object],
) -> dict[str, object]:
    summary: dict[str, object] = {
        "ap_client_present": False,
        "ap_client_process": False,
        "ap_client_pid": None,
        "native_health_age_ms": (
            max(0, int(time.time() * 1000) - int(health["timestamp_ms"]))
            if health.get("timestamp_ms") is not None
            else "unavailable"
        ),
        "doom_process_detected": False,
        "doom_pid": None,
        "xinput_disk_status": "unavailable",
        "xinput_runtime_module_status": "unavailable",
        "rpc_client_created": False,
        "rpc_server_status": "unavailable",
        "rpc_wait_elapsed": "unavailable",
        "last_rpc_result": health.get("result", "unavailable"),
        "memory_probe": "unavailable",
        "gameplay_loaded": "unavailable",
        "native_safe": "unavailable",
    }
    client_dir = config.get("client_dir")
    if client_dir:
        summary["ap_client_path"] = str(Path(str(client_dir)).expanduser() / "ap_client.exe")
        summary["ap_client_present"] = Path(str(summary["ap_client_path"])).is_file()
    else:
        summary["ap_client_path"] = "unavailable"
    for process in processes:
        name = str(process.get("name", "")).casefold()
        if name in {"ap_client", "ap_client.exe"}:
            summary["ap_client_process"] = True
            summary["ap_client_pid"] = process.get("pid")
        if name in {"doometernalx64vk", "doometernalx64vk.exe"}:
            summary["doom_process_detected"] = True
            summary["doom_pid"] = process.get("pid")

    log_text = None
    for candidate in _native_log_candidates(config)[::2]:
        log_text = _read_log_tail(candidate)
        if log_text:
            break
    if not log_text:
        return summary

    def fields(line: str) -> dict[str, str]:
        return dict(re.findall(r"([a-z_]+)=([^\s]+)", line))

    for line in log_text.splitlines():
        if "XINPUT_DISK_PREFLIGHT " in line:
            summary["xinput_disk_status"] = fields(line).get("status", "unavailable")
        elif "XINPUT_RUNTIME_MODULE " in line:
            values = fields(line)
            summary["xinput_runtime_module_status"] = values.get("status", "unavailable")
            summary["doom_pid"] = values.get("doom_pid", summary["doom_pid"])
        elif "RPC_CLIENT_CREATED " in line:
            summary["rpc_client_created"] = True
            summary["rpc_server_status"] = "waiting"
        elif "RPC_SERVER_WAIT " in line:
            values = fields(line)
            summary["rpc_server_status"] = "waiting"
            summary["rpc_wait_elapsed"] = values.get("elapsed_ms", "unavailable")
            summary["last_rpc_result"] = values.get("last_rpc_result", "unavailable")
            summary["doom_process_detected"] = values.get("doom_process_detected") == "yes"
            summary["doom_pid"] = values.get("doom_pid", summary["doom_pid"])
            summary["gameplay_loaded"] = values.get("gameplay_loaded", "unavailable")
            summary["native_safe"] = values.get("native_safe", "unavailable")
        elif "RPC_SERVER_VERIFIED " in line:
            values = fields(line)
            summary["rpc_server_status"] = "verified"
            summary["rpc_wait_elapsed"] = values.get(
                "elapsed_ms", summary["rpc_wait_elapsed"]
            )
            summary["last_rpc_result"] = values.get(
                "transport", summary["last_rpc_result"]
            )
        elif "Game state transition:" in line:
            summary["memory_probe"] = line[-512:]
        elif "GATE_TRANSITION " in line:
            summary["native_safe"] = "yes" if fields(line).get("state") == "open" else "no"
    return summary


def _read_support_log(path: Path) -> str | None:
    """Retain bounded startup, transition/error events and current tail."""
    try:
        with path.open("rb") as source:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            if size <= 0:
                return None
            if size <= SUPPORT_LOG_MAX_BYTES:
                source.seek(0)
                payload = source.read(SUPPORT_LOG_MAX_BYTES)
            else:
                head_size = SUPPORT_LOG_TAIL_BYTES // 2
                source.seek(0)
                head = source.read(head_size)
                source.seek(max(0, size - head_size))
                tail = source.read(head_size)
                source.seek(0)
                events = deque()
                retained = 0
                limit = SUPPORT_LOG_MAX_BYTES - SUPPORT_LOG_TAIL_BYTES - 256
                death_events = deque()
                death_retained = 0
                death_limit = min(128 * 1024, limit // 4)
                limit -= death_limit
                while source.tell() < size:
                    line = source.readline(32768)
                    if not line:
                        break
                    if b"[DeathLink]" in line or b"deathlink-kill" in line:
                        death_events.append(line)
                        death_retained += len(line)
                        while death_retained > death_limit:
                            death_retained -= len(death_events.popleft())
                        continue
                    structured = b'DELIVERY_EVENT ' in line and b'PROBE_RESPONSE' not in line
                    if structured or any(marker in line for marker in (
                        b" ERROR ", b" WARNING ", b"QUEUE_PUBLICATION_", b"GATE_TRANSITION ",
                        b"MISSION_SNAPSHOT ", b"Game state transition:", b"AP_STAGE_COMPLETE_",
                    )):
                        events.append(line)
                        retained += len(line)
                        while retained > limit:
                            retained -= len(events.popleft())
                payload = (head + b"\n\n[... RETAINED TRANSITION/ERROR EVENTS ...]\n\n"
                           + b"".join(events) + b"\n\n[... DEATHLINK EVENTS ...]\n\n"
                           + b"".join(death_events) + b"\n\n[... CURRENT TAIL ...]\n\n" + tail)
        text = _sanitize_support_text(payload.decode("utf-8", errors="replace"))
        return text[: SUPPORT_LOG_MAX_BYTES + 128]
    except (OSError, UnicodeError):
        return None


def _bound_support_text(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= SUPPORT_LOG_MAX_BYTES:
        return text
    half = SUPPORT_LOG_TAIL_BYTES // 2
    head = encoded[:half].decode("utf-8", errors="replace")
    tail = encoded[-half:].decode("utf-8", errors="replace")
    return head + '' + '\n' + '' + '\n' + player_text('launcher.doctor', 'head_tail_boundary') + '\n' + '' + '\n' + '' + tail


def _encounter_console(path: Path) -> tuple[str, dict[str, object]]:
    limit = SUPPORT_LOG_MAX_BYTES
    separator = b"\n[... ENCOUNTER WINDOW / CONSOLE GAP ...]\n"
    with path.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        stream.seek(0)
        if size <= limit:
            payload = stream.read(limit)
            retained = len(payload)
            truncation = "none"
        else:
            last_event = None
            marker = re.compile(rb"encounter|challenge|exit.?trigger|AP_ENEMY_WAIT|AP_CHECKPOINT", re.I)
            while stream.tell() < size:
                offset = stream.tell()
                if marker.search(stream.readline(32768)):
                    last_event = offset
            edge = limit // 8
            window = limit - edge * 2 - len(separator) * 2
            center = last_event if last_event is not None else size
            start = max(0, min(size - window, center - window * 3 // 4))
            spans = [(0, edge), (start, start + window), (size - edge, size)]
            merged = []
            for begin, end in sorted(spans):
                if merged and begin <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                else:
                    merged.append((begin, end))
            chunks = []
            for begin, end in merged:
                stream.seek(begin)
                chunk = stream.read(end - begin)
                if begin:
                    chunk = chunk.partition(b"\n")[2]
                if end < size:
                    chunk = chunk.rpartition(b"\n")[0] + b"\n" if b"\n" in chunk else b""
                chunks.append(chunk)
            retained = sum(map(len, chunks))
            payload = separator.join(chunks)
            truncation = "encounter_window" if last_event is not None else "head_tail"
    text = _sanitize_support_text(payload.decode("utf-8", errors="replace"))
    text = text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
    return text, {"source_size": size, "archived_size": len(text.encode("utf-8")),
                  "truncation": truncation, "omitted_bytes": size - retained}


def _windows_crash_evidence() -> dict[str, object]:
    result: dict[str, object] = {"reports": [], "dumps": [], "errors": []}
    if os.name != "nt":
        return {**result, "status": "unavailable", "reason": "windows_only"}
    locations = []
    for variable, suffix, kind in (
        ('PROGRAMDATA', "Microsoft/Windows/WER/ReportArchive", "reports"),
        ('LOCALAPPDATA', "Microsoft/Windows/WER/ReportArchive", "reports"),
        ('LOCALAPPDATA', "CrashDumps", "dumps"),
        ('LOCALAPPDATA', "LocalDumps", "dumps"),
    ):
        root = os.environ.get(variable)
        if not root:
            continue
        locations.append((Path(root) / suffix, kind))
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for app in ("", r"\DOOMEternalx64vk.exe"):
                try:
                    with winreg.OpenKey(hive, player_text('launcher.doctor', 'software_microsoft_windows_windows_error_reporting_localdumps') + app) as key:
                        folder, _ = winreg.QueryValueEx(key, "DumpFolder")
                    if isinstance(folder, str) and folder:
                        locations.append((Path(os.path.expandvars(folder)), "dumps"))
                except OSError:
                    pass
    except ImportError:
        pass
    for directory, kind in dict.fromkeys(locations):
        try:
            candidates = (directory.glob("DOOMEternalx64vk.exe*.dmp") if kind == "dumps" else
                          (path / "Report.wer" for path in directory.iterdir() if "doometernal" in path.name.casefold()))
            recent = sorted(((path.stat().st_mtime, path) for path in candidates), reverse=True)[:8]
            for modified, path in recent:
                entry = {"name": path.parent.name[:192] if kind == "reports" else path.name[:192],
                         "modified_utc": datetime.datetime.fromtimestamp(modified, datetime.timezone.utc).isoformat(timespec="seconds")}
                if kind == "reports":
                    with path.open("rb") as stream:
                        raw = stream.read(32768)
                    text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig", errors="replace")
                    fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
                    if fields.get("AppName", "").casefold() != "doometernalx64vk.exe":
                        continue
                    for key, label in fields.items():
                        if label.casefold() in {'fault module name', 'exception code', 'exception offset'}:
                            entry[label.casefold().replace(" ", "_")] = fields.get(key.removesuffix("Name") + "Value", "unavailable")[:256]
                    entry["text"] = _sanitize_support_text(text)[:16384]
                else:
                    entry["size"] = path.stat().st_size
                result[kind].append(entry)
        except FileNotFoundError:
            continue
        except OSError as error:
            result["errors"].append(f"{directory.name}: {type(error).__name__}")
    result["status"] = "available" if result["reports"] or result["dumps"] else "no_evidence_found"
    return sanitize_support_value(result)


def _most_recent_log(candidates: Sequence[Path]) -> Path | None:
    useful: list[tuple[int, int, Path]] = []
    for index in range(0, len(candidates), 2):
        current = candidates[index]
        previous = candidates[index + 1] if index + 1 < len(candidates) else None
        current_stat = None
        previous_stat = None
        try:
            stat = current.stat()
            if current.is_file() and stat.st_size > 0:
                current_stat = stat
        except OSError:
            pass
        if previous is not None:
            try:
                stat = previous.stat()
                if previous.is_file() and stat.st_size > 0:
                    previous_stat = stat
            except OSError:
                pass
        if current_stat is not None and (
            previous_stat is None or current_stat.st_mtime_ns >= previous_stat.st_mtime_ns
        ):
            useful.append((current_stat.st_mtime_ns, 1, current))
        elif previous_stat is not None and previous is not None:
            useful.append((previous_stat.st_mtime_ns, 0, previous))
    if not useful:
        return None
    useful.sort(reverse=True)
    return useful[0][2]


def _log_freshness(
    stat_result: os.stat_result,
    session_start: float | None = None,
) -> tuple[str, str]:
    now = time.time()
    iso_time = datetime.datetime.fromtimestamp(
        stat_result.st_mtime, tz=datetime.timezone.utc
    ).isoformat()
    mtime = stat_result.st_mtime

    if session_start is not None:
        if mtime >= (session_start - 10.0):
            return "active_session", iso_time
        age_before_session = max(0.0, session_start - mtime)
        if age_before_session < 86400 * 2:
            return "recent_previous", iso_time
        age_days = int(age_before_session // 86400)
        return player_text('launcher.doctor', 'historical_stale_value_0_days_old', value_0=f'{age_days}'), iso_time

    age_seconds = max(0.0, now - mtime)
    if age_seconds < 300:
        return "active_session", iso_time
    if age_seconds < 86400 * 2:
        return "recent_previous", iso_time
    age_days = int(age_seconds // 86400)
    return player_text('launcher.doctor', 'historical_stale_value_0_days_old', value_0=f'{age_days}'), iso_time


def _support_log_tails(
    config: Mapping[str, object] | None,
    paths: object | None,
    application_dir: Path | None = None,
    session_start: float | None = None,
) -> tuple[dict[str, str], dict[str, dict[str, object]]]:
    tails: dict[str, str] = {}
    provenance: dict[str, dict[str, object]] = {}
    for family, candidates in (
        ("launcher", _launcher_log_candidates(config, paths, application_dir)),
        ("bridge", _bridge_log_candidates(config, paths, application_dir)),
        ("native", _native_log_candidates(config)),
    ):
        names = {
            "launcher": ("launcher.log", "launcher.previous.log"),
            "bridge": ("bridge.log", "bridge.previous.log"),
            "native": ("ap_client.log", "ap_client.previous.log"),
        }[family]
        paired = tuple(candidates[index:index + 2] for index in range(0, len(candidates), 2))
        for role, archive_name in enumerate(names):
            selected: Path | None = None
            attempted: list[str] = []
            for pair in paired:
                if role >= len(pair):
                    continue
                candidate = pair[role]
                attempted.append(_safe_path(candidate))
                try:
                    if candidate.is_file() and candidate.stat().st_size > 0:
                        selected = candidate
                        break
                except OSError:
                    continue
            if selected is None:
                provenance[archive_name] = {
                    "status": "unavailable",
                    "reason": "not_found",
                    "candidate_paths": attempted,
                }
                continue
            try:
                stat_res = selected.stat()
                freshness, iso_time = _log_freshness(stat_res, session_start=session_start)
                provenance[archive_name] = {
                    "status": "available",
                    "source_path": _safe_path(selected),
                    "size_bytes": stat_res.st_size,
                    "modified_iso": iso_time,
                    "freshness": freshness,
                }
            except OSError as error:
                provenance[archive_name] = {
                    "status": "unavailable",
                    "reason": f"stat_failed: {type(error).__name__}",
                    "source_path": _safe_path(selected),
                }
                continue
            content = _read_support_log(selected)
            if content is None:
                provenance[archive_name] = {
                    **provenance[archive_name],
                    "status": "unavailable",
                    "reason": "unreadable",
                }
            else:
                tails[archive_name] = content
    return tails, provenance


def _mtime_document(path: Path) -> dict[str, object]:
    result: dict[str, object] = {"path": str(path), "exists": False}
    try:
        stat_result = path.stat()
    except (OSError, ValueError, RuntimeError) as error:
        result["error"] = f"stat failed: {type(error).__name__}: {error}"[:256]
        return result
    try:
        is_file = path.is_file()
    except (OSError, ValueError, RuntimeError) as error:
        result["error"] = f"file check failed: {type(error).__name__}: {error}"[:256]
        return result
    result.update({
        "exists": True,
        "is_file": is_file,
        "size_bytes": stat_result.st_size,
        "mtime_ns": stat_result.st_mtime_ns,
        "modified_iso": datetime.datetime.fromtimestamp(
            stat_result.st_mtime, tz=datetime.timezone.utc
        ).isoformat(),
    })
    if is_file:
        try:
            result["value"] = path.read_text(encoding="utf-8", errors="replace")[:512]
        except (OSError, ValueError, RuntimeError) as error:
            result["value"] = "[unreadable]"
            result["error"] = f"read failed: {type(error).__name__}: {error}"[:256]
    return result


def _marker_files(root: Path, prefix: str) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    try:
        paths = sorted(root.glob(f"{prefix}*.txt"), key=lambda item: item.name)
    except (OSError, ValueError, RuntimeError) as error:
        return [{
            "path": str(root),
            "exists": False,
            "error": f"marker scan failed: {type(error).__name__}: {error}"[:256],
        }]
    for path in paths[:SUPPORT_DIAGNOSTIC_MAX_ITEMS]:
        detail = _mtime_document(path)
        value = str(detail.get("value", ""))
        evidence = []
        for marker in ('AP_ACTIVE_MAP_V1', 'AP_CHECK_EVENT_', 'AP_TELEMETRY'):
            if marker in value:
                evidence.append(marker)
        detail["marker_evidence"] = evidence
        entries.append(detail)
    return entries


def _recent_relevant_files(root: Path) -> list[dict[str, object]]:
    cutoff = time.time() - 300.0
    entries: list[dict[str, object]] = []
    try:
        paths = sorted(root.iterdir(), key=lambda item: item.name)
    except (OSError, ValueError, RuntimeError):
        return entries
    for path in paths:
        if not path.is_file() or not (
            path.name.startswith(("ap_", 'GAME-AUTOSAVE'))
            or path.name in {"game.details", "game_duration.dat"}
        ):
            continue
        try:
            stat_result = path.stat()
        except OSError:
            continue
        if stat_result.st_mtime >= cutoff:
            entries.append({"name": path.name, "mtime_ns": stat_result.st_mtime_ns})
        if len(entries) >= SUPPORT_DIAGNOSTIC_MAX_ITEMS:
            break
    return entries


def _saved_games_candidates(
    config: Mapping[str, object],
    root: Path | None,
    *,
    known: Path | None = None,
) -> tuple[Path, ...]:
    candidates: list[Path] = []
    configured = config.get("save_games_dir")
    if configured:
        try:
            selected = Path(str(configured)).expanduser()
            candidates.extend((selected, selected.parent, selected.parent.parent))
        except (OSError, TypeError, ValueError):
            pass
    if known is not None:
        candidates.append(known)
    candidates.append(Path.home() / 'Saved Games' / 'id Software' / "DOOMEternal" / "base")
    if root is not None:
        for parent in (root, *root.parents):
            if parent.name.casefold() == "steamapps":
                candidates.append(
                    parent.parent / player_text('launcher.doctor', 'steamapps_compatdata_782330_pfx_drive_c_users_steamuser_saved_ga')
                )
                break
    return tuple(dict.fromkeys(path.expanduser() for path in candidates))


def _saved_games_diagnostics(
    config: Mapping[str, object],
    root: Path | None,
    processes: Sequence[Mapping[str, object]],
    *,
    app_dir: Path | str | None = None,
    client_dir: Path | str | None = None,
) -> dict[str, object]:
    configured_value = config.get("save_games_dir")
    game_root = root.parent if root is not None and root.name.casefold() == "base" else root
    known: Path | None = None
    try:
        known = doom_saved_games_base(game_root=game_root)
    except (OSError, TypeError, ValueError, RuntimeError):
        known = None
    game_detected = any(
        str(item.get("name", "")).casefold() in {"doometernalx64vk", "doometernalx64vk.exe"}
        for item in processes
    )
    selection: SavedGamesSelection | None = None
    try:
        selection = select_saved_games_dir(
            str(configured_value) if configured_value else None,
            known_base=known,
            app_dir=app_dir,
            client_dir=client_dir,
            game_root=game_root,
            game_running=game_detected,
        )
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        selection = None
        selection_error = f"Saved Games selection unavailable: {type(error).__name__}: {error}"[:256]
    else:
        selection_error = None
    selected = Path(str(selection.path)) if selection is not None and selection.path else None
    candidates = []
    for path in _saved_games_candidates(config, root, known=known)[:SUPPORT_DIAGNOSTIC_MAX_ITEMS]:
        try:
            is_dir = path.is_dir()
        except (OSError, ValueError, RuntimeError):
            is_dir = False
        recent = _recent_relevant_files(path) if is_dir else []
        game_detected = any(
            str(item.get("name", "")).casefold() in {"doometernalx64vk", "doometernalx64vk.exe"}
            for item in processes
        )
        candidates.append({
            "path": str(path),
            "exists": is_dir,
            "selected": selected is not None and path == selected,
            "relationship": (
                "configured"
                if selection is not None
                and selection.source == "configured_saved_games_dir"
                and selected is not None
                and path == selected
                else "canonical_known_folder"
                if selection is not None
                and selection.source in {"canonical_saved_games_base", "live_writer_evidence", "single_viable_candidate"}
                and selected is not None
                and path == selected
                else "rejected_stale_config"
                if selection is not None
                and selection.previous_path is not None
                and str(path) == selection.previous_path
                else "discovered_candidate"
            ),
            "shape_verdict": saved_games_shape_verdict(path),
            "markers": {
                "ap_active_map": _marker_files(path, "ap_active_map") if is_dir else [{"path": str(path), "exists": False, "error": player_text('launcher.doctor', 'candidate_is_not_an_accessible_directory')}],
                "ap_telemetry": _marker_files(path, "ap_telemetry") if is_dir else [{"path": str(path), "exists": False, "error": player_text('launcher.doctor', 'candidate_is_not_an_accessible_directory')}],
                "ap_event": _marker_files(path, "ap_event") if is_dir else [{"path": str(path), "exists": False, "error": player_text('launcher.doctor', 'candidate_is_not_an_accessible_directory')}],
            },
            "recent_writes": recent,
            "live_game_detected": game_detected,
            "live_game_writing": bool(recent) and game_detected,
            "live_game_writing_reason": (
                player_text('launcher.doctor', 'recent_ap_save_write_while_doom_process_detected')
                if recent and game_detected else
                player_text('launcher.doctor', 'no_recent_relevant_write_observed') if game_detected else
                player_text('launcher.doctor', 'doom_process_not_detected')
            ),
        })
    result: dict[str, object] = {
        "effective_path": str(selected) if selected else None,
        "configured_path": str(configured_value) if configured_value else None,
        "source": selection.source if selection is not None else "unresolved",
        "reason": selection.reason if selection is not None else player_text('launcher.doctor', 'no_effective_saved_games_path'),
        "repaired": bool(selection is not None and selection.repaired),
        "previous_path": selection.previous_path if selection is not None else None,
        "live_evidence": list(selection.live_evidence) if selection is not None else [],
        "rejected": selection.rejected if selection is not None else [],
        "candidates": candidates,
    }
    if selection_error:
        result["selection_error"] = selection_error
    return result


def _delivery_diagnostics(config, config_path=None, *, live=None):
    from doom_eap.runtime.ledger_records import ItemStatus, RoomKey

    from .items import ItemsQuery
    from .items_reader import LedgerReadAdapter

    metadata = live.get("delivery_ledger", {}) if live else {}
    path = Path(metadata.get("path") or config.get("delivery_ledger_file") or
                _client_state_path(config, config_path).with_name("delivery.sqlite3"))
    result = {"source": "ledger", "path": str(path), "status": "inactive"}
    if not metadata.get("room"):
        return result
    try:
        query = ItemsQuery(RoomKey(**metadata["room"]), "support-report", limit=SUPPORT_DIAGNOSTIC_MAX_ITEMS)
        reader = LedgerReadAdapter(path, expected=bool(config.get("delivery_ledger_expected")))
        snapshot = asyncio.run(reader.read(query))
    except (TypeError, ValueError, OSError) as error:
        return {**result, "status": "unavailable", "reason": type(error).__name__}
    result.update(status=snapshot.availability.value, authority=snapshot.authority.value if snapshot.authority else None)
    if snapshot.page is not None:
        result.update(watermark=snapshot.page.watermark, sampled=len(snapshot.page),
                      truncated=len(snapshot.page) == query.limit,
                      counts={state.value: sum(row.status is state for row in snapshot.page) for state in ItemStatus})
    return result


def _delivery_verdict(delivery):
    if delivery.get("status") != "available" or delivery.get("authority") != "ledger":
        return None
    counts = delivery.get("counts", {})
    if counts.get("blocked", 0):
        return False
    if delivery.get("truncated") or counts.get("received", 0) or counts.get("waiting", 0):
        return None
    return True


def _runtime_diagnostics(config: Mapping[str, object], config_path: Path | None, paths: object | None, processes: Sequence[Mapping[str, object]], *, app_dir: Path | str | None = None) -> dict[str, object]:
    base_error: str | None = None
    try:
        base = doom_base_dir_from_config(config)
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        base = None
        base_error = f"base path unavailable: {type(error).__name__}: {error}"[:256]
    save = config.get("save_games_dir")
    client_dir: Path | str | None = None
    if app_dir is not None:
        try:
            packaged_client = Path(str(app_dir)).expanduser() / "client"
            if packaged_client.is_dir():
                client_dir = packaged_client
        except (OSError, TypeError, ValueError, RuntimeError):
            client_dir = None
    details = {
        "config_file": str(config_path) if config_path else None,
        "doom_base_dir": str(base) if base else None,
        "save_games_dir": str(save) if save else None,
        "INV_DUMP_DIR": str(save) if save else None,
        "STEAM_REMOTE_DIR": str(config.get("steam_remote_dir")) if config.get("steam_remote_dir") else None,
        "STEAM_ID3": config.get("steam_id3"),
        "path_selection": _saved_games_diagnostics(config, base, processes, app_dir=app_dir, client_dir=client_dir),
        "markers": {},
        "materialization": {},
    }
    if base_error:
        details["doom_base_dir_error"] = base_error
    if save:
        try:
            marker_root = Path(str(save)).expanduser()
            details["markers"] = {
                "ap_active_map": _marker_files(marker_root, "ap_active_map"),
                "ap_telemetry": _marker_files(marker_root, "ap_telemetry"),
                "ap_event": _marker_files(marker_root, "ap_event"),
            }
        except (OSError, TypeError, ValueError, RuntimeError) as error:
            details["markers"] = {
                "error": f"marker path unavailable: {type(error).__name__}: {error}"[:256]
            }
    if base is not None:
        materialization = {
            "ap_gameplay_save.state": base / "ap_gameplay_save.state",
        }
        details["materialization"] = {name: _mtime_document(path) for name, path in materialization.items()}
    try:
        health_path = base / "ap_rpc_health.state" if base else Path()
        details["native"] = NativeHealthReader(health_path).read(force=True).document() if base else {"state": "not_ready", "reason": "base_directory_unconfigured"}
    except Exception as error:
        details["native"] = {"state": "unavailable", "reason": str(error)}
    details["native_lifecycle"] = _native_lifecycle_summary(
        config,
        processes,
        details["native"] if isinstance(details["native"], Mapping) else {},
    )
    if paths is not None:
        details["launcher_paths"] = {name: str(getattr(paths, name)) for name in ("config_dir", "state_dir", "data_dir") if getattr(paths, name, None)}
    return details


def _client_state_path(
    config: Mapping[str, object], config_path: Path | None = None
) -> Path:
    configured = config.get("client_state_file")
    if configured:
        path = Path(str(configured)).expanduser()
        if not path.is_absolute() and config_path is not None:
            path = config_path.expanduser().resolve().parent / path
        return path.resolve()
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "doom-eternal-ap" / "client_state.json"


def _expected_room_compiler_digest(application_dir: Path | None = None) -> str | None:
    try:
        from .launcher_core import ROOT, RoomCompiler
        candidates = []
        if application_dir:
            candidates.append(Path(application_dir) / "resources")
        candidates.append(ROOT / "build" / "release" / "client" / "resources")
        candidates.append(ROOT / "resources")
        for res_dir in candidates:
            base = res_dir / "base_mod.zip"
            payload = res_dir / "room_payloads.zip"
            manifest = res_dir / "room_payload_manifest.json"
            if base.is_file() and payload.is_file() and manifest.is_file():
                compiler = RoomCompiler(base, payload, manifest)
                return compiler.static_content_digest
    except Exception:
        pass
    return None


def _preparation_failure_summary(state_dir, *, filename="session_prepare_failure.json", source="last_preparation_failure"):
    if not state_dir:
        return {"status": "unavailable"}
    try:
        path = state_dir / filename
        with path.open("rb") as stream:
            raw = stream.read(SUPPORT_DIAGNOSTIC_MAX_BYTES + 1)
        if len(raw) > SUPPORT_DIAGNOSTIC_MAX_BYTES:
            return {"status": "unavailable", "reason": "diagnostic_exceeds_support_limit"}
        document = json.loads(raw)
        modified = datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc).isoformat(timespec="seconds")
    except (OSError, UnicodeError, ValueError):
        return {"status": "unavailable"}
    return {"status": "retained", "source": source, "modified_utc": modified, "evidence": sanitize_support_value(document)}


def _room_install_receipt_summary(
    state_dir: Path | None,
    application_dir: Path | None = None,
) -> dict[str, object]:
    path = state_dir / "launcher_setup.json" if state_dir else None
    result: dict[str, object] = {
        "status": "unavailable",
        "source": "launcher_install_receipt",
        "path": str(path) if path else None,
    }
    if path is None:
        result["reason"] = "state_directory_unconfigured"
        return result
    try:
        if not path.is_file():
            result["reason"] = "not_found"
            return result
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        result["reason"] = f"unreadable: {type(error).__name__}"
        return result
    if not isinstance(document, Mapping):
        result["reason"] = "malformed"
        return result
    for key in (
        "schema", "manifest_hash", "static_content_digest", "staged_mod", "staged_sha256",
        "room_zip_sha256", "generated_mod", "generated_sha256", "mods_path",
        "injector_version", "injector_command", "adapter_command", "injector_exit_code",
        "required_resources", "post_install_verification", "adapter_details", "adapter_message",
        "adapter_state", "installation_mode", "steam_launch_option",
    ):
        if key in document:
            result[key] = document[key]
    expected_digest = _expected_room_compiler_digest(application_dir)
    recorded_digest = str(document.get("static_content_digest", "")) if document.get("static_content_digest") else ""
    if expected_digest:
        result["expected_static_content_digest"] = expected_digest
        result["receipt_static_content_digest"] = recorded_digest
        result["content_identity_match"] = bool(recorded_digest and recorded_digest == expected_digest)
        if not recorded_digest or recorded_digest != expected_digest:
            result["content_status"] = "stale_content_identity"
    else:
        result["content_identity_match"] = True
    staged = document.get("staged_mod")
    expected = document.get("staged_sha256")
    if isinstance(staged, str) and staged and isinstance(expected, str) and expected:
        staged_path = Path(staged)
        result["staged_exists"] = staged_path.is_file()
        if staged_path.is_file():
            try:
                actual = hashlib.sha256(staged_path.read_bytes()).hexdigest()
                result["staged_sha256_actual"] = actual
                content_fresh = not expected_digest or recorded_digest == expected_digest
                result["hash_match"] = (actual == expected) and content_fresh
            except OSError:
                result["hash_match"] = False
    result["support_evidence"] = {
        key: result[key]
        for key in (
            "room_zip_sha256", "generated_sha256", "mods_path", "injector_version",
            "injector_command", "adapter_command", "injector_exit_code",
            "required_resources", "post_install_verification", "adapter_details", "adapter_message",
        )
        if key in result
    }
    generated = document.get("generated_mod")
    generated_expected = document.get("generated_sha256")
    if isinstance(generated, str) and generated and isinstance(generated_expected, str) and generated_expected:
        generated_path = Path(generated)
        result["generated_exists"] = generated_path.is_file()
        if generated_path.is_file():
            try:
                generated_actual = hashlib.sha256(generated_path.read_bytes()).hexdigest()
                result["generated_sha256_actual"] = generated_actual
                content_fresh = not expected_digest or recorded_digest == expected_digest
                result["generated_hash_match"] = (generated_actual == generated_expected) and content_fresh
                if isinstance(expected, str) and expected and "staged_sha256_actual" in result:
                    result["generated_stage_match"] = (generated_actual == result["staged_sha256_actual"]) and content_fresh
            except OSError:
                result["generated_hash_match"] = False
    result["status"] = "available"
    return result


def _verify_linux_receipt(
    receipt: Mapping[str, object],
    config: Mapping[str, object],
    *,
    application_dir: Path | None = None,
) -> dict[str, object]:
    if os.name == "nt":
        return {"state": "not_applicable"}
    base = Path(str(config.get("doom_base_dir", ""))).expanduser().resolve()
    root = base.parent if base.name.casefold() == "base" else base
    staged = Path(str(receipt.get("staged_mod", ""))).expanduser()
    staged_expected = str(receipt.get("staged_sha256", ""))
    room_zip_expected = str(receipt.get("room_zip_sha256", staged_expected))
    generated_path = Path(str(receipt.get("generated_mod", ""))).expanduser()
    generated_expected = str(receipt.get("generated_sha256", ""))
    if not staged_expected or not room_zip_expected or not generated_expected or not generated_path.is_file():
        raise ValueError(player_text('launcher.doctor', 'linux_install_record_lacks_complete_room_zip_hash_evidence'))
    staged_actual = hashlib.sha256(staged.read_bytes()).hexdigest()
    generated_actual = hashlib.sha256(generated_path.read_bytes()).hexdigest()
    if not (staged_actual == staged_expected == room_zip_expected == generated_expected == generated_actual):
        raise ValueError(player_text('launcher.doctor', 'generated_and_staged_room_zip_hashes_differ'))
    expected_digest = _expected_room_compiler_digest(application_dir)
    if expected_digest:
        recorded_digest = str(receipt.get("static_content_digest", ""))
        if not recorded_digest or recorded_digest != expected_digest:
            raise ValueError(player_text('launcher.doctor', 'installed_package_static_content_is_stale_value_0_value_1', value_0=f"{recorded_digest or 'missing'}", value_1=f'{expected_digest}'))
    raw_command = receipt.get("injector_command", receipt.get("adapter_command", ()))
    raw_resources = receipt.get("required_resources", ())
    expected_mods = str((root / "Mods").resolve())
    if (
        not str(receipt.get("injector_version", ""))
        or not isinstance(raw_command, list | tuple) or not raw_command
        or receipt.get("injector_exit_code") != 0
        or not isinstance(raw_resources, list | tuple) or len(raw_resources) not in (3, 4)
        or any(not isinstance(item, Mapping) or item.get("status") != "verified" for item in raw_resources)
        or str(receipt.get("mods_path", "")) != expected_mods
    ):
        raise ValueError(player_text('launcher.doctor', 'linux_install_record_lacks_complete_injector_or_resource_evidenc'))
    verification = verify_linux_mod_installation(root, staged)
    if str(receipt.get("post_install_verification", "")) != "verified":
        raise ValueError(player_text('launcher.doctor', 'linux_post_install_verification_is_not_recorded'))
    return verification


def _support_game_link_diagnostics(
    config: Mapping[str, object],
    config_path: Path | None,
    paths: object | None,
    processes: Sequence[Mapping[str, object]],
    runtime: Mapping[str, object],
    meathook: Mapping[str, object],
    *,
    live: Mapping[str, object] | None = None,
) -> dict[str, object]:
    game_running = any(
        str(item.get("name", "")).casefold()
        in {"doometernalx64vk", "doometernalx64vk.exe"}
        for item in processes
    )
    process = {
        "status": "running" if game_running else "not_running",
        "game_detected": game_running,
        "items": [dict(item) for item in processes[:SUPPORT_DIAGNOSTIC_MAX_ITEMS]],
    }
    config_paths: dict[str, object] = {
        "config_file": str(config_path) if config_path else None,
        "application_dir": str(getattr(paths, "application_dir", "")) if paths else None,
        "client_state_file": str(_client_state_path(config, config_path)),
        "doom_base_dir": runtime.get("doom_base_dir"),
        "save_games_dir": runtime.get("save_games_dir"),
        "delivery_ledger_file": config.get("delivery_ledger_file"),
    }
    live_paths = live.get("config_paths") if live else None
    if isinstance(live_paths, Mapping):
        config_paths.update(dict(live_paths))
    configured_supervisor = live.get("supervisor") if live else None
    if isinstance(configured_supervisor, Mapping):
        supervisor = dict(configured_supervisor)
        bridge_status = str(supervisor.get("status", "unknown"))
    else:
        supervisor = {"status": "unknown", "reason": "live_supervisor_not_provided"}
        bridge_status = "unknown"
    bridge = {"status": bridge_status, "supervisor": supervisor}

    direct_native = runtime.get("native")
    direct_native = dict(direct_native) if isinstance(direct_native, Mapping) else {}
    native = live.get("native_rpc") if live else None
    native_rpc = dict(native) if isinstance(native, Mapping) else {
        "source": "direct" if direct_native.get("native_state") else "unknown",
        "evidence": "direct" if direct_native.get("native_state") else "unavailable",
        "health": direct_native,
    }
    native_rpc["lifecycle"] = dict(runtime.get("native_lifecycle", {}))
    if isinstance(native_rpc, Mapping):
        native_rpc.setdefault(
            "evidence",
            "direct" if native_rpc.get("source") == "direct" else (
                "last_known" if native_rpc.get("source") == "last_known" else "unavailable"
            ),
        )
    native_status = str(native_rpc.get("health", {}).get("state", "unknown")) if isinstance(native_rpc.get("health"), Mapping) else "unknown"

    markers = runtime.get("markers")
    markers = markers if isinstance(markers, Mapping) else {}
    telemetry_files = sum(
        1
        for value in markers.values()
        if isinstance(value, list)
        for entry in value
        if isinstance(entry, Mapping) and entry.get("exists") is True
    )
    telemetry = {
        "status": "available" if telemetry_files else "unavailable",
        "marker_files": telemetry_files,
        "reason": "marker_evidence_observed" if telemetry_files else "no_marker_evidence",
    }
    native_safe = native_rpc["lifecycle"].get("native_safe")
    current_health = native_rpc.get("health", {})
    safety = {
        "status": "enabled" if native_safe in (True, "yes", "1") and current_health.get("ready") is True else "unknown",
        "reason": "native_safety_observation",
    }
    telemetry_safety = {
        "status": "ready" if telemetry["status"] == "available" and safety["status"] == "enabled" else "degraded",
        "telemetry": telemetry,
        "safety": safety,
    }
    delivery = _delivery_diagnostics(config, config_path, live=live)

    meathook_status = str(meathook.get("status", "unknown"))
    issues: list[str] = []
    if meathook_status not in {"ok", "verified"}:
        issues.append("game_link_runtime_not_verified")
    if game_running and bridge_status in {"unavailable", "stopped", "failed", "unknown"}:
        issues.append("bridge_not_available_for_running_game")
    if game_running and native_status in {"degraded", "not_ready", "unknown"}:
        issues.append("native_rpc_not_ready")
    if telemetry_safety["status"] != "ready" and game_running:
        issues.append("telemetry_or_safety_evidence_not_ready")
    if delivery["status"] in {"missing", "unavailable", "incompatible"}:
        issues.append("delivery_ledger_unavailable")
    if delivery.get("counts", {}).get("blocked", 0):
        issues.append("delivery_has_blocked_items")
    if issues:
        overall_status = "attention" if "game_link_runtime_not_verified" in issues else "degraded"
        overall_reason = issues[0]
    else:
        overall_status = "ready"
        overall_reason = "game_link_evidence_consistent"

    base_dir = doom_base_dir_from_config(config)
    hotkey_file = (base_dir / "ap_queue" / AMMO_HOTKEY_STATE_FILENAME) if base_dir else None
    hotkey_exists = hotkey_file is not None and hotkey_file.is_file()
    configured_ammo_key = str(config.get("ammo_refill_keybind", "F9"))
    parsed_key = 'UNBOUND'
    if hotkey_exists and hotkey_file is not None:
        try:
            first_line = hotkey_file.read_text(encoding="utf-8").splitlines()[0].strip()
            parts = first_line.split()
            if len(parts) >= 2 and parts[0] == "AP_AMMO_REFILL_HOTKEY_V1":
                parsed_key = parts[1]
            elif len(parts) == 1:
                parsed_key = parts[0]
        except Exception:
            pass

    stale_config_bind_present = False
    cfg_path = resolve_doom_config_path(config)
    if cfg_path is not None and cfg_path.is_file():
        try:
            cfg_text = cfg_path.read_text(encoding="utf-8", errors="replace")
            stale_config_bind_present = AMMO_REFILL_BIND_COMMAND in cfg_text
        except Exception:
            pass

    ammo_hotkey = {
        "configured_key": configured_ammo_key,
        "state_file_path": str(hotkey_file) if hotkey_file else None,
        "state_file_exists": hotkey_exists,
        "parsed_key": parsed_key,
        "stale_config_bind_present": stale_config_bind_present,
    }

    return {
        "runtime": dict(meathook),
        "process": process,
        "bridge": bridge,
        "supervisor": supervisor,
        "config_paths": config_paths,
        "native_rpc": native_rpc,
        "telemetry_safety": telemetry_safety,
        "delivery": delivery,
        "ammo_hotkey": ammo_hotkey,
        "overall": {
            "status": overall_status,
            "reason": overall_reason,
            "issues": issues,
            "derived_from": [
                "process", "bridge", "native_rpc", "telemetry_safety", "delivery", "ammo_hotkey",
            ],
        },
    }


def installation_diagnostics(config):
    from .install_journal import InstallJournal
    base = config.get("doom_base_dir") or config.get("game_root")
    if not base:
        return {"status": "unavailable"}
    path = Path(str(base)).expanduser()
    root = path.parent if path.name.casefold() == "base" else path
    try:
        return InstallJournal(root).summary()
    except (OSError, RuntimeError) as error:
        return {"status": "unreadable", "error": str(error)}


def _idfile_diagnostics(data_dir):
    selected_spec = idfile_decompressor_spec()
    idfile_diagnostics: dict[str, object] = {"status": "unsupported" if selected_spec is None else "unknown"}
    if selected_spec is not None:
        idfile_diagnostics.update({
            "expected_version": selected_spec.version,
            "expected_commit": selected_spec.commit,
            "source_url": selected_spec.url,
            "expected_size": selected_spec.expected_size,
            "expected_sha256": selected_spec.sha256,
        })
        idfile_diagnostics["selected_spec"] = {
            "name": selected_spec.name,
            "version": selected_spec.version,
            "commit": selected_spec.commit,
            "url": selected_spec.url,
            "sha256": selected_spec.sha256,
            "executable": selected_spec.executable_glob,
            "platforms": sorted(selected_spec.platforms),
        }
    if data_dir is not None:
        cache_dir = data_dir / "dependencies"
        idfile_diagnostics["cache_dir"] = cache_dir
        idfile_diagnostics["cache_dir_display"] = str(cache_dir)
        idfile_diagnostics["cache_dir_canonical"] = str(cache_dir.resolve())
        manager = DependencyManager(cache_dir)
        if selected_spec is None or not manager.platform_supported(selected_spec):
            idfile_diagnostics.update({
                "status": "unsupported",
                "platform": manager.current_platform_name(),
                "supported_platforms": ["linux", "windows"],
            })
        else:
            try:
                cache_details = manager.cache_diagnostics(selected_spec)
            except OSError as error:
                idfile_diagnostics.update({"status": "unreadable", "error": str(error)})
            else:
                idfile_diagnostics.update(cache_details)
    return idfile_diagnostics


def build_support_diagnostics(
    config: Mapping[str, object],
    config_path: Path | None,
    paths: object | None,
    processes: Sequence[Mapping[str, object]],
    runtime: Mapping[str, object],
    meathook: Mapping[str, object],
    *,
    live: Mapping[str, object] | None = None,
) -> dict[str, object]:
    state_value = getattr(paths, "state_dir", None) if paths else None
    try:
        state_dir = Path(state_value).expanduser() if state_value else None
    except (OSError, TypeError, ValueError, RuntimeError):
        state_dir = None
    data_value = getattr(paths, "data_dir", None) if paths else None
    try:
        data_dir = Path(data_value).expanduser() if data_value else None
    except (OSError, TypeError, ValueError, RuntimeError):
        data_dir = None
    idfile_diagnostics = _idfile_diagnostics(data_dir)
    app_val = config.get("application_dir") or getattr(paths, "application_dir", None)
    app_dir = Path(app_val).expanduser() if app_val else None
    game_link = _support_game_link_diagnostics(config, config_path, paths, processes, runtime, meathook, live=live)
    from .support_evidence import ledger_timeline

    return {
        "ledger_timeline": ledger_timeline(live.get("delivery_ledger", {}) if live else {}),
        **{key: live[key] for key in ("ap_session", "native_diagnostics", "native_helper_startup")
           if live and key in live},
        "item_state": game_link["delivery"],
        "room_install_receipt": _room_install_receipt_summary(state_dir, app_dir),
        "installation": installation_diagnostics(config),
        "session_preparation_failure": _preparation_failure_summary(state_dir),
        "session_admission_failure": _preparation_failure_summary(
            state_dir, filename="session_admission_failure.json", source="last_admission_failure"),
        "game_link": game_link,
        "idfile_decompressor": idfile_diagnostics,
        "mod_injector": {
            "name": (WINDOWS_MOD_INJECTOR if os.name == "nt" else LINUX_MOD_INJECTOR).name,
            "version": (WINDOWS_MOD_INJECTOR if os.name == "nt" else LINUX_MOD_INJECTOR).version,
            "url": (WINDOWS_MOD_INJECTOR if os.name == "nt" else LINUX_MOD_INJECTOR).url,
            "expected_size": (WINDOWS_MOD_INJECTOR if os.name == "nt" else LINUX_MOD_INJECTOR).expected_size,
            "expected_sha256": (WINDOWS_MOD_INJECTOR if os.name == "nt" else LINUX_MOD_INJECTOR).sha256,
            "required_members": (
                list(WINDOWS_INJECTOR_REQUIRED_MEMBERS)
                if os.name == "nt" else [LINUX_MOD_INJECTOR.executable_glob]
            ),
        },
    }


def _native_startup_diagnostics(config, session, install):
    directory = diagnostics_directory(config or {})
    if directory is None:
        return {"status": "unavailable", "reason": "native_log_location_unavailable"}
    manifest = install["manifest"].get("document")
    build = manifest.get("build_id") if isinstance(manifest, dict) else None
    if not build:
        return {"status": "unavailable", "reason": "installed_build_unknown"}
    try:
        candidates = sorted(directory.glob("*.latest.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {"status": "unavailable", "reason": f"native_log_inspection_failed: {type(error).__name__}"}

    records = []
    errors = []
    for path in candidates[:SUPPORT_DIAGNOSTIC_MAX_ITEMS]:
        identity = path.name.removesuffix(".latest.json").split("-")
        if len(identity) != 2 or not all(part.isdecimal() for part in identity):
            continue
        try:
            with path.open("rb") as stream:
                raw = stream.read(SUPPORT_DIAGNOSTIC_MAX_BYTES + 1)
            if len(raw) > SUPPORT_DIAGNOSTIC_MAX_BYTES:
                raise ValueError(player_text('launcher.doctor', 'native_diagnostic_exceeds_support_limit'))
            data = json.loads(raw)
            if not isinstance(data, dict) or [str(data.get("pid")), str(data.get("process_created"))] != identity:
                raise ValueError(player_text('launcher.doctor', 'native_diagnostic_process_identity_mismatch'))
            if data.get("build_id") != build:
                continue
            namespace = session.get("namespace_id")
            if namespace and data.get("admission", {}).get("namespace_id") != namespace:
                continue
            current = [str(session.get("pid")), str(session.get("process_created"))] == identity
            records.append({"source_filename": path.name, "evidence": "current_process" if current else "last_known",
                            "diagnostic": sanitize_support_value(data)})
            if len(records) == 2:
                break
        except (OSError, ValueError, TypeError, AttributeError) as error:
            errors.append({"source_filename": path.name, "reason": type(error).__name__})
    return {"status": "available" if records else "unavailable", "records": records,
            "errors": errors, "reason": None if records else "no_matching_native_diagnostic"}


def _darklord_diagnostics(config, session):
    directory = diagnostics_directory(config or {})
    if directory is None:
        return {}, {"status": "unavailable", "reason": "native_log_location_unavailable"}
    records, errors, logs = [], [], {}
    limit = SUPPORT_LOG_MAX_BYTES
    try:
        candidates = sorted(
            (path for path in directory.glob("*.darklord.log")
             if re.fullmatch(r"\d+-\d+\.darklord\.log", path.name) and not path.is_symlink()),
            key=lambda path: path.stat().st_mtime, reverse=True,
        )
        for path in candidates[:4]:
            size = path.stat().st_size
            with path.open("rb") as stream:
                if size > limit:
                    stream.seek(size - limit)
                    stream.readline()
                raw = stream.read(limit)
            lines = [line for line in raw.decode("utf-8", errors="replace").splitlines()
                     if line.startswith("AP_DARKLORD ")]
            if not lines:
                continue
            identity = path.name.removesuffix(".darklord.log").split("-")
            current = identity == [str(session.get("pid")), str(session.get("process_created"))]
            name = "darklord/" + path.name
            logs[name] = _sanitize_support_text("\n".join(lines)) + "\n"
            records.append({"archive_member": name, "pid": int(identity[0]),
                            "process_created": int(identity[1]), "line_count": len(lines),
                            "source_size": size, "truncated": size > limit,
                            "evidence": "current_process" if current else "last_known"})
    except OSError as error:
        errors.append(type(error).__name__)
    return logs, {"status": "available" if logs else "unavailable", "records": records,
                  "errors": errors, "reason": None if logs else "no_darklord_telemetry"}


def _file_identity(path, name):
    result = {"name": name}
    try:
        if path.is_symlink():
            return {**result, "status": "omitted", "reason": "symbolic_link"}
        with path.open("rb") as stream:
            digest = hashlib.sha256()
            size = 0
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        result.update(status="available", size=size, sha256=digest.hexdigest())
    except FileNotFoundError:
        result["status"] = "missing"
    except OSError as error:
        result.update(status="unreadable", reason=str(error))
    return result


def _install_diagnostics(config):
    from ..contracts.core_distribution import _unique, validate_manifest

    base = doom_base_dir_from_config(config or {})
    result = {"game_found": None, "path_type": "Proton" if os.name != "nt" or (config or {}).get("proton_compat_data_dir") else "Windows",
              "game_root": str(base.parent) if base else None, "dlls": [], "mods": [],
              "manifest": {"parses": None}, "structure": {"status": "unknown"},
              "ownership": {"status": "unknown"}, "compatibility": {"status": "unknown"}, "issues": []}
    if base is None:
        result["issues"].append(player_text('launcher.doctor', 'game_directory_is_not_configured'))
        return result
    root = base.parent
    try:
        result["game_found"] = (root / "DOOMEternalx64vk.exe").is_file() and base.is_dir()
        names = {"sentinel_core.dll", "msimg32.dll", "XINPUT1_3.dll"}
        names.update(path.name for path in root.iterdir() if path.suffix.casefold() == ".dll")
        result["dlls"] = [_file_identity(root / name, name) for name in sorted(names)]
        mods = root / "Mods"
        result["mods_status"] = "available" if mods.is_dir() else "missing"
        result["mods"] = [_file_identity(path, path.relative_to(mods).as_posix())
                          for path in sorted(mods.rglob("*")) if path.is_file() or path.is_symlink()]
    except OSError as error:
        result["issues"].append(str(error))
    manifest = result["manifest"]
    try:
        with (root / "sentinel-distribution.json").open("rb") as stream:
            raw = stream.read(SUPPORT_DIAGNOSTIC_MAX_BYTES + 1)
        if len(raw) > SUPPORT_DIAGNOSTIC_MAX_BYTES:
            manifest.update(status="omitted", reason=player_text('launcher.doctor', 'manifest_exceeds_256_kib'))
            return result
        document = json.loads(raw, object_pairs_hook=_unique)
    except FileNotFoundError:
        manifest.update(status="missing")
        return result
    except (OSError, ValueError, UnicodeError) as error:
        manifest.update(parses=False, status="unreadable", reason=str(error))
        return result
    manifest.update(parses=True, status="available", document=document)
    try:
        validate_manifest(document, require_compatible=False)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        result["structure"] = {"status": "invalid", "reason": str(error)}
        return result
    result["structure"] = {"status": "valid"}
    artifacts = {item["path"].casefold(): item for item in document["artifacts"]}
    installed = {item["name"].casefold(): item for item in result["dlls"]}
    pair = [installed.get(name, {}) for name in ("sentinel_core.dll", "msimg32.dll")]
    matches = all(item.get("status") == "available" and
                  all(item[field] == artifacts[name][field] for field in ("size", "sha256"))
                  for name, item in zip(("sentinel_core.dll", "msimg32.dll"), pair, strict=False))
    result["ownership"] = {"status": "owned" if matches else "unverified",
                           "reason": player_text('launcher.doctor', 'core_and_bootstrap_hashes_match_manifest') if matches else player_text('launcher.doctor', 'core_or_bootstrap_missing_or_different')}
    try:
        validate_manifest(document)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        result["compatibility"] = {"status": "incompatible", "reason": str(error)}
    else:
        result["compatibility"] = {"status": "compatible"}
    return result


def _setup_failure_summary(payload, support):
    failure = payload.get("last_setup_failure") or support.get("session_preparation_failure", {}).get("evidence") or {}
    if failure:
        return (player_text('launcher.doctor', 'fail_last_setup_failure_value_0_value_1_date_value_2', value_0=f"{failure.get('failure_domain', 'unknown')}", value_1=f"{failure.get('user_message') or failure.get('message') or failure.get('technical_message') or 'unknown'}", value_2=f"{failure.get('at_utc') or failure.get('timestamp') or support.get('session_preparation_failure', {}).get('modified_utc', 'unknown')}"))
    else:
        return (player_text('launcher.doctor', 'unknown_last_setup_failure_none_recorded'))


def _support_summary(payload, install, members, notes):
    support = payload.get("support_diagnostics", {})
    session = support.get("session", {})
    link = support.get("game_link", {})
    native = link.get("native_rpc", {})
    health = native.get("health", {})
    delivery_state = link.get("delivery", {})
    receipt = support.get("room_install_receipt", {})
    manifest = install["manifest"].get("document")
    core_version = manifest.get("version", "unknown") if isinstance(manifest, dict) else "unknown"
    apworld_version = session.get("slot_data", {}).get("apworld_revision", "unknown")
    lines = [player_text('launcher.doctor', 'doomeap_support_report'), player_text('launcher.doctor', 'utc_value_0', value_0=f"{payload['created_utc']}"), player_text('launcher.doctor', 'report_id_value_0', value_0=f"{payload['report_id']}"),
             player_text('launcher.doctor', 'versions_launcher_value_0_apworld_value_1_core_value_2', value_0=f"{payload['version']}", value_1=f'{apworld_version}', value_2=f'{core_version}'),
             player_text('launcher.doctor', 'game_path_type_value_0', value_0=f"{install['path_type']}"),
             player_text('launcher.doctor', 'room_seed_value_0_slot_value_1', value_0=f"{session.get('seed_name') or 'unknown'}", value_1=f"{session.get('slot') or 'unknown'}")]
    checks = {item["key"]: item for item in payload["diagnostics"]}
    game = install["game_found"]
    foreign_xinput = any(item["name"].casefold() == "xinput1_3.dll" and item["status"] == "available" for item in install["dlls"])
    core = install["ownership"]["status"] == "owned" and install["compatibility"]["status"] == "compatible" and not foreign_xinput
    core_known = install["manifest"].get("status") in {"available", "missing"}
    staged = receipt.get("hash_match")
    if staged is None and receipt.get("staged_exists") is False:
        staged = False
    game_connected = health.get("ready") if native.get("evidence") == "direct" else None
    ap_connected = session.get("connected") if session.get("identity_source") == "connected" else None
    if session.get("identity_source") == "last_known" and session.get("connected") is False:
        ap_connected = False
    delivery = _delivery_verdict(delivery_state)
    for name, value, detail in (
        (player_text('launcher.play', 'game_found'), game, checks.get("game", {}).get("message", "")),
        (player_text('launcher.doctor', 'core_installed_and_compatible'), core if core_known else None,
         player_text('launcher.doctor', 'ownership_value_0_compatibility_value_1_xinput_present_value_2', value_0=f"{install['ownership']['status']}", value_1=f"{install['compatibility']['status']}", value_2=f'{foreign_xinput}')),
        (player_text('launcher.doctor', 'room_mod_staged'), staged, player_text('launcher.doctor', 'receipt_unavailable') if receipt.get("status") != "available" else player_text('launcher.doctor', 'installation_receipt')),
        (player_text('launcher.support', 'game_connection'), game_connected, str(health.get("reason", player_text('launcher.doctor', 'no_current_evidence')))),
        (player_text('launcher.doctor', 'ap_connection'), ap_connected, str(session.get("identity_source", "unavailable"))),
        (player_text('launcher.doctor', 'item_delivery'), delivery, player_text('launcher.doctor', 'ledger_value_0_counts_value_1_truncated_value_2', value_0=f"{delivery_state.get('status', 'unavailable')}", value_1=f"{delivery_state.get('counts', {})}", value_2=f"{delivery_state.get('truncated', False)}")),
    ):
        verdict = 'PASS' if value is True else 'FAIL' if value is False else 'UNKNOWN'
        lines.append(f"{verdict} {name}: {detail}".rstrip(": "))
    lines.append(_setup_failure_summary(payload, support))
    warnings = []
    for name, content in members.items():
        if not name.endswith((".log", ".jsonl", ".jsonl.previous")):
            continue
        for line in content.decode("utf-8", errors="replace").splitlines():
            if re.search(r"(?:^|[\W_])(warn(?:ing)?|error|failed|failure)(?:$|[\W_])", line, re.I):
                stamp = re.search(r"\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?", line)
                warnings.append((stamp.group() if stamp else player_text('launcher.doctor', 'timestamp_unknown'), name, line))
    warnings.sort(key=lambda item: "" if item[0] == "timestamp unknown" else item[0])
    lines.append(player_text('launcher.doctor', 'last_warnings_errors_up_to_5'))
    lines.extend(f"  {stamp} {name}: {line[:180]}" for stamp, name, line in warnings[-5:])
    if not warnings:
        lines.append(player_text('launcher.doctor', 'none_recorded'))
    lines.append(player_text('launcher.doctor', 'zip_files_uncompressed_bytes'))
    lines.append(player_text('launcher.doctor', 'summary_txt_this_file'))
    lines.extend(f"  {name}: {len(content)}" for name, content in members.items())
    lines.append(player_text('launcher.doctor', 'collection_limits_omissions'))
    lines.extend(f"  {note}" for note in notes)
    if not notes:
        lines.append("  none")
    text = "\n".join(str(line).replace("\r", " ").replace("\n", " ")[:240] for line in lines) + "\n"
    if len(text.encode("utf-8")) > 64 * 1024:
        text = text.encode("utf-8")[:63 * 1024].decode("utf-8", errors="ignore") + '' + '\n' + player_text('launcher.doctor', 'summary_truncated_at_63_kib') + '\n' + ''
    return text


def _compiled_spawn_positions(support):
    compiled_positions = {"status": "unavailable", "reason": "room_diagnostic_missing"}
    staged = support.get("room_install_receipt", {}).get("staged_mod")
    if isinstance(staged, str) and staged:
        try:
            with zipfile.ZipFile(staged) as room:
                member = room.getinfo("enemy_spawn_diagnostics.json")
                if member.file_size <= SUPPORT_SPAWN_DIAGNOSTIC_MAX_BYTES:
                    compiled_positions = {"status": "available", "report": json.loads(room.read(member))}
                else:
                    compiled_positions["reason"] = "room_diagnostic_exceeds_limit"
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            pass
    return compiled_positions


def _executable_identity(config):
    game_root = (config or {}).get("game_root") or (config or {}).get("doom_base_dir")
    if game_root:
        root = Path(str(game_root)).expanduser()
        root = root.parent if root.name.casefold() == "base" else root
        return _file_identity(root / "DOOMEternalx64vk.exe", "DOOMEternalx64vk.exe")
    else:
        return {"status": "unavailable"}


def write_support_bundle(
    destination: Path,
    report: DoctorReport,
    *,
    logs: Sequence[str] = (),
    config: Mapping[str, object] | None = None,
    paths: object | None = None,
    application_dir: Path | None = None,
    session_start: float | None = None,
    last_setup_failure: Mapping[str, object] | None = None,
    last_connection_error: Mapping[str, object] | None = None,
    support_condump: Mapping[str, object] | None = None,
    support_diagnostics: Mapping[str, object] | None = None,
    archive_directory: Path | None = None,
    injector_output: str | None = None,
) -> Path:
    """Retain a verified local archive and export without replacing existing files."""
    destination = destination.expanduser().absolute()
    if destination.exists():
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        candidate = destination.with_name(f"{destination.stem}_{stamp}{destination.suffix}")
        counter = 2
        while candidate.exists():
            candidate = destination.with_name(f"{destination.stem}_{stamp}_{counter}{destination.suffix}")
            counter += 1
        destination = candidate
    tails, provenance = _support_log_tails(
        config, paths, application_dir, session_start=session_start
    )
    payload = sanitize_support_bundle(report.document())
    payload["created_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    payload["report_id"] = str(uuid.uuid4())
    payload["system"] = {
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "architecture": platform.machine(),
    }
    payload["log_provenance"] = sanitize_support_value(provenance)
    if last_setup_failure is not None:
        payload["last_setup_failure"] = sanitize_support_value(dict(last_setup_failure))
    if last_connection_error is not None:
        payload["last_connection_error"] = sanitize_support_value(dict(last_connection_error))
    condump_content = None
    condump_metadata = dict(support_condump or {})
    support_path = condump_metadata.get("path")
    if isinstance(support_path, str | Path):
        try:
            source_path = Path(support_path)
            condump_content, retention = _encounter_console(source_path)
            condump_metadata.update({
                "source_filename": condump_metadata.get("source_filename", source_path.name),
                **retention,
            })
        except OSError as error:
            condump_metadata.update({
                "status": "unavailable",
                "reason": f"source_unreadable: {type(error).__name__}",
            })
    if support_condump is not None:
        payload["support_condump"] = sanitize_support_value(condump_metadata)
    if support_diagnostics is not None:
        payload["support_diagnostics"] = sanitize_support_value(dict(support_diagnostics))
    support = payload.get("support_diagnostics", {})
    from .support_evidence import collect, safe_timeline

    payload.update(sanitize_support_value(collect(support)))
    support["installation"] = payload["installation_journal"]
    support["ledger_timeline"] = sanitize_support_value(safe_timeline(support.get("ledger_timeline", {"status": "inactive"})))
    compiled_positions = _compiled_spawn_positions(support)
    payload["compiled_spawn_positions"] = sanitize_support_value(compiled_positions)
    session = support.get("ap_session") or support.get("campaign_inspection", {}).get("session", {})
    install = sanitize_support_value(_install_diagnostics(config))
    payload["executable_identity"] = sanitize_support_value(_executable_identity(config))
    darklord_logs, darklord = _darklord_diagnostics(config, session)
    tails.update(darklord_logs)
    payload["darklord_telemetry"] = sanitize_support_value(darklord)
    native_startup = _native_startup_diagnostics(config, session, install)
    payload["native_startup"] = sanitize_support_value(native_startup)
    payload["windows_crash_evidence"] = _windows_crash_evidence()
    safe_logs = _bound_support_text(
        "\n".join(_sanitize_support_text(str(line)) for line in logs)
    )
    if safe_logs:
        provenance.setdefault("launcher.log", {})["in_memory_diagnostics"] = {
            "status": "available",
            "source": "launcher_in_memory_diagnostics",
            "line_count": len(safe_logs.splitlines()),
        }
        payload["log_provenance"] = sanitize_support_value(provenance)
    notes = []
    if "[... HEAD+TAIL BOUNDARY ...]" in safe_logs:
        notes.append(player_text('launcher.doctor', 'in_memory_launcher_log_head_tail_retained_middle_omitted'))
    history_files = support.get("native_diagnostics", {}).pop("files", [])
    for source in history_files:
        name = "sentinel/" + source["source_filename"]
        records = source.pop("records")
        if not records:
            notes.append(player_text('launcher.doctor', 'value_0_omitted_no_matching_records_within_source_limits', value_0=f'{name}'))
            continue
        tails[name] = (json.dumps(records[0]) if name.endswith(".json") and records else
                       "\n".join(json.dumps(record) for record in records)) + "\n"
        if source["truncated"]:
            notes.append(player_text('launcher.doctor', 'value_0_source_tail_last_64_records_retained_source_value_1_byte', value_0=f'{name}', value_1=f"{source['source_size']}"))
    if history_files:
        support["native_diagnostics"]["files"] = history_files
    for issue in support.get("native_diagnostics", {}).get("issues", []):
        notes.append(player_text('launcher.doctor', 'sentinel_history_value_0_unavailable_value_1', value_0=f"{issue['file']}", value_1=f"{issue['error']}"))
    for issue in native_startup.get("errors", []):
        notes.append(player_text('launcher.doctor', 'native_startup_value_0_skipped_value_1', value_0=f"{issue['source_filename']}", value_1=f"{issue['reason']}"))
    for error in darklord.get("errors", []):
        notes.append(player_text('launcher.doctor', 'dark_lord_telemetry_value_0', value_0=f'{error}'))
    base = doom_base_dir_from_config(config or {})
    if base is not None:
        console = base / "qconsole.log"
        try:
            tails["logs/qconsole.log"], retention = _encounter_console(console)
            if retention["omitted_bytes"]:
                notes.append(player_text('launcher.doctor', 'logs_qconsole_log_value_0_source_value_1_bytes', value_0=f"{retention['truncation']}", value_1=f"{retention['source_size']}"))
        except OSError as error:
            notes.append(player_text('launcher.doctor', 'logs_qconsole_log_unavailable_value_0', value_0=f'{type(error).__name__}'))
    else:
        notes.append(player_text('launcher.doctor', 'logs_qconsole_log_game_directory_unavailable'))
    if injector_output is not None:
        tails["install/injector.log"] = _sanitize_support_text(injector_output)
        notes.append(player_text('launcher.doctor', 'install_injector_log_full_stdout_and_stderr_of_the_most_recent_r'))
    else:
        notes.append(player_text('launcher.doctor', 'install_injector_log_no_run_retained_in_this_launcher_session'))
    for name, details in provenance.items():
        if details.get("status") == "unavailable":
            notes.append(player_text('launcher.doctor', 'value_0_unavailable_value_1', value_0=f'{name}', value_1=f"{details.get('reason', 'unknown')}"))
        elif details.get("size_bytes", 0) > SUPPORT_LOG_MAX_BYTES:
            notes.append(player_text('launcher.doctor', 'value_0_bounded_log_excerpt_source_value_1_bytes', value_0=f'{name}', value_1=f"{details['size_bytes']}"))
    for record in darklord["records"] if "records" in darklord else ():
        if record["truncated"]:
            notes.append(player_text('launcher.doctor', 'value_0_last_1_mib_retained_source_value_1_bytes', value_0=f"{record['archive_member']}", value_1=f"{record['source_size']}"))
    if condump_metadata.get("omitted_bytes"):
        notes.append(player_text('launcher.doctor', 'ap_support_file_txt_value_0_value_1_bytes_omitted', value_0=f"{condump_metadata['truncation']}", value_1=f"{condump_metadata['omitted_bytes']}"))
    notes.extend(install["issues"])
    notes.extend(player_text('launcher.doctor', 'install_json_value_0_value_1_value_2', value_0=f"{item['name']}", value_1=f"{item['status']}", value_2=f"{item.get('reason', 'unavailable')}")
                 for item in install["dlls"] + install["mods"] if item["status"] in {"omitted", "unreadable"})
    if install["manifest"].get("status") != "available":
        notes.append(player_text('launcher.doctor', 'install_json_manifest_value_0', value_0=f"{install['manifest'].get('reason') or install['manifest'].get('status', 'unavailable')}"))
    members = {
        "installation_journal.json": json.dumps(payload["installation_journal"], indent=2, sort_keys=True),
        "checkpoint_generations.json": json.dumps(payload["checkpoint_generations"], indent=2, sort_keys=True),
        "executable_identity.json": json.dumps(payload["executable_identity"], indent=2, sort_keys=True),
        "ledger_timeline.json": json.dumps(support.get("ledger_timeline", {"status": "inactive"}), indent=2, sort_keys=True),
        "install.json": json.dumps(install, indent=2, sort_keys=True),
        "doctor.json": json.dumps(payload, indent=2, sort_keys=True),
        "native_startup.json": json.dumps(payload["native_startup"], indent=2, sort_keys=True),
        "windows_crash_evidence.json": json.dumps(payload["windows_crash_evidence"], indent=2, sort_keys=True),
        "compiled_spawn_positions.json": json.dumps(payload["compiled_spawn_positions"], indent=2, sort_keys=True),
        "session_admission_failure.json": json.dumps(support.get("session_admission_failure", {"status": "unavailable"}), indent=2, sort_keys=True),
    }
    if "launcher.log" not in tails:
        members["launcher.log"] = safe_logs + ("\n" if safe_logs else "")
    elif safe_logs:
        tails["launcher.log"] += "\n" + safe_logs + "\n"
    for name in sorted(tails, key=lambda name: not name.startswith(("sentinel/", "install/", "logs/"))):
        members[name] = tails[name]
    if condump_content is not None:
        members["AP_SUPPORT_FILE.txt"] = condump_content
    retained = {}
    # reserve space for the summary and ZIP headers, even with incompressible logs
    remaining = SUPPORT_BUNDLE_MAX_BYTES - 128 * 1024
    for name, content in members.items():
        data = content.encode("utf-8")
        limit = min(remaining, SUPPORT_LOG_MAX_BYTES if name == "logs/qconsole.log" else remaining)
        if len(data) > limit:
            if name.endswith(".log") and limit > 0:
                notes.append(player_text('launcher.doctor', 'value_0_truncated_from_value_1_to_at_most_value_2_bytes', value_0=f'{name}', value_1=f'{len(data)}', value_2=f'{limit}'))
                data = data[-limit:].decode("utf-8", errors="ignore").encode("utf-8")
            else:
                notes.append(player_text('launcher.doctor', 'value_0_omitted_value_1_bytes_10_mib_bundle_budget', value_0=f'{name}', value_1=f'{len(data)}'))
                if name in {"doctor.json", "install.json"}:
                    document = {"status": "omitted", "reason": player_text('launcher.doctor', 'text_10_mib_bundle_budget')}
                    if name == "doctor.json":
                        document.update(report_id=payload["report_id"], version=payload["version"])
                    data = json.dumps(document).encode("utf-8")
                else:
                    continue
        retained[name] = data
        remaining -= len(data)
    summary = _support_summary(payload, install, retained, notes)
    storage = archive_directory or destination.parent / "support-bundles"
    storage.mkdir(parents=True, exist_ok=True)
    owned_directory = Path(tempfile.mkdtemp(prefix="report-", dir=storage))
    local_archive = owned_directory / destination.name
    descriptor, temporary_name = tempfile.mkstemp(prefix=".archive.", suffix=".tmp", dir=owned_directory)
    os.close(descriptor)
    temporary = Path(temporary_name)
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("summary.txt", summary)
        for name, data in retained.items():
            archive.writestr(name, data)
    with zipfile.ZipFile(temporary) as archive:
        if archive.testzip() is not None:
            raise OSError(player_text('launcher.doctor', 'support_archive_verification_failed_value_0', value_0=f'{temporary}'))
    publish_file(temporary, local_archive, operation="support_bundle_publish")
    with zipfile.ZipFile(local_archive) as archive:
        if archive.testzip() is not None:
            raise OSError(player_text('launcher.doctor', 'support_archive_verification_failed_value_0', value_0=f'{local_archive}'))
    export_owned = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as exported, local_archive.open("rb") as source:
            export_owned = True
            shutil.copyfileobj(source, exported)
            exported.flush()
            os.fsync(exported.fileno())
        with zipfile.ZipFile(destination) as archive:
            if archive.testzip() is not None:
                raise OSError(player_text('launcher.doctor', 'support_export_verification_failed'))
        return destination
    except (OSError, zipfile.BadZipFile) as error:
        if export_owned:
            try:
                destination.unlink()
            except OSError:
                pass
        logging.getLogger(__name__).warning(
            "Support export unavailable (%s); verified archive retained at %s",
            type(error).__name__, local_archive,
        )
        return local_archive


def _installation_receipt_message(receipt):
    return str(receipt.get("adapter_message") or player_text('launcher.doctor', 'install_the_room_mod_before_playing'))


class LauncherDoctor:
    VERSION = "0.8.0"

    def __init__(
        self,
        *,
        config: Mapping[str, object] | None = None,
        paths: object | None = None,
        config_path: Path | None = None,
        last_setup_failure: Mapping[str, object] | None = None,
        last_room_package_issue: Mapping[str, object] | None = None,
        live_support: Mapping[str, object] | None = None,
    ):
        self.config = dict(config or {})
        self.paths = paths
        self.config_path = config_path
        self.last_setup_failure = dict(last_setup_failure or {})
        self.last_room_package_issue = dict(last_room_package_issue or {})
        self.live_support = dict(live_support or {})

    def _current_issue(self) -> dict[str, object] | None:
        for issue in (self.last_room_package_issue, self.last_setup_failure):
            if issue.get("failure_domain") in {"room_package", "installed_room_package"}:
                return issue
        return None

    def _state_dir(self) -> Path | None:
        value = getattr(self.paths, "state_dir", None)
        return Path(value).expanduser().resolve() if value else None

    def _application_dir(self) -> Path | None:
        value = self.config.get("application_dir") or getattr(self.paths, "application_dir", None)
        return Path(str(value)).expanduser().resolve() if value else None

    def repair_actions(self) -> tuple[RepairAction, ...]:
        """Offer actions only when launcher receipt proves ownership."""
        actions: list[RepairAction] = []
        game_root = self.config.get("game_root") or self.config.get("doom_base_dir")
        if game_root:
            try:
                root = Path(str(game_root))
                root = validate_game_root(root.parent if root.name.lower() == "base" else root)
                meathook_probe = probe_meathook(root)
                if meathook_probe.status == PrerequisiteStatus.MISSING:
                    actions.append(RepairAction(
                        "install_game_link", player_text('launcher.doctor', 'set_up_game_connection'),
                        (player_text('launcher.doctor', 'download_and_install_the_matching_game_connection_files'),),
                        False,
                        player_text('launcher.doctor', 'requires_matching_game_connection_files'),
                    ))
                elif meathook_probe.status in {PrerequisiteStatus.INCOMPATIBLE, PrerequisiteStatus.INVALID}:
                    actions.append(RepairAction(
                        "repair_game_link", player_text('launcher.doctor', 'repair_game_connection'),
                        (
                            player_text('launcher.doctor', 'check_existing_files'),
                            player_text('launcher.doctor', 'reinstall_matching_connection_files'),
                        ),
                        True,
                        player_text('launcher.doctor', 'other_mods_are_kept_previous_files_are_restored_if_installation'),
                    ))
            except Exception:
                pass

        state_dir = self._state_dir()
        data_value = getattr(self.paths, "data_dir", None)
        data_dir = Path(data_value).expanduser() if data_value else None
        selected_spec = idfile_decompressor_spec()
        if data_dir is not None and selected_spec is not None:
            codec_manager = DependencyManager(data_dir / "dependencies")
            try:
                codec_supported = codec_manager.platform_supported(selected_spec)
                codec_inspected = codec_manager.inspect(selected_spec) if codec_supported else None
                codec_legacy_present = (
                    not codec_supported and codec_manager.cache_present(selected_spec)
                )
            except (OSError, TimeoutError, RuntimeError):
                codec_supported = False
                codec_inspected = None
                codec_legacy_present = False
            if codec_supported:
                if codec_inspected is None:
                    actions.append(RepairAction(
                        "repair_idfile_decompressor",
                        player_text('launcher.doctor', 'repair_mod_building_tool'),
                        (
                            player_text('launcher.doctor', 'download_and_check_version_value_0', value_0=f'{selected_spec.version}'),
                            player_text('launcher.doctor', 'verify_sha_256_value_0', value_0=f'{selected_spec.sha256}'),
                        ),
                        True,
                        player_text('launcher.doctor', 'store_the_checked_tool_for_reuse'),
                    ))
                else:
                    actions.append(RepairAction(
                        "remove_idfile_decompressor",
                        player_text('launcher.doctor', 'remove_downloaded_mod_building_tool'),
                        (player_text('launcher.doctor', 'remove_the_downloaded_tool'),),
                        True,
                        player_text('launcher.doctor', 'setup_will_ask_before_downloading_it_again'),
                    ))
            elif codec_legacy_present:
                actions.append(RepairAction(
                    "remove_idfile_decompressor",
                    player_text('launcher.doctor', 'remove_incompatible_mod_building_tool'),
                    (player_text('launcher.doctor', 'remove_the_downloaded_version_that_cannot_run_on_this_system'),),
                    True,
                    player_text('launcher.doctor', 'this_downloaded_version_cannot_run_on_windows'),
                ))
        issue = self._current_issue()
        if issue is not None:
            action_id = (
                "update_room_package"
                if issue.get("recovery_action") == "update_room_package"
                else "rebuild_room_package"
            )
            actions.append(RepairAction(
                action_id,
                player_text('launcher.doctor', 'update_room_mod') if action_id == "update_room_package" else player_text('launcher.doctor', 'rebuild_room_mod'),
                (player_text('launcher.doctor', 'build_and_check_the_mod_for_this_room'),),
                True,
                player_text('launcher.doctor', 'existing_saves_and_received_items_are_kept'),
            ))
        if state_dir is None:
            return tuple(actions)

        root: Path | None = None
        if game_root:
            root = Path(str(game_root)).expanduser().resolve()
            if root.name.casefold() == "base":
                root = root.parent

        receipt_path = state_dir / "launcher_setup.json"
        if not receipt_path.is_file():
            return tuple(actions)
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                raise ValueError(player_text('launcher.doctor', 'record_must_be_an_object'))
            staged = Path(str(receipt["staged_mod"])).resolve()
            digest = str(receipt["staged_sha256"])
            adapter_state = str(receipt.get("adapter_state", ""))
            if root is not None:
                owned_location = staged.parent == root / "Mods"
                if not owned_location:
                    raise ValueError(player_text('launcher.doctor', 'recorded_package_is_outside_configured_mods_folder'))
            if adapter_state in UNINSTALL_OWNED_STATES:
                return tuple(actions)
            if not staged.is_file():
                actions.append(RepairAction(
                    "reinstall_room_mod", player_text('launcher.doctor', 'reinstall_missing_room_mod'),
                    (player_text('launcher.doctor', 'install') + staged.name,), True,
                    player_text('launcher.doctor', 'previous_files_are_backed_up'),
                ))
                return tuple(actions)
            actual = hashlib.sha256(staged.read_bytes()).hexdigest()
            if actual != digest:
                actions.append(RepairAction(
                    "reinstall_room_mod", player_text('launcher.doctor', 'reinstall_changed_room_mod'),
                    (player_text('launcher.doctor', 'replace') + staged.name, player_text('launcher.doctor', 'sha_256_value_0_value_1', value_0=f'{actual}', value_1=f'{digest}')), True,
                    player_text('launcher.doctor', 'previous_files_are_backed_up'),
                ))
                return tuple(actions)
            if os.name == "nt" and adapter_state == "applied" and root is not None:
                try:
                    from .campaign_resources import verify_installed_resources
                    verify_installed_resources(root, receipt)
                except (OSError, ValueError) as error:
                    actions.append(RepairAction(
                        "reinstall_room_mod", player_text('launcher.doctor', 'repair_changed_game_files'),
                        (player_text('launcher.doctor', 'reinstall') + staged.name,), True, str(error),
                    ))
                    return tuple(actions)
            if os.name != "nt" and adapter_state == "applied":
                try:
                    _verify_linux_receipt(receipt, self.config, application_dir=self._application_dir())
                except Exception as error:
                    actions.append(RepairAction(
                        "reinstall_room_mod", player_text('launcher.doctor', 'repair_room_mod'),
                        (player_text('launcher.doctor', 'reinstall_and_check_value_0', value_0=f'{staged.name}'),), True,
                        player_text('launcher.doctor', 'installation_failed_at_verify_value_0_value_1', value_0=f'{type(error).__name__}', value_1=f'{error}'),
                    ))
                    return tuple(actions)
            if adapter_state != "applied":
                actions.append(RepairAction(
                    "reinstall_room_mod", player_text('launcher.ui', 'install_room_mod'),
                    (player_text('launcher.doctor', 'complete_installation_in_eternalmodinjector'),), True,
                    player_text('launcher.doctor', 'confirm_installation_when_prompted'),
                ))
                return tuple(actions)
        except (OSError, RuntimeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            if issue is not None:
                return tuple(actions)
            actions.append(RepairAction(
                "archive_stale_install_record", player_text('launcher.doctor', 'reset_saved_installation_details'),
                (player_text('launcher.doctor', 'back_up_installation_details_value_0', value_0=f'{receipt_path.name}'),), False,
                player_text('launcher.doctor', 'previous_details_can_be_restored_from_the_backup'),
            ))
            return tuple(actions)
        return tuple(actions)

    def repair_preview(self) -> tuple[RepairAction, ...]:
        return self.repair_actions()

    def archive_stale_install_record(self) -> Path:
        """Move only launcher state receipt into rollback storage."""
        state_dir = self._state_dir()
        if state_dir is None:
            raise ValueError(player_text('launcher.doctor', 'launcher_state_directory_is_unavailable'))
        receipt = state_dir / "launcher_setup.json"
        if not receipt.is_file():
            raise ValueError(player_text('launcher.doctor', 'launcher_install_record_is_unavailable'))
        backups = state_dir / "repair-backups"
        backups.mkdir(parents=True, exist_ok=True)
        backup = backups / "launcher_setup.json"
        if backup.exists():
            raise ValueError(player_text('launcher.doctor', 'repair_backup_already_exists_restore_or_remove_it_first'))
        publish_file(receipt, backup, operation="archive_install_record")
        return backup

    def restore_archived_install_record(self) -> Path:
        state_dir = self._state_dir()
        if state_dir is None:
            raise ValueError(player_text('launcher.doctor', 'launcher_state_directory_is_unavailable'))
        backup = state_dir / "repair-backups" / "launcher_setup.json"
        receipt = state_dir / "launcher_setup.json"
        if not backup.is_file() or receipt.exists():
            raise ValueError(player_text('launcher.doctor', 'install_record_rollback_is_unavailable'))
        publish_file(backup, receipt, operation="restore_install_record")
        return receipt

    def run(self) -> DoctorReport:
        checks: list[Diagnostic] = []
        platform_name = player_text('launcher.doctor', 'windows') if os.name == "nt" else player_text('launcher.doctor', 'linux')
        checks.append(Diagnostic("platform", "ok", platform_name))
        game_root = self.config.get("game_root") or self.config.get("doom_base_dir")
        root: Path | None = None
        if game_root:
            try:
                root = validate_game_root(Path(str(game_root)))
                checks.append(Diagnostic("game", "ok", player_text('launcher.doctor', 'game_folder_found'), {"path": str(root)}))
            except ValueError as error:
                checks.append(Diagnostic("game", "invalid", str(error)))
        else:
            game_discovery = self.config.get("game_discovery")
            details: dict[str, object] | None = None
            if isinstance(game_discovery, dict):
                details = {"discovery": game_discovery}
            checks.append(Diagnostic("game", "missing", player_text('launcher.doctor', 'choose_the_folder_containing_doometernalx64vk_exe'), details))

        meathook_probe = probe_meathook(root)
        checks.append(Diagnostic(
            "meathook",
            meathook_probe.status.value,
            meathook_probe.message,
            meathook_probe.details,
        ))

        try:
            processes = list(detect_doom_processes())
            checks.append(Diagnostic("processes", "ok", player_text('launcher.doctor', 'process_probe_complete'), {"items": processes}))
        except ProcessInspectionError as error:
            processes = []
            checks.append(Diagnostic("processes", "attention", str(error)))
        checks.append(Diagnostic("config", "ok", player_text('launcher.doctor', 'launcher_configuration_loaded'), {"keys": sorted(self.config)}))
        checks.append(Diagnostic(
            "runtime_paths",
            "ok",
            player_text('launcher.doctor', 'effective_runtime_paths_and_path_selection_evidence_collected'),
            _runtime_diagnostics(self.config, self.config_path, self.paths, processes, app_dir=self._application_dir()),
        ))
        runtime_details = checks[-1].details if isinstance(checks[-1].details, Mapping) else {}
        support_diagnostics = build_support_diagnostics(
            self.config,
            self.config_path,
            self.paths,
            processes,
            runtime_details,
            {
                "status": meathook_probe.status.value,
                **dict(meathook_probe.details),
            },
            live=self.live_support,
        )
        health = support_diagnostics["game_link"]["native_rpc"].get("health")
        admission = health.get("session") if isinstance(health, Mapping) else None
        game_pids = {str(process.get("pid")) for process in processes
                     if str(process.get("name", "")).casefold() in {"doometernalx64vk", "doometernalx64vk.exe"}}
        if (isinstance(admission, Mapping) and admission.get("state") == "admission_refused"
                and str(admission.get("pid")) in game_pids):
            checks.append(Diagnostic("ap_session", "attention",
                player_text('launcher.doctor', 'save_setup_failed_do_not_load_the_save_yet_select_help_save_supp'),
                dict(admission)))

        state_dir = self._state_dir()
        receipt_path = (state_dir / "launcher_setup.json") if state_dir else None
        room_uninstall_state = ""
        room_receipt_applied = False
        if receipt_path and receipt_path.is_file():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                adapter_state = str(receipt.get("adapter_state", ""))
                room_uninstall_state = adapter_state if adapter_state in UNINSTALL_OWNED_STATES else ""
                mode = str(receipt.get("installation_mode", ""))
                if adapter_state == "applied":
                    if os.name != "nt":
                        try:
                            verification = _verify_linux_receipt(receipt, self.config, application_dir=self._application_dir())
                        except Exception as error:
                            details = {
                                "adapter_state": adapter_state,
                                "installation_mode": mode,
                                "post_install_verification": receipt.get("post_install_verification", ""),
                                "verification_error": str(error),
                                **installation_error_details(error, "verify"),
                            }
                            checks.append(Diagnostic("mod_injection", "failed", player_text('launcher.doctor', 'installation_failed_at_verify_value_0_value_1', value_0=f'{type(error).__name__}', value_1=f'{error}'), details))
                        else:
                            details = {
                                "adapter_state": adapter_state,
                                "installation_mode": mode,
                                "post_install_verification": verification.get("state", ""),
                                "required_resources": verification.get("required_resources", ()),
                            }
                            checks.append(Diagnostic("mod_injection", "ok", player_text('launcher.doctor', 'mod_installed_and_checked'), details))
                            room_receipt_applied = True
                    else:
                        from .campaign_resources import verify_installed_resources
                        try:
                            if root is None:
                                raise ValueError(player_text('launcher.doctor', 'game_files_could_not_be_found_verify_the_game_in_steam_then_retr'))
                            verify_installed_resources(root, receipt)
                        except (OSError, ValueError) as error:
                            for key in ("mod_injection", "windows_mod_installer"):
                                checks.append(Diagnostic(key, "failed", str(error), {"adapter_state": adapter_state}))
                        else:
                            checks.append(Diagnostic("mod_injection", "ok", player_text('launcher.doctor', 'game_files_checked'), {"adapter_state": adapter_state, "installation_mode": mode}))
                            checks.append(Diagnostic("windows_mod_installer", "ok", player_text('launcher.doctor', 'mod_installed_and_checked'), {"installation_mode": mode, "adapter_state": adapter_state}))
                            room_receipt_applied = True
                elif adapter_state in UNINSTALL_OWNED_STATES:
                    details = {"adapter_state": adapter_state, "installation_mode": mode}
                    if adapter_state == "uninstalled":
                        status = "not_applicable"
                        message = player_text('launcher.doctor', 'room_mod_is_not_installed')
                    else:
                        status = "attention"
                        message = player_text('launcher.doctor', 'removal_failed_retry_uninstall_or_select_help_save_support_repor')
                    checks.append(Diagnostic("mod_injection", status, message, details))
                    if os.name == "nt":
                        checks.append(Diagnostic("windows_mod_installer", status, message, details))
                elif adapter_state == "manual_install_required":
                    checks.append(Diagnostic("mod_injection", "failed", player_text('launcher.doctor', 'follow_windows_manual_installation_in_install_md'), {"adapter_state": adapter_state, "installation_mode": mode}))
                    if os.name == "nt":
                        checks.append(Diagnostic("windows_mod_installer", "failed", player_text('launcher.doctor', 'follow_windows_manual_installation_in_install_md'), {"installation_mode": mode, "adapter_state": adapter_state}))
                else:
                    details = {"adapter_state": adapter_state, "installation_mode": mode,
                               **receipt.get("adapter_details", {})}
                    message = _installation_receipt_message(receipt)
                    checks.append(Diagnostic("mod_injection", "failed", message, details))
                    if os.name == "nt":
                        checks.append(Diagnostic("windows_mod_installer", "failed", player_text('launcher.doctor', 'installer_is_not_ready_retry_setup'), {"installation_mode": mode, "adapter_state": adapter_state}))
            except Exception:
                checks.append(Diagnostic("mod_injection", "invalid", player_text('launcher.doctor', 'saved_installation_details_could_not_be_read_select_fix_setup')))
                if os.name == "nt":
                    checks.append(Diagnostic("windows_mod_installer", "invalid", player_text('launcher.doctor', 'saved_installation_details_could_not_be_read_select_fix_setup')))
        else:
            checks.append(Diagnostic("mod_injection", "not_applicable", player_text('launcher.doctor', 'installation_details_are_missing_select_check_setup')))
            if os.name == "nt":
                checks.append(Diagnostic("windows_mod_installer", "not_applicable", player_text('launcher.doctor', 'installation_details_are_missing_select_check_setup')))

        if os.name != "nt":
            checks.append(Diagnostic("windows_mod_installer", "not_applicable", player_text('launcher.doctor', 'windows_only_diagnostic')))

        try:
            actions = self.repair_actions()
        except Exception as error:
            actions = ()
            checks.append(Diagnostic(
                "room_mod",
                "invalid",
                player_text('launcher.doctor', 'room_repair_inspection_is_unavailable_value_0_value_1', value_0=f'{type(error).__name__}', value_1=f'{error}'),
            ))
        room_actions = tuple(
            action for action in actions
            if action.action_id in {
                "archive_stale_install_record", "reinstall_room_mod",
                "rebuild_room_package", "update_room_package",
            }
        )
        if room_actions:
            checks.append(Diagnostic(
                "room_mod", "invalid", player_text('launcher.doctor', 'room_mod_needs_repair_select_fix_setup'),
                {"actions": [asdict(action) for action in room_actions]},
            ))
        elif room_uninstall_state:
            checks.append(Diagnostic(
                "room_mod",
                "not_applicable" if room_uninstall_state == "uninstalled" else "attention",
                (
                    player_text('launcher.doctor', 'room_package_is_intentionally_uninstalled')
                    if room_uninstall_state == "uninstalled"
                    else player_text('launcher.doctor', 'room_package_uninstall_requires_attention_automatic_reinstall_is')
                ),
                {"adapter_state": room_uninstall_state},
            ))
        elif room_receipt_applied:
            checks.append(Diagnostic(
                "room_mod",
                "ok",
                player_text('launcher.doctor', 'room_mod_checked'),
                {"adapter_state": "applied", "receipt": str(receipt_path) if receipt_path else None},
            ))
        else:
            checks.append(Diagnostic("room_mod", "not_applicable", player_text('launcher.doctor', 'installation_could_not_be_checked_select_check_setup')))
        issue = self._current_issue()
        if issue is not None:
            domain = str(issue.get("failure_domain", "room_package"))
            checks.append(Diagnostic(
                "current_room_package",
                "attention",
                str(issue.get("user_message") or issue.get("technical_message") or player_text('launcher.doctor', 'room_mod_needs_repair_select_fix_setup')),
                dict(issue),
            ))
            summary_status = "room_package_needs_attention"
            summary_message = str(issue.get("user_message") or player_text('launcher.doctor', 'room_mod_needs_repair_select_fix_setup'))
            recovery_action = str(issue.get("recovery_action") or "rebuild_room_package")
        else:
            domain = ""
            recovery_action = ""
            overall = support_diagnostics.get("game_link", {}).get("overall", {})
            overall_status = str(overall.get("status", "ready")) if isinstance(overall, Mapping) else "ready"
            overall_reason = str(overall.get("reason", "")) if isinstance(overall, Mapping) else ""
            checks_ok = all(
                item.status not in {"error", "invalid", "missing", "incompatible", "failed", "attention"}
                for item in checks
            )
            if not checks_ok or overall_status == "attention":
                summary_status = "needs_attention"
                summary_message = overall_reason if checks_ok else ""
            elif overall_status == "degraded":
                summary_status = "degraded"
                summary_message = overall_reason
            else:
                summary_status = "ready"
                summary_message = ""
        return DoctorReport(
            self.VERSION,
            tuple(checks),
            summary_status,
            domain,
            recovery_action,
            summary_message,
            support_diagnostics,
        )
