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

from .launcher_platform import (
    PrerequisiteStatus,
    detect_doom_processes,
    probe_meathook,
    redact_secrets,
    validate_game_root,
)

SUPPORT_LOG_TAIL_BYTES = 256 * 1024


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


@dataclass(frozen=True)
class DoctorReport:
    version: str
    diagnostics: tuple[Diagnostic, ...]

    @property
    def ok(self) -> bool:
        return all(item.status not in {"error", "invalid", "missing", "incompatible", "failed"} for item in self.diagnostics)

    def document(self) -> dict[str, object]:
        return {"version": self.version, "ok": self.ok, "diagnostics": [asdict(item) for item in self.diagnostics]}


def _safe_path(value: object) -> str:
    text = str(value)
    home = str(Path.home())
    if home and text.startswith(home):
        return "<USER>" + text[len(home):]
    if os.name == "nt":
        text = re.sub(r"(?i)^[a-z]:\\Users\\[^\\]+", "<USER>", text)
    return re.sub(r"/(?:home|Users)/[^/]+", "/<USER>", text)


def sanitize_support_value(value: object, *, key: str = "") -> object:
    lowered = key.casefold()
    if any(token in lowered for token in ("password", "passwd", "token", "secret", "authorization")):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(name): sanitize_support_value(item, key=str(name)) for name, item in value.items() if "save" not in str(name).casefold()}
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
        tail = _read_log_tail(selected)
        if tail is not None:
            tails[archive_name] = tail
    return tails, provenance


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
    safe_logs = "\n".join(redact_secrets(_safe_path(str(line))) for line in logs)[-20000:]
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("doctor.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
        archive.writestr("launcher.log", safe_logs + ("\n" if safe_logs else ""))
        for name, tail in tails.items():
            archive.writestr(name, tail)
    os.replace(temporary, destination)
    return destination


class LauncherDoctor:
    VERSION = "beta.4"

    def __init__(self, *, config: Mapping[str, object] | None = None, paths: object | None = None):
        self.config = dict(config or {})
        self.paths = paths

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
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
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

        checks.append(Diagnostic("processes", "ok", "process probe complete", {"items": list(detect_doom_processes())}))
        checks.append(Diagnostic("config", "ok", "launcher configuration loaded", {"keys": sorted(self.config)}))

        state_dir = self._state_dir()
        receipt_path = (state_dir / "launcher_setup.json") if state_dir else None
        if receipt_path and receipt_path.is_file():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                adapter_state = str(receipt.get("adapter_state", ""))
                mode = str(receipt.get("installation_mode", ""))
                if adapter_state == "applied":
                    checks.append(Diagnostic("mod_injection", "ok", "Mod installation applied successfully", {"adapter_state": adapter_state, "installation_mode": mode}))
                    checks.append(Diagnostic("windows_mod_installer", "ok", f"Windows mod installation verified ({mode or 'applied'})", {"installation_mode": mode, "adapter_state": adapter_state}))
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
            checks.append(Diagnostic("mod_injection", "ok", "No active room installation record"))
            checks.append(Diagnostic("windows_mod_installer", "ok", "No active room installation record"))

        actions = self.repair_actions()
        if actions:
            checks.append(Diagnostic(
                "room_mod", "invalid", "room mod needs repair",
                {"actions": [asdict(action) for action in actions]},
            ))
        else:
            checks.append(Diagnostic("room_mod", "ok", "launcher-owned room mod verified"))
        return DoctorReport(self.VERSION, tuple(checks))
