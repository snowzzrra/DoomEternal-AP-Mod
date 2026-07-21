#!/usr/bin/env python3
"""Reusable Mission Challenge override validator.

Derives expected override paths from challenge_location_registry.json,
then validates that a given set of override files (or a mod root directory)
matches exactly — no extra files, no missing files, valid structure.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def _load_registry(registry_path: Path) -> list[dict]:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    return registry.get("mission_challenges", [])


def _derive_expected_paths(entries: list[dict]) -> set[str]:
    return {entry["completion_owner"]["path"] for entry in entries}


def _derive_expected_ids(entries: list[dict]) -> set[int]:
    return {entry["location_id"] for entry in entries}


def _find_override_files(mod_root: Path) -> dict[str, Path]:
    """Find all mission challenge override files in mod_root.

    Returns dict mapping relative path (under gameresources/generated/decls/)
    to absolute Path.
    """
    results: dict[str, Path] = {}
    prefix = "unlockable/mission_challenge/"
    base = mod_root / "gameresources" / "generated" / "decls"
    for fpath in base.rglob("*.decl"):
        rel = fpath.relative_to(base).as_posix()
        if rel.startswith(prefix):
            results[rel] = fpath
    return results


def validate_overrides_from_files(
    override_paths: list[Path],
    registry_path: Path,
) -> list[str]:
    """Validate override files against registry. Returns list of error messages."""
    errors: list[str] = []
    entries = _load_registry(registry_path)
    expected_paths = _derive_expected_paths(entries)
    expected_ids = _derive_expected_ids(entries)

    found_paths: set[str] = set()
    found_ids: set[int] = set()
    forbidden_currencies = re.compile(
        r"\bCURRENCY_(?:PRAETOR_UPGRADE|SENTINEL_BATTERY|WEAPON_UPGRADE|WEAPON_MASTERY)\b"
    )

    for fpath in override_paths:
        rel = fpath.as_posix()
        # Extract relative path under generated/decls/
        if "generated/decls/" in rel:
            rel = rel.split("generated/decls/", 1)[1]
        elif "unlockable/mission_challenge/" in rel:
            rel = "unlockable/" + rel.split("unlockable/", 1)[1]
        else:
            errors.append(f"Cannot determine relative path from: {fpath}")
            continue

        assert_path = f"unlockable/mission_challenge/{rel.split('unlockable/mission_challenge/')[1]}" \
            if "unlockable/mission_challenge/" in rel else rel

        if assert_path not in expected_paths:
            errors.append(f"Extra override not in registry: {assert_path}")
            continue

        found_paths.add(assert_path)
        content = fpath.read_text(encoding="utf-8")

        # Check for forbidden currencies
        if forbidden_currencies.search(content):
            errors.append(f"Override contains forbidden currency: {assert_path}")

        # Validate exactly one currencyToGive
        currency_count = content.count("currencyToGive")
        if currency_count != 1:
            errors.append(
                f"Override has {currency_count} currencyToGive (expected 1): {assert_path}"
            )

        # Validate exactly one num = 0
        num_zero_count = len(re.findall(r'\bnum\s*=\s*0\s*;', content))
        if num_zero_count != 1:
            errors.append(
                f"Override has {num_zero_count} num = 0 (expected 1): {assert_path}"
            )

        # Find associated entry for location_id validation
        for entry in entries:
            if entry["completion_owner"]["path"] == assert_path:
                found_ids.add(entry["location_id"])
                # Verify structure: completionStat preserved
                expected_stat = entry["completion_owner"]["completion_stat"]
                if expected_stat not in content:
                    errors.append(
                        f"Override missing completion_stat {expected_stat}: {assert_path}"
                    )
                break

    # Check for missing paths
    missing = expected_paths - found_paths
    if missing:
        errors.append(f"Missing override files: {sorted(missing)}")

    # Validate IDs unique
    if len(expected_ids) != len(found_ids):
        errors.append(
            f"Location ID count mismatch: expected {len(expected_ids)}, found {len(found_ids)}"
        )

    return errors


def validate_overrides_from_mod_root(
    mod_root: Path,
    registry_path: Path,
) -> list[str]:
    """Find and validate all challenge overrides under mod_root."""
    overrides = _find_override_files(mod_root)
    if not overrides:
        return ["No mission challenge override files found under mod root"]
    return validate_overrides_from_files(list(overrides.values()), registry_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry", type=Path, required=True,
        help="Path to challenge_location_registry.json",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--mod-root", type=Path,
        help="Root of unpacked mod directory",
    )
    group.add_argument(
        "--override-files", nargs="*", type=Path,
        help="Explicit list of override .decl files to validate",
    )
    args = parser.parse_args()

    if args.mod_root:
        errors = validate_overrides_from_mod_root(args.mod_root, args.registry)
    else:
        errors = validate_overrides_from_files(args.override_files, args.registry)

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1
    print("All Mission Challenge overrides validated OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
