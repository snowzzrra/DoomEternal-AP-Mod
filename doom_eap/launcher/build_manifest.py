"""Register generated resource banks in the game's encrypted file manifest."""

import hashlib
import io
import json
import os
from pathlib import PurePosixPath

from doom_eap.launcher.ui.strings import text as player_text
from doom_eap.runtime.save_decrypt import _aes_gcm_decrypt, _aes_gcm_encrypt

_KEYS = tuple(bytes.fromhex(value) for value in (
    '8B031F6A24C5C4F3950130C57EF660E9', '2C05001C1BF0134D1B17D4D5C4D784C0'))
_AAD = b"build-manifest"
# the game's hash routine accepts signed 32-bit lengths
_CHUNK = 1024 * 1024


def decode_manifest(data):
    if len(data) < 92:
        raise ValueError(player_text('launcher.build_manifest', 'truncated_build_manifest'))
    for key in _KEYS:
        try:
            plaintext = _aes_gcm_decrypt(key, data[:12], data[12:-80], data[-80:-64], _AAD)
        except ValueError:
            continue
        document = json.loads(plaintext)
        if not isinstance(document, dict) or not isinstance(document.get("files"), dict):
            raise ValueError(player_text('launcher.build_manifest', 'invalid_build_manifest_file_table'))
        return document, key
    raise ValueError(player_text('launcher.build_manifest', 'build_manifest_authentication_failed'))


def register_resource_manifest(base, banks, updates, report, obsolete=(), *, bank_paths=None):
    path = base / "build-manifest.bin"
    if not banks and not path.is_file():
        return
    original = path.read_bytes()
    document, key = decode_manifest(original)
    sizes = {name: len(data) for name, data in updates.items() if name in document["files"]}
    for name in banks:
        parts = PurePosixPath(name)
        if parts.is_absolute() or ".." in parts.parts or "\\" in name or ":" in name:
            raise ValueError(player_text('launcher.build_manifest', 'invalid_generated_resource_bank_path'))
        sizes[name] = banks[name]['size']
    changed = False
    for name in obsolete:
        if name in document['files']:
            del document['files'][name]
            changed = True
    for name, size in sizes.items():
        if size <= 0:
            raise ValueError(player_text('launcher.build_manifest', 'empty_installed_resource_file'))
        with _manifest_stream(base, name, banks, updates, bank_paths) as stream:
            hashes = [hashlib.new(document["hash"], chunk).hexdigest()
                      for chunk in iter(lambda: stream.read(_CHUNK), b"")]
        entry = {**document["files"].get(name, {}), "fileSize": size, "chunkSize": _CHUNK,
                 "hashes": hashes}
        if document["files"].get(name) != entry:
            document["files"][name] = entry
            changed = True
    if changed:
        nonce = os.urandom(12)
        payload = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        updates[path.name] = nonce + _aes_gcm_encrypt(key, nonce, payload, _AAD) + original[-64:]
        assert decode_manifest(updates[path.name])[0] == document
        hashes = report.setdefault("source_hashes", {})
        for name in updates.keys() - {path.name} - hashes.keys():
            field = {"meta.resources": "source_meta_sha256", "idRehash.map": "source_rehash_sha256"}[name]
            hashes[name] = report[field]
        hashes[path.name] = hashlib.sha256(original).hexdigest()


def _manifest_stream(base, name, banks, updates, bank_paths):
    if name in banks:
        return (bank_paths or {}).get(name, base / name).open('rb')
    return io.BytesIO(updates[name])
