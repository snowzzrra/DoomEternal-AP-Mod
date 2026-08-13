"""Bounded launcher diagnostics and sanitized support bundle generation."""

from __future__ import annotations

import json
import os
import re
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

from launcher_platform import (
    DiscoverySentinel,
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
    """Write only diagnostics and bounded redacted logs; saves are never read."""
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
        return DoctorReport(self.VERSION, tuple(checks))
