"""Runtime lifecycle values and map normalization, without registry discovery."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping


def canonical_map_name(name: str | None) -> str | None:
    if not name:
        return name
    normalized = str(name).strip().replace("\\", "/").rstrip("/")
    return "game/hub/hub" if normalized in {"game/hub/hub", "game/sp/hub/hub"} else normalized


@dataclass(frozen=True)
class RuntimeContext:
    identity: str
    campaign: str
    runtime_maps: tuple[str, ...]
    map_keys: tuple[str, ...]
    capabilities: frozenset[str]

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True)
class ContextResolution:
    context: RuntimeContext | None
    source: str
    rejection: tuple[str, str, str] | None = None


@dataclass(frozen=True)
class RuntimeContextSnapshot:
    identity: str = "unknown"
    campaign: str = "Unknown"
    capabilities: frozenset[str] = frozenset()
    pending_transition: tuple[str, str] | None = None


@dataclass(frozen=True)
class MapEpochSnapshot:
    published_materialization_lease: str | None = None
    native_gameplay_epoch: int | None = None
    accepted_marker_mtime: int | None = None
    accepted_marker_evidence_epoch: int | None = None


@dataclass(frozen=True)
class MapIdentitySnapshot:
    cached_marker: Mapping[str, Any] | None = None
    pending_marker: Mapping[str, Any] | None = None
    current_map: str | None = None

    def __post_init__(self):
        for field in ("cached_marker", "pending_marker"):
            marker = getattr(self, field)
            if marker is not None:
                object.__setattr__(self, field, MappingProxyType(dict(marker)))


@dataclass(frozen=True)
class NativeLoadDecision:
    action: Literal["initialize", "transition", "bind", "suspend", "reload"]
    evidence_epoch: int
    runtime_map: str
    map_key: str | None


@dataclass(frozen=True)
class DlcEvidence:
    status: str
    reason: str
    checked_paths: tuple[str, ...]

    @property
    def blocks_enabled(self) -> bool:
        return self.status == "missing"

    def report(self) -> dict[str, Any]:
        return {
            "status": self.status, "reason": self.reason,
            "checked_paths": list(self.checked_paths), "blocks_enabled": self.blocks_enabled,
        }
