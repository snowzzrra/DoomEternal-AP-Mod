#!/usr/bin/env python3
"""Decrypt DOOM Eternal Steam Cloud save files without modifying the originals."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hashlib
import os
import sys
from pathlib import Path

STEAM_ID64_BASE = 76561197960265728
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


def _aes_gcm_decrypt(
    key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes, aad: bytes
) -> bytes:
    crypto = _load_libcrypto()
    void_p = ctypes.c_void_p
    int_p = ctypes.POINTER(ctypes.c_int)

    crypto.EVP_CIPHER_CTX_new.restype = void_p
    crypto.EVP_CIPHER_CTX_free.argtypes = [void_p]
    crypto.EVP_aes_128_gcm.restype = void_p
    crypto.EVP_DecryptInit_ex.argtypes = [
        void_p, void_p, void_p, void_p, void_p
    ]
    crypto.EVP_DecryptInit_ex.restype = ctypes.c_int
    crypto.EVP_CIPHER_CTX_ctrl.argtypes = [
        void_p, ctypes.c_int, ctypes.c_int, void_p
    ]
    crypto.EVP_CIPHER_CTX_ctrl.restype = ctypes.c_int
    crypto.EVP_DecryptUpdate.argtypes = [
        void_p, void_p, int_p, void_p, ctypes.c_int
    ]
    crypto.EVP_DecryptUpdate.restype = ctypes.c_int
    crypto.EVP_DecryptFinal_ex.argtypes = [void_p, void_p, int_p]
    crypto.EVP_DecryptFinal_ex.restype = ctypes.c_int

    context = crypto.EVP_CIPHER_CTX_new()
    if not context:
        raise RuntimeError("EVP_CIPHER_CTX_new failed")

    def buffer(data: bytes):
        return (ctypes.c_ubyte * len(data)).from_buffer_copy(data)

    key_buffer = buffer(key)
    nonce_buffer = buffer(nonce)
    aad_buffer = buffer(aad)
    ciphertext_buffer = buffer(ciphertext)
    tag_buffer = buffer(tag)
    output = (ctypes.c_ubyte * (len(ciphertext) + 16))()
    output_length = ctypes.c_int()
    final_length = ctypes.c_int()

    try:
        cipher = crypto.EVP_aes_128_gcm()
        if not cipher or crypto.EVP_DecryptInit_ex(
            context, cipher, None, None, None
        ) != 1:
            raise RuntimeError("EVP AES-128-GCM initialization failed")
        if crypto.EVP_CIPHER_CTX_ctrl(
            context, EVP_CTRL_GCM_SET_IVLEN, len(nonce), None
        ) != 1:
            raise RuntimeError("EVP GCM nonce setup failed")
        if crypto.EVP_DecryptInit_ex(
            context,
            None,
            None,
            ctypes.cast(key_buffer, void_p),
            ctypes.cast(nonce_buffer, void_p),
        ) != 1:
            raise RuntimeError("EVP GCM key setup failed")
        if aad and crypto.EVP_DecryptUpdate(
            context,
            None,
            ctypes.byref(output_length),
            ctypes.cast(aad_buffer, void_p),
            len(aad),
        ) != 1:
            raise RuntimeError("EVP GCM AAD setup failed")
        if crypto.EVP_DecryptUpdate(
            context,
            ctypes.cast(output, void_p),
            ctypes.byref(output_length),
            ctypes.cast(ciphertext_buffer, void_p),
            len(ciphertext),
        ) != 1:
            raise RuntimeError("EVP GCM decrypt failed")
        if crypto.EVP_CIPHER_CTX_ctrl(
            context,
            EVP_CTRL_GCM_SET_TAG,
            len(tag),
            ctypes.cast(tag_buffer, void_p),
        ) != 1:
            raise RuntimeError("EVP GCM tag setup failed")
        final_pointer = ctypes.cast(
            ctypes.byref(output, output_length.value), void_p
        )
        if crypto.EVP_DecryptFinal_ex(
            context, final_pointer, ctypes.byref(final_length)
        ) != 1:
            raise ValueError("Encrypted save authentication failed")
        total = output_length.value + final_length.value
        return bytes(output[:total])
    finally:
        crypto.EVP_CIPHER_CTX_free(context)


def discover_default_remote() -> Path | None:
    home = Path.home()
    homes = [home]
    if os.name != "nt" and home.is_absolute():
        var_home = Path("/var") / home.relative_to("/")
        if var_home != home:
            homes.append(var_home)

    candidates = [
        path
        for candidate_home in homes
        for path in candidate_home.joinpath(
            ".local/share/Steam/userdata"
        ).glob("*/782330/remote")
    ]
    program_files = os.environ.get("PROGRAMFILES(X86)")
    if program_files:
        candidates.extend(
            Path(program_files).joinpath("Steam/userdata").glob("*/782330/remote")
        )
    return next((path for path in candidates if path.is_dir()), None)


DEFAULT_REMOTE = discover_default_remote()


def default_steam_id3() -> int:
    if DEFAULT_REMOTE is not None:
        try:
            return int(DEFAULT_REMOTE.parents[1].name)
        except (IndexError, ValueError):
            pass
    return 0


def steam_id64(steam_id3: int) -> int:
    return STEAM_ID64_BASE + steam_id3


def decrypt(data: bytes, aad_text: str) -> bytes:
    if len(data) < 28:
        raise ValueError("Encrypted save is too short to contain nonce and GCM tag")
    aad = aad_text.encode("utf-8")
    key = hashlib.sha256(aad).digest()[:16]
    nonce = data[:12]
    ciphertext = data[12:-16]
    tag = data[-16:]
    return _aes_gcm_decrypt(key, nonce, ciphertext, tag, aad)


def decrypt_file(source: Path, destination: Path, identifier: str) -> None:
    aad = f"{identifier}MANCUBUS{source.name}"
    plaintext = decrypt(source.read_bytes(), aad)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(plaintext)
    print(f"{source} -> {destination} ({len(plaintext)} bytes)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", nargs="?", type=Path, default=DEFAULT_REMOTE)
    parser.add_argument("--output", type=Path, default=Path("/tmp/doom-eternal-decrypted"))
    parser.add_argument("--steam-id3", type=int, default=default_steam_id3())
    args = parser.parse_args()

    if args.source is None:
        parser.error("Steam remote directory was not found; provide source explicitly")
    if not args.steam_id3:
        parser.error("Steam account ID was not found; provide --steam-id3")

    identifier = str(steam_id64(args.steam_id3))
    sources = [args.source] if args.source.is_file() else sorted(args.source.glob("*/*"))
    for source in sources:
        if not source.is_file() or source.name.endswith("-BACKUP"):
            continue
        relative = source.name if args.source.is_file() else source.relative_to(args.source)
        try:
            decrypt_file(source, args.output / relative, identifier)
        except Exception as error:
            print(f"SKIP {source}: {error}")


if __name__ == "__main__":
    main()
