"""Headless seed compiler and install workflow. UI/CLI are adapters only."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from doom_eap.content.physical_options import (
    DEATH_LINK_MODES,
    PHYSICAL_OPTIONS,
    PHYSICAL_OPTION_KEYS,
    physical_location_ids,
    project_room_config,
    project_map_config,
)
from tools.decls.devinv_builder import (
    build_devinv_loadout,
    build_tag_devinv_overrides,
    output_path_for_map,
)

MODULE_DIR = Path(__file__).resolve().parent
ROOT = MODULE_DIR if (MODULE_DIR / "data").is_dir() else Path(__file__).resolve().parents[2]
DASH_LOCATION_ID = 7770083
DASH_ENTITY = "AP_CHECK_CAPITOL_PROGRESS_DASH_1"
_CONTRACT_IDENTITY = json.loads(
    (ROOT / "data" / "content_identity.json").read_text(encoding="utf-8")
)
MANIFEST_SCHEMA_VERSION = int(_CONTRACT_IDENTITY["manifest_schema_version"])
SLOT_DATA_SCHEMA_VERSION = int(_CONTRACT_IDENTITY["slot_data_schema_version"])
SLOT_DATA_REVISION = str(_CONTRACT_IDENTITY["slot_data_revision"])
REVEAL_AP_LOCATIONS_OPTION_KEY = "reveal_ap_locations_on_automap"
SUPPORTED_CAPABILITIES = frozenset({
    "room_mod_v2",
    "slot_data_v4",
    "dlc_missions_v1",
    "goal_events_v1",
    "goal_endpoint_events_v1",
    "placement_scouts_v1",
    "randomize_dash_v1",
    "starting_inventory_v1",
    "starting_weapon_v1",
    "special_weapon_progression_v1",
    "ammo_refill_v1",
    "physical_options_v1",
    "room_options_v1",
    "cross_campaign_materialization_v1",
})
ROOM_SLOT_DEFAULTS: dict[str, Any] = {
    "use_dlc_content": True,
    "include_dlc_missions": True,
    "dlc_logic_timing": "Late Game",
    "goal": "Acquire the Unmaykr",
    "goal_endpoint_event": "Internal Goal Endpoint: Acquire the Unmaykr",
    "additional_victory_requirements": [
        "Complete All Enabled Missions",
        "Complete All Escalation Encounters",
        "Complete All Slayer Gates",
    ],
    "special_weapon": "Progressive Special Weapon",
    "enhanced_melee_damage": False,
    "randomize_chainsaw": False,
    "randomize_dash": False,
    "randomize_first_battery": False,
    "include_weapon_mastery_challenges": True,
    REVEAL_AP_LOCATIONS_OPTION_KEY: False,
    "starting_weapon": "Combat Shotgun",
    "praetor_suit_upgrades_in_pool": 6,
    "trap_percentage": 10,
    "enabled_traps": [
        "Ammo Drain Trap",
        "Arachnotron Trap",
        "Archvile Trap",
        "BFG Drain Trap",
        "Baron Trap",
        "Carcass Trap",
        "Cueball Trap",
        "Dread Knight Trap",
        "Fuel Drain Trap",
        "Hell Knight Trap",
        "Imp Trap",
        "Marauder Trap",
        "Revenant Trap",
        "Tyrant Trap",
    ],
    "starting_inventory": {},
}
LOGGER = logging.getLogger(__name__)
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
SPECIAL_WEAPON_VALUES = frozenset({
    "Progressive Special Weapon",
    "Progressive Sentinel Hammer",
    "The Crucible",
})
CLIENT_CONFIG_FIELDS = frozenset({
    "steam_remote_dir",
    "steam_id3",
    "doom_base_dir",
    "save_games_dir",
    "server_address",
    "seed_manifest_hash",
})


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def release_identity() -> dict[str, Any]:
    return json.loads((ROOT / "data" / "content_identity.json").read_text(encoding="utf-8"))


def _normalize_slot_data(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize current APWorld slot fields while retaining legacy defaults."""
    slot_data = {
        key: list(value) if isinstance(value, list) else dict(value) if isinstance(value, dict) else value
        for key, value in ROOM_SLOT_DEFAULTS.items()
    }
    slot_data.update(raw)
    boolean_keys = (
        "use_dlc_content",
        "enhanced_melee_damage",
        "randomize_chainsaw",
        "randomize_dash",
        "randomize_first_battery",
        "include_weapon_mastery_challenges",
        REVEAL_AP_LOCATIONS_OPTION_KEY,
    )
    for key in boolean_keys:
        if not isinstance(slot_data[key], bool):
            raise ValueError(f"Connected.slot_data.{key} must be boolean")
    if slot_data["dlc_logic_timing"] not in {"Late Game", "From the Beginning"}:
        raise ValueError("Connected.slot_data.dlc_logic_timing is invalid")
    if slot_data["goal"] not in GOAL_VALUES:
        raise ValueError("Connected.slot_data.goal is invalid")
    requirements = slot_data["additional_victory_requirements"]
    if not isinstance(requirements, list) or any(
        not isinstance(value, str) or value not in VICTORY_REQUIREMENT_VALUES
        for value in requirements
    ) or len(requirements) != len(set(requirements)):
        raise ValueError("Connected.slot_data.additional_victory_requirements is invalid")
    if slot_data["special_weapon"] not in SPECIAL_WEAPON_VALUES:
        raise ValueError("Connected.slot_data.special_weapon is invalid")
    for key, minimum, maximum in (
        ("praetor_suit_upgrades_in_pool", 0, 21),
        ("trap_percentage", 0, 100),
    ):
        value = slot_data[key]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"Connected.slot_data.{key} is invalid")
    traps = slot_data["enabled_traps"]
    if not isinstance(traps, list) or any(not isinstance(value, str) or not value for value in traps):
        raise ValueError("Connected.slot_data.enabled_traps is invalid")
    if len(traps) != len(set(traps)):
        raise ValueError("Connected.slot_data.enabled_traps contains duplicates")
    if not isinstance(slot_data["goal_endpoint_event"], str) or not slot_data["goal_endpoint_event"]:
        raise ValueError("Connected.slot_data.goal_endpoint_event is invalid")
    if not isinstance(slot_data["starting_weapon"], str) or not slot_data["starting_weapon"]:
        raise ValueError("Connected.slot_data.starting_weapon is invalid")
    if not isinstance(slot_data["starting_inventory"], dict):
        raise ValueError("Connected.slot_data.starting_inventory is invalid")
    return slot_data


@dataclass(frozen=True)
class PlacementRecord:
    """Resolved, immutable placement input for deterministic compilation."""

    location_id: int
    location_name: str
    item_id: int
    item_name: str
    recipient_slot: int
    recipient_name: str
    classification: int
    trap: bool
    local: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PlacementRecord:
        if not isinstance(value, Mapping):
            raise ValueError("placement must be an object")
        required = {
            "location_id", "location_name", "item_id", "item_name",
            "recipient_slot", "recipient_name", "classification", "trap", "local",
        }
        if set(value) != required:
            raise ValueError(
                "placement fields must be exactly " + ", ".join(sorted(required))
            )
        integer_fields = ("location_id", "item_id", "recipient_slot", "classification")
        for field in integer_fields:
            field_value = value[field]
            if not isinstance(field_value, int) or isinstance(field_value, bool):
                raise ValueError(f"placement {field} must be an integer")
        if value["location_id"] <= 0 or value["recipient_slot"] < 1:
            raise ValueError("placement location_id and recipient_slot must be positive")
        if value["classification"] < 0:
            raise ValueError("placement classification must be non-negative")
        for field in ("location_name", "item_name", "recipient_name"):
            if not isinstance(value[field], str) or not value[field].strip():
                raise ValueError(f"placement {field} must be non-empty text")
        if not isinstance(value["trap"], bool) or not isinstance(value["local"], bool):
            raise ValueError("placement trap and local must be boolean")
        expected_trap = bool(value["classification"] & 0b00100)
        if value["trap"] != expected_trap:
            raise ValueError("placement trap does not match classification")
        return cls(**dict(value))

    def document(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_placements(
    placements: object, active_location_ids: tuple[int, ...], slot: int
) -> tuple[PlacementRecord, ...]:
    if not isinstance(placements, (list, tuple)):
        raise ValueError("placements must be a list")
    records = tuple(
        value if isinstance(value, PlacementRecord) else PlacementRecord.from_mapping(value)
        for value in placements
    )
    ids = [record.location_id for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError("placements contain duplicate location IDs")
    expected = set(active_location_ids)
    actual = set(ids)
    if actual != expected:
        raise ValueError(
            f"placement set is incomplete or unknown: missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )
    for record in records:
        if record.local != (record.recipient_slot == slot):
            raise ValueError(
                f"placement local flag disagrees with recipient slot at {record.location_id}"
            )
    return tuple(sorted(records, key=lambda record: record.location_id))


@dataclass(frozen=True)
class RoomSnapshot:
    seed_name: str
    team: int
    slot: int
    slot_data: dict[str, Any]
    missing_locations: tuple[int, ...]
    checked_locations: tuple[int, ...]
    placements: tuple[PlacementRecord, ...]

    @classmethod
    def from_packets(cls, room_info: dict[str, Any], connected: dict[str, Any]) -> RoomSnapshot:
        seed_name = room_info.get("seed_name")
        team = connected.get("team")
        slot = connected.get("slot")
        raw_slot_data = connected.get("slot_data")
        if not isinstance(seed_name, str) or not seed_name:
            raise ValueError("RoomInfo.seed_name is required")
        if not isinstance(team, int) or isinstance(team, bool) or team < 0:
            raise ValueError("Connected.team must be a non-negative integer")
        if not isinstance(slot, int) or isinstance(slot, bool) or slot < 1:
            raise ValueError("Connected.slot must be a positive integer")
        if not isinstance(raw_slot_data, dict):
            raise ValueError("Connected.slot_data must be an object")
        slot_data = _normalize_slot_data(raw_slot_data)
        for key in PHYSICAL_OPTION_KEYS:
            if not isinstance(slot_data.get(key), bool):
                raise ValueError(f"Connected.slot_data.{key} must be boolean")
        for key in ("death_link",):
            if key in slot_data and not isinstance(slot_data[key], bool):
                raise ValueError(f"Connected.slot_data.{key} must be boolean")
        if "death_link_mode" in slot_data and (
            not isinstance(slot_data["death_link_mode"], str)
            or slot_data["death_link_mode"] not in DEATH_LINK_MODES
        ):
            raise ValueError(
                "Connected.slot_data.death_link_mode must be one of "
                + ", ".join(sorted(DEATH_LINK_MODES))
            )

        def locations(field: str) -> tuple[int, ...]:
            values = connected.get(field)
            if not isinstance(values, (list, tuple, set)):
                raise ValueError(f"Connected.{field} must be a location list")
            if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
                raise ValueError(f"Connected.{field} contains invalid location ID")
            return tuple(sorted(set(values)))

        missing = locations("missing_locations")
        checked = locations("checked_locations")
        if set(missing) & set(checked):
            raise ValueError("Connected missing_locations and checked_locations overlap")
        raw_placements = connected.get("placements")
        active = tuple(sorted(set(missing) | set(checked)))
        placements = _normalize_placements(raw_placements, active, slot)

        return cls(
            seed_name=seed_name,
            team=team,
            slot=slot,
            slot_data=dict(slot_data),
            missing_locations=missing,
            checked_locations=checked,
            placements=placements,
        )

    @classmethod
    def from_event(cls, payload: dict[str, Any]) -> RoomSnapshot:
        """Parse launcher event payload; intended for bridge/supervisor IPC."""
        return cls.from_packets(
            {"seed_name": payload.get("seed_name")},
            {
                "team": payload.get("team"),
                "slot": payload.get("slot"),
                "slot_data": payload.get("slot_data"),
                "missing_locations": payload.get("missing_locations"),
                "checked_locations": payload.get("checked_locations"),
                "placements": payload.get("placements"),
            },
        )

    @property
    def active_location_ids(self) -> tuple[int, ...]:
        return tuple(sorted(set(self.missing_locations) | set(self.checked_locations)))


@dataclass(frozen=True)
class SeedManifest:
    schema: int
    game: str
    seed_name: str
    team: int
    slot: int
    bridge_protocol: int
    apworld_revision: str
    content_revision: str
    compiler_revision: int
    manifest_schema_version: int
    mod_contract_revision: int
    required_capabilities: tuple[str, ...]
    options: dict[str, Any]
    active_location_ids: tuple[int, ...]
    placements: tuple[PlacementRecord, ...]
    static_content_digest: str
    manifest_hash: str
    static_precompile: bool = False

    @classmethod
    def create(
        cls,
        *,
        seed_name: str,
        team: int,
        slot: int,
        options: dict[str, Any],
        active_location_ids: list[int],
        placements: object = (),
        static_precompile: bool = False,
        static_content_digest: str = "",
    ) -> SeedManifest:
        identity = release_identity()
        normalized_options = {
            key: options[key]
            for key in sorted(options)
        }
        for key in PHYSICAL_OPTION_KEYS:
            normalized_options.setdefault(key, False)
            if not isinstance(normalized_options[key], bool):
                raise ValueError(f"manifest option {key} must be boolean")
        normalized_options.setdefault(REVEAL_AP_LOCATIONS_OPTION_KEY, False)
        if not isinstance(normalized_options[REVEAL_AP_LOCATIONS_OPTION_KEY], bool):
            raise ValueError(
                f"manifest option {REVEAL_AP_LOCATIONS_OPTION_KEY} must be boolean"
            )
        project_room_config(normalized_options)
        if "starting_inventory" in normalized_options:
            inventory = normalized_options["starting_inventory"]
            if not isinstance(inventory, dict):
                raise ValueError("manifest starting_inventory must be an object")
            for name, quantity in inventory.items():
                if not isinstance(name, str) or not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
                    raise ValueError("manifest starting_inventory has invalid name or quantity")
        if "starting_weapon" in normalized_options and normalized_options["starting_weapon"] is not None and not isinstance(normalized_options["starting_weapon"], str):
            raise ValueError("manifest starting_weapon must be string or null")
        active_ids = tuple(sorted(set(active_location_ids)))
        if not isinstance(static_precompile, bool):
            raise ValueError("static_precompile must be boolean")
        if static_content_digest and not re.fullmatch(r"[0-9a-f]{64}", static_content_digest):
            raise ValueError("static_content_digest must be an empty string or SHA-256")
        normalized_placements = (
            ()
            if static_precompile
            else _normalize_placements(placements, active_ids, int(slot))
        )
        required_capabilities = {
            "room_mod_v2",
            "slot_data_v4",
            "dlc_missions_v1",
            "goal_events_v1",
            "goal_endpoint_events_v1",
            "placement_scouts_v1",
            "special_weapon_progression_v1",
            "ammo_refill_v1",
        }
        required_capabilities.add("physical_options_v1")
        required_capabilities.add("room_options_v1")
        if normalized_options.get("randomize_dash"):
            required_capabilities.add("randomize_dash_v1")
        if normalized_options.get("starting_inventory"):
            required_capabilities.add("starting_inventory_v1")
        if normalized_options.get("starting_weapon"):
            required_capabilities.add("starting_weapon_v1")
        payload = {
            "schema": MANIFEST_SCHEMA_VERSION,
            "game": identity["game"],
            "seed_name": seed_name,
            "team": int(team),
            "slot": int(slot),
            "bridge_protocol": identity["bridge_protocol_version"],
            "apworld_revision": identity["apworld_revision"],
            "content_revision": identity["content_revision"],
            "compiler_revision": identity["compiler_revision"],
            "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
            "mod_contract_revision": identity["session_mod_contract_revision"],
            "required_capabilities": sorted(required_capabilities),
            "options": normalized_options,
            "active_location_ids": list(active_ids),
            "placements": [record.document() for record in normalized_placements],
            "static_content_digest": static_content_digest,
            "static_precompile": static_precompile,
        }
        return cls(
            **{**payload, "placements": normalized_placements},
            manifest_hash=hashlib.sha256(_canonical(payload)).hexdigest(),
        )

    def document(self) -> dict[str, Any]:
        return asdict(self)

    def require_complete_placements(self) -> tuple[PlacementRecord, ...]:
        """Return placement records required by room compilation."""
        if self.static_precompile:
            return ()
        if not self.placements:
            raise ValueError(
                "real-room compilation requires complete placement mapping"
            )
        return _normalize_placements(
            self.placements, self.active_location_ids, self.slot
        )

    @classmethod
    def from_room(
        cls,
        snapshot: RoomSnapshot,
        known_location_ids: set[int],
        *,
        static_content_digest: str = "",
    ) -> SeedManifest:
        identity = release_identity()
        slot_data = snapshot.slot_data
        if "bridge_protocol" in slot_data and slot_data["bridge_protocol"] != identity["bridge_protocol_version"]:
            raise ValueError(f"session bridge_protocol is incompatible: {slot_data['bridge_protocol']!r} != {identity['bridge_protocol_version']!r}")
        schema = slot_data.get("manifest_schema_version", 1)
        supported_contract = identity["session_mod_contract_revision"]
        contract = slot_data.get("mod_contract_revision", supported_contract)
        if not isinstance(schema, int) or schema < 1 or schema > MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"session manifest schema is unsupported: {schema!r}")
        if contract != supported_contract:
            raise ValueError(f"session mod contract is unsupported: {contract!r}")
        slot_data_revision = slot_data.get("slot_data_revision")
        if slot_data_revision is not None and slot_data_revision != SLOT_DATA_REVISION:
            raise ValueError(f"session slot data revision is unsupported: {slot_data_revision!r}")
        required = slot_data.get("required_capabilities")
        if required is None:
            required_capabilities = {
                "room_mod_v2",
                "placement_scouts_v1",
                "physical_options_v1",
                "room_options_v1",
            }
        elif isinstance(required, list) and all(isinstance(value, str) for value in required):
            required_capabilities = set(required)
        else:
            raise ValueError("session required_capabilities must be a list of strings")
        unsupported = sorted(required_capabilities - SUPPORTED_CAPABILITIES)
        if unsupported:
            raise ValueError(f"session requires unsupported capabilities: {unsupported}")
        if "compiler_revision" in slot_data and slot_data["compiler_revision"] != identity["compiler_revision"]:
            # Implementation metadata only. Compatibility is decided above.
            import logging
            logging.getLogger(__name__).info("session compiler_revision differs: room=%r local=%r", slot_data["compiler_revision"], identity["compiler_revision"])
        unknown = sorted(set(snapshot.active_location_ids) - known_location_ids)
        if unknown:
            raise ValueError(f"session contains unknown DOOM Eternal location IDs: {unknown}")
        starting_inventory = dict(slot_data.get("starting_inventory", {}))
        return cls.create(
            seed_name=snapshot.seed_name,
            team=snapshot.team,
            slot=snapshot.slot,
            options={
                "use_dlc_content": slot_data["use_dlc_content"],
                "dlc_logic_timing": slot_data["dlc_logic_timing"],
                "goal": slot_data["goal"],
                "goal_endpoint_event": slot_data["goal_endpoint_event"],
                "additional_victory_requirements": list(
                    slot_data["additional_victory_requirements"]
                ),
                "special_weapon": slot_data["special_weapon"],
                "enhanced_melee_damage": slot_data["enhanced_melee_damage"],
                "include_weapon_mastery_challenges": slot_data[
                    "include_weapon_mastery_challenges"
                ],
                "praetor_suit_upgrades_in_pool": slot_data[
                    "praetor_suit_upgrades_in_pool"
                ],
                "trap_percentage": slot_data["trap_percentage"],
                "enabled_traps": list(slot_data["enabled_traps"]),
                "randomize_dash": slot_data["randomize_dash"],
                REVEAL_AP_LOCATIONS_OPTION_KEY: slot_data[REVEAL_AP_LOCATIONS_OPTION_KEY],
                **{key: slot_data[key] for key in PHYSICAL_OPTION_KEYS},
                "death_link": slot_data.get("death_link", False),
                "death_link_mode": slot_data.get("death_link_mode", "soft"),
                "starting_inventory": starting_inventory,
                "starting_weapon": slot_data["starting_weapon"],
            },
            active_location_ids=list(snapshot.active_location_ids),
            placements=snapshot.placements,
            static_content_digest=static_content_digest,
        )


class RoomCompiler:
    """Assemble canonical base resources and selected dependent map payloads."""

    DEVINV_MAP_KEY = "e1m1_intro"
    FORTRESS_BATTERY_LABEL_SPEC_PATH = ROOT / "data" / "fortress_battery_labels.json"
    FORTRESS_BATTERY_LOCATION_IDS = frozenset({
        7770087, 7770088, 7770163, 7770164, 7770165, 7770166, 7770167,
        7770168, 7770169, 7770171, 7770253, 7770254, 7770255,
    })
    ENHANCED_MELEE_PATH = "gameresources_patch1/generated/decls/damage/damage/player/melee_d5_forward.decl"
    ENHANCED_MELEE_DECL = b'''{
\tinherit = "damage/player/directional_melee";
\tedit = {
\t\tdamageParms = {
\t\t\tweaponDamageType = "PLAYER_WEAPON_MELEE";
\t\t\tdoom5MeleeTest = {
\t\t\t\timpulseVelocity = {
\t\t\t\t\tx = 18;
\t\t\t\t\tz = 1.5;
\t\t\t\t}
\t\t\t\tmonsterImpulseScales = {
\t\t\t\t\tnum = 1;
\t\t\t\t\titem[0] = {
\t\t\t\t\t\tmonsterType = "AI_MONSTER_HELLKNIGHT AI_MONSTER_DREADKNIGHT AI_MONSTER_PINKY AI_MONSTER_SPECTRE AI_MONSTER_CACODEMON AI_MONSTER_PAIN_ELEMENTAL AI_MONSTER_MANCUBUS AI_MONSTER_CYBER_MANCUBUS AI_MONSTER_ARACHNOTRON AI_MONSTER_REVENANT AI_MONSTER_WHIPLASH";
\t\t\t\t\t\tvalue = 0.800000012;
\t\t\t\t\t}
\t\t\t\t}
\t\t\t\tmonsterImpulsePainTypes = {
\t\t\t\t\tnum = 7;
\t\t\t\t\titem[0] = "PAIN_FALTER_LIGHT";
\t\t\t\t\titem[1] = "PAIN_FALTER";
\t\t\t\t\titem[2] = "PAIN_PUSHBACK";
\t\t\t\t\titem[3] = "PAIN_STAGGER";
\t\t\t\t\titem[4] = "PAIN_STAGGER_VULNERABLE";
\t\t\t\t\titem[5] = "PAIN_KNOCKDOWN";
\t\t\t\t\titem[6] = "PAIN_LIVING_RAGDOLL";
\t\t\t\t}
\t\t\t\tattackerKnockback = 43.2054024;
\t\t\t\tattackerKnockbackMS = 200;
\t\t\t\tattackerVelocityScale = 0.850000024;
\t\t\t\tmonsterMaxImpulseVelocity = {
\t\t\t\t\tnum = 2;
\t\t\t\t\titem[0] = {
\t\t\t\t\t\tmonsterType = "AI_MONSTER_ZOMBIE_TIER_1 AI_MONSTER_ZOMBIE_TIER_3 AI_MONSTER_IMP AI_MONSTER_STONE_IMP AI_MONSTER_GARGOYLE AI_MONSTER_PROWLER AI_MONSTER_CURSED_PROWLER AI_MONSTER_SHOTGUN_SOLDIER AI_MONSTER_CARCASS AI_MONSTER_LOSTSOUL AI_MONSTER_CUEBALL";
\t\t\t\t\t\tvalue = 20;
\t\t\t\t\t}
\t\t\t\t\titem[1] = {
\t\t\t\t\t\tmonsterType = "AI_MONSTER_HELLKNIGHT AI_MONSTER_DREADKNIGHT AI_MONSTER_PINKY AI_MONSTER_SPECTRE AI_MONSTER_CACODEMON AI_MONSTER_PAIN_ELEMENTAL AI_MONSTER_MANCUBUS AI_MONSTER_CYBER_MANCUBUS AI_MONSTER_ARACHNOTRON AI_MONSTER_REVENANT AI_MONSTER_WHIPLASH AI_MONSTER_DOOM_HUNTER AI_MONSTER_MARAUDER AI_MONSTER_BARON AI_MONSTER_TYRANT AI_MONSTER_ARCHVILE AI_MONSTER_GLADIATOR AI_MONSTER_ICON_OF_SIN AI_MONSTER_MAYKR_ANGEL";
\t\t\t\t\t\tvalue = 18.5;
\t\t\t\t\t}
\t\t\t\t}
\t\t\t\tisDoom5Melee = true;
\t\t\t\tfreezePlayer = true;
\t\t\t}
\t\t\tdamageDirAttackerToTarget = true;
\t\t}
\t\tmaxDamage = {
\t\t\tdefaultValue = 160;
\t\t}
\t\tminDamage = {
\t\t\tdefaultValue = 160;
\t\t}
\t}
}
'''

    def __init__(
        self,
        base_resource: Path,
        payload_resource: Path,
        payload_manifest: Path,
        *,
        decompressor: Path | None = None,
        dependency_manager: object | None = None,
        consent: object | None = None,
    ):
        from tools.release.room_payloads import canonical_json, load_room_payload_manifest
        missing = [
            str(p)
            for p in (base_resource, payload_resource, payload_manifest)
            if not p.is_file()
        ]
        if missing:
            raise FileNotFoundError(
                "Release package is incomplete or corrupted. "
                "Re-extract the full DOOM Eternal Archipelago package."
            )
        self.base_resource = base_resource
        self.payload_resource = payload_resource
        self.payload_manifest = load_room_payload_manifest(payload_manifest)
        static_material = {
            "schema": 1,
            "base_mod_sha256": hashlib.sha256(base_resource.read_bytes()).hexdigest(),
            "room_payloads_sha256": hashlib.sha256(payload_resource.read_bytes()).hexdigest(),
            "room_payload_manifest_sha256": hashlib.sha256(
                canonical_json(self.payload_manifest)
            ).hexdigest(),
            "conditional_members": {
                self.ENHANCED_MELEE_PATH: hashlib.sha256(
                    self.ENHANCED_MELEE_DECL
                ).hexdigest(),
                "fortress_battery_labels": hashlib.sha256(
                    self.FORTRESS_BATTERY_LABEL_SPEC_PATH.read_bytes()
                ).hexdigest(),
            },
        }
        self.static_content_digest = hashlib.sha256(
            canonical_json(static_material)
        ).hexdigest()
        self.decompressor = decompressor
        self.dependency_manager = dependency_manager
        self.consent = consent

    def _apply_placement_strings(self, assembled: dict[str, bytes], placements: tuple) -> None:
        """Merge placement-aware receipt strings into the packaged locale tables."""
        from collections import defaultdict

        from tools.maps.notification_formatting import item_receipt_text, location_sent_text

        per_item: dict[int, list] = defaultdict(list)
        for record in placements:
            per_item[record.item_id].append(record)
        for locale in ("english", "portuguese"):
            member = f"gameresources_patch1/EternalMod/strings/{locale}.json"
            if member not in assembled:
                continue
            table = json.loads(assembled[member])
            entries = table.get("strings")
            if not isinstance(entries, list):
                continue
            by_name = {
                entry["name"]: entry
                for entry in entries
                if isinstance(entry, dict) and isinstance(entry.get("name"), str)
            }
            for item_id, records in per_item.items():
                if len(records) != 1:
                    continue
                record = records[0]
                entry = by_name.get(f"#str_ap_notify_item_{item_id}")
                if entry is None or not isinstance(entry.get("text"), str):
                    continue
                entry["text"] = item_receipt_text(
                    entry["text"],
                    local=record.local,
                    trap=record.trap,
                    recipient_name=record.recipient_name,
                )
            for record in placements:
                sent_key = f"#str_ap_location_sent_{record.location_id}"
                by_name[sent_key] = {
                    "name": sent_key,
                    "text": location_sent_text(record.document()),
                }
            table["strings"] = sorted(by_name.values(), key=lambda entry: entry["name"])
            assembled[member] = (
                json.dumps(table, indent=4, ensure_ascii=False) + "\n"
            ).encode("utf-8")

    def _apply_placement_entities(
        self, assembled: dict[str, bytes], placements: tuple[PlacementRecord, ...]
    ) -> None:
        """Bind packaged location notifications to their room placements."""
        import re

        from tools.maps.notification_formatting import placement_sent_key

        decompressor = self._verified_decompressor()

        placement_by_id = {record.location_id: record for record in placements}
        fortress_member_suffix, fortress_labels = self._fortress_battery_label_entities(
            placement_by_id
        )
        fortress_labels_written = False
        notification_header = re.compile(
            r'(entityDef\s+ap_notify_location_(\d+)\s*\{.*?'
            r'header\s*=\s*")#str_ap_location_sent(";)',
            re.DOTALL,
        )
        for member, content in list(assembled.items()):
            if not member.endswith(".entities"):
                continue

            with tempfile.TemporaryDirectory(prefix="doom-ap-entities-") as temporary:
                temporary_dir = Path(temporary)
                compressed = temporary_dir / "input.entities"
                decoded = temporary_dir / "decoded.entities"
                recompressed = temporary_dir / "output.entities"
                compressed.write_bytes(content)
                try:
                    subprocess.run(
                        [str(decompressor), "--decompress", str(compressed), str(decoded)],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                except subprocess.CalledProcessError as error:
                    detail = (error.stderr or error.stdout or "").strip()[:500]
                    raise ValueError(
                        f"could not decompress packaged entities member {member}: {detail}"
                    ) from error
                if not decoded.is_file():
                    raise ValueError(
                        f"idFileDeCompressor produced no decoded entities member: {member}"
                    )
                try:
                    text = decoded.read_bytes().decode("utf-8")
                except UnicodeDecodeError as error:
                    raise ValueError(
                        f"packaged entities member is not UTF-8 after decompression: {member}"
                    ) from error

                def replace_header(match: re.Match[str]) -> str:
                    location_id = int(match.group(2))
                    if location_id not in placement_by_id:
                        raise ValueError(
                            "packaged location notification lacks placement mapping: "
                            f"{location_id}"
                        )
                    return (
                        match.group(1)
                        + placement_sent_key(location_id)
                        + match.group(3)
                    )

                rewritten_text = notification_header.sub(replace_header, text)
                if member.endswith(fortress_member_suffix):
                    if fortress_labels_written:
                        raise ValueError("room package contains multiple Fortress hub entity members")
                    rewritten_text = rewritten_text.rstrip() + "\n" + fortress_labels
                    fortress_labels_written = True
                rewritten = rewritten_text.encode("utf-8")
                decoded.write_bytes(rewritten)
                try:
                    subprocess.run(
                        [str(decompressor), "--compress", str(decoded), str(recompressed)],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                except subprocess.CalledProcessError as error:
                    detail = (error.stderr or error.stdout or "").strip()[:500]
                    raise ValueError(
                        f"could not recompress packaged entities member {member}: {detail}"
                    ) from error
                if not recompressed.is_file():
                    raise ValueError(
                        f"idFileDeCompressor produced no encoded entities member: {member}"
                    )
                assembled[member] = recompressed.read_bytes()
                LOGGER.info(
                    "ROOM_PACKAGE_MEMBER member=%s compressed_bytes=%d decoded_bytes=%d encoded_bytes=%d",
                    member[:256],
                    len(content),
                    len(rewritten),
                    len(assembled[member]),
                )
        if not fortress_labels_written:
            raise ValueError(
                "room package lacks Fortress hub entities required for Battery placement labels"
            )

    @staticmethod
    def _doom_gui_text(value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Fortress Battery placement text contains empty display name")
        return normalized.replace("\\", "\\\\").replace('"', '\\"')

    def _fortress_battery_label_entities(
        self, placement_by_id: dict[int, PlacementRecord]
    ) -> tuple[str, str]:
        """Compile seed-specific pre-purchase labels for all Battery consumers."""
        from doom_eap.presentation import (
            ARCHIPELAGO_PRESENTATION_COLORS,
            color_rgb_floats,
            item_classification_color_key,
        )

        missing = sorted(self.FORTRESS_BATTERY_LOCATION_IDS - set(placement_by_id))
        if missing:
            raise ValueError(
                "Fortress Battery placement scout is incomplete; missing location IDs: "
                + ", ".join(str(value) for value in missing)
            )
        document = json.loads(self.FORTRESS_BATTERY_LABEL_SPEC_PATH.read_text(encoding="utf-8"))
        if document.get("schema_version") != 1:
            raise ValueError("Fortress Battery label specification schema is unsupported")
        member_suffix = document.get("map_member_suffix")
        primitive = document.get("primitive")
        rows = document.get("labels")
        if not isinstance(member_suffix, str) or not member_suffix.endswith(".entities"):
            raise ValueError("Fortress Battery label map member is invalid")
        if not isinstance(primitive, dict) or not isinstance(rows, list):
            raise ValueError("Fortress Battery label specification is incomplete")
        required_primitive = {
            "inherit": "gui/text",
            "class": "idGuiEntity_Text",
            "model": "editors/models/gui_text.lwo",
            "swf": "swf/guientity/generic_text.swf",
        }
        if primitive != required_primitive:
            raise ValueError("Fortress Battery label primitive contract is invalid")
        rows_by_id: dict[int, tuple[float, float, float]] = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"location_id", "source_entity", "position"}:
                raise ValueError("Fortress Battery label transform row is invalid")
            location_id = row["location_id"]
            position = row["position"]
            if (
                not isinstance(location_id, int)
                or not isinstance(row["source_entity"], str)
                or not row["source_entity"]
                or not isinstance(position, list)
                or len(position) != 3
                or any(not isinstance(value, (int, float)) for value in position)
                or location_id in rows_by_id
            ):
                raise ValueError("Fortress Battery label transform row is invalid")
            rows_by_id[location_id] = tuple(float(value) for value in position)  # type: ignore[assignment]
        if set(rows_by_id) != self.FORTRESS_BATTERY_LOCATION_IDS:
            raise ValueError("Fortress Battery label specification must cover exactly 13 consumers")

        entities: list[str] = []
        for location_id in sorted(rows_by_id):
            record = placement_by_id[location_id]
            if record.trap:
                display = "A TRAP\\nFOR SOMEONE"
            elif record.local:
                display = "YOUR " + self._doom_gui_text(record.item_name).upper()
            else:
                display = (
                    self._doom_gui_text(record.item_name).upper()
                    + "\\nFOR "
                    + self._doom_gui_text(record.recipient_name).upper()
                )
            color_key = item_classification_color_key(
                record.classification, trap=record.trap
            )
            red, green, blue = color_rgb_floats(
                ARCHIPELAGO_PRESENTATION_COLORS[color_key]
            )
            x, y, z = rows_by_id[location_id]
            entities.append(f'''entity {{
\tentityDef ap_fortress_battery_placement_{location_id} {{
\t\tinherit = "gui/text";
\t\tclass = "idGuiEntity_Text";
\t\texpandInheritance = false;
\t\tpoolCount = 0;
\t\tpoolGranularity = 2;
\t\tnetworkReplicated = false;
\t\tdisableAIPooling = false;
\t\tedit = {{
\t\t\tflags = {{
\t\t\t\tnoknockback = false;
\t\t\t}}
\t\t\trenderModelInfo = {{
\t\t\t\tmodel = "editors/models/gui_text.lwo";
\t\t\t\tscale = {{
\t\t\t\t\tx = 10;
\t\t\t\t\ty = 10;
\t\t\t\t\tz = 10;
\t\t\t\t}}
\t\t\t}}
\t\t\tclipModelInfo = {{
\t\t\t\ttype = "CLIPMODEL_NONE";
\t\t\t}}
\t\t\tswf = "swf/guientity/generic_text.swf";
\t\t\tspawnPosition = {{
\t\t\t\tx = {x:.6f};
\t\t\t\ty = {y:.6f};
\t\t\t\tz = {z:.6f};
\t\t\t}}
\t\t\tspawnOrientation = {{
\t\t\t\tmat = {{
\t\t\t\t\tmat[0] = {{ x = 1; y = 0; z = 0; }}
\t\t\t\t\tmat[1] = {{ x = 0; y = 1; z = 0; }}
\t\t\t\t\tmat[2] = {{ x = 0; y = 0; z = 1; }}
\t\t\t\t}}
\t\t\t}}
\t\t\tdormancy = {{
\t\t\t\tallowDistanceDormancy = false;
\t\t\t\tallowDormancy = false;
\t\t\t\tallowPvsDormancy = false;
\t\t\t}}
\t\t\tdynamicMoveActive = true;
\t\t\tswfScale = 0.020000;
\t\t\theaderText = {{
\t\t\t\ttext = "{display}";
\t\t\t\tcolor = {{
\t\t\t\t\tr = {red:.6f};
\t\t\t\t\tg = {green:.6f};
\t\t\t\t\tb = {blue:.6f};
\t\t\t\t}}
\t\t\t\trelativeWidth = 1;
\t\t\t\talignment = "SWF_ET_ALIGN_CENTER";
\t\t\t}}
\t\t\tbillboard = true;
\t\t}}
\t}}
}}
''')
        return member_suffix, "".join(entities)

    def _verified_decompressor(self) -> Path:
        from .launcher_platform import IDFILE_DECOMPRESSOR

        if self.decompressor is None:
            if self.dependency_manager is None or not callable(self.consent):
                raise FileNotFoundError(
                    "idFileDeCompressor is unavailable in verified dependency cache"
                )
            installed = self.dependency_manager.acquire(  # type: ignore[attr-defined]
                IDFILE_DECOMPRESSOR,
                consent=self.consent,
            )
            decompressor = Path(installed.executable)
        else:
            decompressor = self.decompressor
        decompressor = decompressor.expanduser().resolve()
        if not decompressor.is_file():
            raise FileNotFoundError(
                "idFileDeCompressor is unavailable in verified dependency cache"
            )
        actual_sha256 = hashlib.sha256(decompressor.read_bytes()).hexdigest()
        if actual_sha256 != IDFILE_DECOMPRESSOR.sha256:
            raise ValueError(
                "Cached idFileDeCompressor SHA-256 mismatch: "
                f"expected {IDFILE_DECOMPRESSOR.sha256}, got {actual_sha256}"
            )
        return decompressor

    def _decompress_entities_text(self, content: bytes, member: str) -> str:
        decompressor = self._verified_decompressor()
        with tempfile.TemporaryDirectory(prefix="doom-ap-patch-") as temporary:
            temporary_dir = Path(temporary)
            compressed = temporary_dir / "input.entities"
            decoded = temporary_dir / "decoded.entities"
            compressed.write_bytes(content)
            try:
                subprocess.run(
                    [str(decompressor), "--decompress", str(compressed), str(decoded)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as error:
                detail = (error.stderr or error.stdout or "").strip()[:500]
                raise ValueError(
                    f"could not decompress compiled entities member {member}: {detail}"
                ) from error
            if not decoded.is_file():
                raise ValueError(
                    f"idFileDeCompressor produced no decoded entities member: {member}"
                )
            try:
                return decoded.read_bytes().decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(
                    f"compiled entities member is not UTF-8 after decompression: {member}"
                ) from error

    def _compress_entities_text(self, text: str) -> bytes:
        decompressor = self._verified_decompressor()
        with tempfile.TemporaryDirectory(prefix="doom-ap-overlay-") as temporary:
            temporary_dir = Path(temporary)
            decoded = temporary_dir / "decoded.entities"
            compressed = temporary_dir / "compressed.entities"
            decoded.write_text(text, encoding="utf-8")
            try:
                subprocess.run(
                    [str(decompressor), "--compress", str(decoded), str(compressed)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as error:
                detail = (error.stderr or error.stdout or "").strip()[:500]
                raise ValueError(f"could not compress generated entity overlay: {detail}") from error
            if not compressed.is_file():
                raise ValueError("idFileDeCompressor produced no encoded overlay")
            return compressed.read_bytes()

    def build(self, manifest: SeedManifest, output_root: Path) -> Path:
        from tools.release.room_payloads import assemble_room_files, canonical_json, write_deterministic_zip
        placements = manifest.require_complete_placements()
        if manifest.static_content_digest != self.static_content_digest:
            raise ValueError("room static content identity drifted")
        assembled, selected = assemble_room_files(
            self.base_resource, self.payload_resource, self.payload_manifest, manifest.options
        )
        output_root.mkdir(parents=True, exist_ok=True)
        destination = output_root / f"DoomEternalArchipelago-{manifest.manifest_hash[:16]}.zip"
        # JSON round-trip so tuple fields match what the archive stores.
        seed_document = json.loads(canonical_json(manifest.document()))
        room_config = project_room_config(manifest.options)
        receipt = {
            "schema": 1,
            "manifest_hash": manifest.manifest_hash,
            "physical_options": {key: manifest.options[key] for key in PHYSICAL_OPTION_KEYS},
            "map_payloads": selected,
            "starting_inventory": manifest.options.get("starting_inventory", {}),
            "starting_weapon": manifest.options.get("starting_weapon"),
            "room_config": room_config,
        }
        devinv_path = output_path_for_map(
            Path("."), ROOT / "data" / "map_sources.json", self.DEVINV_MAP_KEY
        ).as_posix()
        devinv_source = build_devinv_loadout(
            manifest.options.get("starting_inventory", {}),
            manifest.options.get("starting_weapon"),
        )
        assembled[devinv_path] = devinv_source.encode("utf-8")
        if manifest.options.get("use_dlc_content", True):
            assembled.update({
                path: source.encode("utf-8")
                for path, source in build_tag_devinv_overrides(
                    manifest.options.get("starting_inventory", {}),
                    manifest.options.get("starting_weapon"),
                ).items()
            })
            from doom_eap.runtime.context_registry import dlc_contexts
            from tools.maps.ap_map_generator import generate_context_marker_overlay
            for context in dlc_contexts():
                if len(context.runtime_maps) != 1 or len(context.map_keys) != 1:
                    raise ValueError(f"DLC context overlay requires one exact map: {context.identity}")
                overlay_member = self.payload_manifest["context_targets"].get(context.identity)
                if not isinstance(overlay_member, str):
                    raise ValueError(f"DLC context lacks room payload target: {context.identity}")
                compiled_content = assembled.get(overlay_member)
                if compiled_content is not None:
                    continue
                overlay_text = generate_context_marker_overlay(
                    context.map_keys[0], context.runtime_maps[0]
                )
                import re
                marker_names = re.findall(r"\bentityDef\s+(\S+)\s*\{", overlay_text)
                if len(marker_names) != len(set(marker_names)):
                    raise ValueError(
                        f"DLC context marker entity names are duplicated: {context.identity}"
                    )
                assembled[overlay_member] = self._compress_entities_text(
                    overlay_text
                )
            from doom_eap.content.content_catalog import load_content_catalog
            from tools.maps.mission_complete_map_patcher import patch_generated_map_text

            catalog = load_content_catalog()
            for context in dlc_contexts():
                map_key = context.map_keys[0]
                spec = catalog.maps.get(map_key)
                if spec is None or not spec.requires_dlc_content:
                    continue
                member = self.payload_manifest["context_targets"][context.identity]
                compiled_content = assembled.get(member)
                if compiled_content is None:
                    raise ValueError(f"DLC map compiled payload is missing: {map_key}/{member}")
                compiled_text = self._decompress_entities_text(compiled_content, member)
                patched_text, _publisher_audit = patch_generated_map_text(
                    map_key, compiled_text, ROOT
                )
                if patched_text != compiled_text:
                    assembled[member] = self._compress_entities_text(patched_text)
        assembled.update({
            "room_config.json": canonical_json(room_config),
            "seed_manifest.json": canonical_json(seed_document),
            "seed_receipt.json": canonical_json(receipt),
        })
        if manifest.options.get("enhanced_melee_damage", False):
            assembled[self.ENHANCED_MELEE_PATH] = self.ENHANCED_MELEE_DECL
        if not manifest.static_precompile:
            self._apply_placement_entities(assembled, placements)
            self._apply_placement_strings(assembled, placements)
        write_deterministic_zip(assembled, destination)
        with zipfile.ZipFile(destination) as output:
            expected = set(assembled)
            if set(output.namelist()) != expected:
                raise ValueError("assembled room package member set drifted")
            if json.loads(output.read("seed_manifest.json")) != seed_document:
                raise ValueError("assembled room manifest validation failed")
            if json.loads(output.read("room_config.json")) != room_config:
                raise ValueError("assembled room config validation failed")
            if json.loads(output.read("seed_receipt.json")) != receipt:
                raise ValueError("assembled room receipt validation failed")
            if devinv_path is not None and devinv_source is not None and output.read(devinv_path) != devinv_source.encode("utf-8"):
                raise ValueError("assembled DevInv validation failed")
            if (self.ENHANCED_MELEE_PATH in output.namelist()) != bool(
                manifest.options.get("enhanced_melee_damage", False)
            ):
                raise ValueError("assembled Enhanced Melee Damage validation failed")
        return destination


@dataclass
class InstallRecord:
    state: str
    game_path: str
    game_fingerprint: str
    endpoint: str
    manifest_hash: str
    installed_files: dict[str, str]
    rollback_path: str | None
    apworld_revision: str = ""
    content_revision: str = ""
    compiler_revision: int = 0
    error: str | None = None


class EternalModInjectorAdapter(Protocol):
    """Runtime adapter boundary; concrete Injector process support is pending."""

    def activate(self, install_root: Path, record: InstallRecord) -> None: ...


class InstallPlan:
    """Install launcher-owned files only, with reverse-order rollback."""

    def __init__(self, target: Path, source: Path):
        self.target = target
        self.source = source

    RECORD_NAME = ".doom_ap_install_record.json"

    def _record_path(self) -> Path:
        return self.target / self.RECORD_NAME

    def _read_record(self) -> dict[str, Any] | None:
        path = self._record_path()
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def _write_record(self, record: InstallRecord) -> None:
        self.target.mkdir(parents=True, exist_ok=True)
        temporary = self._record_path().with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(record), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self._record_path())

    def install(self, record: InstallRecord, *, fail_after: int | None = None) -> InstallRecord:
        files = sorted(path for path in self.source.rglob("*") if path.is_file())
        previous = self._read_record()
        previous_owned = set((previous or {}).get("installed_files", {}))
        expected_hashes = {str(path.relative_to(self.source)): _sha256(path) for path in files}
        if (
            previous
            and previous.get("state") == "active"
            and previous.get("manifest_hash") == record.manifest_hash
            and previous.get("installed_files") == expected_hashes
            and all((self.target / relative).is_file() and _sha256(self.target / relative) == digest for relative, digest in expected_hashes.items())
        ):
            return InstallRecord(**previous)

        collisions = [
            relative for relative in expected_hashes
            if (self.target / relative).exists() and relative not in previous_owned
        ]
        if collisions:
            raise ValueError(f"refusing to replace files not owned by launcher: {collisions}")

        rollback = self.target.parent / f".{self.target.name}.rollback-{record.manifest_hash[:12]}"
        shutil.rmtree(rollback, ignore_errors=True)
        rollback.mkdir(parents=True)
        record.state = "installing"
        record.rollback_path = str(rollback)
        self._write_record(record)
        applied: list[str] = []
        try:
            for index, source_path in enumerate(files, start=1):
                relative = str(source_path.relative_to(self.source))
                destination = self.target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    backup = rollback / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(destination, backup)
                incoming = destination.with_name(f".{destination.name}.incoming")
                shutil.copy2(source_path, incoming)
                os.replace(incoming, destination)
                applied.append(relative)
                if fail_after is not None and index >= fail_after:
                    raise RuntimeError("injected install failure")
        except Exception as error:
            for relative in reversed(applied):
                destination = self.target / relative
                backup = rollback / relative
                if backup.exists():
                    os.replace(backup, destination)
                else:
                    destination.unlink(missing_ok=True)
            record.state = "failed"
            record.error = f"{type(error).__name__}: {error}"
            record.installed_files = dict((previous or {}).get("installed_files", {}))
            self._write_record(record)
            raise
        record.state = "active"
        record.error = None
        record.installed_files = expected_hashes
        self._write_record(record)
        return record

    def rollback(self, record: InstallRecord, error: Exception) -> InstallRecord:
        rollback = Path(record.rollback_path) if record.rollback_path else None
        for relative in reversed(tuple(record.installed_files)):
            destination = self.target / relative
            backup = rollback / relative if rollback else None
            if backup is not None and backup.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, destination)
            else:
                destination.unlink(missing_ok=True)
        record.state = "failed"
        record.error = f"{type(error).__name__}: {error}"
        record.installed_files = {}
        self._write_record(record)
        return record


class ModCompiler:
    def __init__(self, root: Path = ROOT):
        self.root = root

    def active_location_ids(self, options: dict[str, Any] | bool) -> list[int]:
        names = json.loads((self.root / "data" / "location_names.json").read_text(encoding="utf-8"))["locations"]
        ids = sorted(int(location_id) for location_id in names)
        if isinstance(options, bool):
            options = {
                "randomize_chainsaw": False,
                "randomize_dash": options,
                "randomize_first_battery": False,
            }
        physical_ids = {
            int(spec["location_id"])
            for spec in PHYSICAL_OPTIONS.values()
        }
        return [
            location_id for location_id in ids
            if location_id not in (physical_ids - physical_location_ids(options))
        ]

    def known_location_ids(self) -> set[int]:
        return set(self.active_location_ids({key: True for key in PHYSICAL_OPTION_KEYS}))

    def compile(self, manifest: SeedManifest, output_root: Path) -> Path:
        output_root.mkdir(parents=True, exist_ok=True)
        room_config = project_room_config(manifest.options)
        (output_root / "seed_manifest.json").write_text(
            json.dumps(manifest.document(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if not manifest.static_precompile:
            placement_metadata = [record.document() for record in manifest.placements]
            (output_root / "placement_metadata.json").write_text(
                json.dumps(placement_metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (output_root / "placement_string_inputs.json").write_text(
                json.dumps(
                    [
                        {"key": f"#str_ap_location_{record.location_id}", "text": record.location_name}
                        for record in manifest.placements
                    ],
                    indent=2,
                    sort_keys=True,
                ) + "\n",
                encoding="utf-8",
            )
        (output_root / "room_config.json").write_text(
            json.dumps(room_config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        from doom_eap.content.content_catalog import load_content_catalog

        campaign_maps = tuple(
            spec for spec in load_content_catalog(self.root).enabled_maps()
            if spec.key != "hub"
            and (manifest.options.get("use_dlc_content", True) or not spec.requires_dlc_content)
        )
        for map_spec in campaign_maps:
            map_key = map_spec.key
            projected = self.project_map_config(manifest, map_key)
            (output_root / f"{map_key}.locations.json").write_text(
                json.dumps(projected, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        return output_root

    def project_map_config(self, manifest: SeedManifest, map_key: str) -> dict[str, Any]:
        """Project supported campaign config for one map-local transform state."""
        from doom_eap.content.content_catalog import load_content_catalog

        campaign_keys = {
            spec.key for spec in load_content_catalog(self.root).enabled_maps()
            if spec.key != "hub"
            and (manifest.options.get("use_dlc_content", True) or not spec.requires_dlc_content)
        }
        if map_key not in campaign_keys:
            raise ValueError(
                f"map-local room transforms are unsupported for map: {map_key}"
            )
        package = self.root / f"content/maps/{map_key}"
        config = json.loads((package / "locations.json").read_text(encoding="utf-8"))
        descriptor = json.loads((package / "descriptor.json").read_text(encoding="utf-8"))
        config.setdefault("map_key", descriptor["key"])
        config.setdefault("runtime_map", descriptor["runtime_map"])
        assets_path = package / "assets.json"
        if assets_path.is_file():
            assets = json.loads(assets_path.read_text(encoding="utf-8"))
            config = {
                **config,
                "assets": assets.get("assets", []),
                "default_visual_asset": assets.get("default_visual_asset"),
            }
        return project_map_config(config, manifest.options)

    def compile_map(self, manifest: SeedManifest, vanilla_entities: Path, output_entities: Path,
                    map_key: str = "e1m2_war") -> Path:
        """Compile one supported campaign map with map-local room transforms."""
        from doom_eap.content.item_classification import load_item_classifications
        from doom_eap.runtime.item_reconciliation import load_policy_registry
        from tools.maps.ap_map_generator import generate_map

        item_definitions = json.loads(
            (self.root / "data/items.json").read_text(encoding="utf-8")
        )
        policies = load_policy_registry(
            self.root / "data/item_replay_policies.json",
            {int(item_id): definition for item_id, definition in item_definitions.items()},
        )
        with tempfile.TemporaryDirectory() as temporary:
            staged = Path(temporary)
            placements = manifest.require_complete_placements()
            projected = self.project_map_config(manifest, map_key)
            config_path = staged / f"{map_key}.locations.json"
            config_path.write_text(
                json.dumps(projected, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            generate_map(
                vanilla_entities,
                output_entities,
                config_path,
                output_entities.with_suffix(".manifest.json"),
                item_definitions,
                item_names={item_id: policy.name for item_id, policy in policies.items()},
                item_classifications=load_item_classifications(
                    self.root / "data/item_classifications.json"
                ),
                receipt_feedback={
                    item_id: policy.receipt_feedback
                    for item_id, policy in policies.items()
                },
                placement_metadata=(
                    None
                    if manifest.static_precompile
                    else [record.document() for record in placements]
                ),
            )
        output_entities.with_suffix(".seed.json").write_text(
            json.dumps(manifest.document(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return output_entities


class LaunchWorkflow:
    def __init__(self, compiler: ModCompiler | None = None, *, vanilla_exultia: Path | None = None, injector: EternalModInjectorAdapter | None = None):
        self.compiler = compiler or ModCompiler()
        self.vanilla_exultia = vanilla_exultia
        self.injector = injector

    def manifest_for(
        self,
        snapshot: RoomSnapshot,
        *,
        static_content_digest: str = "",
    ) -> SeedManifest:
        return SeedManifest.from_room(
            snapshot,
            self.compiler.known_location_ids(),
            static_content_digest=static_content_digest,
        )

    def execute(self, snapshot: RoomSnapshot, install_root: Path, endpoint: str = "") -> InstallRecord:
        manifest = self.manifest_for(snapshot)
        existing_path = install_root / InstallPlan.RECORD_NAME
        if existing_path.is_file():
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
            if existing.get("state") == "active" and existing.get("manifest_hash") != manifest.manifest_hash:
                # Different identity is valid, but always produces explicit plan/record.
                pass
        identity = release_identity()
        with tempfile.TemporaryDirectory(prefix="doom-ap-compile-") as temporary:
            compiled = self.compiler.compile(manifest, Path(temporary))
            if self.vanilla_exultia is not None:
                self.compiler.compile_map(
                    manifest,
                    self.vanilla_exultia,
                    compiled / "e1m2_war.entities",
                )
            manifest_path = compiled / "seed_manifest.json"
            if not manifest_path.is_file() or json.loads(manifest_path.read_text())["manifest_hash"] != manifest.manifest_hash:
                raise ValueError("compiler output manifest validation failed")
            record = InstallRecord(
                state="installing",
                game_path=str(install_root),
                game_fingerprint="directory-backend",
                endpoint=endpoint,
                manifest_hash=manifest.manifest_hash,
                installed_files={},
                rollback_path=None,
                apworld_revision=identity["apworld_revision"],
                content_revision=identity["content_revision"],
                compiler_revision=identity["compiler_revision"],
            )
            plan = InstallPlan(install_root, compiled)
            installed = plan.install(record)
            if self.injector is not None:
                try:
                    self.injector.activate(install_root, installed)
                except Exception as error:
                    plan.rollback(installed, error)
                    raise
            return installed

    @staticmethod
    def client_config_path(client_dir: Path) -> Path:
        """Resolve config beside native ap_client."""
        candidate = Path(client_dir)
        if candidate.is_file() or candidate.name.casefold() in {"ap_client", "ap_client.exe"}:
            candidate = candidate.parent
        return candidate / "ap_config.json"

    @staticmethod
    def _steam_id3(steam_remote_dir: object, configured: object = None) -> int:
        remote = Path(str(steam_remote_dir)).expanduser()
        if (
            remote.name.casefold() != "remote"
            or remote.parent.name != "782330"
            or remote.parent.parent.parent.name.casefold() != "userdata"
        ):
            raise ValueError(
                "steam_remote_dir must end with userdata/<STEAM_ID3>/782330/remote"
            )
        account = remote.parent.parent.name
        if not account.isascii() or not account.isdigit() or int(account) <= 0:
            raise ValueError(
                "steam_remote_dir must contain numeric userdata/<STEAM_ID3>"
            )
        inferred = int(account)
        if configured is not None:
            if isinstance(configured, bool) or not isinstance(configured, (int, str)):
                raise ValueError("steam_id3 must be a positive integer")
            try:
                configured_id = int(configured)
            except ValueError as error:
                raise ValueError("steam_id3 must be a positive integer") from error
            if configured_id <= 0 or configured_id != inferred:
                raise ValueError(
                    "steam_id3 does not match userdata directory in steam_remote_dir"
                )
        return inferred

    @staticmethod
    def write_client_config(
        client_dir: Path,
        *,
        endpoint: str | None = None,
        manifest_hash: str | None = None,
        runtime_config: Mapping[str, object] | None = None,
    ) -> Path:
        """Atomically materialize native runtime config from nonsecret launcher state."""
        path = LaunchWorkflow.client_config_path(client_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        config = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(config, dict):
            raise ValueError("native client configuration must contain an object")

        source = dict(runtime_config or {})
        for key in CLIENT_CONFIG_FIELDS - {"steam_id3", "steam_remote_dir"}:
            value = source.get(key)
            if key == "server_address" and endpoint is not None:
                value = endpoint
            elif key == "seed_manifest_hash" and manifest_hash is not None:
                value = manifest_hash
            if value is not None:
                config[key] = value

        remote = source.get("steam_remote_dir") or config.get("steam_remote_dir")
        configured_id = source.get("steam_id3")
        if configured_id is None and not source.get("steam_remote_dir"):
            configured_id = config.get("steam_id3")
        if remote:
            config["steam_remote_dir"] = str(Path(str(remote)).expanduser())
            config["steam_id3"] = LaunchWorkflow._steam_id3(
                config["steam_remote_dir"], configured_id
            )
        config.pop("password", None)

        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(json.dumps(config, indent=2, sort_keys=True) + "\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, path)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
        return path

    def join(self, room: dict[str, Any], client_dir: Path, endpoint: str) -> SeedManifest:
        """Offline simulation adapter. Production Join consumes RoomSnapshot."""
        options = room.get("options", {})
        dash = bool(options.get("randomize_dash", False))
        physical_options = {
            "randomize_chainsaw": bool(options.get("randomize_chainsaw", False)),
            "randomize_dash": dash,
            "randomize_first_battery": bool(options.get("randomize_first_battery", False)),
        }
        active = self.compiler.active_location_ids(physical_options)
        slot_data = {
            **physical_options,
            REVEAL_AP_LOCATIONS_OPTION_KEY: bool(
                options.get(REVEAL_AP_LOCATIONS_OPTION_KEY, False)
            ),
            **({"starting_inventory": options["starting_inventory"]} if "starting_inventory" in options else {}),
            **({"starting_weapon": options["starting_weapon"]} if "starting_weapon" in options else {}),
        }
        snapshot = RoomSnapshot.from_packets(
            {"seed_name": room["seed_name"]},
            {
                "team": room["team"], "slot": room["slot"],
                "slot_data": slot_data,
                "missing_locations": active, "checked_locations": [],
            },
        )
        manifest = self.manifest_for(snapshot)
        self.execute(snapshot, client_dir / "compiled_mod", endpoint)
        self.write_client_config(client_dir, endpoint=endpoint, manifest_hash=manifest.manifest_hash)
        return manifest


def validate_game(game_root: Path, meathook_path: Path, client_dir: Path, saves_dir: Path) -> None:
    required = [game_root / "DOOMEternalx64vk.exe", game_root / "base" / "classicwads", meathook_path, client_dir / "bridge_client.py", saves_dir]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise ValueError("missing required game/install paths: " + ", ".join(missing))
