"""Resolved room data shared by generation, admission and tracking."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, is_dataclass
from functools import cache
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Annotated, Literal, Union, get_args, get_origin, get_type_hints

from .fortress_economy import battery_surplus_limit

__all__ = (
    "MAX_PAYLOAD_BYTES", "MAX_JSON_NODES", "MAX_JSON_DEPTH", "MAX_COLLECTION", "MAX_REFERENCE_ENTRIES",
    "MAX_LIMIT_SETS", "MAX_PROFILES", "MAX_PATTERNS", "MAX_EXPANDED_BODIES", "MAX_KILL_REWARDS",
    "ContractError", "StructureError", "ReferenceError", "DigestError", "ExpansionError", "PayloadSizeError",
    "Identity", "Compatibility", "Goal", "BatteryEconomy", "Fortress", "Campaign", "Starting", "Difficulty",
    "DeathLink", "Checks", "Runtime", "Population", "LimitSet", "RecipeMap", "Recipes", "StageProfile",
    "Killsanity", "SlotData", "Profile", "Run", "RecipeRow", "Pair", "TrackerValue", "canonical_bytes", "catalog_digest",
    "recipe_digest", "placement_digest", "generation_fingerprint", "load_slot_catalog", "validate_slot_data",
    "CATALOG_DEPENDENCIES", "SlotCatalog", "catalog_tables", "source_reference", "source_index",
    "native_source_rows", "catalog_target_rows", "dependency_digest", "catalog_dependency_path", "installed_catalog_tables",
)

# compact uncompressed JSON stays below half the 16 MiB message allowance.
MAX_PAYLOAD_BYTES = 8_000_000
# decoded limits leave margin above 523,547 nodes, 40,630 profiles and 44,737 bodies.
MAX_JSON_NODES = 2_000_000
MAX_JSON_DEPTH = 16
MAX_COLLECTION = 131_072
# table ceilings cover 23,745 sources, 671 limit sets, 40,630 profiles and 4,754 patterns.
MAX_REFERENCE_ENTRIES = 65_536
MAX_LIMIT_SETS = 4_096
MAX_PROFILES = 65_536
MAX_PATTERNS = 16_384
MAX_EXPANDED_BODIES = 200_000
MAX_KILL_REWARDS = 2_000
Integer = Annotated[int, 0, 0x7fffffff]
Positive = Annotated[int, 1, 0x7fffffff]
Address = Annotated[int, 1, 0x7fffffffffffffff]
KillCount = Annotated[int, 0, 0xffffffffffffffff]
Attribute = Annotated[int, 1, 100_000]
Key = Annotated[str, 1, 96]
SourceRef = Annotated[str, 16, 16, "[0-9a-f]{16}"]
Profile = tuple[Key, Attribute, Attribute, Attribute, Attribute]
Run = tuple[Integer, Positive]
RecipeRow = tuple[SourceRef, Key, Key, Integer, Positive, Integer, Integer, bool, Integer, Positive,
                  tuple[Integer, ...]]
Pair = tuple[Integer, Positive]
SourceCount = tuple[SourceRef, Positive]
TrackerValue = Annotated[int, -0x80000000, 0x7fffffff] | tuple[str, ...] | Mapping[str, Integer]


class ContractError(ValueError):
    """Room data cannot be used safely."""


class StructureError(ContractError):
    """A field has an invalid shape or value."""


class ReferenceError(ContractError):
    """An identity is absent or repeated."""


class DigestError(ContractError):
    """The data does not match its digest."""


class ExpansionError(ContractError):
    """Recipe expansion exceeds its limit."""


class PayloadSizeError(ContractError):
    def __init__(self, total_bytes: int, recipe_bytes: int):
        self.total_bytes = total_bytes
        self.recipe_bytes = recipe_bytes
        super().__init__(f"Room data uses {total_bytes} bytes (recipes: {recipe_bytes}); ceiling is {MAX_PAYLOAD_BYTES} bytes")


@dataclass(frozen=True)
class Identity:
    schema: Literal[7]
    algorithm_revision: Positive
    catalog_digest: str
    recipe_digest: str


@dataclass(frozen=True)
class Compatibility:
    apworld_version: str
    minimum_client_version: str
    bridge_protocol: Positive
    manifest_schema_version: Positive
    mod_contract_revision: Positive
    required_capabilities: tuple[str, ...]


@dataclass(frozen=True)
class Goal:
    kind: str
    stage_id: str | None
    count: Integer
    endpoint_event: str
    additional_requirements: tuple[str, ...]


@dataclass(frozen=True)
class BatteryEconomy:
    cost: Integer
    surplus: Integer
    total: Integer
    surplus_percent: Annotated[int, 0, 100]
    native_first_battery: bool


@dataclass(frozen=True)
class Fortress:
    active_spend_group_ids: tuple[str, ...]
    phase_thresholds: tuple[Integer, ...]
    battery_economy: BatteryEconomy


@dataclass(frozen=True)
class Campaign:
    mode: str
    sequence: tuple[str, ...]
    starting_stages: tuple[str, ...]
    unprepared_start_stages: tuple[str, ...]
    opening_stage: str | None
    master_level_stage_ids: tuple[str, ...]
    access_items: tuple[Integer, ...]
    goal_as_item: bool
    active_masteries_count: Integer
    fixed_dash_completion_stage: str | None
    goal: Goal
    fortress: Fortress


@dataclass(frozen=True)
class Starting:
    items: tuple[Pair, ...]
    weapon_item_id: Integer


@dataclass(frozen=True)
class Difficulty:
    engine: Annotated[int, 0, 4]
    death_policy: Literal["normal", "restart_stage"]


@dataclass(frozen=True)
class DeathLink:
    enabled: bool
    mode: Literal["soft", "hardcore"]


@dataclass(frozen=True)
class Checks:
    include_slayer_gates: bool
    include_secret_encounters: bool
    include_weapon_mastery_challenges: bool


@dataclass(frozen=True)
class Runtime:
    difficulty: Difficulty
    death_link: DeathLink
    use_dlc_content: bool
    randomize_chainsaw: bool
    randomize_dash: bool
    randomize_first_battery: bool
    enhanced_melee_damage: bool
    easier_secret_encounters: bool
    reveal_ap_locations_on_automap: bool
    checks: Checks
    special_weapon: str
    mastery_requirement_counts: Mapping[str, Positive]


@dataclass(frozen=True)
class Population:
    scope: str
    maximum: Positive
    heavy: Integer
    superheavy: Integer
    pressure: Integer
    mixed: Annotated[int, 0, 1]
    strict: bool
    counter: Key | None
    species_caps: tuple[tuple[Key, Integer], ...]


@dataclass(frozen=True)
class LimitSet:
    special: tuple[Integer, Integer, Integer, Integer]
    population: Population | None


@dataclass(frozen=True)
class RecipeMap:
    rows: tuple[RecipeRow, ...]
    passthrough_sources: tuple[SourceRef, ...]
    disabled_sources: tuple[SourceRef, ...]


@dataclass(frozen=True)
class Recipes:
    enabled: bool
    limit_sets: tuple[LimitSet, ...]
    profiles: tuple[Profile, ...]
    patterns: tuple[tuple[Run, ...], ...]
    maps: Mapping[str, RecipeMap]
    timers: tuple[SourceCount, ...]


@dataclass(frozen=True)
class StageProfile:
    cr_min: Integer
    cr_max: Integer
    opening_cr_min: Integer | None
    opening_cr_max: Integer | None
    counter_ids: tuple[Key, ...]
    opening_counter_ids: tuple[Key, ...]
    renewable_ammo: bool
    native_chainsaw_before_combat: bool
    skulls: Integer
    effort_per_check_milli: Integer


@dataclass(frozen=True)
class Killsanity:
    interval: Integer
    count: Annotated[int, 0, MAX_KILL_REWARDS]
    finite_supported_count: Annotated[int, 0, MAX_KILL_REWARDS]
    finite_kills: KillCount
    estimated_kills: KillCount
    sources: tuple[tuple[SourceRef, Annotated[int, 1, 0xffffffffffffffff]], ...]


@dataclass(frozen=True)
class SlotData:
    identity: Identity
    compatibility: Compatibility
    campaign: Campaign
    starting: Starting
    runtime: Runtime
    recipes: Recipes
    stage_profiles: Mapping[str, StageProfile]
    killsanity: Killsanity
    tracker_options: Mapping[str, TrackerValue]
    placement_digest: str
    native_generation_fingerprint: str


def canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, RecursionError) as error:
        raise StructureError(f"Cannot serialize room data: {error}") from error


def catalog_digest(document: dict) -> str:
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


CATALOG_DEPENDENCIES = (
    "native_enemy_catalog.json", "enemy_species.json", "native_enemy_assets.json",
    "campaign_stage_ids.json", "campaign_kill_sources.json", "secret_encounter_timers.json",
)


class SlotCatalog(dict):
    """The serialized document and its installed reference tables."""

    def __init__(self, document, tables):
        super().__init__(document)
        self.tables = {**document, **tables}


def catalog_tables(catalog):
    return catalog.tables if isinstance(catalog, SlotCatalog) else catalog


def source_reference(stage_id, source_id, role):
    """Hash the canonical JSON string [map key, source id, role]."""
    return hashlib.sha256(canonical_bytes([stage_id, source_id, role])).hexdigest()[:16]


def source_index(sources):
    index = {}
    for row in sources:
        ref = source_reference(row['stage_id'], row['source_id'], row.get('role', 'primary'))
        _require(ref not in index, f"Source hash collision or duplicate: {ref}", ReferenceError)
        index[ref] = row
    return index


def native_source_rows(native, roles):
    return [dict(stage_id=key, source_id=row['id'], role=role)
            for key, stage in native['maps'].items()
            for row in stage['origins'] + stage.get('runtime_origins', []) for role in roles]


def dependency_digest(payload):
    return hashlib.sha256(payload.decode('utf-8').replace('\r\n', '\n').encode('utf-8')).hexdigest()


def catalog_dependency_path(directory, name):
    source = directory.joinpath(name)
    if name in ('native_enemy_assets.json', 'campaign_stage_ids.json') and not source.is_file():
        source = directory.joinpath('../../content/catalog', name)
    return source


def _catalog_dependencies(document, directory, verify_assets):
    digests = _catalog_map(document, 'dependencies')
    _require(set(digests) == set(CATALOG_DEPENDENCIES), "Catalog dependencies are missing or unknown")
    documents = {}
    for name in CATALOG_DEPENDENCIES:
        if name == 'native_enemy_assets.json' and not verify_assets:
            continue
        try:
            payload = catalog_dependency_path(directory, name).read_bytes()
            _require(dependency_digest(payload) == digests[name], f"Catalog dependency digest mismatch: {name}", DigestError)
            documents[name] = json.loads(payload)
        except (OSError, ValueError, UnicodeError, RecursionError) as error:
            if isinstance(error, ContractError):
                raise
            raise StructureError(f"Cannot read catalog dependency {name}: {error}") from error
    return documents


def catalog_target_rows(document, field, key):
    return [dict(row, stage_id=stage_id, source_id=row[key], role=field)
            for stage_id, stage in sorted(document['maps'].items())
            for row in sorted(stage[field], key=lambda row: row[key])]


def _installed_source_tables(document, dependencies):
    rows = dict(sources=native_source_rows(dependencies['native_enemy_catalog.json'], document['roles']),
                finite_sources=catalog_target_rows(dependencies['campaign_kill_sources.json'], 'sources', 'source_id'),
                timers=catalog_target_rows(dependencies['secret_encounter_timers.json'], 'timers', 'entity'))
    source_index([row for table in rows.values() for row in table])
    return {name: source_index(table) for name, table in rows.items()}


def installed_catalog_tables(document, dependencies):
    native = dependencies['native_enemy_catalog.json']
    species = dependencies['enemy_species.json']['species']
    maps = {key: {**stage, 'stage_id': key if key != 'hub' else None} for key, stage in native['maps'].items()}
    return dict(maps=maps, stages=dependencies['campaign_stage_ids.json']['stages'],
        templates={key: dict(species=key, **row, qualification=species[key]) for key, row in native['species'].items()},
        species=sorted(native['species']), counters=sorted(native['species']),
        spawn_types=sorted({spawn for stage in maps.values() for row in stage['origins'] + stage.get('runtime_origins', [])
                            for spawn in row.get('logical_types', [])}),
        **_installed_source_tables(document, dependencies),
        **({'assets': dependencies['native_enemy_assets.json']} if 'native_enemy_assets.json' in dependencies else {}))


def load_slot_catalog(expected_digest: str | None = None, *, path: Path | None = None,
                      verify_assets: bool = True) -> dict:
    """Read installed reference tables and recompute their declared identity."""
    source = path if path is not None else files(__package__).joinpath("slot_catalog.json")
    directory = path.parent if path is not None else files(__package__)
    manifest = directory.joinpath("projection_manifest.json")
    try:
        document = json.loads(source.read_bytes())
        identity = json.loads(manifest.read_bytes())
    except (OSError, ValueError, RecursionError) as error:
        raise StructureError(f"Cannot read slot catalog: {error}") from error
    _require(type(document) is dict and type(identity) is dict, "Slot catalog and manifest must be objects")
    digest = catalog_digest(document)
    _require(digest == identity.get("catalog_digest"), "Installed catalog digest mismatch", DigestError)
    if expected_digest is not None:
        _require(digest == expected_digest, "Room catalog digest mismatch", DigestError)
    _require(document.get('format') == 2, "Unsupported slot catalog format")
    dependencies = _catalog_dependencies(document, directory, verify_assets)
    try:
        return SlotCatalog(document, installed_catalog_tables(document, dependencies))
    except (KeyError, TypeError, AttributeError) as error:
        raise StructureError(f"Invalid catalog dependency: {error}") from error


def recipe_digest(recipes: dict) -> str:
    return hashlib.sha256(canonical_bytes(recipes)).hexdigest()


def placement_digest(placements: list) -> str:
    _json_shape(placements)
    rows = _read(tuple[tuple[Address, Address, Positive], ...], placements, "placements")
    _unique([row[0] for row in rows], "placements", ordered=True)
    return hashlib.sha256(canonical_bytes(sorted(rows))).hexdigest()


def generation_fingerprint(slot_data: dict) -> str:
    return hashlib.sha256(canonical_bytes({key: value for key, value in slot_data.items()
                                          if key != "native_generation_fingerprint"})).hexdigest()


def _require(condition: bool, message: str, error: type[ContractError] = StructureError) -> None:
    if not condition:
        raise error(message)


def _json_shape(value: object, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [MAX_JSON_NODES]
    budget[0] -= 1
    _require(depth <= MAX_JSON_DEPTH, "Room data nesting limit exceeded")
    _require(budget[0] >= 0, "Room data JSON node limit exceeded")
    if type(value) in (dict, list):
        _json_collection(value, depth, budget)
    else:
        _require(type(value) in (str, int, bool, type(None)), "Room data requires JSON values with integer numbers")


def _json_collection(value, depth, budget):
    _require(len(value) <= MAX_COLLECTION, "Room data collection is too large")
    children = value
    if type(value) is dict:
        _require(all(type(key) is str for key in value), "Room data keys must be strings")
        children = value.values()
    for child in children:
        _json_shape(child, depth + 1, budget)


@cache
def _field_types(record):
    return get_type_hints(record, include_extras=True)


def _read_record(record, value, path):
    _require(type(value) is dict, f"{path} must be an object")
    annotations = _field_types(record)
    _require(set(value) == set(annotations), f"{path} has missing or unknown fields")
    return record(**{name: _read(kind, value[name], f"{path}.{name}") for name, kind in annotations.items()})


def _read_tuple(arguments, value, path):
    _require(type(value) is list, f"{path} must be an array")
    if arguments[-1] is Ellipsis:
        return tuple(_read(arguments[0], child, path) for child in value)
    _require(len(value) == len(arguments), f"{path} has the wrong number of fields")
    return tuple(_read(kind, child, path) for kind, child in zip(arguments, value))


def _read(kind, value, path):
    origin, arguments = get_origin(kind), get_args(kind)
    if origin is Annotated:
        if arguments[0] is str:
            return _read_string(arguments, value, path)
        _require(type(value) is int and arguments[1] <= value <= arguments[2], f"{path} requires an integer in range")
        return value
    if origin in (UnionType, Union):
        return _read_union(arguments, value, path)
    if origin is Literal:
        _require(type(value) is type(arguments[0]) and value in arguments, f"{path} has an unsupported value")
        return value
    return _read_container(kind, origin, arguments, value, path)


def _read_string(arguments, value, path):
    _require(type(value) is str and arguments[1] <= len(value) <= arguments[2], f"{path} requires a string in range")
    if len(arguments) > 3:
        _require(re.fullmatch(arguments[3], value) is not None, f"{path} has an invalid reference string")
    return value


def _read_union(arguments, value, path):
    for kind in arguments:
        try:
            return _read(kind, value, path)
        except StructureError:
            pass
    raise StructureError(f"{path} has an unsupported type or value")


def _read_container(kind, origin, arguments, value, path):
    if is_dataclass(kind):
        return _read_record(kind, value, path)
    if origin is tuple:
        return _read_tuple(arguments, value, path)
    if origin is Mapping:
        _require(type(value) is dict, f"{path} must be an object")
        return MappingProxyType({_read(str, key, path): _read(arguments[1], child, path)
                                 for key, child in value.items()})
    _require(type(value) is kind, f"{path} requires {kind.__name__}")
    if kind is str:
        _require(0 < len(value) <= 512, f"{path} requires a short nonempty string")
    return value


def _unique(values, path, *, ordered=False):
    _require(len(set(values)) == len(values), f"{path} has duplicate ids", ReferenceError)
    if not ordered:
        _require(list(values) == sorted(values), f"{path} must be sorted")


def _refs(values, allowed, path, *, ordered=False):
    _unique(values, path, ordered=ordered)
    _require(set(values) <= set(allowed), f"{path} has an unknown reference", ReferenceError)


def _table(catalog, name):
    value = catalog.get(name)
    _require(type(value) in (list, dict), f"Catalog {name} must be a reference table")
    _require(len(value) <= MAX_REFERENCE_ENTRIES, f"Catalog {name} exceeds reference table limit")
    if name in ('templates', 'sources', 'finite_sources', 'timers'):
        _require(type(value) is dict, f"Catalog {name} must be an object")
        return value
    if name in ('roles', 'spawn_types', 'species', 'counters'):
        _require(type(value) is list, f"Catalog {name} must be an array")
        return value
    _require(type(value) is list, f"Catalog {name} must be an array")
    return range(len(value))


def _catalog_map(catalog, name):
    value = catalog.get(name)
    _require(type(value) is dict, f"Catalog {name} must be an object")
    return value


def _catalog_references(catalog):
    stages = _catalog_map(catalog, "stages")
    access = [_read(Positive, _catalog_map(stages, stage).get("access_id"), "catalog.access_id") for stage in stages]
    _unique(access, "catalog.access_ids", ordered=True)
    _table(catalog, "items")
    items = _read(tuple[Address, ...], catalog.get("items"), "catalog.items")
    _unique(items, "catalog.items")
    for name in ("templates", "roles", "spawn_types", "species", "counters"):
        _table(catalog, name)
    maps = _catalog_maps(catalog, stages)
    for name in ("sources", "finite_sources", "timers"):
        _catalog_source_stages(catalog, name, maps)


def _catalog_maps(catalog, stages):
    maps = _catalog_map(catalog, "maps")
    _require(set(stages) <= set(maps), "Catalog maps must cover campaign stages", ReferenceError)
    for name in maps:
        row = _catalog_map(maps, name)
        _require("stage_id" in row, "Catalog map must declare its stage ownership")
        stage = _read(str | None, row.get("stage_id"), "catalog.maps.stage_id")
        _require(stage is None or stage in stages, "Catalog map has an unknown stage", ReferenceError)
        _require(stage == name or stage is None and name not in stages, "Catalog map has inconsistent stage ownership", ReferenceError)
    return maps


def _active_maps(catalog, stages):
    return {name for name, row in catalog["maps"].items() if row["stage_id"] is None or row["stage_id"] in stages}


def _catalog_source_stages(catalog, name, stages):
    for ref in _table(catalog, name):
        source = _catalog_map({"source": catalog[name][ref]}, "source")
        stage = _read(str, source.get("stage_id"), f"catalog.{name}.stage_id")
        _require(stage in stages, f"Catalog {name} has an unknown stage", ReferenceError)


def _stage_refs(campaign, catalog):
    stages = _catalog_map(catalog, "stages")
    _refs(campaign.sequence, stages, "campaign.sequence", ordered=True)
    _refs(campaign.master_level_stage_ids, stages, "campaign.master_level_stage_ids")
    active = set(campaign.sequence) | set(campaign.master_level_stage_ids)
    _require(not set(campaign.sequence) & set(campaign.master_level_stage_ids), "Campaign stages overlap")
    _refs(campaign.starting_stages, active, "campaign.starting_stages")
    _campaign_decisions(campaign, catalog)
    _require(bool(campaign.sequence) and bool(campaign.starting_stages), "Campaign needs a sequence and starting stages")
    for stage_id in (campaign.fixed_dash_completion_stage, campaign.goal.stage_id):
        _require(stage_id is None or stage_id in active, "Campaign has an inactive stage reference", ReferenceError)
    access = []
    for stage_id in active:
        row = _catalog_map(stages, stage_id)
        _require(row.get("status", "active") == "active", "Campaign references a retired stage", ReferenceError)
        access.append(_read(Positive, row.get("access_id"), "catalog.access_id"))
    _unique(access, "catalog.access_ids", ordered=True)
    _refs(campaign.access_items, access, "campaign.access_items")
    return active


def _campaign_decisions(campaign, catalog):
    _refs(campaign.unprepared_start_stages, campaign.starting_stages, "campaign.unprepared_start_stages")
    _require(campaign.opening_stage is None or campaign.opening_stage in campaign.starting_stages,
             "campaign.opening_stage must be a starting stage", ReferenceError)
    _require(campaign.active_masteries_count <= len(_catalog_map(catalog, "masteries")),
             "campaign.active_masteries_count exceeds the mastery count")
    goal_access = catalog["stages"].get(campaign.goal.stage_id, {}).get("access_id")
    _require(campaign.goal_as_item == (goal_access in campaign.access_items),
             "campaign.goal_as_item disagrees with goal access")


def _battery(campaign, catalog):
    fortress = campaign.fortress
    groups = _catalog_map(catalog, "spend_groups")
    _refs(fortress.active_spend_group_ids, groups, "fortress.active_spend_group_ids")
    cost = sum(_read(Integer, groups[key], "catalog.spend_groups.cost") for key in fortress.active_spend_group_ids)
    economy = fortress.battery_economy
    _require(economy.cost == cost and economy.total == cost + economy.surplus, "Battery totals disagree with active groups")
    percent = _read(Annotated[int, 0, 100], catalog.get("battery_surplus_percent"), "catalog.battery_surplus_percent")
    _require(economy.surplus_percent == percent, "Battery surplus percentage disagrees with catalog")
    _require(economy.surplus <= battery_surplus_limit(cost, percent), "Battery surplus exceeds its ceiling")
    _require(list(fortress.phase_thresholds) == sorted(fortress.phase_thresholds), "Fortress thresholds must be ordered")
    _require(all(value <= len(campaign.sequence) for value in fortress.phase_thresholds), "Fortress threshold exceeds campaign")


def _starting(starting, catalog):
    ids = [pair[0] for pair in starting.items]
    _refs(ids, catalog.get("items", []), "starting.items")
    _require(starting.weapon_item_id in ids, "Starting weapon must be owned", ReferenceError)


def _stage_profiles(profiles, active, catalog):
    _require(set(profiles) == active, "Stage profiles must cover active stages", ReferenceError)
    for profile in profiles.values():
        _require(profile.cr_min <= profile.cr_max, "Stage combat range is reversed")
        _require((profile.opening_cr_min is None) == (profile.opening_cr_max is None), "Opening combat range is incomplete")
        if profile.opening_cr_min is not None:
            _require(profile.opening_cr_min <= profile.opening_cr_max, "Opening combat range is reversed")
        _refs(profile.counter_ids, _table(catalog, "counters"), "stage.counter_ids")
        _refs(profile.opening_counter_ids, _table(catalog, "counters"), "stage.opening_counter_ids")


def _population(limits, catalog):
    population = limits.population
    if population is None:
        return
    _require(re.fullmatch("[0-9a-f]{16}", population.scope) is not None, "Population scope requires 16 lowercase hex digits")
    values = (population.heavy, population.superheavy, population.pressure)
    _require(max(values) <= population.maximum, "Population limits exceed maximum")
    _refs([pair[0] for pair in population.species_caps], _table(catalog, "species"), "population.species_caps")
    if population.counter is not None:
        _refs([population.counter], _table(catalog, "species"), "population.counter")
    _require(all(pair[1] <= population.maximum for pair in population.species_caps), "Species cap exceeds maximum")


def _deduplicated(table, path):
    keys = [canonical_bytes(value) for value in table]
    _unique(keys, path)


def _pattern_sizes(recipes):
    sizes = []
    for pattern in recipes.patterns:
        size, previous = 0, None
        for profile_ref, count in pattern:
            _require(profile_ref < len(recipes.profiles), "Pattern has an unknown profile", ReferenceError)
            _require(profile_ref != previous, "Adjacent pattern runs must be combined")
            previous = profile_ref
            size += count
        _require(size <= MAX_EXPANDED_BODIES, "Pattern expansion exceeds body limit", ExpansionError)
        sizes.append(size)
    return sizes


def _recipe_row(row, recipes, sizes, stage_id, catalog):
    source, role, spawn, pattern, logical, maximum, credit, renewable, numerator, denominator, limits = row
    _refs([source], _table(catalog, "sources"), "recipe.source")
    binding = _catalog_map({"binding": catalog["sources"][source]}, "binding")
    _require(binding.get("stage_id") == stage_id, "Recipe source belongs to another stage", ReferenceError)
    _refs([role], _table(catalog, "roles"), "recipe.role")
    _require(binding.get('role', 'primary') == role, "Recipe role disagrees with source", ReferenceError)
    _refs([spawn], _table(catalog, "spawn_types"), "recipe.spawn_type")
    _require(pattern < len(recipes.patterns), "Recipe has an unknown pattern", ReferenceError)
    _require(len(limits) == sizes[pattern], "Recipe limit references must cover every body")
    _require(all(ref < len(recipes.limit_sets) for ref in limits), "Recipe has an unknown limit set", ReferenceError)
    _require(credit <= logical and bool(credit) == bool(maximum), "Recipe final credit disagrees with quota")
    _require(not maximum or sizes[pattern] > 0, "Recipe quota needs bodies")
    _require(not maximum or ((maximum - 1) * logical + credit) % sizes[pattern] == 0, "Recipe credit has fractional units")
    _require(bool(numerator) == bool(sizes[pattern]), "Recipe ratio disagrees with body presence")
    return sizes[pattern]


def _recipe_map(record, stage_id, recipes, sizes, catalog):
    sources = [row[0] for row in record.rows]
    _unique(sources, "recipe.rows")
    for values in (record.passthrough_sources, record.disabled_sources):
        _refs(values, _table(catalog, "sources"), "recipe.sources")
        for ref in values:
            _require(catalog["sources"][ref]["stage_id"] == stage_id, "Source belongs to another stage", ReferenceError)
    _unique(sources + list(record.passthrough_sources) + list(record.disabled_sources), "recipe source decisions", ordered=True)
    return sum(_recipe_row(row, recipes, sizes, stage_id, catalog) for row in record.rows)


def _recipe_coverage(recipes, active, catalog):
    for stage_id in active:
        expected = {ref for ref, source in catalog["sources"].items() if source["stage_id"] == stage_id}
        record = recipes.maps.get(stage_id, RecipeMap((), (), ()))
        supplied = {row[0] for row in record.rows} | set(record.passthrough_sources) | set(record.disabled_sources)
        _require(supplied == expected, "Recipe decisions must cover active sources", ReferenceError)
    expected_timers = sorted(ref for ref, timer in catalog["timers"].items() if timer["stage_id"] in active)
    _require([ref for ref, duration in recipes.timers] == expected_timers,
             "Resolved timers must cover active targets", ReferenceError)


def _recipes(recipes, raw, active, catalog):
    for limits in recipes.limit_sets:
        _population(limits, catalog)
    for profile in recipes.profiles:
        _refs([profile[0]], _table(catalog, "templates"), "profile.template")
    sizes = _pattern_sizes(recipes)
    for name in ("limit_sets", "profiles", "patterns"):
        _deduplicated(raw[name], f"recipes.{name}")
    _require(set(recipes.maps) <= active, "Recipe map is inactive", ReferenceError)
    expanded = sum(_recipe_map(record, stage, recipes, sizes, catalog) for stage, record in recipes.maps.items())
    _require(expanded <= MAX_EXPANDED_BODIES, "Recipe expansion exceeds body limit", ExpansionError)
    _refs([pair[0] for pair in recipes.timers], _table(catalog, "timers"), "recipes.timers")
    _recipe_coverage(recipes, active, catalog)


def _killsanity(kills, active, catalog):
    _refs([pair[0] for pair in kills.sources], _table(catalog, "finite_sources"), "killsanity.sources")
    _require(kills.finite_supported_count <= kills.count, "Finite reward count exceeds total")
    _require(sum(pair[1] for pair in kills.sources) == kills.finite_kills, "Finite kill total disagrees with sources")
    _require(kills.estimated_kills >= kills.finite_kills, "Estimated kills must cover finite kills")
    _require(kills.finite_supported_count * kills.interval <= kills.finite_kills, "Finite rewards exceed supported kills")
    for ref, count in kills.sources:
        source = catalog["finite_sources"][ref]
        _require(type(source) is dict and source.get("stage_id") in active, "Finite source belongs to an inactive stage", ReferenceError)
    if kills.count:
        _require(kills.interval > 0, "Kill rewards need a positive interval")
    else:
        _require(not any((kills.finite_supported_count, kills.finite_kills, kills.estimated_kills, kills.sources)),
                 "Disabled kill rewards must have zero counts and no sources")


def _tracker_options(options):
    _require({"start_inventory", "start_inventory_from_pool"} <= set(options), "Tracker starting options are missing")
    for name, value in options.items():
        if type(value) is list:
            _unique(value, f"tracker_options.{name}")
    for name in ("start_inventory", "start_inventory_from_pool"):
        _require(type(options[name]) is dict, f"tracker_options.{name} must be an object")


def _digests(data, raw, catalog, placements):
    pairs = [("catalog", data.identity.catalog_digest, catalog_digest(catalog)),
             ("recipes", data.identity.recipe_digest, recipe_digest(raw["recipes"])),
             ("generation", data.native_generation_fingerprint, generation_fingerprint(raw))]
    if placements is not None:
        pairs.append(("placements", data.placement_digest, placement_digest(placements)))
    for name, actual, expected in pairs:
        _require(actual == expected, f"Room {name} digest mismatch", DigestError)
    for value in (data.identity.catalog_digest, data.identity.recipe_digest, data.placement_digest, data.native_generation_fingerprint):
        _require(re.fullmatch("[0-9a-f]{64}", value) is not None, "Digest requires 64 lowercase hex digits", DigestError)


def validate_slot_data(slot_data: dict, catalog: dict | None = None, *, placements: list | None = None,
                       supported_capabilities: frozenset[str] = frozenset()) -> SlotData:
    """Validate wire data against installed catalog tables; return immutable resolved values."""
    _json_shape(slot_data)
    if catalog is None:
        catalog = load_slot_catalog()
    _require(type(slot_data) is dict and isinstance(catalog, dict), "Room data and catalog must be objects")
    document, catalog = catalog, catalog_tables(catalog)
    total, recipe_size = len(canonical_bytes(slot_data)), len(canonical_bytes(slot_data.get("recipes")))
    if total > MAX_PAYLOAD_BYTES:
        raise PayloadSizeError(total, recipe_size)
    _recipe_table_bounds(slot_data.get("recipes"))
    data = _read_record(SlotData, slot_data, "slot_data")
    _catalog_references(catalog)
    _unique(data.compatibility.required_capabilities, "required_capabilities")
    _require(set(data.compatibility.required_capabilities) <= supported_capabilities, "Room requires unsupported capabilities")
    active = _stage_refs(data.campaign, catalog)
    _battery(data.campaign, catalog)
    _require(data.campaign.fortress.battery_economy.native_first_battery == (not data.runtime.randomize_first_battery),
             "Native first Battery setting disagrees with runtime")
    _starting(data.starting, catalog)
    _stage_profiles(data.stage_profiles, active, catalog)
    _recipes(data.recipes, slot_data["recipes"], _active_maps(catalog, active), catalog)
    _killsanity(data.killsanity, active, catalog)
    _tracker_options(slot_data["tracker_options"])
    _refs(list(data.runtime.mastery_requirement_counts), _catalog_map(catalog, "masteries"),
          "runtime.mastery_requirement_counts", ordered=True)
    _unique(data.campaign.goal.additional_requirements, "goal.additional_requirements")
    _digests(data, slot_data, document, placements)
    return data


def _recipe_table_bounds(recipes):
    _require(type(recipes) is dict, "recipes must be an object")
    for name, limit in (("limit_sets", MAX_LIMIT_SETS), ("profiles", MAX_PROFILES), ("patterns", MAX_PATTERNS)):
        table = recipes.get(name)
        _require(type(table) is list, f"recipes.{name} must be an array")
        _require(len(table) <= limit, f"recipes.{name} exceeds table limit")
