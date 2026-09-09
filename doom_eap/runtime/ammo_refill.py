"""Ammo charge accounting and staged-spend policy, independent of CommonClient."""
import asyncio
from typing import Protocol
from dataclasses import dataclass
from collections.abc import Mapping

AMMO_REFILL_ITEM_ID = 7770024
AMMO_REFILL_PRIMITIVE_ITEM_ID = 7770042
AMMO_REFILL_CAPACITY = 3


@dataclass(frozen=True)
class AmmoCommandScope:
    state_key: str
    current_map: str | None
    save_slot: str | None
    crucible_active: bool


class AmmoStoragePort(Protocol):
    async def increment(self, key: str, default: int, delta: int): ...
    async def watch(self, keys: tuple[str, str]): ...


class AmmoCommandsPort(Protocol):
    def pause(self): ...
    def stage(self, scope: AmmoCommandScope) -> bool: ...
    def confirm(self) -> bool: ...
    def cancel(self, reason: str): ...


def active_crucible(special_mode, processed_ids):
    if special_mode in {"the_crucible", "The Crucible"}:
        return 7770007 in processed_ids
    if special_mode in {"progressive_special_weapon", "Progressive Special Weapon"}:
        return sum(item_id == 7770901 for item_id in processed_ids) == 1
    return False


def ammo_readiness(*, placement_ready, item_ready, connected, namespace_match, marker,
                   active_lease, native_safe, runtime_ready, active_slot, available):
    marker_epoch = marker.get("gameplay_epoch") if isinstance(marker, Mapping) else None
    lease_match = bool(marker_epoch and active_lease == marker_epoch)
    predicates = (
        ("placement_connected", placement_ready), ("item_state_ready", item_ready),
        ("connected", connected), ("namespace_match", namespace_match),
        ("marker_valid", isinstance(marker, Mapping) and not marker.get("materialization_suspended")),
        ("lease_match", lease_match), ("native_safe", native_safe), ("runtime_effects_ready", runtime_ready),
    )
    failure = next((name for name, passed in predicates if not passed), None)
    return failure, {
        "marker": marker.get("runtime_map") if isinstance(marker, Mapping) else None,
        "lease": active_lease, "lease_match": lease_match, "native_safe": native_safe,
        "namespace_match": namespace_match, "connected": connected, "active_slot": active_slot,
        "available_charges": available or 0,
    }


class AmmoRefill:
    def __init__(self, commands: AmmoCommandsPort, storage: AmmoStoragePort, emit, logger):
        self._commands = commands
        self._storage = storage
        self._emit = emit
        self._logger = logger
        self._generation = 0
        self._overflow_task = None
        self._received = 0
        self.bind("")

    def invalidate(self, reason):
        self._generation += 1
        if self._overflow_task is not None:
            self._overflow_task.cancel()
            self._overflow_task = None
        self._commands.cancel(reason)
        self._pending = False
        self._discard_pending = None

    def bind(self, state_key):
        self.invalidate("room rebind")
        self._state_key = state_key
        self._storage_key = f"doom_eap:{state_key}:ammo_refill_consumed" if state_key else None
        self._discarded_key = f"doom_eap:{state_key}:ammo_refill_discarded" if state_key else None
        self._consumed = self._discarded = self._available = None
        self._received = 0

    @property
    def storage_keys(self):
        return self._storage_key, self._discarded_key

    @property
    def pending(self):
        return self._pending

    @property
    def available(self):
        return self._available

    def observe_receipts(self, count):
        self._received = count
        return self.refresh()

    async def load_storage(self):
        if self._storage_key is not None:
            await self._storage.watch(self.storage_keys)

    def refresh(self):
        consumed = getattr(self, "_consumed", None)
        discarded = getattr(self, "_discarded", None)
        pending_discarded = getattr(self, "_discard_pending", None)
        if not isinstance(consumed, int) or consumed < 0 or not isinstance(discarded, int) or discarded < 0:
            self._available = None
            return None
        effective_discarded = discarded
        if isinstance(pending_discarded, int):
            effective_discarded = max(effective_discarded, pending_discarded)
        raw_available = max(
            self._received - consumed - effective_discarded,
            0,
        )
        overflow_target = self.overflow_target()
        if (
            isinstance(pending_discarded, int)
            or (
                isinstance(overflow_target, int)
                and overflow_target > discarded
            )
        ):
            raw_available = min(raw_available, AMMO_REFILL_CAPACITY)
        self._available = raw_available
        return self._available


    def balance(self):
        available = self.refresh()
        consumed = getattr(self, "_consumed", None)
        discarded = getattr(self, "_discarded", None)
        return {
            "available": available if isinstance(available, int) else 0,
            "consumed": consumed if isinstance(consumed, int) else None,
            "discarded": discarded if isinstance(discarded, int) else None,
            "received": self._received,
            "capacity": AMMO_REFILL_CAPACITY,
            "authoritative": isinstance(consumed, int) and isinstance(discarded, int),
        }


    def emit_balance(self, status="ready", message=None, source=None):
        payload = self.balance()
        if source is not None:
            payload["source"] = source
        if message is not None:
            payload["message"] = message
        self._logger.info(
            "AMMO_REFILL_BALANCE available=%s received=%s consumed=%s discarded=%s source=%s",
            payload.get("available"),
            payload.get("received"),
            payload.get("consumed"),
            payload.get("discarded"),
            source or "unspecified",
        )
        self._emit("ammo_refill", status=status, **payload)


    def overflow_target(self):
        consumed = getattr(self, "_consumed", None)
        discarded = getattr(self, "_discarded", None)
        if (
            not isinstance(consumed, int)
            or isinstance(consumed, bool)
            or consumed < 0
            or not isinstance(discarded, int)
            or isinstance(discarded, bool)
            or discarded < 0
        ):
            return None
        return max(discarded, self._received - consumed - AMMO_REFILL_CAPACITY)


    def schedule_overflow(self, source):
        if isinstance(self._discard_pending, int):
            return
        if self._overflow_task is not None and not self._overflow_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._overflow_task = loop.create_task(
            self.normalize_overflow(source)
        )


    async def normalize_overflow(self, source):
        generation = self._generation
        try:
            target = self.overflow_target()
            current = self._discarded
            if not isinstance(target, int) or not isinstance(current, int) or target <= current:
                return
            delta = target - current
            self._discard_pending = target
            self._logger.info(
                "AMMO_REFILL_OVERFLOW received=%s consumed=%s discarded_before=%s "
                "discarded_increment=%s discarded_target=%s capacity=%s source=%s",
                self._received,
                self._consumed,
                current,
                delta,
                target,
                AMMO_REFILL_CAPACITY,
                source,
            )
            try:
                await self._storage.increment(self._discarded_key, current, delta)
            except Exception as error:
                if generation != self._generation:
                    return
                self._discard_pending = None
                self._logger.warning("AMMO_REFILL_OVERFLOW retry_required error=%s", error)
        finally:
            if generation == self._generation:
                self._overflow_task = None


    def consume_discarded(self, value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            self._logger.warning("[Ammo Refill] Ignoring invalid server discarded count: %r", value)
            return
        previous = self._discarded
        if isinstance(previous, int) and value < previous:
            self._logger.error(
                "[Ammo Refill] Refusing non-monotonic server discarded count: %s < %s",
                value,
                previous,
            )
            return
        pending = self._discard_pending
        if isinstance(pending, int) and value < pending:
            self._logger.warning(
                "[Ammo Refill] Discarded update below pending target: %s < %s",
                value,
                pending,
            )
            return
        self._discarded = value
        self._discard_pending = None
        self.refresh()
        self.emit_balance(source="discarded_storage_update")
        self.schedule_overflow("discarded_storage_update")


    def consume_consumed(self, value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            self._logger.warning("[Ammo Refill] Ignoring invalid server consumed count: %r", value)
            if self._pending:
                self._commands.cancel("invalid storage update")
                self._pending = False
            self.emit_balance(
                status="error",
                source="storage_update",
                message="Ammo Refill storage returned invalid balance",
            )
            return
        if (
            self._consumed is not None
            and value < self._consumed
        ):
            self._logger.error(
                "[Ammo Refill] Refusing non-monotonic server consumed count: %s < %s",
                value,
                self._consumed,
            )
            if self._pending:
                self._commands.cancel("non-monotonic storage update")
                self._pending = False
            self.emit_balance(
                status="error",
                source="storage_update",
                message="Ammo Refill storage balance moved backwards",
            )
            return
        if self._pending:
            prior_consumed = self._consumed
            if not isinstance(prior_consumed, int):
                self._commands.cancel("storage baseline unavailable")
                self._pending = False
                self.emit_balance(
                    status="error",
                    source="storage_update",
                    message="Ammo Refill storage baseline unavailable",
                )
                return
            expected = prior_consumed + 1
            if value != expected:
                self._commands.cancel("storage update did not apply staged charge")
                self._pending = False
                self.emit_balance(
                    status="error",
                    source="storage_update",
                    message="Ammo Refill storage update did not apply",
                )
                return
        self._consumed = value
        available = self.refresh()
        if self._pending:
            self._pending = False
            try:
                if not self._commands.confirm():
                    raise RuntimeError("execution gate refused enable")
            except Exception as error:
                self._logger.error("[Ammo Refill] Could not arm staged command: %s", error)
                self.emit_balance(
                    status="queued",
                    source="storage_update",
                    message=f"Ammo Refill queued; execution gate unavailable: {error}",
                )
                return
        self.emit_balance(
            status="ready",
            source="storage_update",
            message=f"Ammo Refill charges available: {available or 0}",
        )
        self.schedule_overflow("storage_update")


    async def request(self, scope, readiness_result):
        generation = self._generation
        if scope.state_key != self._state_key:
            return False
        failing_predicate, readiness = readiness_result
        if failing_predicate is not None:
            self._logger.info(
                "AMMO_REFILL_REQUEST result=rejected predicate=%s marker=%s lease=%s "
                "lease_match=%s native_safe=%s namespace_match=%s connected=%s "
                "active_slot=%s available_charges=%s",
                failing_predicate,
                readiness["marker"] or "<none>",
                readiness["lease"] or "<none>",
                str(readiness["lease_match"]).lower(),
                str(readiness["native_safe"]).lower(),
                str(readiness["namespace_match"]).lower(),
                str(readiness["connected"]).lower(),
                readiness["active_slot"] or "<none>",
                readiness["available_charges"],
            )
            self._emit(
                "ammo_refill",
                status="blocked",
                available=self._available or 0,
                message=f"Ammo Refill unavailable: {failing_predicate}",
            )
            return False
        available = self.refresh()
        if not available or self._pending:
            self._emit(
                "ammo_refill",
                status="empty" if not available else "busy",
                available=available or 0,
                message="No Ammo Refill charge available" if not available else "Ammo Refill already pending",
            )
            return False
        key = self._storage_key
        consumed = self._consumed
        if key is None or consumed is None:
            self._emit("ammo_refill", status="loading", available=available, message="Ammo Refill storage is not ready")
            return False
        self._pending = True
        try:
            self._commands.pause()
        except Exception as error:
            self._pending = False
            self._emit("ammo_refill", status="error", **self.balance(),
                       message=f"Ammo Refill command staging unavailable: {error}")
            return False
        if not self._commands.stage(scope):
            self._pending = False
            self._commands.cancel("primitive could not queue")
            self._emit(
                "ammo_refill",
                status="error",
                **self.balance(),
                message="Ammo Refill command could not be staged",
            )
            return False
        try:
            await self._storage.increment(key, consumed, 1)
        except Exception as error:
            if generation != self._generation:
                return False
            self._commands.cancel("storage update failed")
            self._pending = False
            self._emit(
                "ammo_refill",
                status="error",
                **self.balance(),
                message=str(error),
            )
            return False
        if generation != self._generation:
            return False
        self._emit(
            "ammo_refill",
            status="pending",
            **self.balance(),
            message="Ammo Refill storage update pending",
        )
        return True

