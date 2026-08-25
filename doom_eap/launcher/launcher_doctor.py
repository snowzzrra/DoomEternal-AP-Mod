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
from dataclasses import asdict, dataclass
from pathlib import Path

from .launcher_native_health import NativeHealthReader, doom_base_dir_from_config
from .launcher_integration import UNINSTALL_OWNED_STATES
from .launcher_platform import (
    PrerequisiteStatus,
    detect_doom_processes,
    probe_meathook,
    redact_secrets,
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

    @property
    def ok(self) -> bool:
        return all(item.status not in {"error", "invalid", "missing", "incompatible", "failed", "attention"} for item in self.diagnostics)

    def document(self) -> dict[str, object]:
        return {"version": self.version, "ok": self.ok, "diagnostics": [asdict(item) for item in self.diagnostics]}


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
    for archive_name, candidates in (
        ("bridge.log", _bridge_log_candidates(config, paths, application_dir)),
        ("ap_client.log", _native_log_candidates(config)),
    ):
        selected = _most_recent_log(candidates)
        if selected is None:
            continue
        try:
            stat_res = selected.stat()
            freshness, iso_time = _log_freshness(stat_res, session_start=session_start)
            provenance[archive_name] = {
                "source_path": _safe_path(selected),
                "size_bytes": stat_res.st_size,
                "modified_iso": iso_time,
                "freshness": freshness,
            }
        except OSError:
            pass
        tail = _read_support_log(selected)
        if tail is not None:
            tails[archive_name] = tail
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
    safe_logs = _bound_support_text(
        "\n".join(redact_secrets(_safe_path(str(line))) for line in logs)
    )
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("doctor.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
        archive.writestr("launcher.log", safe_logs + ("\n" if safe_logs else ""))
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
    VERSION = "beta.4"

    def __init__(
        self,
        *,
        config: Mapping[str, object] | None = None,
        paths: object | None = None,
        config_path: Path | None = None,
    ):
        self.config = dict(config or {})
        self.paths = paths
        self.config_path = config_path

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
            if action.action_id in {"archive_stale_install_record", "reinstall_room_mod"}
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
        return DoctorReport(self.VERSION, tuple(checks))
