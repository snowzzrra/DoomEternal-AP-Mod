#!/usr/bin/env python3
"""Capture and restore local DOOM test-save scenarios without deleting saves."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from datetime import datetime, timezone


DEFAULT_ROOT = Path(
    os.environ.get(
        "DOOM_AP_TEST_SAVES",
        Path.home() / ".local/state/doom-eternal-ap/test-saves",
    )
).expanduser()
PROJECT_ROOT = Path(__file__).resolve().parents[1]
KNOWN_SCENARIOS = (
    "HELL_ON_EARTH_SANDBOX",
    "EXULTIA_BEFORE_RUNE",
    "HUB_VISIT_1",
    "HUB_VISIT_2_NO_ICE",
    "HUB_VISIT_2_WITH_ICE",
    "CULTIST_BEFORE_LOAD_NO_ROCKET",
    "CULTIST_BEFORE_LOAD_WITH_ROCKET",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checksums(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def process_names() -> set[str]:
    names = set()
    proc = Path("/proc")
    if not proc.is_dir():
        return names
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            name = (entry / "comm").read_text(encoding="utf-8").strip().lower()
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore").lower()
        except (OSError, UnicodeError):
            continue
        names.add(name)
        names.add(cmdline)
    return names


def assert_runtime_stopped():
    active = [
        marker
        for marker in ("doometernalx64vk.exe", "ap_client.exe", "bridge_client.py")
        if any(marker in process for process in process_names())
    ]
    if active:
        raise RuntimeError("Refusing save restore while runtime is active: " + ", ".join(active))


def load_project_config() -> dict:
    config_path = Path(os.environ.get("DOOM_AP_CONFIG_FILE", PROJECT_ROOT / "ap_config.json"))
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def discover_save_base(explicit: str | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_dir():
            raise RuntimeError(f"Explicit save path is not a directory: {path}")
        return path
    configured = load_project_config().get("save_games_dir")
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(Path.home().glob(".local/share/Steam/userdata/*/782330/remote"))
    candidates.extend(Path("/run/media/system/Eris/SteamLibrary/steamapps/compatdata/782330/pfx/drive_c/users/steamuser/Saved Games/id Software/DOOMEternal/base").parent.glob("base"))
    existing = [path.resolve() for path in candidates if path.is_dir()]
    if not existing:
        raise RuntimeError("No DOOM save base found; pass --path explicitly")
    return max(existing, key=lambda path: path.stat().st_mtime_ns)


def select_latest_save(save_base: Path) -> Path:
    save_dirs = [path for path in save_base.iterdir() if path.is_dir()]
    if not save_dirs:
        raise RuntimeError(f"No save directories below {save_base}")
    return max(save_dirs, key=lambda path: path.stat().st_mtime_ns)


def scenario_dir(root: Path, name: str) -> Path:
    if not name or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for char in name):
        raise RuntimeError("Scenario names must use A-Z, 0-9 and underscore")
    return root / name


def capture(name: str, root: Path, explicit: str | None, confirm: bool) -> Path:
    destination = scenario_dir(root, name)
    if destination.exists() and not confirm:
        raise RuntimeError("Scenario exists; pass --confirm to replace its archived copy")
    save_base = discover_save_base(explicit)
    source = save_base if explicit and not save_base.name.lower().startswith("base") else select_latest_save(save_base)
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=root))
    try:
        shutil.copytree(source, staging / "save")
        manifest = {
            "schema_version": 1,
            "scenario": name,
            "captured_utc": datetime.now(timezone.utc).isoformat(),
            "source_path": str(source),
            "steam_cloud_warning": "Disable/synchronize Steam Cloud before restore.",
            "checksums": checksums(staging / "save"),
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        if destination.exists():
            backup = root / f"{name}.archive-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            os.replace(destination, backup)
        os.replace(staging, destination)
        return destination
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def restore(name: str, root: Path, explicit: str | None, confirm: bool) -> Path:
    if not confirm:
        raise RuntimeError("Restore requires --confirm")
    assert_runtime_stopped()
    archived = scenario_dir(root, name)
    manifest_path = archived / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"Scenario not found: {name}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = archived / "save"
    if checksums(source) != manifest.get("checksums"):
        raise RuntimeError("Scenario checksum mismatch; refusing restore")
    destination = Path(explicit).expanduser().resolve() if explicit else Path(manifest["source_path"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.restore.", dir=destination.parent))
    shutil.rmtree(staging)
    try:
        shutil.copytree(source, staging)
        if checksums(staging) != manifest["checksums"]:
            raise RuntimeError("Restore staging checksum mismatch")
        if destination.exists():
            backup = destination.with_name(
                f"{destination.name}.pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            )
            os.replace(destination, backup)
        os.replace(staging, destination)
        return destination
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def list_scenarios(root: Path):
    found = []
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if (path / "manifest.json").is_file():
                found.append(path.name)
    for name in KNOWN_SCENARIOS:
        print(f"{'captured' if name in found else 'recommended'}\t{name}")
    for name in sorted(set(found) - set(KNOWN_SCENARIOS)):
        print(f"captured\t{name}")


def info(name: str, root: Path):
    manifest = scenario_dir(root, name) / "manifest.json"
    if not manifest.is_file():
        raise RuntimeError(f"Scenario not found: {name}")
    print(manifest.read_text(encoding="utf-8"), end="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    for command in ("capture", "restore"):
        child = subparsers.add_parser(command)
        child.add_argument("scenario")
        child.add_argument("--path")
        child.add_argument("--confirm", action="store_true")
    child = subparsers.add_parser("info")
    child.add_argument("scenario")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            list_scenarios(args.root)
        elif args.command == "capture":
            result = capture(args.scenario, args.root, args.path, args.confirm)
            print(f"Captured {args.scenario}: {result}")
        elif args.command == "restore":
            result = restore(args.scenario, args.root, args.path, args.confirm)
            print("WARNING: verify Steam Cloud state before launching the game.")
            print(f"Restored {args.scenario}: {result}")
        else:
            info(args.scenario, args.root)
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
