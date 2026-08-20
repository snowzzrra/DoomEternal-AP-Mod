"""Canonical notification keys and text for Archipelago receipts."""

from __future__ import annotations

import re
from typing import Any

_AP_COLOR_CODE = re.compile(r"(?:\{[^{}]*\}|\^[0-9A-Fa-f])")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
ITEM_NOTIFICATION_HEADER_KEY = "#str_ap_item_received"
MAJOR_NOTIFICATION_KEY_PREFIX = "#str_ap_notify_item_received_"
LOCATION_NOTIFICATION_HEADER_KEY = "#str_ap_location_sent"

ITEM_NOTIFICATION_TITLE = {
    "english": "AP ITEM RECEIVED",
    "portuguese": "ITEM AP RECEBIDO",
}
MAJOR_NOTIFICATION_SUFFIX = {
    "english": "RECEIVED",
    "portuguese": "RECEBIDO",
}
LOCATION_NOTIFICATION_TITLE = {
    "english": "AP LOCATION SENT",
    "portuguese": "LOCALIZAÇÃO AP ENVIADA",
}


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


def major_notification_key(item_id: int, definition: Any, *, stage: int | None = None) -> str:
    """Return item-specific major header key, including progressive stage."""
    return major_notification_key_from_item_key(
        notification_key(item_id, definition, stage=stage)
    )


def major_notification_key_from_item_key(item_key: str) -> str:
    """Convert filler subtitle key to distinct major receipt header key."""
    prefix = "#str_ap_notify_item_"
    if not isinstance(item_key, str) or not item_key.startswith(prefix):
        raise ValueError(f"invalid item notification key: {item_key!r}")
    suffix = item_key[len(prefix):]
    if not re.fullmatch(r"\d+(?:_\d+)?", suffix):
        raise ValueError(f"invalid item notification key suffix: {item_key!r}")
    return f"{MAJOR_NOTIFICATION_KEY_PREFIX}{suffix}"


def notification_text(
    item_id: int,
    definition: Any,
    item_name: str,
    *,
    stage: int | None = None,
) -> str:
    # Validate item identity and style metadata even though subtitle text is
    # intentionally independent of progressive/currency presentation details.
    notification_key(item_id, definition, stage=stage)
    name = _sanitized_name(item_name)
    progressive_count = _progressive_stage_count(definition, stage)
    if progressive_count is not None:
        if stage is None:
            raise ValueError("progressive notification stage is required")
    elif isinstance(definition, dict) and definition.get("type") == "currency":
        count = definition.get("count", 1)
        if not isinstance(count, int) or count <= 0:
            raise ValueError("currency notification count must be positive")
    return name


def major_notification_text(
    item_id: int,
    definition: Any,
    item_name: str,
    *,
    stage: int | None = None,
    locale: str = "english",
) -> str:
    """Return localized major receipt text: ``<ITEM> RECEIVED``."""
    notification_text(item_id, definition, item_name, stage=stage)
    try:
        suffix = MAJOR_NOTIFICATION_SUFFIX[locale]
    except KeyError as error:
        raise ValueError(f"unsupported notification locale: {locale}") from error
    return f"{_sanitized_name(item_name)} {suffix}"


def location_notification_key(location_id: int) -> str:
    """Return canonical string-table key for one AP location receipt."""
    if not isinstance(location_id, int) or location_id <= 0:
        raise ValueError(f"invalid AP location id: {location_id!r}")
    return f"#str_ap_location_{location_id}"


def location_notification_text(location_name: str) -> str:
    """Return canonical AP location receipt text."""
    if not isinstance(location_name, str) or not location_name.strip():
        raise ValueError("AP location name cannot be empty")
    return location_name.strip()
