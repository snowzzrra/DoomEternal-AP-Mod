"""Portable binary identity audit; replaces GNU strings in release paths."""

from __future__ import annotations

import argparse
from pathlib import Path


def audit_binary(path: Path, required: str, forbidden: tuple[str, ...]) -> None:
    data = path.read_bytes()
    if data[:2] != b"MZ":
        raise ValueError(f"not a Windows PE executable: {path}")

    def present(text: str) -> bool:
        return text.encode("ascii") in data or text.encode("utf-16-le") in data

    stale = [value for value in forbidden if present(value)]
    if stale:
        raise ValueError(f"stale product version(s) in {path.name}: {stale}")
    if not present(required):
        raise ValueError(f"required product version {required!r} missing from {path.name}")
    print(f"BINARY_AUDIT path={path} required={required} stale=none")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--required", required=True)
    parser.add_argument("--forbid", action="append", default=[])
    args = parser.parse_args()
    audit_binary(args.binary, args.required, tuple(args.forbid))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
