"""Headless seed compiler and install workflow. UI/CLI are adapters only."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from doom_eap.content.compiler_identity import load_compiler_source_identity
from doom_eap.content.killsanity_plan import milestone_ids
from doom_eap.content.physical_options import (
    PHYSICAL_OPTION_KEYS,
    PHYSICAL_OPTIONS,
    physical_location_ids,
    project_map_config,
    project_room_config,
)
from doom_eap.content.slot_data_contract import placement_digest
from doom_eap.launcher.launcher_injector import IDFILE_DECOMPRESSOR, client_config_write
from doom_eap.launcher.launcher_platform import publish_file
from doom_eap.runtime.observers import SlotAdmissionError, admit_slot_data
from tools.decls.devinv_builder import (
    build_devinv_loadout,
    build_tag_devinv_overrides,
    output_path_for_map,
)

MODULE_DIR = Path(__file__).resolve().parent
ROOT = MODULE_DIR if (MODULE_DIR / "data").is_dir() else Path(__file__).resolve().parents[2]
DASH_LOCATION_ID = 7770083
DASH_ENTITY = 'AP_CHECK_CAPITOL_PROGRESS_DASH_1'
_CONTRACT_IDENTITY = json.loads(
    (ROOT / "data" / "content_identity.json").read_text(encoding="utf-8")
)
MANIFEST_SCHEMA_VERSION = int(_CONTRACT_IDENTITY["manifest_schema_version"])
SLOT_DATA_SCHEMA_VERSION = int(_CONTRACT_IDENTITY["slot_data_schema_version"])
REVEAL_AP_LOCATIONS_OPTION_KEY = "reveal_ap_locations_on_automap"
RESOLVED_ROOM_CAPABILITIES = frozenset({"unified_campaign_v1", "fortress_economy_v1", "progressive_blood_punch_v1", "master_levels_v1"})
ROOM_SLOT_DEFAULTS: dict[str, Any] = {
    "use_dlc_content": True,
    "include_dlc_missions": True,
    "logic_difficulty": "Standard",
    "mastery_requirements": "Half",
    "resource_profile": "Balanced",
    "item_pool_counts": {},
    "filler_weights": {},
    "trap_weights": {},
    "goal": "Acquire the Unmaykr",
    "goal_endpoint_event": "Internal Goal Endpoint: Acquire the Unmaykr",
    "additional_victory_requirements": [],
    "special_weapon": "Progressive Special Weapon",
    "enhanced_melee_damage": False,
    "randomize_chainsaw": False,
    "randomize_dash": False,
    "randomize_first_battery": False,
    "include_weapon_mastery_challenges": True,
    "include_slayer_gates": True,
    "include_secret_encounters": True,
    "arena_checks": 1,
    "easier_secret_encounters": False,
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
CLIENT_CONFIG_FIELDS = frozenset({
    "steam_remote_dir",
    "steam_id3",
    "doom_base_dir",
    "save_games_dir",
    "server_address",
    "seed_manifest_hash",
    "killsanity_counter_visible",
    "death_link_grace",
    "core_runtime_manifest",
    "selected_core_runtime_manifest",
    "proton_executable",
    "proton_compat_data_dir",
})


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def release_identity() -> dict[str, Any]:
    return json.loads(
    (ROOT / "data" / "content_identity.json").read_text(encoding="utf-8")
)


def _normalize_slot_data(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the room line before preparing its content or local state."""
    admit_slot_data(raw)
    return dict(raw)


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
    def _validate_placement_numbers(cls, value):
        integer_fields = ("location_id", "item_id", "recipient_slot", "classification")
        for field in integer_fields:
            field_value = value[field]
            if not isinstance(field_value, int) or isinstance(field_value, bool):
                raise ValueError('placement {value_0} must be an integer'.format(value_0=f'{field}'))
        if value["location_id"] <= 0 or value["recipient_slot"] < 1:
            raise ValueError('placement location_id and recipient_slot must be positive')
        if value["classification"] < 0:
            raise ValueError('placement classification must be non-negative')

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PlacementRecord:
        if not isinstance(value, Mapping):
            raise ValueError('placement must be an object')
        required = {
            "location_id", "location_name", "item_id", "item_name",
            "recipient_slot", "recipient_name", "classification", "trap", "local",
        }
        if set(value) != required:
            raise ValueError(
                'placement fields must be exactly ' + ", ".join(sorted(required))
            )
        cls._validate_placement_numbers(value)
        for field in ("location_name", "item_name", "recipient_name"):
            if not isinstance(value[field], str) or not value[field].strip():
                raise ValueError('placement {value_0} must be non-empty text'.format(value_0=f'{field}'))
        if not isinstance(value["trap"], bool) or not isinstance(value["local"], bool):
            raise ValueError('placement trap and local must be boolean')
        expected_trap = bool(value["classification"] & 0b00100)
        if value["trap"] != expected_trap:
            raise ValueError('placement trap does not match classification')
        return cls(**dict(value))

    def document(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_placements(
    placements: object, active_location_ids: tuple[int, ...], slot: int
) -> tuple[PlacementRecord, ...]:
    if not isinstance(placements, list | tuple):
        raise ValueError('placements must be a list')
    records = tuple(
        value if isinstance(value, PlacementRecord) else PlacementRecord.from_mapping(value)
        for value in placements
    )
    ids = [record.location_id for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError('placements contain duplicate location IDs')
    expected = set(active_location_ids)
    actual = set(ids)
    if actual != expected:
        raise ValueError(
            'placement set is incomplete or unknown: missing={value_0}, unknown={value_1}'.format(value_0=f'{sorted(expected - actual)}', value_1=f'{sorted(actual - expected)}')
        )
    for record in records:
        if record.local != (record.recipient_slot == slot):
            raise ValueError(
                'placement local flag disagrees with recipient slot at {value_0}'.format(value_0=f'{record.location_id}')
            )
    return tuple(sorted(records, key=lambda record: record.location_id))


def _resolved_kill_ids(slot_data):
    return milestone_ids(slot_data["killsanity"])


def _room_placements(slot_data, placements, active, slot):
    try:
        records = _normalize_placements(placements, active, slot)
    except ValueError as error:
        raise SlotAdmissionError("slot_placement", str(error)) from error
    rows = [[record.location_id, record.item_id, record.recipient_slot] for record in records]
    if placement_digest(rows) != slot_data["placement_digest"]:
        raise SlotAdmissionError("slot_digest", 'Room placements digest mismatch.')
    return records


def _validate_manifest_starting(options):
    if "starting_inventory" in options:
        inventory = options["starting_inventory"]
        if not isinstance(inventory, dict):
            raise ValueError('manifest starting_inventory must be an object')
        for name, quantity in inventory.items():
            if not isinstance(name, str) or type(quantity) is not int or quantity < 1:
                raise ValueError('manifest starting_inventory has invalid name or quantity')
    weapon = options.get("starting_weapon")
    if weapon is not None and not isinstance(weapon, str):
        raise ValueError('manifest starting_weapon must be string or null')


def _admit_manifest(manifest):
    if not manifest.static_precompile:
        rows = [[record.location_id, record.item_id, record.recipient_slot] for record in manifest.placements]
        return admit_slot_data(manifest.options, placements=rows)


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
            raise ValueError('RoomInfo.seed_name is required')
        if not isinstance(team, int) or isinstance(team, bool) or team < 0:
            raise ValueError('Connected.team must be a non-negative integer')
        if not isinstance(slot, int) or isinstance(slot, bool) or slot < 1:
            raise ValueError('Connected.slot must be a positive integer')
        slot_data = _normalize_slot_data(raw_slot_data)

        def locations(field: str) -> tuple[int, ...]:
            values = connected.get(field)
            if not isinstance(values, list | tuple | set):
                raise ValueError('Connected.{value_0} must be a location list'.format(value_0=f'{field}'))
            if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
                raise ValueError('Connected.{value_0} contains invalid location ID'.format(value_0=f'{field}'))
            return tuple(sorted(set(values)))

        missing = locations("missing_locations")
        checked = locations("checked_locations")
        if set(missing) & set(checked):
            raise ValueError('Connected missing_locations and checked_locations overlap')
        raw_placements = connected.get("placements")
        active = tuple(sorted(set(missing) | set(checked)))
        placements = _room_placements(slot_data, raw_placements, active, slot)

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
    def _create_resolved(cls, *, seed_name, team, slot, options, active_location_ids, placements, static_content_digest):
        resolved = admit_slot_data(options)
        records = _room_placements(options, placements, tuple(sorted(set(active_location_ids))), slot)
        if static_content_digest and not re.fullmatch(r"[0-9a-f]{64}", static_content_digest):
            raise ValueError('static_content_digest must be an empty string or SHA-256')
        identity = release_identity()
        compatibility = resolved.compatibility
        payload = {
            "schema": compatibility.manifest_schema_version, "game": identity["game"],
            "seed_name": seed_name, "team": int(team), "slot": int(slot),
            "bridge_protocol": compatibility.bridge_protocol, "apworld_revision": compatibility.apworld_version,
            "content_revision": identity["content_revision"], "compiler_revision": identity["compiler_revision"],
            "manifest_schema_version": compatibility.manifest_schema_version,
            "mod_contract_revision": compatibility.mod_contract_revision,
            "required_capabilities": tuple(sorted(set(compatibility.required_capabilities) | RESOLVED_ROOM_CAPABILITIES)),
            "options": dict(options),
            "active_location_ids": tuple(sorted(set(active_location_ids))),
            "placements": [record.document() for record in records],
            "static_content_digest": static_content_digest, "static_precompile": False,
        }
        return cls(**{**payload, "placements": records}, manifest_hash=hashlib.sha256(_canonical(payload)).hexdigest())

    @classmethod
    def _from_resolved_room(cls, snapshot, known_location_ids, static_content_digest):
        unknown = set(snapshot.active_location_ids) - known_location_ids - _resolved_kill_ids(snapshot.slot_data)
        if unknown:
            raise SlotAdmissionError("slot_reference", 'Room contains unknown location IDs: {value_0}'.format(value_0=f'{sorted(unknown)}'))
        return cls.create(
            seed_name=snapshot.seed_name, team=snapshot.team, slot=snapshot.slot, options=snapshot.slot_data,
            active_location_ids=list(snapshot.active_location_ids), placements=snapshot.placements,
            static_content_digest=static_content_digest,
        )

    @classmethod
    def _static_manifest_options(cls, options):
        normalized_options = {
            key: options[key]
            for key in sorted(options)
        }
        for key in PHYSICAL_OPTION_KEYS:
            normalized_options.setdefault(key, False)
            if not isinstance(normalized_options[key], bool):
                raise ValueError('manifest option {value_0} must be boolean'.format(value_0=f'{key}'))
        normalized_options.setdefault(REVEAL_AP_LOCATIONS_OPTION_KEY, False)
        if not isinstance(normalized_options[REVEAL_AP_LOCATIONS_OPTION_KEY], bool):
            raise ValueError(
                'manifest option {value_0} must be boolean'.format(value_0=f'{REVEAL_AP_LOCATIONS_OPTION_KEY}')
            )
        normalized_options.setdefault("use_dlc_content", True)
        normalized_options.setdefault("include_dlc_missions", True)
        for key in ("use_dlc_content", "include_dlc_missions"):
            if not isinstance(normalized_options[key], bool):
                raise ValueError('manifest option {value_0} must be boolean'.format(value_0=f'{key}'))
        project_room_config(normalized_options)
        _validate_manifest_starting(normalized_options)
        return normalized_options

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
        if not static_precompile:
            return cls._create_resolved(
                seed_name=seed_name, team=team, slot=slot, options=options,
                active_location_ids=active_location_ids, placements=placements,
                static_content_digest=static_content_digest,
            )
        identity = release_identity()
        normalized_options = cls._static_manifest_options(options)
        active_ids = tuple(sorted(set(active_location_ids)))
        if not isinstance(static_precompile, bool):
            raise ValueError('static_precompile must be boolean')
        if static_content_digest and not re.fullmatch(r"[0-9a-f]{64}", static_content_digest):
            raise ValueError('static_content_digest must be an empty string or SHA-256')
        normalized_placements = ()
        required_capabilities = {
            "room_mod_v2",
            "slot_data_v6",
            "unified_campaign_v1",
            "dlc_missions_v1",
            "goal_events_v1",
            "goal_endpoint_events_v1",
            "placement_scouts_v1",
            "special_weapon_progression_v1",
            "ammo_refill_v1",
        }
        required_capabilities.add("physical_options_v1")
        required_capabilities.add("room_options_v1")
        required_capabilities.update(set(normalized_options.get("required_capabilities", ())) & {
            "fortress_economy_v1", "progressive_blood_punch_v1",
        })
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

    def _complete_placements(self):
        if self.static_precompile:
            return ()
        if not self.placements:
            raise ValueError(
                'real-room compilation requires complete placement mapping'
            )
        return _room_placements(self.options, self.placements, self.active_location_ids, self.slot)

    def require_complete_placements(self) -> tuple[PlacementRecord, ...]:
        """Return placement records required by room compilation."""
        _admit_manifest(self)
        return self._complete_placements()

    @classmethod
    def from_room(
        cls,
        snapshot: RoomSnapshot,
        known_location_ids: set[int],
        *,
        static_content_digest: str = "",
    ) -> SeedManifest:
        _normalize_slot_data(snapshot.slot_data)
        return cls._from_resolved_room(snapshot, known_location_ids, static_content_digest)


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
                'Release package is incomplete or corrupted. Re-extract the full DOOM Eternal Archipelago package.'
            )
        self.base_resource = base_resource
        self.payload_resource = payload_resource
        self.payload_manifest = load_room_payload_manifest(payload_manifest)
        self.static_material = self.compute_static_content_material(
            base_resource, payload_resource, self.payload_manifest
        )
        self.static_content_digest = hashlib.sha256(
            canonical_json(self.static_material)
        ).hexdigest()
        self.decompressor = decompressor
        self.dependency_manager = dependency_manager
        self.consent = consent

    @classmethod
    def compute_static_content_material(
        cls,
        base_resource: Path,
        payload_resource: Path,
        payload_manifest: object,
    ) -> dict[str, object]:
        import sys

        from tools.release.room_payloads import canonical_json

        compiler_identity = load_compiler_source_identity(ROOT, frozen=getattr(sys, "frozen", False))

        def _file_sha(p: Path) -> str | None:
            if p.suffix == ".py":
                return compiler_identity["source_hashes"].get(p.relative_to(ROOT).as_posix())
            return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None

        tag_decl_hashes: dict[str, str | None] = {}
        tag_dir = ROOT / "data" / "devinv_sources" / "tag"
        if tag_dir.is_dir():
            for p in sorted(tag_dir.glob("*.decl")):
                tag_decl_hashes[p.name] = _file_sha(p)

        catalog_hashes: dict[str, str | None] = {}
        catalog_dir = ROOT / "content" / "catalog"
        if catalog_dir.is_dir():
            for p in sorted(catalog_dir.glob("**/*.json")):
                catalog_hashes[p.relative_to(catalog_dir).as_posix()] = _file_sha(p)

        return {
            "schema": 2,
            "base_mod_sha256": _file_sha(base_resource),
            "room_payloads_sha256": _file_sha(payload_resource),
            "room_payload_manifest_sha256": hashlib.sha256(
                canonical_json(payload_manifest)
            ).hexdigest(),
            "conditional_members": {
                cls.ENHANCED_MELEE_PATH: hashlib.sha256(
                    cls.ENHANCED_MELEE_DECL
                ).hexdigest(),
                "fortress_battery_labels": _file_sha(cls.FORTRESS_BATTERY_LABEL_SPEC_PATH),
            },
            "compiler_sources": {
                "closure_sha256": compiler_identity["fingerprint"],
                "launcher_core": _file_sha(ROOT / "doom_eap" / "launcher" / "launcher_core.py"),
                "devinv_builder": _file_sha(ROOT / "tools" / "decls" / "devinv_builder.py"),
                "tag_prerequisites": _file_sha(ROOT / "doom_eap" / "contracts" / "tag_prerequisites.py"),
                "runtime_context": _file_sha(ROOT / "doom_eap" / "contracts" / "runtime_context.py"),
                "lifecycle": _file_sha(ROOT / "doom_eap" / "runtime" / "lifecycle.py"),
                "mission_complete_map_patcher": _file_sha(ROOT / "tools" / "maps" / "mission_complete_map_patcher.py"),
                "ap_map_generator": _file_sha(ROOT / "tools" / "maps" / "ap_map_generator.py"),
                "notification_formatting": _file_sha(ROOT / "tools" / "maps" / "notification_formatting.py"),
                "context_registry": _file_sha(ROOT / "doom_eap" / "runtime" / "context_registry.py"),
                "content_catalog": _file_sha(ROOT / "doom_eap" / "content" / "content_catalog.py"),
            },
            "devinv_sources": {
                "tag_chain": _file_sha(ROOT / "data" / "devinv_sources" / "tag_dev_inv_chain.json"),
                "map_sources": _file_sha(ROOT / "data" / "map_sources.json"),
                "devinv_start_mapping": _file_sha(ROOT / "data" / "devinv_start_mapping.json"),
                "tag_decls": tag_decl_hashes,
            },
            "content_catalog_data": catalog_hashes,
        }

    @classmethod
    def compute_static_content_digest(
        cls,
        base_resource: Path,
        payload_resource: Path,
        payload_manifest: object,
    ) -> str:
        from tools.release.room_payloads import canonical_json
        material = cls.compute_static_content_material(
            base_resource, payload_resource, payload_manifest
        )
        return hashlib.sha256(canonical_json(material)).hexdigest()

    @staticmethod
    def _cached_manifest_matches(manifest_doc, receipt_doc, manifest):
        if manifest_doc.get("manifest_hash") != manifest.manifest_hash:
            return False
        if manifest.static_content_digest and manifest_doc.get("static_content_digest") != manifest.static_content_digest:
            return False
        if manifest_doc.get("seed_name") != manifest.seed_name:
            return False
        if manifest_doc.get("slot") != manifest.slot:
            return False
        if manifest_doc.get("team") != manifest.team:
            return False
        if receipt_doc.get("manifest_hash") != manifest.manifest_hash:
            return False
        return True

    @staticmethod
    def _cached_package_matches(package_path, manifest):
        if not package_path.is_file():
            return False
        try:
            with zipfile.ZipFile(package_path) as package:
                names = set(package.namelist())
                if "seed_manifest.json" not in names or "seed_receipt.json" not in names:
                    return False
                manifest_doc = json.loads(package.read("seed_manifest.json"))
                receipt_doc = json.loads(package.read("seed_receipt.json"))
            if not isinstance(manifest_doc, dict) or not isinstance(receipt_doc, dict):
                return False
            return RoomCompiler._cached_manifest_matches(manifest_doc, receipt_doc, manifest)
        except (OSError, ValueError, KeyError, json.JSONDecodeError, zipfile.BadZipFile):
            return False

    @staticmethod
    def validate_cached_package(package_path: Path, manifest: SeedManifest) -> bool:
        """Verify that an existing generated room ZIP matches manifest and content identity."""
        _admit_manifest(manifest)
        return RoomCompiler._cached_package_matches(package_path, manifest)

    def _placement_receipt_strings(self, per_item, by_name, locale, item_receipt_text):
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
                locale=locale,
            )

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
            self._placement_receipt_strings(per_item, by_name, locale, item_receipt_text)
            for record in placements:
                sent_key = f"#str_ap_location_sent_{record.location_id}"
                by_name[sent_key] = {
                    "name": sent_key,
                    "text": location_sent_text(record.document(), locale=locale),
                }
            table["strings"] = sorted(by_name.values(), key=lambda entry: entry["name"])
            assembled[member] = (
                json.dumps(table, indent=4, ensure_ascii=False) + "\n"
            ).encode("utf-8")

    def _decode_placement_entities(self, compressed, content, decompressor, decoded, member):
        compressed.write_bytes(content)
        try:
            subprocess.run(
            [str(decompressor), "--decompress", str(compressed), str(decoded)],
            check=True,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or "").strip()[:500]
            raise ValueError(
                'could not decompress packaged entities member {value_0}: {value_1}'.format(value_0=f'{member}', value_1=f'{detail}')
            ) from error
        if not decoded.is_file():
            raise ValueError(
                'idFileDeCompressor produced no decoded entities member: {value_0}'.format(value_0=f'{member}')
            )
        try:
            text = decoded.read_bytes().decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(
                'packaged entities member is not UTF-8 after decompression: {value_0}'.format(value_0=f'{member}')
        ) from error
        return text

    def _neutralize_inactive_aliases(self, inactive_aliases, text):
        if inactive_aliases:
            from tools.maps.ap_map_generator import (
                find_entity_block_bounds,
                neutralize_conditional_pickup_block,
            )
            for check, location_id in inactive_aliases.items():
                bounds = find_entity_block_bounds(text, check)
                if bounds is not None:
                    text = text[:bounds[0]] + neutralize_conditional_pickup_block(text[slice(*bounds)]) + text[bounds[1]:]
                bounds = find_entity_block_bounds(text, f"ap_notify_location_{location_id}")
                if bounds is not None:
                    text = text[:bounds[0]] + text[bounds[1]:]
        return text

    def _remove_inactive_notifications(self, economy_enabled, notification_header, text, placement_by_id):
        if economy_enabled:
            from tools.maps.ap_map_generator import find_entity_block_bounds
            inactive = {int(match.group(2)) for match in notification_header.finditer(text)} - placement_by_id.keys()
            for location_id in sorted(inactive):
                bounds = find_entity_block_bounds(text, f"ap_notify_location_{location_id}")
                if bounds is None:
                    raise ValueError('notification entity missing: {value_0}'.format(value_0=f'{location_id}'))
                text = text[:bounds[0]] + text[bounds[1]:]
        return text

    def _append_fortress_labels(self, member, fortress_member_suffix, fortress_labels_written, economy_enabled, rewritten_text, fortress_labels):
        if member.endswith(fortress_member_suffix):
            if fortress_labels_written:
                raise ValueError('room package contains multiple Fortress hub entity members')
            if not economy_enabled:
                rewritten_text = rewritten_text.rstrip() + "\n" + fortress_labels
            fortress_labels_written = True
        return rewritten_text, fortress_labels_written

    def _encode_placement_entities(self, rewritten_text, decoded, decompressor, recompressed, member, assembled):
        rewritten = rewritten_text.encode("utf-8")
        decoded.write_bytes(rewritten)
        try:
            subprocess.run(
                [str(decompressor), "--compress", str(decoded), str(recompressed)],
                check=True,
                capture_output=True,
                text=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or "").strip()[:500]
            raise ValueError(
                'could not recompress packaged entities member {value_0}: {value_1}'.format(value_0=f'{member}', value_1=f'{detail}')
            ) from error
        if not recompressed.is_file():
            raise ValueError(
                'idFileDeCompressor produced no encoded entities member: {value_0}'.format(value_0=f'{member}')
            )
        assembled[member] = recompressed.read_bytes()
        return rewritten

    def _placement_header_rewriter(self, placement_by_id, placement_sent_key):
        def replace_header(match: re.Match[str]) -> str:
            location_id = int(match.group(2))
            if location_id not in placement_by_id:
                raise ValueError(
                    'packaged location notification lacks placement mapping: {value_0}'.format(value_0=f'{location_id}')
                )
            return (
                match.group(1)
                + placement_sent_key(location_id)
                + match.group(3)
            )
        return replace_header

    def _placement_entity_policies(self, placements, options, resolved):
        placement_by_id = {record.location_id: record for record in placements}
        from doom_eap.content.content_catalog import load_content_catalog
        inactive_aliases = {spec.ap_check: spec.location_id for spec in load_content_catalog().physical_locations
                            if spec.policy.get("legacy_alias_of") and spec.location_id not in placement_by_id}
        timer_transform = self._room_timer_transform(options or {}, resolved)
        options = self._compile_options(options or {})
        selected_groups = (options or {}).get("active_fortress_spend_group_ids")
        economy_enabled = "fortress_economy_v1" in (options or {}).get("required_capabilities", ())
        fortress_member_suffix, fortress_labels = self._fortress_battery_label_entities(
            placement_by_id, selected_groups if economy_enabled else None
        )
        fortress_labels_written = False
        return placement_by_id, inactive_aliases, timer_transform, options, selected_groups, economy_enabled, fortress_member_suffix, fortress_labels, fortress_labels_written

    def _apply_placement_entities(
        self, assembled: dict[str, bytes], placements: tuple[PlacementRecord, ...],
        options: dict | None = None, *, resolved=None,
    ) -> None:
        """Bind packaged location notifications to their room placements."""
        import re

        from tools.maps.notification_formatting import placement_sent_key

        decompressor = self._verified_decompressor()

        placement_by_id, inactive_aliases, timer_transform, options, selected_groups, economy_enabled, fortress_member_suffix, fortress_labels, fortress_labels_written = self._placement_entity_policies(placements, options, resolved)
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
                text = self._decode_placement_entities(compressed, content, decompressor, decoded, member)

                replace_header = self._placement_header_rewriter(placement_by_id, placement_sent_key)

                if "progressive_blood_punch_v1" in (options or {}).get("required_capabilities", ()):
                    from tools.maps.ap_map_generator import remove_blood_punch_grants
                    text = remove_blood_punch_grants(text)
                text = self._neutralize_inactive_aliases(inactive_aliases, text)
                text = timer_transform(text, member)
                if economy_enabled and member.endswith(fortress_member_suffix):
                    from tools.maps.fortress_campaign import project_spend_groups
                    text = text.rstrip() + "\n" + fortress_labels
                    text = project_spend_groups(text, selected_groups)
                text = self._remove_inactive_notifications(economy_enabled, notification_header, text, placement_by_id)
                rewritten_text = notification_header.sub(replace_header, text)
                rewritten_text, fortress_labels_written = self._append_fortress_labels(member, fortress_member_suffix, fortress_labels_written, economy_enabled, rewritten_text, fortress_labels)
                rewritten = self._encode_placement_entities(rewritten_text, decoded, decompressor, recompressed, member, assembled)
                LOGGER.info(
                    "ROOM_PACKAGE_MEMBER member=%s compressed_bytes=%d decoded_bytes=%d encoded_bytes=%d",
                    member[:256],
                    len(content),
                    len(rewritten),
                    len(assembled[member]),
                )
        if not fortress_labels_written:
            raise ValueError(
                'room package lacks Fortress hub entities required for Battery placement labels'
            )

    @staticmethod
    def _doom_gui_text(value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError('Fortress Battery placement text contains empty display name')
        return normalized.replace("\\", "\\\\").replace('"', '\\"')

    def _fortress_expected_locations(self, active_group_ids):
        expected = self.FORTRESS_BATTERY_LOCATION_IDS
        if active_group_ids is not None:
            from doom_eap.content.content_catalog import load_content_catalog
            policy = json.loads((ROOT / "content/catalog/fortress.json").read_text(encoding="utf-8"))
            names = {name for group in policy["spend_groups"] if group["id"] in active_group_ids
                     for name in group["locations"]}
            expected = {code for code, name in load_content_catalog().location_names.items() if name in names}
            expected.add(7770171)
        return expected

    def _fortress_label_spec(self):
        document = json.loads(self.FORTRESS_BATTERY_LABEL_SPEC_PATH.read_text(encoding="utf-8"))
        if document.get("schema_version") != 1:
            raise ValueError('Fortress Battery label specification schema is unsupported')
        member_suffix = document.get("map_member_suffix")
        primitive = document.get("primitive")
        rows = document.get("labels")
        if not isinstance(member_suffix, str) or not member_suffix.endswith(".entities"):
            raise ValueError('Fortress Battery label map member is invalid')
        if not isinstance(primitive, dict) or not isinstance(rows, list):
            raise ValueError('Fortress Battery label specification is incomplete')
        required_primitive = {
            "inherit": "gui/text",
            "class": "idGuiEntity_Text",
            "model": "editors/models/gui_text.lwo",
            "swf": "swf/guientity/generic_text.swf",
        }
        if primitive != required_primitive:
            raise ValueError('Fortress Battery label primitive contract is invalid')
        return member_suffix, rows

    @staticmethod
    def _valid_label_orientation(orientation):
        return isinstance(orientation, list) and len(orientation) == 3 and all(
            isinstance(vec, list) and len(vec) == 3 and all(isinstance(val, int | float) for val in vec)
            for vec in orientation)

    def _validate_fortress_transform(self, location_id, row, position, orientation, rows_by_id):
        if (
            not isinstance(location_id, int)
            or not isinstance(row["source_entity"], str)
            or not row["source_entity"]
            or not isinstance(position, list)
            or len(position) != 3
            or any(not isinstance(value, int | float) for value in position)
            or not self._valid_label_orientation(orientation)
            or location_id in rows_by_id
        ):
            raise ValueError('Fortress Battery label transform row is invalid')

    def _fortress_label_transforms(self, rows):
        rows_by_id: dict[int, tuple[float, float, float]] = {}
        orientations_by_id: dict[int, list[list[float]]] = {}
        for row in rows:
            if not isinstance(row, dict) or not {"location_id", "source_entity", "position"}.issubset(set(row)):
                raise ValueError('Fortress Battery label transform row is invalid')
            location_id = row["location_id"]
            position = row["position"]
            orientation = row.get("orientation", [
                [1.0, 0.0, 0.0],
                [0.0, 1.0, 0.0],
                [0.0, 0.0, 1.0],
            ])
            self._validate_fortress_transform(location_id, row, position, orientation, rows_by_id)
            rows_by_id[location_id] = tuple(float(value) for value in position)  # type: ignore[assignment]
            orientations_by_id[location_id] = [
                [float(val) for val in vec] for vec in orientation
            ]
        if set(rows_by_id) != self.FORTRESS_BATTERY_LOCATION_IDS:
            raise ValueError('Fortress placement label specification must cover exactly 13 anchors')
        return rows_by_id, orientations_by_id

    def _fortress_battery_label_entities(
        self, placement_by_id: dict[int, PlacementRecord], active_group_ids: list[str] | None = None
    ) -> tuple[str, str]:
        """Compile seed-specific pre-purchase labels for all Battery consumers."""
        from doom_eap.presentation import (
            ARCHIPELAGO_PRESENTATION_COLORS,
            color_rgb_floats,
            item_classification_color_key,
        )

        expected = self._fortress_expected_locations(active_group_ids)
        missing = sorted(expected - set(placement_by_id))
        if missing:
            raise ValueError(
                'Fortress Battery placement scout is incomplete; missing location IDs: '
                + ", ".join(str(value) for value in missing)
            )
        member_suffix, rows = self._fortress_label_spec()
        rows_by_id, orientations_by_id = self._fortress_label_transforms(rows)

        entities: list[str] = []
        for location_id in sorted(expected - {7770171}):
            record = placement_by_id[location_id]
            if record.trap:
                display = 'A TRAP\\nFOR SOMEONE'
            elif record.local:
                display = 'YOUR ' + self._doom_gui_text(record.item_name).upper()
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
            m0, m1, m2 = orientations_by_id[location_id]
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
\t\t\t\thide = false;
\t\t\t\tnoknockback = false;
\t\t\t}}
\t\t\trenderModelInfo = {{
\t\t\t\tmodel = "editors/models/gui_text.lwo";
\t\t\t\tscale = {{
\t\t\t\t\tx = 15;
\t\t\t\t\ty = 15;
\t\t\t\t\tz = 15;
\t\t\t\t}}
\t\t\t}}
\t\t\tclipModelInfo = {{
\t\t\t\ttype = "CLIPMODEL_NONE";
\t\t\t}}
\t\t\tswf = "swf/guientity/generic_text.swf";
\t\t\tspawnPosition = {{
\t\t\t\tx = {x:.6f};
\t\t\t\ty = {y:.6f};
				z = {z:.6f};
			}}
			spawnOrientation = {{
				mat = {{
					mat[0] = {{ x = {m0[0]:.6f}; y = {m0[1]:.6f}; z = {m0[2]:.6f}; }}
					mat[1] = {{ x = {m1[0]:.6f}; y = {m1[1]:.6f}; z = {m1[2]:.6f}; }}
					mat[2] = {{ x = {m2[0]:.6f}; y = {m2[1]:.6f}; z = {m2[2]:.6f}; }}
				}}
			}}
			dormancy = {{
				allowDistanceDormancy = false;
				allowDormancy = false;
				allowPvsDormancy = false;
			}}
			dynamicMoveActive = true;
			swfScale = 0.009667;
			bodyText = {{
				text = "{display}";
				color = {{
					r = {red:.6f};
					g = {green:.6f};
					b = {blue:.6f};
				}}
				relativeWidth = 1;
				alignment = "SWF_ET_ALIGN_CENTER";
			}}
		}}
	}}
}}
''')
        return member_suffix, "".join(entities)

    def _verified_decompressor(self) -> Path:
        if self.decompressor is None:
            if self.dependency_manager is None or not callable(self.consent):
                raise FileNotFoundError(
                    'idFileDeCompressor is unavailable in verified dependency cache'
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
                'idFileDeCompressor is unavailable in verified dependency cache'
            )
        actual_sha256 = hashlib.sha256(decompressor.read_bytes()).hexdigest()
        if actual_sha256 != IDFILE_DECOMPRESSOR.sha256:
            raise ValueError(
                'Cached idFileDeCompressor SHA-256 mismatch: expected {value_0}, got {value_1}'.format(value_0=f'{IDFILE_DECOMPRESSOR.sha256}', value_1=f'{actual_sha256}')
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
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )
            except subprocess.CalledProcessError as error:
                detail = (error.stderr or error.stdout or "").strip()[:500]
                raise ValueError(
                    'could not decompress compiled entities member {value_0}: {value_1}'.format(value_0=f'{member}', value_1=f'{detail}')
                ) from error
            if not decoded.is_file():
                raise ValueError(
                    'idFileDeCompressor produced no decoded entities member: {value_0}'.format(value_0=f'{member}')
                )
            try:
                return decoded.read_bytes().decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(
                    'compiled entities member is not UTF-8 after decompression: {value_0}'.format(value_0=f'{member}')
                ) from error

    def _compress_entities_text(self, text: str) -> bytes:
        decompressor = self._verified_decompressor()
        with tempfile.TemporaryDirectory(prefix="doom-ap-overlay-") as temporary:
            temporary_dir = Path(temporary)
            decoded = temporary_dir / "decoded.entities"
            compressed = temporary_dir / "compressed.entities"
            decoded.write_text(text, encoding="utf-8", newline="\n")
            try:
                subprocess.run(
                    [str(decompressor), "--compress", str(decoded), str(compressed)],
                    check=True,
                    capture_output=True,
                    text=True,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )
            except subprocess.CalledProcessError as error:
                detail = (error.stderr or error.stdout or "").strip()[:500]
                raise ValueError('could not compress generated entity overlay: {value_0}'.format(value_0=f'{detail}')) from error
            if not compressed.is_file():
                raise ValueError('idFileDeCompressor produced no encoded overlay')
            return compressed.read_bytes()

    @staticmethod
    def _resolved_enemy_plan(manifest, *, resolved=None):
        from doom_eap.runtime.observers import load_slot_catalog
        from tools.maps.native_recipe_materializer import expand_resolved_native_recipes

        return expand_resolved_native_recipes(resolved if resolved is not None else admit_slot_data(manifest.options), load_slot_catalog())

    def _compile_enemy_content(self, assembled, manifest, plan=None):
        from tools.maps.native_recipe_materializer import RecipeMaterializationError

        try:
            return self._apply_enemy_plan(assembled, manifest, plan=plan)
        except (ValueError, KeyError, TypeError, OSError) as error:
            if isinstance(error, RecipeMaterializationError | SlotAdmissionError):
                raise
            raise RecipeMaterializationError('recipe_materialization', str(error)) from error

    def _room_timer_transform(self, options, resolved=None):
        from functools import partial

        from doom_eap.runtime.observers import load_slot_catalog
        from tools.maps.native_recipe_materializer import materialize_resolved_timers

        return partial(materialize_resolved_timers, slot=resolved if resolved is not None else admit_slot_data(options), catalog=load_slot_catalog())

    def _compile_options(self, options):
        data = options
        runtime = data['runtime']
        from tools.release.room_payloads import dlc_mission_map_keys

        items = json.loads((ROOT / 'data/item_replay_policies.json').read_bytes())['items']
        return {**runtime, **runtime['checks'],
            'include_dlc_missions': bool(dlc_mission_map_keys().intersection(data['recipes']['maps'])),
            'death_link': runtime['death_link']['enabled'], 'death_link_mode': runtime['death_link']['mode'],
            'campaign_plan': {'sequence': data['campaign']['sequence']},
            'active_fortress_spend_group_ids': data['campaign']['fortress']['active_spend_group_ids'],
            'required_capabilities': sorted(set(data['compatibility']['required_capabilities']) | RESOLVED_ROOM_CAPABILITIES),
            'starting_inventory': {items[str(code)]['name']: count for code, count in data['starting']['items']
                                   if code != data['starting']['weapon_item_id']},
            'starting_weapon': items[str(data['starting']['weapon_item_id'])]['name'],
            'mastery_requirements': 'Original'}

    def _assemble_room_files(self, manifest, options):
        from tools.release.room_payloads import assemble_room_files

        assembled, selected = assemble_room_files(
            self.base_resource, self.payload_resource, self.payload_manifest, options)
        from doom_eap.content.mastery_requirements import patch_mastery_decl
        from doom_eap.content.slot_data_contract import catalog_tables
        from doom_eap.runtime.observers import load_slot_catalog

        masteries = catalog_tables(load_slot_catalog())['masteries']
        for identity, count in manifest.options['runtime']['mastery_requirement_counts'].items():
            entry = masteries[identity]
            entry = {**entry, 'signal': {**entry['signal'], 'rule_0_statCount': count}}
            member = 'gameresources/generated/decls/' + entry['decls']['unlockable']['path']
            assembled[member] = patch_mastery_decl(assembled[member].decode('utf-8'), entry).encode('utf-8')
        return assembled, selected

    def _enemy_required_sources(self, changed_maps):
        required_sources = set(changed_maps)
        try:
            for row in changed_maps.values():
                required_sources.update(body["ai"]["map_key"] for recipe in
                    [*row["recipes"], *row.get('compatibility_recipes', []), *row.get('counter_recipes', [])]
                    for body in recipe["bodies"])
        except (KeyError, TypeError) as error:
            raise ValueError('Enemy generation source references are malformed') from error
        return required_sources

    def _frozen_body_assets(self, references, precache, sources, find_entity_block_bounds):
        roots, assets = set(), {}
        for map_key, entity, sha in sorted(references):
            profile = precache.get(sha)
            if profile is None or profile["source"] != {"map_key": map_key, "entity": entity, "sha256": sha}:
                raise ValueError('Native enemy resource catalog lacks frozen body: {value_0}/{value_1}'.format(value_0=f'{map_key}', value_1=f'{entity}'))
            for entry in profile["assets"]:
                assets.setdefault((entry["mapResourceType"], entry["name"]), None)
            source = sources[map_key]
            start, end = find_entity_block_bounds(source, entity)
            roots.update(json.loads(match[0]) for match in re.finditer(r'"(?:[^"\\]|\\.)*"', source[start:end]))
        return roots, assets

    def _enemy_resource_assets(self, changed_maps, precache, sources, find_entity_block_bounds):
        map_assets = {}
        resource_roots = {}
        for key, row in changed_maps.items():
            references = {tuple(body["ai"][field] for field in ("map_key", "entity", "sha256"))
                          for recipe in [*row["recipes"], *row.get('compatibility_recipes', []), *row.get('counter_recipes', [])]
                          for body in recipe["bodies"]}
            roots, assets = self._frozen_body_assets(references, precache, sources, find_entity_block_bounds)
            roots.update(name for _, name in assets)
            map_assets[key] = assets
            resource_roots[row["runtime_map"]] = sorted(roots - {""})
        return map_assets, resource_roots

    def _materialize_enemy_maps(self, changed_maps, assembled, sources, plan, spawn_diagnostics, staged, compiler, neutral_manifest, patch_mission_complete_maps, materialize_native_recipe_calls, map_assets):
        for map_key, row in sorted(changed_maps.items()):
            if map_key not in self.payload_manifest["maps"]:
                raise ValueError('Enemy generation map is not packaged: {value_0}'.format(value_0=f'{map_key}'))
            member = self.payload_manifest["maps"][map_key]["target_member"]
            if member not in assembled:
                raise ValueError('Enemy generation map is not active: {value_0}'.format(value_0=f'{map_key}'))
            materialized = materialize_native_recipe_calls(sources[map_key], map_key, plan, sources,
                enemy_revision=9, diagnostics=spawn_diagnostics)
            source_path = staged / f"{map_key}.source"
            output_path = staged / f"{map_key}.entities"
            source_path.write_bytes(materialized.encode("utf-8"))
            compiler.compile_map(neutral_manifest, source_path, output_path, map_key,
                                 encounter_source=sources[map_key], write_seed_sidecar=False)
            audit = patch_mission_complete_maps(ROOT / "data/mission_complete_map_contracts.json",
                                                 {map_key: output_path}, staged / "publishers",
                                                 source_maps=sources, retained_overrides=assembled)
            if audit["unrelated_generated_entity_diff_count"]:
                raise ValueError('Enemy map publisher patch changed unrelated entities: {value_0}'.format(value_0=f'{map_key}'))
            assembled[member] = self._compress_entities_text(output_path.read_bytes().decode("utf-8"))
            assets_path = member.split('/', 1)[0] + "/EternalMod/assetsinfo/" + row["runtime_map"].rsplit("/", 1)[1] + ".json"
            assets = json.loads(assembled.get(assets_path, b"{}"))
            entries = assets.setdefault("assets", [])
            existing = {(entry["mapResourceType"], entry["name"]) for entry in entries}
            # binary dependencies need their own native cache entries before model preparation.
            entries.extend({"name": name, "mapResourceType": kind}
                           for kind, name in map_assets[map_key] if (kind, name) not in existing)
            assembled[assets_path] = (json.dumps(assets, sort_keys=True) + "\n").encode("utf-8")

    def _apply_enemy_plan(self, assembled: dict[str, bytes], manifest: SeedManifest, *, plan=None) -> dict[str, list[str]]:
        from dataclasses import replace

        from tools.maps.mission_complete_map_patcher import patch_mission_complete_maps
        from tools.maps.native_recipe_materializer import (
            MAX_POSITION_DIAGNOSTICS,
            MAX_POSITION_DIAGNOSTICS_PER_MAP,
            materialize_native_recipe_calls,
        )
        from tools.release.room_payloads import read_source_snapshots

        if plan is None:
            plan = self._resolved_enemy_plan(manifest)
        if plan is None:
            return {}
        from tools.maps.arena_checks import room_compile_maps
        changed_maps = room_compile_maps(plan["maps"], manifest.active_location_ids, ROOT)
        if not changed_maps:
            return {}
        required_sources = self._enemy_required_sources(changed_maps)
        sources = read_source_snapshots(self.payload_resource, self.payload_manifest, required_sources)
        from tools.maps.ap_map_generator import find_entity_block_bounds
        precache = json.loads((ROOT / "content/catalog/native_enemy_assets.json").read_bytes())["bodies"]
        map_assets, resource_roots = self._enemy_resource_assets(changed_maps, precache, sources, find_entity_block_bounds)
        compiler = ModCompiler(ROOT)
        neutral_manifest = replace(manifest, options=self._compile_options(manifest.options), static_precompile=True)
        spawn_diagnostics = []
        with tempfile.TemporaryDirectory(prefix="doom-ap-enemies-") as temporary:
            staged = Path(temporary)
            self._materialize_enemy_maps(changed_maps, assembled, sources, plan, spawn_diagnostics, staged, compiler, neutral_manifest, patch_mission_complete_maps, materialize_native_recipe_calls, map_assets)
        assembled['enemy_spawn_diagnostics.json'] = (json.dumps({
            'positions': spawn_diagnostics, 'limit': MAX_POSITION_DIAGNOSTICS,
            'limit_per_map': MAX_POSITION_DIAGNOSTICS_PER_MAP,
            'limit_reached': len(spawn_diagnostics) >= MAX_POSITION_DIAGNOSTICS,
        }, sort_keys=True) + '\n').encode('utf8')
        return resource_roots

    def _boon_map_resources(self, manifest, assembled, resource_roots, roots, entries):
        for key in manifest.options['recipes']['maps']:
            row = self.payload_manifest['maps'].get(key)
            if row is None or row['target_member'] not in assembled:
                continue
            member = row['target_member']
            runtime = member.split('/maps/', 1)[1].removesuffix('.entities')
            resource_roots[runtime] = sorted(set(resource_roots.get(runtime, ())) | roots)
            asset_path = member.split('/', 1)[0] + '/EternalMod/assetsinfo/' + runtime.rsplit('/', 1)[1] + '.json'
            document = json.loads(assembled.get(asset_path, b'{}'))
            assets = document.setdefault('assets', [])
            existing = {(entry['mapResourceType'], entry['name']) for entry in assets}
            assets.extend(dict(mapResourceType=kind, name=name) for kind, name in entries if (kind, name) not in existing)
            assembled[asset_path] = (json.dumps(document, sort_keys=True) + '\n').encode('utf-8')

    def _apply_boon_resources(self, assembled, manifest, resource_roots):
        profiles = json.loads((ROOT / "content/catalog/native_enemy_assets.json").read_bytes()).get('powerups', {})
        if set(profiles) != {'berserk', 'onslaught', 'overdrive'}:
            raise ValueError('Powerup resource catalog is incomplete')
        entries = dict.fromkeys((entry['mapResourceType'], entry['name'])
                                for profile in profiles.values() for entry in profile['assets'])
        roots = {name for _, name in entries} | {f'pickup/powerup/{effect}' for effect in profiles}
        self._boon_map_resources(manifest, assembled, resource_roots, roots, entries)

    def _room_loadout_resources(self, options, manifest, assembled):
        devinv_path = output_path_for_map(
            Path("."), ROOT / "data" / "map_sources.json", self.DEVINV_MAP_KEY
        ).as_posix()
        devinv_source = build_devinv_loadout(
            options.get("starting_inventory", {}),
            options.get("starting_weapon"),
            starting=manifest.options.get("starting"),
        )
        assembled[devinv_path] = devinv_source.encode("utf-8")
        # Core redirects NewGame from sp/e1m1 to Fortress, which needs the same room loadout.
        hub_devinv_path = output_path_for_map(
            Path("."), ROOT / "data" / "map_sources.json", "hub"
        ).as_posix()
        assembled[hub_devinv_path] = devinv_source.encode("utf-8")
        # Mars Core's inherited replay loadout grants weapons outside AP ownership.
        assembled["e2m3_core/generated/decls/devinvloadout/devinvloadout/sp/e2m3.decl"] = (
            ''.join(('{{', '\n', '', '\t', 'edit = {{', '\n', '', '\t', '', '\t', 'clearAllBeforeApply = false;', '\n', '', '\t', '', '\t', 'startingInventory = {{ num = 0; }}', '\n', '', '\t', '', '\t', 'currencyToGive = {{ num = 0; }}', '\n', '', '\t', '}}', '\n', '}}', '\n', ''))
        ).encode("utf-8")
        from tools.decls.campaign_builder import build_campaign_overrides
        holt_member = "gameresources_patch2/generated/decls/campaign/campaign/dlc1.decl"
        if hashlib.sha256(assembled[holt_member]).hexdigest() != \
                "4475c7dc6a745d5ed26d9bfe40b7536e6b90afe6a7536e7df998951fc776ee19":
            raise ValueError('credits overlay drifted')
        assembled.update(build_campaign_overrides(
            assembled["hub_patch2/EternalMod/assetsinfo/hub.json"],
            skip_dlc1_credits=True,
            skip_dlc2_credits=True,
        ))
        return devinv_path, devinv_source

    def _room_dlc_publishers(self, dlc_contexts, catalog, assembled, patch_generated_map_text):
        for context in dlc_contexts():
            if len(context.runtime_maps) != 1 or len(context.map_keys) != 1:
                raise ValueError('DLC context overlay requires one exact map: {value_0}'.format(value_0=f'{context.identity}'))
            overlay_member = self.payload_manifest["context_targets"].get(context.identity)
            if not isinstance(overlay_member, str):
                raise ValueError('DLC context lacks room payload target: {value_0}'.format(value_0=f'{context.identity}'))
            map_key = context.map_keys[0]
            spec = catalog.maps.get(map_key)
            if spec is None:
                raise ValueError('DLC context map spec missing: {value_0}'.format(value_0=f'{map_key}'))
            member = overlay_member
            compiled_content = assembled.get(member)
            if compiled_content is None:
                raise ValueError('DLC map compiled payload is missing: {value_0}/{value_1}'.format(value_0=f'{map_key}', value_1=f'{member}'))
            compiled_text = self._decompress_entities_text(compiled_content, member)
            if "idWorldspawn" not in compiled_text:
                raise ValueError('DLC map compiled payload is incomplete: {value_0}/{value_1}'.format(value_0=f'{map_key}', value_1=f'{member}'))
            patched_text, _publisher_audit = patch_generated_map_text(
                map_key, compiled_text, ROOT
            )
            if patched_text != compiled_text:
                assembled[member] = self._compress_entities_text(patched_text)

    def _room_dlc_resources(self, options, manifest, assembled):
        if options.get("include_dlc_missions", True):
            assembled.update({
                path: source.encode("utf-8")
                for path, source in build_tag_devinv_overrides(
                    options.get("starting_inventory", {}),
                    options.get("starting_weapon"),
                    starting=manifest.options.get("starting"),
                    progressive_blood_punch="progressive_blood_punch_v1" in options.get("required_capabilities", ()),
                ).items()
            })
            from doom_eap.content.content_catalog import load_content_catalog
            from doom_eap.runtime.observers import dlc_contexts
            from tools.maps.mission_complete_map_patcher import patch_generated_map_text

            catalog = load_content_catalog()
            self._room_dlc_publishers(dlc_contexts, catalog, assembled, patch_generated_map_text)

    def _publish_room_package(self, destination, assembled, write_deterministic_zip):
        incoming = destination.with_name(f".{destination.name}.incoming")
        try:
            write_deterministic_zip(assembled, incoming)
            publish_file(incoming, destination, operation="room_package_publish")
        finally:
            try:
                incoming.unlink()
            except OSError:
                pass

    def _verify_room_package(self, destination, assembled, seed_document, room_config, receipt, devinv_path, devinv_source, options):
        with zipfile.ZipFile(destination) as output:
            expected = set(assembled)
            if set(output.namelist()) != expected:
                raise ValueError('assembled room package member set drifted')
            if json.loads(output.read("seed_manifest.json")) != seed_document:
                raise ValueError('assembled room manifest validation failed')
            if json.loads(output.read("room_config.json")) != room_config:
                raise ValueError('assembled room config validation failed')
            if json.loads(output.read("seed_receipt.json")) != receipt:
                raise ValueError('assembled room receipt validation failed')
            if devinv_path is not None and devinv_source is not None and output.read(devinv_path) != devinv_source.encode("utf-8"):
                raise ValueError('assembled DevInv validation failed')
            if (self.ENHANCED_MELEE_PATH in output.namelist()) != bool(
                options.get("enhanced_melee_damage", False)
            ):
                raise ValueError('assembled Enhanced Melee Damage validation failed')

    def build(self, manifest: SeedManifest, output_root: Path, *, force: bool = False) -> Path:
        from tools.release.room_payloads import canonical_json, write_deterministic_zip
        resolved = _admit_manifest(manifest)
        placements = manifest._complete_placements()
        if resolved is None:
            resolved = admit_slot_data(manifest.options)
        plan = self._resolved_enemy_plan(manifest, resolved=resolved)
        options = self._compile_options(manifest.options)
        if manifest.static_content_digest != self.static_content_digest:
            raise ValueError('room static content identity drifted')
        output_root.mkdir(parents=True, exist_ok=True)
        destination = output_root / f"DoomEternalArchipelago-{manifest.manifest_hash[:16]}.zip"
        if not force and self._cached_package_matches(destination, manifest):
            return destination
        assembled, selected = self._assemble_room_files(manifest, options)
        resource_roots = self._compile_enemy_content(assembled, manifest, plan)
        self._apply_boon_resources(assembled, manifest, resource_roots)
        seed_document = json.loads(canonical_json(manifest.document()))
        room_config = project_room_config(options)
        receipt = {
            "schema": 1,
            "manifest_hash": manifest.manifest_hash,
            "physical_options": {key: options[key] for key in PHYSICAL_OPTION_KEYS},
            "map_payloads": selected,
            "starting_inventory": options.get("starting_inventory", {}),
            "starting_weapon": options.get("starting_weapon"),
            "room_config": room_config,
            "native_resource_roots": resource_roots,
        }
        devinv_path, devinv_source = self._room_loadout_resources(options, manifest, assembled)
        self._room_dlc_resources(options, manifest, assembled)
        assembled.update({
            "room_config.json": canonical_json(room_config),
            "seed_manifest.json": canonical_json(seed_document),
            "seed_receipt.json": canonical_json(receipt),
        })
        if options.get("enhanced_melee_damage", False):
            assembled[self.ENHANCED_MELEE_PATH] = self.ENHANCED_MELEE_DECL
        if not manifest.static_precompile:
            self._apply_placement_entities(assembled, placements, manifest.options, resolved=resolved)
            self._apply_placement_strings(assembled, placements)
        self._publish_room_package(destination, assembled, write_deterministic_zip)
        self._verify_room_package(destination, assembled, seed_document, room_config, receipt, devinv_path, devinv_source, options)
        return destination


class ModCompiler:
    def __init__(self, root: Path = ROOT):
        self.root = root

    def _enabled_location_ids(self, ids, physical_ids, options, names):
        active = [
            location_id for location_id in ids
            if location_id not in (physical_ids - physical_location_ids(options))
            and (options.get("include_slayer_gates", True) or not names[str(location_id)].endswith(" - Slayer Gate Complete"))
            and (options.get("include_secret_encounters", True) or " - Secret Encounter - " not in names[str(location_id)])
            and (options.get("include_slayer_gates", True) or names[str(location_id)] != "Fortress of Doom - Unmaykr Acquired")
        ]
        return active

    def active_location_ids(self, options: dict[str, Any]) -> list[int]:
        from doom_eap.content.arena_checks import inactive_arena_ids
        names = json.loads((self.root / "data" / "location_names.json").read_text(encoding="utf-8"))["locations"]
        ids = sorted(int(location_id) for location_id in names)
        ids = sorted(set(ids) - inactive_arena_ids(self.root, options.get("arena_checks", 1)))
        physical_ids = {
            int(spec["location_id"])
            for spec in PHYSICAL_OPTIONS.values()
        }
        active = self._enabled_location_ids(ids, physical_ids, options, names)
        if "killsanity" in options:
            active.extend(milestone_ids(options["killsanity"]))
        return sorted(active)

    def known_location_ids(self) -> set[int]:
        return set(self.active_location_ids({**{key: True for key in PHYSICAL_OPTION_KEYS}, "arena_checks": 0}))

    def project_map_config(self, manifest: SeedManifest, map_key: str) -> dict[str, Any]:
        """Project supported campaign config for one map-local transform state."""
        from doom_eap.content.content_catalog import load_content_catalog

        campaign_keys = {
            spec.key for spec in load_content_catalog(self.root).compiler_maps()
            if spec.key != "hub"
            and (manifest.options.get("include_dlc_missions", True) or not spec.requires_dlc_content)
        }
        if map_key not in campaign_keys:
            raise ValueError(
                'map-local room transforms are unsupported for map: {value_0}'.format(value_0=f'{map_key}')
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
        from doom_eap.content.arena_checks import load_arena_checks

        entry = load_arena_checks(self.root).get(map_key, {})
        config["arena_checks"] = [arena for arena in entry.get("arenas", [])
                                  if arena["location_id"] in manifest.active_location_ids]
        return project_map_config(config, manifest.options)

    def compile_map(self, manifest: SeedManifest, vanilla_entities: Path, output_entities: Path,
                    map_key: str = "e1m2_war", *, encounter_source: str | None = None,
                    write_seed_sidecar: bool = True) -> Path:
        """Compile one supported campaign map with map-local room transforms."""
        from doom_eap.content.item_classification import load_item_classifications
        from doom_eap.runtime.inventory import load_policy_registry
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
            , newline="\n")
            generate_map(
                vanilla_entities,
                output_entities,
                config_path,
                output_entities.with_suffix(".manifest.json"),
                item_definitions,
                encounter_source=encounter_source,
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
        if write_seed_sidecar:
            output_entities.with_suffix(".seed.json").write_text(
                json.dumps(manifest.document(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n")
        return output_entities


class LaunchWorkflow:
    def __init__(self, compiler: ModCompiler | None = None):
        self.compiler = compiler or ModCompiler()

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

    @staticmethod
    def client_config_path(client_dir: Path) -> Path:
        """Resolve config beside native ap_client."""
        candidate = Path(client_dir)
        if candidate.is_file() or candidate.name.casefold() in {"ap_client", "ap_client.exe"}:
            candidate = candidate.parent
        return candidate / "ap_config.json"

    @staticmethod
    def _validate_steam_account(configured, inferred):
        if configured is not None:
            if isinstance(configured, bool) or not isinstance(configured, int | str):
                raise ValueError('The Steam save account could not be identified. Select the correct account or select Help -> SAVE SUPPORT REPORT.')
            try:
                configured_id = int(configured)
            except ValueError as error:
                raise ValueError('The Steam save account could not be identified. Select the correct account or select Help -> SAVE SUPPORT REPORT.') from error
            if configured_id <= 0 or configured_id != inferred:
                raise ValueError(
                    'The Steam save account could not be identified. Select the correct account or select Help -> SAVE SUPPORT REPORT.'
                )

    @staticmethod
    def _steam_id3(steam_remote_dir: object, configured: object = None) -> int:
        remote = Path(str(steam_remote_dir)).expanduser()
        if (
            remote.name.casefold() != "remote"
            or remote.parent.name != "782330"
            or remote.parent.parent.parent.name.casefold() != "userdata"
        ):
            raise ValueError(
                'The Steam save account could not be identified. Select the correct account or select Help -> SAVE SUPPORT REPORT.'
            )
        account = remote.parent.parent.name
        if not account.isascii() or not account.isdigit() or int(account) <= 0:
            raise ValueError(
                'The Steam save account could not be identified. Select the correct account or select Help -> SAVE SUPPORT REPORT.'
            )
        inferred = int(account)
        LaunchWorkflow._validate_steam_account(configured, inferred)
        return inferred

    @staticmethod
    def _update_client_config(config, runtime_config, endpoint, manifest_hash):
        previous_base = config.get("doom_base_dir")
        source = dict(runtime_config or {})
        for key in CLIENT_CONFIG_FIELDS - {"steam_id3", "steam_remote_dir"}:
            value = source.get(key)
            if key == "server_address" and endpoint is not None:
                value = endpoint
            elif key == "seed_manifest_hash" and manifest_hash is not None:
                value = manifest_hash
            if value is not None:
                config[key] = value
            elif runtime_config is not None and key in {"core_runtime_manifest", "selected_core_runtime_manifest", "proton_executable", "proton_compat_data_dir"}:
                config.pop(key, None)

        LaunchWorkflow._client_steam_config(config, source)
        config.pop("password", None)
        return previous_base

    @staticmethod
    def write_client_config(
        client_dir: Path,
        *,
        endpoint: str | None = None,
        manifest_hash: str | None = None,
        runtime_config: Mapping[str, object] | None = None,
        journal=None,
    ) -> Path:
        """Atomically materialize native runtime config from nonsecret launcher state."""
        path = LaunchWorkflow.client_config_path(client_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        config = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(config, dict):
            raise ValueError('The game connection settings could not be read. Select Help -> SAVE SUPPORT REPORT.')

        previous_base = LaunchWorkflow._update_client_config(config, runtime_config, endpoint, manifest_hash)

        if journal is not None:
            journal.replace(path, (json.dumps(config, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                            operation="client_config_publish")
            return path

        with client_config_write((previous_base, config.get("doom_base_dir"))):
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
                publish_file(temporary_name, path, operation="client_config_publish")
            finally:
                if temporary_name is not None:
                    Path(temporary_name).unlink(missing_ok=True)
            return path

    @staticmethod
    def _client_steam_config(config, source):
        remote = source.get("steam_remote_dir") or config.get("steam_remote_dir")
        configured_id = source.get("steam_id3")
        if configured_id is None and not source.get("steam_remote_dir"):
            configured_id = config.get("steam_id3")
        if remote:
            config["steam_remote_dir"] = str(Path(str(remote)).expanduser())
            config["steam_id3"] = LaunchWorkflow._steam_id3(config["steam_remote_dir"], configured_id)
