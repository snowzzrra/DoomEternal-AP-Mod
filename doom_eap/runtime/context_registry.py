"""Exact campaign-context registry and bounded DLC readiness evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from doom_eap.contracts.runtime_context import DlcEvidence, RuntimeContext
from doom_eap.runtime.lifecycle import RuntimeLifecycle
from doom_eap.contracts.materialization import (
    SUPPORT_RUNE_CAPABILITY, TAG_SPECIAL_CAPABILITY, SUPPORT_RUNE_IDS,
    TAG_SPECIAL_IDS, CRUCIBLE_ID, GATE_KEY_TO_MAP, context_item_ids, support_rune_commands,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO_ROOT / "data" / "runtime_contexts.json"
SLOT_DATA_REVISION = str(json.loads(
    (REPO_ROOT / "data" / "content_identity.json").read_text(encoding="utf-8")
)["slot_data_revision"])
CAPABILITY_CROSS_CAMPAIGN = "cross_campaign_materialization_v1"
TAG_CAPABILITY = "tag_context_v1"
GOAL_CAPABILITIES = frozenset({"goal_events_v1", "goal_endpoint_events_v1"})
GOAL_VALUES = frozenset({
    "Acquire the Unmaykr",
    "Kill the Icon of Sin",
    "Kill the Dark Lord",
    "Complete the Full Saga",
})
VICTORY_REQUIREMENT_VALUES = frozenset({
    "Complete All Enabled Missions",
    "Complete All Slayer Gates",
    "Complete All Escalation Encounters",
    "Complete All Secret Encounters",
    "Complete All Mission Challenges",
    "Complete All Weapon Mastery Challenges",
    "Acquire the Unmaykr",
})


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


def resolve_context_evidence(marker_map, evidence_map, *, materialization_suspended, base_maps=()):
    """Supply authored context data to the lifecycle selection policy."""
    return RuntimeLifecycle.resolve_context(
        marker_map, evidence_map, materialization_suspended=materialization_suspended,
        contexts_by_map=CONTEXT_BY_MAP,
    )


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
    """Validate MOD-facing 0.5-D identity and exact option contract."""
    if slot_data.get("slot_data_revision") != SLOT_DATA_REVISION:
        raise ValueError(f"slot_data_revision must be {SLOT_DATA_REVISION}")
    required = slot_data.get("required_capabilities")
    if (
        not isinstance(required, list)
        or any(not isinstance(value, str) for value in required)
        or CAPABILITY_CROSS_CAMPAIGN not in required
    ):
        raise ValueError("cross_campaign_materialization_v1 capability is required")
    goal_fields = {
        "goal",
        "goal_endpoint_event",
        "goal_endpoint_available",
        "additional_victory_requirements",
    }
    goal_contract_present = bool(goal_fields & slot_data.keys()) or bool(
        GOAL_CAPABILITIES & set(required)
    )
    if not isinstance(slot_data.get("use_dlc_content"), bool):
        raise ValueError("use_dlc_content must be boolean")
    if not isinstance(slot_data.get("include_dlc_missions"), bool):
        raise ValueError("include_dlc_missions must be boolean")
    if slot_data["include_dlc_missions"] and not slot_data["use_dlc_content"]:
        raise ValueError("include_dlc_missions requires use_dlc_content")
    if slot_data.get("dlc_logic_timing") not in {
        "late_game", "from_the_beginning", "Late Game", "From the Beginning",
    }:
        raise ValueError("dlc_logic_timing is invalid")
    if slot_data.get("special_weapon") not in {
        "progressive_special_weapon", "progressive_sentinel_hammer", "the_crucible",
        "Progressive Special Weapon", "Progressive Sentinel Hammer", "The Crucible",
    }:
        raise ValueError("special_weapon is invalid")
    if goal_contract_present:
        if not GOAL_CAPABILITIES <= set(required):
            raise ValueError("goal event capabilities are required")
        goal = slot_data.get("goal")
        if goal not in GOAL_VALUES:
            raise ValueError("goal is invalid")
        if slot_data.get("goal_endpoint_event") != f"Internal Goal Endpoint: {goal}":
            raise ValueError("goal_endpoint_event is invalid")
        if slot_data.get("goal_endpoint_available") is not True:
            raise ValueError("goal_endpoint_available must be true")
        requirements = slot_data.get("additional_victory_requirements")
        if (
            not isinstance(requirements, list)
            or any(value not in VICTORY_REQUIREMENT_VALUES for value in requirements)
            or len(requirements) != len(set(requirements))
        ):
            raise ValueError("additional_victory_requirements is invalid")
    return dict(slot_data)


def dlc_contexts() -> tuple[RuntimeContext, ...]:
    return tuple(context for context in CONTEXTS if context.campaign != "Base")
