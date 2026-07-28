"""Focused baseline acceptance command for one generated map."""

from __future__ import annotations

import argparse

from content_catalog import discover_maps
from tools.maps.map_semantic_baseline import accept_frozen_map_baseline


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", required=True, dest="map_key")
    args = parser.parse_args()
    if args.map_key not in {item.key for item in discover_maps()}:
        raise ValueError(f"unknown map key: {args.map_key}")
    accept_frozen_map_baseline(args.map_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
