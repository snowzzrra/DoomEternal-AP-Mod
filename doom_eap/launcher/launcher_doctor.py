"""Bounded launcher diagnostics and sanitized support bundle generation."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import time
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .launcher_native_health import NativeHealthReader, doom_base_dir_from_config
from .launcher_integration import UNINSTALL_OWNED_STATES
from .launcher_platform import (
    AMMO_HOTKEY_STATE_FILENAME,
    AMMO_REFILL_BIND_COMMAND,
    PrerequisiteStatus,
    detect_doom_processes,
    probe_meathook,
    redact_secrets,
    resolve_doom_config_path,
    validate_game_root,
)

SUPPORT_LOG_TAIL_BYTES = 256 * 1024
SUPPORT_LOG_MAX_BYTES = 1024 * 1024
SUPPORT_DIAGNOSTIC_MAX_BYTES = 256 * 1024
SUPPORT_DIAGNOSTIC_MAX_ITEMS = 24
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
        return all(item.status not in {"error", "invalid", "missing", "incompatible", "failed", "attention"} for item in self.diagnostics)

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
    return str(value)


def sanitize_support_value(value: object, *, key: str = "") -> object:
    lowered = key.casefold()
    if lowered in _CREDENTIAL_FIELDS:
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(name): sanitize_support_value(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_support_value(item, key=key) for item in value]
    if isinstance(value, Path) or (isinstance(value, str) and ("path" in lowered or "dir" in lowered or "root" in lowered)):
        return _safe_path(value)
    if isinstance(value, str):
        return redact_secrets(_safe_path(value))
    return value


def sanitize_support_bundle(document: Mapping[str, object]) -> dict[str, object]:
    return sanitize_support_value(dict(document))  # type: ignore[return-value]


def _configured_paths(value: object) -> tuple[Path, ...]:
    if isinstance(value, (str, Path)):
        values = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
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
        text = redact_secrets(_safe_path(payload.decode("utf-8", errors="replace")))
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


def _read_support_log(path: Path) -> str | None:
    """Keep complete bounded session logs, otherwise retain meaningful head and tail."""
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
                payload = head + b"\n\n[... HEAD+TAIL BOUNDARY ...]\n\n" + tail
        text = redact_secrets(_safe_path(payload.decode("utf-8", errors="replace")))
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
    return head + "\n\n[... HEAD+TAIL BOUNDARY ...]\n\n" + tail


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
        return f"historical_stale ({age_days} days old)", iso_time

    age_seconds = max(0.0, now - mtime)
    if age_seconds < 300:
        return "active_session", iso_time
    if age_seconds < 86400 * 2:
        return "recent_previous", iso_time
    age_days = int(age_seconds // 86400)
    return f"historical_stale ({age_days} days old)", iso_time


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
        for marker in ("AP_ACTIVE_MAP_V1", "AP_CHECK_EVENT_", "AP_TELEMETRY"):
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
            path.name.startswith(("ap_", "GAME-AUTOSAVE"))
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


def _saved_games_candidates(config: Mapping[str, object], root: Path | None) -> tuple[Path, ...]:
    candidates: list[Path] = []
    configured = config.get("save_games_dir")
    if configured:
        try:
            selected = Path(str(configured)).expanduser()
            candidates.extend((selected, selected.parent, selected.parent.parent))
        except (OSError, TypeError, ValueError):
            pass
    candidates.append(Path.home() / "Saved Games" / "id Software" / "DOOMEternal" / "base")
    if root is not None:
        for parent in (root, *root.parents):
            if parent.name.casefold() == "steamapps":
                candidates.append(
                    parent.parent / "steamapps/compatdata/782330/pfx/drive_c/users/steamuser/Saved Games/id Software/DOOMEternal/base"
                )
                break
    return tuple(dict.fromkeys(path.expanduser() for path in candidates))


def _saved_games_diagnostics(config: Mapping[str, object], root: Path | None, processes: Sequence[Mapping[str, object]]) -> dict[str, object]:
    selected_value = config.get("save_games_dir")
    selected_error: str | None = None
    try:
        selected = Path(str(selected_value)).expanduser() if selected_value else None
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        selected = None
        selected_error = f"configured Saved Games path unavailable: {type(error).__name__}: {error}"[:256]
    candidates = []
    for path in _saved_games_candidates(config, root)[:SUPPORT_DIAGNOSTIC_MAX_ITEMS]:
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
            "relationship": "configured" if selected is not None and path == selected else "discovered_candidate",
            "markers": {
                "ap_active_map": _marker_files(path, "ap_active_map") if is_dir else [{"path": str(path), "exists": False, "error": "candidate is not an accessible directory"}],
                "ap_telemetry": _marker_files(path, "ap_telemetry") if is_dir else [{"path": str(path), "exists": False, "error": "candidate is not an accessible directory"}],
                "ap_event": _marker_files(path, "ap_event") if is_dir else [{"path": str(path), "exists": False, "error": "candidate is not an accessible directory"}],
            },
            "recent_writes": recent,
            "live_game_detected": game_detected,
            "live_game_writing": bool(recent) and game_detected,
            "live_game_writing_reason": (
                "recent AP/save write while DOOM process detected"
                if recent and game_detected else
                "no recent relevant write observed" if game_detected else
                "DOOM process not detected"
            ),
        })
    source = "configured_save_games_dir" if selected_value else "unconfigured"
    try:
        selected_exists = selected is not None and selected.is_dir()
    except (OSError, ValueError, RuntimeError):
        selected_exists = False
    if selected_value and selected is not None and selected_exists:
        reason = "launcher configuration value; selected directory exists"
    elif selected_value:
        reason = "launcher configuration value; selected directory is missing"
    else:
        reason = "no effective Saved Games path"
    result: dict[str, object] = {
        "effective_path": str(selected) if selected else None,
        "source": source,
        "reason": reason,
        "candidates": candidates,
    }
    if selected_error:
        result["selection_error"] = selected_error
    return result


def _queue_diagnostics(base: Path | None) -> dict[str, object]:
    queue_path = base / "ap_queue" if base is not None else None
    result: dict[str, object] = {"path": str(queue_path) if queue_path else None, "pending": 0, "processing": 0, "failed": 0, "items": {}}
    try:
        queue_available = queue_path is not None and queue_path.is_dir()
    except (OSError, ValueError, RuntimeError) as error:
        result["error"] = f"queue path unavailable: {type(error).__name__}: {error}"[:256]
        return result
    if queue_path is None or not queue_available:
        return result
    items: dict[str, list[str]] = {"pending": [], "processing": [], "failed": []}
    errors: list[str] = []
    for suffix, key in ((".cmd", "pending"), (".processing", "processing"), (".failed", "failed")):
        try:
            paths = sorted(queue_path.glob(f"*{suffix}"), key=lambda item: item.name)
        except (OSError, ValueError, RuntimeError) as error:
            errors.append(f"{suffix} scan failed: {type(error).__name__}: {error}"[:256])
            continue
        for path in paths:
            current = result.get(key, 0)
            result[key] = (current if isinstance(current, int) else 0) + 1
            if len(items[key]) < SUPPORT_DIAGNOSTIC_MAX_ITEMS:
                items[key].append(path.stem)
    result["items"] = items
    if errors:
        result["errors"] = errors
    return result


def _runtime_diagnostics(config: Mapping[str, object], config_path: Path | None, paths: object | None, processes: Sequence[Mapping[str, object]]) -> dict[str, object]:
    base_error: str | None = None
    try:
        base = doom_base_dir_from_config(config)
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        base = None
        base_error = f"base path unavailable: {type(error).__name__}: {error}"[:256]
    save = config.get("save_games_dir")
    details = {
        "config_file": str(config_path) if config_path else None,
        "doom_base_dir": str(base) if base else None,
        "save_games_dir": str(save) if save else None,
        "INV_DUMP_DIR": str(save) if save else None,
        "STEAM_REMOTE_DIR": str(config.get("steam_remote_dir")) if config.get("steam_remote_dir") else None,
        "STEAM_ID3": config.get("steam_id3"),
        "path_selection": _saved_games_diagnostics(config, base, processes),
        "queue": _queue_diagnostics(base),
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
            "active_materialization_lease": base / "ap_queue/active_materialization_lease",
            "active_session_namespace": base / "ap_queue/active_session_namespace",
            "ap_rpc_enabled": base / "ap_rpc_enabled",
            "ap_gameplay_save.state": base / "ap_gameplay_save.state",
        }
        details["materialization"] = {name: _mtime_document(path) for name, path in materialization.items()}
    try:
        health_path = base / "ap_rpc_health.state" if base else Path()
        details["native"] = NativeHealthReader(health_path).read(force=True).document() if base else {"state": "not_ready", "reason": "base_directory_unconfigured"}
    except Exception as error:
        details["native"] = {"state": "unavailable", "reason": str(error)}
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


def _item_state_summary(
    config: Mapping[str, object], config_path: Path | None = None
) -> dict[str, object]:
    """Read bounded ownership counters; never copy persistent state wholesale."""
    path = _client_state_path(config, config_path)
    result: dict[str, object] = {
        "status": "unavailable",
        "source": "bridge_client_persistent_state",
        "path": str(path),
        "sessions": [],
        "session_count": 0,
    }
    try:
        if not path.is_file():
            result["reason"] = "not_found"
            return result
        if path.stat().st_size > SUPPORT_DIAGNOSTIC_MAX_BYTES:
            result["reason"] = "bounded_read_limit_exceeded"
            return result
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        result["reason"] = f"unreadable: {type(error).__name__}"
        return result
    if not isinstance(document, dict) or not isinstance(document.get("sessions"), Mapping):
        result["reason"] = "malformed"
        return result

    sessions = document["sessions"]
    summaries: list[dict[str, object]] = []
    for session_index, raw_key in enumerate(sorted(sessions, key=str)[:SUPPORT_DIAGNOSTIC_MAX_ITEMS]):
        session = sessions[raw_key]
        if not isinstance(session, Mapping):
            continue
        history = session.get("receipt_history")
        history = history if isinstance(history, Mapping) else {}
        counts = history.get("receipt_counts")
        counts = counts if isinstance(counts, Mapping) else {}
        owned = history.get("owned_item_ids")
        owned_count = len(owned) if isinstance(owned, list) else 0
        processed = session.get("processed_items", 0)
        highest = history.get("highest_observed_index", -1)
        summaries.append({
            "session_index": session_index,
            "processed_items": processed if isinstance(processed, int) and not isinstance(processed, bool) and processed >= 0 else 0,
            "highest_observed_index": highest if isinstance(highest, int) and not isinstance(highest, bool) else -1,
            "receipt_id_count": len(counts),
            "receipt_count": sum(
                value for value in counts.values()
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            ),
            "owned_item_count": owned_count,
            "item_mapping_revision": session.get("item_mapping_revision", 0)
            if isinstance(session.get("item_mapping_revision", 0), int)
            and not isinstance(session.get("item_mapping_revision", 0), bool)
            else 0,
            "item_resync": isinstance(session.get("item_resync"), Mapping),
        })
    result.update({
        "status": "available",
        "version": document.get("version") if isinstance(document.get("version"), int) else None,
        "session_count": len(sessions),
        "sessions": summaries,
        "truncated": len(sessions) > len(summaries),
    })
    return result


def _room_install_receipt_summary(state_dir: Path | None) -> dict[str, object]:
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
        "schema", "manifest_hash", "staged_mod", "staged_sha256",
        "adapter_state", "installation_mode", "steam_launch_option",
    ):
        if key in document:
            result[key] = document[key]
    staged = document.get("staged_mod")
    expected = document.get("staged_sha256")
    if isinstance(staged, str) and staged and isinstance(expected, str) and expected:
        staged_path = Path(staged)
        result["staged_exists"] = staged_path.is_file()
        if staged_path.is_file():
            try:
                actual = hashlib.sha256(staged_path.read_bytes()).hexdigest()
                result["staged_sha256_actual"] = actual
                result["hash_match"] = actual == expected
            except OSError:
                result["hash_match"] = False
    result["status"] = "available"
    return result


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
    queue_details = runtime.get("queue")
    queue_details = queue_details if isinstance(queue_details, Mapping) else {}
    config_paths: dict[str, object] = {
        "config_file": str(config_path) if config_path else None,
        "application_dir": str(getattr(paths, "application_dir", "")) if paths else None,
        "client_state_file": str(_client_state_path(config, config_path)),
        "doom_base_dir": runtime.get("doom_base_dir"),
        "save_games_dir": runtime.get("save_games_dir"),
        "queue_dir": queue_details.get("path"),
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
    materialization = runtime.get("materialization")
    materialization = materialization if isinstance(materialization, Mapping) else {}
    gate = materialization.get("ap_rpc_enabled")
    gate_enabled = isinstance(gate, Mapping) and gate.get("exists") is True
    safety = {
        "status": "enabled" if gate_enabled else "blocked",
        "rpc_gate": gate,
        "reason": "rpc_gate_present" if gate_enabled else "rpc_gate_unavailable",
    }
    telemetry_safety = {
        "status": "ready" if telemetry["status"] == "available" and safety["status"] == "enabled" else "degraded",
        "telemetry": telemetry,
        "safety": safety,
    }
    queue = runtime.get("queue")
    queue = dict(queue) if isinstance(queue, Mapping) else {"status": "unavailable", "reason": "queue_diagnostics_unavailable"}
    queue_path = queue.get("path")
    try:
        queue["status"] = (
            "available"
            if isinstance(queue_path, str) and Path(queue_path).is_dir() and not queue.get("errors")
            else "unavailable"
        )
    except (OSError, ValueError, RuntimeError):
        queue["status"] = "unavailable"

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
    if queue.get("failed", 0) or queue.get("errors"):
        issues.append("queue_has_failed_work")
    if game_running and queue.get("status") == "unavailable":
        issues.append("queue_evidence_unavailable")
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
    parsed_key = "UNBOUND"
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
        "queue": queue,
        "ammo_hotkey": ammo_hotkey,
        "overall": {
            "status": overall_status,
            "reason": overall_reason,
            "issues": issues,
            "derived_from": [
                "process", "bridge", "native_rpc", "telemetry_safety", "queue", "ammo_hotkey",
            ],
        },
    }


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
    return {
        "item_state": _item_state_summary(config, config_path),
        "room_install_receipt": _room_install_receipt_summary(state_dir),
        "game_link": _support_game_link_diagnostics(
            config, config_path, paths, processes, runtime, meathook, live=live
        ),
    }


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
    support_condump: Mapping[str, object] | None = None,
    support_diagnostics: Mapping[str, object] | None = None,
) -> Path:
    """Write bounded diagnostics and redacted logs with freshness metadata."""
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    tails, provenance = _support_log_tails(
        config, paths, application_dir, session_start=session_start
    )
    payload = sanitize_support_bundle(report.document())
    payload["log_provenance"] = sanitize_support_value(provenance)
    if last_setup_failure is not None:
        payload["last_setup_failure"] = sanitize_support_value(dict(last_setup_failure))
    if support_condump is not None:
        payload["support_condump"] = sanitize_support_value(dict(support_condump))
    if support_diagnostics is not None:
        payload["support_diagnostics"] = sanitize_support_value(dict(support_diagnostics))
    safe_logs = _bound_support_text(
        "\n".join(redact_secrets(_safe_path(str(line))) for line in logs)
    )
    if safe_logs:
        if "launcher.log" in tails:
            provenance["launcher.history.log"] = {
                "status": "available",
                "source": "launcher_in_memory_diagnostics",
                "line_count": len(safe_logs.splitlines()),
            }
        else:
            provenance.setdefault("launcher.log", {})[
                "in_memory_diagnostics"
            ] = {
                "status": "available",
                "source": "launcher_in_memory_diagnostics",
                "line_count": len(safe_logs.splitlines()),
            }
        payload["log_provenance"] = sanitize_support_value(provenance)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("doctor.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
        if "launcher.log" not in tails:
            archive.writestr("launcher.log", safe_logs + ("\n" if safe_logs else ""))
        elif safe_logs:
            archive.writestr("launcher.history.log", safe_logs + "\n")
        for name, tail in tails.items():
            archive.writestr(name, tail)
        if support_condump is not None:
            support_path = support_condump.get("path")
            if isinstance(support_path, (str, Path)):
                try:
                    content = Path(support_path).read_bytes()[:SUPPORT_DIAGNOSTIC_MAX_BYTES]
                except OSError:
                    content = None
                if content is not None:
                    archive.writestr("AP_SUPPORT_FILE.txt", redact_secrets(content.decode("utf-8", errors="replace")))
    os.replace(temporary, destination)
    return destination


class LauncherDoctor:
    VERSION = "0.5.0"

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

    def repair_actions(self) -> tuple[RepairAction, ...]:
        """Offer actions only when launcher receipt proves ownership."""
        actions: list[RepairAction] = []
        game_root = self.config.get("game_root") or self.config.get("doom_base_dir")
        if game_root:
            try:
                root = validate_game_root(Path(str(game_root)))
                meathook_probe = probe_meathook(root)
                if meathook_probe.status == PrerequisiteStatus.MISSING:
                    actions.append(RepairAction(
                        "install_game_link", "Install verified Game Link runtime",
                        ("Download official Meathook v7.2 and install to DOOM Eternal folder",),
                        False,
                        "Downloads verified XINPUT1_3.dll from GitHub release.",
                    ))
                elif meathook_probe.status in {PrerequisiteStatus.INCOMPATIBLE, PrerequisiteStatus.INVALID}:
                    actions.append(RepairAction(
                        "repair_game_link", "Repair Game Link runtime",
                        (
                            "Back up existing XINPUT1_3.dll to repair-backups",
                            "Install verified Meathook v7.2 runtime library",
                        ),
                        True,
                        "Backs up foreign/unverified XINPUT1_3.dll before replacing.",
                    ))
            except Exception:
                pass

        state_dir = self._state_dir()
        issue = self._current_issue()
        if issue is not None:
            action_id = (
                "update_room_package"
                if issue.get("recovery_action") == "update_room_package"
                else "rebuild_room_package"
            )
            actions.append(RepairAction(
                action_id,
                "Update room package" if action_id == "update_room_package" else "Rebuild room package",
                ("Prepare and verify the current room package.",),
                True,
                "Keeps launcher-owned package verification and receipt checks in place.",
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
                raise ValueError("record must be an object")
            staged = Path(str(receipt["staged_mod"])).resolve()
            digest = str(receipt["staged_sha256"])
            adapter_state = str(receipt.get("adapter_state", ""))
            if root is not None:
                owned_location = staged.parent == root / "Mods"
                if not owned_location:
                    raise ValueError("recorded package is outside configured Mods folder")
            if adapter_state in UNINSTALL_OWNED_STATES:
                return tuple(actions)
            if not staged.is_file():
                actions.append(RepairAction(
                    "reinstall_room_mod", "Reinstall missing room mod",
                    (f"Create launcher-owned package: {staged.name}",), True,
                    "Installer keeps file backups and verifies installed hash.",
                ))
                return tuple(actions)
            actual = hashlib.sha256(staged.read_bytes()).hexdigest()
            if actual != digest:
                actions.append(RepairAction(
                    "reinstall_room_mod", "Reinstall changed room mod",
                    (f"Replace launcher-owned package: {staged.name}", f"SHA-256: {actual} → {digest}"), True,
                    "Installer keeps file backups and verifies installed hash.",
                ))
                return tuple(actions)
            if adapter_state != "applied":
                actions.append(RepairAction(
                    "reinstall_room_mod", "Apply room mod setup",
                    ("Run room mod setup and confirm installation.",), True,
                    "Applies room mod into game.",
                ))
                return tuple(actions)
        except (OSError, RuntimeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            if issue is not None:
                return tuple(actions)
            actions.append(RepairAction(
                "archive_stale_install_record", "Archive stale install record",
                (f"Move launcher record to repair backup: {receipt_path.name}",), False,
                f"Restore {receipt_path.name} from repair backup. ({error})",
            ))
            return tuple(actions)
        return tuple(actions)

    def repair_preview(self) -> tuple[RepairAction, ...]:
        return self.repair_actions()

    def archive_stale_install_record(self) -> Path:
        """Move only launcher state receipt into rollback storage."""
        state_dir = self._state_dir()
        if state_dir is None:
            raise ValueError("launcher state directory is unavailable")
        receipt = state_dir / "launcher_setup.json"
        if not receipt.is_file():
            raise ValueError("launcher install record is unavailable")
        backups = state_dir / "repair-backups"
        backups.mkdir(parents=True, exist_ok=True)
        backup = backups / "launcher_setup.json"
        if backup.exists():
            raise ValueError("repair backup already exists; restore or remove it first")
        os.replace(receipt, backup)
        return backup

    def restore_archived_install_record(self) -> Path:
        state_dir = self._state_dir()
        if state_dir is None:
            raise ValueError("launcher state directory is unavailable")
        backup = state_dir / "repair-backups" / "launcher_setup.json"
        receipt = state_dir / "launcher_setup.json"
        if not backup.is_file() or receipt.exists():
            raise ValueError("install record rollback is unavailable")
        os.replace(backup, receipt)
        return receipt

    def run(self) -> DoctorReport:
        checks: list[Diagnostic] = []
        platform_name = "windows" if os.name == "nt" else "linux"
        checks.append(Diagnostic("platform", "ok", platform_name))
        game_root = self.config.get("game_root") or self.config.get("doom_base_dir")
        root: Path | None = None
        if game_root:
            try:
                root = validate_game_root(Path(str(game_root)))
                checks.append(Diagnostic("game", "ok", "DOOM Eternal installation validated", {"path": str(root)}))
            except ValueError as error:
                checks.append(Diagnostic("game", "invalid", str(error)))
        else:
            game_discovery = self.config.get("game_discovery")
            details: dict[str, object] | None = None
            if isinstance(game_discovery, dict):
                details = {"discovery": game_discovery}
            checks.append(Diagnostic("game", "missing", "DOOM Eternal installation is not configured", details))

        meathook_probe = probe_meathook(root)
        checks.append(Diagnostic(
            "meathook",
            meathook_probe.status.value,
            meathook_probe.message,
            meathook_probe.details,
        ))

        processes = list(detect_doom_processes())
        checks.append(Diagnostic("processes", "ok", "process probe complete", {"items": processes}))
        checks.append(Diagnostic("config", "ok", "launcher configuration loaded", {"keys": sorted(self.config)}))
        checks.append(Diagnostic(
            "runtime_paths",
            "ok",
            "effective runtime paths and path-selection evidence collected",
            _runtime_diagnostics(self.config, self.config_path, self.paths, processes),
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

        state_dir = self._state_dir()
        receipt_path = (state_dir / "launcher_setup.json") if state_dir else None
        room_uninstall_state = ""
        if receipt_path and receipt_path.is_file():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                adapter_state = str(receipt.get("adapter_state", ""))
                room_uninstall_state = adapter_state if adapter_state in UNINSTALL_OWNED_STATES else ""
                mode = str(receipt.get("installation_mode", ""))
                if adapter_state == "applied":
                    checks.append(Diagnostic("mod_injection", "ok", "Mod installation applied successfully", {"adapter_state": adapter_state, "installation_mode": mode}))
                    checks.append(Diagnostic("windows_mod_installer", "ok", f"Windows mod installation verified ({mode or 'applied'})", {"installation_mode": mode, "adapter_state": adapter_state}))
                elif adapter_state in UNINSTALL_OWNED_STATES:
                    details = {"adapter_state": adapter_state, "installation_mode": mode}
                    if adapter_state == "uninstalled":
                        status = "not_applicable"
                        message = "Room mod is intentionally uninstalled"
                    else:
                        status = "attention"
                        message = "Room mod uninstall requires attention; automatic reinstall is disabled"
                    checks.append(Diagnostic("mod_injection", status, message, details))
                    checks.append(Diagnostic("windows_mod_installer", status, message, details))
                elif adapter_state == "manual_install_required":
                    checks.append(Diagnostic("mod_injection", "failed", "Windows mod installation requires manual setup in INSTALL.md", {"adapter_state": adapter_state, "installation_mode": mode}))
                    checks.append(Diagnostic("windows_mod_installer", "failed", "Windows mod installation requires manual setup in INSTALL.md", {"installation_mode": mode, "adapter_state": adapter_state}))
                else:
                    checks.append(Diagnostic("mod_injection", "failed", f"Mod installation has not been applied (state: {adapter_state or 'unknown'})", {"adapter_state": adapter_state, "installation_mode": mode}))
                    checks.append(Diagnostic("windows_mod_installer", "failed", f"Windows mod installer is not ready (state: {adapter_state or 'unknown'})", {"installation_mode": mode, "adapter_state": adapter_state}))
            except Exception:
                checks.append(Diagnostic("mod_injection", "invalid", "Could not parse launcher setup record"))
                checks.append(Diagnostic("windows_mod_installer", "invalid", "Could not parse launcher setup record"))
        else:
            checks.append(Diagnostic("mod_injection", "not_applicable", "No room package receipt is available"))
            checks.append(Diagnostic("windows_mod_installer", "not_applicable", "No room package receipt is available"))

        actions = self.repair_actions()
        room_actions = tuple(
            action for action in actions
            if action.action_id in {
                "archive_stale_install_record", "reinstall_room_mod",
                "rebuild_room_package", "update_room_package",
            }
        )
        if room_actions:
            checks.append(Diagnostic(
                "room_mod", "invalid", "room mod needs repair",
                {"actions": [asdict(action) for action in room_actions]},
            ))
        elif room_uninstall_state:
            checks.append(Diagnostic(
                "room_mod",
                "not_applicable" if room_uninstall_state == "uninstalled" else "attention",
                (
                    "Room package is intentionally uninstalled"
                    if room_uninstall_state == "uninstalled"
                    else "Room package uninstall requires attention; automatic reinstall is disabled"
                ),
                {"adapter_state": room_uninstall_state},
            ))
        else:
            checks.append(Diagnostic("room_mod", "not_applicable", "Room package verification is unavailable without a receipt"))
        issue = self._current_issue()
        if issue is not None:
            domain = str(issue.get("failure_domain", "room_package"))
            checks.append(Diagnostic(
                "current_room_package",
                "attention",
                str(issue.get("user_message") or issue.get("technical_message") or "Current room package needs attention"),
                dict(issue),
            ))
            summary_status = "room_package_needs_attention"
            summary_message = str(issue.get("user_message") or "Current room package needs attention")
            recovery_action = str(issue.get("recovery_action") or "rebuild_room_package")
        else:
            domain = ""
            recovery_action = ""
            summary_status = "ready" if all(
                item.status not in {"error", "invalid", "missing", "incompatible", "failed", "attention"}
                for item in checks
            ) else "needs_attention"
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
