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


def load_content_catalog(root: Path = ROOT) -> ContentCatalog:
    map_source_data = _json(root / "data" / "map_sources.json")
    package_by_key = {
        package["descriptor"]["key"]: package
        for package in _map_content_packages(root)
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
            "source_owner": f"vanillamaps/{descriptor['source_file']}",
            "onboarding_status": "package",
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
            ))
        for encounter in config.get("secret_encounters", []):
            location_id = encounter["location_id"]
            physical.append(PhysicalLocationSpec(
                "", location_id, key, encounter.get("ap_check", f"AP_CHECK_SECRET_{location_id}"),
                default_region, "secret_encounter", _freeze(encounter),
            ))

    names = {int(location_id): name for location_id, name in _json(root / "data" / "location_names.json")["locations"].items()}
    physical = [PhysicalLocationSpec(names.get(p.location_id, ""), p.location_id, p.map_key, p.ap_check, p.region, p.strategy, p.policy) for p in physical]
    challenge_registry = _json(root / "data" / "challenge_location_registry.json")
    runtime: list[RuntimeLocationSpec] = []
    challenges: list[ChallengeSpec] = []
    for entry in [*challenge_registry.get("mission_complete", []), *challenge_registry.get("weapon_masteries", []), *challenge_registry.get("mission_challenges", []), *challenge_registry.get("all_mission_challenges", [])]:
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
        item = RuntimeLocationSpec(entry["name"], entry["location_id"], strategy, mission_key, signal, _freeze(entry))
        runtime.append(item)
        if entry in challenge_registry.get("mission_challenges", []) or entry in challenge_registry.get("weapon_masteries", []) or entry in challenge_registry.get("all_mission_challenges", []):
            challenges.append(ChallengeSpec(item.name, item.location_id, mission_key, strategy, signal))
    for key, package in package_by_key.items():
        for entry in package["runtime"].get("locations", []):
            signal = _freeze(entry.get("signal", {}))
            item = RuntimeLocationSpec(
                entry["name"], entry["location_id"], entry["strategy"], key,
                signal, _freeze(entry),
            )
            runtime.append(item)

    goal = _freeze(_json(root / "data" / "campaign_goal_contract.json"))
    publishers = list(load_publisher_contracts(root / "data" / "publisher_contracts.json"))
    for package in package_by_key.values():
        publishers.extend(publisher_contracts_from_document(
            package["publishers"], allow_empty=True
        ))
    catalog = ContentCatalog(root, MappingProxyType(maps), tuple(physical), tuple(runtime), tuple(challenges), tuple(publishers), tuple(assets), MappingProxyType(names), goal)
    validate_content_catalog(catalog)
    return catalog


def validate_content_catalog(catalog: ContentCatalog) -> None:
    ids = [spec.location_id for spec in (*catalog.physical_locations, *catalog.runtime_locations)]
    names = [spec.name for spec in (*catalog.physical_locations, *catalog.runtime_locations)]
    if len(ids) != len(set(ids)) or not all(isinstance(value, int) and not isinstance(value, bool) for value in ids):
        raise ValueError("content catalog location IDs must be unique integers")
    if any(not name for name in names) or len(names) != len(set(names)):
        raise ValueError("content catalog location names must be unique")
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
