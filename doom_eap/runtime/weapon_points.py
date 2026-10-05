"""Spendable WUP receipt ownership and the typed Sentinel Game Link adapter."""
from __future__ import annotations
import re

import hashlib
import json
import os
import secrets
import struct
import subprocess
import time
from doom_eap.contracts.core_distribution import version_key
from pathlib import Path

ITEM_ID = 7770903
AMOUNT = 3


class WeaponPointsBlocked(RuntimeError):
    pass


def namespace_id(seed: str, team: int, slot: int, fingerprint: str) -> str:
    if not isinstance(seed, str) or not seed or type(team) is not int or not 0 <= team <= 0xffffffff or type(slot) is not int or not 1 <= slot <= 0xffffffff:
        raise WeaponPointsBlocked("Incomplete AP identity")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
        raise WeaponPointsBlocked("Missing generated native save fingerprint")
    digest = hashlib.sha256(b"sentinel-ap-session-identity")
    for field in (seed, str(team), str(slot), fingerprint):
        encoded = field.encode("utf-8")
        digest.update(struct.pack(">I", len(encoded)))
        digest.update(encoded)
    return digest.hexdigest()


class SentinelWeaponPoints:
    """Uses the existing process-authenticated inspection transport and queue."""
    def __init__(self, probe: Path, pid: int, namespace: str, diagnostic=None, *, command_prefix=(), environment=None):
        self.probe, self.pid, self.namespace = probe.resolve(), pid, namespace
        self.diagnostic = diagnostic
        self.command_prefix, self.environment = command_prefix, environment
        self._probe_sha256 = None
        self._diagnostic_signatures = {}

    def _diagnose(self, evidence):
        if self.diagnostic:
            key = (evidence["stage"], evidence["operation"])
            try:
                response = json.loads(evidence.get("stdout", "{}"))
            except ValueError:
                response = evidence.get("stdout")
            def semantic(value):
                if isinstance(value, dict):
                    return {key: (item not in (0, "0", None) if key == "event_gap_count" else semantic(item)) for key, item in value.items()
                            if key not in {"sequence", "timestamp_ms", "at_monotonic", "sampled_at_ms",
                                "event_sequence", "callback_sequence", "callback_at_ms", "context_sampled_at_ms",
                                "history_oldest", "history_overwritten", "events", "at_ms"}}
                if isinstance(value, list):
                    return [semantic(item) for item in value]
                return value
            signature = json.dumps((evidence.get("predicate"), evidence.get("error"),
                evidence.get("returncode"), evidence.get("argv"), semantic(response)), sort_keys=True)
            now = time.monotonic()
            previous = self._diagnostic_signatures.get(key)
            if previous is None or previous["signature"] != signature:
                row = {"signature": signature, "first_seen": now, "last_seen": now, "count": 1, "emitted_at": now}
                first_error = previous.get("first_error") if previous else None
                if first_error is None and (evidence.get("error") or evidence.get("returncode", 0)):
                    first_error = {**evidence, "at_monotonic": now}
                if first_error is not None:
                    row["first_error"] = first_error
                self._diagnostic_signatures[key] = row
                self.diagnostic({**evidence, "first_seen": now, "last_seen": now, "count": 1})
            else:
                previous.update(last_seen=now, count=previous["count"] + 1)
                if now - previous["emitted_at"] >= 60:
                    self.diagnostic({"stage": key[0], "operation": key[1], "summary": "unchanged",
                        **{field: previous[field] for field in ("first_seen", "last_seen", "count")}})
                    previous["emitted_at"] = now

    def _run(self, args, payload=None):
        evidence = {"executable": str(self.probe), "argv": [str(self.probe), *args],
                    "cwd": os.getcwd(), "stage": args[2] if args[0] == "--pid" else args[0],
                    "operation": struct.unpack_from("<H", payload, 6)[0] if payload else None}
        try:
            if self._probe_sha256 is None:
                self._probe_sha256 = hashlib.sha256(self.probe.read_bytes()).hexdigest()
            evidence["sha256"] = self._probe_sha256
            executable = str(self.probe)
            if self.command_prefix:
                from doom_eap.runtime.proton import windows_path
                executable = windows_path(self.probe)
            process = subprocess.run([*self.command_prefix, executable, *args], input=payload,
                                     capture_output=True, timeout=4, check=False, env=self.environment)
            evidence.update(returncode=process.returncode,
                            stdout=process.stdout.decode("utf-8", errors="replace"),
                            stderr=process.stderr.decode("utf-8", errors="replace"))
            result = json.loads(process.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            evidence.update(predicate="transport_or_json", error=str(error))
            self._diagnose(evidence)
            raise WeaponPointsBlocked(f"Sentinel transport unavailable: {error}") from error
        try:
            supported = result.get("core_version") in {"0.8.0", "0.9.0"} or version_key(result.get("core_version"))[:3] == (1, 0, 0)
        except (ValueError, TypeError):
            supported = False
        predicate = ("exit_or_result" if process.returncode or result.get("result") != "ok"
                     else "core_version_supported" if not supported else None)
        evidence["predicate"] = predicate
        self._diagnose(evidence)
        if predicate:
            error = WeaponPointsBlocked(f"Sentinel response validation failed: {json.dumps(evidence)}")
            error.probe_result = result.get("result")
            raise error
        return result

    def _execute(self, amount=0, expected=0):
        body = struct.pack("<III", int(amount != 0), amount, expected)
        return self._execute_typed(2048, "--weapon-points", 18, 19, 21, body)

    def _execute_typed(self, capability, flag, submit, result_operation, release_operation, body, *, trace=None):
        scope = self._run(["--pid", str(self.pid), "--native", "--json"])
        if trace is not None:
            trace.append({"stage": "scope", "response": scope})
        if scope.get("availability") != "enabled" or scope.get("lifecycle") != "active":
            raise WeaponPointsBlocked("Native gameplay context is not active")
        identity = (scope["process_created"], scope["instance_id"], scope["build_id"])
        if getattr(self, "_runtime_identity", None) != identity:
            self._runtime_identity, self._incompatible_capabilities = identity, set()
            self._diagnostic_signatures.clear()
        if capability in self._incompatible_capabilities:
            raise WeaponPointsBlocked("Native probe response incompatible with this runtime identity")
        request_id, nonce = secrets.randbits(64) or 1, secrets.token_bytes(16)
        payload = struct.pack("<QIQ16sQQ16sI", capability, self.pid,
                              int(scope["process_created"]), bytes.fromhex(scope["instance_id"]),
                              int(scope["lifecycle_generation"]), request_id, nonce, 2000)
        payload += self.namespace.encode("ascii") + b"\0" + body
        deadline = time.monotonic() + 5
        operation = submit
        while True:
            message = struct.pack("<IHHII", 0x50494353, 1, operation, len(payload), 0) + payload
            try:
                result = self._run([flag], message)
            except WeaponPointsBlocked as error:
                if trace is not None:
                    trace.append({"stage": "transport", "operation": operation, "error": str(error)})
                if operation != release_operation:
                    release = struct.pack("<IHHII", 0x50494353, 1, release_operation, len(payload), 0) + payload
                    try:
                        cleaned = self._run([flag], release)
                        if trace is not None:
                            trace.append({"stage": "cleanup", "operation": release_operation, "response": cleaned})
                    except WeaponPointsBlocked as cleanup_error:
                        if trace is not None:
                            trace.append({"stage": "cleanup", "operation": release_operation, "error": str(cleanup_error)})
                if getattr(error, "probe_result", None) == "invalid_response":
                    self._incompatible_capabilities.add(capability)
                raise
            if trace is not None:
                trace.append({"stage": "response", "operation": operation,
                              "request_id": request_id, "nonce": nonce.hex(),
                              "response": result, "at_monotonic": time.monotonic()})
            if (result.get("namespace") != self.namespace or int(result["request_id"]) != request_id
                    or result.get("build_id") != scope.get("build_id")):
                raise WeaponPointsBlocked("Sentinel response identity changed")
            if result["state"] in (3, 4, 5, 6):
                # Release the retained queue slot; durable receipt intent belongs to the caller.
                release = struct.pack("<IHHII", 0x50494353, 1, release_operation, len(payload), 0) + payload
                try:
                    released = self._run([flag], release)
                    if trace is not None:
                        trace.append({"stage": "release", "operation": release_operation, "response": released})
                except WeaponPointsBlocked as error:
                    if trace is not None:
                        trace.append({"stage": "release", "operation": release_operation, "error": str(error)})
            if result["state"] == 3:
                return result
            if result["state"] not in (1, 2) or time.monotonic() >= deadline:
                # Never resubmit on an uncertain result. A future pass observes
                # saved/native gained against the already durable receipt intent.
                raise WeaponPointsBlocked(f"Native {flag[2:]} execution not confirmed: {result}")
            operation = result_operation
            time.sleep(0.025)

    def observe(self):
        result = self._execute()
        if result["outcome"] not in (1, 2) or not result["flags"] & 16:
            raise WeaponPointsBlocked(f"Native WUP observation failed: {result}")
        return result

    def admitted_save_root(self):
        """Observe the exact provider admitted for this AP identity; never scan vanilla."""
        facts = self._run(["--pid", str(self.pid), "--save-admission", "--json"])
        if (facts.get("namespace_id") != self.namespace or not facts.get("accepting_requests")
                or not facts.get("route_retained") or facts.get("state") != "admitted"
                or not re.fullmatch(r"ap-[0-9a-f]{40}", str(facts.get("native_root", "")))):
            raise WeaponPointsBlocked("AP save provider is not admitted for this identity")
        self.save_admission = facts
        return facts["native_root"]

    def grant_weapon_upgrade_points(self, amount, expected_gained):
        if type(amount) is not int or amount <= 0 or amount > 117:
            raise WeaponPointsBlocked("Invalid WUP amount")
        result = self._execute(amount, expected_gained)
        if result["outcome"] not in (1, 2) or not result["flags"] & 16:
            raise WeaponPointsBlocked(f"Native WUP execution failed: {result}")
        return result

    def ensure_meat_hook(self):
        body = struct.pack("<IIIIBBHIB3s", 1, 1 << 8, 0, 0, 0, 0, 0, 0, 0, b"\0\0\0")
        result = self._execute_typed(16384, "--arsenal", 29, 30, 32, body)
        if (result["outcome"] not in (0, 1) or result["flags"] & 26 != 26
                or not result["mods_after"] & (1 << 8)):
            raise WeaponPointsBlocked(f"Native Meat Hook authorization failed: {result}")
        return result

    def ensure_progressive_special_weapon(self, count):
        if type(count) is not int or not 1 <= count <= 3:
            raise WeaponPointsBlocked("Invalid Progressive Special Weapon count")
        own_hammer = int(count >= 2)
        hammer_tier = 2 if count >= 3 else own_hammer
        body = struct.pack("<IIIIIIIQII", 1, 1, own_hammer, hammer_tier, 0, 0, 0, 0, 0, 0)
        result = self._execute_typed(65536, "--special", 37, 38, 40, body)
        required_known = 1 | (2 if own_hammer else 0) | (4 if hammer_tier == 2 else 0)
        required_flags = 34 | (64 if result["outcome"] == 1 else 0)
        if (result["outcome"] not in (0, 1) or result["flags"] & required_flags != required_flags
                or result["owns_crucible"] != 1 or result["native_crucible"] != 1
                or result["owns_hammer"] < own_hammer or result["native_hammer"] < own_hammer
                or result["hammer_tier"] < hammer_tier
                or (hammer_tier == 2 and result["native_hammer_perks"] != 3)
                or result["native_state_known"] & required_known != required_known):
            raise WeaponPointsBlocked(f"Native Special ownership failed: {result}")
        # NOOP performs no native mutation, independently of selection knowledge.
        # A mutating acquisition still requires its preservation postconditions.
        if ((result["outcome"] == 1 and result["flags"] & 4)
                or (result["outcome"] == 0 and (result["flags"] & 8 != 8
                    or (result["flags"] & 4096 and not result["flags"] & 8192)))):
            raise WeaponPointsBlocked(f"Native Special ownership confirmed; preservation unconfirmed: {result}")
        return result

    def select_special_weapon(self, selected):
        if type(selected) is not int or selected not in (1, 2):
            raise WeaponPointsBlocked("Invalid Special selection")
        body = struct.pack("<IIIIIIIQII", 2, 0, 0, 0, selected, 0, 0, 0, 0, 0)
        result = self._execute_typed(65536, "--special", 37, 38, 40, body)
        if (result["outcome"] not in (0, 1) or not result["flags"] & 4096
                or not result["native_state_known"] & 8 or result["native_selected"] != selected):
            raise WeaponPointsBlocked(f"Special route selection unconfirmed: {result}")
        return result

    def publish_ammo_refill(self, snapshot, *, connected):
        authoritative = snapshot.get("authoritative") is True
        available = snapshot.get("available")
        known = authoritative and type(available) is int and 0 <= available <= 3
        balance = available if known else 0
        flags = int(bool(connected)) | (2 if authoritative else 0) | (4 if known else 0)
        body = struct.pack("<IIIIIIIQII", 4, 0, 0, 0, 0, balance, flags, 0, 0, 0)
        result = self._execute_typed(65536, "--special", 37, 38, 40, body)
        if (result["outcome"] not in (0, 1) or result.get("kind") != 4
                or result.get("refill_balance") != balance or result.get("refill_flags") != flags
                or result["flags"] & 4):
            raise WeaponPointsBlocked(f"Ammo presentation publication unconfirmed: {result}")
        return result

    def observe_blood_punch(self):
        body = struct.pack("<5I4B", *([0] * 9))
        result = self._execute_typed(8192, "--inventory", 25, 26, 28, body)
        if (result.get("inventory_abi") not in (4, 5) or result.get("outcome") != 1
                or result.get("flags", 0) & 3 != 3 or result.get("upgrades_after") == 0xffffffff):
            raise WeaponPointsBlocked(f"Blood Punch native observation is unknown or incompatible: {result}")
        return (result["upgrades_after"] >> 11) & 15

    def observe_inventory(self):
        body = struct.pack("<5I4B", *([0] * 9))
        result = self._execute_typed(8192, "--inventory", 25, 26, 28, body)
        if result.get("inventory_abi") == 4:
            result["dash_before"] = result["dash_after"] = 255
        if (result.get("inventory_abi") not in (4, 5) or result.get("kind") != 0
                or result.get("outcome") != 1 or result.get("flags", 0) & 3 != 3
                or result.get("ice_bomb_after") not in (0, 1, 255)
                or result.get("dash_after") not in (0, 1, 255)):
            raise WeaponPointsBlocked(f"Native inventory observation is unknown or incompatible: {result}")
        return result

    def ensure_masteries(self, mask):
        if type(mask) is not int or not 0 < mask <= 0x1FFF:
            raise WeaponPointsBlocked("Invalid Arsenal mastery mask")
        body = struct.pack("<IIIIBBHIB3s", 4, 0, 0, mask, 0, 0, 0, 0, 0, b"\0\0\0")
        result = self._execute_typed(16384, "--arsenal", 29, 30, 32, body)
        if (result["outcome"] not in (0, 1, 2) or result["flags"] & 27 != 27
                or (result["outcome"] == 2 and not result["flags"] & 32)
                or result["masteries_ap_after"] & mask != mask):
            raise WeaponPointsBlocked(f"Native Arsenal mastery projection unconfirmed: {result}")
        return result

    def ensure_normal_runes(self, mask):
        if type(mask) is not int or not 0 < mask <= 0x1FF:
            raise WeaponPointsBlocked("Invalid normal Rune mask")
        body = struct.pack("<IIIBb2s", 1, mask, 0, 0, -1, b"\0\0")
        result = self._execute_typed(32768, "--runes", 33, 34, 36, body)
        if (result["outcome"] not in (0, 1) or result["flags"] & 11 != 11
                or result["owned_normal_after"] & mask != mask
                or result["selected_slots_after"] != result["selected_slots_before"]):
            raise WeaponPointsBlocked(f"Native normal Rune registration unconfirmed: {result}")
        return result

    def publish_checked_locations(self, checked_locations, revision):
        if type(revision) is not int or revision <= 0:
            raise WeaponPointsBlocked("Invalid Automap snapshot revision")
        scope = self._run(["--pid", str(self.pid), "--native", "--json"])
        if scope.get("availability") != "enabled" or scope.get("lifecycle") != "active":
            raise WeaponPointsBlocked("Native gameplay context is not active")
        bits = [0] * 8
        for location_id in checked_locations:
            offset = int(location_id) - 7770000
            if 0 <= offset < 512:
                bits[offset // 64] |= 1 << (offset % 64)
        request_id, nonce = secrets.randbits(64) or 1, secrets.token_bytes(16)
        payload = struct.pack("<QIQ16sQQ16sI", 262144, self.pid,
                              int(scope["process_created"]), bytes.fromhex(scope["instance_id"]),
                              int(scope["lifecycle_generation"]), request_id, nonce, 2000)
        payload += self.namespace.encode("ascii") + b"\0" + struct.pack("<IIQ8Q", 1, 1, revision, *bits)
        message = struct.pack("<IHHII", 0x50494353, 1, 45, len(payload), 0) + payload
        result = self._run(["--automap"], message)
        if (result.get("namespace") != self.namespace or int(result.get("request_id", 0)) != request_id
                or result.get("build_id") != scope.get("build_id") or result.get("outcome") != 0
                or result.get("known") != 1 or int(result.get("revision", 0)) != revision):
            raise WeaponPointsBlocked(f"Native Automap snapshot refused: {result}")
        return result


class WeaponPointReceipts:
    """Durable ordered grant intents, independent of ReceiptSession's cursor.

    Native cumulative gained is a precondition/result fact, never a desired
    current balance. Native saves restore gained, current and purchases together.
    An older checkpoint can need missing grants; previously spent points whose
    gained count is present are never re-created. Core owns no receipt history.
    """
    def __init__(self, session_state, persist, link, namespace):
        self.state, self.persist, self.link, self.namespace = session_state, persist, link, namespace

    def _ledger(self):
        ledger = self.state.get("weapon_points")
        if ledger is None:
            facts = self.link.observe()
            if facts["gained_after"] != 0:
                raise WeaponPointsBlocked("Unowned native WUP history; refusing to reconstruct a lost receipt ledger")
            ledger = {"version": 1, "namespace": self.namespace, "receipts": []}
            self.state["weapon_points"] = ledger
            try:
                self.persist()
            except Exception:
                self.state.pop("weapon_points", None)
                raise
        if (not isinstance(ledger, dict) or ledger.get("version") != 1
                or ledger.get("namespace") != self.namespace or not isinstance(ledger.get("receipts"), list)):
            raise WeaponPointsBlocked("Incompatible WUP receipt ownership")
        previous = -1
        for record in ledger["receipts"]:
            if (not isinstance(record, dict) or set(record) != {"index", "receipt", "amount"} or type(record["index"]) is not int
                    or record["index"] <= previous or not isinstance(record["receipt"], str)
                    or not record["receipt"] or record["amount"] != AMOUNT):
                raise WeaponPointsBlocked("Invalid WUP receipt ledger")
            previous = record["index"]
        return ledger

    def reconcile(self, authoritative):
        ledger = self._ledger()
        for record in ledger["receipts"]:
            if authoritative.get(record["index"]) != record["receipt"]:
                raise WeaponPointsBlocked("AP history does not own the persisted WUP receipt")
        gained = self.link.observe()["gained_after"]
        total = sum(record["amount"] for record in ledger["receipts"])
        if gained > total or gained % AMOUNT:
            raise WeaponPointsBlocked("Native earned WUP disagrees with receipt ownership")
        while gained < total:
            result = self.link.grant_weapon_upgrade_points(AMOUNT, gained)
            if result["gained_before"] != gained or result["gained_after"] != gained + AMOUNT:
                raise WeaponPointsBlocked("Native grant result does not match the pending receipt")
            gained += AMOUNT

    def deliver(self, index, receipt, authoritative):
        ledger = self._ledger()
        if authoritative.get(index) != receipt or not receipt:
            raise WeaponPointsBlocked("WUP receipt is not authoritative")
        existing = next((r for r in ledger["receipts"] if r["index"] == index), None)
        if existing is not None:
            if existing["receipt"] != receipt:
                raise WeaponPointsBlocked("WUP receipt changed at an existing index")
        else:
            if ledger["receipts"] and ledger["receipts"][-1]["index"] >= index:
                raise WeaponPointsBlocked("WUP receipt order changed")
            ledger["receipts"].append({"index": index, "receipt": receipt, "amount": AMOUNT})
            # This must succeed before any native mutation. A crash after it is
            # resolved using gained, even if the general processed cursor lags.
            try:
                self.persist()
            except Exception:
                ledger["receipts"].pop()
                raise
        self.reconcile(authoritative)
