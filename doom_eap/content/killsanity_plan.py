"""Kill rewards with stable threshold addresses and a finite campaign budget."""
import json

KILLSANITY_REVISION = 3
KILLSANITY_ADDRESS_BASE = 7800000
MAXIMUM_MILESTONES = 65535
MAXIMUM_THRESHOLD = 2147483647 - KILLSANITY_ADDRESS_BASE


def milestone_name(threshold):
    return f"Killed {threshold} Enemies"


def build_killsanity_plan(options, finite_sources=None):
    enabled = options.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("Killsanity enabled must be a boolean")
    interval = options.get("interval", 100)
    maximum = options.get("maximum_milestones", "auto")
    if type(interval) is not int or not 1 <= interval <= 2147483647:
        raise ValueError("Kills per Check must be an integer between 1 and 2147483647")
    if maximum != "auto" and (type(maximum) is not int or not 1 <= maximum <= MAXIMUM_MILESTONES):
        raise ValueError(f"Kill Rewards must be Auto or 1..{MAXIMUM_MILESTONES}")
    sources = []
    if enabled:
        if not isinstance(finite_sources, list) or not finite_sources:
            raise ValueError("Killsanity requires the final finite campaign sources")
        seen = set()
        for source in finite_sources:
            if not isinstance(source, dict) or set(source) != {"source_id", "map_key", "count", "repeatable"}:
                raise ValueError("Killsanity finite source fields are invalid")
            if (not isinstance(source["source_id"], str) or not source["source_id"]
                    or not isinstance(source["map_key"], str) or not source["map_key"]
                    or type(source["count"]) is not int or source["count"] < 1
                    or type(source["repeatable"]) is not bool):
                raise ValueError("Killsanity finite source identity or count is invalid")
            identity = (source["map_key"], source["source_id"])
            if identity in seen:
                raise ValueError("Killsanity finite sources count one source twice")
            seen.add(identity)
            sources.append(dict(source))
        sources.sort(key=lambda source: (source["map_key"], source["source_id"]))
    finite_kills = sum(source["count"] for source in sources)
    count = (finite_kills * 110 // (interval * 100) if maximum == "auto" else maximum) if enabled else 0
    if count > MAXIMUM_MILESTONES:
        raise ValueError(f"Auto exceeds {MAXIMUM_MILESTONES} kill rewards; increase Kills per Check")
    if count * interval > MAXIMUM_THRESHOLD:
        raise ValueError(f"Killsanity's final threshold exceeds {MAXIMUM_THRESHOLD}. "
                         "Reduce Kills per Check or Kill Rewards.")
    return {"revision": KILLSANITY_REVISION, "enabled": enabled,
            "interval": interval, "maximum_milestones": count,
            "finite_kills": finite_kills, "finite_sources": sources,
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
    expected = build_killsanity_plan(options, plan.get("finite_sources"))
    if json.dumps(plan, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ValueError("Killsanity rewards do not match the room's settings")
    return expected
