"""Validation, fingerprinting, and extraction of hermetic frozen room compiler resources.

Prebuilt canonical room compiler resources represent the boundary between
the authorial/game-dependent build stage and the portable CI/release stage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

DEPENDENCY_DIRS = (
    "content",
    "level_configs",
    "manifests",
    "packaging/mod_assets",
    "player_templates",
    "doom_eap/content",
    "tools/decls",
    "tools/maps",
)

DEPENDENCY_FILES = (
    "data/automap_specs.json",
    "data/campaign_goal_contract.json",
    "data/checked_location_visuals.json",
    "data/content_identity.json",
    "data/items.json",
    "data/item_classifications.json",
    "data/item_replay_policies.json",
    "data/location_names.json",
    "data/map_sources.json",
    "data/mission_complete_map_contracts.json",
    "data/options_schema.json",
    "data/publisher_contracts.json",
    "data/start_inventory_catalog.json",
    "data/weapon_mods.json",
    "tools/content/compile_content_catalog.py",
    "tools/content/compile_start_inventory_catalog.py",
    "tools/release/room_payloads.py",
)

CANONICAL_RESOURCE_FILENAMES = (
    "base_mod.zip",
    "room_payloads.zip",
    "room_payload_manifest.json",
)

METADATA_FILENAMES = (
    "ROOM_RESOURCES_PROVENANCE.json",
    "SHA256SUMS.txt",
)


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fp:
        while chunk := fp.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def compute_room_resource_input_fingerprint(repo_root: Path | None = None) -> tuple[str, dict[str, str]]:
    """Compute deterministic SHA-256 fingerprint across all content/data inputs affecting room resources."""
    root = (repo_root or REPO_ROOT).resolve()
    files_to_hash: dict[str, str] = {}

    for rel_dir in DEPENDENCY_DIRS:
        d = root / rel_dir
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if p.is_file() and not p.name.endswith((".pyc", ".pyo")) and "__pycache__" not in p.parts:
                rel = p.relative_to(root).as_posix()
                files_to_hash[rel] = sha256_file(p)

    for rel_file in DEPENDENCY_FILES:
        p = root / rel_file
        if p.is_file():
            files_to_hash[rel_file] = sha256_file(p)

    sorted_items = sorted(files_to_hash.items())
    manifest_bytes = "".join(f"{k}:{v}\n" for k, v in sorted_items).encode("utf-8")
    fingerprint = hashlib.sha256(manifest_bytes).hexdigest()
    return fingerprint, files_to_hash


def get_frozen_bundle_dir(repo_root: Path | None = None, version: str = "v0.5.1") -> Path:
    root = (repo_root or REPO_ROOT).resolve()
    version_dir = version if version.startswith("v") else f"v{version}"
    return root / "packaging" / "room_resources" / version_dir


def validate_prebuilt_room_resources(
    bundle_dir: Path,
    repo_root: Path | None = None,
    expected_version: str = "0.5.1",
) -> dict[str, Any]:
    """Validate frozen room compiler resources against the current content input fingerprint and schema contracts."""
    root = (repo_root or REPO_ROOT).resolve()
    bundle_path = bundle_dir.resolve()

    if not bundle_path.is_dir():
        raise ValueError(f"Prebuilt room resources bundle directory missing: {bundle_path}")

    # 1. Verify existence of canonical files and metadata
    for filename in (*CANONICAL_RESOURCE_FILENAMES, *METADATA_FILENAMES):
        target = bundle_path / filename
        if not target.is_file():
            raise ValueError(f"Required file missing from room resources bundle: {filename} at {target}")

    # 2. Verify SHA256SUMS.txt
    sums_path = bundle_path / "SHA256SUMS.txt"
    sums_lines = sums_path.read_text(encoding="utf-8").splitlines()
    recorded_sums: dict[str, str] = {}
    for line in sums_lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            recorded_sums[parts[1].lstrip("*").strip()] = parts[0].lower()

    for filename in CANONICAL_RESOURCE_FILENAMES:
        if filename not in recorded_sums:
            raise ValueError(f"File {filename} missing from SHA256SUMS.txt in {bundle_path}")
        actual_hash = sha256_file(bundle_path / filename)
        if actual_hash.lower() != recorded_sums[filename]:
            raise ValueError(
                f"Checksum mismatch for {filename}: expected {recorded_sums[filename]}, got {actual_hash}"
            )

    # 3. Parse and validate ROOM_RESOURCES_PROVENANCE.json
    prov_path = bundle_path / "ROOM_RESOURCES_PROVENANCE.json"
    try:
        provenance = json.loads(prov_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"Corrupt ROOM_RESOURCES_PROVENANCE.json: {e}") from e

    norm_expected = expected_version.lstrip("v")
    norm_actual = str(provenance.get("release_version", "")).lstrip("v")
    if norm_actual != norm_expected:
        raise ValueError(
            f"Release version mismatch in provenance: expected {norm_expected}, got {norm_actual}"
        )

    for res_name, key in [
        ("base_mod.zip", "base_mod"),
        ("room_payloads.zip", "room_payloads"),
        ("room_payload_manifest.json", "room_payload_manifest"),
    ]:
        rec = provenance.get(key, {})
        actual_hash = sha256_file(bundle_path / res_name)
        actual_size = (bundle_path / res_name).stat().st_size
        if rec.get("sha256", "").lower() != actual_hash.lower():
            raise ValueError(
                f"Provenance SHA mismatch for {res_name}: expected {rec.get('sha256')}, got {actual_hash}"
            )
        if rec.get("size") != actual_size:
            raise ValueError(
                f"Provenance size mismatch for {res_name}: expected {rec.get('size')}, got {actual_size}"
            )

    # 4. Strict NO STALE ARTIFACT: current content input fingerprint must match provenance
    current_fingerprint, current_hashes = compute_room_resource_input_fingerprint(root)
    expected_fingerprint = provenance.get("room_resource_input_fingerprint")
    if current_fingerprint != expected_fingerprint:
        # Diagnose divergence
        old_hashes = provenance.get("source_hashes", {})
        diffs = []
        for k in set(current_hashes) | set(old_hashes):
            old_h = old_hashes.get(k)
            new_h = current_hashes.get(k)
            if old_h is None:
                diffs.append(f"  + added: {k}")
            elif new_h is None:
                diffs.append(f"  - removed: {k}")
            elif old_h != new_h:
                diffs.append(f"  * changed: {k} (old={old_h[:12]}..., new={new_h[:12]}...)")
        diff_summary = "\n".join(diffs[:15])
        if len(diffs) > 15:
            diff_summary += f"\n  ... and {len(diffs) - 15} more differences"
        raise ValueError(
            f"STALE ROOM RESOURCES: content input fingerprint mismatch.\n"
            f"Expected: {expected_fingerprint}\n"
            f"Current:  {current_fingerprint}\n"
            f"Diverging inputs:\n{diff_summary}"
        )

    # 5. Canonical validate_room_resources contract
    from scripts.release.assemble_ci_artifact import validate_room_resources

    manifest_doc = validate_room_resources(bundle_path, repo_root=root)

    return {
        "status": "PASS",
        "bundle_dir": str(bundle_path),
        "fingerprint": current_fingerprint,
        "provenance": provenance,
        "manifest_doc": manifest_doc,
    }


def export_prebuilt_room_resources(
    bundle_dir: Path,
    target_dir: Path,
    repo_root: Path | None = None,
    expected_version: str = "0.5.1",
) -> dict[str, Any]:
    """Validate frozen room resources and export them to target directory."""
    result = validate_prebuilt_room_resources(
        bundle_dir=bundle_dir,
        repo_root=repo_root,
        expected_version=expected_version,
    )
    target = target_dir.resolve()
    target.mkdir(parents=True, exist_ok=True)

    for filename in (*CANONICAL_RESOURCE_FILENAMES, *METADATA_FILENAMES):
        src = bundle_dir / filename
        dst = target / filename
        shutil.copy2(src, dst)

    print(f"Exported verified room resources to {target}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate or export prebuilt room compiler resources.")
    parser.add_argument("--check", action="store_true", help="Validate frozen room resources bundle")
    parser.add_argument("--export-dir", type=Path, default=None, help="Export validated room resources to directory")
    parser.add_argument("--bundle-dir", type=Path, default=None, help="Path to frozen room resources bundle")
    parser.add_argument("--version", type=str, default="0.5.1", help="Expected release version")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help="Repository root path")

    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    bundle_dir = (args.bundle_dir or get_frozen_bundle_dir(repo_root, args.version)).resolve()

    try:
        if args.export_dir is not None:
            res = export_prebuilt_room_resources(
                bundle_dir=bundle_dir,
                target_dir=args.export_dir,
                repo_root=repo_root,
                expected_version=args.version,
            )
        else:
            res = validate_prebuilt_room_resources(
                bundle_dir=bundle_dir,
                repo_root=repo_root,
                expected_version=args.version,
            )

        print(f"[OK] Room resources validated successfully (fingerprint={res['fingerprint']})")
        print(f"  base_mod SHA-256:             {res['provenance']['base_mod']['sha256']}")
        print(f"  room_payloads SHA-256:        {res['provenance']['room_payloads']['sha256']}")
        print(f"  room_payload_manifest SHA-256:{res['provenance']['room_payload_manifest']['sha256']}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
