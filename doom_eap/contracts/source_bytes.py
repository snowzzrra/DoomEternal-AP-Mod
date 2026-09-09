"""Canonical bytes for explicitly identified first-party UTF-8 source text."""


SOURCE_BYTE_CONTRACT = "first_party_utf8_lf_v1"


def first_party_text_bytes(raw: bytes) -> bytes:
    return raw.decode("utf-8").replace("\r\n", "\n").encode("utf-8")
