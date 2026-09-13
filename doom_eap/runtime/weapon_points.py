"""Spendable WUP receipt ownership and the typed Sentinel Game Link adapter."""
from __future__ import annotations

import hashlib
import json
import secrets
import struct
import subprocess
import time
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
    def __init__(self, probe: Path, pid: int, namespace: str):
        self.probe, self.pid, self.namespace = probe, pid, namespace

    def _run(self, args, payload=None):
        try:
            process = subprocess.run([str(self.probe), *args], input=payload,
                                     capture_output=True, timeout=4, check=False)
            result = json.loads(process.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise WeaponPointsBlocked(f"Sentinel transport unavailable: {error}") from error
        if process.returncode or result.get("result") != "ok":
            raise WeaponPointsBlocked(f"Sentinel refused: {result}")
        if result.get("core_version") != "0.8.0":
            raise WeaponPointsBlocked("Weapon Points require the Core 0.8.0 candidate")
        return result

    def _execute(self, amount=0, expected=0):
        scope = self._run(["--pid", str(self.pid), "--native", "--json"])
        if scope.get("availability") != "enabled" or scope.get("lifecycle") != "active":
            raise WeaponPointsBlocked("Native gameplay context is not active")
        request_id, nonce = secrets.randbits(64) or 1, secrets.token_bytes(16)
        payload = struct.pack("<QIQ16sQQ16sI", 2048, self.pid,
                              int(scope["process_created"]), bytes.fromhex(scope["instance_id"]),
                              int(scope["lifecycle_generation"]), request_id, nonce, 2000)
        payload += self.namespace.encode("ascii") + b"\0" + struct.pack("<III", int(amount != 0), amount, expected)
        deadline = time.monotonic() + 5
        operation = 18
        while True:
            message = struct.pack("<IHHII", 0x50494353, 1, operation, len(payload), 0) + payload
            result = self._run(["--weapon-points"], message)
            if (result.get("namespace") != self.namespace or int(result["request_id"]) != request_id
                    or result.get("build_id") != scope.get("build_id")):
                raise WeaponPointsBlocked("Sentinel response identity changed")
            if result["state"] in (3, 4, 5, 6):
                # The native result is now consumed. Release only its retained
                # queue slot; the pre-grant durable receipt intent remains ours.
                release = struct.pack("<IHHII", 0x50494353, 1, 21, len(payload), 0) + payload
                try:
                    self._run(["--weapon-points"], release)
                except WeaponPointsBlocked:
                    pass  # Default Core retention still expires a lost release.
            if result["state"] == 3:
                if result["outcome"] not in (1, 2) or not result["flags"] & 16:
                    raise WeaponPointsBlocked(f"Native WUP execution failed: {result}")
                return result
            if result["state"] not in (1, 2) or time.monotonic() >= deadline:
                # Never resubmit on an uncertain result. A future pass observes
                # saved/native gained against the already durable receipt intent.
                raise WeaponPointsBlocked(f"Native WUP execution not confirmed: {result}")
            operation = 19
            time.sleep(0.025)

    def observe(self):
        return self._execute()

    def admitted_save_root(self):
        """Observe the exact provider admitted for this AP identity; never scan vanilla."""
        facts = self._run(["--pid", str(self.pid), "--save-admission", "--json"])
        if (facts.get("namespace_id") != self.namespace or not facts.get("accepting_requests")
                or not facts.get("route_retained") or facts.get("state") != "admitted"
                or facts.get("native_root") != "ap-" + self.namespace[:40]):
            raise WeaponPointsBlocked("AP save provider is not admitted for this identity")
        return facts["native_root"]

    def grant_weapon_upgrade_points(self, amount, expected_gained):
        if type(amount) is not int or amount <= 0 or amount > 117:
            raise WeaponPointsBlocked("Invalid WUP amount")
        return self._execute(amount, expected_gained)


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
