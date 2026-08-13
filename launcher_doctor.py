"""Bounded launcher diagnostics and sanitized support bundle generation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from launcher_platform import (
    detect_doom_processes,
    redact_secrets,
    validate_game_root,
)


@dataclass(frozen=True)
class Diagnostic:
    key: str
    status: str
    message: str
    details: dict[str, object] | None = None


@dataclass(frozen=True)
class RepairAction:
    """Small, previewable repair limited to launcher-owned state."""

    key: str
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
        return all(item.status not in {"error", "invalid"} for item in self.diagnostics)

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


def write_support_bundle(destination: Path, report: DoctorReport, *, logs: Sequence[str] = ()) -> Path:
    """Write bounded diagnostics and redacted logs."""
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = sanitize_support_bundle(report.document())
    safe_logs = "\n".join(redact_secrets(_safe_path(str(line))) for line in logs)[-20000:]
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("doctor.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
        archive.writestr("launcher.log", safe_logs + ("\n" if safe_logs else ""))
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
        state_dir = self._state_dir()
        if state_dir is None:
            return ()
        receipt_path = state_dir / "launcher_setup.json"
        if not receipt_path.is_file():
            return (RepairAction(
                "reinstall_room_mod", "Install room mod",
                ("Build and install mod for connected room.",), True,
                "Installer keeps file backups and verifies installed hash.",
            ),)
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                raise ValueError("record must be an object")
            staged = Path(str(receipt["staged_mod"])).resolve()
            digest = str(receipt["staged_sha256"])
            game_root = self.config.get("game_root") or self.config.get("doom_base_dir")
            root = Path(str(game_root)).expanduser().resolve()
            if root.name.casefold() == "base":
                root = root.parent
            owned_location = staged.parent == root / "Mods"
            if not owned_location:
                raise ValueError("recorded package is outside configured Mods folder")
            if not staged.is_file():
                return (RepairAction(
                    "reinstall_room_mod", "Reinstall missing room mod",
                    (f"Create launcher-owned package: {staged.name}",), True,
                    "Installer keeps file backups and verifies installed hash.",
                ),)
            actual = hashlib.sha256(staged.read_bytes()).hexdigest()
            if actual != digest:
                return (RepairAction(
                    "reinstall_room_mod", "Reinstall changed room mod",
                    (f"Replace launcher-owned package: {staged.name}", f"SHA-256: {actual} → {digest}"), True,
                    "Installer keeps file backups and verifies installed hash.",
                ),)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            return (RepairAction(
                "archive_stale_install_record", "Archive stale install record",
                (f"Move launcher record to repair backup: {receipt_path.name}",), False,
                f"Restore {receipt_path.name} from repair backup. ({error})",
            ),)
        return ()

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
        if game_root:
            try:
                root = validate_game_root(Path(str(game_root)))
                checks.append(Diagnostic("game", "ok", "DOOM Eternal installation validated", {"path": str(root)}))
            except ValueError as error:
                checks.append(Diagnostic("game", "invalid", str(error)))
        else:
            checks.append(Diagnostic("game", "missing", "DOOM Eternal installation is not configured"))
        checks.append(Diagnostic("processes", "ok", "process probe complete", {"items": list(detect_doom_processes())}))
        checks.append(Diagnostic("config", "ok", "launcher configuration loaded", {"keys": sorted(self.config)}))
        actions = self.repair_actions()
        if actions:
            checks.append(Diagnostic(
                "room_mod", "invalid", "room mod needs repair",
                {"actions": [asdict(action) for action in actions]},
            ))
        else:
            checks.append(Diagnostic("room_mod", "ok", "launcher-owned room mod verified"))
        return DoctorReport(self.VERSION, tuple(checks))
