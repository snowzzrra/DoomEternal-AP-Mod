from __future__ import annotations

import hashlib
import json
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Protocol

from websockets.exceptions import WebSocketException

from doom_eap.strings import load_catalog, text

DEATHLINK_KILL_COALESCE_KEY = "deathlink-kill"


# causes go to every player in the room, so they stay in English
DEATHLINK_MESSAGES = tuple(
    text("deathlink.causes", key, player="{player}")
    for key in sorted(load_catalog().categories["deathlink.causes"])
)


class DeathLinkDispatchError(RuntimeError):
    def __init__(self, kind, native_result):
        super().__init__(f"Native DeathLink request {kind} refused: {native_result}")
        self.request_kind = kind
        self.native_result = native_result


class DeathLinkPublication:
    def __init__(self, link, event, mode):
        self._link, self._event, self._mode = link, event, mode

    def _identity(self):
        digest = bytes.fromhex(self._event.event_id)
        return int.from_bytes(digest[:8], "little") or 1, digest[8:24]

    @staticmethod
    def _validate_gameplay(link, scope):
        generation = scope.get('lifecycle_generation', 0)
        if (scope.get('target_pid') != link.pid or scope.get('server_pid') != link.pid
                or scope.get('availability') != 'enabled' or scope.get('lifecycle') != 'active'
                or scope.get('context_reason') != 'none' or generation <= 0
                or scope.get('context_generation') != generation):
            raise RuntimeError('native_instance_or_gameplay_generation_unavailable')

    @staticmethod
    def _same_scope(before, after):
        return all(before.get(key) and before[key] == after.get(key) for key in
                   ('process_created', 'instance_id', 'lifecycle_generation'))

    @classmethod
    def admit(cls, link):
        scope = link._run(['--native'])
        admission = link.request(12)
        cls._validate_gameplay(link, scope)
        if not cls._same_scope(scope, admission):
            raise RuntimeError('native_instance_or_gameplay_generation_changed')
        if (admission.get('namespace_id') != link.namespace or admission.get('state') != 'admitted'
                or not admission.get('accepting_requests') or not admission.get('route_retained')
                or not any(bytes.fromhex(admission.get('save_binding', '')))):
            raise RuntimeError('native_namespace_or_save_binding_unavailable')
        return scope

    @classmethod
    def poll_local(cls, link, enabled, mode):
        scope = cls.admit(link)
        result = link.deathlink_request(1, enabled=enabled, mode=mode)
        if result['outcome'] not in {0, 1}:
            raise DeathLinkDispatchError(1, result)
        if enabled:
            result = link.deathlink_request(3)
        if (result.get('namespace_id') != link.namespace
                or not cls._same_scope(scope, result)):
            raise RuntimeError('native_deathlink_observation_changed')
        return result

    @classmethod
    def ack_local(cls, link, event):
        if not cls._same_scope(cls.admit(link), event):
            raise RuntimeError('native_deathlink_ack_scope_changed')
        result = link.deathlink_request(4, ack_sequence=event['local_death_sequence'])
        if result['outcome'] not in {0, 1}:
            raise DeathLinkDispatchError(4, result)

    def dispatch(self, state_key):
        event_id, event_hash = self._identity()
        link = self._link()
        for kind, args in ((1, dict(enabled=True, mode=self._mode)),
                           (2, dict(event_id=event_id, event_hash=event_hash))):
            try:
                result = link.deathlink_request(kind, **args)
            except (OSError, RuntimeError, WebSocketException) as error:
                native_result = getattr(error, "native_result", None)
                if isinstance(native_result, dict):
                    raise DeathLinkDispatchError(kind, native_result) from error
                raise
        if result["outcome"] not in {0, 1, 7} or result["remote_state"] in {8, 9, 10}:
            raise DeathLinkDispatchError(2, result)
        return True

    def exists(self, state_key):
        if not self._event or not self._event.attempts:
            return False
        result = self._link().deathlink_request(0)
        if int(result["remote_event_id"]) != self._identity()[0]:
            raise RuntimeError("Native DeathLink event identity changed")
        if result["remote_state"] in {8, 9, 10}:
            raise RuntimeError("Native DeathLink failed without confirmation")
        if result["remote_state"] not in {1, 2, 3, 4, 6, 7}:
            raise RuntimeError("Native DeathLink effect remains unknown")
        return result["remote_state"] in {1, 2, 3}

    def cancel(self, state_key):
        if self._event and self._event.attempts:
            event_id, event_hash = self._identity()
            return self._link().deathlink_request(5, event_id=event_id, event_hash=event_hash)


class ReceiveState(str, Enum):
    RECEIVED = "RECEIVED"
    WAITING_FOR_SAFE_GAMEPLAY = "WAITING_FOR_SAFE_GAMEPLAY"
    COMMAND_IN_FLIGHT = "COMMAND_IN_FLIGHT"
    BURST_IN_FLIGHT = "BURST_IN_FLIGHT"
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
    """Bounded logical events dispatched once to the native domain."""

    def __init__(
        self,
        *,
        wait_timeout: float = 20.0,
        confirm_timeout: float = 10.0,
        retry_interval: float = 2.0,
        burst_interval: float = 0.5,
        total_timeout: float = 60.0,
        late_suppression_grace: float = 15.0,
        max_attempts: int = 2,
        max_burst_hits: int = 2,
        max_queue: int = 4,
        mode: str = "soft",
    ):
        limits = (wait_timeout, confirm_timeout, retry_interval, burst_interval, total_timeout, late_suppression_grace)
        if any(value <= 0 for value in limits) or max_attempts < 1 or max_burst_hits < 1 or max_queue < 1:
            raise ValueError("invalid DeathLink receiver limits")
        self.wait_timeout = wait_timeout
        self.confirm_timeout = confirm_timeout
        self.retry_interval = retry_interval
        self.burst_interval = burst_interval
        self.total_timeout = total_timeout
        self.late_suppression_grace = late_suppression_grace
        self.mode = self._validate_mode(mode)
        self.max_burst_hits = 1
        self.max_attempts: int | None = 1
        self.max_queue = max_queue
        self._queue: deque[ReceivedDeathLink] = deque()
        self._recent: dict[str, float] = {}
        self._suppression_event_id: str | None = None
        self._late_suppression: tuple[str, float] | None = None
        self._instrumentation: deque[DeathLinkInstrumentation] = deque(maxlen=128)

    @staticmethod
    def _validate_mode(mode: str) -> str:
        if mode not in {"soft", "hardcore"}:
            raise ValueError("invalid DeathLink mode")
        return mode

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
        """One native operation owns the selected mode's effect."""
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

    def _advance_waiting(self, event, now, in_flight, safe_gameplay, dispatch):
        if in_flight:
            if event.attempts:
                event.state = ReceiveState.COMMAND_IN_FLIGHT
                return self._result(event.event_id, event.state, "awaiting_delivery", now)
            return self._result(event.event_id, event.state, "foreign_command_in_flight", now)
        if not safe_gameplay:
            event.unsafe_pauses += 1
            return self._result(event.event_id, event.state, "unsafe_gameplay", now)
        event.safe_pauses += 1
        if now < event.next_attempt_at:
            return self._result(event.event_id, event.state, "retry_interval", now)
        if self.max_attempts is not None and event.attempts >= self.max_attempts:
            return self._finish(ReceiveState.FAILED, "attempt_limit", now=now, allow_late_suppression=True)
        return self._dispatch_attempt(event, now, dispatch)

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

        return self._advance_waiting(event, now, in_flight, safe_gameplay, dispatch)

    def _dispatch_attempt(self, event, now, dispatch):
        try:
            accepted = dispatch()
        except (OSError, RuntimeError, ValueError) as error:
            if _budget_refused(error):
                event.next_attempt_at = now + self.retry_interval
                return self._result(event.event_id, event.state, "native_budget_refused", now)
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
            detail = "echo_suppressed"
            if event.state is ReceiveState.BURST_IN_FLIGHT:
                detail = "second_hit_cancelled_player_dead"
            return self._finish(ReceiveState.RESOLVED, detail, now=now)
        if self._late_suppression is not None:
            event_id, deadline = self._late_suppression
            self._late_suppression = None
            if now <= deadline:
                # save observations lack the native cause, so late remote kills need bounded suppression
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


class DeathLinkPublicationPort(Protocol):
    def dispatch(self, state_key: str) -> bool: ...
    def exists(self, state_key: str) -> bool: ...
    def cancel(self, state_key: str): ...


class DeathLinkSession:
    def __init__(self, receiver, messages, emit, logger):
        self._receiver = receiver
        self._messages = tuple(messages)
        self._emit, self._logger = emit, logger
        self._enabled = False
        self._state_key = None
        self._seen = set()
        self._confirmed_echo = deque(maxlen=64)
        self._generation = 0
        self._state = {}
        self._grace = 0
        self._available = False
        self._progress = None
        self._pending_echo = set()
        self._waiting_reason = None

    @property
    def enabled(self):
        return self._enabled

    @property
    def mode(self):
        return self._receiver.mode

    @property
    def receiving(self):
        return self._receiver.active is not None

    @property
    def active_event(self):
        return self._receiver.active

    @property
    def seen_events(self):
        return frozenset(self._seen)

    def instrumentation(self):
        return tuple(MappingProxyType(row) for row in self._receiver.instrumentation_dicts())

    def configure(self, enabled, mode="soft"):
        self._enabled = bool(enabled)
        self._receiver.configure_mode(mode)
        self.emit_progress(force=True)

    def invalidate_outbound(self):
        self._generation += 1
        self._pending_echo.clear()

    def bind(self, state_key, state):
        self.invalidate_outbound()
        self._state_key = state_key
        self._state = state
        seen = state.get("received_deathlink_event_ids", [])
        if not isinstance(seen, list):
            seen = []
        self._seen = {value for value in seen[-64:] if isinstance(value, str) and value}
        state["received_deathlink_event_ids"] = sorted(self._seen)[-64:]
        self._available = False
        self.emit_progress(force=True)

    def abandon(self, now, reason):
        self.invalidate_outbound()
        self.set_observation(False)
        return self._receiver.abandon(now, reason)

    def expect_echo(self, timestamp):
        self._pending_echo.add(timestamp)

    def observe_echo(self, data_time):
        if data_time in self._pending_echo:
            self._logger.info("[DeathLink] Server received and echoed the death.")
            self._confirmed_echo.append(data_time)
            self._pending_echo.remove(data_time)
            return True
        return data_time is not None and data_time in self._confirmed_echo

    def receive(self, data, persist):
        if not self._enabled:
            return
        now = time.monotonic()
        event_id = hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if event_id in self._seen:
            self._logger.info("[DeathLink] Ignored persisted duplicate event %s.", event_id[:12])
            return
        result = self._receiver.receive(event_id, now)
        if result.detail == "duplicate":
            self._logger.info("[DeathLink] Ignored duplicate received event %s.", event_id[:12])
            return
        if result.state is ReceiveState.FAILED:
            self._logger.warning("[DeathLink] Rejected %s: bounded receive queue is full.", event_id[:12])
            return
        self._seen.add(event_id)
        persist()
        self._logger.info("[DeathLink] Received logical event %s; queued for safe gameplay.", event_id[:12])
        return event_id

    def advance(self, safe_gameplay, publication, waiting_reason=None):
        if not self._enabled:
            return
        result = self._receiver.advance(
            now=time.monotonic(),
            safe_gameplay=safe_gameplay,
            dispatch=lambda: publication.dispatch(self._state_key),
            command_in_flight=lambda: publication.exists(self._state_key),
        )
        event_id = (result.event_id or "unknown")[:12]
        active = self._receiver.active
        self._log_wait(result, waiting_reason)
        if result.detail == "dispatched":
            hit_num = active.attempts if active else 1
            self._logger.info(
                "[DeathLink] %s native operation %d queued; effect pending.",
                event_id,
                hit_num,
            )
        elif result.state is ReceiveState.APPLIED:
            self._logger.info(
                "[DeathLink] %s native operation terminal (%s).",
                event_id,
                result.detail,
            )
        elif result.state is ReceiveState.RESOLVED:
            self._logger.info(
                "[DeathLink] %s native event resolved (%s).",
                event_id,
                result.detail,
            )
        elif result.state in {ReceiveState.EXPIRED, ReceiveState.FAILED}:
            self._cancel(publication)
            state_name = result.state.value.lower() if result.state else "unknown"
            self._logger.warning(
                "[DeathLink] %s %s (%s); event cleared without claiming success.",
                event_id,
                state_name,
                result.detail,
            )

    def _log_wait(self, result, waiting_reason):
        if result.detail != 'unsafe_gameplay':
            self._waiting_reason = None
            return
        reason = waiting_reason or 'native_gameplay_unavailable'
        event_id = result.event_id[:12]
        if self._waiting_reason != (event_id, reason):
            active = self._receiver.active
            self._logger.info('[DeathLink] %s waiting reason=%s attempts=%d dispatches=%d unsafe_pauses=%d',
                              event_id, reason, active.attempts, active.attempts, active.unsafe_pauses)
            self._waiting_reason = (event_id, reason)


    def _validate_grace(self, grace):
        if type(grace) is not int or not 0 <= grace <= 2147483647:
            raise ValueError("DeathLink Grace must be a whole number from 0 to 2147483647")

    async def report_local_death(self, player, send, *, native, grace=0, persist):
        self._validate_grace(grace)
        domain, event = native
        sequence = event['local_death_sequence']
        previous = self._state.get('death_link_native', {}).get(domain, {})
        if previous.get('status') == 'pending' and previous['sequence'] < sequence:
            persist()
            if not await self._send_native_decision(previous, send, persist):
                return
        decision = self._native_decision(domain, event, player, grace)
        # the queue may replay after an ACK failure or bridge restart
        persist()
        self.emit_progress()
        self._logger.info('[DeathLink] LOCAL_DEATH_OBSERVED source=native domain=%s sequence=%s decision=%s',
                          domain, sequence, decision['status'])
        if sequence != decision.get('sequence') or decision['status'] != 'pending':
            return
        await self._send_native_decision(decision, send, persist)

    async def _send_native_decision(self, decision, send, persist):
        sequence = decision['sequence']
        generation = self._generation
        self._logger.info('[DeathLink] DEATHLINK_OUTBOUND_ACCEPTED sequence=%s', sequence)
        await send(decision['cause'], timestamp=decision['time'])
        if generation != self._generation:
            return False
        decision['status'] = 'sent'
        persist()
        self._logger.info('[DeathLink] DEATHLINK_SEND_CONFIRMED sequence=%s', sequence)
        self._emit('deathlink', direction='sent', cause=decision['cause'], message=decision['cause'])
        return True

    def _native_decision(self, domain, event, player, grace):
        decisions = self._state.setdefault('death_link_native', {})
        decision = decisions.get(domain, {})
        sequence = event['local_death_sequence']
        if sequence <= decision.get('sequence', 0):
            return decision
        suppressed = not self._enabled or event['local_cause'] != 0 or bool(event['local_flags'] & 512)
        skipped = suppressed or self._apply_grace(grace, lambda: None)
        status = 'suppressed' if suppressed else 'skipped' if skipped else 'pending'
        cause = '' if skipped else random.choice(self._messages).format(
            player=player or text('deathlink', 'local_player_fallback'))
        decision = dict(sequence=sequence, status=status, cause=cause, time=time.time())
        decisions[domain] = decision
        return decision

    def reconfigure_grace(self, grace, persist):
        self._validate_grace(grace)
        self._grace = grace
        progress = self._state.get('death_link_grace')
        if not isinstance(progress, dict) or progress.get('interval') != grace:
            self._state['death_link_grace'] = dict(interval=grace, deaths=0)
            persist()
        self.emit_progress(force=True)

    def set_observation(self, available):
        self._available = available
        self.emit_progress()

    def emit_progress(self, force=False):
        progress = self._state.get('death_link_grace', {})
        deaths = progress.get('deaths', 0) if isinstance(progress, dict) and progress.get('interval') == self._grace else 0
        interval = max(1, self._grace)
        if type(deaths) is not int or not 0 <= deaths < interval:
            deaths = 0
        status = 'off' if not self._enabled else 'ready' if self._available else 'unavailable'
        payload = dict(status=status, interval=interval, remaining=interval-deaths if status == 'ready' else None)
        if force or payload != self._progress:
            self._emit('deathlink_progress', **payload)
            self._progress = payload

    def _cancel(self, publication):
        try:
            publication.cancel(self._state_key)
        except (RuntimeError, OSError) as error:
            self._logger.warning("[DeathLink] Native cancellation unavailable: %s", error)


    def _apply_grace(self, grace, persist):
        if grace > 1:
            progress = self._state.get("death_link_grace", {})
            deaths = progress.get("deaths", 0) if isinstance(progress, dict) and progress.get("interval") == grace else 0
            if type(deaths) is not int or not 0 <= deaths < grace:
                deaths = 0
            deaths = (deaths + 1) % grace
            self._state["death_link_grace"] = {"interval": grace, "deaths": deaths}
            persist()
            if deaths:
                self._logger.info("[DeathLink] Grace: local death %d/%d; outgoing link skipped", deaths, grace)
                return True
        elif self._state.pop("death_link_grace", None) is not None:
            persist()
        return False


class DeathObservation:
    def __init__(self, logger):
        self._logger = logger
        self._checkpoints = {}
        self._initialized = set()
        self._consumed_tokens = set()
        self._respawn_carryover = {}
        self._consumed_epoch = {}
        self._last_event = {}
        self._warning = None
        self._details_dead = self._details_mtime = self._details_path = None
        self._namespace = None

    def bind(self, namespace):
        if self._namespace == namespace:
            return False
        self.reset()
        self._namespace = namespace
        return True

    def reset(self):
        namespace = self._namespace
        self.__init__(self._logger)
        self._namespace = namespace

    def start_empty_slot(self, slot):
        self._initialized.add(slot)
        self._checkpoints[slot] = False
        self._respawn_carryover[slot] = False

    @property
    def checkpoints(self):
        return MappingProxyType(dict(self._checkpoints))

    @property
    def warning(self):
        return self._warning

    def probe_failed(self, error):
        warning = str(error)
        if warning != self._warning:
            self._logger.warning("[DeathLink] game_duration probe failed; using game.details fallback: %s", error)
            self._warning = warning

    def probe_succeeded(self):
        self._warning = None

    def observe_checkpoint(self, snapshot, selected, load_epoch, current_map):
        died = bool(snapshot.get("checkpoint_death", False))
        raw_deaths = int(snapshot.get("raw_num_checkpoint_deaths", 1 if died else 0))
        slot_directory = selected.slot_directory
        save_mtime_ns = selected.mtime_ns
        save_snapshot_token = f"{slot_directory}:{selected.path.name}:{save_mtime_ns}"
        event_identity = f"{save_snapshot_token}:deaths={raw_deaths}"

        self._logger.info(
            "[DeathLink] DEATH_DETECTOR_OBSERVATION slot=%s map=%s load_epoch=%s "
            "raw_num_checkpoint_deaths=%s save_snapshot_token=%s save_mtime_ns=%s",
            slot_directory, current_map, load_epoch, raw_deaths, save_snapshot_token, save_mtime_ns,
        )

        if slot_directory not in self._initialized:
            self._initialized.add(slot_directory)
            self._checkpoints[slot_directory] = died
            if died:
                self._consumed_tokens.add(save_snapshot_token)
                self._respawn_carryover[slot_directory] = True
                self._consumed_epoch[slot_directory] = load_epoch
                self._last_event[slot_directory] = event_identity
                self._logger.info(
                    "[DeathLink] DEATH_DETECTOR_BASELINE reason=session_start_preexisting_death"
                )
            else:
                self._respawn_carryover[slot_directory] = False
                self._logger.info(
                    "[DeathLink] DEATH_DETECTOR_BASELINE reason=initial_clean_baseline"
                )
            return None

        self._checkpoints[slot_directory] = died

        if not died:
            self._respawn_carryover[slot_directory] = False
            return None

        if save_snapshot_token in self._consumed_tokens:
            self._logger.info(
                "[DeathLink] DEATH_EVIDENCE_ALREADY_CONSUMED event_identity=%s",
                event_identity,
            )
            return None

        if (
            self._respawn_carryover.get(slot_directory)
            and load_epoch != self._consumed_epoch.get(slot_directory)
        ):
            self._consumed_tokens.add(save_snapshot_token)
            self._respawn_carryover[slot_directory] = False
            previous_death_event = (
                self._last_event.get(slot_directory)
                or "unknown"
            )
            self._logger.info(
                "[DeathLink] POST_DEATH_RESPAWN_CARRYOVER slot=%s previous_death_event=%s "
                "new_epoch=%s raw_num_checkpoint_deaths=%s snapshot=%s action=suppressed",
                slot_directory,
                previous_death_event,
                load_epoch,
                raw_deaths,
                save_snapshot_token,
            )
            return None

        self._consumed_tokens.add(save_snapshot_token)
        self._respawn_carryover[slot_directory] = True
        self._consumed_epoch[slot_directory] = load_epoch
        self._last_event[slot_directory] = event_identity
        return event_identity

    def observe_details(self, details):
        if not details:
            return False
        died = details.get("diedLastGame") == "1"
        mtime = details.get("_mtime_ns")
        details_path = details.get("_path")
        if self._details_dead is None:
            self._details_dead = died
            self._details_mtime = mtime
            self._details_path = details_path
            self._logger.info(
                f"[Save] Monitoring {details.get('_path')} for DeathLink."
            )
            return False

        if details_path != self._details_path:
            self._details_dead = died
            self._details_mtime = mtime
            self._details_path = details_path
            self._logger.info(
                "[Save] Active autosave changed; DeathLink baseline reset to "
                f"{details_path}."
            )
            return False

        changed = mtime != self._details_mtime
        transitioned_to_dead = changed and died and not self._details_dead
        self._details_dead = died
        self._details_mtime = mtime

        return transitioned_to_dead


def _budget_refused(error):
    return (isinstance(error, DeathLinkDispatchError) and error.request_kind in {1, 2}
            and error.native_result.get("state") == 4 and error.native_result.get("reason") == 25)
