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
                                "history_oldest", "history_overwritten", "events", "qualified_kills", "at_ms"}}
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
                                     capture_output=True, timeout=4, check=False, env=self.environment,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            evidence.update(returncode=process.returncode,
                            stdout=process.stdout.decode("utf-8", errors="replace"),
                            stderr=process.stderr.decode("utf-8", errors="replace"))
            result = json.loads(process.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            evidence.update(predicate="transport_or_json", error=str(error))
            self._diagnose(evidence)
            raise WeaponPointsBlocked(f"Sentinel transport unavailable: {error}") from error
        predicate = "exit_or_result" if process.returncode or result.get("result") != "ok" else None
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
                # Uncertain results must be checked against the persisted receipt before resubmission.
                raise WeaponPointsBlocked(f"Native {flag[2:]} execution not confirmed: {result}")
            operation = result_operation
            time.sleep(0.025)

    def observe_combat(self, mission_maps):
        if not re.fullmatch(r"[0-9a-f]{64}", self.namespace):
            raise WeaponPointsBlocked("Invalid combat namespace")
        if (not isinstance(mission_maps, (list, tuple, set, frozenset)) or not 1 <= len(mission_maps) <= 64
                or any(not isinstance(name, str) or not 1 <= len(name) <= 255
                       or not name.isascii() or "\0" in name for name in mission_maps)):
            raise WeaponPointsBlocked("Invalid combat mission maps")
        maps = sorted(set(mission_maps))
        body = struct.pack("<QIIH", 4194304, 2, self.pid, 64) + self.namespace.encode("ascii")
        body += struct.pack("<I", len(maps))
        for name in maps:
            encoded = name.encode("ascii")
            body += struct.pack("<H", len(encoded)) + encoded
        reply = self._run(["--combat"], struct.pack("<4sHHII", b"SCIP", 1, 50, len(body), 0) + body)
        try:
            def uint(value, bits=64):
                if type(value) is not int and not (isinstance(value, str) and re.fullmatch(r"[0-9]+", value)):
                    raise ValueError("invalid integer")
                number = int(value)
                if not 0 <= number < 2**bits:
                    raise ValueError("integer outside wire range")
                return number

            if uint(reply["combat_abi"], 32) != 2 or uint(reply["pid"], 32) != self.pid:
                raise ValueError("combat ABI or process mismatch")
            created = uint(reply["process_created"])
            instance = reply["instance_id"]
            build = reply["build_id"]
            if (not created or not isinstance(instance, str) or not re.fullmatch(r"[0-9a-f]{32}", instance)
                    or not isinstance(build, str) or not build or len(build) > 128):
                raise ValueError("invalid producer identity")
            if reply["namespace_id"] != self.namespace:
                raise ValueError("combat namespace mismatch")
            return {"producer": [str(self.pid), str(created), instance, build], "namespace_id": self.namespace,
                    "qualified_kills": uint(reply["qualified_kills"]),
                    "observation_gaps": uint(reply["observation_gaps"])}
        except (KeyError, TypeError, ValueError) as error:
            raise WeaponPointsBlocked(f"Invalid native combat observation: {error}") from error

    def publish_combat_progress(self, total, target, first_milestone=0, milestone=0,
                                milestone_step=0, visible=True):
        values = (total, target, first_milestone, milestone, milestone_step)
        if any(type(value) is not int or not 0 <= value < 2**64 for value in values) or type(visible) is not bool:
            raise WeaponPointsBlocked("Invalid combat presentation values")
        notices = (first_milestone, milestone, milestone_step)
        if (target and target <= total or any(notices) and not (
                0 < first_milestone <= milestone <= total and milestone_step > 0
                and (milestone - first_milestone) % milestone_step == 0)):
            raise WeaponPointsBlocked("Invalid combat presentation targets")
        scope = self._run(["--pid", str(self.pid), "--native", "--json"])
        if scope.get("availability") != "enabled" or scope.get("lifecycle") != "active":
            raise WeaponPointsBlocked("Native gameplay context is not active")
        request_id, nonce = secrets.randbits(64) or 1, secrets.token_bytes(16)
        try:
            payload = struct.pack("<QIQ16sQQ16sI", 8388608, self.pid,
                                  int(scope["process_created"]), bytes.fromhex(scope["instance_id"]),
                                  int(scope["lifecycle_generation"]), request_id, nonce, 1000)
            payload += struct.pack("<I65sQQQQQI", 1, self.namespace.encode("ascii"), *values, int(visible))
            message = struct.pack("<4sHHII", b"SCIP", 1, 51, len(payload), 0) + payload
            result = self._run(["--combat-presentation"], message)
            expected = (self.pid, int(scope["process_created"]), scope["instance_id"],
                        int(scope["lifecycle_generation"]), scope["build_id"], request_id, nonce.hex())
            actual = (int(result["pid"]), int(result["process_created"]), result["instance_id"],
                      int(result["lifecycle_generation"]), result["build_id"],
                      int(result["request_id"]), result["nonce"])
            if (actual != expected or result["combat_presentation_abi"] != 1 or result["status"] != 0
                    or tuple(int(result[key]) for key in ("total", "target", "milestone", "visible"))
                    != (total, target, milestone, int(visible))):
                raise ValueError("native acknowledgement does not match presentation")
        except (KeyError, TypeError, ValueError, OverflowError, struct.error) as error:
            raise WeaponPointsBlocked(f"Native combat presentation refused: {error}") from error
        return result

    def deathlink_request(self, kind, *, enabled=False, mode="soft", event_id=0,
                          event_hash=bytes(16), ack_sequence=0):
        body = struct.pack("<IIIIQ16sQI", kind, int(enabled), int(mode == "hardcore"),
                           0, event_id, event_hash, ack_sequence, 0)
        return self._execute_typed(131072, "--deathlink", 41, 42, 44, body)

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

    def ensure_progressive_special_weapon(self, count, *, hammer_only=False):
        if type(count) is not int or not 1 <= count <= (2 if hammer_only else 3):
            raise WeaponPointsBlocked("Invalid Progressive Special Weapon count")
        own_crucible = int(not hammer_only)
        own_hammer = int(hammer_only or count >= 2)
        hammer_tier = 2 if count >= (2 if hammer_only else 3) else own_hammer
        body = struct.pack("<IIIIIIIQII", 1, own_crucible, own_hammer, hammer_tier, 0, 0, 0, 0, 0, 0)
        result = self._execute_typed(65536, "--special", 37, 38, 40, body)
        required_known = own_crucible | (2 if own_hammer else 0) | (4 if hammer_tier == 2 else 0)
        required_flags = 34 | (64 if result["outcome"] == 1 else 0)
        if (result["outcome"] not in (0, 1) or result["flags"] & required_flags != required_flags
                or (own_crucible and (result["owns_crucible"] != 1 or result["native_crucible"] != 1))
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
        if (result.get("inventory_abi") not in (4, 5, 6, 7) or result.get("outcome") != 1
                or result.get("flags", 0) & 3 != 3 or result.get("upgrades_after") == 0xffffffff):
            raise WeaponPointsBlocked(f"Blood Punch native observation is unknown or incompatible: {result}")
        return (result["upgrades_after"] >> 11) & 15

    def observe_inventory(self):
        body = struct.pack("<5I4B", *([0] * 9))
        result = self._execute_typed(8192, "--inventory", 25, 26, 28, body)
        if result.get("inventory_abi") == 4:
            result["dash_before"] = result["dash_after"] = 255
        if result.get("inventory_abi") in (4, 5):
            result["slayer_key_before"] = result["slayer_key_after"] = 255
        if result.get("inventory_abi") in (4, 5, 6):
            result["frag_grenade_before"] = result["frag_grenade_after"] = 255
        if (result.get("inventory_abi") not in (4, 5, 6, 7) or result.get("kind") != 0
                or result.get("outcome") != 1 or result.get("flags", 0) & 3 != 3
                or result.get("ice_bomb_after") not in (0, 1, 255)
                or result.get("dash_after") not in (0, 1, 255)
                or result.get("slayer_key_after") not in (0, 1, 255)
                or result.get("frag_grenade_after") not in (0, 1, 255)):
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

    def ensure_support_runes(self, mask):
        if type(mask) is not int or not 0 < mask <= 7:
            raise WeaponPointsBlocked("Invalid support Rune mask")
        body = struct.pack("<IIIBb2s", 2, 0, mask, 0, -1, b"\0\0")
        result = self._execute_typed(32768, "--runes", 33, 34, 36, body)
        if (result["outcome"] not in (0, 1) or result["flags"] & 11 != 11
                or result["owned_support_after"] & mask != mask
                or result["selected_support_after"] != result["selected_support_before"]):
            raise WeaponPointsBlocked(f"Native support Rune registration unconfirmed: {result}")
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
    def __init__(self, session_state, persist, link, namespace):
        self.state, self.persist, self.link, self.namespace = session_state, persist, link, namespace

    def _ledger(self, authoritative):
        ledger = self.state.get("weapon_points")
        if ledger is None:
            facts = self.link.observe()
            gained = facts["gained_after"]
            history = sorted(authoritative.items())
            if type(gained) is not int or gained < 0 or gained % AMOUNT or gained > len(history) * AMOUNT:
                raise WeaponPointsBlocked("Native earned WUP needs the complete matching AP item history")
            ledger = {"version": 1, "namespace": self.namespace, "receipts": [
                {"index": index, "receipt": receipt, "amount": AMOUNT}
                for index, receipt in history[:gained // AMOUNT]]}
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
        ledger = self._ledger(authoritative)
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
        if authoritative.get(index) != receipt or not receipt:
            raise WeaponPointsBlocked("WUP receipt is not authoritative")
        ledger = self._ledger(authoritative)
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
