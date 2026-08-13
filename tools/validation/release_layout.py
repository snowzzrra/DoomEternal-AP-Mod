"""Canonical public root layout for playable releases."""

from __future__ import annotations


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


def expected_release_roots(launcher: str) -> set[str]:
    if launcher not in LAUNCHER_NAMES:
        raise ValueError(f"unsupported release launcher: {launcher}")
    return set(RELEASE_ROOTS | {launcher})
