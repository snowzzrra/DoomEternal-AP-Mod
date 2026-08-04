"""Focused baseline acceptance command for one generated map."""

from __future__ import annotations

import argparse

from content_catalog import discover_maps
from tools.maps.map_semantic_baseline import accept_map_baseline
from tools.validation.pipeline import Pipeline


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", required=True, dest="map_key")
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    if args.map_key not in {item.key for item in discover_maps()}:
        raise ValueError(f"unknown map key: {args.map_key}")
    pipeline = Pipeline()
    artifact = pipeline.validate_map(args.map_key, baseline=False)
    path = accept_map_baseline(
        args.map_key,
        artifact.output,
        artifact.manifest,
        reason=args.reason,
    )
    print(f"accepted map baseline: {path}")
    pipeline.report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
