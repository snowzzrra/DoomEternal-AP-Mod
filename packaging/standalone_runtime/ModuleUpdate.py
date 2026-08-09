"""Disable pip self-mutation inside frozen standalone runtime."""

from __future__ import annotations


def update(*_args, **_kwargs) -> None:
    return None
