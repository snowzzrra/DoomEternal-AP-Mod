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
    AGGREGATE_SOURCE_OWNER,
    AGGREGATE_TARGET_OWNER,
    AGGREGATE_LIST_PATH,
    CHILD_SOURCE_OWNER,
    CHILD_TARGET_OWNER,
    DHB_DUMMY_PATH,
    _challenge_paths,
    _dhb_dummy_override,
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
    """Find child Mission Challenge overrides in their canonical owner."""
    results: dict[str, Path] = {}
    prefix = "unlockable/mission_challenge/"
    decls = mod_root / CHILD_TARGET_OWNER / "generated" / "decls"
    if not decls.is_dir():
        return results
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
    """Validate the distinct child and aggregate vanilla owner contracts."""
    errors: list[str] = []
    child_decl_root = mod_root / CHILD_TARGET_OWNER / "generated" / "decls"
    aggregate_decl_root = mod_root / AGGREGATE_TARGET_OWNER / "generated" / "decls"
    
    if not child_decl_root.is_dir():
        errors.append(f"Child owner directory missing: {CHILD_TARGET_OWNER}")
        return errors

    for candidate in mod_root.glob("gameresources*"):
        decls = candidate / "generated" / "decls"
        if not decls.is_dir():
            continue
        for fpath in decls.rglob("*.decl"):
            rel = fpath.relative_to(decls).as_posix()
            if (
                rel.startswith("unlockable/mission_challenge/")
                and candidate.name != CHILD_TARGET_OWNER
            ):
                errors.append(
                    f"Child override found outside {CHILD_TARGET_OWNER}: "
                    f"{candidate.name}: {rel}"
                )
            if rel == AGGREGATE_LIST_PATH and candidate.name != AGGREGATE_TARGET_OWNER:
                errors.append(
                    f"Aggregate main.decl found outside {AGGREGATE_TARGET_OWNER}: "
                    f"{candidate.name}: {rel}"
                )
            if rel.startswith("warehouseofflinecontainer/"):
                errors.append(
                    f"Forbidden warehouse aggregate override found in {candidate.name}: {rel}"
                )

    overrides = _find_override_files(mod_root)
    if not overrides:
        return ["No mission challenge override files found under mod root"]
    
    errors.extend(validate_overrides_from_files(list(overrides.values()), registry_path))
    registry = load_challenge_registry(registry_path)

    aggregate_path = aggregate_decl_root / AGGREGATE_LIST_PATH
    if not aggregate_path.is_file():
        errors.append(
            f"Missing aggregate Mission Challenge override in "
            f"{AGGREGATE_TARGET_OWNER}: {AGGREGATE_LIST_PATH}"
        )
        return errors

    aggregate_source = aggregate_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    expected_aggregate, dhb_contract = _dhb_dummy_override(registry)
    if aggregate_source != expected_aggregate:
        errors.append(
            "Aggregate main.decl differs from canonical patch2 outside the approved "
            "DHB fourth-challenge registration"
        )

    blocks = [
        (index, block)
        for index, _, _, block in _level_blocks(aggregate_source)
        if "_dev_" not in block
    ]
    dhb_matches = [
        (index, block)
        for index, block in blocks
        if index == dhb_contract["level_index"]
    ]
    if len(dhb_matches) != 1:
        errors.append(f"DHB packaged registration count is {len(dhb_matches)}")
    else:
        registered = _challenge_paths(dhb_matches[0][1])
        approved = (*dhb_contract["real_challenges"], DHB_DUMMY_PATH)
        if registered != approved:
            errors.append(
                f"DHB packaged registration is {registered}, expected {approved}"
            )

    dummy_registry_owners = [
        entry for entry in registry["mission_challenges"]
        if entry["completion_owner"]["path"].removesuffix(".decl") == DHB_DUMMY_PATH
        or entry["signal"].get("unlockable") == DHB_DUMMY_PATH
    ]
    if dummy_registry_owners:
        errors.append("DHB dummy challenge acquired AP location ownership")
    dhb_aggregate = next(
        aggregate for aggregate in registry["all_mission_challenges"]
        if aggregate["mission_key"] == "e1m4"
    )
    if tuple(dhb_aggregate["signal"]["children"]) != (7770172, 7770173, 7770174):
        errors.append("DHB AP aggregate children include anything except three real challenges")

    if re.search(
        r"\bcompletionUnlock\b|warehouseofflinecontainer|CURRENCY_SENTINEL_BATTERY",
        aggregate_source,
    ):
        errors.append("Aggregate main.decl contains a forbidden reward hack")

    protected_paths = (
        "propitem/propitem/batteries/sentinel_battery.decl",
        "entitydef/interact/hub/battery_socket_for_engine.decl",
    )
    for candidate in mod_root.glob("gameresources*"):
        decls = candidate / "generated" / "decls"
        for protected in protected_paths:
            if (decls / protected).exists():
                errors.append(
                    f"Protected Sentinel Battery contract was overridden in "
                    f"{candidate.name}: {protected}"
                )

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

    candidate_owners = ["gameresources_patch3", "gameresources_patch2", "gameresources_patch1", "gameresources"]
    registry = load_challenge_registry(registry_path)
    entries = registry.get("mission_challenges", [])

    for candidate in mod_root.glob("gameresources*"):
        decls_dir = candidate / "generated" / "decls"
        if decls_dir.is_dir():
            for fpath in decls_dir.rglob("*.decl"):
                rel_path = fpath.relative_to(decls_dir).as_posix()
                if rel_path.startswith("unlockable/mission_challenge/") or rel_path == AGGREGATE_LIST_PATH or rel_path.startswith("warehouseofflinecontainer/"):
                    found_vanilla = any(
                        (vanilla_base / owner / "generated" / "decls" / rel_path).is_file()
                        for owner in candidate_owners
                    )
                    if not found_vanilla:
                        errors.append(f"Generated DECL path does not exist in vanilla corpus: {rel_path}")

    for entry in entries:
        rel_path = entry["completion_owner"]["path"]
        source_file = (
            vanilla_base
            / CHILD_SOURCE_OWNER
            / "generated"
            / "decls"
            / rel_path
        )
        if not source_file.is_file():
            errors.append(
                f"No vanilla child source template found in {CHILD_SOURCE_OWNER}: {rel_path}"
            )
            continue
        effective_source = source_file.read_text(encoding="utf-8").replace("\r\n", "\n")

        override_file = mod_root / CHILD_TARGET_OWNER / "generated" / "decls" / rel_path
        if not override_file.is_file():
            continue

        override_content = override_file.read_text(encoding="utf-8").replace("\r\n", "\n")

        expected_override = effective_source.replace(
            "\tedit = {\n",
            "\tedit = {\n\t\tcurrencyToGive = {\n\t\t\tnum = 0;\n\t\t}\n",
            1,
        )
        if override_content != expected_override:
            errors.append(
                f"Vanilla child source template drift for {rel_path} "
                f"(source owner: {CHILD_SOURCE_OWNER})"
            )

    aggregate_source_file = (
        vanilla_base
        / AGGREGATE_SOURCE_OWNER
        / "generated"
        / "decls"
        / AGGREGATE_LIST_PATH
    )
    if not aggregate_source_file.is_file():
        errors.append(
            f"Canonical aggregate source missing in {AGGREGATE_SOURCE_OWNER}: "
            f"{AGGREGATE_LIST_PATH}"
        )
        return errors

    aggregate_override_file = (
        mod_root
        / AGGREGATE_TARGET_OWNER
        / "generated"
        / "decls"
        / AGGREGATE_LIST_PATH
    )
    if aggregate_override_file.is_file():
        expected_aggregate, _ = _dhb_dummy_override(registry)
        aggregate_override = aggregate_override_file.read_text(
            encoding="utf-8"
        ).replace("\r\n", "\n")
        if aggregate_override != expected_aggregate:
            errors.append(
                "Aggregate main.decl differs from canonical patch2 outside the approved "
                "DHB fourth-challenge registration"
            )

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
