"""Room-bound DeathLink policy and observation-to-publication coordination."""
import hashlib
import json
import random
import time
from types import MappingProxyType
from typing import Protocol

from doom_eap.runtime.deathlink_receive import ReceiveState


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
        self._confirmed_echo = None
        self._generation = 0

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
    def seen_events(self):
        return frozenset(self._seen)

    def instrumentation(self):
        return tuple(MappingProxyType(row) for row in self._receiver.instrumentation_dicts())

    def configure(self, enabled):
        self._enabled = bool(enabled)
        self._receiver.configure_mode("soft")

    def invalidate_outbound(self):
        self._generation += 1

    def bind(self, state_key, state):
        self.invalidate_outbound()
        self._state_key = state_key
        seen = state.get("received_deathlink_event_ids", [])
        if not isinstance(seen, list):
            seen = []
        self._seen = {value for value in seen[-64:] if isinstance(value, str) and value}
        state["received_deathlink_event_ids"] = sorted(self._seen)[-64:]

    def abandon(self, now, reason):
        self.invalidate_outbound()
        return self._receiver.abandon(now, reason)

    def observe_echo(self, data_time, last_death_link):
        if data_time == last_death_link and data_time != self._confirmed_echo:
            self._logger.info("[DeathLink] Server received and echoed the death.")
            self._confirmed_echo = data_time

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

    def advance(self, safe_gameplay, publication):
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
        if result.detail == "dispatched":
            hit_num = active.attempts if active else 1
            self._logger.info(
                "[DeathLink] %s hit %d queued; command in flight.",
                event_id,
                hit_num,
            )
        elif result.detail == "burst_wait":
            self._logger.info(
                "[DeathLink] %s hit 1 delivered; waiting ~500ms before second hit.",
                event_id,
            )
        elif result.state is ReceiveState.APPLIED:
            self._logger.info(
                "[DeathLink] %s lethal burst complete (%s).",
                event_id,
                result.detail,
            )
        elif result.state is ReceiveState.RESOLVED:
            self._logger.info(
                "[DeathLink] %s lethal burst resolved (%s).",
                event_id,
                result.detail,
            )
        elif result.state in {ReceiveState.EXPIRED, ReceiveState.FAILED}:
            publication.cancel(self._state_key)
            state_name = result.state.value.lower() if result.state else "unknown"
            self._logger.warning(
                "[DeathLink] %s %s (%s); event cleared without claiming success.",
                event_id,
                state_name,
                result.detail,
            )


    async def report_local_death(self, player, send, publication):
        if not self._enabled:
            self._logger.info("[DeathLink] DEATHLINK_OUTBOUND_DROPPED reason=death_link_disabled")
            return
        receive_result = self._receiver.confirm_local_death(time.monotonic())
        if receive_result.detail in {
            "echo_suppressed",
            "late_echo_suppressed",
            "second_hit_cancelled_player_dead",
        }:
            publication.cancel(self._state_key)
            self._logger.info(
                "[DeathLink] DEATHLINK_OUTBOUND_DROPPED reason=%s event=%s",
                receive_result.detail,
                (receive_result.event_id or "unknown")[:12],
            )
            return
        player = player or "The Doom Slayer"
        cause = random.choice(self._messages).format(player=player)
        self._logger.info("[DeathLink] DEATHLINK_OUTBOUND_ACCEPTED cause=%s", cause)
        generation = self._generation
        try:
            await send(cause)
        except Exception:
            if generation != self._generation:
                return
            raise
        if generation != self._generation:
            return
        self._logger.info("[DeathLink] DEATHLINK_SEND_CONFIRMED")
        self._emit(
            "deathlink",
            direction="sent",
            cause=cause,
            message=cause,
        )


