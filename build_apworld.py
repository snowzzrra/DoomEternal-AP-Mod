#!/usr/bin/env python3
"""Build a standards-compliant APWorld container for Archipelago 0.6.7."""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    source = args.source.resolve()
    manifest = json.loads(
        source.joinpath("archipelago.json").read_text(encoding="utf-8")
    )
    manifest["version"] = 7
    manifest["compatible_version"] = 7

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        args.output, "w", zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        archive.writestr("archipelago.json", json.dumps(manifest))
        for path in sorted(source.rglob("*")):
            if (
                not path.is_file()
                or "__pycache__" in path.parts
                or path.suffix == ".pyc"
            ):
                continue
            archive.write(path, Path(source.name) / path.relative_to(source))


if __name__ == "__main__":
    main()
