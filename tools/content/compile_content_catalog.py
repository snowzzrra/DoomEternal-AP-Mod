"""Compile the normalized content catalog into a standalone APWorld module."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from doom_eap.content.campaign_stages import campaign_stages
from doom_eap.content.content_catalog import ContentCatalog, load_content_catalog
from doom_eap.content.killsanity_plan import build_campaign_kill_budget
from doom_eap.contracts.source_bytes import first_party_text_bytes

MOD_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AP_ROOT = MOD_ROOT.parent / "Archipelago"
DEFAULT_OUTPUT = DEFAULT_AP_ROOT / "worlds" / "doometernal" / "generated_content.py"


def projected_module(source: Path, root: Path) -> bytes:
    name = source.relative_to(root).as_posix()
    notice = f"# GENERATED from {name}; regenerate: python -m tools.content.compile_content_catalog\n"
    return notice.encode("ascii") + first_party_text_bytes(source.read_bytes())


def remove_asset_projection(output, check):
    path = output / "native_enemy_assets.json"
    if not path.is_file():
        return ()
    if check:
        raise ValueError(f"component=compiled_content file={path} field=projection value=unexpected")
    path.unlink()
    return (path,)


def project_contracts(root, output, staging, payloads, write_bytes, *, check=False):
    from doom_eap.content.slot_data_contract import CATALOG_DEPENDENCIES, catalog_dependency_path, catalog_digest

    removed = remove_asset_projection(output, check)
    directory = root / "doom_eap/content"
    for name in CATALOG_DEPENDENCIES:
        source = catalog_dependency_path(directory, name)
        payload = payloads.get(source)
        if payload is None:
            payload = first_party_text_bytes(source.read_bytes())
        if name != "native_enemy_assets.json":
            write_bytes(output / name, payload)
        if staging is not None:
            write_bytes(staging / name, payload)
    for name in ("slot_data_contract.py", "recipe_codec.py"):
        payload = projected_module(root / "doom_eap/content" / name, root)
        # APWorld trusts the compiled asset digest; the Mod checks the installed table.
        write_bytes(output / name, payload.replace(b"verify_assets: bool = True", b"verify_assets: bool = False"))
        if staging is not None:
            write_bytes(staging / name, payload)
    catalog = root / "doom_eap/content/slot_catalog.json"
    payload = payloads[catalog]
    write_bytes(output / catalog.name, payload)
    if staging is not None:
        write_bytes(staging / catalog.name, payload)
    manifest = {"generated": "python -m tools.content.compile_content_catalog",
                "catalog_source": "content/maps + content/catalog + qualified native inventory",
                "catalog_digest": catalog_digest(json.loads(payload)),
                "files": projection_entries(payloads, output, staging)}
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("ascii")
    write_bytes(output / "projection_manifest.json", payload)
    write_bytes(root / "doom_eap/content/projection_manifest.json", payload)
    if staging is not None:
        write_bytes(staging / "projection_manifest.json", payload)
    return removed


def projection_entries(payloads, output, staging):
    entries = {}
    for path, payload in sorted(payloads.items()):
        if not path.is_relative_to(output):
            continue
        if staging is not None and path.is_relative_to(staging):
            continue
        entries[path.relative_to(output).as_posix()] = hashlib.sha256(payload).hexdigest()
    return entries


def comparison_bytes(path, payload, projection_root):
    if payload is None or path.is_relative_to(projection_root):
        return payload
    return first_party_text_bytes(payload)


def challenge_enemy_targets(catalog):
    from doom_eap.content.enemy_options import SPECIES_IDS

    targets = []
    for item in catalog.runtime_locations:
        target = item.data.get("enemy_targets")
        if target is None:
            continue
        species, minimum = list(target["species"]), target["minimum"]
        if (not species or len(set(species)) != len(species) or not set(species) <= set(SPECIES_IDS)
                or type(minimum) is not int or minimum < 1):
            raise ValueError(f"{item.name}: invalid enemy targets")
        targets.append(dict(name=item.name, map_key=None if item.category == "weapon_masteries" else
                            catalog.region_metadata[item.region]["mission_key"], species=species, minimum=minimum))
    return targets


def render(catalog: ContentCatalog, selected_map: str | None = None) -> str:
    physical = [item for item in catalog.physical_locations
                if not item.policy.get('legacy_alias_of') and selected_map in (None, item.map_key)]
    if selected_map is not None and selected_map not in catalog.maps:
        raise ValueError(f"unknown map key: {selected_map}")
    # --map narrows catalog discovery while output remains a complete APWorld table.
    if selected_map is not None:
        physical = [item for item in catalog.physical_locations if not item.policy.get('legacy_alias_of')]
    rows: list[tuple[str, int | None, str]] = [
        (item.name, item.location_id, item.region) for item in physical
    ]
    rows.extend(
        (item.name, item.location_id, item.region) for item in catalog.runtime_locations
    )
    route = catalog.route
    rows.extend((item["name"], None, item["region"]) for item in route.get("virtual_locations", []))
    if len({name for name, _, _ in rows}) != len(rows):
        raise ValueError("generated APWorld locations are not unique")
    by_region: dict[str, list[str]] = {}
    for name, _, region in rows:
        by_region.setdefault(region, []).append(name)

    def string(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    lines = [
        '"""GENERATED from tools/content/compile_content_catalog.py; regenerate: '
        'python -m tools.content.compile_content_catalog."""',
        "",
        "# This file has no timestamp: stable input yields a stable diff.",
    ]
    identity = json.loads((catalog.root / "data" / "content_identity.json").read_text(encoding="utf-8"))
    lines.extend(
        [
            f"CONTENT_SCHEMA_VERSION = {identity['content_schema_version']!r}",
            f"CONTENT_REVISION = {string(identity['content_revision'])}",
            f"BRIDGE_PROTOCOL_VERSION = {identity['bridge_protocol_version']!r}",
            f"COMPILER_REVISION = {identity['compiler_revision']!r}",
            f"SESSION_MOD_CONTRACT_REVISION = {identity['session_mod_contract_revision']!r}",
            f"MANIFEST_SCHEMA_VERSION = {identity['manifest_schema_version']!r}",
            f"SLOT_DATA_SCHEMA_VERSION = {identity['slot_data_schema_version']!r}",
            f"SLOT_DATA_REVISION = {string(identity['slot_data_revision'])}",
            "",
            "LOCATION_ROWS = (",
        ]
    )
    for name, code, region in rows:
        line = f"    ({string(name)}, {code!r}, {string(region)}),"
        if len(line) <= 120:
            lines.append(line)
        else:
            lines.extend(
                [
                    "    (",
                    f"        {string(name)},",
                    f"        {code!r},",
                    f"        {string(region)},",
                    "    ),",
                ]
            )
    lines.extend(
        [
            ")",
            "LOCATION_NAME_TO_ID = {name: code for name, code, _ in LOCATION_ROWS if code is not None}",
            "LOCATION_NAMES_BY_REGION = {",
        ]
    )
    for region in sorted(by_region):
        lines.append(f"    {string(region)}: (")
        lines.extend(f"        {string(name)}," for name in by_region[region])
        lines.append("    ),")
    lines.extend(
        [
            "}",
            "CAMPAIGN_REGIONS = (",
        ]
    )
    lines.extend(f"    {string(region)}," for region in route["regions"])
    lines.extend(
        [
            ")",
            "CAMPAIGN_CONNECTIONS = (",
        ]
    )
    for source, destination, entrance, condition in route["connections"]:
        lines.append(
            f"    ({string(source)}, {string(destination)}, {string(entrance)}, {dict(condition)!r}),"
        )
    lines.extend(
        [
            ")",
            "MISSION_DIFFICULTY = {",
        ]
    )
    for region in route["regions"]:
        difficulty = catalog.region_metadata.get(region, {}).get("mission_difficulty")
        if difficulty is not None:
            lines.append(f"    {string(region)}: {dict(difficulty)!r},")
    lines.extend(
        [
            "}",
            "RUNTIME_LOCATION_NAMES = frozenset({",
        ]
    )
    lines.extend(f"    {string(item.name)}," for item in catalog.runtime_locations)
    lines.extend(["})", "", "CAMPAIGN_STAGES = ("])
    lines.extend(f"    {stage!r}," for stage in campaign_stages(catalog))
    lines.extend([")", ""])
    fortress = json.loads((catalog.root / "content/catalog/fortress.json").read_text(encoding="utf-8"))
    lines.append(f"FORTRESS_POLICY = {fortress!r}")
    lines.append("FORTRESS_SPEND_GROUPS = tuple(FORTRESS_POLICY['spend_groups'])")
    lines.append("")
    from doom_eap.content.mastery_requirements import MASTERY_REQUIREMENT_DIVISORS, mastery_entries_for_profile
    from doom_eap.contracts.challenge_registry import challenge_registry_document
    masteries = challenge_registry_document(catalog)["weapon_masteries"]
    counts = {profile: {entry["signal"]["unlockable"]: entry["signal"]["rule_0_statCount"]
                        for entry in mastery_entries_for_profile(masteries, profile)}
              for profile in MASTERY_REQUIREMENT_DIVISORS}
    lines.append(f"MASTERY_REQUIREMENT_COUNTS = {counts!r}")
    lines.append("")
    targets = challenge_enemy_targets(catalog)
    lines.append(f"CHALLENGE_ENEMY_TARGETS = {targets!r}")
    lines.append("")
    neutral = {item.name for item in catalog.physical_locations
               if item.policy.get("permanent_resource_budget") is False
               and (selected_map is None or item.map_key == selected_map)}
    neutral.update(item.name for item in catalog.runtime_locations
                   if item.data.get("permanent_resource_budget") is False
                   and (selected_map is None or item.mission_key == selected_map))
    lines.append(f"RESOURCE_NEUTRAL_LOCATION_NAMES = frozenset({tuple(sorted(neutral))!r})")
    lines.append("")
    return "\n".join(lines)


def compile_kill_sources(catalog, native):
    reference = json.loads((catalog.root / 'content/catalog/killsanity_estimates.json').read_bytes())
    if reference.get('schema_version') != 1 or reference.get('quantity') != 'conservative_estimate':
        raise ValueError('Unsupported campaign kill estimate reference')
    stage_ids = [stage['id'] for stage in campaign_stages(catalog)]
    if set(reference.get('maps', {})) != set(stage_ids):
        raise ValueError('Campaign kill estimates must cover the supported mission list')
    kill_sources = {}
    for key, estimate in reference['maps'].items():
        descriptor = json.loads((catalog.root / 'content/maps' / key / 'descriptor.json').read_bytes())
        if (estimate['source_sha256'] != descriptor['source_sha256']
                or estimate.get('coverage') not in {'partial', 'protected_boss_only', 'unreviewed'}
                or not isinstance(estimate.get('basis'), str) or not estimate['basis']):
            raise ValueError(f'Campaign kill estimate has invalid provenance: {key}')
        kill_sources[key] = dict(source_sha256=estimate['source_sha256'], estimated_kills=estimate['estimated_kills'],
            coverage=estimate['coverage'], basis=estimate['basis'], sources=estimate.get('guaranteed_sources', []),
            progression_source_ids=[row['source_id'] for row in estimate.get('guaranteed_sources', [])])
        if estimate.get('source_alternatives'):
            kill_sources[key]['source_alternatives'] = estimate['source_alternatives']
    from doom_eap.content.native_enemy_plan import native_event_identity
    secret_locations = {(item.map_key, item.policy.get("manager", item.policy.get("manager_entity"))): item.name
                        for item in catalog.physical_locations if item.strategy == "secret_encounter"}
    for key, estimate in reference["maps"].items():
        original = kill_sources[key]
        by_id = {row["source_id"]: row for row in original["sources"]}
        alternatives = {member: group['source_id'] for group in original.get('source_alternatives', [])
                        for branch in group['branches'] for member in branch}
        native_sources = {native_event_identity(row): row for row in native["maps"][key]["origins"] if "binding" in row}
        seen_events = set()
        for location, events in estimate.get("event_sources", {}).items():
            for identity, count in events.items():
                if identity in seen_events:
                    raise ValueError(f"Kill estimate repeats an event: {key}/{identity}")
                seen_events.add(identity)
                source = native_sources.get(identity)
                if source is None or source.get("protected_reason"):
                    raise ValueError(f"Kill estimate refers to an absent or protected native event: {key}/{identity}")
                secret = secret_locations.get((key, identity.split('/')[0]))
                if secret is not None and location != secret:
                    raise ValueError(f"Kill estimate uses the wrong Secret Encounter: {key}/{identity}")
                if type(count) is not int or count < 1:
                    raise ValueError(f"Kill estimate has an invalid event count: {key}/{identity}")
                if identity in estimate.get('alternative_source_ids', []) and identity not in alternatives:
                    raise ValueError(f'Kill estimate needs an alternative-source plan: {key}/{identity}')
                if identity not in by_id:
                    by_id[identity] = {"source_id": identity, "map_key": key, "count": count, "repeatable": True}
                    if identity in alternatives:
                        by_id[identity].update({field: value for field, value in by_id[alternatives[identity]].items()
                                               if field not in {'source_id', 'count'}})
                row = by_id[identity]
                if row["count"] != count:
                    raise ValueError(f"Guaranteed source and event counts differ: {key}/{identity}")
                if not {"location", "region"}.intersection(row):
                    row["location"] = location
        original["sources"] = list(by_id.values())
        if estimate.get("conditional_managers"):
            original["conditional_managers"] = estimate["conditional_managers"]
    kill_catalog = dict(schema_version=2, quantity='conservative_estimate', maps=kill_sources)
    build_campaign_kill_budget(dict(stage_ids=stage_ids), None, catalog=kill_catalog)
    return kill_catalog


def _prebuilt_enemy_catalogs(root: Path) -> tuple[dict, dict]:
    from doom_eap.content.vanilla_enemy_profiles import CANONICAL_ORDER
    from tools.release.prebuilt_room_resources import get_frozen_bundle_dir

    provenance = json.loads((get_frozen_bundle_dir(root) / "ROOM_RESOURCES_PROVENANCE.json").read_bytes())
    documents = []
    for name in ("native_enemy_catalog.json", "vanilla_enemy_profiles.json"):
        path = root / "doom_eap/content" / name
        payload = first_party_text_bytes(path.read_bytes())
        if hashlib.sha256(payload).hexdigest() != provenance["source_hashes"][path.relative_to(root).as_posix()]:
            raise ValueError(f"Committed enemy catalog differs from room resource provenance: {name}")
        documents.append(json.loads(payload))
    native, profiles = documents
    descriptors = {path.parent.name: json.loads(path.read_bytes())
                   for path in (root / "content/maps").glob("*/descriptor.json")}
    if (native.get("schema_version") != 1 or set(native["maps"]) != set(descriptors)
            or profiles.get("schema_version") != 1 or tuple(profiles.get("order", ())) != CANONICAL_ORDER
            or set(profiles["maps"]) != set(CANONICAL_ORDER)):
        raise ValueError("Committed enemy catalogs have invalid map coverage or schema")
    for key, descriptor in descriptors.items():
        if (native["maps"][key]["source_sha256"] != descriptor["source_sha256"]
                or native["maps"][key]["runtime_map"] != descriptor["runtime_map"]
                or (key in profiles["maps"]
                and profiles["maps"][key]["source_sha256"] != descriptor["source_sha256"])):
            raise ValueError(f"Committed enemy catalog source differs from descriptor: {key}")
    return native, profiles


def _slot_finite_rules(kills):
    return {key: {field: value for field, value in stage.items() if field != 'sources'}
            for key, stage in kills['maps'].items()}


def _slot_restrictions():
    from doom_eap.content import native_enemy_safety as safety

    names = ('SCRIPTED_BOSS_ACTORS', 'CINEMATIC_BOSS_ACTORS', 'FLYING_SPAWN_POINTS', 'FLYING_SUMMONS')
    result = {name.lower(): {key: sorted(value) for key, value in getattr(safety, name).items()} for name in names}
    result['flying_replacements'] = sorted(safety.FLYING_REPLACEMENTS)
    result['ground_encounters'] = [dict(map_key=key, owner=owner, species=sorted(species))
        for (key, owner), species in sorted(safety.GROUND_ENCOUNTERS.items())]
    return result


def compile_slot_catalog(catalog, native, kills, item_ids):
    from doom_eap.content.native_enemy_safety import COUNTER_REPLACEMENTS
    from doom_eap.content.slot_data_contract import (
        CATALOG_DEPENDENCIES, catalog_dependency_path, catalog_target_rows, dependency_digest, native_source_rows, source_index,
    )
    from doom_eap.contracts.challenge_registry import challenge_registry_document

    fortress = json.loads((catalog.root / 'content/catalog/fortress.json').read_bytes())
    roles = sorted(('primary', 'compatibility', 'counter'))
    timers = json.loads((catalog.root / 'content/catalog/secret_encounter_timers.json').read_bytes())
    source_index(native_source_rows(native, roles) + catalog_target_rows(kills, 'sources', 'source_id')
                 + catalog_target_rows(timers, 'timers', 'entity'))
    directory = catalog.root / 'doom_eap/content'
    dependencies = {name: dependency_digest(catalog_dependency_path(directory, name).read_bytes())
                    for name in CATALOG_DEPENDENCIES}
    return dict(format=2, dependencies=dependencies, items=sorted(set(item_ids.values())), roles=roles,
        finite_source_rules=_slot_finite_rules(kills),
        masteries={row['signal']['unlockable']: row for row in challenge_registry_document(catalog)['weapon_masteries']},
        counter_rules=[dict(species=species, replacement=replacement, action=action)
                       for species, replacement, action in sorted(COUNTER_REPLACEMENTS)],
        runtime_restrictions=_slot_restrictions(),
        challenge_enemy_targets=challenge_enemy_targets(catalog),
        spend_groups={group['id']: group['cost'] for group in fortress['spend_groups']},
        battery_surplus_percent=fortress['battery_surplus_percent'])


def _compile_catalog(
    output: Path = DEFAULT_OUTPUT,
    *,
    output_root: Path | None = None,
    check: bool = False,
    map_key: str | None = None,
    source_only: bool = False,
    archipelago_source: Path = DEFAULT_AP_ROOT,
) -> tuple[Path, ...]:
    catalog = load_content_catalog()
    rendered = render(catalog, map_key)
    changed_paths: list[Path] = []
    projection_payloads: dict[Path, bytes] = {}

    def write_artifact(path: Path, payload: str) -> None:
        encoded = payload.encode("utf-8")
        projection_payloads[path] = encoded
        current = comparison_bytes(path, path.read_bytes(), output.parent) if path.exists() else b""
        if check:
            if current != encoded:
                raise ValueError(f"component=compiled_content file={path} field=synchronization value=stale")
        elif current != encoded:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8", newline="\n")
            changed_paths.append(path)

    def write_bytes_artifact(path: Path, payload: bytes) -> None:
        payload = first_party_text_bytes(payload)
        projection_payloads[path] = payload
        current = comparison_bytes(path, path.read_bytes(), output.parent) if path.is_file() else None
        if check:
            if current != payload:
                raise ValueError(
                    f"component=compiled_content file={path} field=synchronization value=stale"
                )
        elif current != payload:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            changed_paths.append(path)

    write_artifact(output, rendered)
    setup_guide = (catalog.root / "docs/INSTALL.md").read_text(encoding="utf-8")
    setup_guide = setup_guide.replace("(GAMEPLAY.md)", "(https://github.com/snowzzrra/DoomEternal-AP-Mod/blob/main/docs/GAMEPLAY.md)")
    write_artifact(output.parent / "docs/setup_en.md", setup_guide)
    write_bytes_artifact(output.with_name("enemy_options.py"),
                         (catalog.root / "doom_eap/content/enemy_options.py").read_bytes())
    write_bytes_artifact(output.with_name("fortress_economy.py"),
                         (catalog.root / "doom_eap/content/fortress_economy.py").read_bytes())
    write_bytes_artifact(output.with_name("enemy_plan.py"),
                         (catalog.root / "doom_eap/content/enemy_plan.py").read_bytes())
    write_bytes_artifact(output.with_name("native_enemy_plan.py"),
                         (catalog.root / "doom_eap/content/native_enemy_plan.py").read_bytes())
    write_bytes_artifact(output.with_name("native_enemy_balance.py"),
                         (catalog.root / "doom_eap/content/native_enemy_balance.py").read_bytes())
    write_bytes_artifact(output.with_name("native_enemy_safety.py"),
                         (catalog.root / "doom_eap/content/native_enemy_safety.py").read_bytes())
    targets = json.dumps(challenge_enemy_targets(catalog), indent=2) + "\n"
    write_artifact(output.with_name("challenge_enemy_targets.json"), targets)
    write_artifact(catalog.root / "doom_eap/content/challenge_enemy_targets.json", targets)
    write_bytes_artifact(output.with_name("vanilla_enemy_profiles.py"),
                         (catalog.root / "doom_eap/content/vanilla_enemy_profiles.py").read_bytes())
    from tools.maps.native_enemy_inventory import build_native_recipe_catalog
    from tools.maps.vanilla_enemy_profiles import build_vanilla_profiles

    if source_only and not (catalog.root / "vanillamaps").exists():
        native, profiles = _prebuilt_enemy_catalogs(catalog.root)
    else:
        native = build_native_recipe_catalog(catalog.root)
        profiles = build_vanilla_profiles(catalog.root, native)
    native_catalog = json.dumps(native, sort_keys=True, separators=(",", ":")) + "\n"
    write_artifact(output.with_name("native_enemy_catalog.json"), native_catalog)
    write_artifact(catalog.root / "doom_eap/content/native_enemy_catalog.json", native_catalog)
    vanilla_profiles = json.dumps(profiles,
                                 sort_keys=True, separators=(",", ":")) + "\n"
    write_artifact(output.with_name("vanilla_enemy_profiles.json"), vanilla_profiles)
    write_artifact(catalog.root / "doom_eap/content/vanilla_enemy_profiles.json", vanilla_profiles)
    write_bytes_artifact(output.with_name("enemy_species.py"),
                         (catalog.root / "doom_eap/content/enemy_species.py").read_bytes())
    write_bytes_artifact(output.with_name("enemy_species.json"),
                         (catalog.root / "content/catalog/enemy_species.json").read_bytes())
    write_bytes_artifact(catalog.root / "doom_eap/content/enemy_species.json",
                         (catalog.root / "content/catalog/enemy_species.json").read_bytes())
    write_bytes_artifact(output.with_name("secret_encounter_timers.py"),
                         (catalog.root / "doom_eap/content/secret_encounter_timers.py").read_bytes())
    write_bytes_artifact(output.with_name("secret_encounter_timers.json"),
                         (catalog.root / "content/catalog/secret_encounter_timers.json").read_bytes())
    write_bytes_artifact(catalog.root / "doom_eap/content/secret_encounter_timers.json",
                         (catalog.root / "content/catalog/secret_encounter_timers.json").read_bytes())
    write_bytes_artifact(output.with_name("killsanity_plan.py"),
                         (catalog.root / "doom_eap/content/killsanity_plan.py").read_bytes())
    kill_catalog = compile_kill_sources(catalog, native)
    kill_sources_payload = json.dumps(kill_catalog, indent=2) + "\n"
    write_artifact(output.with_name("campaign_kill_sources.json"), kill_sources_payload)
    write_artifact(catalog.root / "doom_eap/content/campaign_kill_sources.json", kill_sources_payload)
    write_artifact(catalog.root / "data" / "campaign_stages.json",
                   json.dumps(campaign_stages(catalog), indent=2) + "\n")
    from tools.validation.validate_data import extract_item_classifications, extract_namedtuple_table
    item_source = archipelago_source / "worlds/doometernal/items.py"
    item_ids = extract_namedtuple_table(item_source, "item_data_table")
    classifications = extract_item_classifications(item_source)
    write_artifact(catalog.root / "data/item_classifications.json", json.dumps({
        "schema_version": 1, "item_mapping_revision": 9,
        "source": "Archipelago/worlds/doometernal/items.py",
        "source_sha256": hashlib.sha256(first_party_text_bytes(item_source.read_bytes())).hexdigest(),
        "items": {str(code): {"name": name, "classification": classifications[code]}
                  for name, code in sorted(item_ids.items(), key=lambda pair: pair[1])},
    }, indent=2, ensure_ascii=False) + "\n")

    if output_root is None:
        output_root = MOD_ROOT / "build" / "staging" / "runtime_catalog"

    from doom_eap.content.slot_data_contract import canonical_bytes

    slot_catalog = compile_slot_catalog(catalog, native, kill_catalog, item_ids)
    write_bytes_artifact(catalog.root / "doom_eap/content/slot_catalog.json", canonical_bytes(slot_catalog) + b"\n")
    changed_paths.extend(project_contracts(catalog.root, output.parent, output_root, projection_payloads,
                                         write_bytes_artifact, check=check))

    if source_only:
        return tuple(changed_paths)

    staging_gen = output_root / "generated_content.py"
    write_artifact(staging_gen, rendered)

    for json_name in ["content_identity.json", "items.json", "item_replay_policies.json", "campaign_stages.json"]:
        src = catalog.root / "data" / json_name
        if src.exists():
            content_bytes = src.read_bytes()
            dst = output_root / json_name
            write_bytes_artifact(dst, content_bytes)

    from doom_eap.content.automap_visual_registry import build_authorial_registry
    from doom_eap.content.map_registry import _catalog_registry
    from doom_eap.contracts.challenge_registry import challenge_registry_document
    from doom_eap.contracts.publisher_contracts import publisher_contracts_document

    compiled_sources = _catalog_registry(catalog.root)

    generated_data = {
        "physical_pickups.json": {
            "schema": 1,
            "maps": {
                spec.runtime_map: sorted(item.location_id for item in catalog.physical_locations
                                         if item.map_key == spec.key and item.strategy != "secret_encounter"
                                         and not item.policy.get('legacy_alias_of'))
                for spec in catalog.maps.values() if spec.enabled
            },
        },
        "map_sources.json": compiled_sources,
        "location_names.json": {
            "schema_version": 1,
            "source": "content/maps/*/locations.json + runtime.json",
            "locations": {
                str(item.location_id): item.name for item in (*catalog.physical_locations, *catalog.runtime_locations)
            },
        },
        "runtime_locations.json": {item.name: item.location_id for item in catalog.runtime_locations},
        "challenge_location_registry.json": challenge_registry_document(catalog),
        "publisher_contracts.json": publisher_contracts_document(catalog.publishers),
        "campaign_goal_contract.json": dict(catalog.campaign_goal),
    }
    for name, value in generated_data.items():
        rendered_json = json.dumps(value, indent=2, sort_keys=True) + "\n"
        write_artifact(catalog.root / "data" / name, rendered_json)
        write_artifact(output_root / name, rendered_json)
    for spec in catalog.maps.values():
        locations = json.loads(spec.level_config_path.read_text(encoding="utf-8"))
        manifest = dict(locations.get("entities", {}))
        manifest.update(
            {
                item.get("ap_check", f"AP_CHECK_SECRET_{item['location_id']}"): item["location_id"]
                for item in locations.get("secret_encounters", [])
            }
        )
        write_artifact(
            catalog.root / "manifests" / f"{spec.key}.json",
            json.dumps(manifest, indent=4) + "\n",
        )

    visuals_rendered = json.dumps(build_authorial_registry(catalog.root), indent=2, sort_keys=True) + "\n"
    for path in (catalog.root / "data" / "checked_location_visuals.json", output_root / "checked_location_visuals.json"):
        write_artifact(path, visuals_rendered)

    from doom_eap.contracts.foundation import load_foundation_contracts

    compiled_foundation = load_foundation_contracts(authorial=True)
    foundation_rendered = json.dumps(compiled_foundation, indent=2, sort_keys=True) + "\n"
    for dst_path in [output_root / "foundation_contracts.json", catalog.root / "data" / "foundation_contracts.json"]:
        write_artifact(dst_path, foundation_rendered)

    identity_src = catalog.root / "data" / "content_identity.json"
    identity_data = json.loads(identity_src.read_text(encoding="utf-8")) if identity_src.exists() else {}

    catalog_dict = {
        "maps": list(catalog.maps.keys()),
        "physical_locations_count": len(catalog.physical_locations),
        "revision": identity_data.get("content_revision", "v0.3.5-alpha.2"),
        "runtime_locations_count": len(catalog.runtime_locations),
        "schema_version": identity_data.get("content_schema_version", 2),
    }
    catalog_rendered = json.dumps(catalog_dict, indent=2, sort_keys=True) + "\n"
    staging_catalog = output_root / "catalog.json"
    write_artifact(staging_catalog, catalog_rendered)

    return tuple(changed_paths)


def materialize_catalog(
    output: Path = DEFAULT_OUTPUT,
    *,
    output_root: Path | None = None,
    map_key: str | None = None,
    source_only: bool = False,
    archipelago_source: Path = DEFAULT_AP_ROOT,
) -> tuple[Path, ...]:
    return _compile_catalog(
        output,
        output_root=output_root,
        map_key=map_key,
        source_only=source_only,
        archipelago_source=archipelago_source,
    )


def check_catalog(
    output: Path = DEFAULT_OUTPUT,
    *,
    output_root: Path | None = None,
    map_key: str | None = None,
    source_only: bool = False,
    archipelago_source: Path = DEFAULT_AP_ROOT,
) -> None:
    _compile_catalog(
        output,
        output_root=output_root,
        check=True,
        map_key=map_key,
        source_only=source_only,
        archipelago_source=archipelago_source,
    )


def compile_catalog(
    output: Path = DEFAULT_OUTPUT,
    *,
    output_root: Path | None = None,
    check: bool = False,
    map_key: str | None = None,
    source_only: bool = False,
    archipelago_source: Path = DEFAULT_AP_ROOT,
) -> bool:
    return bool(
        _compile_catalog(
            output,
            output_root=output_root,
            check=check,
            map_key=map_key,
            source_only=source_only,
            archipelago_source=archipelago_source,
        )
    )


def _print_changed_paths(changed_paths: tuple[Path, ...], roots: dict[str, Path]) -> None:
    groups: dict[str, list[str]] = {}
    for path in changed_paths:
        path = path.resolve()
        for label, root in roots.items():
            if path.is_relative_to(root.resolve()):
                groups.setdefault(label, []).append(path.relative_to(root.resolve()).as_posix())
                break
    for label, paths in groups.items():
        print(f"{label}:")
        for path in paths:
            print(f"  {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--map", dest="map_key")
    parser.add_argument("--archipelago-source", type=Path, default=DEFAULT_AP_ROOT,
                        help="Archipelago fork root (default: sibling Archipelago checkout)")
    parser.add_argument("--output", type=Path, help="Generated module path (default: inside --archipelago-source)")
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--source-only", action="store_true")
    args = parser.parse_args()
    item_source = args.archipelago_source / "worlds/doometernal/items.py"
    if not item_source.is_file():
        parser.error(f"APWorld source not found: {item_source}; set --archipelago-source to the Archipelago fork root")
    output = args.output or args.archipelago_source / "worlds/doometernal/generated_content.py"
    changed_paths = _compile_catalog(
        output,
        output_root=args.output_root,
        check=args.check,
        map_key=args.map_key,
        source_only=args.source_only,
        archipelago_source=args.archipelago_source,
    )
    print("updated" if changed_paths else "up-to-date")
    _print_changed_paths(changed_paths, {
        "Mod": MOD_ROOT,
        "APWorld": args.archipelago_source,
        "APWorld output": output.parent,
        "Runtime output": args.output_root or MOD_ROOT / "build/staging/runtime_catalog",
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
