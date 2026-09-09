"""CommonClient name/Hint/PrintJSON presentation adapters, loaded after AP bootstrap."""
from dataclasses import dataclass
from collections.abc import Callable, Mapping
from NetUtils import Hint, HintStatus, JSONMessagePart, JSONTypes

@dataclass(frozen=True)
class ProtocolNames:
    item_names: object
    location_names: object
    player_names: Mapping
    slot_concerns_self: Callable[[int], bool]

ARCHIPELAGO_EVENT_SCHEMA = 1
ARCHIPELAGO_EVENT_PLAIN_LIMIT = 512
ARCHIPELAGO_EVENT_SEGMENT_LIMIT = 128
ARCHIPELAGO_EVENT_SEGMENT_COUNT = 64
_ARCHIPELAGO_TEXT_TYPES = frozenset({
    "text",
    "color",
    "hint_status",
})
_ARCHIPELAGO_ITEM_TYPES = frozenset({
    "item_name",
    "item_id",
})
_ARCHIPELAGO_LOCATION_TYPES = frozenset({
    "location_name",
    "location_id",
})
def _bounded_event_text(value, limit):
    """Return bounded plain text with control characters removed."""
    if not isinstance(value, str):
        return ""
    value = value.replace("\r", " ").replace("\n", " ")
    output = []
    for character in value:
        if not character.isprintable():
            continue
        if len(output) >= limit:
            break
        output.append(character)
    return "".join(output)


def _fallback_archipelago_segment(part):
    raw_text = part.get("text") if isinstance(part, dict) else None
    if not isinstance(raw_text, str):
        raw_text = "[unavailable]"
    return {"type": "text", "text": _bounded_event_text(raw_text, ARCHIPELAGO_EVENT_SEGMENT_LIMIT)}, raw_text


def _valid_part_type(part):
    if not isinstance(part, dict):
        return None
    part_type = part.get("type", JSONTypes.text.value)
    part_type = getattr(part_type, "value", part_type)
    return part_type if isinstance(part_type, str) else None


def _item_event_classification(flags):
    if not isinstance(flags, int) or isinstance(flags, bool) or flags < 0:
        return None
    from doom_eap.content.item_classification import (
        ITEM_CLASSIFICATION_PROGRESSION,
        ITEM_CLASSIFICATION_TRAP,
        ITEM_CLASSIFICATION_USEFUL,
    )

    if flags & ITEM_CLASSIFICATION_TRAP:
        return "trap"
    if flags & ITEM_CLASSIFICATION_PROGRESSION:
        return "progression"
    if flags & ITEM_CLASSIFICATION_USEFUL:
        return "useful"
    return "filler"


def _format_archipelago_part(context, part: "JSONMessagePart"):
    part_type = _valid_part_type(part)
    if part_type in _ARCHIPELAGO_TEXT_TYPES:
        raw_text = part.get("text") if isinstance(part, dict) else None
        if isinstance(raw_text, str):
            return {"type": "text", "text": _bounded_event_text(raw_text, ARCHIPELAGO_EVENT_SEGMENT_LIMIT)}, raw_text
        return _fallback_archipelago_segment(part)

    if part_type in _ARCHIPELAGO_ITEM_TYPES:
        raw_text = part.get("text") if isinstance(part, dict) else None
        classification = _item_event_classification(part.get("flags", 0)) if isinstance(part, dict) else None
        if not isinstance(raw_text, str) or classification is None:
            return _fallback_archipelago_segment(part)
        if part_type == JSONTypes.item_id.value:
            player = part.get("player")
            if not isinstance(player, int) or isinstance(player, bool):
                return _fallback_archipelago_segment(part)
            try:
                item_text = context.item_names.lookup_in_slot(int(raw_text), player)
            except (AttributeError, TypeError, ValueError, KeyError, LookupError, AssertionError):
                return _fallback_archipelago_segment(part)
            if not isinstance(item_text, str):
                return _fallback_archipelago_segment(part)
            raw_text = item_text
        return {
            "type": "item",
            "text": _bounded_event_text(raw_text, ARCHIPELAGO_EVENT_SEGMENT_LIMIT),
            "classification": classification,
        }, raw_text

    if part_type in _ARCHIPELAGO_LOCATION_TYPES:
        raw_text = part.get("text") if isinstance(part, dict) else None
        if not isinstance(raw_text, str):
            return _fallback_archipelago_segment(part)
        if part_type == JSONTypes.location_id.value:
            player = part.get("player")
            if not isinstance(player, int) or isinstance(player, bool):
                return _fallback_archipelago_segment(part)
            try:
                location_text = context.location_names.lookup_in_slot(int(raw_text), player)
            except (AttributeError, TypeError, ValueError, KeyError, LookupError, AssertionError):
                return _fallback_archipelago_segment(part)
            if not isinstance(location_text, str):
                return _fallback_archipelago_segment(part)
            raw_text = location_text
        return {"type": "location", "text": _bounded_event_text(raw_text, ARCHIPELAGO_EVENT_SEGMENT_LIMIT)}, raw_text

    if part_type in {JSONTypes.player_id.value, JSONTypes.player_name.value}:
        raw_text = part.get("text") if isinstance(part, dict) else None
        player = part.get("player") if isinstance(part, dict) else None
        if part_type == JSONTypes.player_id.value:
            if not isinstance(raw_text, str):
                return _fallback_archipelago_segment(part)
            try:
                player = int(raw_text)
            except (TypeError, ValueError):
                return _fallback_archipelago_segment(part)
            try:
                player_text = context.player_names.get(player, raw_text)
            except (AttributeError, TypeError):
                return _fallback_archipelago_segment(part)
            if not isinstance(player_text, str):
                return _fallback_archipelago_segment(part)
            raw_text = player_text
        if not isinstance(raw_text, str) or not isinstance(player, int) or isinstance(player, bool):
            return _fallback_archipelago_segment(part)
        try:
            is_self = bool(context.slot_concerns_self(player))
        except Exception:
            return _fallback_archipelago_segment(part)
        return {
            "type": "player",
            "text": _bounded_event_text(raw_text, ARCHIPELAGO_EVENT_SEGMENT_LIMIT),
            "self": is_self,
        }, raw_text

    return _fallback_archipelago_segment(part)


def format_archipelago_event(context, args):
    parts = args.get("data") if isinstance(args, dict) else None
    if not isinstance(parts, (list, tuple)):
        parts = (None,)
    segments = []
    plain_parts = []
    for part in parts[:ARCHIPELAGO_EVENT_SEGMENT_COUNT]:
        try:
            segment, raw_text = _format_archipelago_part(context, part)
        except Exception:
            segment, raw_text = _fallback_archipelago_segment(part)
        segments.append(segment)
        plain_parts.append(raw_text if isinstance(raw_text, str) else "[unavailable]")
    return {
        "schema": ARCHIPELAGO_EVENT_SCHEMA,
        "plain": _bounded_event_text("".join(plain_parts), ARCHIPELAGO_EVENT_PLAIN_LIMIT),
        "segments": segments,
    }


def emit_hints(key, source, names, emit, logger, update_kind="DATA_RECEIVED") -> None:
    if key is None:
        return
    records = []
    rejected = 0
    row_arities = []
    if isinstance(source, (list, tuple)):
        for raw in source:
            if isinstance(raw, dict):
                hint_fields = (
                    "receiving_player",
                    "finding_player",
                    "location",
                    "item",
                    "found",
                    "entrance",
                    "item_flags",
                    "status",
                )
                required_keys = set(hint_fields) | {"class"}
                if (
                    raw.get("class") != "Hint"
                    or set(raw) != required_keys
                    or not all(isinstance(raw[field], int) and not isinstance(raw[field], bool)
                               for field in hint_fields[:4] + hint_fields[6:])
                    or not isinstance(raw["found"], bool)
                    or not isinstance(raw["entrance"], str)
                ):
                    rejected += 1
                    row_arities.append(type(raw).__name__)
                    continue
                try:
                    hint = Hint(*(raw[field] for field in hint_fields))
                except (TypeError, ValueError):
                    rejected += 1
                    row_arities.append(type(raw).__name__)
                    continue
            elif not isinstance(raw, (list, tuple)) or not 5 <= len(raw) <= 8:
                rejected += 1
                row_arities.append(len(raw) if isinstance(raw, (list, tuple)) else type(raw).__name__)
                continue
            else:
                try:
                    hint = Hint(*raw)
                except (TypeError, ValueError):
                    rejected += 1
                    row_arities.append(len(raw))
                    continue
            try:
                status = HintStatus(hint.status)
                status_value, status_name = int(status), status.name
            except (TypeError, ValueError):
                status_value, status_name = int(HintStatus.HINT_UNSPECIFIED), "HINT_UNSPECIFIED"
            try:
                item_name = names.item_names.lookup_in_slot(hint.item, hint.receiving_player)
            except (KeyError, LookupError, AttributeError):
                item_name = f"Unknown item ({hint.item})"
            try:
                location_name = names.location_names.lookup_in_slot(hint.location, hint.finding_player)
            except (KeyError, LookupError, AttributeError):
                location_name = f"Unknown location ({hint.location})"
            records.append({
                "receiving_player": hint.receiving_player,
                "receiving_player_name": names.player_names.get(hint.receiving_player, str(hint.receiving_player)),
                "finding_player": hint.finding_player,
                "finding_player_name": names.player_names.get(hint.finding_player, str(hint.finding_player)),
                "location": hint.location,
                "location_name": location_name,
                "item": hint.item,
                "item_name": item_name,
                "found": hint.found,
                "entrance": hint.entrance,
                "item_flags": hint.item_flags,
                "status": status_value,
                "status_name": status_name,
            })
    elif source is not None:
        rejected = 1
        row_arities.append(type(source).__name__)
    if rejected:
        logger.warning(
            "HINTS_DATA_REJECTED key=%s source_type=%s source_count=%s rejected=%d row_arities=%s",
            key,
            type(source).__name__,
            len(source) if isinstance(source, (list, tuple)) else "n/a",
            rejected,
            row_arities,
        )
    logger.info("HINTS_%s key=%s records=%d", update_kind, key, len(records))
    emit("hints", hints=records)
