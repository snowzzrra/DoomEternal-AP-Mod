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
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    WAITING_FOR_RETRY = "WAITING_FOR_RETRY"
    CONFIRMED = "CONFIRMED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


@dataclass
class ReceivedDeathLink:
    event_id: str
    state: ReceiveState
    total_deadline: float
    next_attempt_at: float
    confirmation_deadline: float | None = None
    attempts: int = 0
    deliveries: int = 0


@dataclass(frozen=True)
class ReceiveResult:
    event_id: str | None
    state: ReceiveState | None
    detail: str


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
    ):
        limits = (wait_timeout, confirm_timeout, retry_interval, total_timeout, late_suppression_grace)
        if any(value <= 0 for value in limits) or max_attempts < 1 or max_queue < 1:
            raise ValueError("invalid DeathLink receiver limits")
        self.wait_timeout = wait_timeout
        self.confirm_timeout = confirm_timeout
        self.retry_interval = retry_interval
        self.total_timeout = total_timeout
        self.late_suppression_grace = late_suppression_grace
        self.max_attempts = max_attempts
        self.max_queue = max_queue
        self._queue: deque[ReceivedDeathLink] = deque()
        self._recent: dict[str, float] = {}
        self._suppression_event_id: str | None = None
        self._late_suppression: tuple[str, float] | None = None

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
            return ReceiveResult(event_id, None, "duplicate")
        self._recent[event_id] = now + self.total_timeout + self.late_suppression_grace
        if len(self._queue) >= self.max_queue:
            return ReceiveResult(event_id, ReceiveState.FAILED, "queue_full")
        event = ReceivedDeathLink(
            event_id=event_id,
            state=ReceiveState.RECEIVED,
            total_deadline=now + self.total_timeout,
            next_attempt_at=now,
        )
        self._queue.append(event)
        return ReceiveResult(event_id, event.state, "queued")

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
            return ReceiveResult(None, None, "idle")
        if now >= event.total_deadline:
            return self._finish(ReceiveState.EXPIRED, "total_timeout", now=now, allow_late_suppression=True)
        if event.state is ReceiveState.RECEIVED:
            event.state = ReceiveState.WAITING_FOR_SAFE_GAMEPLAY
            event.next_attempt_at = min(event.next_attempt_at, now + self.wait_timeout)

        in_flight = command_in_flight()
        if event.state is ReceiveState.COMMAND_IN_FLIGHT:
            if in_flight:
                return ReceiveResult(event.event_id, event.state, "awaiting_delivery")
            event.deliveries += 1
            event.state = ReceiveState.AWAITING_CONFIRMATION
            event.confirmation_deadline = now + self.confirm_timeout
            self._suppression_event_id = event.event_id
            return ReceiveResult(event.event_id, event.state, "delivered")

        if event.state is ReceiveState.AWAITING_CONFIRMATION:
            if event.confirmation_deadline is not None and now < event.confirmation_deadline:
                return ReceiveResult(event.event_id, event.state, "awaiting_confirmation")
            if event.attempts >= self.max_attempts:
                return self._finish(ReceiveState.FAILED, "attempt_limit", now=now, allow_late_suppression=True)
            event.state = ReceiveState.WAITING_FOR_RETRY
            event.next_attempt_at = now + self.retry_interval
            return ReceiveResult(event.event_id, event.state, "retry_scheduled")

        if in_flight:
            if event.attempts:
                event.state = ReceiveState.COMMAND_IN_FLIGHT
                return ReceiveResult(event.event_id, event.state, "awaiting_delivery")
            # A timed-out predecessor may still be owned by the consumer. Never
            # adopt or delete that .processing file for this newer event.
            return ReceiveResult(event.event_id, event.state, "foreign_command_in_flight")
        if not safe_gameplay:
            return ReceiveResult(event.event_id, event.state, "unsafe_gameplay")
        if now < event.next_attempt_at:
            return ReceiveResult(event.event_id, event.state, "retry_interval")
        if event.attempts >= self.max_attempts:
            return self._finish(ReceiveState.FAILED, "attempt_limit", now=now, allow_late_suppression=True)
        try:
            accepted = dispatch()
        except Exception as error:
            return self._finish(ReceiveState.FAILED, f"dispatch_error:{type(error).__name__}", now=now)
        if not accepted:
            event.state = ReceiveState.WAITING_FOR_RETRY
            event.next_attempt_at = now + self.retry_interval
            return ReceiveResult(event.event_id, event.state, "rpc_not_accepted")
        event.attempts += 1
        event.state = ReceiveState.COMMAND_IN_FLIGHT
        self._suppression_event_id = event.event_id
        return ReceiveResult(event.event_id, event.state, "dispatched")

    def confirm_local_death(self, now: float) -> ReceiveResult:
        event = self.active
        if event is not None and self._suppression_event_id == event.event_id and event.attempts:
            return self._finish(ReceiveState.CONFIRMED, "echo_suppressed", now=now)
        if self._late_suppression is not None:
            event_id, deadline = self._late_suppression
            self._late_suppression = None
            if now <= deadline:
                # Timeout policy: suppress one death arriving shortly after a delivered
                # kill. This can suppress a coincidental local death during the bounded
                # grace window, but prevents a late DeathLink kill from echoing.
                return ReceiveResult(event_id, ReceiveState.CONFIRMED, "late_echo_suppressed")
        return ReceiveResult(None, None, "not_linked")

    def abandon(self, now: float) -> tuple[str, ...]:
        """Clear room-bound work while retaining bounded suppression for an in-flight kill."""
        abandoned: list[str] = []
        while self.active is not None:
            result = self._finish(
                ReceiveState.FAILED,
                "room_changed",
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
        return ReceiveResult(event.event_id, state, detail)

    def _prune_recent(self, now: float) -> None:
        self._recent = {event_id: expiry for event_id, expiry in self._recent.items() if expiry > now}
        if self._late_suppression is not None and self._late_suppression[1] < now:
            self._late_suppression = None
