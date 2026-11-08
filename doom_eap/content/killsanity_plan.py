"""Resolve kill rewards and finite campaign budgets."""
import json
from copy import deepcopy
from importlib.resources import files

KILLSANITY_ADDRESS_BASE = 7800000
KILLSANITY_REVISION = 4
MAXIMUM_MILESTONES = 65535
AUTO_MAXIMUM_MILESTONES = 50
MAXIMUM_THRESHOLD = 2147483647 - KILLSANITY_ADDRESS_BASE


def _collapse_source_alternatives_branches(group, rows):
    identity, branches = group['source_id'], group['branches']
    if (not isinstance(identity, str) or not identity or identity in rows or not isinstance(branches, list)
            or len(branches) < 2 or any(not isinstance(branch, list) or not branch for branch in branches)):
        raise ValueError('Invalid alternative campaign source group')
    return branches, identity


def _collapse_source_alternatives_members(branches, rows):
    members = [member for branch in branches for member in branch]
    if (any(not isinstance(member, str) or member not in rows for member in members)
            or len(members) != len(set(members))):
        raise ValueError('Alternative campaign sources are missing or repeated')
    access = {key: value for key, value in rows[members[0]].items() if key not in {'source_id', 'count'}}
    return access, members


def _collapse_source_alternatives_access(branches, rows):
    access, members = _collapse_source_alternatives_members(branches, rows)
    if any({key: value for key, value in rows[member].items() if key not in {'source_id', 'count'}} != access
           for member in members):
        raise ValueError('Alternative campaign sources have different access requirements')
    count = min(sum(rows[member]['count'] for member in branch) for branch in branches)
    for member in members:
        del rows[member]
    return access, count


def collapse_source_alternatives(rows, alternatives):
    rows = dict(rows)
    if not isinstance(alternatives, list):
        raise ValueError('Invalid alternative campaign source groups')
    if alternatives:
        _finite_sources(list(rows.values()), allow_zero=True)
        if any(key != row['source_id'] for key, row in rows.items()):
            raise ValueError('Alternative campaign source identities differ')
    for group in alternatives:
        if not isinstance(group, dict) or not {'source_id', 'branches'} <= group.keys():
            raise ValueError('Invalid alternative campaign source group')
        branches, identity = _collapse_source_alternatives_branches(group, rows)
        access, count = _collapse_source_alternatives_access(branches, rows)
        rows[identity] = {**access, 'source_id': identity, 'count': count}
    return rows


def load_campaign_kill_sources():
    return json.loads(files(__package__).joinpath("campaign_kill_sources.json").read_bytes())


def _campaign_kill_stages(campaign, catalog):
    if catalog.get("schema_version") != 2 or not isinstance(catalog.get("maps"), dict):
        raise ValueError("Unsupported finite campaign source catalog")
    stages = [key for key in campaign["stage_ids"] if key not in campaign.get("master_level_stage_ids", ())]
    missing = [key for key in stages
               if type(catalog["maps"].get(key, {}).get("estimated_kills")) is not int]
    if missing:
        raise ValueError("Campaign kill estimate is missing for: " + ", ".join(missing))
    return stages


def _build_campaign_kill_budget_recipe(by_id, conditional, estimated_kills, members, planned):
    from .native_enemy_plan import native_event_identity
    counts = {}
    for recipe in planned["recipes"]:
        if recipe.get("runtime_only"):
            continue
        identity = native_event_identity(recipe)
        if identity in by_id:
            logical, physical = counts.get(identity, (0, 0))
            counts[identity] = logical + recipe["logical_count"], physical + recipe["physical_count"]
    for identity, (logical, physical) in counts.items():
        # host and Spirit can share one catalog event
        count = (by_id[identity]["count"] * physical + logical - 1) // logical
        if identity.split('/')[0] not in conditional and identity not in members:
            estimated_kills += count - by_id[identity]["count"]
        by_id[identity]["count"] = count
    return estimated_kills


def _build_campaign_kill_budget_planned(alternatives, conditional, enemy_plan, estimated_kills, groups, key, members, original, sources):
    estimated_kills += original['estimated_kills']
    planned = (enemy_plan or {}).get("maps", {}).get(key)
    by_id = {source["source_id"]: source for source in sources}
    if planned is not None:
        if planned["source_sha256"] != original["source_sha256"]:
            raise ValueError(f"Enemy plan and finite campaign sources use different maps: {key}")
        if enemy_plan.get("kind") == "native_recipes":
            estimated_kills = _build_campaign_kill_budget_recipe(by_id, conditional, estimated_kills, members, planned)
    collapsed = collapse_source_alternatives({row['source_id']: row for row in sources
                                             if row['source_id'] not in groups}, alternatives)
    return collapsed, estimated_kills


def _build_campaign_kill_budget_source_id(collapsed, groups, original, sources):
    if any(collapsed[row['source_id']]['count'] != row['count'] for row in sources if row['source_id'] in groups):
        raise ValueError("Alternative campaign source counts differ from their initial cohort")
    source_ids = {source["source_id"] for source in sources}
    progression = original.get("progression_source_ids", sorted(source_ids))
    if (not isinstance(progression, list) or any(not isinstance(identity, str) for identity in progression)
            or len(progression) != len(set(progression)) or set(progression) - source_ids):
        raise ValueError("Invalid guaranteed campaign source selection")
    return progression


def _build_campaign_kill_budget_collapsed(alternatives, groups, original, sources):
    collapsed = collapse_source_alternatives({row['source_id']: row for row in sources
                                             if row['source_id'] not in groups}, alternatives)
    original_alternatives = sum(collapsed[identity]['count'] for identity in groups)
    progression = _build_campaign_kill_budget_source_id(collapsed, groups, original, sources)
    progression = set(progression)
    conditional = original.get("conditional_managers", [])
    if (not isinstance(conditional, list) or any(not isinstance(name, str) or not name or '/' in name for name in conditional)
            or len(conditional) != len(set(conditional))):
        raise ValueError("Invalid conditional campaign source managers")
    return conditional, original_alternatives, progression


def _build_campaign_kill_budget_conditional(alternatives, enemy_plan, estimated_kills, groups, key, members, original, sources):
    conditional, original_alternatives, progression = _build_campaign_kill_budget_collapsed(alternatives, groups, original, sources)
    conditional = set(conditional)
    if (any(source["map_key"] != key for source in sources)
            or not sum(source["count"] for source in sources if source["source_id"] not in members
                       and source["source_id"].split('/')[0] not in conditional)
            <= original["estimated_kills"] <= 0xffffffffffffffff):
        raise ValueError(f"Campaign kill estimate is smaller than its guaranteed sources: {key}")
    collapsed, estimated_kills = _build_campaign_kill_budget_planned(alternatives, conditional, enemy_plan, estimated_kills, groups, key, members, original, sources)
    estimated_kills += sum(collapsed[identity]['count'] for identity in groups) - original_alternatives
    return collapsed, conditional, estimated_kills, progression


def _build_campaign_kill_budget_alternatives(enemy_plan, estimated_kills, key, original):
    sources = _finite_sources(original["sources"], allow_empty=True)
    alternatives = deepcopy(original.get("source_alternatives", []))
    if not isinstance(alternatives, list):
        raise ValueError("Invalid alternative campaign source groups")
    try:
        groups = {group['source_id'] for group in alternatives}
        branches = [branch for group in alternatives for branch in group['branches']]
        members = {identity for branch in branches for identity in branch}
    except (KeyError, TypeError) as error:
        raise ValueError("Invalid alternative campaign source groups") from error
    collapsed, conditional, estimated_kills, progression = _build_campaign_kill_budget_conditional(alternatives, enemy_plan, estimated_kills, groups, key, members, original, sources)
    return collapsed, conditional, estimated_kills, progression


def _build_campaign_kill_budget_stages(campaign, catalog, enemy_plan):
    stages = _campaign_kill_stages(campaign, catalog)
    result, estimated_kills, stage_estimates = [], 0, {}
    for key in stages:
        original = catalog["maps"][key]
        stage_estimate_before = estimated_kills
        collapsed, conditional, estimated_kills, progression = _build_campaign_kill_budget_alternatives(enemy_plan, estimated_kills, key, original)
        if (enemy_plan or {}).get('revision', 0) >= 5:
            estimate = enemy_plan.get('combat_costs', {}).get(key, {}).get('campaign', {}).get('estimated_kills')
            if type(estimate) is not int or not 0 <= estimate <= 0xffffffffffffffff:
                raise ValueError(f'Native campaign kill estimate is invalid: {key}')
            estimated_kills = stage_estimate_before + max(estimated_kills - stage_estimate_before, estimate)
        stage_estimates[key] = estimated_kills - stage_estimate_before
        sources = list(collapsed.values())
        result.extend(source for source in sources if source["count"] and source['source_id'] in progression
                      and source['source_id'].split('/')[0] not in conditional)
    return estimated_kills, result, stage_estimates


def build_campaign_kill_budget(campaign, enemy_plan, *, catalog=None):
    if catalog is None:
        catalog = load_campaign_kill_sources()
    estimated_kills, result, stage_estimates = _build_campaign_kill_budget_stages(campaign, catalog, enemy_plan)
    return dict(estimated_kills=estimated_kills, finite_sources=_finite_sources(result, allow_empty=True),
                stage_estimates=stage_estimates)


def _restored_stage_recipes(stage, record, tables):
    origins = {row['id']: row for row in tables['maps'][stage]['origins']
               + tables['maps'][stage].get('runtime_origins', [])}
    result = []
    for row in record.rows:
        if row.role_ref != 'primary':
            continue
        origin = origins[tables['sources'][row.source_ref]['source_id']]
        result.append({**origin, 'logical_count': row.logical_count, 'physical_count': len(row.bodies),
                       'maximum_emissions': row.maximum_emissions, 'renewable': row.renewable})
    return result


def _campaign_recipe_active(recipe):
    return not recipe.get('runtime_only') and not (
        recipe.get('layers') and all('master' in layer.lower() for layer in recipe['layers']))


def restore_campaign_kill_budget(campaign, recipes, catalog, *, revision):
    from .native_enemy_plan import _campaign_kill_estimates
    from .recipe_codec import decode_recipes
    from .slot_data_contract import catalog_tables

    if not recipes.enabled:
        return build_campaign_kill_budget(campaign, None)
    tables = catalog_tables(catalog)
    maps, costs = {}, {}
    for stage, record in decode_recipes(recipes).maps.items():
        restored = _restored_stage_recipes(stage, record, tables)
        maps[stage] = {'source_sha256': tables['maps'][stage]['source_sha256'], 'recipes': restored}
        costs[stage] = {'campaign': {}}
        active = [row for row in restored if _campaign_recipe_active(row)]
        _campaign_kill_estimates(active, costs, stage, (), False, ())
    plan = dict(kind='native_recipes', revision=revision, maps=maps, combat_costs=costs)
    return build_campaign_kill_budget(campaign, plan)


def _finite_sources_identity(source):
    for field in ("location", "region"):
        if field in source and (not isinstance(source[field], str) or not source[field]):
            raise ValueError("Killsanity finite source access point is invalid")
    items = source.get("required_items", [])
    if (not isinstance(items, list) or any(not isinstance(item, str) or not item for item in items)
            or len(items) != len(set(items))):
        raise ValueError("Killsanity finite source required items are invalid")
    identity = (source["map_key"], source["source_id"])
    return identity, items


def _finite_sources_source_id(allow_zero, source):
    if (not isinstance(source["source_id"], str) or not source["source_id"]
            or not isinstance(source["map_key"], str) or not source["map_key"]
            or type(source["count"]) is not int or source["count"] < (0 if allow_zero else 1)
            or type(source["repeatable"]) is not bool):
        raise ValueError("Killsanity finite source identity or count is invalid")


def _finite_sources_finite_sources(allow_zero, finite_sources):
    sources = []
    seen = set()
    for source in finite_sources:
        required = {"source_id", "map_key", "count", "repeatable"}
        if (not isinstance(source, dict) or not required <= source.keys()
                or source.keys() - required - {"location", "region", "required_items"}):
            raise ValueError("Killsanity finite source fields are invalid")
        _finite_sources_source_id(allow_zero, source)
        if "location" in source and "region" in source:
            raise ValueError("Killsanity finite source needs one access point")
        identity, items = _finite_sources_identity(source)
        if identity in seen:
            raise ValueError("Killsanity finite sources count one source twice")
        seen.add(identity)
        sources.append({**source, **({"required_items": sorted(items)} if "required_items" in source else {})})
    return sources


def _finite_sources(finite_sources, *, allow_empty=False, allow_zero=False):
    if not isinstance(finite_sources, list) or (not finite_sources and not allow_empty):
        raise ValueError("Killsanity requires the final finite campaign sources")
    sources = _finite_sources_finite_sources(allow_zero, finite_sources)
    sources.sort(key=lambda source: (source["map_key"], source["source_id"]))
    return sources


def _build_killsanity_plan_finite_kills(enabled, estimated_kills, finite_sources, interval, maximum):
    sources = _finite_sources(finite_sources, allow_empty=estimated_kills is not None) if enabled else []
    finite_kills = sum(source["count"] for source in sources)
    estimate = (finite_kills if estimated_kills is None else estimated_kills) if enabled else 0
    if type(estimate) is not int or not finite_kills <= estimate <= 0xffffffffffffffff:
        raise ValueError('Campaign kill estimate must cover its guaranteed sources and fit the native counter')
    count = (min(AUTO_MAXIMUM_MILESTONES, estimate * 110 // (interval * 100)) if maximum == "auto" else maximum) if enabled else 0
    if count * interval > MAXIMUM_THRESHOLD:
        raise ValueError(f"Killsanity's final threshold exceeds {MAXIMUM_THRESHOLD}. "
                         "Reduce Kills per Check or Kill Rewards.")
    return count, estimate, finite_kills, sources


def build_killsanity_plan(options, finite_sources=None, *, estimated_kills=None):
    enabled = options.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("Killsanity enabled must be a boolean")
    interval = options.get("interval", 100)
    maximum = options.get("maximum_milestones", "auto")
    if type(interval) is not int or not 1 <= interval <= 2147483647:
        raise ValueError("Kills per Check must be an integer between 1 and 2147483647")
    if maximum != "auto" and (type(maximum) is not int or not 1 <= maximum <= MAXIMUM_MILESTONES):
        raise ValueError(f"Kill Rewards must be Auto or 1..{MAXIMUM_MILESTONES}")
    count, estimate, finite_kills, sources = _build_killsanity_plan_finite_kills(enabled, estimated_kills, finite_sources, interval, maximum)
    return {"revision": KILLSANITY_REVISION, "enabled": enabled,
            "interval": interval, "maximum_milestones": count,
            "finite_kills": finite_kills, "estimated_kills": estimate, "finite_sources": sources,
            "milestones": [{"threshold": number * interval,
                            "location_id": 2_200_000_000 + number,
                            "finite_supported": number * interval <= finite_kills}
                           for number in range(1, count + 1)]}


def milestone_ids(plan):
    return frozenset(row["location_id"] for row in resolved_rewards(plan))


def resolved_rewards(plan):
    return tuple({"threshold": ordinal * plan["interval"], "location_id": 2_200_000_000 + ordinal}
                 for ordinal in range(1, plan["count"] + 1))


def build_resolved_killsanity(options, budget, catalog):
    """Resolve reward counts and finite source references at the producer boundary."""
    from .slot_data_contract import MAX_KILL_REWARDS, StructureError

    enabled = options.get('enabled', False)
    interval = options.get('interval', 100)
    count = options.get('count', 'auto')
    if type(enabled) is not bool or type(interval) is not int or not 1 <= interval <= 0x7fffffff:
        raise StructureError('Kill rewards require a boolean setting and a positive integer interval')
    if count != 'auto' and (type(count) is not int or not 1 <= count <= MAX_KILL_REWARDS):
        raise StructureError(f'Kill rewards must be Auto or 1..{MAX_KILL_REWARDS}')
    if not enabled:
        return dict(interval=0, count=0, finite_supported_count=0, finite_kills=0, estimated_kills=0, sources=[])
    return _resolved_kill_counts(interval, count, budget, catalog)


def _resolved_kill_counts(interval, count, budget, catalog):
    from .slot_data_contract import ReferenceError, StructureError, catalog_tables

    try:
        sources = _finite_sources(budget['finite_sources'], allow_empty=True)
    except ValueError as error:
        raise StructureError(str(error)) from error
    finite = sum(row['count'] for row in sources)
    estimate = budget['estimated_kills']
    if type(estimate) is not int or not finite <= estimate <= 0xffffffffffffffff:
        raise StructureError('Campaign kill estimate must cover its finite sources and fit the native counter')
    count = _resolved_reward_count(interval, count, estimate)
    if not count:
        return dict(interval=0, count=0, finite_supported_count=0, finite_kills=0, estimated_kills=0, sources=[])
    refs = {(row['stage_id'], row['source_id']): ref for ref, row in catalog_tables(catalog)['finite_sources'].items()}
    try:
        packed = sorted([refs[row['map_key'], row['source_id']], row['count']] for row in sources)
    except KeyError as error:
        raise ReferenceError(f'Finite kill source has no catalog reference: {error}') from error
    return dict(interval=interval, count=count, finite_supported_count=min(count, finite // interval),
                finite_kills=finite, estimated_kills=estimate, sources=packed)


def _resolved_reward_count(interval, count, estimate):
    from .slot_data_contract import StructureError

    count = min(50, estimate * 110 // (interval * 100)) if count == 'auto' else count
    if count * interval > MAXIMUM_THRESHOLD:
        raise StructureError('Kill reward threshold exceeds the native counter')
    return count
