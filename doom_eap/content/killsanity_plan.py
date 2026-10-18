"""Kill rewards with stable threshold addresses and a finite campaign budget."""
import json
from copy import deepcopy
from importlib.resources import files
from pathlib import Path

KILLSANITY_REVISION = 4
KILLSANITY_ADDRESS_BASE = 7800000
MAXIMUM_MILESTONES = 65535
MAXIMUM_THRESHOLD = 2147483647 - KILLSANITY_ADDRESS_BASE
AUTO_MAXIMUM_MILESTONES = 50


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
        identity, branches = group['source_id'], group['branches']
        if (not isinstance(identity, str) or not identity or identity in rows or not isinstance(branches, list)
                or len(branches) < 2 or any(not isinstance(branch, list) or not branch for branch in branches)):
            raise ValueError('Invalid alternative campaign source group')
        members = [member for branch in branches for member in branch]
        if (any(not isinstance(member, str) or member not in rows for member in members)
                or len(members) != len(set(members))):
            raise ValueError('Alternative campaign sources are missing or repeated')
        access = {key: value for key, value in rows[members[0]].items() if key not in {'source_id', 'count'}}
        if any({key: value for key, value in rows[member].items() if key not in {'source_id', 'count'}} != access
               for member in members):
            raise ValueError('Alternative campaign sources have different access requirements')
        count = min(sum(rows[member]['count'] for member in branch) for branch in branches)
        for member in members:
            del rows[member]
        rows[identity] = {**access, 'source_id': identity, 'count': count}
    return rows


def build_campaign_kill_budget(campaign, enemy_plan, *, catalog=None):
    if catalog is None:
        path = files(__package__).joinpath("campaign_kill_sources.json")
        if not path.is_file():
            path = Path(__file__).resolve().parents[2] / "data/campaign_kill_sources.json"
        catalog = json.loads(path.read_text(encoding="utf-8"))
    if catalog.get("schema_version") != 2 or not isinstance(catalog.get("maps"), dict):
        raise ValueError("Unsupported finite campaign source catalog")
    missing = [key for key in campaign["stage_ids"]
               if type(catalog["maps"].get(key, {}).get("estimated_kills")) is not int]
    if missing:
        raise ValueError("Campaign kill estimate is missing for: " + ", ".join(missing))
    result, estimated_kills = [], 0
    for key in campaign["stage_ids"]:
        original = catalog["maps"][key]
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
        collapsed = collapse_source_alternatives({row['source_id']: row for row in sources
                                                 if row['source_id'] not in groups}, alternatives)
        original_alternatives = sum(collapsed[identity]['count'] for identity in groups)
        if any(collapsed[row['source_id']]['count'] != row['count'] for row in sources if row['source_id'] in groups):
            raise ValueError("Alternative campaign source counts differ from their initial cohort")
        source_ids = {source["source_id"] for source in sources}
        progression = original.get("progression_source_ids", sorted(source_ids))
        if (not isinstance(progression, list) or any(not isinstance(identity, str) for identity in progression)
                or len(progression) != len(set(progression)) or set(progression) - source_ids):
            raise ValueError("Invalid guaranteed campaign source selection")
        progression = set(progression)
        conditional = original.get("conditional_managers", [])
        if (not isinstance(conditional, list) or any(not isinstance(name, str) or not name or '/' in name for name in conditional)
                or len(conditional) != len(set(conditional))):
            raise ValueError("Invalid conditional campaign source managers")
        conditional = set(conditional)
        if (any(source["map_key"] != key for source in sources)
                or not sum(source["count"] for source in sources if source["source_id"] not in members
                           and source["source_id"].split('/')[0] not in conditional)
                <= original["estimated_kills"] <= 0xffffffffffffffff):
            raise ValueError(f"Campaign kill estimate is smaller than its guaranteed sources: {key}")
        estimated_kills += original['estimated_kills']
        planned = (enemy_plan or {}).get("maps", {}).get(key)
        by_id = {source["source_id"]: source for source in sources}
        if planned is not None:
            if planned["source_sha256"] != original["source_sha256"]:
                raise ValueError(f"Enemy plan and finite campaign sources use different maps: {key}")
            replaced = set()
            for operation in planned["operations"]:
                identity = f"{operation['manager']['entity']}/{operation['script_index']}/{operation['event_index']}"
                if identity not in by_id or identity in replaced:
                    raise ValueError(f"Enemy replacement has no distinct reviewed finite source: {key}/{identity}")
                replaced.add(identity)
                count = sum(len(wave["instances"]) for wave in operation["waves"])
                guaranteed = operation['manager']['entity'] not in conditional
                if guaranteed and identity not in members:
                    estimated_kills += count - by_id[identity]['count']
                by_id[identity]["count"] = count
                reserve = operation.get("reserve")
                if reserve is not None and guaranteed:
                    sources.append({**by_id[identity], "source_id": f"{reserve['manager_name']}/initial",
                                    "count": reserve["desired_count"]})
                    branch = next((branch for branch in branches if identity in branch), None)
                    if branch is not None:
                        branch.append(f"{reserve['manager_name']}/initial")
                    elif identity in progression:
                        progression.add(f"{reserve['manager_name']}/initial")
                    if identity not in members:
                        estimated_kills += reserve['desired_count']
        collapsed = collapse_source_alternatives({row['source_id']: row for row in sources
                                                 if row['source_id'] not in groups}, alternatives)
        estimated_kills += sum(collapsed[identity]['count'] for identity in groups) - original_alternatives
        sources = list(collapsed.values())
        result.extend(source for source in sources if source["count"] and source['source_id'] in progression
                      and source['source_id'].split('/')[0] not in conditional)
    return dict(estimated_kills=estimated_kills, finite_sources=_finite_sources(result, allow_empty=True))


def milestone_name(threshold):
    return f"Killed {threshold} Enemies"


def _finite_sources(finite_sources, *, allow_empty=False, allow_zero=False):
    if not isinstance(finite_sources, list) or not finite_sources and not allow_empty:
        raise ValueError("Killsanity requires the final finite campaign sources")
    sources = []
    seen = set()
    for source in finite_sources:
        required = {"source_id", "map_key", "count", "repeatable"}
        if (not isinstance(source, dict) or not required <= source.keys()
                or source.keys() - required - {"location", "region", "required_items"}):
            raise ValueError("Killsanity finite source fields are invalid")
        if (not isinstance(source["source_id"], str) or not source["source_id"]
                or not isinstance(source["map_key"], str) or not source["map_key"]
                or type(source["count"]) is not int or source["count"] < (0 if allow_zero else 1)
                or type(source["repeatable"]) is not bool):
            raise ValueError("Killsanity finite source identity or count is invalid")
        if "location" in source and "region" in source:
            raise ValueError("Killsanity finite source needs one access point")
        for field in ("location", "region"):
            if field in source and (not isinstance(source[field], str) or not source[field]):
                raise ValueError("Killsanity finite source access point is invalid")
        items = source.get("required_items", [])
        if (not isinstance(items, list) or any(not isinstance(item, str) or not item for item in items)
                or len(items) != len(set(items))):
            raise ValueError("Killsanity finite source required items are invalid")
        identity = (source["map_key"], source["source_id"])
        if identity in seen:
            raise ValueError("Killsanity finite sources count one source twice")
        seen.add(identity)
        sources.append({**source, **({"required_items": sorted(items)} if "required_items" in source else {})})
    sources.sort(key=lambda source: (source["map_key"], source["source_id"]))
    return sources


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
    sources = _finite_sources(finite_sources, allow_empty=estimated_kills is not None) if enabled else []
    finite_kills = sum(source["count"] for source in sources)
    estimate = (finite_kills if estimated_kills is None else estimated_kills) if enabled else 0
    if type(estimate) is not int or not finite_kills <= estimate <= 0xffffffffffffffff:
        raise ValueError('Campaign kill estimate must cover its guaranteed sources and fit the native counter')
    count = (min(AUTO_MAXIMUM_MILESTONES, estimate * 110 // (interval * 100)) if maximum == "auto" else maximum) if enabled else 0
    if count * interval > MAXIMUM_THRESHOLD:
        raise ValueError(f"Killsanity's final threshold exceeds {MAXIMUM_THRESHOLD}. "
                         "Reduce Kills per Check or Kill Rewards.")
    return {"revision": KILLSANITY_REVISION, "enabled": enabled,
            "interval": interval, "maximum_milestones": count,
            "finite_kills": finite_kills, "estimated_kills": estimate, "finite_sources": sources,
            "milestones": [{"threshold": number * interval,
                            "location_id": KILLSANITY_ADDRESS_BASE + number * interval,
                            "finite_supported": number * interval <= finite_kills}
                           for number in range(1, count + 1)]}


def milestone_names(plan):
    return {milestone_name(row["threshold"]): row["location_id"] for row in plan["milestones"]}


def milestone_ids(plan):
    return frozenset(row["location_id"] for row in plan["milestones"])


def validate_killsanity_plan(options, plan):
    if not isinstance(options, dict) or not isinstance(plan, dict):
        raise ValueError("killsanity options and plan must be objects")
    expected = build_killsanity_plan(options, plan.get("finite_sources"), estimated_kills=plan.get('estimated_kills'))
    if json.dumps(plan, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ValueError("Killsanity rewards do not match the room's settings")
    return expected
