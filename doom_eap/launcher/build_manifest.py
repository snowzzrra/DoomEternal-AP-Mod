"""Register generated resource banks in the game's encrypted file manifest."""

import ctypes
import ctypes.util
import hashlib
import io
import json
import os
import sys
from pathlib import Path, PurePosixPath

_KEYS = tuple(bytes.fromhex(value) for value in (
    '8B031F6A24C5C4F3950130C57EF660E9', '2C05001C1BF0134D1B17D4D5C4D784C0'))
_AAD = b"build-manifest"
# the game's hash routine accepts signed 32-bit lengths
_CHUNK = 1024 * 1024


def decode_manifest(data):
    if len(data) < 92:
        raise ValueError('Truncated build manifest')
    for key in _KEYS:
        try:
            plaintext = _aes_gcm_decrypt(key, data[:12], data[12:-80], data[-80:-64], _AAD)
        except ValueError:
            continue
        document = json.loads(plaintext)
        if not isinstance(document, dict) or not isinstance(document.get("files"), dict):
            raise ValueError('Invalid build manifest file table')
        return document, key
    raise ValueError('Build manifest authentication failed')


def _manifest_file_sizes(updates, document, banks):
    sizes = {name: len(data) for name, data in updates.items() if name in document["files"]}
    for name in banks:
        parts = PurePosixPath(name)
        if parts.is_absolute() or ".." in parts.parts or "\\" in name or ":" in name:
            raise ValueError('Invalid generated resource bank path')
        sizes[name] = banks[name]['size']
    return sizes

def _update_manifest_files(sizes, base, banks, updates, bank_paths, document, changed):
    for name, size in sizes.items():
        if size <= 0:
            raise ValueError('Empty installed resource file')
        with _manifest_stream(base, name, banks, updates, bank_paths) as stream:
            hashes = [hashlib.new(document["hash"], chunk).hexdigest()
                      for chunk in iter(lambda: stream.read(_CHUNK), b"")]
        entry = {**document["files"].get(name, {}), "fileSize": size, "chunkSize": _CHUNK,
                 "hashes": hashes}
        if document["files"].get(name) != entry:
            document["files"][name] = entry
            changed = True
    return changed

def register_resource_manifest(base, banks, updates, report, obsolete=(), *, bank_paths=None):
    path = base / "build-manifest.bin"
    if not banks and not path.is_file():
        return
    original = path.read_bytes()
    document, key = decode_manifest(original)
    sizes = _manifest_file_sizes(updates, document, banks)
    changed = False
    for name in obsolete:
        if name in document['files']:
            del document['files'][name]
            changed = True
    changed = _update_manifest_files(sizes, base, banks, updates, bank_paths, document, changed)
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


EVP_CTRL_GCM_SET_IVLEN = 0x9


EVP_CTRL_GCM_SET_TAG = 0x11


def _load_libcrypto():
    executable_dir = Path(sys.executable).resolve().parent
    names = [
        executable_dir / "lib" / "libcrypto.so",
        executable_dir / "lib" / "libcrypto-3-x64.dll",
        executable_dir / "libcrypto-3-x64.dll",
        executable_dir / "libcrypto.dll",
        ctypes.util.find_library("crypto"),
        "libcrypto.so.3",
        "libcrypto.so",
        "libcrypto-3-x64.dll",
        "libcrypto.dll",
    ]
    errors = []
    for name in names:
        if not name:
            continue
        try:
            return ctypes.CDLL(str(name))
        except OSError as error:
            errors.append(str(error))
    raise RuntimeError(
        "OpenSSL libcrypto was not found. Archipelago 0.6.7 includes it. "
        + "; ".join(errors[-3:])
    )


def _init_gcm_context(crypto, context, init, nonce, key_buffer, nonce_buffer, void_p):
    cipher = crypto.EVP_aes_128_gcm()
    if not cipher or init(
        context, cipher, None, None, None
    ) != 1:
        raise RuntimeError("EVP AES-128-GCM initialization failed")
    if crypto.EVP_CIPHER_CTX_ctrl(
        context, EVP_CTRL_GCM_SET_IVLEN, len(nonce), None
    ) != 1:
        raise RuntimeError("EVP GCM nonce setup failed")
    if init(
        context,
        None,
        None,
        ctypes.cast(key_buffer, void_p),
        ctypes.cast(nonce_buffer, void_p),
    ) != 1:
        raise RuntimeError("EVP GCM key setup failed")


def _set_gcm_aad(context, update, aad, output_length, aad_buffer, void_p):
    if aad and update(
        context,
        None,
        ctypes.byref(output_length),
        ctypes.cast(aad_buffer, void_p),
        len(aad),
    ) != 1:
        raise RuntimeError("EVP GCM AAD setup failed")


def _gcm_result(crypto, context, encrypt, tag_buffer, output, total, void_p):
    if encrypt:
        if crypto.EVP_CIPHER_CTX_ctrl(context, 0x10, 16, ctypes.cast(tag_buffer, void_p)) != 1:
            raise RuntimeError("EVP GCM tag extraction failed")
        return bytes(output[:total]) + bytes(tag_buffer)
    return bytes(output[:total])


def _update_gcm_ciphertext(crypto, context, update, output, output_length, ciphertext_buffer, ciphertext, encrypt, tag_buffer, tag, void_p):
    if update(
        context,
        ctypes.cast(output, void_p),
        ctypes.byref(output_length),
        ctypes.cast(ciphertext_buffer, void_p),
        len(ciphertext),
    ) != 1:
        raise RuntimeError("EVP GCM decrypt failed")
    if not encrypt and crypto.EVP_CIPHER_CTX_ctrl(
        context,
        EVP_CTRL_GCM_SET_TAG,
        len(tag),
        ctypes.cast(tag_buffer, void_p),
    ) != 1:
        raise RuntimeError("EVP GCM tag setup failed")


def _validate_gcm_lengths(key, nonce, tag, encrypt):
    if len(key) != 16 or len(nonce) != 12 or (not encrypt and len(tag) != 16):
        raise ValueError("Invalid AES-128-GCM key, nonce or tag length")


def _aes_gcm(
    key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes, aad: bytes, *, encrypt=False
) -> bytes:
    _validate_gcm_lengths(key, nonce, tag, encrypt)
    crypto = _load_libcrypto()
    void_p = ctypes.c_void_p
    int_p = ctypes.POINTER(ctypes.c_int)

    crypto.EVP_CIPHER_CTX_new.restype = void_p
    crypto.EVP_CIPHER_CTX_free.argtypes = [void_p]
    crypto.EVP_aes_128_gcm.restype = void_p
    init = getattr(crypto, "EVP_EncryptInit_ex" if encrypt else "EVP_DecryptInit_ex")
    update = getattr(crypto, "EVP_EncryptUpdate" if encrypt else "EVP_DecryptUpdate")
    final = getattr(crypto, "EVP_EncryptFinal_ex" if encrypt else "EVP_DecryptFinal_ex")
    init.argtypes = [
        void_p, void_p, void_p, void_p, void_p
    ]
    init.restype = ctypes.c_int
    crypto.EVP_CIPHER_CTX_ctrl.argtypes = [
        void_p, ctypes.c_int, ctypes.c_int, void_p
    ]
    crypto.EVP_CIPHER_CTX_ctrl.restype = ctypes.c_int
    update.argtypes = [
        void_p, void_p, int_p, void_p, ctypes.c_int
    ]
    update.restype = ctypes.c_int
    final.argtypes = [void_p, void_p, int_p]
    final.restype = ctypes.c_int

    context = crypto.EVP_CIPHER_CTX_new()
    if not context:
        raise RuntimeError("EVP_CIPHER_CTX_new failed")

    def buffer(data: bytes):
        return (ctypes.c_ubyte * len(data)).from_buffer_copy(data)

    key_buffer = buffer(key)
    nonce_buffer = buffer(nonce)
    aad_buffer = buffer(aad)
    ciphertext_buffer = buffer(ciphertext)
    tag_buffer = buffer(b"\0" * 16 if encrypt else tag)
    output = (ctypes.c_ubyte * (len(ciphertext) + 16))()
    output_length = ctypes.c_int()
    final_length = ctypes.c_int()

    try:
        _init_gcm_context(crypto, context, init, nonce, key_buffer, nonce_buffer, void_p)
        _set_gcm_aad(context, update, aad, output_length, aad_buffer, void_p)
        _update_gcm_ciphertext(crypto, context, update, output, output_length, ciphertext_buffer, ciphertext, encrypt, tag_buffer, tag, void_p)
        final_pointer = ctypes.cast(
            ctypes.byref(output, output_length.value), void_p
        )
        if final(
            context, final_pointer, ctypes.byref(final_length)
        ) != 1:
            raise ValueError("Encrypted save authentication failed")
        total = output_length.value + final_length.value
        return _gcm_result(crypto, context, encrypt, tag_buffer, output, total, void_p)
    finally:
        crypto.EVP_CIPHER_CTX_free(context)


def _aes_gcm_decrypt(key, nonce, ciphertext, tag, aad):
    return _aes_gcm(key, nonce, ciphertext, tag, aad)


def _aes_gcm_encrypt(key, nonce, plaintext, aad):
    return _aes_gcm(key, nonce, plaintext, b"", aad, encrypt=True)
