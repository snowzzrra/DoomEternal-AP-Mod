"""Byte contract for explicitly enumerated first-party compiler source inputs."""
from pathlib import Path

from doom_eap.contracts.source_bytes import SOURCE_BYTE_CONTRACT, first_party_text_bytes

_SOURCE_TEXT = frozenset({".py", ".json", ".yaml", ".yml"})


def compiler_source_bytes(path: Path) -> bytes:
    raw = path.read_bytes()
    if path.suffix.lower() in _SOURCE_TEXT:
        return first_party_text_bytes(raw)
    return raw
