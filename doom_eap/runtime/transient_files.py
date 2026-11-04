"""Explicit native baseline/health reads and transient command publication."""
import os
import time
from pathlib import Path

from doom_eap.contracts.command_publication import stable_command_key
from doom_eap.runtime.transient_effects import TransientRuntime

TRANSIENT_EFFECT_BASELINE_FILE = "ap_effect_baseline.state"


def _read_key_value_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return {}
    result = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator:
            result[key] = value
    return result


def transient_baseline_ready(base_directory: str | os.PathLike[str]) -> bool:
    base = Path(base_directory)
    data = _read_key_value_file(base / TRANSIENT_EFFECT_BASELINE_FILE)
    health = _read_key_value_file(base / "ap_rpc_health.state")
    if data.get("state") != "ready" or not data.get("attachment_epoch", "").isdigit():
        return False
    if data.get("pid") != health.get("pid") or health.get("state") != "ready":
        return False
    try:
        timestamp = int(data["timestamp_ms"])
        freshness = int(data["freshness_ms"])
    except (KeyError, ValueError):
        return False
    return timestamp <= int(time.time() * 1000) <= timestamp + freshness


def _baseline_binding(base_directory: str | os.PathLike[str]) -> tuple[str, str] | None:
    base = Path(base_directory)
    data = _read_key_value_file(base / TRANSIENT_EFFECT_BASELINE_FILE)
    health = _read_key_value_file(base / "ap_rpc_health.state")
    if data.get("state") != "ready" or health.get("state") != "ready":
        return None
    pid = data.get("pid")
    epoch = data.get("attachment_epoch")
    if not pid or not epoch or pid != health.get("pid"):
        return None
    return pid, epoch


def read_transient_runtime(base_directory, state_key, native_ready):
    return TransientRuntime(state_key, _baseline_binding(base_directory),
                            bool(native_ready and transient_baseline_ready(base_directory)))


class TransientPublication:
    def __init__(self, send):
        self._send = send

    async def send(self, cvar, command, state_key, scope, *, room_scoped, receipt_index=None):
        command_id = (f"temporary-{receipt_index}-{cvar}" if receipt_index is not None
                      else stable_command_key("effect", state_key, scope, cvar, command))
        return await self._send(command, coalesce_key=command_id, state_key=state_key,
                                receipt_index=receipt_index)

    def publish_scope(self, scope: str | None) -> None:
        self.scope = scope
