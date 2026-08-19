"""Pure, process-local DeathLink receive state machine."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


def discard_unclaimed_command(queue_dir, coalesce_key: str) -> bool:
    """Remove only producer-owned .cmd; consumer exclusively owns .processing."""
    command = queue_dir / f"{coalesce_key}.cmd"
    try:
        command.unlink()
    except FileNotFoundError:
        return False
    return True


class ReceiveState(str, Enum):
    RECEIVED = "RECEIVED"
    WAITING_FOR_SAFE_GAMEPLAY = "WAITING_FOR_SAFE_GAMEPLAY"
    COMMAND_IN_FLIGHT = "COMMAND_IN_FLIGHT"
    APPLIED = "APPLIED"
    RESOLVED = "RESOLVED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


@dataclass
class ReceivedDeathLink:
    event_id: str
    state: ReceiveState
    total_deadline: float
    next_attempt_at: float
    attempts: int = 0
    deliveries: int = 0
    safe_pauses: int = 0
    unsafe_pauses: int = 0


@dataclass(frozen=True)
class ReceiveResult:
    event_id: str | None
    state: ReceiveState | None
    detail: str


@dataclass(frozen=True)
class DeathLinkInstrumentation:
    """Structured evidence for one receiver decision."""

    event_id: str | None
    state: ReceiveState | None
    detail: str
    attempts: int
    deliveries: int
    timestamp: float
    dispatches: int = 0
    safe_pauses: int = 0
    unsafe_pauses: int = 0
    death_confirmation: bool = False
    cancel_reason: str | None = None


class DeathLinkReceiver:
    """Bounded logical events with one observable spool command in flight."""

    def __init__(
        self,
        *,
        wait_timeout: float = 20.0,
        confirm_timeout: float = 10.0,
        retry_interval: float = 2.0,
        total_timeout: float = 60.0,
        late_suppression_grace: float = 15.0,
        max_attempts: int = 3,
        max_queue: int = 4,
        mode: str = "soft",
    ):
        limits = (wait_timeout, confirm_timeout, retry_interval, total_timeout, late_suppression_grace)
        if any(value <= 0 for value in limits) or max_attempts < 1 or max_queue < 1:
            raise ValueError("invalid DeathLink receiver limits")
        self.wait_timeout = wait_timeout
        self.confirm_timeout = confirm_timeout
        self.retry_interval = retry_interval
        self.total_timeout = total_timeout
        self.late_suppression_grace = late_suppression_grace
        if max_attempts < 1:
            raise ValueError("invalid DeathLink receiver limits")
        self.mode = "soft"
        self.max_attempts: int | None = 1
        self.max_queue = max_queue
        self._queue: deque[ReceivedDeathLink] = deque()
        self._recent: dict[str, float] = {}
        self._suppression_event_id: str | None = None
        self._late_suppression: tuple[str, float] | None = None
        self._instrumentation: deque[DeathLinkInstrumentation] = deque(maxlen=128)

    @staticmethod
    def _validate_mode(mode: str) -> str:
        # Legacy slot compatibility; normalized to single-burst delivery
        return "soft"

    @property
    def instrumentation(self) -> tuple[DeathLinkInstrumentation, ...]:
        return tuple(self._instrumentation)

    def instrumentation_dicts(self) -> tuple[dict[str, object], ...]:
        """Return bounded JSON-compatible receiver evidence."""
        return tuple(
            {
                "event_id": entry.event_id,
                "state": entry.state.value if entry.state else None,
                "detail": entry.detail,
                "attempts": entry.attempts,
                "deliveries": entry.deliveries,
                "timestamp": entry.timestamp,
                "dispatches": entry.dispatches,
                "safe_pauses": entry.safe_pauses,
                "unsafe_pauses": entry.unsafe_pauses,
                "death_confirmation": entry.death_confirmation,
                "cancel_reason": entry.cancel_reason,
            }
            for entry in self._instrumentation
        )

    def configure_mode(self, mode: str) -> None:
        """Tolerate legacy mode strings while enforcing single-burst delivery."""
        self.mode = self._validate_mode(mode)
        self.max_attempts = 1

    def _result(
        self,
        event_id: str | None,
        state: ReceiveState | None,
        detail: str,
        now: float,
    ) -> ReceiveResult:
        event = self.active if event_id and self.active and self.active.event_id == event_id else None
        self._instrumentation.append(
            DeathLinkInstrumentation(
                event_id=event_id,
                state=state,
                detail=detail,
                attempts=event.attempts if event else 0,
                deliveries=event.deliveries if event else 0,
                timestamp=now,
            )
        )
        return ReceiveResult(event_id, state, detail)

    @property
    def active(self) -> ReceivedDeathLink | None:
        return self._queue[0] if self._queue else None

    @property
    def queued_event_ids(self) -> tuple[str, ...]:
        return tuple(event.event_id for event in self._queue)

    @property
    def suppression_event_id(self) -> str | None:
        return self._suppression_event_id

    def receive(self, event_id: str, now: float) -> ReceiveResult:
        if not event_id:
            raise ValueError("DeathLink event_id is required")
        self._prune_recent(now)
        if event_id in self._recent or event_id in self.queued_event_ids:
            return self._result(event_id, None, "duplicate", now)
        self._recent[event_id] = now + self.total_timeout + self.late_suppression_grace
        if len(self._queue) >= self.max_queue:
            return self._result(event_id, ReceiveState.FAILED, "queue_full", now)
        event = ReceivedDeathLink(
            event_id=event_id,
            state=ReceiveState.RECEIVED,
            total_deadline=now + self.total_timeout,
            next_attempt_at=now,
        )
        self._queue.append(event)
        return self._result(event_id, event.state, "queued", now)

    def advance(
        self,
        *,
        now: float,
        safe_gameplay: bool,
        dispatch: Callable[[], bool],
        command_in_flight: Callable[[], bool],
    ) -> ReceiveResult:
        event = self.active
        if event is None:
            return self._result(None, None, "idle", now)
        if now >= event.total_deadline:
            return self._finish(ReceiveState.EXPIRED, "total_timeout", now=now, allow_late_suppression=True)
        if event.state is ReceiveState.RECEIVED:
            event.state = ReceiveState.WAITING_FOR_SAFE_GAMEPLAY
            event.next_attempt_at = min(event.next_attempt_at, now + self.wait_timeout)

        in_flight = command_in_flight()
        if event.state is ReceiveState.COMMAND_IN_FLIGHT:
            if in_flight:
                return self._result(event.event_id, event.state, "awaiting_delivery", now)
            event.deliveries += 1
            return self._finish(
                ReceiveState.APPLIED,
                "accepted",
                now=now,
                allow_late_suppression=True,
            )

        if in_flight:
            if event.attempts:
                event.state = ReceiveState.COMMAND_IN_FLIGHT
                return self._result(event.event_id, event.state, "awaiting_delivery", now)
            # Consumer ownership keeps the existing .processing file bound to
            # its original event.
            return self._result(event.event_id, event.state, "foreign_command_in_flight", now)
        if not safe_gameplay:
            event.unsafe_pauses += 1
            return self._result(event.event_id, event.state, "unsafe_gameplay", now)
        event.safe_pauses += 1
        if now < event.next_attempt_at:
            return self._result(event.event_id, event.state, "retry_interval", now)
        if self.max_attempts is not None and event.attempts >= self.max_attempts:
            return self._finish(ReceiveState.FAILED, "attempt_limit", now=now, allow_late_suppression=True)
        try:
            accepted = dispatch()
        except Exception as error:
            return self._finish(ReceiveState.FAILED, f"dispatch_error:{type(error).__name__}", now=now)
        if not accepted:
            return self._finish(ReceiveState.FAILED, "rpc_not_accepted", now=now)
        event.attempts += 1
        event.state = ReceiveState.COMMAND_IN_FLIGHT
        self._suppression_event_id = event.event_id
        return self._result(event.event_id, event.state, "dispatched", now)

    def confirm_local_death(self, now: float) -> ReceiveResult:
        event = self.active
        if event is not None and self._suppression_event_id == event.event_id and event.attempts:
            return self._finish(ReceiveState.RESOLVED, "echo_suppressed", now=now)
        if self._late_suppression is not None:
            event_id, deadline = self._late_suppression
            self._late_suppression = None
            if now <= deadline:
                # Timeout policy: suppress one death arriving shortly after a delivered
                # kill. This can suppress a coincidental local death during the bounded
                # grace window, but prevents a late DeathLink kill from echoing.
                return self._result(event_id, ReceiveState.APPLIED, "late_echo_suppressed", now)
        return self._result(None, None, "not_linked", now)

    def abandon(self, now: float, reason: str = "room_changed") -> tuple[str, ...]:
        """Clear room-bound work while retaining bounded suppression for an in-flight kill."""
        abandoned: list[str] = []
        while self.active is not None:
            result = self._finish(
                ReceiveState.FAILED,
                reason,
                now=now,
                allow_late_suppression=True,
            )
            if result.event_id:
                abandoned.append(result.event_id)
        return tuple(abandoned)

    def _finish(
        self,
        state: ReceiveState,
        detail: str,
        *,
        now: float,
        allow_late_suppression: bool = False,
    ) -> ReceiveResult:
        event = self._queue.popleft()
        event.state = state
        if allow_late_suppression and event.attempts:
            self._late_suppression = (event.event_id, now + self.late_suppression_grace)
        if self._suppression_event_id == event.event_id:
            self._suppression_event_id = None
        self._instrumentation.append(
            DeathLinkInstrumentation(
                event_id=event.event_id,
                state=state,
                detail=detail,
                attempts=event.attempts,
                deliveries=event.deliveries,
                timestamp=now,
                dispatches=event.attempts,
                safe_pauses=event.safe_pauses,
                unsafe_pauses=event.unsafe_pauses,
                death_confirmation=state is ReceiveState.RESOLVED,
                cancel_reason=detail if detail in {"disconnect", "room_changed", "safe_action"} else None,
            )
        )
        return ReceiveResult(event.event_id, state, detail)

    def _prune_recent(self, now: float) -> None:
        self._recent = {event_id: expiry for event_id, expiry in self._recent.items() if expiry > now}
        if self._late_suppression is not None and self._late_suppression[1] < now:
            self._late_suppression = None
