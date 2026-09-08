"""Verified APWorld fingerprint and standard-build cache interface.

This module only describes or verifies cache entries. It never treats an
unverified Generate.py output directory as reusable.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.release.build_cache import content_key, publish, restore

APWORLD_ZIP_EPOCH = (2026, 1, 1, 0, 0, 0)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _member_hashes(root: Path) -> dict[str, dict[str, int | str]] | None:
    """Return complete file inventory, or None for unsafe/incomplete trees."""
    if root.is_symlink() or not root.is_dir():
        return None
    members: dict[str, dict[str, int | str]] = {}
    try:
        paths = sorted(root.rglob("*"))
        for path in paths:
            if path.is_symlink():
                return None
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            members[relative] = {
                "sha256": _sha256(path),
                "size": path.stat().st_size,
            }
    except OSError:
        return None
    return members


def _safe_relative(value: Any) -> Path | None:
    if not isinstance(value, str):
        return None
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value:
        return None
    return path


def _verified_outputs(plan: APWorldBuildPlan, outputs: Any) -> bool:
    if not isinstance(outputs, list) or not outputs:
        return False

    expected: dict[str, dict[str, int | str]] = {}
    for item in outputs:
        if not isinstance(item, dict):
            return False
        relative = _safe_relative(item.get("path"))
        if relative is None:
            return False
        path = plan.output_root / relative
        if path.is_symlink():
            return False
        members = item.get("members")
        if members is not None:
            if not path.is_dir() or not isinstance(members, dict):
                return False
            for member, metadata in members.items():
                member_path = _safe_relative(member)
                if member_path is None or not isinstance(metadata, dict):
                    return False
                expected_path = (relative / member_path).as_posix()
                digest = metadata.get("sha256")
                size = metadata.get("size")
                if not isinstance(digest, str) or len(digest) != 64:
                    return False
                if not isinstance(size, int) or size < 0:
                    return False
                if expected_path in expected:
                    return False
                expected[expected_path] = {"sha256": digest, "size": size}
            continue
        digest = item.get("sha256")
        size = item.get("size")
        if not path.is_file() or not isinstance(digest, str) or len(digest) != 64:
            return False
        if size is not None and (not isinstance(size, int) or size < 0):
            return False
        expected_path = relative.as_posix()
        if expected_path in expected:
            return False
        expected[expected_path] = {
            "sha256": digest,
            "size": path.stat().st_size if size is None else size,
        }

    inventory = _member_hashes(plan.output_root)
    if inventory is None:
        return False
    for relative, metadata in expected.items():
        actual_metadata = inventory.get(relative)
        if actual_metadata != metadata:
            return False
        path = plan.output_root / relative
        if _sha256(path) != metadata["sha256"]:
            return False
    return set(inventory) == set(expected)


@dataclass(frozen=True)
class APWorldBuildPlan:
    fingerprint: str
    command: tuple[str, ...]
    output_root: Path
    cache_hit: bool = False


def apworld_fingerprint(apworld_root: Path) -> str:
    files = {
        str(path.relative_to(apworld_root)): _sha256(path)
        for path in sorted(apworld_root.rglob("*.py"))
        if "__pycache__" not in path.parts
    }
    return hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def canonicalize_native_apworld(path: Path) -> None:
    """Normalize metadata only after Archipelago's native builder chose content."""
    with zipfile.ZipFile(path, "r") as source:
        infos = source.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise RuntimeError("native APWorld contains duplicate members")
        members = {info.filename: source.read(info) for info in infos if not info.is_dir()}

    manifest_name = "doometernal/archipelago.json"
    if manifest_name not in members:
        raise RuntimeError(f"native APWorld is missing {manifest_name}")
    manifest = json.loads(members[manifest_name].decode("utf-8"))
    if (
        manifest.get("game") != "DOOM Eternal"
        or manifest.get("compatible_version") != 7
        or not isinstance(manifest.get("version"), int)
    ):
        raise RuntimeError(f"native APWorld manifest contract is invalid: {manifest!r}")
    if any(Path(name).is_absolute() or ".." in Path(name).parts for name in members):
        raise RuntimeError("native APWorld contains an unsafe member")

    temporary = path.with_suffix(".canonicalizing")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, APWORLD_ZIP_EPOCH)
            info.external_attr = (0o100644 << 16)
            info.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(info, members[name])
    temporary.replace(path)


def plan_apworld_build(
    *,
    python: str,
    generate_script: Path,
    player_files_path: Path,
    output_root: Path,
    apworld_root: Path,
    extra_args: Sequence[str] = (),
) -> APWorldBuildPlan:
    """Return standard Generate.py command; cache reuse remains opt-in and verified."""
    fingerprint = apworld_fingerprint(apworld_root)
    command = (
        python,
        str(generate_script),
        "--player_files_path", str(player_files_path),
        "--outputpath", str(output_root),
        *extra_args,
    )
    return APWorldBuildPlan(fingerprint, command, output_root)


def verified_cache_entry(plan: APWorldBuildPlan, receipt: Path) -> APWorldBuildPlan:
    """Mark hit only when receipt fingerprint, command, and output hashes verify."""
    try:
        document = json.loads(receipt.read_text(encoding="utf-8"))
        if (
            not isinstance(document, dict)
            or document.get("fingerprint") != plan.fingerprint
            or tuple(document.get("command", ())) != plan.command
        ):
            return plan
        if not _verified_outputs(plan, document.get("outputs")):
            return plan
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return plan
    return APWorldBuildPlan(plan.fingerprint, plan.command, plan.output_root, True)


def run_standard_build(plan: APWorldBuildPlan) -> None:
    """Execute Generate.py exactly as planned; caller owns receipt writing."""
    subprocess.run(plan.command, check=True)


def build_apworld(
    *,
    output: Path,
    archipelago_source: Path,
    archipelago_python: str,
    cache_root: Path | None = None,
) -> Path:
    """Build or restore canonical DOOM Eternal APWorld from verified cache."""
    archipelago_source = archipelago_source.resolve()
    output = output.resolve()
    if output.name != "doometernal.apworld":
        raise ValueError("APWorld output must be doometernal.apworld")
    if not (archipelago_source / "Launcher.py").is_file():
        raise ValueError(f"invalid Archipelago source: {archipelago_source}")
    world_source = archipelago_source / "worlds/doometernal"
    python_result = subprocess.run(
        (archipelago_python, "--version"),
        check=True,
        capture_output=True,
        text=True,
    )
    python_identity = (python_result.stdout or python_result.stderr).strip()
    key = content_key(
        "apworld",
        [
            ("world", world_source),
            ("Launcher.py", archipelago_source / "Launcher.py"),
            ("apworld_cache.py", Path(__file__)),
            ("Utils.py", archipelago_source / "Utils.py"),
            ("BaseClasses.py", archipelago_source / "BaseClasses.py"),
            ("Options.py", archipelago_source / "Options.py"),
            ("worlds/__init__.py", archipelago_source / "worlds/__init__.py"),
            ("worlds/AutoWorld.py", archipelago_source / "worlds/AutoWorld.py"),
            ("worlds/Files.py", archipelago_source / "worlds/Files.py"),
        ],
        config={
            "command": "Launcher.py Build APWorlds -- DOOM Eternal --skip_open_folder",
            "python": str(Path(archipelago_python).resolve()),
            "python_identity": python_identity,
            "platform": sys.platform,
            "os": os.name,
            "machine": platform.machine(),
            "architecture": platform.architecture()[0],
        },
    )
    cache_root = cache_root or Path(os.environ.get("AP_BUILD_CACHE_ROOT", REPO_ROOT / ".cache/ap-build"))
    output.parent.mkdir(parents=True, exist_ok=True)
    hit, reason = restore(cache_root, "apworld", key, output.parent, (output.name,))
    if hit:
        print(f"APWORLD cache=hit key={key}")
        return output
    print(f"APWORLD cache=miss reason={reason} key={key}")
    candidate = archipelago_source / "build/apworlds/doometernal.apworld"
    candidate.unlink(missing_ok=True)
    build_env = os.environ.copy()
    build_env["SKIP_REQUIREMENTS_UPDATE"] = "1"
    subprocess.run(
        (
            archipelago_python,
            str(archipelago_source / "Launcher.py"),
            "Build APWorlds",
            "--",
            "DOOM Eternal",
            "--skip_open_folder",
        ),
        check=True,
        cwd=archipelago_source,
        env=build_env,
    )
    if not candidate.is_file() or candidate.is_symlink():
        raise RuntimeError(f"canonical APWorld build did not produce {candidate}")
    canonicalize_native_apworld(candidate)
    output.unlink(missing_ok=True)
    shutil.copyfile(candidate, output)
    publish(cache_root, "apworld", key, output.parent, (output.name,))
    restored, restore_reason = restore(cache_root, "apworld", key, output.parent, (output.name,))
    if not restored:
        raise RuntimeError(f"published APWorld cache could not be restored: {restore_reason}")
    return output


REPO_ROOT = Path(__file__).resolve().parents[2]


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archipelago-source", type=Path, required=True)
    parser.add_argument("--archipelago-python", default=sys.executable)
    parser.add_argument("--cache-root", type=Path)
    arguments = parser.parse_args(argv)
    build_apworld(
        output=arguments.output,
        archipelago_source=arguments.archipelago_source,
        archipelago_python=arguments.archipelago_python,
        cache_root=arguments.cache_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
