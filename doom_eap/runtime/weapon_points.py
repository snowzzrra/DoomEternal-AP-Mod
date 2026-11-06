"""Typed Sentinel Game Link adapter for runtime features and observations."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import struct
import time
from pathlib import Path
from uuid import uuid4

from doom_eap.runtime.inventory import WeaponPointsBlocked

from . import sentinel_protocol_generated as codec
from .native_transport import NativeTransport, TransportError

ITEM_ID = 7770903
AMOUNT = 3


class NativeGameplayInactive(WeaponPointsBlocked):
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
    """Keep one v2 connection on the bridge's serialized native worker."""
    def __init__(self, probe: Path, pid: int, namespace: str, diagnostic=None, *, command_prefix=(), environment=None):
        self.probe, self.pid, self.namespace = probe.resolve(), pid, namespace
        self.diagnostic = diagnostic
        self.command_prefix, self.environment = command_prefix, environment
        self._runner = None
        self._transport = None
        self._diagnostic_signatures = {}

    def close(self):
        if self._runner is not None:
            self._runner.run_until_complete(self._transport.close())
            self._runner.run_until_complete(self._runner.shutdown_asyncgens())
            self._runner.run_until_complete(self._runner.shutdown_default_executor())
            self._runner.close()
            self._runner = self._transport = None

    def _diagnose(self, evidence):
        if not self.diagnostic:
            return
        key = (evidence["stage"], evidence["operation"])
        try:
            response = json.loads(evidence.get("stdout", "{}"))
        except ValueError:
            response = evidence.get("stdout")
        signature = json.dumps((evidence.get("predicate"), evidence.get("error"), evidence.get("returncode"),
                                evidence.get("argv"), diagnostic_value(response)), sort_keys=True)
        now = time.monotonic()
        previous = self._diagnostic_signatures.get(key)
        if previous is None or previous["signature"] != signature:
            self._diagnostic_signatures[key] = diagnostic_entry(evidence, previous, now, signature)
            self.diagnostic({**evidence, "first_seen": now, "last_seen": now, "count": 1})
            return
        previous.update(last_seen=now, count=previous["count"] + 1)
        if now - previous["emitted_at"] >= 60:
            self.diagnostic({"stage": key[0], "operation": key[1], "summary": "unchanged",
                **{field: previous[field] for field in ("first_seen", "last_seen", "count")}})
            previous["emitted_at"] = now

    def request(self, command_type, tag=0, payload=None):
        if self._runner is None:
            self._runner = asyncio.new_event_loop()
            executable = str(self.probe)
            if self.command_prefix:
                from .proton import windows_path
                executable = windows_path(self.probe)
            self._transport = NativeTransport((*self.command_prefix, executable), self.pid, env=self.environment)
        try:
            result = self._runner.run_until_complete(self._request(command_type, tag, payload or {}))
        except (TransportError, OSError, TimeoutError, ValueError) as error:
            self._diagnose(dict(stage='v2', operation=str(command_type), error=str(error), returncode=1))
            self.close()
            raise WeaponPointsBlocked(f"Sentinel v2 unavailable: {error}") from error
        self._diagnose(dict(stage='v2', operation=str(command_type), stdout=json.dumps(result), returncode=0))
        return result

    async def _request(self, command_type, tag, payload):
        transport = self._transport
        await transport.connect()
        if command_type in (22, 23, 24) and not all(transport.supports(kind, 4) for kind in (22, 23, 24)):
            raise WeaponPointsBlocked('Native Campaign menu requires payload revision 4')
        command = codec.COMMANDS[command_type]
        revision = max(command['payload_revisions'])
        variant = next(v for v in command['payload']['variants'] if v['tag'] == tag)
        namespace, binding = await self._admit(command_type, tag, payload, variant)
        scope = transport.scope
        encoded = codec.encode_variant(command_type, revision, tag, payload)
        if variant['operation'] == 'read':
            result = await transport.read(dict(scope=scope, namespace_id=namespace, save_binding=binding,
                                               command_type=command_type, payload_revision=revision), encoded)
        else:
            submission = dict(reference=dict(scope=scope, namespace_id=namespace, command_id=str(uuid4()),
                                             command_type=command_type, payload_revision=revision),
                              save_binding=binding, deadline_ms=2000)
            result = await self._submit(submission, encoded)
        return self._result(command_type, result, transport.scope)

    async def _admit(self, command_type, tag, payload, variant):
        namespace, binding = self.namespace.encode('ascii'), bytes(16)
        if variant['admission'] in ('S', 'G'):
            admission = await self._transport.observe(12, b'0' * 64, bytes(16))
            if admission is None or not any(admission['save_binding']):
                raise NativeGameplayInactive('Native gameplay context is not active')
            binding = bytes(admission['save_binding'])
            if command_type == 46 and tag == 8 and payload in (
                    {'variable': 0, 'value': '1.00'}, {'variable': 1, 'value': '1.00'}, {'variable': 2, 'value': '0'}):
                namespace = wire_text(admission['observation']['namespace_id']).encode('ascii')
        elif variant['admission'] == 'P':
            namespace = b'0' * 64
        return namespace, binding

    async def _submit(self, submission, encoded):
        transport = self._transport
        prepared = await transport.prepare(submission, encoded)
        response = await transport.submit(prepared)
        deadline = time.monotonic() + 5
        while response.response['reply']['dimensions']['progress'] in (0, 1) and time.monotonic() < deadline:
            await asyncio.sleep(.01)
            response = await transport.query(prepared.reference)
        dimensions = response.response['reply']['dimensions']
        if response.response['error'] or dimensions['progress'] != 2 or response.result is None:
            raise WeaponPointsBlocked('Native feature result is uncertain')
        await transport.ack(prepared.reference, bytes(dimensions['result_token']))
        return response.result

    def _result(self, command_type, result, scope):
        value = admission_result(result) if command_type == 12 else dict(result)
        value.update(pid=scope['pid'], target_pid=scope['pid'], process_created=str(scope['process_created']),
                     instance_id=bytes(scope['instance_id']).hex(), lifecycle_generation=scope['generation'])
        if 'namespace_id' in value:
            value['namespace_id'] = wire_text(value['namespace_id'])
            value['namespace'] = value['namespace_id']
        if 'abi_version' in value:
            value[codec.COMMANDS[command_type]['name'] + '_abi'] = value['abi_version']
        if 'current_map' in value:
            value['current_map'] = wire_text(value['current_map']['bytes'])
        if command_type == 4:
            value['availability'] = 'enabled' if value['availability'] == 1 else 'unavailable'
            value['lifecycle'] = 'active' if value['lifecycle'] == 2 else 'inactive'
        value.setdefault('build_id', getattr(self, '_build_id', ''))
        if command_type == 1:
            value['build_id'] = self._build_id = wire_text(value['build_id'])
        if command_type in (22, 23, 24):
            value['hint_intents'] = value['intent_ids'][:value['intent_count']]
        value['result'] = 'ok'
        return value

    def _run(self, args):
        operations = {'--native': 4, '--inspect': 1, '--save-admission': 12, '--save': 11,
                      '--engine': 2, '--context': 3, '--save-installation': 17}
        operation = next(operations[arg] for arg in args if arg in operations)
        if operation == 4:
            self.request(1)
        return self.request(operation, payload={'after_event': 0} if operation == 4 else {})

    def admitted_save_root(self):
        facts = self._run(['--save-admission'])
        if (facts['namespace_id'] != self.namespace or not facts['accepting_requests']
                or not facts['route_retained'] or facts['state'] != 'admitted'
                or not re.fullmatch(r'ap-[0-9a-f]{40}', facts['native_root'])):
            raise WeaponPointsBlocked('AP save provider is not admitted for this identity')
        self.save_admission = facts
        return facts['native_root']

    def observe_inventory(self):
        scope = self._run(['--native'])
        result = self.request(25)
        if (result['abi_version'] != 8 or result['kind'] != 0 or result['outcome'] != 1 or result['flags'] & 3 != 3
                or result['perks_owned'] & ~result['perks_known']
                or any(result[name] not in (0, 1, 255) for name in ('ice_bomb_after', 'dash_after', 'slayer_key_after',
                                                                  'frag_grenade_after', 'flame_belch_after', 'blood_punch_after'))):
            raise WeaponPointsBlocked('Native inventory observation is malformed or unknown')
        result['_observation_scope'] = tuple(str(scope[k]) for k in ('process_created', 'instance_id', 'build_id', 'lifecycle_generation'))
        result['_observation_map'] = scope['current_map']
        return result

    def observe_special_inventory(self):
        scope = self._run(['--native'])
        result = self.request(37)
        result['_observation_scope'] = tuple(str(scope[k]) for k in ('process_created', 'instance_id', 'build_id', 'lifecycle_generation'))
        result['_observation_map'] = scope['current_map']
        return result

    def observe_combat(self, mission_maps):
        self._run(['--native'])
        if not 1 <= len(mission_maps) <= 64:
            raise WeaponPointsBlocked('Invalid combat mission maps')
        result = self.request(50, payload={'maps': [{'value': name} for name in sorted(set(mission_maps))]})
        return dict(producer=[str(self.pid), result['process_created'], result['instance_id'], result['build_id']],
                    namespace_id=result['namespace_id'], qualified_kills=result['qualified_kills'],
                    observation_gaps=result['observation_gaps'])

    def deathlink_request(self, kind, *, enabled=False, mode='soft', event_id=0, event_hash=bytes(16), ack_sequence=0):
        payloads = {0: {}, 1: dict(enabled=int(enabled), mode=int(mode == 'hardcore')),
                    2: dict(event_id=event_id, event_hash=event_hash), 3: {},
                    4: dict(sequence=ack_sequence), 5: dict(event_id=event_id)}
        return self.request(41, kind, payloads[kind])

    def select_special_weapon(self, selected):
        return self.request(37, 2, dict(selected=selected))

    def publish_ammo_refill(self, snapshot, *, connected):
        known = snapshot.get('authoritative') is True
        balance = snapshot.get('available', 0) if known else 0
        flags = int(bool(connected)) | (6 if known else 0)
        return self.request(37, 4, dict(balance=balance, flags=flags))

    def publish_checked_locations(self, checked_locations, revision):
        bits = [0] * codec.SCHEMA['limits']['automap_words']
        for location in checked_locations:
            offset = location - 7770000
            if 0 <= offset < len(bits) * 64:
                bits[offset // 64] |= 1 << (offset % 64)
        return self.request(45, 1, dict(known=1, revision=revision, checked_locations=bits))

    def publish_combat_progress(self, total, target, first_milestone=0, milestone=0, milestone_step=0, visible=True):
        scope = self._run(['--native'])
        native_scope = dict(pid=self.pid, process_created=int(scope['process_created']),
                            instance_id=bytes.fromhex(scope['instance_id']), lifecycle_generation=scope['lifecycle_generation'])
        presentation = dict(execution=dict(expected=native_scope, request_id=1, nonce=uuid4().bytes, deadline_ms=2000),
                            namespace_id=self.namespace.encode('ascii') + b'\0', total=total, target=target,
                            first_milestone=first_milestone, milestone=milestone, milestone_step=milestone_step, visible=int(visible))
        return self.request(51, payload=dict(presentation=presentation))


def wire_text(value):
    return bytes(value).split(b'\0', 1)[0].decode('utf-8')


def admission_result(result):
    value = dict(result['observation'])
    value.update(save_binding=bytes(result['save_binding']).hex(),
                 state='admitted' if value['state'] == 3 else 'unavailable',
                 accepting_requests=bool(value['flags'] & 2), route_retained=bool(value['flags'] & 1))
    value['native_root'] = wire_text(value['native_root'])
    return value


def diagnostic_value(value):
    if isinstance(value, dict):
        return {key: (item not in (0, "0", None) if key == "event_gap_count" else diagnostic_value(item))
                for key, item in value.items() if key not in {
                    "sequence", "timestamp_ms", "at_monotonic", "sampled_at_ms", "event_sequence",
                    "callback_sequence", "callback_at_ms", "context_sampled_at_ms", "history_oldest",
                    "history_overwritten", "events", "qualified_kills", "at_ms"}}
    if isinstance(value, list):
        return [diagnostic_value(item) for item in value]
    return value


def diagnostic_entry(evidence, previous, now, signature):
    row = {"signature": signature, "first_seen": now, "last_seen": now, "count": 1, "emitted_at": now}
    first_error = previous.get("first_error") if previous else None
    if first_error is None and (evidence.get("error") or evidence.get("returncode", 0)):
        first_error = {**evidence, "at_monotonic": now}
    if first_error is not None:
        row["first_error"] = first_error
    return row
