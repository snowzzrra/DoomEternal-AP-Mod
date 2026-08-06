#!/usr/bin/env python3
"""Reusable Mission Challenge override validator.

Derives expected override paths from challenge_location_registry.json,
then validates that a given set of override files (or a mod root directory)
matches exactly — no extra files, no missing files, valid structure.
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

# Add root to sys.path to import from challenge_registry
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from challenge_registry import load_challenge_registry
from tools.decls.mission_challenge_decl_builder import (
    AGGREGATE_LIST_PATH,
    NO_REWARD_CONTAINER,
    NO_REWARD_CONTAINER_DECL,
    NO_REWARD_CONTAINER_PATH,
    _aggregate_contracts,
    _challenge_paths,
    _level_blocks,
)


def _load_registry(registry_path: Path) -> list[dict]:
    registry = load_challenge_registry(registry_path)
    return registry.get("mission_challenges", [])


def _derive_expected_paths(entries: list[dict]) -> set[str]:
    return {entry["completion_owner"]["path"] for entry in entries}


def _derive_expected_ids(entries: list[dict]) -> set[int]:
    return {entry["location_id"] for entry in entries}


def _find_override_files(mod_root: Path) -> dict[str, Path]:
    """Find all mission challenge override files in mod_root.

    Returns dict mapping relative path (under gameresources*/generated/decls/)
    to absolute Path.
    """
    results: dict[str, Path] = {}
    prefix = "unlockable/mission_challenge/"
    for base in mod_root.glob("gameresources*"):
        decls = base / "generated" / "decls"
        if not decls.is_dir():
            continue
        for fpath in decls.rglob("*.decl"):
            rel = fpath.relative_to(decls).as_posix()
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
    found_ids: list[int] = []
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
                found_ids.append(entry["location_id"])
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
    id_counts = collections.Counter(found_ids)
    duplicates = [loc_id for loc_id, count in id_counts.items() if count > 1]
    if duplicates:
        errors.append(f"Duplicate location IDs found in overrides: {duplicates}")

    if len(expected_ids) != len(set(found_ids)):
        errors.append(
            f"Location ID count mismatch: expected {len(expected_ids)}, found {len(set(found_ids))}"
        )

    return errors


def validate_overrides_from_mod_root(
    mod_root: Path,
    registry_path: Path,
) -> list[str]:
    """Find and validate all challenge overrides under mod_root.
    
    Enforces that overrides reside strictly in the winning owner container
    (gameresources), with zero competing copies in higher priority archives.
    """
    errors: list[str] = []
    winning_owner = "gameresources"
    winning_decl_root = mod_root / winning_owner / "generated" / "decls"
    
    if not winning_decl_root.is_dir():
        errors.append(f"Winning owner directory missing: {winning_owner}")
        return errors

    for candidate in mod_root.glob("gameresources*"):
        if candidate.name == winning_owner:
            continue
        decls = candidate / "generated" / "decls"
        if decls.is_dir():
            for fpath in decls.rglob("*.decl"):
                rel = fpath.relative_to(decls).as_posix()
                if rel.startswith("unlockable/mission_challenge/") or rel == AGGREGATE_LIST_PATH or rel == NO_REWARD_CONTAINER_PATH:
                    errors.append(f"Competing override found in alternate container {candidate.name}: {rel}")

    overrides = _find_override_files(mod_root)
    if not overrides:
        return ["No mission challenge override files found under mod root"]
    
    errors.extend(validate_overrides_from_files(list(overrides.values()), registry_path))
    registry = load_challenge_registry(registry_path)

    aggregate_path = winning_decl_root / AGGREGATE_LIST_PATH
    container_path = winning_decl_root / NO_REWARD_CONTAINER_PATH
    if not aggregate_path.is_file():
        errors.append(f"Missing aggregate Mission Challenge override in {winning_owner}: {AGGREGATE_LIST_PATH}")
        return errors
    if not container_path.is_file():
        errors.append(f"Missing aggregate no-reward container in {winning_owner}: {NO_REWARD_CONTAINER_PATH}")
        return errors
    if container_path.read_text(encoding="utf-8") != NO_REWARD_CONTAINER_DECL:
        errors.append("Aggregate no-reward container contract drift")

    aggregate_source = aggregate_path.read_text(encoding="utf-8")
    blocks = [
        (index, block)
        for index, _, _, block in _level_blocks(aggregate_source)
        if "_dev_" not in block
    ]
    matched_indexes: set[int] = set()
    forbidden_rewards = re.compile(
        r"\bCURRENCY_|inventoryItemReward|currencyToGive|"
        r"gainedItems\s*=\s*\{\s*num\s*=\s*[1-9]"
    )
    for contract in _aggregate_contracts(registry):
        matches = [
            (index, block)
            for index, block in blocks
            if set(_challenge_paths(block)) == set(contract["unlockables"])
        ]
        if len(matches) != 1:
            errors.append(
                f"{contract['name']}: packaged aggregate owner count is {len(matches)}"
            )
            continue
        index, block = matches[0]
        matched_indexes.add(index)
        if block.count(f'completionUnlock = "{NO_REWARD_CONTAINER}";') != 1:
            errors.append(f"{contract['name']}: aggregate suppression is missing")
        if forbidden_rewards.search(block):
            errors.append(f"{contract['name']}: aggregate retains a vanilla reward")
    if len(matched_indexes) != len(registry["all_mission_challenges"]):
        errors.append("Packaged aggregate suppression set is incomplete")

    protected_paths = (
        "propitem/propitem/batteries/sentinel_battery.decl",
        "entitydef/interact/hub/battery_socket_for_engine.decl",
    )
    for protected in protected_paths:
        if (winning_decl_root / protected).exists():
            errors.append(f"Protected Sentinel Battery contract was overridden: {protected}")

    errors.extend(validate_vanilla_source_equivalence(mod_root, registry_path))
    return errors


def validate_vanilla_source_equivalence(
    mod_root: Path,
    registry_path: Path,
) -> list[str]:
    """Verify that source DECL templates from vanilla_decls/owners/ represent the winning
    vanilla copies and that generated overrides permit line-by-line drift ONLY in reward fields.
    Also verifies invariant: generated_decl_paths ⊆ vanilla_decl_paths.
    """
    errors: list[str] = []
    repo_root = mod_root.parent
    vanilla_base = repo_root / "vanilla_decls" / "owners"
    if not vanilla_base.is_dir():
        vanilla_base = Path(__file__).resolve().parents[2] / "vanilla_decls" / "owners"

    if not vanilla_base.is_dir():
        return errors

    winning_owner = "gameresources"
    candidate_owners = ["gameresources_patch3", "gameresources_patch2", "gameresources_patch1", "gameresources"]
    registry = load_challenge_registry(registry_path)
    entries = registry.get("mission_challenges", [])

    for candidate in mod_root.glob("gameresources*"):
        decls_dir = candidate / "generated" / "decls"
        if decls_dir.is_dir():
            for fpath in decls_dir.rglob("*.decl"):
                rel_path = fpath.relative_to(decls_dir).as_posix()
                if rel_path.startswith("unlockable/mission_challenge/") or rel_path == AGGREGATE_LIST_PATH or rel_path == NO_REWARD_CONTAINER_PATH or rel_path.startswith("warehouseofflinecontainer/"):
                    found_vanilla = any(
                        (vanilla_base / owner / "generated" / "decls" / rel_path).is_file()
                        for owner in candidate_owners
                    )
                    if not found_vanilla:
                        errors.append(f"Generated DECL path does not exist in vanilla corpus: {rel_path}")

    for entry in entries:
        rel_path = entry["completion_owner"]["path"]
        effective_source = None
        source_owner_found = None

        for owner in candidate_owners:
            candidate_file = vanilla_base / owner / "generated" / "decls" / rel_path
            if candidate_file.is_file():
                effective_source = candidate_file.read_text(encoding="utf-8").replace("\r\n", "\n")
                source_owner_found = owner
                break

        if effective_source is None:
            errors.append(f"No vanilla source template found for {rel_path}")
            continue

        override_file = mod_root / winning_owner / "generated" / "decls" / rel_path
        if not override_file.is_file():
            continue

        override_content = override_file.read_text(encoding="utf-8").replace("\r\n", "\n")

        expected_override = effective_source.replace(
            "\tedit = {\n",
            "\tedit = {\n\t\tcurrencyToGive = {\n\t\t\tnum = 0;\n\t\t}\n",
            1,
        )
        if override_content != expected_override:
            errors.append(f"Vanilla source template drift for {rel_path} (source owner: {source_owner_found})")

    return errors


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
