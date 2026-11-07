"""Synthetic v2 feature replies; records submissions without game access."""
import asyncio

from native_transport_support import Replies, codec, split

from doom_eap.runtime.native_transport import NativeTransport


def _field_value(field):
    kind = field['type']
    if kind in codec.WIDTHS:
        return field.get('const', field.get('min', 0))
    if kind == 'text':
        return 'x' * field.get('min', 0)
    return record(kind)


def record(name):
    result = {}
    for field in codec.TYPES[name]['fields']:
        if 'count' in field:
            result[field['name']] = [_field_value(field) for _ in range(field['count'])]
            if field.get('encoding', '').startswith('lower_hex'):
                result[field['name']] = list(b'0' * 64) + ([0] if field['count'] == 65 else [])
        else:
            result[field['name']] = [] if 'max_count' in field else _field_value(field)
    return result


class FeatureReplies(Replies):
    def __init__(self, scope, relay):
        super().__init__(scope)
        self.relay = relay
        self.hello['supported'] = [dict(command_type=i, payload_revision=r)
                                   for i, command in codec.COMMANDS.items() for r in command['payload_revisions']]
        self.submissions = []
        self.reference = None
        self.revision = 0

    async def prepare(self, submission, payload):
        transport = NativeTransport((self.relay,), self.scope)
        try:
            return await transport.prepare(submission, payload)
        finally:
            await transport.close()

    def _read_result(self, body):
        envelope, _ = split('read_envelope', body)
        command_type = envelope['command_type']
        result = record(codec.COMMANDS[command_type]['result'])
        if 'namespace_id' in result:
            result['namespace_id'] = [*list(envelope['namespace_id']), 0]
        if command_type == 1:
            result['build_id'][:7] = b'fixture'
        elif command_type == 4:
            result.update(availability=1, lifecycle=2, scope=dict(pid=self.scope['pid'],
                process_created=self.scope['process_created'], instance_id=self.scope['instance_id'], lifecycle_generation=1))
        elif command_type == 12:
            result['observation'].update(state=3, flags=3, namespace_id=[*list(b'a' * 64), 0])
            result['observation']['native_root'] = [*list(('ap-' + 'b' * 40).encode()), 0]
            result['save_binding'] = [1] + [0] * 15
        elif command_type == 24:
            result.update(committed_revision=self.revision, selected_id=1, loaded_id=1)
        return codec.encode('scope', self.scope) + codec.encode(codec.COMMANDS[command_type]['result'], result)


    def _submit(self, body):
        submission, encoded = split('submission', body)
        reference = submission['reference']
        command_type = reference['command_type']
        tag, payload = codec.decode_variant(command_type, reference['payload_revision'], encoded)
        self.submissions.append((command_type, tag, payload))
        prepared = asyncio.run(self.prepare(submission, encoded))
        self.reference = codec.decode('command_reference', prepared.reference)
        result = record(codec.COMMANDS[command_type]['result'])
        if 'namespace_id' in result:
            result['namespace_id'] = [*list(reference['namespace_id']), 0]
        if 'kind' in result:
            result['kind'] = tag
        if command_type in (46, 52):
            result['outcome'] = 1
        if command_type == 37:
            result.update(refill_balance=payload['balance'], refill_flags=payload['flags'])
        if command_type == 45:
            result.update(known=1, revision=payload['revision'])
        if command_type == 51:
            result.update({key: payload['presentation'][key] for key in ('total', 'target', 'milestone', 'visible')})
        if command_type in (22, 23, 24):
            self.revision = max(self.revision, payload['revision'])
            result.update(committed_revision=self.revision, selected_id=1, loaded_id=1)
        self.result = codec.encode(codec.COMMANDS[command_type]['result'], result)


    def body(self, operation, body):
        self.operations.append(operation)
        if operation == 6:
            return codec.encode('hello_result', self.hello)
        if operation == 1:
            return self._read_result(body)
        if operation == 2:
            self._submit(body)
        response = record('command_response')
        response['reply'].update(actual_instance_id=self.scope['instance_id'], reference=self.reference)
        response['reply']['dimensions'].update(progress=2, execution=1, effect=2, retention=0, result_token=[1] * 16)
        return codec.encode('command_response', response) + self.result
