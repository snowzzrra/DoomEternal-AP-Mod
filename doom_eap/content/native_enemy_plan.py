"""Freeze body recipes while native commands retain encounter scheduling."""

import hashlib
import json
import math
import re
import struct
from fractions import Fraction
from importlib.resources import files

from .enemy_plan import (BASE_COMBAT_COST, _density_weights, _limit_suffix, _random_for, enemy_combat_cost,
                         effective_special_limits, sample_instance_attributes, sample_species)
from .enemy_species import enemy_spawn_mask, enemy_tracking_suffix, selected_spawn_type

NATIVE_FAMILIES = {"immediate": "i", "group": "g", "staggered": "t", "maintenance": "m", "archvile": "a",
                   "spirit": "s", "possessed": "p", "buff_pod": "b", "independent_actor": "d", "summon": "u", "direct_point": "f"}


def _body_hitpoints(species, attributes):
    result = {}
    for field in ("max", "starting"):
        value = species.get("hitpoints", {}).get(field)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or field == "max" and value == 0:
            raise ValueError("Native body needs qualified health inputs")
        percent = attributes["health_percent"]
        # match the entity writer before the native float conversion.
        scaled = float(format(value * (percent / 100), ".9g")) if percent != 100 else value
        try:
            result[field] = struct.unpack("<f", struct.pack("<f", scaled))[0]
        except (OverflowError, struct.error) as error:
            raise ValueError("Native body health exceeds float range") from error
        if not math.isfinite(result[field]) or field == "max" and result[field] <= 0:
            raise ValueError("Native body health exceeds float range")
    if result["starting"] > result["max"]:
        raise ValueError("Native body starts above its maximum health")
    return result


def _health_metadata(recipe):
    encoded = []
    for body in recipe["bodies"]:
        values = body["hitpoints"]
        maximum, starting = values["max"], values["starting"]
        if (any(type(value) not in (int, float) or not math.isfinite(value) for value in (maximum, starting))
                or not 0 < maximum <= 3.4028234663852886e38 or not 0 <= starting <= maximum):
            raise ValueError("Invalid native body health metadata")
        for value in (maximum, starting):
            encoded.append(f"{struct.unpack('<I', struct.pack('<f', value))[0]:08x}")
    return "".join(encoded)


def _native_call_fields(recipe):
    binding = recipe["binding"]
    args = binding.get("arguments", [])
    entity, first, second, hosts, flag = "", "", "", 0, False
    if args:
        family = recipe["family"]
        if family == "spirit":
            offset = 1 if binding["event_def"] == "spawnSpirit" else 4
            hosts, first, flag = (enemy_spawn_mask(args[offset]["eEncounterSpawnType_t"]),
                                 args[offset + 1]["string"], args[offset + 2]["bool"])
        elif family == "archvile":
            entity, first, second = args[1]["entity"], args[2]["string"], args[3]["string"]
        elif family == "buff_pod":
            first, second = args[1]["string"], args[2]["string"]
        elif family == "possessed":
            first = args[2]["string"]
    if type(flag) is not bool or type(hosts) is not int or not 0 <= hosts < 1 << 45:
        raise ValueError("Invalid native recipe call flags")
    if not isinstance(entity, str) or len(entity) > 191 or re.fullmatch(r"[A-Za-z0-9_./-]*", entity) is None:
        raise ValueError("Invalid native recipe call entity")
    if any(not isinstance(value, str) or "\0" in value or len(value.encode("utf-8")) > 1024 for value in (first, second)):
        raise ValueError("Invalid native recipe call label")
    return [entity, first.encode("utf-8").hex(), second.encode("utf-8").hex(), f"{hosts:016x}", f"{int(flag):02x}"]


def native_recipe_metadata(plan, map_key):
    if re.fullmatch(r"[0-9a-f]{64}", plan["fingerprint"]) is None or re.fullmatch(r"[a-z0-9_]+", map_key) is None:
        raise ValueError("Invalid native recipe library identity")
    if plan["fingerprint"] != native_recipe_fingerprint(plan):
        raise ValueError("Native recipe fingerprint differs from its contents")
    runtime_map = plan["maps"][map_key].get("runtime_map", map_key)
    if not isinstance(runtime_map, str) or not runtime_map or len(runtime_map) > 191 or re.fullmatch(r"[A-Za-z0-9_./-]+", runtime_map) is None:
        raise ValueError("Invalid native recipe runtime map")
    lines = [f"EAP4|{plan['fingerprint']}|{runtime_map}"]
    seen = set()
    for recipe in plan["maps"][map_key]["recipes"]:
        binding = recipe.get("binding")
        if not isinstance(binding, dict) or binding.get("mode") not in {"event", "independent_actor", "summon", "direct_point"}:
            raise ValueError("Native recipe needs a source binding")
        source = binding["reference"] if binding["mode"] in {"event", "direct_point"} else binding["entity"]
        owner = binding.get("owner", "")
        if any(not isinstance(value, str) or len(value) > 191 or re.fullmatch(r"[A-Za-z0-9_./-]*", value) is None
               for value in (source, owner)):
            raise ValueError("Invalid native recipe source identity")
        origin = recipe["origin"]
        if re.fullmatch(r"[0-9a-f]{20}", origin) is None or origin in seen:
            raise ValueError("Invalid or duplicate native recipe origin")
        seen.add(origin)
        values = [recipe[field] for field in ("logical_count", "physical_count", "maximum_emissions", "final_credit_units")]
        if any(type(value) is not int or not 0 <= value <= 0x7fffffff for value in values) or not values[0]:
            raise ValueError("Invalid native recipe library quantity")
        if values[3] > values[0] or bool(values[3]) != bool(values[2]) or not values[1] and values[2]:
            raise ValueError("Invalid native recipe final credit")
        if values[2] and ((values[2] - 1) * values[0] + values[3]) % values[1]:
            raise ValueError("Native recipe quota has fractional original units")
        mask = recipe["logical_mask"]
        if type(mask) is not int or mask <= 0 or mask >= 1 << 44 or mask & (mask - 1):
            raise ValueError("Invalid native recipe logical type")
        if enemy_spawn_mask(recipe["selected_spawn_type"]) != mask:
            raise ValueError("Native recipe logical identity differs")
        ratio = recipe.get("quantity_ratio", [values[1], values[0]])
        if (not isinstance(ratio, list) or len(ratio) != 2 or
                any(type(value) is not int or not 0 <= value <= 0x7fffffff for value in ratio) or not ratio[1]):
            raise ValueError("Invalid native recipe quantity ratio")
        lines.append("|".join([origin, NATIVE_FAMILIES[recipe["family"]], source, owner, f"{mask:016x}",
                               *(f"{value:08x}" for value in values), *_native_call_fields(recipe), _health_metadata(recipe),
                               "".join(f"{value:08x}" for value in ratio)]))
    metadata = "\n".join(lines) + "\n"
    if len(metadata) > 64 * 1024 * 1024:
        raise ValueError("Native recipe metadata exceeds the supported size")
    return metadata


def native_recipe_fingerprint(plan):
    return hashlib.sha256(json.dumps({key: value for key, value in plan.items() if key != "fingerprint"},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def recipe_token(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]


def native_event_identity(recipe):
    binding = recipe["binding"]
    if binding.get("mode") != "event":
        return None
    match = re.search(r"(?:^|\.)entityEvents\.item\[(\d+)\]\.events\.item\[(\d+)\]\.eventCall$", binding["path"])
    return f"{binding['entity']}/{match[1]}/{match[2]}" if match else None


def _secret_timers(maps, catalog):
    from .secret_encounter_timers import load_secret_encounter_timers

    timers = {}
    for key, record in load_secret_encounter_timers()["maps"].items():
        if key not in maps:
            continue
        costs = {origin["id"]: Fraction(origin["combat_cost"]) for origin in catalog["maps"][key]["origins"]
                 if "combat_cost" in origin}
        timers[key] = {}
        for timer in record["timers"]:
            baseline = sum((BASE_COMBAT_COST[species] * count
                            for species, count in timer["original_enemies"].items()), Fraction(0))
            actual = baseline
            for recipe in maps[key]["recipes"]:
                if recipe["binding"].get("owner") == timer["manager"]:
                    actual -= costs[recipe["id"]]
                    actual += sum((enemy_combat_cost(body["species"], body["attributes"])
                                   for body in recipe["bodies"]), Fraction(0))
            duration = max(1, math.ceil(timer["duration_ms"] * actual / baseline))
            if duration > 0x7fffffff:
                raise ValueError("Enemy settings require a Secret Encounter timer outside the game's range")
            timers[key][timer["entity"]] = duration
    return timers


def native_body_name(origin, ordinal, logical_count, physical_count, logical_type, body, settings, *, credit_units=None):
    tracking = enemy_tracking_suffix(logical_type, body["spawn_type"])
    suffix = _limit_suffix(settings, body["species"]) + f"__ap_v070_s{body['attributes']['speed_percent']:05d}"
    credit = f"_w{credit_units:08x}" if credit_units is not None else ""
    return f"ap_v070_ai_r{origin}_{ordinal:08x}_q{logical_count:08x}{physical_count:08x}{credit}{tracking}{suffix}"


def _phase_bodies(sources, candidates, settings, intensity, *, seed, key, catalog, stage_index, stage_count):
    bodies = {source["id"]: [] for source in sources}
    if not candidates:
        return bodies
    roster = [source for source in sources for _ in range(source["logical_count"])]
    _random_for(seed, key, "positions").shuffle(roster)
    fixed = ((len(roster) * settings["count_multiplier"] + 99) // 100
             if settings["count_mode"] == "multiplier" else None)
    selected = settings if fixed is not None else {**settings, "weights": _density_weights(
        settings, candidates, seed=seed, phase=key, stage_index=stage_index, stage_count=stage_count)}
    shared = sample_species(selected, candidates, seed=seed, instance_key=[key, "group"])
    budget = sum((Fraction(source["combat_cost"]) for source in sources), Fraction(0)) * intensity
    spent, ordinal = Fraction(0), 0
    while ordinal < fixed if fixed is not None else ordinal == 0 or spent < budget:
        draw_key = [key, ordinal]
        individual = _random_for(seed, draw_key, "variety").randrange(100) < settings["variety"]
        species = sample_species(selected, candidates, seed=seed, instance_key=draw_key) if individual else shared
        attributes = sample_instance_attributes(settings, species, seed=seed, instance_key=draw_key,
            stage_index=stage_index, stage_count=stage_count)
        cost = enemy_combat_cost(species, attributes)
        if fixed is None and spent + cost > budget:
            if ordinal:
                break
            draws = {candidate: sample_instance_attributes(settings, candidate, seed=seed,
                instance_key=draw_key, stage_index=stage_index, stage_count=stage_count) for candidate in candidates}
            costs = {candidate: enemy_combat_cost(candidate, attrs) for candidate, attrs in draws.items()}
            fitting = [candidate for candidate in candidates if costs[candidate] <= budget]
            if not fitting:
                fitting = [candidate for candidate in candidates if costs[candidate] == min(costs.values())]
            species = sample_species(selected, fitting, seed=seed, instance_key=draw_key)
            attributes, cost = draws[species], costs[species]
            if not individual:
                shared = species
        bodies[roster[ordinal % len(roster)]["id"]].append({"species": species, "attributes": attributes,
            "spawn_type": catalog["species"][species]["spawn_type"], "ai": dict(catalog["species"][species]["ai"]),
            "hitpoints": _body_hitpoints(catalog["species"][species], attributes)})
        spent += cost
        ordinal += 1
    return bodies


def build_native_recipes(enemies, campaign, *, seed, catalog=None):
    if not enemies["enabled"]:
        return None
    seed = str(seed)
    if catalog is None:
        catalog = json.loads(files(__package__).joinpath("native_enemy_catalog.json").read_bytes())
    if catalog.get("schema_version") != 1:
        raise ValueError("Unsupported native recipe catalog")
    maps, combat_costs = {}, {}
    sequence = campaign.get("sequence") if isinstance(campaign, dict) else None
    if (not isinstance(sequence, list) or not sequence or len(sequence) > len(catalog["maps"])
            or any(not isinstance(key, str) or key not in catalog["maps"] for key in sequence)
            or len(sequence) != len(set(sequence))):
        raise ValueError("Native recipe campaign needs distinct source maps")
    if campaign.get("mode") == "mission_access_as_items":
        bands = campaign.get("enemy_progression_bands")
        if (not isinstance(bands, dict) or any(type(bands.get(key)) is not int or not 0 <= bands[key] <= 4 for key in sequence)):
            raise ValueError("Native recipe campaign progression is invalid")
    stage_count = 5 if campaign.get("mode") == "mission_access_as_items" else len(sequence)
    for stage_index, map_key in enumerate(sequence):
        if map_key not in catalog["maps"]:
            raise ValueError(f"Missing native recipe map: {map_key}")
        if catalog["maps"][map_key].get("unresolved"):
            raise ValueError(f"Unresolved native origin: {catalog['maps'][map_key]['unresolved'][0]['id']}")
        if campaign.get("mode") == "mission_access_as_items":
            stage_index = campaign["enemy_progression_bands"][map_key]
        denominator = max(1, stage_count - 1)
        intensity = Fraction(enemies["intensity"][0] * (denominator - stage_index)
                             + enemies["intensity"][1] * stage_index, 100 * denominator)
        limits = effective_special_limits(enemies, stage_index, stage_count)
        settings = {**enemies, "special_limits": limits}
        candidates = sorted(species for species in catalog["species"]
                            if enemies["weights"].get(species, 0) > 0 and limits.get(species, 1) > 0
                            and (species not in {"marauder", "archvile", "spirit"}
                                 or limits["combined_special"] > 0))
        recipes, protected, inactive = [], [], []
        phases = {}
        origins = [*catalog["maps"][map_key]["origins"], *catalog["maps"][map_key].get("runtime_origins", [])]
        for source in origins:
            if not source.get("protected_reason") and not source.get("inactive_reason"):
                count = source["logical_count"]
                if type(count) is not int or not 0 < count <= 0x7fffffff:
                    raise ValueError(f"Invalid native request quantity: {source['id']}")
                phases.setdefault(source.get("phase", source["id"]), []).append(source)
        phase_bodies = {}
        for phase, sources in phases.items():
            phase_bodies.update(_phase_bodies(sources, candidates, settings, intensity, seed=seed,
                key=[map_key, phase], catalog=catalog, stage_index=stage_index, stage_count=stage_count))
        for source in origins:
            if source.get("protected_reason"):
                protected.append({"id": source["id"], "reason": source["protected_reason"]})
                continue
            if source.get("inactive_reason"):
                inactive.append({"id": source["id"], "reason": source["inactive_reason"]})
                continue
            key = [map_key, source["id"]]
            token = recipe_token([str(seed), key])
            logical_types = source["logical_types"]
            if not logical_types or len(logical_types) != len(set(logical_types)):
                raise ValueError(f"Native origin needs distinct logical types: {source['id']}")
            selected_type = logical_types[_random_for(seed, key, "logical_type").randrange(len(logical_types))]
            selected_spawn_type({"spawn_type": source["spawn_type"], "selected_spawn_type": selected_type})
            logical_count = source["logical_count"]
            bodies = phase_bodies[source["id"]]
            quantity_ratio = [len(bodies), logical_count]
            if source.get("runtime_only"):
                bodies = bodies[:len(catalog["species"])]
                if bodies and all(body == bodies[0] for body in bodies):
                    bodies = bodies[:1]
            if len(bodies) > 0x7fffffff:
                raise ValueError(f"Native recipe has an invalid physical quantity: {source['id']}")
            maximum = source.get("maximum_emissions", logical_count)
            if type(maximum) is not int or maximum < 0:
                raise ValueError(f"Invalid native emission maximum: {source['id']}")
            physical_maximum = ((maximum * len(bodies) + logical_count - 1) // logical_count if maximum else 0)
            if physical_maximum > 0x7fffffff:
                raise ValueError(f"Native recipe exceeds the engine's emission range: {source['id']}")
            final_credit = maximum * len(bodies) - (physical_maximum - 1) * logical_count if physical_maximum else 0
            for ordinal, body in enumerate(bodies):
                body["definition"] = native_body_name(token, ordinal, logical_count, len(bodies), selected_type, body, settings)
                if 0 < final_credit < logical_count and ordinal == (physical_maximum - 1) % len(bodies):
                    body["tail_definition"] = native_body_name(token, ordinal, logical_count, len(bodies),
                        selected_type, body, settings, credit_units=final_credit)
            recipes.append({"id": source["id"], "origin": token,
                "family": source["family"], "selected_spawn_type": selected_type,
                "runtime_only": source.get("runtime_only", False), "quantity_ratio": quantity_ratio,
                "logical_mask": enemy_spawn_mask(selected_type), "logical_count": logical_count,
                "physical_count": len(bodies), "maximum_emissions": physical_maximum, "renewable": maximum == 0,
                "final_credit_units": final_credit,
                "binding": dict(source["binding"]) if "binding" in source else None,
                "bodies": bodies})
        maps[map_key] = {"source_sha256": catalog["maps"][map_key]["source_sha256"],
                         "runtime_map": catalog["maps"][map_key].get("runtime_map", map_key),
                         "recipes": recipes, "protected": protected, "inactive": inactive,
                         "progression": [stage_index, stage_count], "special_limits": limits}
        original_cost = sum((Fraction(source["combat_cost"]) for sources in phases.values() for source in sources
                             if not source.get("runtime_only")), Fraction(0))
        actual_cost = sum((enemy_combat_cost(body["species"], body["attributes"])
                           for recipe in recipes if not recipe["runtime_only"] for body in recipe["bodies"]), Fraction(0))
        combat_costs[map_key] = {"original": float(original_cost), "target": float(original_cost * intensity),
                               "actual": float(actual_cost)}
    frozen_campaign = {"sequence": list(sequence), "mode": campaign.get("mode", "linear")}
    if campaign.get("mode") == "mission_access_as_items":
        frozen_campaign["enemy_progression_bands"] = {key: campaign["enemy_progression_bands"][key] for key in sequence}
    plan = {"kind": "native_recipes", "revision": 1, "seed": str(seed), "campaign": frozen_campaign, "maps": maps,
            "secret_timers": _secret_timers(maps, catalog), "combat_costs": combat_costs}
    plan["fingerprint"] = native_recipe_fingerprint(plan)
    return plan


def validate_native_recipes(plan, enemies, campaign=None, *, catalog=None):
    if plan is None:
        if enemies["enabled"]:
            raise ValueError("Enemizer requires frozen native recipes")
        return None
    if not isinstance(plan, dict) or not isinstance(plan.get("seed"), str) or not plan["seed"]:
        raise ValueError("Native recipe seed is invalid")
    campaign = plan.get("campaign") if campaign is None else campaign
    if not isinstance(campaign, dict) or not isinstance(campaign.get("sequence"), list):
        raise ValueError("Native recipe campaign is invalid")
    expected = build_native_recipes(enemies, campaign, seed=plan["seed"], catalog=catalog)
    if plan != expected:
        raise ValueError("Native recipes differ from the seed, options or source catalog")
    return plan
