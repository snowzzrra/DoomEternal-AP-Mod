"""Fail-closed reader for native AP RPC health state."""

from __future__ import annotations

from dataclasses import dataclass
import re
import threading
import time
from pathlib import Path
from typing import Any, Mapping


HEALTH_FILE_NAME = "ap_rpc_health.state"
HEALTH_SCHEMA = 1
HEALTH_FRESHNESS_MS = 3000
HEALTH_READ_INTERVAL_MS = 350
HEALTH_STATES = frozenset({"starting", "ready", "unavailable", "stopped"})
_KEY = re.compile(r"[A-Za-z0-9_]{1,40}\Z")
_VALUE = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_REQUIRED_KEYS = frozenset(
    {
        "schema",
        "state",
        "pid",
        "timestamp_ms",
        "freshness_ms",
        "sequence",
        "result",
        "result_code",
        "transport",
        "transport_status",
    }
)
_MAX_BYTES = 8192


@dataclass(frozen=True)
class NativeHealthSnapshot:
    state: str
    ready: bool
    degraded: bool
    reason: str
    native_state: str | None = None
    path: str = ""
    pid: int | None = None
    timestamp_ms: int | None = None
    freshness_ms: int | None = None
    sequence: int | None = None
    result: str = ""
    result_code: int | None = None
    transport: str = ""
    transport_status: int | None = None

    def document(self) -> dict[str, object]:
        return {
            "state": self.state,
            "ready": self.ready,
            "degraded": self.degraded,
            "reason": self.reason,
            "native_state": self.native_state,
            "pid": self.pid,
            "timestamp_ms": self.timestamp_ms,
            "freshness_ms": self.freshness_ms,
            "sequence": self.sequence,
            "result": self.result,
            "result_code": self.result_code,
            "transport": self.transport,
            "transport_status": self.transport_status,
        }


def _not_ready(path: Path, reason: str) -> NativeHealthSnapshot:
    return NativeHealthSnapshot(
        state="not_ready",
        ready=False,
        degraded=False,
        reason=reason,
        path=str(path),
    )


def _integer(values: Mapping[str, str], key: str, *, minimum: int = 0) -> int:
    value = values[key]
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError(key)
    result = int(value)
    if result < minimum:
        raise ValueError(key)
    return result


def read_ap_rpc_health_state(path: Path, *, now_ms: int | None = None) -> NativeHealthSnapshot:
    """Read one native health snapshot; never creates, logs, or repairs state."""
    path = Path(path)
    try:
        payload = path.read_bytes()
    except OSError:
        return _not_ready(path, "missing")
    if not payload or len(payload) > _MAX_BYTES:
        return _not_ready(path, "malformed")
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError:
        return _not_ready(path, "malformed")

    values: dict[str, str] = {}
    lines = text.splitlines()
    if len(lines) != len(_REQUIRED_KEYS) or text.endswith("\n") is False:
        return _not_ready(path, "malformed")
    for line in lines:
        if line.count("=") != 1:
            return _not_ready(path, "malformed")
        key, value = line.split("=", 1)
        if not _KEY.fullmatch(key) or not value or len(value) > 64 or not _VALUE.fullmatch(value):
            return _not_ready(path, "malformed")
        if key in values:
            return _not_ready(path, "malformed")
        values[key] = value
    if set(values) != _REQUIRED_KEYS:
        return _not_ready(path, "unsupported")
    if values["state"] not in HEALTH_STATES:
        return _not_ready(path, "unsupported")

    try:
        schema = _integer(values, "schema", minimum=1)
        pid = _integer(values, "pid", minimum=1)
        timestamp_ms = _integer(values, "timestamp_ms")
        freshness_ms = _integer(values, "freshness_ms", minimum=1)
        sequence = _integer(values, "sequence", minimum=1)
        result_code = _integer(values, "result_code")
        transport_status = _integer(values, "transport_status")
    except (KeyError, ValueError):
        return _not_ready(path, "malformed")
    if schema != HEALTH_SCHEMA or freshness_ms != HEALTH_FRESHNESS_MS:
        return _not_ready(path, "unsupported")
    if result_code > 6 or transport_status > 0xFFFFFFFF:
        return _not_ready(path, "unsupported")

    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    age_ms = current_ms - timestamp_ms
    if age_ms < 0 or age_ms > freshness_ms:
        return _not_ready(path, "stale")

    native_state = values["state"]
    if native_state == "ready":
        normalized = "ready"
        reason = "ready"
        degraded = False
        ready = True
    elif native_state == "unavailable":
        normalized = "degraded"
        reason = "native_unavailable"
        degraded = True
        ready = False
    else:
        normalized = "not_ready"
        reason = native_state
        degraded = False
        ready = False
    return NativeHealthSnapshot(
        state=normalized,
        ready=ready,
        degraded=degraded,
        reason=reason,
        native_state=native_state,
        path=str(path),
        pid=pid,
        timestamp_ms=timestamp_ms,
        freshness_ms=freshness_ms,
        sequence=sequence,
        result=values["result"],
        result_code=result_code,
        transport=values["transport"],
        transport_status=transport_status,
    )


class NativeHealthReader:
    """Throttled launcher-side reader for one AP native health file."""

    def __init__(self, path: Path, *, interval_ms: int = HEALTH_READ_INTERVAL_MS):
        if interval_ms < 250 or interval_ms > 500:
            raise ValueError("health read interval must be between 250 and 500 ms")
        self.path = Path(path)
        self.interval_ms = interval_ms
        self._lock = threading.Lock()
        self._last_read_ms: int | None = None
        self._snapshot: NativeHealthSnapshot | None = None

    def read(self, *, force: bool = False, now_ms: int | None = None) -> NativeHealthSnapshot:
        current_ms = int(time.time() * 1000) if now_ms is None else now_ms
        with self._lock:
            if (
                not force
                and self._snapshot is not None
                and self._last_read_ms is not None
                and current_ms - self._last_read_ms < self.interval_ms
            ):
                return self._snapshot
            self._snapshot = read_ap_rpc_health_state(self.path, now_ms=current_ms)
            self._last_read_ms = current_ms
            return self._snapshot


def doom_base_dir_from_config(config: Mapping[str, Any]) -> Path | None:
    value = config.get("doom_base_dir")
    if not isinstance(value, str) or not value.strip():
        game_root = config.get("game_root")
        if not isinstance(game_root, str) or not game_root.strip():
            return None
        value = str(Path(game_root).expanduser() / "base")
    return Path(value).expanduser()
