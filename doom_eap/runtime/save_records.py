"""Structured unlockable decoding from supplied, already-decrypted save bytes."""

MASTERY_MANAGER = b"UnlockableManager_0_1_2"
MASTERY_MANAGER_TYPE = b"idUnlockableManager_2"


def _read_serialized_uint(payload, offset):
    """Read one width-prefixed little-endian unsigned value."""
    if offset >= len(payload):
        raise ValueError("metric value width is missing")
    width = payload[offset]
    if width < 1 or width > 8 or offset + 1 + width > len(payload):
        raise ValueError(f"invalid metric value width {width}")
    return (
        int.from_bytes(payload[offset + 1:offset + 1 + width], "little"),
        offset + 1 + width,
    )


def _read_structured_bool(payload, offset, field):
    if not payload.startswith(field, offset):
        raise ValueError(f"unlockable record missing {field.decode('ascii').strip()}")
    value_offset = offset + len(field)
    try:
        value = {0x0B: False, 0x0C: True}[payload[value_offset]]
    except (IndexError, KeyError) as error:
        raise ValueError(f"unlockable record has invalid {field.decode('ascii').strip()}") from error
    return value, value_offset + 1


def _mastery_manager_type_offset(payload):
    manager_offset = payload.find(MASTERY_MANAGER)
    if manager_offset < 0 or payload.find(MASTERY_MANAGER, manager_offset + 1) >= 0:
        raise ValueError("native unlockable manager is missing or ambiguous")
    manager_type_offset = payload.find(MASTERY_MANAGER_TYPE, manager_offset)
    if (
        manager_type_offset < 0
        or payload.find(MASTERY_MANAGER_TYPE, manager_type_offset + 1) >= 0
    ):
        raise ValueError("native unlockable manager type is missing or ambiguous")
    return manager_type_offset


def read_unlockable_record(payload, entry):
    """Decode one exact native unlockable record; global stats are ignored."""
    signal = entry["signal"]
    unlockable = signal["unlockable"].encode("ascii")
    manager_type_offset = _mastery_manager_type_offset(payload)
    record_prefix = (
        bytes([len(unlockable) * 2]) + unlockable
        + b"\x0e\x0c$numUnlockableRules"
    )
    record_offset = payload.find(record_prefix, manager_type_offset)
    if (
        record_offset < manager_type_offset
        or payload.find(record_prefix, record_offset + 1) >= 0
    ):
        if record_offset < 0:
            return None
        raise ValueError(f"{signal['unlockable']}: native record is ambiguous")

    cursor = record_offset + len(record_prefix)
    rule_count, cursor = _read_serialized_uint(payload, cursor)
    satisfied, cursor = _read_structured_bool(payload, cursor, b" rule_0_satisfied")
    if not payload.startswith(b" rule_0_statCount", cursor):
        raise ValueError(f"{signal['unlockable']}: missing rule_0_statCount")
    stat_count, cursor = _read_serialized_uint(
        payload, cursor + len(b" rule_0_statCount")
    )
    if not payload.startswith(b"&rule_0_statDuration", cursor):
        raise ValueError(f"{signal['unlockable']}: missing rule_0_statDuration")
    stat_duration, cursor = _read_serialized_uint(
        payload, cursor + len(b"&rule_0_statDuration")
    )
    stat_prefix = b"\x1erule_0_statname\x0a"
    if not payload.startswith(stat_prefix, cursor):
        raise ValueError(f"{signal['unlockable']}: missing rule_0_statname")
    cursor += len(stat_prefix)
    stat_len = payload[cursor] // 2
    cursor += 1
    stat_bytes = payload[cursor:cursor + stat_len]
    cursor += stat_len
    unlocked, cursor = _read_structured_bool(
        payload, cursor, b"(unlockableIsUnlocked"
    )
    return {
        "numUnlockableRules": rule_count,
        "rule_0_statname": stat_bytes.decode("ascii", errors="ignore"),
        "rule_0_statCount": stat_count,
        "rule_0_statDuration": stat_duration,
        "rule_0_satisfied": satisfied,
        "unlockableIsUnlocked": unlocked,
    }
