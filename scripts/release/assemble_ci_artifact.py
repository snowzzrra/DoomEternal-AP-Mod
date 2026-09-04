#!/usr/bin/env python3
"""Local release assembly script.

Consumes the single downloaded GitHub Actions handoff artifact and precompiled
canonical room compiler resources, and produces exactly TWO final public release ZIPs:
  - DoomEternalArchipelago-<version>-linux-x86_64.zip
  - DoomEternalArchipelago-<version>-windows-x86_64.zip
plus a companion SHA256SUMS.txt.

This script performs ZERO compilation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import zipfile
from itertools import product
from pathlib import Path
from typing import Any, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from doom_eap.content.content_catalog import load_content_catalog
from tools.release.release_manifest import (
    MANIFEST_FILENAME,
    build_release_manifest,
    validate_release_manifest,
    write_release_manifest,
)
from tools.release.room_payloads import (
    assemble_room_files,
    load_room_payload_manifest,
    read_zip,
)
from tools.validation.release_layout import (
    ROOM_COMPILER_RESOURCE_FILES,
    public_file_members,
)

FORBIDDEN_DIR_NAMES = frozenset({
    ".git", ".cache", ".pytest_cache", "__pycache__", ".serena",
    "test", "tests", "venv", ".venv", "fuzz_output",
})


def sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_deterministic_zip(source_dir: Path, output_zip_path: Path, prefix: str = "DoomEternalArchipelago") -> None:
    """Create a deterministic zip archive from a source directory under a single root prefix."""
    output_zip_path.parent.mkdir(parents=True, exist_ok=True)
    temp_zip = output_zip_path.with_suffix(".tmp.zip")

    files_to_add: list[tuple[str, Path]] = []
    for root, dirs, files in os.walk(source_dir):
        dirs[:] = sorted([d for d in dirs if d not in FORBIDDEN_DIR_NAMES])
        for f in sorted(files):
            full_path = Path(root) / f
            rel_path = full_path.relative_to(source_dir).as_posix()
            files_to_add.append((f"{prefix}/{rel_path}", full_path))

    with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for arcname, file_path in files_to_add:
            zinfo = zipfile.ZipInfo(arcname)
            zinfo.date_time = (2026, 1, 1, 0, 0, 0)
            st = file_path.stat()
            if bool(st.st_mode & stat.S_IXUSR):
                zinfo.external_attr = (0o755 | stat.S_IFREG) << 16
            else:
                zinfo.external_attr = (0o644 | stat.S_IFREG) << 16
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(zinfo, file_path.read_bytes())

    temp_zip.replace(output_zip_path)


def validate_handoff_structure(handoff_dir: Path) -> dict[str, Any]:
    """Validate handoff artifact files and manifest."""
    manifest_path = handoff_dir / "BUILD-MANIFEST.json"
    if not manifest_path.is_file():
        raise ValueError(f"Handoff missing BUILD-MANIFEST.json at {manifest_path}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"Failed to parse BUILD-MANIFEST.json: {e}") from e

    sums_path = handoff_dir / "SHA256SUMS.txt"
    if sums_path.is_file():
        for line in sums_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                expected_hash, rel_path = parts[0], parts[1].lstrip("*").strip()
                target_file = handoff_dir / rel_path
                if not target_file.is_file():
                    raise ValueError(f"Handoff file listed in SHA256SUMS.txt is missing: {rel_path}")
                actual_hash = sha256_file(target_file)
                if actual_hash.lower() != expected_hash.lower():
                    raise ValueError(
                        f"Checksum mismatch in handoff for {rel_path}: expected {expected_hash}, got {actual_hash}"
                    )

    linux_launcher = handoff_dir / "linux" / "DoomEternalArchipelagoLauncher"
    if not linux_launcher.is_file():
        raise ValueError(f"Missing Linux launcher at {linux_launcher}")
    linux_magic = linux_launcher.read_bytes()[:4]
    if linux_magic != b"\x7fELF":
        raise ValueError(f"Linux launcher at {linux_launcher} does not have valid ELF magic (got {linux_magic!r})")

    win_launcher = handoff_dir / "windows" / "DoomEternalArchipelagoLauncher.exe"
    if not win_launcher.is_file():
        raise ValueError(f"Missing Windows launcher at {win_launcher}")
    win_magic = win_launcher.read_bytes()[:2]
    if win_magic != b"MZ":
        raise ValueError(f"Windows launcher at {win_launcher} does not have valid MZ magic (got {win_magic!r})")

    apworld_file = handoff_dir / "shared" / "doometernal.apworld"
    if not apworld_file.is_file():
        raise ValueError(f"Missing APWorld at {apworld_file}")
    if not zipfile.is_zipfile(apworld_file):
        raise ValueError(f"APWorld at {apworld_file} is not a valid zip archive")

    ap_client_file = handoff_dir / "shared" / "client" / "ap_client.exe"
    if not ap_client_file.is_file():
        raise ValueError(f"Missing native ap_client.exe at {ap_client_file}")
    ap_client_magic = ap_client_file.read_bytes()[:2]
    if ap_client_magic != b"MZ":
        raise ValueError(f"ap_client.exe does not have valid MZ magic (got {ap_client_magic!r})")

    return manifest


def validate_room_resources(resources_dir: Path, repo_root: Path | None = None) -> dict[str, Any]:
    """Validate that the canonical room resource set is complete, uncorrupted, and contract-compatible."""
    base_mod = resources_dir / "base_mod.zip"
    room_payloads = resources_dir / "room_payloads.zip"
    room_manifest = resources_dir / "room_payload_manifest.json"

    for path, name in (
        (base_mod, "base_mod.zip"),
        (room_payloads, "room_payloads.zip"),
        (room_manifest, "room_payload_manifest.json"),
    ):
        if not path.is_file():
            raise ValueError(f"Required room compiler resource is missing: {name} at {path}")

    manifest_doc = load_room_payload_manifest(room_manifest)
    base_members = read_zip(base_mod)
    payload_members = read_zip(room_payloads)

    if set(base_members) != set(manifest_doc.get("base_members", [])):
        raise ValueError("base_mod.zip archive members disagree with room_payload_manifest.json")

    expected_payload_members = {
        state["member"]
        for record in manifest_doc.get("maps", {}).values()
        for state in record.get("states", [])
        if state.get("source") == "replacement"
    }
    if set(payload_members) != expected_payload_members:
        raise ValueError("room_payloads.zip archive members disagree with room_payload_manifest.json")

    for map_key, record in manifest_doc.get("maps", {}).items():
        for state in record.get("states", []):
            if state.get("source") == "base":
                target = record.get("target_member")
                if target in base_members:
                    actual_hash = hashlib.sha256(base_members[target]).hexdigest()
                    if actual_hash != state.get("sha256"):
                        raise ValueError(f"Base room member hash mismatch for {map_key}/{target}")
            elif state.get("source") == "replacement":
                member = state.get("member")
                if member in payload_members:
                    actual_hash = hashlib.sha256(payload_members[member]).hexdigest()
                    if actual_hash != state.get("sha256"):
                        raise ValueError(f"Replacement room member hash mismatch for {map_key}/{member}")

    if repo_root is not None:
        catalog = load_content_catalog(repo_root)
        enabled_map_keys = {spec.key for spec in catalog.enabled_maps()}
        if set(manifest_doc.get("maps", {})) != enabled_map_keys:
            raise ValueError(
                f"Room payload map set disagrees with enabled maps: "
                f"missing={sorted(enabled_map_keys - set(manifest_doc.get('maps', {})))}, "
                f"extra={sorted(set(manifest_doc.get('maps', {})) - enabled_map_keys)}"
            )

    keys = manifest_doc.get("physical_option_keys", [])
    for values in product((False, True), repeat=len(keys)):
        opts = dict(zip(keys, values))
        assembled, selected = assemble_room_files(base_mod, room_payloads, manifest_doc, opts)
        if len(selected) != len(manifest_doc.get("maps", {})):
            raise ValueError(f"Room selection non-deterministic for options {opts}")

    return manifest_doc


def assemble_platform_release(
    platform_name: str,
    handoff_dir: Path,
    repo_root: Path,
    room_resources_dir: Path,
    manifest: dict[str, Any],
    stage_dir: Path,
) -> Path:
    """Populate staging tree for a platform release."""
    out_root = stage_dir / platform_name
    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    # 1. Copy platform launcher
    if platform_name == "linux":
        src_launcher = handoff_dir / "linux" / "DoomEternalArchipelagoLauncher"
        dst_launcher = out_root / "DoomEternalArchipelagoLauncher"
        shutil.copy2(src_launcher, dst_launcher)
        dst_launcher.chmod(dst_launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    elif platform_name == "windows":
        src_launcher = handoff_dir / "windows" / "DoomEternalArchipelagoLauncher.exe"
        dst_launcher = out_root / "DoomEternalArchipelagoLauncher.exe"
        shutil.copy2(src_launcher, dst_launcher)
    else:
        raise ValueError(f"Unsupported platform: {platform_name}")

    # 2. Copy doometernal.apworld
    src_apworld = handoff_dir / "shared" / "doometernal.apworld"
    dst_apworld = out_root / "doometernal.apworld"
    shutil.copy2(src_apworld, dst_apworld)

    # 3. Copy docs & license
    for doc in ["README.md", "INSTALL.md", "LICENSE"]:
        src_doc = repo_root / "docs" / doc if (repo_root / "docs" / doc).is_file() else repo_root / doc
        if src_doc.is_file():
            shutil.copy2(src_doc, out_root / doc)

    # 4. Copy client runtime, resources & templates
    client_dir = out_root / "client"
    client_dir.mkdir(parents=True, exist_ok=True)

    # Shared client binaries from handoff
    shared_client_dir = handoff_dir / "shared" / "client"
    if shared_client_dir.is_dir():
        for item in shared_client_dir.iterdir():
            if item.is_file():
                shutil.copy2(item, client_dir / item.name)

    # Copy precompiled canonical room resources
    resources_dst = client_dir / "resources"
    resources_dst.mkdir(parents=True, exist_ok=True)
    for filename in ("base_mod.zip", "room_payloads.zip", "room_payload_manifest.json"):
        src_res = room_resources_dir / filename
        if not src_res.is_file():
            raise ValueError(f"Room resource file missing during staging: {src_res}")
        shutil.copy2(src_res, resources_dst / filename)

    # Repository client templates & scripts
    repo_client_example = repo_root / "packaging" / "client" / "ap_config.example.json"
    if repo_client_example.is_file():
        shutil.copy2(repo_client_example, client_dir / "ap_config.example.json")

    run_bridge = repo_root / "scripts" / "launch" / "run_bridge.sh"
    if run_bridge.is_file():
        dst_bridge = client_dir / "run_bridge.sh"
        shutil.copy2(run_bridge, dst_bridge)
        dst_bridge.chmod(dst_bridge.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    val_install = repo_root / "scripts" / "validate" / "runtime_install.sh"
    if val_install.is_file():
        dst_val = client_dir / "validate_runtime_install.sh"
        shutil.copy2(val_install, dst_val)
        dst_val.chmod(dst_val.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Data directory
    repo_data = repo_root / "data"
    if repo_data.is_dir():
        dst_data = client_dir / "data"
        shutil.copytree(repo_data, dst_data, dirs_exist_ok=True)

    # Manifests directory
    repo_manifests = repo_root / "manifests"
    if repo_manifests.is_dir():
        dst_manifests = client_dir / "manifests"
        dst_manifests.mkdir(parents=True, exist_ok=True)
        for f in repo_manifests.glob("*.json"):
            shutil.copy2(f, dst_manifests / f.name)

    # Player templates directory
    repo_templates = repo_root / "player_templates"
    if repo_templates.is_dir():
        dst_templates = client_dir / "player_templates"
        shutil.copytree(repo_templates, dst_templates, dirs_exist_ok=True)

    # Canonical content tree (maps, catalog, global_runtime.json)
    repo_content = repo_root / "content"
    if repo_content.is_dir():
        dst_content = client_dir / "content"
        shutil.copytree(
            repo_content,
            dst_content,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            dirs_exist_ok=True,
        )

    # doom_eap python package
    repo_doom_eap = repo_root / "doom_eap"
    if repo_doom_eap.is_dir():
        dst_doom_eap = client_dir / "doom_eap"
        shutil.copytree(
            repo_doom_eap,
            dst_doom_eap,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
            dirs_exist_ok=True,
        )

    # tools python package
    repo_tools = repo_root / "tools"
    if repo_tools.is_dir():
        dst_tools = client_dir / "tools"
        dst_tools.mkdir(parents=True, exist_ok=True)
        for subpkg in ("decls", "maps", "release"):
            src_sub = repo_tools / subpkg
            if src_sub.is_dir():
                shutil.copytree(
                    src_sub,
                    dst_tools / subpkg,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                    dirs_exist_ok=True,
                )
        if (repo_tools / "__init__.py").is_file():
            shutil.copy2(repo_tools / "__init__.py", dst_tools / "__init__.py")

    # Copy top-level doom_logo.png if available
    logo_src = repo_root.parent / "Archipelago" / "worlds" / "doometernal" / "doom_logo.png"
    if logo_src.is_file():
        shutil.copy2(logo_src, client_dir / "doom_logo.png")

    # Generate bridge_identity.json
    bridge_src = repo_root / "doom_eap" / "runtime" / "bridge_client.py"
    if bridge_src.is_file() and (repo_data / "content_identity.json").is_file():
        content_id = json.loads((repo_data / "content_identity.json").read_text(encoding="utf-8"))
        bridge_sha = sha256_file(bridge_src)
        bridge_id_doc = {
            "protocol": content_id.get("bridge_protocol_version", 2),
            "game": "DOOM Eternal",
            "sha256": bridge_sha,
            "revision": f"mission-unified-{bridge_sha[:12]}",
            "item_notifications": {
                "enabled": True,
                "revision": 2,
                "experimental": False,
            },
        }
        (client_dir / "bridge_identity.json").write_text(
            json.dumps(bridge_id_doc, indent=2) + "\n", encoding="utf-8"
        )

    # Save handoff build provenance metadata in client directory
    provenance_doc = {
        "version_label": manifest.get("version_label", "v0.5.1"),
        "architecture": "x86_64",
        "mod_commit_sha": manifest.get("mod", {}).get("resolved_sha", "unknown"),
        "apworld_commit_sha": manifest.get("apworld", {}).get("resolved_sha", "unknown"),
        "build": manifest.get("build", {}),
    }
    (client_dir / "BUILD-PROVENANCE.json").write_text(
        json.dumps(provenance_doc, indent=2) + "\n", encoding="utf-8"
    )

    # 5. Build canonical RELEASE_MANIFEST.json
    declared_public_files = sorted(
        list(public_file_members(out_root)) + [MANIFEST_FILENAME]
    )
    canonical_manifest = build_release_manifest(
        repo_root,
        room_resources=room_resources_dir,
        release_version=manifest.get("version_label", "v0.5.1"),
        public_files=declared_public_files,
        apworld=manifest.get("apworld"),
    )
    write_release_manifest(out_root / MANIFEST_FILENAME, canonical_manifest)
    validate_release_manifest(canonical_manifest, package_root=out_root)

    return out_root


def audit_platform_parity(linux_root: Path, windows_root: Path) -> None:
    """Verify that shared files match byte-for-byte between Linux and Windows packages."""
    linux_files = {p.relative_to(linux_root).as_posix(): p for p in linux_root.rglob("*") if p.is_file()}
    windows_files = {p.relative_to(windows_root).as_posix(): p for p in windows_root.rglob("*") if p.is_file()}

    # Check allowed launcher differences
    if "DoomEternalArchipelagoLauncher" not in linux_files:
        raise ValueError("Linux package missing DoomEternalArchipelagoLauncher")
    if "DoomEternalArchipelagoLauncher.exe" in linux_files:
        raise ValueError("Linux package must NOT contain DoomEternalArchipelagoLauncher.exe")

    if "DoomEternalArchipelagoLauncher.exe" not in windows_files:
        raise ValueError("Windows package missing DoomEternalArchipelagoLauncher.exe")
    if "DoomEternalArchipelagoLauncher" in windows_files:
        raise ValueError("Windows package must NOT contain Linux DoomEternalArchipelagoLauncher")

    # Filter out platform-specific launcher and canonical release manifest
    ignored_keys = {"DoomEternalArchipelagoLauncher", "DoomEternalArchipelagoLauncher.exe", MANIFEST_FILENAME}
    shared_linux_keys = set(linux_files.keys()) - ignored_keys
    shared_windows_keys = set(windows_files.keys()) - ignored_keys

    if shared_linux_keys != shared_windows_keys:
        missing_in_win = sorted(shared_linux_keys - shared_windows_keys)
        missing_in_lin = sorted(shared_windows_keys - shared_linux_keys)
        raise ValueError(
            f"Package parity mismatch between Linux and Windows: missing in Windows={missing_in_win}, missing in Linux={missing_in_lin}"
        )

    for rel_path in sorted(shared_linux_keys):
        lin_path = linux_files[rel_path]
        win_path = windows_files[rel_path]
        lin_hash = sha256_file(lin_path)
        win_hash = sha256_file(win_path)
        if lin_hash != win_hash:
            raise ValueError(
                f"Byte mismatch in shared file {rel_path}: Linux={lin_hash} vs Windows={win_hash}"
            )


def audit_final_zip(zip_path: Path, expected_platform: str, repo_root: Path | None = None) -> None:
    """Audit single output release ZIP against canonical contracts."""
    if not zip_path.is_file():
        raise ValueError(f"Release ZIP does not exist: {zip_path}")

    with zipfile.ZipFile(zip_path, "r") as zf:
        infolist = zf.infolist()
        if not infolist:
            raise ValueError(f"Release ZIP is empty: {zip_path}")

        names = [zi.filename for zi in infolist]
        if len(names) != len(set(names)):
            raise ValueError(f"Release ZIP contains duplicate paths: {zip_path}")

        for name in names:
            if not name.startswith("DoomEternalArchipelago/"):
                raise ValueError(f"Entry {name!r} in {zip_path.name} does not start with root 'DoomEternalArchipelago/'")
            parts = Path(name).parts
            if any(p in FORBIDDEN_DIR_NAMES for p in parts):
                raise ValueError(f"Forbidden directory name found in {zip_path.name}: {name}")
            if ".." in parts or Path(name).is_absolute():
                raise ValueError(f"Path traversal or absolute path found in {zip_path.name}: {name}")

        # Check canonical manifest
        if f"DoomEternalArchipelago/{MANIFEST_FILENAME}" not in names:
            raise ValueError(f"{MANIFEST_FILENAME} missing in {zip_path.name}")

        if "DoomEternalArchipelago/doometernal.apworld" not in names:
            raise ValueError(f"doometernal.apworld missing in {zip_path.name}")

        if "DoomEternalArchipelago/client/ap_client.exe" not in names:
            raise ValueError(f"client/ap_client.exe missing in {zip_path.name}")

        # Mandatory room compiler resources
        for resource_rel in ROOM_COMPILER_RESOURCE_FILES:
            expected_zip_path = f"DoomEternalArchipelago/{resource_rel}"
            if expected_zip_path not in names:
                raise ValueError(f"Mandatory room compiler resource missing in {zip_path.name}: {resource_rel}")

        if expected_platform == "linux":
            if "DoomEternalArchipelago/DoomEternalArchipelagoLauncher" not in names:
                raise ValueError(f"Linux launcher missing in {zip_path.name}")
            if "DoomEternalArchipelago/DoomEternalArchipelagoLauncher.exe" in names:
                raise ValueError(f"Windows launcher erroneously found in Linux zip: {zip_path.name}")
        elif expected_platform == "windows":
            if "DoomEternalArchipelago/DoomEternalArchipelagoLauncher.exe" not in names:
                raise ValueError(f"Windows launcher missing in {zip_path.name}")
            if "DoomEternalArchipelago/DoomEternalArchipelagoLauncher" in names:
                raise ValueError(f"Linux launcher erroneously found in Windows zip: {zip_path.name}")

    with tempfile.TemporaryDirectory(prefix="doomeap_audit_zip_") as temp_extract_str:
        temp_extract = Path(temp_extract_str)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(temp_extract)
        extracted_root = temp_extract / "DoomEternalArchipelago"
        manifest_path = extracted_root / MANIFEST_FILENAME
        manifest_doc = json.loads(manifest_path.read_text(encoding="utf-8"))
        validate_release_manifest(manifest_doc, package_root=extracted_root)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Assemble public Linux and Windows release ZIPs from a GitHub Actions build handoff artifact and canonical room resources."
    )
    parser.add_argument("handoff_artifact", nargs="?", type=Path, default=None, help="Path to handoff ZIP or extracted directory")
    parser.add_argument("--handoff", type=Path, default=None, help="Path to handoff ZIP or extracted directory")
    parser.add_argument("--room-resources-dir", type=Path, default=None, help="Path to precompiled room compiler resources")
    parser.add_argument("--version", type=str, default=None, help="Expected release version label")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "build/final-release", help="Output directory")
    parser.add_argument("--expect-mod-sha", type=str, default=None, help="Expected MOD commit SHA")
    parser.add_argument("--expect-apworld-sha", type=str, default=None, help="Expected APWorld commit SHA")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help="Path to DoomEternal-AP-Mod repository root")

    args = parser.parse_args()

    handoff_arg = args.handoff or args.handoff_artifact
    if handoff_arg is None:
        parser.error("handoff artifact path must be provided as positional argument or via --handoff")
    handoff_path = handoff_arg.resolve()
    if not handoff_path.exists():
        print(f"Error: Handoff artifact path not found: {handoff_path}", file=sys.stderr)
        return 1

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    repo_root = args.repo_root.resolve()

    resources_dir = (
        args.room_resources_dir.resolve()
        if args.room_resources_dir is not None
        else (repo_root / "build/release/client/resources").resolve()
    )
    if not resources_dir.is_dir():
        print(f"Error: Room resources directory not found: {resources_dir}", file=sys.stderr)
        return 1

    print("--> Validating precompiled room resources...")
    validate_room_resources(resources_dir, repo_root=repo_root)

    with tempfile.TemporaryDirectory(prefix="doomeap_release_stage_") as temp_stage_str:
        temp_stage = Path(temp_stage_str)
        extracted_handoff = temp_stage / "handoff"

        if handoff_path.is_file() and (handoff_path.suffix == ".zip" or zipfile.is_zipfile(handoff_path)):
            with zipfile.ZipFile(handoff_path, "r") as zf:
                zf.extractall(extracted_handoff)
            top_level = list(extracted_handoff.iterdir())
            if len(top_level) == 1 and top_level[0].is_dir() and (top_level[0] / "BUILD-MANIFEST.json").is_file():
                extracted_handoff = top_level[0]
        elif handoff_path.is_dir():
            extracted_handoff = handoff_path
        else:
            print(f"Error: Unrecognized handoff format at {handoff_path}", file=sys.stderr)
            return 1

        print("--> Validating handoff artifact...")
        manifest = validate_handoff_structure(extracted_handoff)
        version_label = manifest.get("version_label", "v0.5.1")

        if args.version and args.version != version_label:
            raise ValueError(f"Version mismatch: handoff says {version_label}, expected {args.version}")

        mod_sha = manifest.get("mod", {}).get("resolved_sha", "")
        if args.expect_mod_sha and not mod_sha.startswith(args.expect_mod_sha):
            raise ValueError(f"MOD SHA mismatch: handoff says {mod_sha}, expected {args.expect_mod_sha}")

        apworld_sha = manifest.get("apworld", {}).get("resolved_sha", "")
        if args.expect_apworld_sha and not apworld_sha.startswith(args.expect_apworld_sha):
            raise ValueError(f"APWorld SHA mismatch: handoff says {apworld_sha}, expected {args.expect_apworld_sha}")

        print(f"--> Building Linux release staging tree ({version_label})...")
        linux_stage = assemble_platform_release("linux", extracted_handoff, repo_root, resources_dir, manifest, temp_stage)

        print(f"--> Building Windows release staging tree ({version_label})...")
        windows_stage = assemble_platform_release("windows", extracted_handoff, repo_root, resources_dir, manifest, temp_stage)

        print("--> Running platform parity audit...")
        audit_platform_parity(linux_stage, windows_stage)

        linux_zip_name = f"DoomEternalArchipelago-{version_label}-linux-x86_64.zip"
        windows_zip_name = f"DoomEternalArchipelago-{version_label}-windows-x86_64.zip"

        linux_zip_path = output_dir / linux_zip_name
        windows_zip_path = output_dir / windows_zip_name

        print(f"--> Creating {linux_zip_name}...")
        write_deterministic_zip(linux_stage, linux_zip_path)
        audit_final_zip(linux_zip_path, "linux", repo_root=repo_root)

        print(f"--> Creating {windows_zip_name}...")
        write_deterministic_zip(windows_stage, windows_zip_path)
        audit_final_zip(windows_zip_path, "windows", repo_root=repo_root)

        sums_file = output_dir / "SHA256SUMS.txt"
        sums_content = f"{sha256_file(linux_zip_path)}  {linux_zip_name}\n{sha256_file(windows_zip_path)}  {windows_zip_name}\n"
        sums_file.write_text(sums_content, encoding="utf-8")

        print("\n=======================================================")
        print("PUBLIC RELEASE PACKAGES ASSEMBLED SUCCESSFULLY!")
        print(f"Linux ZIP:    {linux_zip_path} ({linux_zip_path.stat().st_size} bytes)")
        print(f"Windows ZIP:  {windows_zip_path} ({windows_zip_path.stat().st_size} bytes)")
        print(f"Checksums:    {sums_file}")
        print("=======================================================")

    return 0


if __name__ == "__main__":
    sys.exit(main())
