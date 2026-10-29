"""Exact campaign-context registry and bounded DLC readiness evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from doom_eap.contracts.runtime_context import DlcEvidence, RuntimeContext
from doom_eap.runtime.lifecycle import RuntimeLifecycle
from doom_eap.content import slot_data_contract
from doom_eap.contracts.core_distribution import version_key
from doom_eap.contracts.materialization import (
    SUPPORT_RUNE_CAPABILITY, TAG_SPECIAL_CAPABILITY, SUPPORT_RUNE_IDS,
    TAG_SPECIAL_IDS, CRUCIBLE_ID, GATE_KEY_TO_MAP, context_item_ids, support_rune_commands,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO_ROOT / "data" / "runtime_contexts.json"
_SLOT_IDENTITY = json.loads(
    (REPO_ROOT / "data" / "content_identity.json").read_text(encoding="utf-8")
)
TAG_CAPABILITY = "tag_context_v1"
GOAL_CAPABILITIES = frozenset({"goal_events_v1", "goal_endpoint_events_v1"})

SUPPORTED_SLOT_CAPABILITIES = frozenset({
    "room_mod_v2", "slot_data_v6", "slot_data_v7", "unified_campaign_v1",
    "fortress_economy_v1", "progressive_blood_punch_v1", "enemy_recipe_seed_v1",
    "dlc_missions_v1", "goal_events_v1", "goal_endpoint_events_v1", "placement_scouts_v1",
    "randomize_dash_v1", "starting_inventory_v1", "starting_weapon_v1",
    "special_weapon_progression_v1", "ammo_refill_v1", "physical_options_v1",
    "room_options_v1", "cross_campaign_materialization_v1", "deathlink_mode_v1",
})


class SlotAdmissionError(ValueError):
    failure_domain = "room_package"

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def slot_schema_version() -> int:
    return _SLOT_IDENTITY["slot_data_schema_version"]


def load_slot_catalog() -> dict:
    return slot_data_contract.load_slot_catalog(path=REPO_ROOT / "doom_eap/content/slot_catalog.json")


def admit_slot_data(slot_data, *, placements=None):
    """Select the packaged schema before any room preparation or state changes."""
    if not isinstance(slot_data, Mapping):
        raise SlotAdmissionError("slot_structure", "Room data must be an object.")
    expected = slot_schema_version()
    identity = slot_data.get("identity")
    if not isinstance(identity, dict) or type(identity.get("schema")) is not int or identity["schema"] != expected:
        version = _SLOT_IDENTITY["release_version"]
        apworld = _SLOT_IDENTITY["apworld_revision"]
        raise SlotAdmissionError("slot_schema", f"This client requires a DoomEAP {version} room "
                                 f"(slot_data schema {expected}). Generate a compatible room with APWorld {apworld}.")
    try:
        resolved = slot_data_contract.validate_slot_data(
            slot_data, load_slot_catalog(), placements=placements,
            supported_capabilities=SUPPORTED_SLOT_CAPABILITIES,
        )
    except slot_data_contract.ContractError as error:
        codes = {slot_data_contract.StructureError: "slot_structure", slot_data_contract.ReferenceError: "slot_reference",
                 slot_data_contract.DigestError: "slot_digest", slot_data_contract.ExpansionError: "slot_expansion",
                 slot_data_contract.PayloadSizeError: "slot_size"}
        raise SlotAdmissionError(codes.get(type(error), "slot_contract"), str(error)) from error
    _validate_resolved_compatibility(resolved.compatibility)
    return resolved


def _validate_resolved_compatibility(compatibility):
    identity = _SLOT_IDENTITY
    expected = (identity["bridge_protocol_version"], identity["manifest_schema_version"],
                identity["session_mod_contract_revision"])
    actual = (compatibility.bridge_protocol, compatibility.manifest_schema_version, compatibility.mod_contract_revision)
    if actual != expected:
        raise SlotAdmissionError("slot_compatibility", "The room requires a different client contract.")
    try:
        supported = version_key(identity["release_version"]) >= version_key(compatibility.minimum_client_version)
        version_key(compatibility.apworld_version)
    except ValueError as error:
        raise SlotAdmissionError("slot_compatibility", "The room has an invalid compatibility version.") from error
    if not supported:
        raise SlotAdmissionError("slot_compatibility", "The room requires a newer client.")


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
    """Validate the generated room identity and exact option contract."""
    admit_slot_data(slot_data)
    return dict(slot_data)


def dlc_contexts() -> tuple[RuntimeContext, ...]:
    return tuple(context for context in CONTEXTS if context.campaign != "Base")
