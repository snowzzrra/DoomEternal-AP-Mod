"""Command identity and publication evidence, independent of transport and AP."""

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re


EXECUTION_CLASS_HEADER = "AP_EXECUTION_CLASS_V1"
PLAYER_RUNTIME = "PLAYER_RUNTIME"
MAP_ENTITY_SAFE = "MAP_ENTITY_SAFE"
TRANSIENT_EFFECT = "TRANSIENT_EFFECT"
TRANSIENT_SCOPE_HEADER = "AP_TRANSIENT_SCOPE_V1"
VALID_EXECUTION_CLASSES = frozenset({PLAYER_RUNTIME, MAP_ENTITY_SAFE, TRANSIENT_EFFECT})
MAP_ENTITY_OPERATION_HEADER = "AP_MAP_ENTITY_OPERATION_V1"
CHECKED_VISUAL_HIDE = "CHECKED_VISUAL_HIDE"
FAST_TRAVEL_UNLOCK = "FAST_TRAVEL_UNLOCK"
VALID_MAP_ENTITY_OPERATIONS = frozenset({CHECKED_VISUAL_HIDE, FAST_TRAVEL_UNLOCK})
MATERIALIZATION_LEASE_HEADER = "AP_MATERIALIZATION_LEASE_V1"
MATERIALIZATION_LEASE_MARKER = "active_materialization_lease"
SPOOL_ID_MAX_BYTES = 128
SPOOL_ID_HASH_HEX_LENGTH = 20
_WINDOWS_ILLEGAL_SPOOL_ID_CHARS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_SPOOL_ID_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
})


class CommandEvidence(str, Enum):
    PLANNED = "planned"
    SPOOL_PRESENT = "spool_file_observed"
    DURABLY_PUBLISHED = "durably_published"
    CLAIMED = "claimed"
    CONSUMED_UNVERIFIED = "consumed_unverified"
    GAMEPLAY_VERIFIED = "gameplay_verified"


@dataclass(frozen=True)
class PublicationResult:
    accepted: bool
    evidence: CommandEvidence
    command_id: str | None = None


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


def validate_spool_id(command_id):
    """Reject command IDs that cannot be one filesystem component."""
    if not isinstance(command_id, str) or not command_id:
        raise ValueError("spool command ID must be a non-empty string")
    if len(command_id.encode("utf-8")) > SPOOL_ID_MAX_BYTES:
        raise ValueError(
            f"spool command ID exceeds {SPOOL_ID_MAX_BYTES} UTF-8 bytes"
        )
    if any(character in command_id for character in "/\\"):
        raise ValueError("spool command ID contains a path separator")
    if any(
        ord(character) < 32 or ord(character) == 127
        for character in command_id
    ):
        raise ValueError("spool command ID contains a control character")
    if any(character in _WINDOWS_ILLEGAL_SPOOL_ID_CHARS for character in command_id):
        raise ValueError("spool command ID contains a Windows-illegal character")
    if command_id.endswith((".", " ")):
        raise ValueError("spool command ID has a Windows-illegal trailing character")
    if command_id in {".", ".."}:
        raise ValueError("spool command ID is a traversal component")
    windows_stem = command_id.split(".", 1)[0].upper()
    if windows_stem in _WINDOWS_RESERVED_SPOOL_ID_NAMES:
        raise ValueError("spool command ID is a Windows-reserved device name")
    return command_id


def stable_spool_id(prefix, *logical_components):
    """Return bounded ID for logical coalescing identity."""
    validate_spool_id(prefix)
    canonical = json.dumps(
        logical_components,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return validate_spool_id(
        f"{prefix}-{digest[:SPOOL_ID_HASH_HEX_LENGTH]}"
    )


def queue_session_namespace(state_key):
    """Opaque durable queue namespace derived from room identity."""
    if not isinstance(state_key, str) or not state_key:
        return None
    return hashlib.sha256(state_key.encode("utf-8")).hexdigest()[:16]
