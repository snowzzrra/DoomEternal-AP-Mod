from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path

from tools.release.build_cache import content_key, publish, restore

APWORLD_ZIP_EPOCH = (2026, 1, 1, 0, 0, 0)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apworld_fingerprint(apworld_root: Path) -> str:
    files = {
        str(path.relative_to(apworld_root)): _sha256(path)
        for path in sorted(apworld_root.rglob("*.py"))
        if "__pycache__" not in path.parts
    }
    return hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _canonicalize_native_apworld_manifest_name(members, path):
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
    return temporary


def canonicalize_native_apworld(path: Path) -> None:
    """Normalize metadata only after Archipelago's native builder chose content."""
    with zipfile.ZipFile(path, "r") as source:
        infos = source.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise RuntimeError("native APWorld contains duplicate members")
        members = {info.filename: source.read(info) for info in infos if not info.is_dir()}

    temporary = _canonicalize_native_apworld_manifest_name(members, path)
    temporary.replace(path)


def _build_apworld_world_source(archipelago_python, archipelago_source, cache_root, output):
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
            ("rule_builder", archipelago_source / "rule_builder"),
            ("worlds/__init__.py", archipelago_source / "worlds/__init__.py"),
            ("worlds/AutoWorld.py", archipelago_source / "worlds/AutoWorld.py"),
            ("worlds/Files.py", archipelago_source / "worlds/Files.py"),
            ("worlds/LauncherComponents.py", archipelago_source / "worlds/LauncherComponents.py"),
            ("data/GLOBAL.apignore", archipelago_source / "data/GLOBAL.apignore"),
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
    build_env = os.environ.copy()
    build_env["SKIP_REQUIREMENTS_UPDATE"] = "1"
    with tempfile.TemporaryDirectory(prefix="apworld-build-", dir=output.parent) as temporary:
        work = Path(temporary)
        for source in archipelago_source.glob("*.py"):
            shutil.copy2(source, work / source.name)
        shutil.copytree(archipelago_source / "rule_builder", work / "rule_builder")
        (work / "worlds").mkdir()
        for source in (archipelago_source / "worlds").glob("*.py"):
            shutil.copy2(source, work / "worlds" / source.name)
        shutil.copytree(archipelago_source / "worlds/generic", work / "worlds/generic")
        shutil.copytree(archipelago_source / "worlds/doometernal", work / "worlds/doometernal")
        (work / "data").mkdir()
        shutil.copy2(archipelago_source / "data/GLOBAL.apignore", work / "data/GLOBAL.apignore")
        subprocess.run(
            (archipelago_python, str(work / "Launcher.py"), "Build APWorlds", "--",
             "DOOM Eternal", "--skip_open_folder"),
            check=True, cwd=work, env=build_env,
        )
        candidate = work / "build/apworlds/doometernal.apworld"
        if not candidate.is_file() or candidate.is_symlink():
            raise RuntimeError(f"canonical APWorld build did not produce {candidate}")
        canonicalize_native_apworld(candidate)
        shutil.copyfile(candidate, output)
    publish(cache_root, "apworld", key, output.parent, (output.name,))
    restored, restore_reason = restore(cache_root, "apworld", key, output.parent, (output.name,))
    if not restored:
        raise RuntimeError(f"published APWorld cache could not be restored: {restore_reason}")
    return output


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
    return _build_apworld_world_source(archipelago_python, archipelago_source, cache_root, output)


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
