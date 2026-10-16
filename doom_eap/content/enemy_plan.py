"""Deterministic generation shared by APWorld and the content compiler."""

import hashlib
import json
import random
import re

from . import enemy_species

ENEMY_PLAN_REVISION = 1


def validate_frozen_enemy_plan(plan, enemies):
    if plan is None:
        if enemies["enabled"]:
            raise ValueError("Enemy Randomizer requires a frozen enemy generation plan")
        return None
    if not isinstance(plan, dict) or set(plan) != {
        "revision", "seed", "maps", "finite_kills", "finite_sources"
    }:
        raise ValueError("Enemy generation plan fields are invalid")
    if type(plan["revision"]) is not int or plan["revision"] != ENEMY_PLAN_REVISION:
        raise ValueError("Enemy generation plan revision is unsupported")
    if not isinstance(plan["seed"], str) or not plan["seed"]:
        raise ValueError("Enemy generation plan seed is missing")
    if type(plan["finite_kills"]) is not int or not 0 <= plan["finite_kills"] <= 0xffffffffffffffff:
        raise ValueError("Enemy generation plan finite kill total is invalid")
    maps = plan["maps"]
    if not isinstance(maps, dict) or not maps:
        raise ValueError("Enemy generation plan maps are missing")
    for key, row in maps.items():
        if not isinstance(key, str) or re.fullmatch(r"[a-z0-9_]+", key) is None:
            raise ValueError("Enemy generation plan map key is invalid")
        if not isinstance(row, dict) or set(row) != {"schema", "map_key", "source_sha256", "operations"}:
            raise ValueError(f"Enemy generation map fields are invalid: {key}")
        if type(row["schema"]) is not int or row["schema"] not in (1, 2, 3) or row["map_key"] != key:
            raise ValueError(f"Enemy generation map identity is invalid: {key}")
        if not isinstance(row["source_sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", row["source_sha256"]) is None:
            raise ValueError(f"Enemy generation map source hash is invalid: {key}")
        if not isinstance(row["operations"], list):
            raise ValueError(f"Enemy generation map operations are invalid: {key}")
        if not enemies["enabled"] and row["operations"]:
            raise ValueError("Enemizer Off cannot contain enemy replacement operations")
        try:
            for operation in row["operations"]:
                for wave in operation["waves"]:
                    for instance in wave["instances"]:
                        enemy_species.validate_enemy_speed_percent(instance["species"], instance["attributes"]["speed_percent"])
        except (KeyError, TypeError) as error:
            raise ValueError(f"Enemy generation Speed fields are malformed: {key}") from error
    sources = plan["finite_sources"]
    if not isinstance(sources, list):
        raise ValueError("Enemy generation plan finite sources are invalid")
    seen = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"map_key", "location", "count", "repeatable"}:
            raise ValueError("Enemy generation plan finite source fields are invalid")
        if not isinstance(source["map_key"], str) or source["map_key"] not in maps:
            raise ValueError("Enemy generation plan finite source map is unknown")
        if not isinstance(source["location"], str) or not source["location"]:
            raise ValueError("Enemy generation plan finite source location is missing")
        if type(source["count"]) is not int or source["count"] <= 0 or type(source["repeatable"]) is not bool:
            raise ValueError("Enemy generation plan finite source count or replay qualification is invalid")
        identity = (source["map_key"], source["location"])
        if identity in seen:
            raise ValueError("Enemy generation plan finite source is duplicated")
        seen.add(identity)
    if sum(source["count"] for source in sources) != plan["finite_kills"]:
        raise ValueError("Enemy generation plan finite sources do not match its kill total")
    return plan


def _random_for(seed, instance_key, field):
    key = json.dumps([ENEMY_PLAN_REVISION, seed, instance_key, field], separators=(",", ":"), sort_keys=True)
    return random.Random(int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest(), "big"))


def sample_species(enemies, eligible_species, *, seed, instance_key):
    weighted = [(species, enemies["weights"][species]) for species in sorted(set(eligible_species))
                if enemies["weights"][species] > 0]
    if not weighted:
        raise ValueError("No enabled enemy species fits this spawn's qualified restrictions")
    draw = _random_for(seed, instance_key, "species").randrange(sum(weight for _, weight in weighted))
    for species, weight in weighted:
        if draw < weight:
            return species
        draw -= weight


def sample_instance_attributes(enemies, species, *, seed, instance_key, stage_index, stage_count):
    if type(stage_count) is not int or type(stage_index) is not int or not 0 <= stage_index < stage_count:
        raise ValueError("Enemy progression requires an included stage index")
    denominator = max(1, stage_count - 1)
    attributes = {}
    for attribute in ("health", "damage", "speed", "size"):
        ranges = enemies["attributes"][attribute]
        bounds = ranges["species"].get(species, ranges["global"])
        lower, upper = (
            (bounds["early"][edge] * (denominator - stage_index)
             + bounds["late"][edge] * stage_index + denominator // 2) // denominator
            for edge in (0, 1)
        )
        attributes[f"{attribute}_percent"] = _random_for(seed, instance_key, attribute).randint(lower, upper)

    if enemy_species.enemy_speed_capabilities().get(species) != "qualified":
        attributes["speed_percent"] = 100

    if species not in enemies["size_variation_exclusions"]:
        chance = enemies["size_variation_chance"]
        draw = _random_for(seed, instance_key, "size_variation").randrange(200)
        attributes["size_percent"] = 50 if draw < chance else 200 if draw < 2 * chance else 100
    return attributes
