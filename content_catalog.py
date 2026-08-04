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

from ap_visual_contract import load_ap_visual_contract
from publisher_contracts import (
    EFFECT_STRATEGIES,
    TRIGGER_STRATEGIES,
    PublisherContract,
    load_publisher_contracts,
    publisher_contracts_from_document,
)


ROOT = Path(__file__).resolve().parent
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
    """Load opt-in authoring packages without changing legacy map inputs."""
    packages = []
    packages_root = root / "content" / "maps"
    if not packages_root.exists():
        return ()
    for directory in sorted(path for path in packages_root.iterdir() if path.is_dir()):
        descriptor_path = directory / "descriptor.json"
        if not descriptor_path.exists():
            continue
        descriptor = _json(descriptor_path)
        key = descriptor.get("key")
        if descriptor.get("schema_version") != 1 or key != directory.name:
            raise ValueError(
                f"component=map_package map={directory.name} file={descriptor_path} "
                f"field=key value={key!r}: descriptor schema/key mismatch"
            )
        documents = {
            name: _json(directory / f"{name}.json")
            for name in ("locations", "runtime", "publishers", "assets", "onboarding")
        }
        if any(document.get("schema_version") != 1 for document in documents.values()):
            raise ValueError(
                f"component=map_package map={key} file={directory} "
                "field=schema_version value!=1"
            )
        packages.append({
            "directory": directory,
            "descriptor": descriptor,
            **documents,
        })
    return tuple(packages)


def _normalized_route(
    root: Path,
    packages: tuple[dict[str, Any], ...],
) -> Mapping[str, Any]:
    legacy = _json(root / "data" / "campaign_route.json")
    regions = list(legacy.get("regions", []))
    connections = [list(row) for row in legacy.get("connections", [])]
    virtual_locations = [dict(row) for row in legacy.get("virtual_locations", [])]
    for package in packages:
        raw = package["descriptor"].get("route", {})
        if isinstance(raw, list):
            raw = {"connections": raw}
        if not isinstance(raw, Mapping):
            raise ValueError(
                f"{package['descriptor']['key']}: route must be an object or connection list"
            )
        for region in raw.get("regions", []):
            if region not in regions:
                regions.append(region)
        for row in raw.get("connections", []):
            normalized = list(row)
            if len(normalized) != 3:
                raise ValueError(
                    f"{package['descriptor']['key']}: route connection must have three fields"
                )
            if normalized not in connections:
                connections.append(normalized)
        for row in raw.get("virtual_locations", []):
            normalized = dict(row)
            if normalized not in virtual_locations:
                virtual_locations.append(normalized)
    known = set(regions)
    for source, destination, _rule in connections:
        if source not in known or destination not in known:
            raise ValueError(
                f"route references undeclared region: {source!r} -> {destination!r}"
            )
    return _freeze({
        "schema_version": legacy.get("schema_version", 1),
        "regions": regions,
        "connections": connections,
        "virtual_locations": virtual_locations,
    })


def load_content_catalog(root: Path = ROOT) -> ContentCatalog:
    map_source_data = _json(root / "data" / "map_sources.json")
    canonical_visual = load_ap_visual_contract(root)
    packages = _map_content_packages(root)
    package_by_key = {
        package["descriptor"]["key"]: package
        for package in packages
    }
    source_maps = dict(map_source_data["maps"])
    for key, package in package_by_key.items():
        if key in source_maps:
            raise ValueError(f"map package duplicates legacy map key: {key}")
        descriptor = dict(package["descriptor"])
        directory = package["directory"]
        resource_path = descriptor["resource_owner"]
        source_maps[key] = {
            **descriptor,
            "level_config": str(directory / "locations.json"),
            "manifest": descriptor.get("manifest", f"manifests/{key}.json"),
            "onboarding_audit": str(directory / "onboarding.json"),
            "resource_path": resource_path,
            "source_owner": descriptor.get("source_owner", descriptor["source_file"]),
            "onboarding_status": descriptor.get(
                "onboarding_status", "implementation_candidate"
            ),
            "test_only": False,
            "package_directory": str(directory.relative_to(root)),
        }
    maps: dict[str, MapSpec] = {}
    physical: list[PhysicalLocationSpec] = []
    assets: list[AssetSpec] = []
    for key, source in source_maps.items():
        config_path = Path(source["level_config"])
        if not config_path.is_absolute():
            config_path = root / config_path
        onboarding = source.get("onboarding_audit")
        onboarding_path = Path(onboarding) if onboarding else None
        if onboarding_path is not None and not onboarding_path.is_absolute():
            onboarding_path = root / onboarding_path
        spec = MapSpec(
            key, source["display_name"], source["source_file"], config_path,
            root / source["manifest"], onboarding_path,
            source["runtime_map"], source.get("resource_base", _resource_base(source["resource_path"])),
            source["resource_owner"], source["relative_entities_path"],
            bool(source.get("enabled", True)), _freeze(source),
            str(source.get("source_sha256", "")),
            int(source.get("source_size", 0)),
            str(source.get("resource_path", source["resource_owner"])),
            int(source.get("resource_priority", 0)),
            str(source.get("supported_game_revision", "")),
        )
        maps[key] = spec
        config = _json(config_path)
        policies = config.get("target_policies", {})
        regions = config.get("region_overrides", {})
        default_region = config.get("region", source["display_name"])
        for ap_check, location_id in config.get("entities", {}).items():
            entity = ap_check.removeprefix("AP_CHECK_").lower()
            policy = _freeze(policies.get(entity, {}))
            physical.append(PhysicalLocationSpec(
                "", location_id, key, ap_check, regions.get(str(location_id), default_region),
                _strategy_for_policy(policy, ap_check), policy,
            ))
        for raw_asset in config.get("assets", []):
            assets.append(AssetSpec(
                key=raw_asset["key"],
                map_key=key,
                strategy=raw_asset["strategy"],
                model=raw_asset["model"],
                resource_base=raw_asset.get("resource_base", spec.resource_base),
                resource_owner=spec.resource_owner,
                dependencies=tuple(raw_asset.get("dependencies", [])),
                dependency_policy=raw_asset.get("dependency_policy", "required"),
                donor=_freeze(raw_asset.get("donor", {})),
                replacement_slot_policy=raw_asset.get(
                    "replacement_slot_policy", ""
                ),
                replacement_slot=_freeze(raw_asset.get("replacement_slot", {})),
                usage_policy=raw_asset.get("usage_policy", ""),
                preserve=tuple(raw_asset.get("preserve", [])),
                visual_presentation_policy=_freeze(
                    raw_asset.get("visual_presentation_policy", {})
                ),
            ))
        for raw_asset in package_by_key.get(key, {}).get("assets", {}).get("assets", []):
            assets.append(AssetSpec(
                key=raw_asset["key"],
                map_key=key,
                strategy=raw_asset["strategy"],
                model=raw_asset["model"],
                resource_base=raw_asset.get("resource_base", spec.resource_base),
                resource_owner=raw_asset.get(
                    "resource_owner", spec.resource_owner
                ),
                dependencies=tuple(raw_asset.get("dependencies", [])),
                dependency_policy=raw_asset.get(
                    "dependency_policy", "required"
                ),
                donor=_freeze(raw_asset.get("donor", {})),
                replacement_slot_policy=raw_asset.get(
                    "replacement_slot_policy", ""
                ),
                replacement_slot=_freeze(raw_asset.get("replacement_slot", {})),
                usage_policy=raw_asset.get("usage_policy", ""),
                preserve=tuple(raw_asset.get("preserve", [])),
                visual_presentation_policy=_freeze(
                    raw_asset.get("visual_presentation_policy", {})
                ),
            ))
        if spec.enabled:
            slot = {
                **canonical_visual["replacement_slot"],
                "resource_archive": spec.resource_base,
            }
            assets.append(AssetSpec(
                key=canonical_visual["key"],
                map_key=key,
                strategy=canonical_visual["strategy"],
                model=canonical_visual["model"],
                resource_base=spec.resource_base,
                resource_owner=spec.resource_owner,
                dependencies=tuple(canonical_visual["dependencies"]),
                dependency_policy=canonical_visual["dependency_policy"],
                replacement_slot_policy=canonical_visual[
                    "replacement_slot_policy"
                ],
                replacement_slot=_freeze(slot),
                usage_policy=canonical_visual["usage_policy"],
                preserve=tuple(canonical_visual["preserve"]),
                visual_presentation_policy=_freeze(
                    canonical_visual["visual_presentation_policy"]
                ),
            ))
        for encounter in config.get("secret_encounters", []):
            location_id = encounter["location_id"]
            physical.append(PhysicalLocationSpec(
                "", location_id, key, encounter.get("ap_check", f"AP_CHECK_SECRET_{location_id}"),
                default_region, "secret_encounter", _freeze(encounter),
            ))

    names = {
        int(location_id): name
        for location_id, name in
        _json(root / "data" / "location_names.json")["locations"].items()
    }
    for key, package in package_by_key.items():
        package_names = package["locations"].get("names", {})
        package_ids = {
            int(location_id)
            for location_id in package["locations"].get("entities", {}).values()
        } | {
            int(entry["location_id"])
            for entry in package["locations"].get("secret_encounters", [])
        }
        normalized_names = {
            int(location_id): name
            for location_id, name in package_names.items()
        }
        if set(normalized_names) != package_ids:
            raise ValueError(
                f"{key}: package physical names must match physical location IDs"
            )
        for location_id, name in normalized_names.items():
            if location_id in names and names[location_id] != name:
                raise ValueError(
                    f"{key}: package/legacy location name divergence for {location_id}"
                )
            names[location_id] = name
    physical = [PhysicalLocationSpec(names.get(p.location_id, ""), p.location_id, p.map_key, p.ap_check, p.region, p.strategy, p.policy) for p in physical]
    challenge_registry = _json(root / "data" / "challenge_location_registry.json")
    runtime: list[RuntimeLocationSpec] = []
    challenges: list[ChallengeSpec] = []
    for category in (
        "mission_complete", "weapon_masteries",
        "mission_challenges", "all_mission_challenges",
    ):
        for entry in challenge_registry.get(category, []):
            signal = _freeze(entry.get("signal", {}))
            strategy = signal.get("kind", "")
            mission_key = entry.get("mission_key") or next(
                (
                    key for key, spec in maps.items()
                    if signal.get("runtime_map") == spec.runtime_map
                ),
                None,
            ) or (
                str(signal.get("unlockable", "")).split("/")[1]
                if str(signal.get("unlockable", "")).count("/") >= 2 else None
            )
            item = RuntimeLocationSpec(
                entry["name"], entry["location_id"], strategy, mission_key,
                signal, _freeze(entry), category,
            )
            runtime.append(item)
            if category != "mission_complete":
                challenges.append(ChallengeSpec(
                    item.name, item.location_id, mission_key, strategy, signal
                ))
    for key, package in package_by_key.items():
        for entry in package["runtime"].get("locations", []):
            signal = _freeze(entry.get("signal", {}))
            category = entry.get("category", "")
            item = RuntimeLocationSpec(
                entry["name"], entry["location_id"], entry["strategy"], key,
                signal, _freeze(entry), category,
            )
            runtime.append(item)
            if category in {
                "weapon_masteries", "mission_challenges",
                "all_mission_challenges",
            }:
                challenges.append(ChallengeSpec(
                    item.name, item.location_id, key, item.strategy, signal
                ))

    goal = _freeze(_json(root / "data" / "campaign_goal_contract.json"))
    publishers = list(load_publisher_contracts(root / "data" / "publisher_contracts.json"))
    for package in package_by_key.values():
        publishers.extend(publisher_contracts_from_document(
            package["publishers"], allow_empty=True
        ))
    reserved: dict[int, Mapping[str, Any]] = {}
    for key, package in package_by_key.items():
        for entry in package["onboarding"].get("reserved_ids", []):
            location_id = int(entry["id"])
            if location_id in reserved:
                raise ValueError(f"duplicate reserved location ID: {location_id}")
            reserved[location_id] = _freeze({**entry, "map_key": key})
    catalog = ContentCatalog(
        root, MappingProxyType(maps), tuple(physical), tuple(runtime),
        tuple(challenges), tuple(publishers), tuple(assets),
        MappingProxyType(names), goal, _normalized_route(root, packages),
        MappingProxyType(reserved),
    )
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
    for item in catalog.runtime_locations:
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
                or presentation.get("think_component") != "bob_rotate_fast"
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
