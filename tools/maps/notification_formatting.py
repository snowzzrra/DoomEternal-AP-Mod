"""Canonical notification keys and text for received Archipelago items."""

from __future__ import annotations

import re
from typing import Any

_AP_COLOR_CODE = re.compile(r"(?:\{[^{}]*\}|\^[0-9A-Fa-f])")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


def _sanitized_name(item_name: str) -> str:
    if not isinstance(item_name, str):
        raise ValueError("notification item name must be a string")
    name = _CONTROL_CHARACTER.sub("", _AP_COLOR_CODE.sub("", item_name)).strip()
    if not name:
        raise ValueError("notification item name cannot be empty")
    return name


def _progressive_stage_count(definition: Any, stage: int | None) -> int | None:
    if not isinstance(definition, dict) or definition.get("type") != "progressive_perk":
        if stage is not None:
            raise ValueError("only progressive notifications accept a stage")
        return None
    perks = definition.get("perks")
    if not isinstance(perks, list) or not perks:
        raise ValueError("progressive notification requires non-empty perks")
    if not isinstance(stage, int) or not 0 <= stage < len(perks):
        raise ValueError("progressive notification stage is out of range")
    return len(perks)


def notification_key(item_id: int, definition: Any, *, stage: int | None = None) -> str:
    if not isinstance(item_id, int):
        raise ValueError("notification item ID must be an integer")
    if _progressive_stage_count(definition, stage) is None:
        return f"#str_ap_notify_item_{item_id}"
    return f"#str_ap_notify_item_{item_id}_{stage}"


def notification_text(
    item_id: int,
    definition: Any,
    item_name: str,
    *,
    stage: int | None = None,
) -> str:
    del item_id
    name = _sanitized_name(item_name)
    progressive_count = _progressive_stage_count(definition, stage)
    if progressive_count is not None:
        name = f"{name} ({stage + 1}/{progressive_count})"
    elif isinstance(definition, dict) and definition.get("type") == "currency":
        count = definition.get("count", 1)
        if not isinstance(count, int) or count <= 0:
            raise ValueError("currency notification count must be positive")
        if count > 1:
            name = f"{name} x{count}"
    return f"AP: {name}"
