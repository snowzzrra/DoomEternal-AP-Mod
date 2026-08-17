#!/usr/bin/env python3
"""Local release assembly script.

Consumes the single downloaded GitHub Actions handoff artifact and produces
exactly TWO final public release ZIPs:
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
from pathlib import Path
from typing import Any, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]

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


def assemble_platform_release(
    platform_name: str,
    handoff_dir: Path,
    repo_root: Path,
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

    # 4. Copy client runtime & templates
    client_dir = out_root / "client"
    client_dir.mkdir(parents=True, exist_ok=True)

    # Shared client binaries from handoff
    shared_client_dir = handoff_dir / "shared" / "client"
    if shared_client_dir.is_dir():
        for item in shared_client_dir.iterdir():
            if item.is_file():
                shutil.copy2(item, client_dir / item.name)

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
        dst_data.mkdir(parents=True, exist_ok=True)
        for f in repo_data.glob("*.json"):
            shutil.copy2(f, dst_data / f.name)

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

    # 5. Build provenance RELEASE-MANIFEST.json
    all_files: list[dict[str, Any]] = []
    for root, dirs, files in os.walk(out_root):
        dirs[:] = sorted([d for d in dirs if d not in FORBIDDEN_DIR_NAMES])
        for f in sorted(files):
            fp = Path(root) / f
            rel = fp.relative_to(out_root).as_posix()
            if rel != "RELEASE_MANIFEST.json":
                all_files.append({
                    "path": rel,
                    "sha256": sha256_file(fp),
                    "size": fp.stat().st_size,
                })

    launcher_file = dst_launcher
    release_manifest_doc = {
        "release_version": manifest.get("version_label", "v0.4.0-beta.4"),
        "platform": f"{platform_name}-x86_64",
        "architecture": "x86_64",
        "mod_commit_sha": manifest.get("mod", {}).get("resolved_sha", "unknown"),
        "apworld_commit_sha": manifest.get("apworld", {}).get("resolved_sha", "unknown"),
        "launcher": {
            "filename": launcher_file.name,
            "sha256": sha256_file(launcher_file),
        },
        "apworld": {
            "filename": "doometernal.apworld",
            "sha256": sha256_file(dst_apworld),
        },
        "native_client": {
            "filename": "client/ap_client.exe",
            "sha256": sha256_file(client_dir / "ap_client.exe") if (client_dir / "ap_client.exe").is_file() else "",
        },
        "files": all_files,
    }

    (out_root / "RELEASE-MANIFEST.json").write_text(
        json.dumps(release_manifest_doc, indent=2) + "\n", encoding="utf-8"
    )

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

    # Filter out platform-specific launcher and release manifest
    ignored_keys = {"DoomEternalArchipelagoLauncher", "DoomEternalArchipelagoLauncher.exe", "RELEASE-MANIFEST.json"}
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


def audit_final_zip(zip_path: Path, expected_platform: str) -> None:
    """Audit single output release ZIP."""
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

        # Check manifest
        if "DoomEternalArchipelago/RELEASE-MANIFEST.json" not in names:
            raise ValueError(f"RELEASE-MANIFEST.json missing in {zip_path.name}")

        if "DoomEternalArchipelago/doometernal.apworld" not in names:
            raise ValueError(f"doometernal.apworld missing in {zip_path.name}")

        if "DoomEternalArchipelago/client/ap_client.exe" not in names:
            raise ValueError(f"client/ap_client.exe missing in {zip_path.name}")

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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Assemble public Linux and Windows release ZIPs from a GitHub Actions build handoff artifact."
    )
    parser.add_argument("handoff_artifact", type=Path, help="Path to handoff ZIP or extracted directory")
    parser.add_argument("--version", type=str, default=None, help="Expected release version label")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "build/final-release", help="Output directory")
    parser.add_argument("--expect-mod-sha", type=str, default=None, help="Expected MOD commit SHA")
    parser.add_argument("--expect-apworld-sha", type=str, default=None, help="Expected APWorld commit SHA")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help="Path to DoomEternal-AP-Mod repository root")

    args = parser.parse_args()

    handoff_path = args.handoff_artifact.resolve()
    if not handoff_path.exists():
        print(f"Error: Handoff artifact path not found: {handoff_path}", file=sys.stderr)
        return 1

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    repo_root = args.repo_root.resolve()

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
        version_label = manifest.get("version_label", "v0.4.0-beta.4")

        if args.version and args.version != version_label:
            raise ValueError(f"Version mismatch: handoff says {version_label}, expected {args.version}")

        mod_sha = manifest.get("mod", {}).get("resolved_sha", "")
        if args.expect_mod_sha and not mod_sha.startswith(args.expect_mod_sha):
            raise ValueError(f"MOD SHA mismatch: handoff says {mod_sha}, expected {args.expect_mod_sha}")

        apworld_sha = manifest.get("apworld", {}).get("resolved_sha", "")
        if args.expect_apworld_sha and not apworld_sha.startswith(args.expect_apworld_sha):
            raise ValueError(f"APWorld SHA mismatch: handoff says {apworld_sha}, expected {args.expect_apworld_sha}")

        print(f"--> Building Linux release staging tree ({version_label})...")
        linux_stage = assemble_platform_release("linux", extracted_handoff, repo_root, manifest, temp_stage)

        print(f"--> Building Windows release staging tree ({version_label})...")
        windows_stage = assemble_platform_release("windows", extracted_handoff, repo_root, manifest, temp_stage)

        print("--> Running platform parity audit...")
        audit_platform_parity(linux_stage, windows_stage)

        linux_zip_name = f"DoomEternalArchipelago-{version_label}-linux-x86_64.zip"
        windows_zip_name = f"DoomEternalArchipelago-{version_label}-windows-x86_64.zip"

        linux_zip_path = output_dir / linux_zip_name
        windows_zip_path = output_dir / windows_zip_name

        print(f"--> Creating {linux_zip_name}...")
        write_deterministic_zip(linux_stage, linux_zip_path)
        audit_final_zip(linux_zip_path, "linux")

        print(f"--> Creating {windows_zip_name}...")
        write_deterministic_zip(windows_stage, windows_zip_path)
        audit_final_zip(windows_zip_path, "windows")

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
