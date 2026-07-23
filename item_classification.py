"""Packaged Archipelago item classification and notification-style policy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ITEM_CLASSIFICATION_SCHEMA_VERSION = 1
ITEM_CLASSIFICATION_FILLER = 0b00000
ITEM_CLASSIFICATION_PROGRESSION = 0b00001
ITEM_CLASSIFICATION_USEFUL = 0b00010
ITEM_CLASSIFICATION_TRAP = 0b00100
ITEM_CLASSIFICATION_SKIP_BALANCING = 0b01000
ITEM_CLASSIFICATION_KNOWN_MASK = (
    ITEM_CLASSIFICATION_PROGRESSION
    | ITEM_CLASSIFICATION_USEFUL
    | ITEM_CLASSIFICATION_TRAP
    | ITEM_CLASSIFICATION_SKIP_BALANCING
)


def notification_style_for_item(item_id: int, classification: int) -> str:
    """Choose the one allowed received-item style using AP bit precedence."""
    if not isinstance(item_id, int) or isinstance(item_id, bool):
        raise ValueError("item ID must be an integer")
    if not isinstance(classification, int) or isinstance(classification, bool):
        raise ValueError(f"item {item_id} has no valid classification")
    if classification < 0 or classification & ~ITEM_CLASSIFICATION_KNOWN_MASK:
        raise ValueError(
            f"item {item_id} has unknown classification bits: {classification}"
        )
    if classification & ITEM_CLASSIFICATION_TRAP:
        return "major"
    if classification & ITEM_CLASSIFICATION_PROGRESSION:
        return "major"
    if classification & ITEM_CLASSIFICATION_USEFUL:
        return "major"
    if classification == ITEM_CLASSIFICATION_FILLER:
        return "filler"
    raise ValueError(
        f"item {item_id} classification has modifiers without a base class: "
        f"{classification}"
    )


def normalize_network_classification(item_id: int, classification: int) -> int:
    """Ignore the non-semantic AP skip-balancing modifier before comparison."""
    notification_style_for_item(item_id, classification)
    return classification & ~ITEM_CLASSIFICATION_SKIP_BALANCING


def notification_entity_name(
    item_id: int,
    classification: int,
    *,
    stage: int | None = None,
) -> str:
    style = notification_style_for_item(item_id, classification)
    suffix = f"_{stage}" if stage is not None else ""
    return f"ap_notify_item_{style}_{item_id}{suffix}"


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_item_classification_identity(path: Path) -> dict[int, dict[str, Any]]:
    """Load and strictly validate the deterministic packaged identity."""
    data = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_pairs,
    )
    if data.get("schema_version") != ITEM_CLASSIFICATION_SCHEMA_VERSION:
        raise ValueError("unsupported item classification schema")
    revision = data.get("item_mapping_revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise ValueError("item classification identity lacks mapping revision")
    raw_items = data.get("items")
    if not isinstance(raw_items, dict):
        raise ValueError("item classification identity lacks items")

    result: dict[int, dict[str, Any]] = {}
    for raw_item_id, entry in raw_items.items():
        try:
            item_id = int(raw_item_id)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid item classification ID: {raw_item_id}") from error
        if str(item_id) != raw_item_id or item_id in result:
            raise ValueError(f"duplicate or non-canonical item ID: {raw_item_id}")
        if not isinstance(entry, dict) or set(entry) != {"name", "classification"}:
            raise ValueError(f"item {item_id} has invalid classification identity")
        if not isinstance(entry["name"], str) or not entry["name"].strip():
            raise ValueError(f"item {item_id} has no canonical name")
        notification_style_for_item(item_id, entry["classification"])
        result[item_id] = entry
    return result


def load_item_classifications(path: Path) -> dict[int, int]:
    return {
        item_id: entry["classification"]
        for item_id, entry in load_item_classification_identity(path).items()
    }
