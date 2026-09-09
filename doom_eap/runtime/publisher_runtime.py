"""Explicit file adapters for publisher events and quarantine."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

def read_map_event(path: Path, marker: str) -> tuple[bool, str, str]:
    try:
        contents = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        contents = ""
    digest = hashlib.sha256(contents.encode("utf-8")).hexdigest()
    return marker in contents, contents, digest


def quarantine_malformed_event(
    path: Path,
    *,
    key: str,
    contents: str,
    sha256: str,
    quarantine_root: Path,
) -> tuple[Path, Path]:
    failed = quarantine_root / "failed"
    failed.mkdir(parents=True, exist_ok=True)
    stem = f"{int(time.time_ns())}_{sha256[:12]}_{path.name}"
    quarantined = failed / stem
    metadata = failed / f"{stem}.json"
    try:
        os.replace(path, quarantined)
    except OSError:
        quarantined.write_text(contents, encoding="utf-8", newline="")
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    metadata.write_text(
        json.dumps(
            {
                "publisher_key": key,
                "filename": path.name,
                "content": contents,
                "sha256": sha256,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return quarantined, metadata
