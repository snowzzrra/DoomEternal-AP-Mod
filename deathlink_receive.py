"""Pure, process-local DeathLink receive state machine."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class ReceiveState(str, Enum):
    RECEIVED = "RECEIVED"
    WAITING_FOR_SAFE_GAMEPLAY = "WAITING_FOR_SAFE_GAMEPLAY"
    DISPATCHED_ONCE = "DISPATCHED_ONCE"
    CONFIRMED = "CONFIRMED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


@dataclass
class ReceivedDeathLink:
    event_id: str
    state: ReceiveState
    deadline: float


@dataclass(frozen=True)
class ReceiveResult:
    event_id: str | None
    state: ReceiveState | None
    detail: str


class DeathLinkReceiver:
    """Bounded FIFO. State intentionally has no serialization API."""

    def __init__(self, *, wait_timeout: float = 20.0, confirm_timeout: float = 10.0, max_queue: int = 4):
        if wait_timeout <= 0 or confirm_timeout <= 0 or max_queue < 1:
            raise ValueError("invalid DeathLink receiver limits")
        self.wait_timeout = wait_timeout
        self.confirm_timeout = confirm_timeout
        self.max_queue = max_queue
        self._queue: deque[ReceivedDeathLink] = deque()
        self._recent: dict[str, float] = {}
        self._suppression_event_id: str | None = None

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
        self._recent[event_id] = now + self.wait_timeout + self.confirm_timeout
        if len(self._queue) >= self.max_queue:
            return ReceiveResult(event_id, ReceiveState.FAILED, "queue_full")
        event = ReceivedDeathLink(event_id, ReceiveState.RECEIVED, now + self.wait_timeout)
        self._queue.append(event)
        return ReceiveResult(event_id, event.state, "queued")

    def advance(self, *, now: float, safe_gameplay: bool, dispatch: Callable[[], bool]) -> ReceiveResult:
        event = self.active
        if event is None:
            return ReceiveResult(None, None, "idle")
        if now >= event.deadline:
            return self._finish(ReceiveState.EXPIRED, "deadline")
        if event.state is ReceiveState.RECEIVED:
            event.state = ReceiveState.WAITING_FOR_SAFE_GAMEPLAY
        if event.state is ReceiveState.DISPATCHED_ONCE:
            return ReceiveResult(event.event_id, event.state, "awaiting_confirmation")
        if not safe_gameplay:
            return ReceiveResult(event.event_id, event.state, "unsafe_gameplay")
        try:
            accepted = dispatch()
        except Exception as error:
            return self._finish(ReceiveState.FAILED, f"dispatch_error:{type(error).__name__}")
        if not accepted:
            return ReceiveResult(event.event_id, event.state, "rpc_not_accepted")
        event.state = ReceiveState.DISPATCHED_ONCE
        event.deadline = now + self.confirm_timeout
        self._suppression_event_id = event.event_id
        return ReceiveResult(event.event_id, event.state, "dispatched")

    def confirm_local_death(self) -> ReceiveResult:
        event = self.active
        if (
            event is None
            or event.state is not ReceiveState.DISPATCHED_ONCE
            or self._suppression_event_id != event.event_id
        ):
            return ReceiveResult(None, None, "not_linked")
        return self._finish(ReceiveState.CONFIRMED, "echo_suppressed")

    def _finish(self, state: ReceiveState, detail: str) -> ReceiveResult:
        event = self._queue.popleft()
        event.state = state
        if self._suppression_event_id == event.event_id:
            self._suppression_event_id = None
        return ReceiveResult(event.event_id, state, detail)

    def _prune_recent(self, now: float) -> None:
        self._recent = {
            event_id: expiry for event_id, expiry in self._recent.items() if expiry > now
        }
