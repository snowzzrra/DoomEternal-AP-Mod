"""Receipt-only, narrow temporary CVar effects."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Protocol

TRANSIENT_EFFECTS = {
    7770156: {"name": "damage_boost", "cvar": "g_damageScaleAllToAI", "factor": 1.50, "duration": 20.0},
    7770157: {"name": "damage_resistance", "cvar": "g_damageScaleAllToSlayer", "factor": 0.65, "duration": 20.0},
    7770158: {"name": "infinite_ammo", "cvar": "g_infiniteAmmo", "factor": 1, "duration": 10.0},
    7770159: {"name": "weakness_trap", "cvar": "g_damageScaleAllToAI", "factor": 0.70, "duration": 12.0},
    7770160: {"name": "vulnerability_trap", "cvar": "g_damageScaleAllToSlayer", "factor": 1.35, "duration": 12.0},
}


@dataclass(frozen=True)
class TransientRuntime:
    state_key: str
    binding: tuple[str, str] | None
    ready: bool


class TransientPublicationPort(Protocol):
    def publish_scope(self, scope: str | None): ...
    async def send(self, cvar: str, command: str, state_key: str, scope: str, *, room_scoped: bool, receipt_index=None) -> bool: ...


class TransientEffectManager:
    """Owns only five named effects and their monotonic expirations."""

    def __init__(self, publication: TransientPublicationPort, process_id: int):
        self._publication = publication
        self._process_id = process_id
        self._active: dict[str, tuple[float, float, str]] = {}
        self._receipts = set()
        self._scope_generation = 0
        self._bound_baseline: tuple[str, str] | None = None
        self._reset_pending = True

    def _scope(self, runtime) -> str | None:
        state_key = runtime.state_key
        if not state_key:
            return None
        binding = runtime.binding
        if binding is None:
            return None
        if self._bound_baseline != binding:
            if self._bound_baseline is not None:
                self._active.clear()
                self._reset_pending = False
                self._publication.publish_scope(None)
            self._bound_baseline = binding
        session_namespace = hashlib.sha256(str(state_key).encode("utf-8")).hexdigest()[:16]
        identity = "|".join(
            (
                str(state_key),
                str(session_namespace),
                str(self._process_id),
                binding[0],
                binding[1],
                str(self._scope_generation),
            )
        )
        return f"effectscope-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"

    def _commands(self, now: float) -> list[tuple[str, str]]:
        factors = {"g_damageScaleAllToAI": 1.0, "g_damageScaleAllToSlayer": 1.0}
        infinite_ammo = 0
        for _name, (expiry, factor, cvar) in self._active.items():
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

    async def _emit(self, now: float, runtime, *, room_scoped: bool = True, receipt_index=None) -> bool:
        scope = self._scope(runtime)
        if scope is None or not runtime.ready:
            return False
        self._publication.publish_scope(scope)
        for cvar, command in self._commands(now):
            if not await self._publication.send(cvar, command, runtime.state_key, scope, room_scoped=room_scoped, receipt_index=receipt_index):
                return False
        return True

    async def apply_receipt(self, item_id: int, runtime: TransientRuntime, *, receipt_index=None) -> tuple[bool, str, bool]:
        effect = TRANSIENT_EFFECTS.get(item_id)
        if effect is None:
            return False, "not a transient effect", False
        now = time.monotonic()
        await self.tick(runtime, now, emit=False)
        if not runtime.ready:
            return False, "transient baseline or safe gameplay unavailable", False
        identity = (runtime.state_key, receipt_index)
        if receipt_index is not None and identity in self._receipts:
            return True, effect["name"], False
        if receipt_index is not None:
            self._receipts.add(identity)
        current = self._active.get(effect["name"])
        expiry = max(now, current[0] if current is not None else now)
        self._active[effect["name"]] = (
            expiry + effect["duration"], effect["factor"], effect["cvar"]
        )
        self._reset_pending = True
        if not await self._emit(now, runtime, receipt_index=receipt_index):
            return False, "transient command rejected", True
        self._reset_pending = False
        return True, effect["name"], False

    async def tick(self, runtime: TransientRuntime, now: float | None = None, *, emit: bool = True) -> bool:
        now = time.monotonic() if now is None else now
        expired = [name for name, (expiry, _factor, _cvar) in self._active.items() if expiry <= now]
        for name in expired:
            self._active.pop(name, None)
        if expired:
            self._reset_pending = True
        if not emit:
            return not expired
        if expired or self._reset_pending:
            emitted = await self._emit(now, runtime)
            if emitted:
                self._reset_pending = False
            return emitted
        return True

    async def reset(self, reason: str, runtime: TransientRuntime) -> None:
        self._active.clear()
        self._scope_generation += 1
        self._reset_pending = True
        self._publication.publish_scope(None)
        if runtime.ready:
            if await self._emit(time.monotonic(), runtime, room_scoped=False):
                self._reset_pending = False
