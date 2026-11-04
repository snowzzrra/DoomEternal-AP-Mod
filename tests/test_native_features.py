"""Exercise feature consumers and the native client against v2 pipes, without DOOM."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from native_feature_support import FeatureReplies
from test_native_transport_pipe import PipeServer, process_scope
from test_native_transport_pipe import relay as relay_fixture

from doom_eap.runtime.campaign_menu import CampaignMenu
from doom_eap.runtime.weapon_points import SentinelWeaponPoints
from doom_eap.runtime.inventory import WeaponPointsBlocked

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows v2 pipe')
relay = relay_fixture


def test_feature_consumers_share_v2_connection_and_ack_results(relay):
    scope = process_scope()
    replies = FeatureReplies(scope, relay)
    server = PipeServer(replies).start()
    link = SentinelWeaponPoints(Path(relay), scope['pid'], 'a' * 64)
    try:
        assert link.admitted_save_root() == 'ap-' + 'b' * 40
        assert link.deathlink_request(1, enabled=True)['kind'] == 1
        assert link.deathlink_request(2, event_id=7, event_hash=bytes([1] * 16))['kind'] == 2
        assert link.publish_ammo_refill(dict(authoritative=True, available=2), connected=True)['refill_balance'] == 2
        assert link.publish_checked_locations({7770000}, 10)['revision'] == 10
        assert link.publish_combat_progress(3, 6)['total'] == 3
        assert link.request(52)['outcome'] == 1
        assert [i for i, _, _ in replies.submissions] == [41, 41, 37, 45, 51, 52]
        assert replies.operations.count(5) == 6
        result = CampaignMenu().synchronize(link, dict(fortress_phase=0, hub_map='game/hub/hub', rows=[], selected='hub'))
        assert result['selected_stage'] == 'hub' and result['committed_revision'] == 1
        assert [i for i, _, _ in replies.submissions][-2:] == [22, 23]
        assert replies.submissions[-2][2]['rewards_known'] == 1
        assert replies.submissions[-1][2] == dict(revision=1, count=1, selected_id=1)
        assert replies.reference['payload_revision'] == 4
        link.namespace = 'b' * 64
        assert link.request(46, 8, {'variable': 2, 'value': '0'})['outcome'] == 1
        assert bytes(replies.reference['namespace_id']) == b'a' * 64
    finally:
        link.close()
        server.join()


@pytest.mark.parametrize('missing_command', [22, 23, 24])
def test_campaign_requires_revision4_from_hello(relay, missing_command):
    scope = process_scope()
    replies = FeatureReplies(scope, relay)
    replies.hello['supported'] = [entry for entry in replies.hello['supported']
                                if (entry['command_type'], entry['payload_revision']) != (missing_command, 4)]
    server = PipeServer(replies).start()
    link = SentinelWeaponPoints(Path(relay), scope['pid'], 'a' * 64)
    try:
        with pytest.raises(WeaponPointsBlocked, match='requires payload revision 4'):
            CampaignMenu().synchronize(link, dict(fortress_phase=0, hub_map='game/hub/hub', rows=[], selected='hub'))
        assert replies.submissions == []
    finally:
        link.close()
        server.join()


def test_native_client_uses_v2_and_never_reads_spool(tmp_path, relay):
    compiler = shutil.which('cl.exe')
    if compiler is None:
        pytest.skip('MSVC environment required')
    root = Path(__file__).resolve().parents[1]
    core = Path(os.environ.get('SENTINEL_CORE_SOURCE', root.parent / 'Sentinel-Core'))
    libraries = next(path for path in (Path(relay).parent / 'transport/core', Path(relay).parent / 'core')
                     if (path / 'sentinel_inspection_client.lib').is_file())
    output = tmp_path / 'native-client-contract.exe'
    built = subprocess.run([compiler, '/nologo', '/std:c++17', '/EHsc', '/MT', '/DNOMINMAX',
        f'/I{root}', f'/I{core / "include"}', f'/I{core / "src"}',
        f'/I{libraries / "protocol_generated/include"}',
        str(root / 'tests/native_client_v2_contract.cpp'), str(root / 'native/client/sentinel_command_client.cpp'),
        str(libraries / 'sentinel_inspection_client.lib'), str(libraries / 'sentinel_protocol.lib'), 'bcrypt.lib',
        f'/Fe{output}', f'/Fo{tmp_path}\\'], cwd=tmp_path, capture_output=True, text=True, timeout=90)
    assert built.returncode == 0, built.stdout + built.stderr
    queue = tmp_path / 'base/ap_queue'
    queue.mkdir(parents=True)
    for suffix in ('.cmd', '.processing', '.attempt', '.failed'):
        (queue / ('old' + suffix)).write_text('give health')
    scope = process_scope()
    replies = FeatureReplies(scope, relay)
    server = PipeServer(replies).start()
    try:
        ran = subprocess.run([str(output), str(scope['pid'])], cwd=tmp_path, capture_output=True, text=True, timeout=15)
        assert ran.returncode == 0, ran.stdout + ran.stderr
        assert replies.submissions == [(46, 8, {'variable': 2, 'value': '0'})]
        assert replies.operations.count(5) == 1
        assert len(list(queue.iterdir())) == 4
    finally:
        server.join()
