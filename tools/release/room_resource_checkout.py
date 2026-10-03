"""Materialize first-party Git checkout text without modifying pinned releases."""
from pathlib import Path
import shutil

from doom_eap.contracts.source_bytes import first_party_text_bytes


CHECKOUT_TEXT_FILES = frozenset({
    "room_payload_manifest.json", "ROOM_RESOURCES_PROVENANCE.json", "SHA256SUMS.txt",
})


def stage_room_resource_files(source: Path, target: Path, repo_root: Path, filenames) -> None:
    source, target, repo_root = source.resolve(), target.resolve(), repo_root.resolve()
    checkout = repo_root / "packaging" / "room_resources"
    if target == source or target.is_relative_to(checkout):
        raise ValueError("Room resource staging must not overwrite a pinned checkout bundle")
    # Only this first-party checkout role permits CRLF -> LF. Downloads, archives,
    # binaries and compiler outputs retain exact bytes and strict integrity checks.
    from_checkout = source.parent == checkout
    target.mkdir(parents=True, exist_ok=True)
    for filename in filenames:
        original, staged = source / filename, target / filename
        if from_checkout and filename in CHECKOUT_TEXT_FILES:
            staged.write_bytes(first_party_text_bytes(original.read_bytes()))
        else:
            shutil.copy2(original, staged)
