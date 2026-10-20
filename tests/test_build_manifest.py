import hashlib
import json

import pytest

from doom_eap.launcher.build_manifest import decode_manifest, register_resource_manifest
from doom_eap.launcher.campaign_resources import publish_resource_activation
from doom_eap.runtime.save_decrypt import _aes_gcm_decrypt, _aes_gcm_encrypt


def test_aes_gcm_matches_nist_vector_and_rejects_corrupted_data():
    encrypted = bytes.fromhex('0388dace60b6a392f328c2b971b2fe78ab6e47d42cec13bdf53a67b21257bddf')
    assert _aes_gcm_encrypt(bytes(16), bytes(12), bytes(16), b'') == encrypted
    assert _aes_gcm_decrypt(bytes(16), bytes(12), encrypted[:-16], encrypted[-16:], b'') == bytes(16)
    with pytest.raises(ValueError, match='authentication'):
        _aes_gcm_decrypt(bytes(16), bytes(12), encrypted[:-16], bytes(16), b'')


def test_generated_bank_is_registered_with_its_size_and_unrelated_files_preserved(tmp_path, monkeypatch):
    key = bytes.fromhex('8B031F6A24C5C4F3950130C57EF660E9')
    original = {'hash': 'sha1', 'files': {'native.resources': {'fileSize': 9, 'other': 7},
                'meta.resources': {'fileSize': 3, 'chunkSize': 4, 'hashes': ['before']}}}
    nonce, footer = bytes(12), b'F' * 64
    path = tmp_path / 'build-manifest.bin'
    path.write_bytes(nonce + _aes_gcm_encrypt(key, nonce, json.dumps(original).encode(), b'build-manifest') + footer)
    (tmp_path / 'meta.resources').write_bytes(b'old')
    bank = 'ap_enemy_resources/' + 'a' * 64 + '.resources'
    (tmp_path / bank).parent.mkdir()
    (tmp_path / bank).write_bytes(b'bank data')
    updates = {'meta.resources': b'updated'}
    monkeypatch.setattr('doom_eap.launcher.build_manifest._CHUNK', 4)
    report = {'source_meta_sha256': hashlib.sha256(b'old').hexdigest()}
    register_resource_manifest(tmp_path, {bank: {}}, updates, report)
    document, used_key = decode_manifest(updates[path.name])
    assert used_key == key and updates[path.name][-64:] == footer
    assert document['hash'] == original['hash']
    assert document['files']['native.resources'] == original['files']['native.resources']
    assert document['files'][bank]['fileSize'] == 9
    assert document['files'][bank]['chunkSize'] == 4
    assert document['files'][bank]['hashes'] == [hashlib.sha1(chunk).hexdigest() for chunk in (b'bank', b' dat', b'a')]
    assert document['files']['meta.resources']['fileSize'] == 7
    assert document['files']['meta.resources']['hashes'] == [hashlib.sha1(chunk).hexdigest() for chunk in (b'upda', b'ted')]
    monkeypatch.setattr('doom_eap.launcher.launcher_platform.publish_file', lambda source, target, **kw: source.replace(target))
    publish_resource_activation(tmp_path, updates, report)
    repeated = {'meta.resources': b'updated'}
    register_resource_manifest(tmp_path, {bank: {}}, repeated, {})
    assert path.name not in repeated
    (tmp_path / bank).write_bytes(b'BANK data')
    changed = {}
    register_resource_manifest(tmp_path, {bank: {}}, changed, {})
    assert decode_manifest(changed[path.name])[0]['files'][bank]['hashes'][0] == hashlib.sha1(b'BANK').hexdigest()
