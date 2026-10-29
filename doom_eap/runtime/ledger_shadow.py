"""Development-only delivery observations and durable comparison.
Legacy delivery retains all authority, including when observation fails.
"""

import asyncio
from collections import Counter, deque
from dataclasses import asdict, is_dataclass
from functools import partial, wraps
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import uuid4

from .ledger import HistoryConflict, InvalidRecord, Ledger, NetworkReceipt, Observation, RoomKey


def isolated(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        if not self.enabled:
            return None
        try:
            return method(self, *args, **kwargs)
        except Exception as error:
            key = kwargs.get("state_key") or self.storage_keys.get(kwargs.get("storage_key"))
            self.failure(error, key)
            return None
    return call


def observed_publication(method):
    @wraps(method)
    def publish(self, cmd, coalesce_key=None, *args, **kwargs):
        result = method(self, cmd, coalesce_key, *args, **kwargs)
        self._observe("spool_publication", state_key=kwargs.get("state_key"), payload=cmd,
                      requested_id=coalesce_key, command_id=result.command_id,
                      accepted=result.accepted, evidence=result.evidence.value)
        return result
    return publish


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def snapshot(value):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {str(key): snapshot(entry) for key, entry in value.items()
                if not re.search(r"password|credential|secret|auth|token|address", str(key), re.I)}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [snapshot(entry) for entry in value]
    if value is None or type(value) in (str, int, bool, float):
        return value
    raise InvalidRecord("unsupported shadow evidence")


def integer(value):
    if type(value) is not int or value < 0:
        raise InvalidRecord("shadow count must be a nonnegative integer")
    return value


class ShadowObserver:
    def __init__(self, enabled, path, logger, *, capacity=1024):
        self.enabled = enabled is True
        self.path = Path(path)
        self.logger = logger
        self.capacity = capacity
        self.run = uuid4().hex
        self.sequence = 0
        self.watermark = 0
        self.current = None
        self.rooms = {}
        self.keys = {}
        self.storage_keys = {}
        self.pending = deque()
        self.task = None
        self.ledger = None
        self.incomplete = set()
        self.dropped = 0
        self.last_summary = None

    def failure(self, error, state_key=None):
        self.dropped += 1
        self.incomplete.add(state_key or self.current)
        try:
            self.logger.warning("DELIVERY_LEDGER_SHADOW error=%s coverage=incomplete", type(error).__name__)
        except Exception:
            pass

    def scope(self, state_key=None):
        return partial(self.capture, state_key=state_key or self.current)

    @isolated
    def bind(self, context, seed, generation):
        room = RoomKey(seed, integer(context.team), integer(context.slot), generation)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", seed) or not re.fullmatch(r"[0-9a-f]{64}", generation):
            raise InvalidRecord("invalid shadow room identity")
        key = context.state_key
        self.current = key
        self.keys[key] = room
        self.rooms[key] = {"room": room, "key": key, "connection": uuid4().hex, "binding": None,
                           "history": [], "boundary": integer(context.items_processed), "plans": {},
                           "captured_count": 0, "projection_count": 0, "history_observed": False, "groups": {},
                           "receipt_boundary": 0,
                           "uncertain": set(), "effect_gaps": 0, "native": set(), "mismatches": Counter(), "samples": [],
                           "ammo_gaps": 0, "checks": {}}
        if context.items_processed or None in self.incomplete:
            self.incomplete.add(key)
        self.storage_keys[f"doom_eap:{key}:ammo_refill_consumed"] = key
        self.storage_keys[f"doom_eap:{key}:ammo_refill_discarded"] = key
        self.capture("binding", state_key=key, baseline=context.items_processed)

    @isolated
    def history(self, context, start):
        key = context.state_key
        state = self.rooms[key]
        history = context.items_received
        offset = state["captured_count"]
        if start == 0:
            offset = 0
            state["projection_count"] = 0
        receipts = tuple(NetworkReceipt(item.item, item.location, item.player, item.flags)
                         for item in history[offset:])
        for receipt in receipts:
            self._validate_receipt(receipt)
        if len(history) < len(state["history"]):
            self.incomplete.add(key)
        self.capture("history", state_key=key, start=offset, receipts=receipts)
        state["captured_count"] = len(history)

    @staticmethod
    def _validate_receipt(receipt):
        for value in (receipt.item, receipt.location, receipt.player, receipt.flags):
            if type(value) is not int:
                raise InvalidRecord("invalid accepted receipt")
        integer(receipt.player)
        integer(receipt.flags)

    @isolated
    def projection(self, context, observation):
        state = self.rooms[context.state_key]
        start = state["projection_count"]
        ids = observation.receipt_item_ids
        self.capture("ownership", state_key=context.state_key, item_ids=ids[start:], start=start,
                     count=len(ids), boundary=observation.processed_boundary)
        state["projection_count"] = len(ids)

    @isolated
    def context(self, context, kind):
        self.capture(kind, state_key=context.state_key, map_name=context.current_map_name,
                     save_slot=context.active_save_slot, lease=context.published_materialization_lease,
                     context_identity=context.context_identity, load=context.native_gameplay_epoch,
                     save_identity=context.active_save_token, evidence_epoch=context.active_native_evidence_epoch)

    @isolated
    def native(self, context, kind, result, receipt_index=None, identity=None):
        fields = {key: result.get(key) for key in
                  ("outcome", "flags", "operations_applied", "namespace", "request_id", "build_id")}
        self.capture(kind, state_key=context.state_key, result=fields, receipt_index=receipt_index,
                     identity=identity)

    @isolated
    def commit(self, kind, *, state, reason, boundary, success):
        sessions = state.get("sessions", {})
        facts = {key: {"boundary": session.get("processed_items", 0),
                       "groups": session.get("item_command_groups", {})}
                 for key, session in sessions.items() if key in self.keys}
        self.capture(kind, reason=reason, boundary=boundary, success=success, sessions=facts)

    @isolated
    def package(self, context, cmd, args):
        if cmd in ("Connected", "RoomUpdate"):
            self.capture("check_ack", state_key=context.state_key,
                         locations=tuple(context.checked_locations))
        elif cmd == "SetReply":
            key = args.get("key")
            if key in self.storage_keys:
                self.capture("storage_reply", state_key=self.storage_keys[key], storage_key=key,
                             value=args.get("value"), original_value=args.get("original_value"))
        elif cmd == "Retrieved":
            for key, value in args.get("keys", {}).items():
                if key in self.storage_keys:
                    self.capture("storage_read", state_key=self.storage_keys[key], storage_key=key, value=value)

    @isolated
    def capture(self, kind, *, state_key=None, **facts):
        if "storage_key" in facts:
            state_key = self.storage_keys.get(facts["storage_key"])
        key = state_key or self.current
        if key is None:
            return
        if key not in self.rooms:
            raise InvalidRecord("shadow room is unavailable")
        if len(self.pending) >= self.capacity:
            raise InvalidRecord("shadow observation queue is full")
        evidence = snapshot(facts)
        self._validate_facts(evidence)
        if "payload" in evidence:
            if kind == "receipt_publication":
                evidence["optional"] = evidence.get("optional", False) or evidence["payload"].startswith("ai_ScriptCmdEnt ap_notify_item_")
            evidence["payload_hash"] = sha256(evidence.pop("payload").encode()).hexdigest()
        self.sequence += 1
        self.pending.append((key, self.rooms[key], self.sequence, kind, evidence))
        self._start()

    @staticmethod
    def _validate_facts(facts):
        for name in ("boundary", "item_index", "ordinal", "next_command"):
            if name in facts:
                integer(facts[name])
        if "accepted" in facts and type(facts["accepted"]) is not bool:
            raise InvalidRecord("shadow acceptance must be a boolean")

    def _start(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.incomplete.add(self.current)
            return
        if self.task is None or self.task.done():
            self.task = loop.create_task(self._drain())

    async def _open(self):
        if self.ledger is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                self.ledger = await Ledger.open(self.path)
            else:
                self.ledger = await Ledger.create(self.path)

    async def _binding(self, state):
        if state["binding"] is None:
            existing = await self.ledger.room(state["room"])
            revision = None
            if existing is not None:
                self.incomplete.add(state["key"])
                revision = existing.revision
            state["binding"] = await self.ledger.bind(state["room"], state["connection"], compatibility_revision=1,
                                                      expected_revision=revision)
        return state["binding"]

    async def _drain(self):
        while self.pending:
            key, state, sequence, kind, facts = self.pending.popleft()
            try:
                await self._open()
                binding = await self._binding(state)
                await self._record(binding, state, sequence, kind, facts)
                self.watermark = sequence
                await self._compare(key, state, sequence, binding)
            except Exception as error:
                if isinstance(error, HistoryConflict):
                    self._mismatch(state, "receipts", sequence)
                self.incomplete.add(key)
                self.failure(error)
                await self._failed_summary(key, state, sequence)
            await asyncio.sleep(0)

    async def _record(self, binding, state, sequence, kind, facts):
        if kind == "history":
            await self._history(binding, state, facts)
        observation = Observation(self.run, sequence * 2, kind, None, None, canonical(facts))
        await self.ledger.observe(binding, (observation,))
        stored = await self.ledger.observations(binding.room, self.run, sequence * 2 - 1, 1)
        if stored != (observation,):
            self._mismatch(state, "facts", sequence)
        self._facts(state, kind, facts)

    async def _history(self, binding, state, facts):
        start = facts["start"]
        receipts = tuple(NetworkReceipt(**item) for item in facts["receipts"])
        for offset in range(0, len(receipts), 256):
            batch = receipts[offset:offset + 256]
            await self.ledger.ingest(binding, start + offset, batch)
            await asyncio.sleep(0)
        old = state["history"]
        overlap = old[start:start + len(receipts)]
        if overlap != list(receipts[:len(overlap)]):
            self._mismatch(state, "receipts", start)
        state["history"][start:start + len(receipts)] = receipts
        state["history_observed"] = True

    def _facts(self, state, kind, facts):
        handlers = {"ownership": self._ownership, "compiled_plan": self._plan,
                    "receipt_publication": self._publication, "legacy_commit": self._commit_fact,
                    "cursor": self._cursor, "native_receipt": self._native_fact,
                    "spool_publication": self._spool_fact, "group_progress": self._progress,
                    "group_completion": self._completion, "ammo_legacy_authorization": self._ammo_authorization,
                    "check_sent": self._check_sent, "check_ack": self._check_ack, "spool_payload": self._envelope}
        handler = handlers.get(kind)
        if handler is not None:
            handler(state, facts)

    def _ownership(self, state, facts):
        expected = [item.item for item in state["history"][facts["start"]:facts["count"]]]
        if facts["item_ids"] != expected:
            self._mismatch(state, "ownership", facts["boundary"])

    def _plan(self, state, facts):
        from .receipt_publication import receipt_command_id

        commands = facts["commands"]
        if commands is None:
            return
        for ordinal, payload in enumerate(commands):
            command_id = receipt_command_id(state["key"], facts["item_id"], facts["item_index"], ordinal, payload)
            envelope = "AP_EXECUTION_CLASS_V1 PLAYER_RUNTIME\n"
            if facts.get("lease") is not None:
                envelope += f"AP_MATERIALIZATION_LEASE_V1 {facts['lease']}\n"
            envelope += payload.strip() + "\n"
            state["plans"][command_id] = {"hash": sha256(payload.encode()).hexdigest(), "ordinal": ordinal,
                                          "lease": facts.get("lease"), "index": facts["item_index"],
                                          "envelope": sha256(envelope.encode()).hexdigest()}

    def _publication(self, state, facts):
        command_id = facts["command_id"]
        expected = state["plans"].get(command_id)
        if expected is None:
            self.incomplete.add(state["key"])
        elif expected["hash"] != facts["payload_hash"]:
            self._mismatch(state, "payload", command_id)
        self._publication_scope(state, expected, facts)
        if facts["accepted"] and not facts.get("optional", False):
            self._uncertain(state, facts["item_index"])

    @staticmethod
    def _uncertain(state, index):
        if index not in state["uncertain"] and index not in state["native"] and index < state["boundary"]:
            state["effect_gaps"] += 1
        state["uncertain"].add(index)

    def _publication_scope(self, state, expected, facts):
        if expected is None or facts.get("optional", False):
            return
        if "ordinal" in facts and expected["ordinal"] != facts["ordinal"]:
            self._mismatch(state, "ordinal", facts["command_id"])
        if "scope" in facts and expected["lease"] != facts["scope"]["materialization_lease"]:
            self._mismatch(state, "lease", facts["command_id"])

    def _envelope(self, state, facts):
        expected = state["plans"].get(facts["command_id"])
        if expected is not None and expected["envelope"] != facts["payload_hash"]:
            self._mismatch(state, "envelope", facts["command_id"])

    def _commit_fact(self, state, facts):
        if not facts["success"]:
            return
        for key, session in facts["sessions"].items():
            if self.keys[key] == state["room"]:
                self._legacy_boundary(state, integer(session["boundary"]))
                state["groups"] = session["groups"]

    @staticmethod
    def _legacy_boundary(state, boundary):
        previous = state["boundary"]
        delta = 1 if boundary > previous else -1
        for index in range(min(previous, boundary), max(previous, boundary)):
            if index in state["uncertain"] and index not in state["native"]:
                state["effect_gaps"] += delta
        state["boundary"] = boundary

    def _spool_fact(self, state, facts):
        requested = facts.get("requested_id")
        if requested in state["plans"] and requested != facts["command_id"]:
            self._mismatch(state, "command_id", facts["command_id"])
        expected = state["plans"].get(facts["command_id"])
        if expected is not None and expected["hash"] != facts["payload_hash"]:
            self._mismatch(state, "spool_payload", facts["command_id"])
        if facts["evidence"] == "effect_unknown_hold":
            self.incomplete.add(state["key"])

    def _progress(self, state, facts):
        group = state["groups"].get(str(facts["item_index"]))
        if group is None or group.get("next_command") != facts["next_command"]:
            self._mismatch(state, "group_progress", facts["item_index"])

    def _completion(self, state, facts):
        if str(facts["item_index"]) in state["groups"]:
            self._mismatch(state, "group_completion", facts["item_index"])

    @staticmethod
    def _ammo_authorization(state, facts):
        if not facts["correlated"]:
            state["ammo_gaps"] += 1

    @staticmethod
    def _check_sent(state, facts):
        for location in facts["locations"]:
            state["checks"].setdefault(integer(location), "sent")

    @staticmethod
    def _check_ack(state, facts):
        for location in facts["locations"]:
            state["checks"][integer(location)] = "acknowledged"

    def _cursor(self, state, facts):
        if facts["boundary"] != state["boundary"]:
            self._mismatch(state, "legacy_boundary", facts["boundary"])

    def _native_fact(self, state, facts):
        index = facts["receipt_index"]
        if facts.get("result_kind") == "weapon_points_delivered":
            if index in state["uncertain"] and index not in state["native"] and index < state["boundary"]:
                state["effect_gaps"] -= 1
            state["native"].add(index)
        else:
            self._uncertain(state, index)

    @staticmethod
    def _mismatch(state, kind, sample):
        state["mismatches"][kind] += 1
        if len(state["samples"]) < 8:
            state["samples"].append(str(sample)[:96])

    async def _compare(self, key, state, sequence, binding):
        room = await self.ledger.room(binding.room)
        state["receipt_boundary"] = room.receipt_boundary
        if state["boundary"] > room.receipt_boundary:
            self.incomplete.add(key)
        if state["history_observed"]:
            self._compare_boundary(key, state, sequence, room.receipt_boundary)
        summary = self.summary(key, state, sequence)
        self.last_summary = summary
        await self.ledger.observe(binding, (Observation(self.run, sequence * 2 + 1, "comparison", None, None,
                                                      canonical(summary)),))
        self.logger.info("DELIVERY_LEDGER_COMPARE %s", canonical(summary).decode())

    def _compare_boundary(self, key, state, sequence, boundary):
        if boundary < len(state["history"]):
            self._mismatch(state, "receipt_boundary", sequence)
        elif boundary > len(state["history"]):
            self.incomplete.add(key)

    def summary(self, key, state, sequence):
        gaps = state["effect_gaps"] + state["ammo_gaps"]
        coverage = key not in self.incomplete and not gaps
        status = self._status(state, coverage, sequence)
        return {"room": asdict(state["room"]), "run": self.run, "watermark": sequence,
                "observed_watermark": self.sequence, "coverage": "complete" if coverage else "incomplete",
                "status": status, "mismatches": dict(state["mismatches"]), "samples": state["samples"][:],
                "dropped": self.dropped, "legacy_boundary": state["boundary"],
                "receipt_boundary": state["receipt_boundary"], "legacy_safety_gaps": gaps,
                "ingestion_lead": max(0, state["receipt_boundary"] - state["boundary"])}

    async def _failed_summary(self, key, state, sequence):
        try:
            self.last_summary = self.summary(key, state, self.watermark)
            if state["binding"] is not None:
                await self.ledger.observe(state["binding"], (Observation(
                    self.run, sequence * 2 + 1, "comparison", None, None, canonical(self.last_summary)),))
        except Exception:
            pass
        try:
            self.logger.info("DELIVERY_LEDGER_COMPARE %s", canonical(self.last_summary).decode())
        except Exception:
            pass

    def _status(self, state, coverage, sequence):
        if state["mismatches"]:
            return "divergent"
        if not coverage:
            return "incomplete"
        if sequence < self.sequence:
            return "lagging"
        return "match"

    async def flush(self):
        if not self.enabled:
            return
        try:
            self._start()
            if self.task is not None:
                await asyncio.shield(self.task)
        except Exception as error:
            self.failure(error)

    async def close(self):
        await self.flush()
        if self.ledger is not None:
            try:
                await self.ledger.close()
            except Exception as error:
                self.failure(error)


def ignore(*args, **kwargs):
    return None
