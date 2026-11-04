"""Stable command identity and runtime effect categories."""

import hashlib
import json
import re

MAP_ENTITY_SAFE = "MAP_ENTITY_SAFE"
TRANSIENT_EFFECT = "TRANSIENT_EFFECT"
CHECKED_VISUAL_HIDE = "CHECKED_VISUAL_HIDE"
FAST_TRAVEL_UNLOCK = "FAST_TRAVEL_UNLOCK"


def valid_materialization_epoch(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9]+:[0-9]+", value) is not None


def build_materialization_epoch(native_epoch, marker_mtime_ns):
    if (
        isinstance(native_epoch, bool)
        or not isinstance(native_epoch, int)
        or isinstance(marker_mtime_ns, bool)
        or not isinstance(marker_mtime_ns, int)
        or native_epoch < 0
        or marker_mtime_ns < 0
    ):
        return None
    return f"{native_epoch}:{marker_mtime_ns}"


def stable_command_key(prefix, *logical_components):
    """Return bounded ID for logical coalescing identity."""
    if not isinstance(prefix, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", prefix) is None:
        raise ValueError("Invalid command key prefix")
    canonical = json.dumps(
        logical_components,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:20]}"


def queue_session_namespace(state_key):
    """Opaque durable queue namespace derived from room identity."""
    if not isinstance(state_key, str) or not state_key:
        return None
    return hashlib.sha256(state_key.encode("utf-8")).hexdigest()[:16]
