from __future__ import annotations

import asyncio
import datetime
import itertools
import json
import logging
import math
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
from pathlib import Path
from typing import TYPE_CHECKING

from doom_eap.launcher.launcher_platform import publish_file, redact_secrets
from doom_eap.launcher.ui.strings import text as player_text
from doom_eap.runtime.ledger import Ledger, LedgerError, RoomKey

from .launcher_native_health import doom_base_dir_from_config

_CREDENTIAL_FIELDS = frozenset({
    'password', 'passwd', 'passphrase', 'authorization', 'token', 'secret', 'access_token', 'api_token', 'auth_token',
    'refresh_token', 'id_token', 'bearer_token', 'oauth_token', 'oauth_token_secret', 'session_token', 'client_secret',
    'consumer_secret', 'webhook_secret', 'secret_key', 'api_key', 'private_key', 'signing_key',
})


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
        text = text[:match.start()] + '[REDACTED]' + text[end:]
    fields = "|".join(sorted(_CREDENTIAL_FIELDS, key=len, reverse=True))
    text = re.sub(
        rf"""(?ix)(["']?\b(?:[\w-]+_)?(?:{fields})["']?\s*[=:]\s*)
        (?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|(?:Bearer|Basic)\s+\S+|[^\s&,;}}]+)""",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)(\b(?:wss?|https?)://)[^/\s@]+@", r"\1[REDACTED]@", text)
    return redact_secrets(_safe_path(text))


def _redacted_support_field(key, value):
    lowered = key.casefold().replace("-", "_")
    if lowered == "gameplay_authorization" and value == "unvalidated":
        return value
    if lowered in _CREDENTIAL_FIELDS or any(lowered.endswith("_" + field) for field in _CREDENTIAL_FIELDS):
        return '[REDACTED]'


def sanitize_support_value(value: object, *, key: str = "") -> object:
    redacted = _redacted_support_field(key, value)
    if redacted is not None:
        return redacted
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


SUPPORT_LOG_TAIL_BYTES = 256 * 1024
SUPPORT_LOG_MAX_BYTES = 1024 * 1024
SUPPORT_DIAGNOSTIC_MAX_BYTES = 256 * 1024
SUPPORT_DIAGNOSTIC_MAX_ITEMS = 24


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


def _native_process_summary(config, processes, summary):
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


def _native_log_summary(log_text, summary, fields):
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
            summary["rpc_wait_elapsed"] = values.get('elapsed_ms', summary['rpc_wait_elapsed'])
            summary["last_rpc_result"] = values.get('transport', summary['last_rpc_result'])
        elif "Game state transition:" in line:
            summary["memory_probe"] = line[-512:]
        elif "GATE_TRANSITION " in line:
            summary["native_safe"] = "yes" if fields(line).get("state") == "open" else "no"


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
    _native_process_summary(config, processes, summary)

    log_text = None
    for candidate in _native_log_candidates(config)[::2]:
        log_text = _read_log_tail(candidate)
        if log_text:
            break
    if not log_text:
        return summary

    def fields(line: str) -> dict[str, str]:
        return dict(re.findall(r"([a-z_]+)=([^\s]+)", line))

    _native_log_summary(log_text, summary, fields)
    return summary


def _retain_event(events, line, retained, limit):
    events.append(line)
    retained += len(line)
    while retained > limit:
        retained -= len(events.popleft())
    return retained


def _retain_support_events(source, size, death_events, death_retained, death_limit, events, retained, limit):
    while source.tell() < size:
        line = source.readline(32768)
        if not line:
            break
        if b"[DeathLink]" in line or b"deathlink-kill" in line:
            death_retained = _retain_event(death_events, line, death_retained, death_limit)
            continue
        structured = b'DELIVERY_EVENT ' in line and b'PROBE_RESPONSE' not in line
        if structured or any(marker in line for marker in (
            b' ERROR ', b' WARNING ', b'QUEUE_PUBLICATION_', b'GATE_TRANSITION ', b'MISSION_SNAPSHOT ',
            b'Game state transition:', b'AP_STAGE_COMPLETE_',
        )):
            retained = _retain_event(events, line, retained, limit)


def _read_large_support_log(source, size):
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
    _retain_support_events(source, size, death_events, death_retained, death_limit, events, retained, limit)
    payload = (head + b"\n\n[... RETAINED TRANSITION/ERROR EVENTS ...]\n\n"
               + b"".join(events) + b"\n\n[... DEATHLINK EVENTS ...]\n\n"
               + b"".join(death_events) + b"\n\n[... CURRENT TAIL ...]\n\n" + tail)
    return payload


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
                payload = _read_large_support_log(source, size)
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
    return head + '' + '\n' + '' + '\n' + '[... HEAD+TAIL BOUNDARY ...]' + '\n' + '' + '\n' + '' + tail


def _merge_console_spans(spans):
    merged = []
    for begin, end in sorted(spans):
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((begin, end))
    return merged


def _console_chunks(merged, stream, size):
    chunks = []
    for begin, end in merged:
        stream.seek(begin)
        chunk = stream.read(end - begin)
        if begin:
            chunk = chunk.partition(b"\n")[2]
        if end < size:
            chunk = chunk.rpartition(b"\n")[0] + b"\n" if b"\n" in chunk else b""
        chunks.append(chunk)
    return chunks


def _encounter_window(stream, size, limit, separator):
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
    merged = _merge_console_spans(spans)
    chunks = _console_chunks(merged, stream, size)
    retained = sum(map(len, chunks))
    payload = separator.join(chunks)
    truncation = "encounter_window" if last_event is not None else "head_tail"
    return payload, retained, truncation


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
            payload, retained, truncation = _encounter_window(stream, size, limit, separator)
    text = _sanitize_support_text(payload.decode("utf-8", errors="replace"))
    text = text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
    return text, {"source_size": size, "archived_size": len(text.encode("utf-8")),
                  "truncation": truncation, "omitted_bytes": size - retained}


def _crash_dump_locations(locations):
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for app in ("", r"\DOOMEternalx64vk.exe"):
                try:
                    with winreg.OpenKey(hive, 'SOFTWARE\\Microsoft\\Windows\\Windows Error Reporting\\LocalDumps' + app) as key:
                        folder, _ = winreg.QueryValueEx(key, "DumpFolder")
                    if isinstance(folder, str) and folder:
                        locations.append((Path(os.path.expandvars(folder)), "dumps"))
                except OSError:
                    pass
    except ImportError:
        pass


def _crash_report_entry(path, entry, kind):
    if kind == "reports":
        with path.open("rb") as stream:
            raw = stream.read(32768)
        text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig", errors="replace")
        fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        if fields.get("AppName", "").casefold() != "doometernalx64vk.exe":
            return None
        for key, label in fields.items():
            if label.casefold() in {'fault module name', 'exception code', 'exception offset'}:
                entry[label.casefold().replace(" ", "_")] = fields.get(key.removesuffix("Name") + "Value", "unavailable")[:256]
        entry["text"] = _sanitize_support_text(text)[:16384]
    else:
        entry["size"] = path.stat().st_size
    return entry


def _collect_crash_directory(directory, kind, result):
    candidates = (directory.glob("DOOMEternalx64vk.exe*.dmp") if kind == "dumps" else
                  (path / "Report.wer" for path in directory.iterdir() if "doometernal" in path.name.casefold()))
    recent = sorted(((path.stat().st_mtime, path) for path in candidates), reverse=True)[:8]
    for modified, path in recent:
        entry = {"name": path.parent.name[:192] if kind == "reports" else path.name[:192],
                 "modified_utc": datetime.datetime.fromtimestamp(modified, datetime.timezone.utc).isoformat(timespec="seconds")}
        entry = _crash_report_entry(path, entry, kind)
        if entry is not None:
            result[kind].append(entry)


def _windows_crash_evidence() -> dict[str, object]:
    result: dict[str, object] = {"reports": [], "dumps": [], "errors": []}
    if os.name != "nt":
        return {**result, "status": "unavailable", "reason": "windows_only"}
    locations = []
    for variable, suffix, kind in (
        ('PROGRAMDATA', 'Microsoft/Windows/WER/ReportArchive', 'reports'),
        ('LOCALAPPDATA', 'Microsoft/Windows/WER/ReportArchive', 'reports'), ('LOCALAPPDATA', 'CrashDumps', 'dumps'),
        ('LOCALAPPDATA', 'LocalDumps', 'dumps'),
    ):
        root = os.environ.get(variable)
        if not root:
            continue
        locations.append((Path(root) / suffix, kind))
    _crash_dump_locations(locations)
    for directory, kind in dict.fromkeys(locations):
        try:
            _collect_crash_directory(directory, kind, result)
        except FileNotFoundError:
            continue
        except OSError as error:
            result["errors"].append(f"{directory.name}: {type(error).__name__}")
    result["status"] = "available" if result["reports"] or result["dumps"] else "no_evidence_found"
    return sanitize_support_value(result)


def _most_recent_log(candidates: Sequence[Path]) -> Path | None:
    useful = []
    for index in range(0, len(candidates), 2):
        current = candidates[index]
        previous = candidates[index + 1] if index + 1 < len(candidates) else None
        current_stat = _nonempty_log_stat(current)
        previous_stat = _nonempty_log_stat(previous) if previous is not None else None
        if current_stat is not None and (previous_stat is None or current_stat.st_mtime_ns >= previous_stat.st_mtime_ns):
            useful.append((current_stat.st_mtime_ns, 1, current))
        elif previous_stat is not None:
            useful.append((previous_stat.st_mtime_ns, 0, previous))
    return sorted(useful, reverse=True)[0][2] if useful else None


def _nonempty_log_stat(path):
    try:
        info = path.stat()
        if path.is_file() and info.st_size > 0:
            return info
    except OSError:
        return None
    return None


def _log_freshness(stat_result: os.stat_result, session_start: float | None=None) -> tuple[str, str]:
    now = time.time()
    iso_time = datetime.datetime.fromtimestamp(stat_result.st_mtime, tz=datetime.timezone.utc).isoformat()
    mtime = stat_result.st_mtime

    if session_start is not None:
        if mtime >= (session_start - 10.0):
            return "active_session", iso_time
        age_before_session = max(0.0, session_start - mtime)
        if age_before_session < 86400 * 2:
            return "recent_previous", iso_time
        age_days = int(age_before_session // 86400)
        return 'historical_stale ({value_0} days old)'.format(value_0=f'{age_days}'), iso_time

    age_seconds = max(0.0, now - mtime)
    if age_seconds < 300:
        return "active_session", iso_time
    if age_seconds < 86400 * 2:
        return "recent_previous", iso_time
    age_days = int(age_seconds // 86400)
    return 'historical_stale ({value_0} days old)'.format(value_0=f'{age_days}'), iso_time


def _collect_log_tail(paired, role, archive_name, provenance, session_start, tails):
    selected: Path | None = None
    attempted: list[str] = []
    for pair in paired:
        if role >= len(pair):
            return
        candidate = pair[role]
        attempted.append(_safe_path(candidate))
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                selected = candidate
                break
        except OSError:
            return
    if selected is None:
        provenance[archive_name] = {'status': 'unavailable', 'reason': 'not_found', 'candidate_paths': attempted}
        return
    try:
        stat_res = selected.stat()
        freshness, iso_time = _log_freshness(stat_res, session_start=session_start)
        provenance[archive_name] = {
            'status': 'available', 'source_path': _safe_path(selected), 'size_bytes': stat_res.st_size,
            'modified_iso': iso_time, 'freshness': freshness,
        }
    except OSError as error:
        provenance[archive_name] = {
            'status': 'unavailable', 'reason': f'stat_failed: {type(error).__name__}',
            'source_path': _safe_path(selected),
        }
        return
    content = _read_support_log(selected)
    if content is None:
        provenance[archive_name] = {**provenance[archive_name], 'status': 'unavailable', 'reason': 'unreadable'}
    else:
        tails[archive_name] = content


def _support_log_tails(
    config: Mapping[str, object] | None,
    paths: object | None,
    application_dir: Path | None = None,
    session_start: float | None = None,
) -> tuple[dict[str, str], dict[str, dict[str, object]]]:
    tails: dict[str, str] = {}
    provenance: dict[str, dict[str, object]] = {}
    for family, candidates in (
        ('launcher', _launcher_log_candidates(config, paths, application_dir)),
        ('bridge', _bridge_log_candidates(config, paths, application_dir)), ('native', _native_log_candidates(config)),
    ):
        names = {
            'launcher': ('launcher.log', 'launcher.previous.log'), 'bridge': ('bridge.log', 'bridge.previous.log'),
            'native': ('ap_client.log', 'ap_client.previous.log'),
        }[family]
        paired = tuple(candidates[index:index + 2] for index in range(0, len(candidates), 2))
        for role, archive_name in enumerate(names):
            _collect_log_tail(paired, role, archive_name, provenance, session_start, tails)
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
        'exists': True, 'is_file': is_file, 'size_bytes': stat_result.st_size, 'mtime_ns': stat_result.st_mtime_ns,
        'modified_iso': datetime.datetime.fromtimestamp(stat_result.st_mtime, tz=datetime.timezone.utc).isoformat(),
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
            'path': str(root), 'exists': False, 'error': f'marker scan failed: {type(error).__name__}: {error}'[:256],
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


def fields(value, allowed):
    if not isinstance(value, dict):
        return {}
    return {
        key: scalar(value[key])
        for key in allowed
        if key in value and type(value[key]) in {str, int, float, bool, type(None)}
    }


def scalar(value):
    if isinstance(value, str):
        return value[:512]
    return None if isinstance(value, float) and not math.isfinite(value) else value


def installation_evidence(summary):
    result = fields(
        summary,
        (
            'status', 'schema', 'transaction_id', 'revision', 'phase', 'created_at', 'updated_at', 'resources_verified',
            'session_pending',
        ),
    )
    result["failure"] = fields(summary.get("failure"), ("phase", "code", "failure_domain", "message"))
    evidence = summary.get("evidence", {})
    result["evidence"] = fields(
        evidence, ('injector_pid', 'injector_finished', 'injector_state', 'recovery_error', 'cleanup_error'),
    )
    outcome = evidence.get("outcome")
    if isinstance(outcome, dict):
        adapter = outcome.get("adapter", {})
        result["evidence"]["outcome"] = fields(adapter, ("state", "returncode", "message"))
        result["evidence"]["outcome"]["details"] = fields(
            adapter.get('details'),
            ('failure_step', 'exception_type', 'verification_error', 'recovery_error', 'rollback_error', 'traceback'),
        )
    times = fields(
        summary.get("phase_times"),
        (
            'PREPARING', 'PREPARED', 'INJECTING', 'AWAITING_CONFIRMATION', 'CONFIRMED', 'FINALIZING_RESOURCES',
            'VERIFYING', 'PREPARING_SESSION', 'COMPLETE', 'ROLLING_BACK', 'ROLLED_BACK', 'RECOVERY_REQUIRED',
        ),
    )
    times = {phase: value for phase, value in times.items() if type(value) in {int, float}}
    result["phase_times"] = times
    ordered = sorted(times.items(), key=lambda pair: pair[1])
    result["timings"] = [
        {"phase": phase, "started_at": start, "elapsed_seconds": max(0, end - start)}
        for (phase, start), (_, end) in itertools.pairwise(ordered)
    ]
    return result


def checkpoint_evidence(session):
    result = fields(
        session,
        (
            'checkpoint', 'checkpoint_state', 'recovery_state', 'resume_allowed', 'resume_verified',
            'native_generation', 'save_generation', 'checkpoint_generation', 'previous_checkpoint_generation',
        ),
    )
    admission = session.get("admission", {})
    if isinstance(admission, dict):
        result["admission"] = fields(
            admission, ('checkpoint', 'native_generation', 'save_generation', 'checkpoint_generation'),
        )
    return result


async def _timeline(path, room):
    ledger = await Ledger.open(path.resolve(), read_only=True)
    try:
        rows = await ledger.timeline(room, 129)
        return {"status": "available", "limit": 128, "truncated": len(rows) > 128, "events": rows[-128:]}
    finally:
        await ledger.close()


def ledger_timeline(metadata):
    if not metadata.get("room") or not metadata.get("path"):
        return {"status": "inactive"}
    try:
        path = Path(metadata["path"])
        if not path.is_file():
            return {"status": "missing"}
        return asyncio.run(_timeline(path, RoomKey(**metadata["room"])))
    except (LedgerError, OSError, TypeError, ValueError) as error:
        return {"status": "unavailable", "reason": type(error).__name__}


def collect(support):
    link = support.get("game_link", {})
    session = support.get("ap_session") or link.get("native_rpc", {}).get("session", {})
    checkpoint = checkpoint_evidence(session)
    checkpoint["native"] = native_generations(support.get("native_diagnostics", {}).get("records", ()))
    return {
        'installation_journal': installation_evidence(support.get('installation', {})),
        'checkpoint_generations': checkpoint,
    }


def native_generations(records):
    generations = []
    for record in records[-64:]:
        correlation = record.get("correlation", {})
        if correlation.get("build_matches") is not True or correlation.get("namespace_matches") is not True:
            continue
        row = fields(
            record.get('campaign'),
            ('checkpoint', 'generation_before', 'generation_after', 'checkpoint_generation', 'checkpoint_at_ms'),
        )
        if row and (not generations or row != generations[-1]):
            generations.append(row)
    return generations[-16:]


def safe_timeline(value):
    result = fields(value, ("status", "limit", "truncated", "reason"))
    events = value.get("events", ())
    result["events"] = [
        fields(row, ("sequence", "kind", "receipt_index", "command_id", "previous_state", "new_state", "wall_time"))
        for row in events[-128:]
    ]
    return result


SUPPORT_SPAWN_DIAGNOSTIC_MAX_BYTES = 16 * 1024 * 1024
SUPPORT_BUNDLE_MAX_BYTES = 10 * 1024 * 1024


def _summary_warnings(members, lines):
    warnings = []
    for name, content in members.items():
        if not name.endswith((".log", ".jsonl", ".jsonl.previous")):
            continue
        for line in content.decode("utf-8", errors="replace").splitlines():
            if re.search(r"(?:^|[\W_])(warn(?:ing)?|error|failed|failure)(?:$|[\W_])", line, re.I):
                stamp = re.search(r"\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?", line)
                warnings.append((stamp.group() if stamp else 'timestamp unknown', name, line))
    warnings.sort(key=lambda item: "" if item[0] == "timestamp unknown" else item[0])
    lines.append('Last warnings/errors (up to 5):')
    lines.extend(f"  {stamp} {name}: {line[:180]}" for stamp, name, line in warnings[-5:])
    if not warnings:
        lines.append('  none recorded')


def _summary_connection_state(receipt, health, native, session):
    staged = receipt.get("hash_match")
    if staged is None and receipt.get("staged_exists") is False:
        staged = False
    game_connected = health.get("ready") if native.get("evidence") == "direct" else None
    ap_connected = session.get("connected") if session.get("identity_source") == "connected" else None
    if session.get("identity_source") == "last_known" and session.get("connected") is False:
        ap_connected = False
    return staged, game_connected, ap_connected


def _summary_verdict_state(payload, install, receipt, health, native, session, delivery_state):
    from .launcher_doctor import _delivery_verdict

    checks = {item["key"]: item for item in payload["diagnostics"]}
    game = install["game_found"]
    foreign_xinput = any(item["name"].casefold() == "xinput1_3.dll" and item["status"] == "available" for item in install["dlls"])
    core = install["ownership"]["status"] == "owned" and install["compatibility"]["status"] == "compatible" and not foreign_xinput
    core_known = install["manifest"].get("status") in {"available", "missing"}
    staged, game_connected, ap_connected = _summary_connection_state(receipt, health, native, session)
    delivery = _delivery_verdict(delivery_state)
    return checks, game, foreign_xinput, core, core_known, staged, game_connected, ap_connected, delivery


def _summary_status_lines(game, checks, core, core_known, install, foreign_xinput, staged, receipt, game_connected, health, ap_connected, session, delivery, delivery_state, lines):
    for name, value, detail in (
        (player_text('launcher.play', 'game_found'), game, checks.get("game", {}).get("message", "")),
        ('Core installed and compatible', core if core_known else None,
         'ownership={value_0}, compatibility={value_1}, XINPUT present={value_2}'.format(value_0=f"{install['ownership']['status']}", value_1=f"{install['compatibility']['status']}", value_2=f'{foreign_xinput}')),
        ('Room mod staged', staged, 'receipt unavailable' if receipt.get("status") != "available" else 'installation receipt'),
        (player_text('launcher.support', 'game_connection'), game_connected, str(health.get("reason", 'no current evidence'))),
        ('AP connection', ap_connected, str(session.get("identity_source", "unavailable"))),
        ('Item delivery', delivery, 'ledger={value_0}, counts={value_1}, truncated={value_2}'.format(value_0=f"{delivery_state.get('status', 'unavailable')}", value_1=f"{delivery_state.get('counts', {})}", value_2=f"{delivery_state.get('truncated', False)}")),
    ):
        verdict = 'PASS' if value is True else 'FAIL' if value is False else 'UNKNOWN'
        lines.append(f"{verdict} {name}: {detail}".rstrip(": "))


def _support_summary(payload, install, members, notes):
    from .launcher_doctor import _setup_failure_summary

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
    lines = ['DoomEAP support report', 'UTC: {value_0}'.format(value_0=f"{payload['created_utc']}"), 'Report ID: {value_0}'.format(value_0=f"{payload['report_id']}"),
             'Versions: Launcher {value_0} | APWorld {value_1} | Core {value_2}'.format(value_0=f"{payload['version']}", value_1=f'{apworld_version}', value_2=f'{core_version}'),
             'Game path type: {value_0}'.format(value_0=f"{install['path_type']}"),
             'Room seed: {value_0} | Slot: {value_1}'.format(value_0=f"{session.get('seed_name') or 'unknown'}", value_1=f"{session.get('slot') or 'unknown'}")]
    checks, game, foreign_xinput, core, core_known, staged, game_connected, ap_connected, delivery = _summary_verdict_state(payload, install, receipt, health, native, session, delivery_state)
    _summary_status_lines(game, checks, core, core_known, install, foreign_xinput, staged, receipt, game_connected, health, ap_connected, session, delivery, delivery_state, lines)
    lines.append(_setup_failure_summary(payload, support))
    _summary_warnings(members, lines)
    lines.append('ZIP files (uncompressed bytes):')
    lines.append('  summary.txt (this file)')
    lines.extend(f"  {name}: {len(content)}" for name, content in members.items())
    lines.append('Collection limits/omissions:')
    lines.extend(f"  {note}" for note in notes)
    if not notes:
        lines.append("  none")
    text = "\n".join(str(line).replace("\r", " ").replace("\n", " ")[:240] for line in lines) + "\n"
    if len(text.encode("utf-8")) > 64 * 1024:
        text = text.encode("utf-8")[:63 * 1024].decode("utf-8", errors="ignore") + '' + '\n' + 'Summary truncated at 63 KiB.' + '\n' + ''
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
    from .launcher_doctor import _file_identity

    game_root = (config or {}).get("game_root") or (config or {}).get("doom_base_dir")
    if game_root:
        root = Path(str(game_root)).expanduser()
        root = root.parent if root.name.casefold() == "base" else root
        return _file_identity(root / "DOOMEternalx64vk.exe", "DOOMEternalx64vk.exe")
    else:
        return {"status": "unavailable"}


def _support_export_destination(destination):
    if destination.exists():
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        candidate = destination.with_name(f"{destination.stem}_{stamp}{destination.suffix}")
        counter = 2
        while candidate.exists():
            candidate = destination.with_name(f"{destination.stem}_{stamp}_{counter}{destination.suffix}")
            counter += 1
        destination = candidate
    return destination


def _support_condump_content(support_condump):
    condump_content = None
    condump_metadata = dict(support_condump or {})
    support_path = condump_metadata.get("path")
    if isinstance(support_path, str | Path):
        try:
            source_path = Path(support_path)
            condump_content, retention = _encounter_console(source_path)
            condump_metadata.update({
                'source_filename': condump_metadata.get('source_filename', source_path.name), **retention,
            })
        except OSError as error:
            condump_metadata.update({'status': 'unavailable', 'reason': f'source_unreadable: {type(error).__name__}'})
    return condump_content, condump_metadata


def _support_history_notes(support, notes, native_startup, darklord):
    for issue in support.get("native_diagnostics", {}).get("issues", []):
        notes.append('Sentinel history {value_0}: unavailable ({value_1})'.format(value_0=f"{issue['file']}", value_1=f"{issue['error']}"))
    for issue in native_startup.get("errors", []):
        notes.append('Native startup {value_0}: skipped ({value_1})'.format(value_0=f"{issue['source_filename']}", value_1=f"{issue['reason']}"))
    for error in darklord.get("errors", []):
        notes.append('Dark Lord telemetry: {value_0}'.format(value_0=f'{error}'))


def _support_history_members(support, tails, notes, native_startup, darklord):
    history_files = support.get("native_diagnostics", {}).pop("files", [])
    for source in history_files:
        name = "sentinel/" + source["source_filename"]
        records = source.pop("records")
        if not records:
            notes.append('{value_0}: omitted, no matching records within source limits'.format(value_0=f'{name}'))
            continue
        tails[name] = (json.dumps(records[0]) if name.endswith(".json") and records else
                       "\n".join(json.dumps(record) for record in records)) + "\n"
        if source["truncated"]:
            notes.append('{value_0}: source tail/last 64 records retained; source {value_1} bytes'.format(value_0=f'{name}', value_1=f"{source['source_size']}"))
    if history_files:
        support["native_diagnostics"]["files"] = history_files
    _support_history_notes(support, notes, native_startup, darklord)


def _support_console_members(config, tails, notes, injector_output):
    base = doom_base_dir_from_config(config or {})
    if base is not None:
        console = base / "qconsole.log"
        try:
            tails["logs/qconsole.log"], retention = _encounter_console(console)
            if retention["omitted_bytes"]:
                notes.append('logs/qconsole.log: {value_0}; source {value_1} bytes'.format(value_0=f"{retention['truncation']}", value_1=f"{retention['source_size']}"))
        except OSError as error:
            notes.append('logs/qconsole.log: unavailable ({value_0})'.format(value_0=f'{type(error).__name__}'))
    else:
        notes.append('logs/qconsole.log: game directory unavailable')
    if injector_output is not None:
        tails["install/injector.log"] = _sanitize_support_text(injector_output)
        notes.append('install/injector.log: full stdout and stderr of the most recent run')
    else:
        notes.append('install/injector.log: no run retained in this launcher session')


def _support_log_notes(provenance, notes):
    for name, details in provenance.items():
        if details.get("status") == "unavailable":
            notes.append('{value_0}: unavailable ({value_1})'.format(value_0=f'{name}', value_1=f"{details.get('reason', 'unknown')}"))
        elif details.get("size_bytes", 0) > SUPPORT_LOG_MAX_BYTES:
            notes.append('{value_0}: bounded log excerpt; source {value_1} bytes'.format(value_0=f'{name}', value_1=f"{details['size_bytes']}"))


def _support_darklord_notes(darklord, notes):
    for record in darklord["records"] if "records" in darklord else ():
        if record["truncated"]:
            notes.append('{value_0}: last 1 MiB retained; source {value_1} bytes'.format(value_0=f"{record['archive_member']}", value_1=f"{record['source_size']}"))


def _support_install_notes(install, notes):
    notes.extend(install["issues"])
    notes.extend('install.json {value_0}: {value_1} ({value_2})'.format(value_0=f"{item['name']}", value_1=f"{item['status']}", value_2=f"{item.get('reason', 'unavailable')}")
                 for item in install["dlls"] + install["mods"] if item["status"] in {"omitted", "unreadable"})
    if install["manifest"].get("status") != "available":
        notes.append('install.json manifest: {value_0}'.format(value_0=f"{install['manifest'].get('reason') or install['manifest'].get('status', 'unavailable')}"))


def _bounded_support_member(name, data, limit, notes, payload):
    if len(data) > limit:
        if name.endswith(".log") and limit > 0:
            notes.append('{value_0}: truncated from {value_1} to at most {value_2} bytes'.format(value_0=f'{name}', value_1=f'{len(data)}', value_2=f'{limit}'))
            data = data[-limit:].decode("utf-8", errors="ignore").encode("utf-8")
        else:
            notes.append('{value_0}: omitted ({value_1} bytes, 10 MiB bundle budget)'.format(value_0=f'{name}', value_1=f'{len(data)}'))
            if name in {"doctor.json", "install.json"}:
                document = {"status": "omitted", "reason": '10 MiB bundle budget'}
                if name == "doctor.json":
                    document.update(report_id=payload["report_id"], version=payload["version"])
                data = json.dumps(document).encode("utf-8")
            else:
                return None
    return data


def _budget_support_members(members, notes, payload):
    retained = {}
    # reserve space for the summary and ZIP headers, even with incompressible logs
    remaining = SUPPORT_BUNDLE_MAX_BYTES - 128 * 1024
    for name, content in members.items():
        data = content.encode("utf-8")
        limit = min(remaining, SUPPORT_LOG_MAX_BYTES if name == "logs/qconsole.log" else remaining)
        data = _bounded_support_member(name, data, limit, notes, payload)
        if data is None:
            continue
        retained[name] = data
        remaining -= len(data)
    return retained


def _export_support_archive(destination, local_archive):
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
                raise OSError('Support export verification failed')
        return destination
    except (OSError, zipfile.BadZipFile) as error:
        if export_owned:
            try:
                destination.unlink()
            except OSError:
                pass
        logging.getLogger(__name__).warning(
            'Support export unavailable (%s); verified archive retained at %s', type(error).__name__, local_archive,
        )
        return local_archive


def _publish_support_archive(archive_directory, destination, summary, retained):
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
            raise OSError('Support archive verification failed: {value_0}'.format(value_0=f'{temporary}'))
    publish_file(temporary, local_archive, operation="support_bundle_publish")
    with zipfile.ZipFile(local_archive) as archive:
        if archive.testzip() is not None:
            raise OSError('Support archive verification failed: {value_0}'.format(value_0=f'{local_archive}'))
    return _export_support_archive(destination, local_archive)


def _support_failure_evidence(last_setup_failure, last_connection_error, payload):
    if last_setup_failure is not None:
        payload["last_setup_failure"] = sanitize_support_value(dict(last_setup_failure))
    if last_connection_error is not None:
        payload["last_connection_error"] = sanitize_support_value(dict(last_connection_error))


def _support_text_members(tails, members, safe_logs, condump_content):
    if "launcher.log" not in tails:
        members["launcher.log"] = safe_logs + ("\n" if safe_logs else "")
    elif safe_logs:
        tails["launcher.log"] += "\n" + safe_logs + "\n"
    for name in sorted(tails, key=lambda name: not name.startswith(("sentinel/", "install/", "logs/"))):
        members[name] = tails[name]
    if condump_content is not None:
        members["AP_SUPPORT_FILE.txt"] = condump_content


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
    from .launcher_doctor import _darklord_diagnostics, _install_diagnostics, _native_startup_diagnostics
    destination = destination.expanduser().absolute()
    destination = _support_export_destination(destination)
    tails, provenance = _support_log_tails(config, paths, application_dir, session_start=session_start)
    payload = sanitize_support_bundle(report.document())
    payload["created_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    payload["report_id"] = str(uuid.uuid4())
    payload["system"] = {
        'os': platform.system(), 'os_release': platform.release(), 'os_version': platform.version(),
        'architecture': platform.machine(),
    }
    payload["log_provenance"] = sanitize_support_value(provenance)
    _support_failure_evidence(last_setup_failure, last_connection_error, payload)
    condump_content, condump_metadata = _support_condump_content(support_condump)
    if support_condump is not None:
        payload["support_condump"] = sanitize_support_value(condump_metadata)
    if support_diagnostics is not None:
        payload["support_diagnostics"] = sanitize_support_value(dict(support_diagnostics))
    support = payload.get("support_diagnostics", {})

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
    safe_logs = _bound_support_text('\n'.join(_sanitize_support_text(str(line)) for line in logs))
    if safe_logs:
        provenance.setdefault("launcher.log", {})["in_memory_diagnostics"] = {
            'status': 'available', 'source': 'launcher_in_memory_diagnostics',
            'line_count': len(safe_logs.splitlines()),
        }
        payload["log_provenance"] = sanitize_support_value(provenance)
    notes = []
    if "[... HEAD+TAIL BOUNDARY ...]" in safe_logs:
        notes.append('In-memory launcher log: head/tail retained, middle omitted')
    _support_history_members(support, tails, notes, native_startup, darklord)
    _support_console_members(config, tails, notes, injector_output)
    _support_log_notes(provenance, notes)
    _support_darklord_notes(darklord, notes)
    if condump_metadata.get("omitted_bytes"):
        notes.append('AP_SUPPORT_FILE.txt: {value_0}; {value_1} bytes omitted'.format(value_0=f"{condump_metadata['truncation']}", value_1=f"{condump_metadata['omitted_bytes']}"))
    _support_install_notes(install, notes)
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
    _support_text_members(tails, members, safe_logs, condump_content)
    retained = _budget_support_members(members, notes, payload)
    summary = _support_summary(payload, install, retained, notes)
    return _publish_support_archive(archive_directory, destination, summary, retained)


if TYPE_CHECKING:
    from .launcher_doctor import DoctorReport
