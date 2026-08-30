"""Shared Archipelago semantic presentation colors."""

from __future__ import annotations


ARCHIPELAGO_PRESENTATION_COLORS = {
    "text": "#f1f3ef",
    "location": "#43bfc7",
    "player_self": "#a8d52a",
    "player_remote": "#f28a35",
    "item_filler": "#8fc8e8",
    "item_useful": "#438bc4",
    "item_progression": "#bd84e8",
    "item_trap": "#9edb45",
}


def item_classification_color_key(classification: int, *, trap: bool = False) -> str:
    """Resolve Archipelago item flags to Activity's semantic color key."""
    if trap or classification & 0b00100:
        return "item_trap"
    if classification & 0b00001:
        return "item_progression"
    if classification & 0b00010:
        return "item_useful"
    return "item_filler"


def color_rgb_floats(color: str) -> tuple[float, float, float]:
    """Convert a canonical #RRGGBB color to normalized engine RGB values."""
    if len(color) != 7 or not color.startswith("#"):
        raise ValueError(f"unsupported semantic color: {color!r}")
    return (
        int(color[1:3], 16) / 255,
        int(color[3:5], 16) / 255,
        int(color[5:7], 16) / 255,
    )
