"""Compiler source bytes and identity for authored and frozen packages."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

SOURCE_ROOTS = ("doom_eap/content", "doom_eap/contracts", "tools/decls", "tools/maps")
SOURCE_FILES = ("doom_eap/strings.py", "tools/release/build_string_table.py",
                "doom_eap/launcher/launcher_core.py", "doom_eap/runtime/bridge_saves.py",
                "doom_eap/runtime/observers.py", "tools/release/room_payloads.py",
                "tools/release/build_room_resources.py", "tools/release/stage_room_resources.py")
BUNDLED_IDENTITY_PATH = "data/compiler_source_identity.json"


def capture_compiler_sources(root: Path) -> dict[str, str]:
    hashes = {}
    paths = [root / name for name in SOURCE_FILES]
    paths.extend((root / "data/strings").glob("*.json"))
    for relative in SOURCE_ROOTS:
        paths.extend((root / relative).rglob("*.py"))
    for path in sorted(paths):
        if path.relative_to(root).as_posix() == "doom_eap/contracts/core_distribution.py":
            continue
        if path.is_symlink():
            raise ValueError(f"Compiler source cannot be a symlink: {path}")
        hashes[path.relative_to(root).as_posix()] = hashlib.sha256(first_party_text_bytes(path.read_bytes())).hexdigest()
    return hashes


def compiler_source_document(hashes: dict[str, str]) -> dict:
    encoded = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"schema_version": 1, "byte_contract": SOURCE_BYTE_CONTRACT,
            "source_hashes": hashes, "fingerprint": hashlib.sha256(encoded).hexdigest()}


def load_compiler_source_identity(root: Path, *, frozen: bool) -> dict:
    if not frozen:
        return compiler_source_document(capture_compiler_sources(root))
    document = json.loads((root / BUNDLED_IDENTITY_PATH).read_text(encoding="utf-8"))
    expected = compiler_source_document(document["source_hashes"])
    if document != expected or not document["source_hashes"]:
        raise ValueError("Bundled compiler source identity is invalid")
    return document


SOURCE_BYTE_CONTRACT = "first_party_utf8_lf_v1"


def first_party_text_bytes(raw: bytes) -> bytes:
    return raw.decode("utf-8").replace("\r\n", "\n").encode("utf-8")


_SOURCE_TEXT = frozenset({".py", ".json", ".yaml", ".yml"})


def compiler_source_bytes(path: Path) -> bytes:
    raw = path.read_bytes()
    if path.suffix.lower() in _SOURCE_TEXT:
        return first_party_text_bytes(raw)
    return raw
