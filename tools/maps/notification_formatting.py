"""Canonical notification keys and text for Archipelago receipts."""

from __future__ import annotations

import re
from typing import Any

_AP_COLOR_CODE = re.compile(r"(?:\{[^{}]*\}|\^[0-9A-Fa-f])")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
ITEM_NOTIFICATION_HEADER_KEY = "#str_ap_item_received"
MAJOR_NOTIFICATION_KEY_PREFIX = "#str_ap_notify_item_received_"
LOCATION_NOTIFICATION_HEADER_KEY = "#str_ap_location_sent"
PROGRESSIVE_NOTIFICATION_ITEM_IDS = frozenset({
    7770017,
    7770088,
    7770092,
    7770901,
    7770902,
})

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


def progressive_notification_stage_count(item_id: int, definition: Any) -> int | None:
    if item_id not in PROGRESSIVE_NOTIFICATION_ITEM_IDS:
        return None
    if not isinstance(definition, dict) or definition.get("type") not in {
        "progressive_perk", "progressive_item",
    }:
        raise ValueError(f"progressive notification item {item_id} has invalid metadata")
    perks = definition.get("perks")
    if not isinstance(perks, list) or not perks:
        raise ValueError("progressive notification requires non-empty perks")
    return len(perks)


def _progressive_stage_count(
    item_id: int, definition: Any, stage: int | None
) -> int | None:
    count = progressive_notification_stage_count(item_id, definition)
    if count is None:
        if stage is not None:
            raise ValueError("only progressive notifications accept a stage")
        return None
    if not isinstance(stage, int) or not 0 <= stage < count:
        raise ValueError("progressive notification stage is out of range")
    return count


def notification_key(item_id: int, definition: Any, *, stage: int | None = None) -> str:
    if not isinstance(item_id, int):
        raise ValueError("notification item ID must be an integer")
    if _progressive_stage_count(item_id, definition, stage) is None:
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
    progressive_count = _progressive_stage_count(item_id, definition, stage)
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
    name = _sanitized_name(item_name)
    progressive_count = _progressive_stage_count(item_id, definition, stage)
    if progressive_count is not None:
        if stage is None:
            raise ValueError("progressive notification stage is required")
        if name.startswith("Progressive "):
            name = name[len("Progressive "):]
        name = f"{name} ({stage + 1}/{progressive_count})"
    return f"{name} {suffix}"


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


PLACEMENT_SENT_KEY_PREFIX = "#str_ap_location_sent_"


def placement_sent_key(location_id: int) -> str:
    """Return placement-aware string-table key for one AP location receipt."""
    if not isinstance(location_id, int) or location_id <= 0:
        raise ValueError(f"invalid AP location id: {location_id!r}")
    return f"{PLACEMENT_SENT_KEY_PREFIX}{location_id}"


def _placement_identity(placement: Any) -> tuple[str, str, bool, bool]:
    if not isinstance(placement, dict):
        raise ValueError("placement presentation requires a placement record")
    item_name = _sanitized_name(str(placement.get("item_name", "")))
    recipient_name = _sanitized_name(str(placement.get("recipient_name", "")))
    return item_name, recipient_name, bool(placement.get("local")), bool(placement.get("trap"))


def location_sent_text(placement: Any) -> str:
    """Return the placement-aware ``AP LOCATION SENT`` header text.

    Local checks report ``FOUND YOUR <ITEM>``; remote and trap checks report
    ``SENT <ITEM> TO <RECIPIENT>``.
    """
    item_name, recipient_name, local, trap = _placement_identity(placement)
    if local and not trap:
        return f"FOUND YOUR {item_name.upper()}"
    return f"SENT {item_name.upper()} TO {recipient_name.upper()}"


def item_receipt_text(
    item_name: str,
    *,
    local: bool,
    trap: bool,
    recipient_name: str,
) -> str:
    """Return the placement-aware received-item subtitle text.

    Traps never reveal their recipient; local items read ``YOUR <ITEM>``;
    remote items read ``<ITEM> FOR <RECIPIENT>``.
    """
    item = _sanitized_name(item_name)
    if trap:
        return "A TRAP FOR SOMEONE"
    if local:
        return f"YOUR {item.upper()}"
    recipient = _sanitized_name(recipient_name)
    return f"{item.upper()} FOR {recipient.upper()}"
