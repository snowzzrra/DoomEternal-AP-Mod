"""Compiler code identity survives freezing without relying on absent .py files."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from doom_eap.contracts.source_bytes import SOURCE_BYTE_CONTRACT, first_party_text_bytes

SOURCE_ROOTS = ("doom_eap", "tools/decls", "tools/maps", "tools/release")
BUNDLED_IDENTITY_PATH = "data/compiler_source_identity.json"


def capture_compiler_sources(root: Path) -> dict[str, str]:
    hashes = {}
    for relative in SOURCE_ROOTS:
        for path in sorted((root / relative).rglob("*.py")):
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
