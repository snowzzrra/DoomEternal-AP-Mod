"""Content-addressed cache for verified release build outputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(path: Path) -> Iterable[tuple[str, Path]]:
    if path.is_symlink() or not path.exists():
        raise ValueError(f"cache input is missing or symlinked: {path}")
    if path.is_file():
        yield "", path
        return
    for child in sorted(path.rglob("*")):
        if child.is_symlink():
            raise ValueError(f"cache input contains symlink: {child}")
        if child.is_file():
            yield child.relative_to(path).as_posix(), child


def content_key(
    kind: str,
    inputs: Sequence[tuple[str, Path]],
    *,
    config: Mapping[str, Any] | None = None,
) -> str:
    records: list[dict[str, Any]] = []
    for label, path in inputs:
        for relative, member in _files(path):
            records.append({
                "path": f"{label}/{relative}" if relative else label,
                "sha256": _sha256(member),
                "size": member.stat().st_size,
            })
    document = {
        "schema": 1,
        "kind": kind,
        "config": config or {},
        "inputs": sorted(records, key=lambda item: item["path"]),
    }
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _output_inventory(root: Path, outputs: Sequence[str]) -> dict[str, dict[str, int | str]] | None:
    expected = set(outputs)
    if len(expected) != len(outputs):
        return None
    inventory: dict[str, dict[str, int | str]] = {}
    for relative in outputs:
        relative_path = Path(relative)
        if not relative or relative_path.is_absolute() or ".." in relative_path.parts:
            return None
        path = root / relative
        if path.is_symlink() or not path.is_file():
            return None
        try:
            path.relative_to(root)
        except ValueError:
            return None
        inventory[relative] = {"sha256": _sha256(path), "size": path.stat().st_size}
    return inventory


def _member_names(root: Path) -> set[str] | None:
    if not root.is_dir() or root.is_symlink():
        return None
    names: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            return None
        if path.is_file():
            names.add(path.relative_to(root).as_posix())
    return names


def _entry(cache_root: Path, kind: str, key: str) -> Path:
    if not kind or "/" in kind or ".." in kind or not key.isalnum():
        raise ValueError("invalid cache entry identity")
    return cache_root / kind / key


def restore(
    cache_root: Path,
    kind: str,
    key: str,
    output_root: Path,
    outputs: Sequence[str],
) -> tuple[bool, str]:
    entry = _entry(cache_root, kind, key)
    receipt_path = entry / "receipt.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("schema") != 1 or receipt.get("kind") != kind or receipt.get("key") != key:
            return False, "receipt identity mismatch"
        inventory = receipt.get("outputs")
        if _member_names(entry / "files") != set(outputs):
            return False, "cached output member set mismatch"
        if inventory != _output_inventory(entry / "files", outputs):
            return False, "cached output hash or size mismatch"
        if set(inventory) != set(outputs):
            return False, "cached output set mismatch"
        output_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".cache-restore-", dir=output_root.parent))
        try:
            for relative in outputs:
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(entry / "files" / relative, destination)
            for relative in outputs:
                destination = output_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary / relative, destination)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        return True, "verified"
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False, "missing or corrupt entry"


def publish(
    cache_root: Path,
    kind: str,
    key: str,
    output_root: Path,
    outputs: Sequence[str],
) -> None:
    inventory = _output_inventory(output_root, outputs)
    if inventory is None:
        raise ValueError(f"cannot cache incomplete {kind} output")
    cache_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{kind}-", dir=cache_root))
    try:
        files_root = temporary / "files"
        for relative in outputs:
            destination = files_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(output_root / relative, destination)
        (temporary / "receipt.json").write_text(
            json.dumps({"schema": 1, "kind": kind, "key": key, "outputs": inventory},
                       sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        destination = _entry(cache_root, kind, key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            displaced = cache_root / f".{kind}-old-{key}-{os.getpid()}"
            os.replace(destination, displaced)
            try:
                os.replace(temporary, destination)
            except BaseException:
                os.replace(displaced, destination)
                raise
            if displaced.is_dir() and not displaced.is_symlink():
                shutil.rmtree(displaced, ignore_errors=True)
            else:
                displaced.unlink(missing_ok=True)
            temporary = Path()
        else:
            os.replace(temporary, destination)
            temporary = Path()

    finally:
        if str(temporary) not in {"", "."}:
            shutil.rmtree(temporary, ignore_errors=True)


def validate_source_contract(repo_root: Path) -> None:
    """Validate cache wiring from source files without requiring cache entries."""
    required = (
        repo_root / "tools/release/build_cache.py",
        repo_root / "tools/release/apworld_cache.py",
        repo_root / "tools/release/build_launcher.py",
        repo_root / "scripts/build/client.sh",
        repo_root / "scripts/build/playable_test.sh",
    )
    for path in required:
        if not path.is_file():
            raise ValueError(f"cache source contract missing: {path}")
    build_source = (repo_root / "tools/release/build_cache.py").read_text(encoding="utf-8")
    if not all(marker in build_source for marker in ("content_key", "restore", "publish", "os.replace")):
        raise ValueError("cache source contract lacks content-addressed atomic output flow")
    for relative, markers in {
        "scripts/build/client.sh": ("NATIVE_CLIENT cache=hit", "NATIVE_CLIENT cache=miss"),
        "tools/release/build_launcher.py": (
            "LAUNCHER cache=hit", "LAUNCHER cache=miss", "_launcher_inputs",
            "sys.version", "sys.platform", "platform.machine",
            "packaging/standalone_runtime",
        ),
        "tools/release/apworld_cache.py": ("APWORLD cache=hit", "APWORLD cache=miss"),
        "scripts/build/playable_test.sh": ("tools.release.apworld_cache",),
    }.items():
        text = (repo_root / relative).read_text(encoding="utf-8")
        if not all(marker in text for marker in markers):
            raise ValueError(f"cache source contract incomplete: {relative}")
def _parse_inputs(values: Sequence[str], root: Path) -> list[tuple[str, Path]]:
    result = []
    for value in values:
        path = Path(value)
        if not path.is_absolute():
            path = root / path
        result.append((value.replace("\\", "/"), path))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    key_parser = subparsers.add_parser("key")
    key_parser.add_argument("--kind", required=True)
    key_parser.add_argument("--root", type=Path, required=True)
    key_parser.add_argument("--input", action="append", required=True)
    key_parser.add_argument("--config", default="{}")
    restore_parser = subparsers.add_parser("restore")
    for command_parser in (restore_parser,):
        command_parser.add_argument("--cache-root", type=Path, required=True)
        command_parser.add_argument("--kind", required=True)
        command_parser.add_argument("--key", required=True)
        command_parser.add_argument("--output-root", type=Path, required=True)
        command_parser.add_argument("--output", action="append", required=True)
    publish_parser = subparsers.add_parser("publish")
    for command_parser in (publish_parser,):
        command_parser.add_argument("--cache-root", type=Path, required=True)
        command_parser.add_argument("--kind", required=True)
        command_parser.add_argument("--key", required=True)
        command_parser.add_argument("--output-root", type=Path, required=True)
        command_parser.add_argument("--output", action="append", required=True)
    arguments = parser.parse_args(argv)
    if arguments.command == "key":
        try:
            config = json.loads(arguments.config)
            print(content_key(arguments.kind, _parse_inputs(arguments.input, arguments.root), config=config))
            return 0
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            print(str(error), file=sys.stderr)
            return 2
    if arguments.command == "restore":
        hit, reason = restore(
            arguments.cache_root, arguments.kind, arguments.key,
            arguments.output_root, arguments.output,
        )
        if hit:
            return 0
        print(reason, file=sys.stderr)
        return 1
    publish(
        arguments.cache_root, arguments.kind, arguments.key,
        arguments.output_root, arguments.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
