"""Normalized, data-only view of DOOM Eternal Archipelago content.

The JSON files remain the authoring surface.  This module deliberately has no
game/parser knowledge: consumers ask it for maps, locations, publishers and
assets instead of carrying a second list of campaign content.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from doom_eap.contracts.ap_visual_contract import load_ap_visual_contract
from doom_eap.contracts.publisher_contracts import (EFFECT_STRATEGIES, TRIGGER_STRATEGIES,
                                 PublisherContract, publisher_contracts_from_document)


REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT = REPO_ROOT
PHYSICAL_STRATEGIES = frozenset({
    "prop_pickup", "interactable", "codex", "audio_terminal",
    "secret_encounter", "armor_terminal", "independent_trigger",
})
RUNTIME_STRATEGIES = frozenset({
    "native_transition", "map_terminal", "unlockable_record", "stat_threshold",
    "physical_event_equivalent", "aggregate", "all_mission_challenge_records",
})
PUBLISHER_STRATEGIES = TRIGGER_STRATEGIES | EFFECT_STRATEGIES
ASSET_STRATEGIES = frozenset({
    "donor_model_override", "packaged_bundle", "resident_model",
    "map_resource", "streamdb",
})


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def thaw_content(value: Any) -> Any:
    """Return a JSON-serializable copy of normalized catalog data."""
    if isinstance(value, Mapping):
        return {key: thaw_content(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_content(item) for item in value]
    return value


@dataclass(frozen=True)
class MapSpec:
    key: str
    display_name: str
    source_file: str
    level_config_path: Path
    manifest_path: Path
    onboarding_path: Path | None
    runtime_map: str
    resource_base: str
    resource_owner: str
    relative_entities_path: str
    enabled: bool
    data: Mapping[str, Any]
    source_sha256: str = ""
    source_size: int = 0
    resource_path: str = ""
    resource_priority: int = 0
    supported_game_revision: str = ""

    @property
    def requires_dlc_content(self) -> bool:
        return self.runtime_map.startswith("game/dlc")


@dataclass(frozen=True)
class PhysicalLocationSpec:
    name: str
    location_id: int
    map_key: str
    ap_check: str
    region: str
    strategy: str
    policy: Mapping[str, Any]


@dataclass(frozen=True)
class RuntimeLocationSpec:
    name: str
    location_id: int
    strategy: str
    mission_key: str | None
    signal: Mapping[str, Any]
    data: Mapping[str, Any]
    category: str = ""
    region: str = ""


@dataclass(frozen=True)
class ChallengeSpec:
    name: str
    location_id: int
    mission_key: str | None
    strategy: str
    signal: Mapping[str, Any]


PublisherSpec = PublisherContract


@dataclass(frozen=True)
class AssetSpec:
    key: str
    map_key: str
    strategy: str
    model: str
    resource_base: str
    resource_owner: str
    dependencies: tuple[str, ...]
    dependency_policy: str
    donor: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    replacement_slot_policy: str = ""
    replacement_slot: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    usage_policy: str = ""
    preserve: tuple[str, ...] = ()
    visual_presentation_policy: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True)
class ContentCatalog:
    root: Path
    maps: Mapping[str, MapSpec]
    physical_locations: tuple[PhysicalLocationSpec, ...]
    runtime_locations: tuple[RuntimeLocationSpec, ...]
    challenges: tuple[ChallengeSpec, ...]
    publishers: tuple[PublisherSpec, ...]
    assets: tuple[AssetSpec, ...]
    location_names: Mapping[int, str]
    campaign_goal: Mapping[str, Any]
    route: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    reserved_location_ids: Mapping[int, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    region_metadata: Mapping[str, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    region_assignments: Mapping[int, str] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def map(self, key: str) -> MapSpec:
        return self.maps[key]

    def enabled_maps(self) -> tuple[MapSpec, ...]:
        return tuple(spec for spec in self.maps.values() if spec.enabled)

    def location_by_id(self, location_id: int) -> PhysicalLocationSpec | RuntimeLocationSpec:
        for spec in (*self.physical_locations, *self.runtime_locations):
            if spec.location_id == location_id:
                return spec
        raise KeyError(location_id)


def _strategy_for_policy(policy: Mapping[str, Any], ap_check: str) -> str:
    if policy.get("independent_ap_trigger"):
        return "independent_trigger"
    lowered = ap_check.lower()
    if "codex" in lowered:
        return "codex"
    if "interact" in lowered:
        return "interactable"
    if "audio" in lowered or "lore_kiosk" in lowered:
        return "audio_terminal"
    if "argent" in lowered or "armor" in lowered:
        return "armor_terminal"
    return "prop_pickup"


def _resource_base(resource_path: str) -> str:
    """Map asset namespace; intentionally distinct from a patch container."""
    return Path(re.sub(r"_patch\d+(?=\.resources$)", "", resource_path)).stem


def _map_content_packages(root: Path) -> tuple[dict[str, Any], ...]:
    """Discover the one complete authoring package for every map."""
    component_names = ("locations", "runtime", "publishers", "assets", "onboarding")
    required = {
        "schema_version", "key", "display_name", "enabled", "test_only",
        "onboarding_status", "source_file", "source_sha256", "source_size",
        "source_owner", "generated_output", "runtime_map", "resource_base",
        "resource_path", "resource_owner", "resource_priority",
        "relative_entities_path", "supported_game_revision", "route", "order",
    }
    root_path = root / "content" / "maps"
    if not root_path.is_dir():
        raise ValueError("component=map_package file=content/maps value=missing")
    packages = []
    keys: set[str] = set()
    for directory in sorted(path for path in root_path.iterdir() if path.is_dir()):
        paths = {name: directory / f"{name}.json" for name in ("descriptor", *component_names)}
        missing = [str(path.name) for path in paths.values() if not path.is_file()]
        if missing:
            raise ValueError(f"component=map_package map={directory.name} field=components value=missing {missing}")
        descriptor = _json(paths["descriptor"])
        key = descriptor.get("key")
        unknown = set(descriptor) - required
        missing_fields = required - set(descriptor)
        if descriptor.get("schema_version") != 1 or key != directory.name or unknown or missing_fields:
            raise ValueError(f"component=map_package map={directory.name} field=descriptor value=invalid key/schema/fields")
        if key in keys:
            raise ValueError(f"component=map_package map={key} field=key value=duplicate")
        keys.add(key)
        documents = {name: _json(path) for name, path in paths.items() if name != "descriptor"}
        invalid_components = [
            name for name, document in documents.items()
            if document.get("schema_version") != 1
            and not (name == "onboarding" and document.get("schema_version") == 2)
        ]
        if invalid_components:
            raise ValueError(f"component=map_package map={key} field=schema_version value!=1")
        packages.append({"directory": directory, "descriptor": descriptor, **documents})
    if not packages:
        raise ValueError("component=map_package file=content/maps value=empty")
    if any(not isinstance(item["descriptor"]["order"], int) for item in packages):
        raise ValueError("component=map_package field=order value=non-integer")
    if len({item["descriptor"]["order"] for item in packages}) != len(packages):
        raise ValueError("component=map_package field=order value=duplicate")
    return tuple(sorted(packages, key=lambda item: item["descriptor"]["order"]))


def _load_region_topology(root: Path) -> tuple[Mapping[str, Any], Mapping[int, str]]:
    path = root / "content" / "catalog" / "region_topology.json"
    topology = _json(path)
    if topology.get("schema_version") != 1:
        raise ValueError("region topology schema_version must be 1")
    regions = topology.get("regions", [])
    assignments = topology.get("assignments", {})
    if not isinstance(regions, list) or not isinstance(assignments, Mapping):
        raise ValueError("region topology regions/assignments are invalid")
    names = [entry.get("name") for entry in regions]
    if any(not isinstance(entry, Mapping) or not entry.get("name") for entry in regions):
        raise ValueError("region topology contains an invalid region")
    if len(names) != len(set(names)):
        raise ValueError("region topology contains duplicate regions")
    if set(assignments) - set(names):
        raise ValueError("region topology assigns locations to undeclared regions")
    flattened = [location_id for values in assignments.values() for location_id in values]
    if len(flattened) != len(set(flattened)):
        raise ValueError("region topology assigns a location more than once")
    location_regions = {
        int(location_id): region
        for region, values in assignments.items()
        for location_id in values
    }
    raw_connections = topology.get("connections", [])
    raw_conditions = topology.get("connection_conditions", {})
    if not isinstance(raw_connections, list) or not isinstance(raw_conditions, Mapping):
        raise ValueError("region topology connections/connection_conditions are invalid")
    connection_keys = set()
    connections = []
    for row in raw_connections:
        if not isinstance(row, list) or len(row) != 3 or any(not isinstance(value, str) for value in row):
            raise ValueError("region topology contains an invalid connection")
        source, destination, entrance = row
        key = f"{source} -> {destination}"
        if key in connection_keys:
            raise ValueError("region topology contains duplicate connection pairs")
        connection_keys.add(key)
        condition = raw_conditions.get(key, {})
        if not isinstance(condition, Mapping):
            raise ValueError(f"{key}: connection condition must be an object")
        unknown = set(condition) - {"soft_capabilities"}
        if unknown:
            raise ValueError(f"{key}: unknown connection condition fields: {sorted(unknown)}")
        capabilities = condition.get("soft_capabilities", [])
        if not isinstance(capabilities, list) or any(not isinstance(value, str) or not value for value in capabilities):
            raise ValueError(f"{key}: soft_capabilities must be a list of non-empty strings")
        connections.append([source, destination, entrance, _freeze(dict(condition))])
    undeclared_conditions = set(raw_conditions) - connection_keys
    if undeclared_conditions:
        raise ValueError(f"region topology declares conditions for unknown connections: {sorted(undeclared_conditions)}")
    route = {
        "schema_version": 1,
        "regions": [entry["name"] for entry in sorted(regions, key=lambda entry: entry["order"])],
        "connections": connections,
        "virtual_locations": [],
    }
    metadata = {entry["name"]: entry for entry in regions}
    return _freeze({"topology": topology, "route": route, "metadata": metadata}), MappingProxyType(location_regions)


def _asset(raw_asset: Mapping[str, Any], spec: MapSpec) -> AssetSpec:
    return AssetSpec(raw_asset["key"], spec.key, raw_asset["strategy"], raw_asset["model"],
        raw_asset.get("resource_base", spec.resource_base), raw_asset.get("resource_owner", spec.resource_owner),
        tuple(raw_asset.get("dependencies", [])), raw_asset.get("dependency_policy", "required"),
        _freeze(raw_asset.get("donor", {})), raw_asset.get("replacement_slot_policy", ""),
        _freeze(raw_asset.get("replacement_slot", {})), raw_asset.get("usage_policy", ""),
        tuple(raw_asset.get("preserve", [])), _freeze(raw_asset.get("visual_presentation_policy", {})))


def load_content_catalog(root: Path = ROOT) -> ContentCatalog:
    canonical_visual = load_ap_visual_contract(root)
    packages = _map_content_packages(root)
    topology_data, region_assignments = _load_region_topology(root)
    maps: dict[str, MapSpec] = {}
    physical: list[PhysicalLocationSpec] = []
    runtime: list[RuntimeLocationSpec] = []
    challenges: list[ChallengeSpec] = []
    assets: list[AssetSpec] = []
    publishers: list[PublisherSpec] = []
    names: dict[int, str] = {}
    reserved: dict[int, Mapping[str, Any]] = {}
    goal: Mapping[str, Any] | None = None
    for package in packages:
        source = package["descriptor"]
        key = source["key"]
        directory = package["directory"]
        spec = MapSpec(
            key, source["display_name"], source["source_file"], directory / "locations.json",
            root / "manifests" / f"{key}.json", directory / "onboarding.json",
            source["runtime_map"], source["resource_base"], source["resource_owner"],
            source["relative_entities_path"], bool(source["enabled"]),
            _freeze({**source, "package_directory": str(directory.relative_to(root))}),
            source["source_sha256"], source["source_size"], source["resource_path"],
            source["resource_priority"], source["supported_game_revision"],
        )
        maps[key] = spec
        config = package["locations"]
        policies = config.get("target_policies", {})
        package_ids = set(config.get("entities", {}).values()) | {item["location_id"] for item in config.get("secret_encounters", [])}
        package_names = {int(location_id): name for location_id, name in config.get("names", {}).items()}
        if set(package_names) != package_ids:
            raise ValueError(f"{key}: package physical names must match physical location IDs")
        names.update(package_names)
        for ap_check, location_id in config.get("entities", {}).items():
            entity = ap_check.removeprefix("AP_CHECK_").lower()
            policy = _freeze(policies.get(entity, {}))
            physical.append(PhysicalLocationSpec("", location_id, key, ap_check,
                region_assignments[location_id], _strategy_for_policy(policy, ap_check), policy))
        for encounter in config.get("secret_encounters", []):
            location_id = encounter["location_id"]
            physical.append(PhysicalLocationSpec("", location_id, key,
                encounter.get("ap_check", f"AP_CHECK_SECRET_{location_id}"), region_assignments[location_id],
                "secret_encounter", _freeze(encounter)))
        assets.extend(_asset(asset, spec) for asset in package["assets"].get("assets", []))
        if spec.enabled:
            assets.append(AssetSpec(canonical_visual["key"], key, canonical_visual["strategy"],
                canonical_visual["model"], spec.resource_base, spec.resource_owner,
                tuple(canonical_visual["dependencies"]), canonical_visual["dependency_policy"],
                replacement_slot_policy=canonical_visual["replacement_slot_policy"],
                replacement_slot=_freeze({**canonical_visual["replacement_slot"], "resource_archive": spec.resource_base}),
                usage_policy=canonical_visual["usage_policy"], preserve=tuple(canonical_visual["preserve"]),
                visual_presentation_policy=_freeze(canonical_visual["visual_presentation_policy"])))
        for entry in package["runtime"].get("locations", []):
            signal, category = _freeze(entry.get("signal", {})), entry.get("category", "")
            item = RuntimeLocationSpec(entry["name"], entry["location_id"], entry.get("strategy", signal.get("kind", "")), entry.get("mission_key"), signal, _freeze(entry), category, region_assignments[entry["location_id"]])
            runtime.append(item)
            if category != "mission_complete":
                challenges.append(ChallengeSpec(item.name, item.location_id, item.mission_key, item.strategy, signal))
        publishers.extend(publisher_contracts_from_document(package["publishers"], allow_empty=True))
        for entry in package["onboarding"].get("reserved_ids", []):
            location_id = int(entry["id"])
            if location_id in reserved:
                raise ValueError(f"duplicate reserved location ID: {location_id}")
            reserved[location_id] = _freeze({**entry, "map_key": key})
        if "campaign_goal" in package["onboarding"]:
            if goal is not None:
                raise ValueError("campaign goal must have exactly one package owner")
            goal = _freeze(package["onboarding"]["campaign_goal"])
    for entry in _json(root / "content" / "global_runtime.json").get("locations", []):
        signal, category = _freeze(entry.get("signal", {})), entry.get("category", "")
        item = RuntimeLocationSpec(entry["name"], entry["location_id"], entry.get("strategy", signal.get("kind", "")), entry.get("mission_key"), signal, _freeze(entry), category, region_assignments[entry["location_id"]])
        runtime.append(item)
        if category != "mission_complete":
            challenges.append(ChallengeSpec(item.name, item.location_id, item.mission_key, item.strategy, signal))
    if goal is None:
        raise ValueError("campaign goal has no package owner")
    runtime.sort(key=lambda item: item.data.get("order", len(runtime)))
    physical = [PhysicalLocationSpec(names.get(item.location_id, ""), item.location_id,
        item.map_key, item.ap_check, item.region, item.strategy, item.policy) for item in physical]
    catalog = ContentCatalog(root, MappingProxyType(maps), tuple(physical), tuple(runtime),
        tuple(challenges), tuple(publishers), tuple(assets), MappingProxyType(names), goal,
        topology_data["route"], MappingProxyType(reserved),
        MappingProxyType(topology_data["metadata"]), region_assignments)
    validate_content_catalog(catalog)
    return catalog

def validate_content_catalog(catalog: ContentCatalog) -> None:
    ids = [spec.location_id for spec in (*catalog.physical_locations, *catalog.runtime_locations)]
    names = [spec.name for spec in (*catalog.physical_locations, *catalog.runtime_locations)]
    if len(ids) != len(set(ids)) or not all(isinstance(value, int) and not isinstance(value, bool) for value in ids):
        raise ValueError("content catalog location IDs must be unique integers")
    if any(not name for name in names) or len(names) != len(set(names)):
        raise ValueError("content catalog location names must be unique")
    collisions = set(ids) & set(catalog.reserved_location_ids)
    if collisions:
        raise ValueError(
            f"reserved location IDs are public: {sorted(collisions)}"
        )
    known_ids = set(ids)
    region_names = set(catalog.route.get("regions", ()))
    metadata_names = set(catalog.region_metadata)
    if region_names != metadata_names or set(catalog.region_assignments) != known_ids:
        raise ValueError("region topology must declare and assign every public location")
    if any(region not in region_names for region in catalog.region_assignments.values()):
        raise ValueError("region topology assigns a location to an unknown region")
    connections = catalog.route.get("connections", ())
    if any(len(row) != 4 or row[0] not in region_names or row[1] not in region_names or row[0] == row[1] for row in connections):
        raise ValueError("region topology contains an illegal connection")
    pairs = {(row[0], row[1]) for row in connections}
    if len(pairs) != len(connections):
        raise ValueError("region topology contains duplicate connections")
    if any(set(row[3]) - {"soft_capabilities"} for row in connections):
        raise ValueError("region topology contains an unknown connection condition field")
    reachable = {"Menu"}
    while True:
        expanded = reachable | {row[1] for row in connections if row[0] in reachable}
        if expanded == reachable:
            break
        reachable = expanded
    if reachable != region_names:
        raise ValueError("region topology contains unreachable regions")
    mission_regions: dict[str, list[str]] = {}
    terminal_regions: dict[str, list[str]] = {}
    for region, metadata in catalog.region_metadata.items():
        mission_key = metadata.get("mission_key")
        if mission_key:
            mission_regions.setdefault(mission_key, []).append(region)
            if metadata.get("terminal") and not metadata.get("challenge_meta"):
                terminal_regions.setdefault(mission_key, []).append(region)
            if metadata.get("challenge_meta") and not region.endswith(" - Challenges - Mission Challenges"):
                raise ValueError(f"{region}: challenge meta region has non-canonical name")
    if any(len(regions) != 1 for regions in terminal_regions.values()) or set(terminal_regions) != set(mission_regions):
        raise ValueError("each base mission must have exactly one terminal region")
    for item in (*catalog.physical_locations, *catalog.runtime_locations):
        if catalog.region_assignments.get(item.location_id) != item.region:
            raise ValueError(f"{item.name}: region assignment is not canonical")
    for item in catalog.physical_locations:
        region_mission = catalog.region_metadata[item.region].get("mission_key")
        if item.map_key != "hub" and region_mission != item.map_key:
            raise ValueError(f"{item.name}: physical location is outside its mission topology")
    for item in catalog.runtime_locations:
        metadata = catalog.region_metadata[item.region]
        if item.category == "mission_complete":
            if not metadata.get("terminal") or not metadata.get("mission_key"):
                raise ValueError(f"{item.name}: mission complete is outside terminal mission region")
        elif item.category in {"mission_challenges", "all_mission_challenges"}:
            if not metadata.get("mission_key"):
                raise ValueError(f"{item.name}: challenge is outside its mission topology")
        if item.strategy not in RUNTIME_STRATEGIES:
            raise ValueError(f"{item.name}: unknown runtime strategy {item.strategy}")
        sources = item.signal.get("physical_location_ids", ())
        if sources:
            if len(sources) != len(set(sources)) or not set(sources) <= known_ids:
                raise ValueError(f"{item.name}: invalid physical_location_ids")
            count = item.signal.get("required_count", 1)
            if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= len(sources):
                raise ValueError(f"{item.name}: invalid required_count")
    asset_keys = {(asset.map_key, asset.key): asset for asset in catalog.assets}
    if len(asset_keys) != len(catalog.assets):
        raise ValueError("asset keys must be unique within each map")
    for asset in catalog.assets:
        if (
            asset.strategy not in ASSET_STRATEGIES
            or not asset.key
            or not asset.model
            or not asset.resource_base
            or not asset.resource_owner
        ):
            raise ValueError("invalid asset specification")
        if "_patch" in asset.resource_base:
            raise ValueError(f"asset resource_base must not be a patch owner: {asset.resource_base}")
        presentation = asset.visual_presentation_policy
        if presentation:
            material_decl = Path(str(presentation.get("material_decl", "")))
            preserved_maps = presentation.get("preserve_maps", {})
            allowed_strips = {
                "pickup_shader", "animated_emissive_mask", "bloom", "sheen",
                "cover_alpha_test",
            }
            strip = set(presentation.get("strip", ()))
            required_maps = {
                "albedo", "normal", "specular", "smoothness", "heightmap",
            }
            if (
                presentation.get("scope") != "ap_generated_entities_only"
                or presentation.get("material_mode")
                != "resource_scoped_opaque_override"
                or presentation.get("opaque_template") != "template/pbr"
                or presentation.get("think_component") != "bob_rotate_slow"
                or any(
                    presentation.get(key) is not True
                    for key in (
                        "preserve_motion", "preserve_bobbing", "preserve_rotation",
                    )
                )
                or material_decl.is_absolute()
                or ".." in material_decl.parts
                or material_decl.suffix != ".decl"
                or not strip
                or not strip <= allowed_strips
                or not isinstance(preserved_maps, Mapping)
                or set(preserved_maps) != required_maps
                or not re.fullmatch(
                    r"[0-9a-f]{64}", str(presentation.get("material_sha256", ""))
                )
            ):
                raise ValueError(
                    f"{asset.key}: invalid AP visual presentation policy"
                )
        if asset.strategy == "packaged_bundle" and not asset.dependencies:
            raise ValueError(f"{asset.key}: packaged_bundle requires declared dependencies")
        if asset.strategy == "resident_model" and asset.dependencies:
            raise ValueError(f"{asset.key}: resident_model must not declare copied dependencies")
        if asset.strategy == "donor_model_override":
            if (
                not asset.donor.get("kind")
                or asset.donor.get("selection") not in {
                    "per_location_source", "named_entity",
                }
            ):
                raise ValueError(f"{asset.key}: invalid donor_model_override contract")
            if (
                asset.donor.get("selection") == "named_entity"
                and not asset.donor.get("entity")
            ):
                raise ValueError(f"{asset.key}: named donor requires entity")
            required_preserve = {
                "trigger", "collision", "transform", "layers", "interaction",
            }
            if not required_preserve <= set(asset.preserve):
                raise ValueError(
                    f"{asset.key}: donor_model_override preserve contract is incomplete"
                )
            if asset.replacement_slot_policy not in {
                "native_question_mark", "safe_resident_static_lwo",
            }:
                raise ValueError(
                    f"{asset.key}: invalid replacement_slot_policy"
                )
            if asset.replacement_slot_policy == "native_question_mark":
                if (
                    asset.model != "art/pickups/question_mark_a.lwo"
                    or asset.replacement_slot
                ):
                    raise ValueError(
                        f"{asset.key}: native_question_mark must use the native slot"
                    )
                continue
            slot = asset.replacement_slot
            if asset.dependency_policy == "model_importer_bundle_pending":
                required_pending = {
                    "model_path", "resource_archive", "material2",
                    "pending_import", "vanilla_reference_allowlist",
                }
                pending = slot.get("pending_import", {})
                model_path = Path(str(slot.get("model_path", "")))
                if (
                    not required_pending <= set(slot)
                    or model_path.as_posix() != asset.model
                    or model_path.suffix.lower() != ".lwo"
                    or slot.get("resource_archive") != asset.resource_base
                    or not slot.get("material2")
                    or not isinstance(pending, Mapping)
                    or pending.get("producer")
                    != "Doom Eternal Model Importer v1.2"
                    or not re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(pending.get("source_obj_sha256", "")),
                    )
                    or not re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(pending.get("vanilla_lwo_sha256", "")),
                    )
                    or not re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(pending.get("material2_decl_sha256", "")),
                    )
                ):
                    raise ValueError(
                        f"{asset.key}: invalid pending Model Importer contract"
                    )
                allowlist = tuple(slot["vanilla_reference_allowlist"])
                if (
                    asset.usage_policy != "removed_vanilla_entity_allowlist"
                    or not allowlist
                    or len(allowlist) != len(set(allowlist))
                ):
                    raise ValueError(
                        f"{asset.key}: invalid pending importer allowlist"
                    )
                continue
            required_slot = {
                "model_path", "resource_archive", "material2",
                "import_bundle", "asset_id", "streamdb_payload",
                "resource_payload_sha256", "streamdb_payload_sha256",
                "provenance",
            }
            if not required_slot <= set(slot):
                raise ValueError(
                    f"{asset.key}: incomplete safe resident replacement slot"
                )
            asset_id = str(slot["asset_id"])
            model_path = Path(str(slot["model_path"]))
            expected_payload = (
                model_path.parent
                / f"{model_path.stem}_id#{asset_id}{model_path.suffix}"
            ).as_posix()
            if (
                not asset_id.isdecimal()
                or model_path.as_posix() != asset.model
                or model_path.suffix.lower() != ".lwo"
                or model_path.is_absolute()
                or ".." in model_path.parts
                or slot["resource_archive"] != asset.resource_base
                or slot["streamdb_payload"] != expected_payload
                or slot["import_bundle"]
                != f"{model_path.stem}_id#{asset_id}"
            ):
                raise ValueError(
                    f"{asset.key}: incoherent Model Importer asset identity"
                )
            provenance = slot["provenance"]
            if (
                not isinstance(provenance, Mapping)
                or provenance.get("producer")
                != "Doom Eternal Model Importer v1.2"
                or not re.fullmatch(
                    r"[0-9a-f]{64}", str(provenance.get("source_obj_sha256", ""))
                )
                or not re.fullmatch(
                    r"[0-9a-f]{64}", str(slot["resource_payload_sha256"])
                )
                or not re.fullmatch(
                    r"[0-9a-f]{64}", str(slot["streamdb_payload_sha256"])
                )
            ):
                raise ValueError(
                    f"{asset.key}: missing Model Importer provenance"
                )
            allowlist = tuple(slot.get("vanilla_reference_allowlist", ()))
            if asset.usage_policy == "no_vanilla_entity_references":
                if allowlist:
                    raise ValueError(
                        f"{asset.key}: zero-reference policy has an allowlist"
                    )
            elif asset.usage_policy == "removed_vanilla_entity_allowlist":
                if not allowlist or len(allowlist) != len(set(allowlist)):
                    raise ValueError(
                        f"{asset.key}: invalid vanilla reference allowlist"
                    )
            else:
                raise ValueError(f"{asset.key}: invalid usage_policy")
    active_goals = [
        publisher
        for publisher in catalog.publishers
        if any(effect["strategy"] == "campaign_goal" for effect in publisher.effects)
    ]
    if len(active_goals) != 1:
        raise ValueError("exactly one campaign goal publisher is required")
    for publisher in catalog.publishers:
        for trigger in publisher.triggers:
            if trigger["strategy"] not in TRIGGER_STRATEGIES:
                raise ValueError(f"unsupported publisher trigger: {trigger['strategy']}")
        for effect in publisher.effects:
            if effect["strategy"] not in EFFECT_STRATEGIES:
                raise ValueError(f"unsupported publisher effect: {effect['strategy']}")


def discover_maps(root: Path = ROOT) -> tuple[MapSpec, ...]:
    return load_content_catalog(root).enabled_maps()
