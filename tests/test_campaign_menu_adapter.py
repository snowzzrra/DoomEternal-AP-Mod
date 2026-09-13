import json
import struct
import unittest
from pathlib import Path

from doom_eap.runtime.campaign_menu import CampaignMenu
from doom_eap.runtime.unified_campaign import STAGES


class CampaignMenuAdapter(unittest.TestCase):
    def test_captured_native_identity_uploads_and_reuses_committed_projection(self):
        scope = json.loads((Path(__file__).parent / 'fixtures/campaign-native-scope.json').read_text())
        self.assertNotIn('pid', scope)
        snapshot = dict(hub_map='game/hub/hub', fortress_phase=0, selected='hub', rows=[
            dict(stage=stage, revealed=False, unlocked=False, completed=False, goal=False,
                 details_visible=False, map='', title='???') for stage in STAGES])
        test = self

        class Link:
            pid = scope['target_pid']
            namespace = 'a' * 64
            revision = 0
            operations = []

            def admitted_save_root(self):
                return 'admitted'

            def _run(self, command, message=None):
                if message is None:
                    return scope
                magic, version, operation, size, reserved = struct.unpack_from('<IHHII', message)
                test.assertEqual((magic, version, size, reserved), (0x50494353, 1, len(message)-16, 0))
                execution = struct.unpack_from('<QIQ16sQQ16sI', message, 16)
                test.assertEqual(execution[:5], (4096, self.pid, int(scope['process_created']),
                                               bytes.fromhex(scope['instance_id']), int(scope['lifecycle_generation'])))
                offset = 16 + struct.calcsize('<QIQ16sQQ16sI')
                test.assertEqual(message[offset:offset+65], self.namespace.encode()+b'\0')
                revision, index, count, row_id, flags, native_index, map_name, title = struct.unpack_from(
                    '<QIIIII192s96s', message, offset+65)
                self.operations.append(operation)
                if operation == 22:
                    test.assertEqual(count, 21)
                    if index:
                        test.assertEqual(flags, 0)
                        test.assertEqual(map_name.rstrip(b'\0'), b'')
                if operation == 23:
                    self.revision = revision
                return dict(namespace=self.namespace, request_id=execution[5], build_id=scope['build_id'],
                            status=0, committed_revision=self.revision, selected_id=1, loaded_id=0)

        link = Link()
        menu = CampaignMenu()
        self.assertEqual(menu.synchronize(link, snapshot)['selected_stage'], 'hub')
        self.assertEqual(link.operations, [24] + [22]*21 + [23])
        menu.synchronize(link, snapshot)
        self.assertEqual(link.operations[-1], 24)
        self.assertEqual(link.operations.count(22), 21)
