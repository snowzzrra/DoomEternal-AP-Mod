"""Exact campaign-context registry and bounded DLC readiness evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO_ROOT / "data" / "runtime_contexts.json"
CAPABILITY_CROSS_CAMPAIGN = "cross_campaign_materialization_v1"
SUPPORT_RUNE_CAPABILITY = "support_runes_v1"
TAG_CAPABILITY = "tag_context_v1"
TAG_SPECIAL_CAPABILITY = "tag_special_v1"
SUPPORT_RUNE_IDS = frozenset({7770145, 7770146, 7770147})
TAG_SPECIAL_IDS = frozenset({7770009, 7770902})
CRUCIBLE_ID = 7770007
GATE_KEY_TO_MAP = {
    7770148: "e4m1_rig",
    7770149: "e4m3_mcity",
    7770150: "e1m2_war",
    7770151: "e1m3_cult",
    7770152: "e2m1_nest",
    7770153: "e2m2_base",
    7770154: "e2m3_core",
    7770155: "e3m1_slayer",
}


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
class DlcEvidence:
    status: str
    reason: str
    checked_paths: tuple[str, ...]

    @property
    def blocks_enabled(self) -> bool:
        return self.status == "missing"

    def report(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "checked_paths": list(self.checked_paths),
            "blocks_enabled": self.blocks_enabled,
        }


def _load_registry() -> tuple[RuntimeContext, ...]:
    document = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    contexts = []
    for raw in document["contexts"]:
        contexts.append(RuntimeContext(
            identity=raw["identity"],
            campaign=raw["campaign"],
            runtime_maps=tuple(raw["runtime_maps"]),
            map_keys=tuple(raw["map_keys"]),
            capabilities=frozenset(raw["capabilities"]),
        ))
    return tuple(contexts)


CONTEXTS = _load_registry()
CONTEXT_BY_IDENTITY = {context.identity: context for context in CONTEXTS}
CONTEXT_BY_MAP = {
    runtime_map: context
    for context in CONTEXTS
    for runtime_map in context.runtime_maps
}


def classify_runtime_context(runtime_map: str, *, base_maps: Iterable[str] = ()) -> RuntimeContext | None:
    """Classify exact technical map identities; never infer from map catalogs."""
    if not isinstance(runtime_map, str):
        return None
    context = CONTEXT_BY_MAP.get(runtime_map)
    if context is not None:
        return context
    return None


def evaluate_dlc_availability(base_dir: str | Path | None) -> DlcEvidence:
    """Use only filesystem proof; unknown never blocks DLC-enabled readiness."""
    if base_dir is None:
        return DlcEvidence("unknown", "Doom base path is unavailable", ())
    root = Path(base_dir).expanduser().resolve()
    base = root if root.name.lower() == "base" else root / "base"
    game_root = base.parent
    paths = (
        base / "game" / "dlc" / "e4m1_rig",
        base / "game" / "dlc2" / "e5m1_spear",
    )
    rendered = tuple(path.as_posix() for path in paths)
    authoritative = (
        base.is_dir()
        and (base / "game").is_dir()
        and (base / "classicwads").is_dir()
        and (game_root / "DOOMEternalx64vk.exe").is_file()
    )
    if not authoritative:
        return DlcEvidence(
            "unknown",
            "DOOM Eternal installation root is not proven",
            rendered,
        )
    missing = tuple(
        path for path in paths
        if not path.is_dir()
        or not any(resource.is_file() for resource in path.glob("*.resources"))
    )
    if missing:
        return DlcEvidence("missing", "required DLC runtime directory is absent", rendered)
    return DlcEvidence("present", "required TAG runtime resources are present", rendered)


def validate_slot_contract(slot_data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate MOD-facing 0.5-C identity and exact option contract."""
    if slot_data.get("slot_data_revision") != "0.5-C":
        raise ValueError("slot_data_revision must be 0.5-C")
    required = slot_data.get("required_capabilities")
    if not isinstance(required, list) or CAPABILITY_CROSS_CAMPAIGN not in required:
        raise ValueError("cross_campaign_materialization_v1 capability is required")
    if not isinstance(slot_data.get("use_dlc_content"), bool):
        raise ValueError("use_dlc_content must be boolean")
    if slot_data.get("dlc_logic_timing") not in {
        "late_game", "from_the_beginning", "Late Game", "From the Beginning",
    }:
        raise ValueError("dlc_logic_timing is invalid")
    if slot_data.get("special_weapon") not in {
        "progressive_special_weapon", "progressive_sentinel_hammer", "the_crucible",
        "Progressive Special Weapon", "Progressive Sentinel Hammer", "The Crucible",
    }:
        raise ValueError("special_weapon is invalid")
    return dict(slot_data)


def context_item_ids(context: RuntimeContext, received_item_ids: Iterable[int]) -> tuple[int, ...]:
    """Select only materializable owned IDs for active context."""
    selected = []
    for item_id in received_item_ids:
        if item_id in SUPPORT_RUNE_IDS and not context.supports(SUPPORT_RUNE_CAPABILITY):
            continue
        if item_id in TAG_SPECIAL_IDS and not context.supports(TAG_SPECIAL_CAPABILITY):
            continue
        if item_id == 7770901 and not context.supports("special_weapon_v1"):
            continue
        if item_id == CRUCIBLE_ID and not context.supports("crucible_v1"):
            continue
        if item_id in GATE_KEY_TO_MAP:
            target_map = GATE_KEY_TO_MAP[item_id]
            if target_map not in context.map_keys:
                continue
        selected.append(item_id)
    return tuple(selected)


def support_rune_commands(received_item_ids: Iterable[int], context: RuntimeContext) -> tuple[int, ...]:
    if not context.supports(SUPPORT_RUNE_CAPABILITY):
        return ()
    return tuple(
        item_id
        for item_id in sorted(set(received_item_ids))
        if item_id in SUPPORT_RUNE_IDS
    )


def dlc_contexts() -> tuple[RuntimeContext, ...]:
    return tuple(context for context in CONTEXTS if context.campaign != "Base")
