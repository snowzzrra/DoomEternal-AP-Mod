"""Receipt-only, narrow temporary CVar effects."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

TRANSIENT_EFFECTS = {
    7770156: {"name": "damage_boost", "cvar": "g_damageScaleAllToAI", "factor": 1.50, "duration": 20.0},
    7770157: {"name": "damage_resistance", "cvar": "g_damageScaleAllToSlayer", "factor": 0.65, "duration": 20.0},
    7770158: {"name": "infinite_ammo", "cvar": "g_infiniteAmmo", "factor": 1, "duration": 10.0},
    7770159: {"name": "weakness_trap", "cvar": "g_damageScaleAllToAI", "factor": 0.70, "duration": 12.0},
    7770160: {"name": "vulnerability_trap", "cvar": "g_damageScaleAllToSlayer", "factor": 1.35, "duration": 12.0},
}
TRANSIENT_EFFECT_BASELINE_FILE = "ap_effect_baseline.state"
TRANSIENT_SCOPE_HEADER = "AP_TRANSIENT_SCOPE_V1"
TRANSIENT_SCOPE_PATH = "active_transient_scope"


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


class TransientEffectManager:
    """Owns only five named effects and their monotonic expirations."""

    def __init__(self, owner):
        self.owner = owner
        self.active: dict[str, tuple[float, float, str]] = {}
        self.scope_generation = 0
        self.bound_baseline: tuple[str, str] | None = None
        self.reset_pending = False

    def _scope(self) -> str | None:
        state_key = getattr(self.owner, "state_key", "")
        if not state_key:
            return None
        binding = _baseline_binding(self.owner.base_directory)
        if binding is None:
            return None
        if self.bound_baseline != binding:
            if self.bound_baseline is not None:
                self.active.clear()
                self.reset_pending = False
                self._publish_scope(None)
            self.bound_baseline = binding
        session_namespace = hashlib.sha256(str(state_key).encode("utf-8")).hexdigest()[:16]
        identity = "|".join(
            (
                str(state_key),
                str(session_namespace),
                str(os.getpid()),
                binding[0],
                binding[1],
                str(self.scope_generation),
            )
        )
        return f"effectscope-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"

    def _publish_scope(self, scope: str | None) -> None:
        path = Path(self.owner.base_directory) / "ap_queue" / TRANSIENT_SCOPE_PATH
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            if scope is None:
                temporary.unlink(missing_ok=True)
                path.unlink(missing_ok=True)
            else:
                temporary.write_text(scope + "\n", encoding="ascii")
                os.replace(temporary, path)
        except OSError:
            return

    def _commands(self, now: float) -> list[tuple[str, str]]:
        factors = {"g_damageScaleAllToAI": 1.0, "g_damageScaleAllToSlayer": 1.0}
        infinite_ammo = 0
        for _name, (expiry, factor, cvar) in self.active.items():
            if expiry > now:
                if cvar == "g_infiniteAmmo":
                    infinite_ammo = 1
                else:
                    factors[cvar] *= factor
        return [
            ("g_damageScaleAllToAI", f"g_damageScaleAllToAI {factors['g_damageScaleAllToAI']:.2f}"),
            ("g_damageScaleAllToSlayer", f"g_damageScaleAllToSlayer {factors['g_damageScaleAllToSlayer']:.2f}"),
            ("g_infiniteAmmo", f"g_infiniteAmmo {infinite_ammo}"),
        ]

    def _emit(self, now: float, *, room_scoped: bool = True) -> bool:
        scope = self._scope()
        if scope is None or not self.owner.transient_effects_ready():
            return False
        self._publish_scope(scope)
        for cvar, command in self._commands(now):
            command_id = self.owner.transient_command_id(cvar, command, scope)
            if not self.owner.send_transient_command(
                command, command_id, scope, room_scoped=room_scoped
            ):
                return False
        return True

    def apply_receipt(self, item_id: int) -> tuple[bool, str, bool]:
        effect = TRANSIENT_EFFECTS.get(item_id)
        if effect is None:
            return False, "not a transient effect", False
        now = time.monotonic()
        self.tick(now, emit=False)
        if not self.owner.transient_effects_ready():
            return False, "transient baseline or safe gameplay unavailable", False
        current = self.active.get(effect["name"])
        expiry = max(now, current[0] if current is not None else now)
        self.active[effect["name"]] = (
            expiry + effect["duration"], effect["factor"], effect["cvar"]
        )
        if not self._emit(now):
            return False, "transient command spool rejected", True
        self.reset_pending = False
        return True, effect["name"], False

    def tick(self, now: float | None = None, *, emit: bool = True) -> bool:
        now = time.monotonic() if now is None else now
        expired = [name for name, (expiry, _factor, _cvar) in self.active.items() if expiry <= now]
        for name in expired:
            self.active.pop(name, None)
        if expired:
            self.reset_pending = True
        if not emit:
            return not expired
        if expired or self.reset_pending:
            emitted = self._emit(now)
            if emitted:
                self.reset_pending = False
            return emitted
        return True

    def reset(self, reason: str) -> None:
        self.active.clear()
        self.scope_generation += 1
        self.reset_pending = True
        self._publish_scope(None)
        if self.owner.transient_effects_ready():
            if self._emit(time.monotonic(), room_scoped=False):
                self.reset_pending = False
