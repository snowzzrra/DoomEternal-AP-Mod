#!/usr/bin/env python3
"""Non-destructive inventory and format probe for DOOM Eternal Steam Cloud saves."""

from __future__ import annotations

import argparse
import hashlib
import math
from collections import Counter
from pathlib import Path

DEFAULT_ROOT = Path(
    "/var/home/guilherme/.local/share/Steam/userdata/160032537/782330/remote"
)


def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = Counter(data)
    length = len(data)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def printable_ratio(data: bytes) -> float:
    if not data:
        return 0.0
    printable = sum(byte in (9, 10, 13) or 32 <= byte < 127 for byte in data)
    return printable / len(data)


def inspect(path: Path) -> None:
    data = path.read_bytes()
    signature = data[16] if len(data) > 16 else None
    print(
        f"{path}\n"
        f"  size={len(data)} sha256={hashlib.sha256(data).hexdigest()}\n"
        f"  entropy={entropy(data):.4f} printable={printable_ratio(data):.3%} "
        f"byte16={signature!r} oodle_entities_header={signature == 0x8C}\n"
        f"  first32={data[:32].hex()}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()

    for path in sorted(args.root.glob("*/game.details")):
        inspect(path)
    for path in sorted(args.root.glob("*/game_duration.dat")):
        inspect(path)


if __name__ == "__main__":
    main()
