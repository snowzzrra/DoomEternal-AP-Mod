#!/usr/bin/env python3
"""Generate a short, non-executing directed-test checklist for one map."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from foundation import load_foundation_contracts


def generate(map_key: str) -> list[str]:
    contracts = load_foundation_contracts()
    if map_key not in contracts["active_maps"]:
        raise ValueError(f"Unknown active map: {map_key}")
    lines = [
        f"0.3.0c.1 runtime: {map_key} ({contracts['active_maps'][map_key]})",
        "1. Complete all three Cultist Base Challenges and force checkpoint/save. Expect 7770138, 7770139, 7770140, and 7770141 once each; zero Suit Point.",
        "2. Confirm the Sentinel Battery reward remains vanilla.",
        "3. Reload and Mission Select. Expect no new 7770141 LocationChecks.",
        "4. Keep bridge open and switch newest primary save Slot 0 -> Slot 2. Expect SAVE_SLOT_ACTIVE and no cross-slot duplicate.",
        "Capture SAVE_SLOT_ACTIVE, [Challenge], and matching LocationChecks lines only.",
    ]
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        contents = "\n".join(generate(args.map)) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(contents, encoding="utf-8")
        else:
            print(contents, end="")
    except ValueError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
