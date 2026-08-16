"""Canonical public root layout for playable releases."""

from __future__ import annotations

from pathlib import Path


RELEASE_ROOTS = frozenset({
    "client",
    "doometernal.apworld",
    "README.md",
    "INSTALL.md",
    "LICENSE",
    "RELEASE_MANIFEST.json",
})

LAUNCHER_NAMES = frozenset({
    "DoomEternalArchipelagoLauncher",
    "DoomEternalArchipelagoLauncher.exe",
})

ROOM_COMPILER_RESOURCE_FILES = frozenset({
    "client/resources/base_mod.zip",
    "client/resources/room_payloads.zip",
    "client/resources/room_payload_manifest.json",
})


def expected_release_roots(launcher: str) -> set[str]:
    if launcher not in LAUNCHER_NAMES:
        raise ValueError(f"unsupported release launcher: {launcher}")
    return set(RELEASE_ROOTS | {launcher})


def public_file_members(root: Path) -> set[str]:
    """Return every regular file exposed by an extracted public release."""
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }


def validate_public_file_members(root: Path, declared: list[str]) -> None:
    if len(declared) != len(set(declared)):
        raise ValueError("release manifest public_files contains duplicates")
    if any(
        not isinstance(path, str)
        or not path
        or "\\" in path
        or Path(path).is_absolute()
        or ".." in Path(path).parts
        for path in declared
    ):
        raise ValueError("release manifest public_files contains invalid paths")
    actual = public_file_members(root)
    if actual != set(declared):
        raise ValueError(
            "release manifest public_files disagrees with package membership: "
            f"missing={sorted(set(declared) - actual)} extra={sorted(actual - set(declared))}"
        )
