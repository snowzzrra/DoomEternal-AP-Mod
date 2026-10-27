"""Freeze body recipes while native commands retain encounter scheduling."""

import hashlib
import json
import math
import re
import struct
import os
import tempfile
from pathlib import Path
from fractions import Fraction
from importlib.resources import files
from collections import OrderedDict
from threading import Lock

from .enemy_plan import (BASE_COMBAT_COST, _density_weights, _limit_suffix, _random_for, enemy_combat_cost,
                         effective_special_limits, sample_instance_attributes, sample_species)
from .enemy_species import enemy_spawn_mask, enemy_tracking_suffix, selected_spawn_type, enemy_speed_capabilities
from .enemy_options import BALANCE_PRESETS, ENEMY_RECIPES, SPECIES_GROUPS, uses_protected_opening

NATIVE_FAMILIES = {"immediate": "i", "group": "g", "staggered": "t", "maintenance": "m", "archvile": "a",
                   "spirit": "s", "possessed": "p", "buff_pod": "b", "independent_actor": "d", "summon": "u", "direct_point": "f"}
AMMO_FODDER = frozenset({"zombie_tier_1", "zombie_tier_3", "imp", "gargoyle", "hell_soldier"})


def ammunition_sources(origins, catalog, *, finite=False):
    original_suppliers = AMMO_FODDER | {'shotgun_soldier', 'chaingun_soldier', 'stone_imp', 'zombie_maykr'}
    spawn_types = {catalog['species'][species]['spawn_type'] for species in original_suppliers if species in catalog['species']}
    fodder = AMMO_FODDER | {'prowler', 'shotgun_soldier', 'chaingun_soldier', 'demonic_trooper',
                           'stone_imp', 'zombie_maykr', 'lost_soul', 'zombie_t1_screecher'}
    fodder_types = {row['spawn_type'] for species, row in catalog['species'].items() if species in fodder}
    def ammunition_pool(source):
        types = set(source.get('logical_types', []))
        return bool(types & spawn_types) and types <= fodder_types
    def group(source):
        binding = source.get('binding') or {}
        label = next((argument['string'] for argument in binding.get('arguments', []) if 'string' in argument), '')
        return binding.get('owner', binding.get('entity', source['id'])), label
    groups = {group(source) for source in origins if source.get('family') == 'maintenance'
              and (source.get('maximum_emissions') == 0 or finite
                   and source.get('maximum_emissions', 0) > source.get('logical_count', 0))
              and ammunition_pool(source)
              and not source.get('protected_reason') and not source.get('inactive_reason')}
    return {source['id'] for source in origins if group(source) in groups and ammunition_pool(source)}


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
    identity = plan.get('source_fingerprint', plan['fingerprint'])
    if re.fullmatch(r"[0-9a-f]{64}", identity) is None:
        raise ValueError("Invalid native recipe source identity")
    lines = [f"EAP4|{identity}|{runtime_map}"]
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
    binding = recipe["binding"] or {}
    if binding.get("mode") != "event":
        return None
    match = re.search(r"(?:^|\.)entityEvents\.item\[(\d+)\]\.events\.item\[(\d+)\]\.eventCall$", binding.get("path", ""))
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
    limits = {**settings, **({'population_limits': body['population_limits']} if 'population_limits' in body else {})}
    if 'special_limits' in body:
        limits['special_limits'] = body['special_limits']
    suffix = _limit_suffix(limits, body["species"]) + f"__ap_v070_s{body['attributes']['speed_percent']:05d}"
    credit = f"_w{credit_units:08x}" if credit_units is not None else ""
    name = f"ap_v070_ai_r{origin}_{ordinal:08x}_q{logical_count:08x}{physical_count:08x}{credit}{tracking}{suffix}"
    if not name.isascii() or len(name) >= 512:
        raise ValueError('Native recipe body name exceeds the supported ASCII size')
    return name


def _revision_one_bodies(sources, candidates, settings, intensity, *, seed, key, catalog, stage_index, stage_count):
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
        source = roster[ordinal % len(roster)]
        eligible = [species for species in candidates if species in source['allowed_species']] if source.get('allowed_species') else candidates
        if not eligible:
            raise ValueError(f"Native spawn has no enabled compatible species: {source['id']}")
        individual = _random_for(seed, draw_key, "variety").randrange(100) < settings["variety"]
        species = sample_species(selected, eligible, seed=seed, instance_key=draw_key) if individual or shared not in eligible else shared
        attributes = sample_instance_attributes(settings, species, seed=seed, instance_key=draw_key,
            stage_index=stage_index, stage_count=stage_count)
        cost = enemy_combat_cost(species, attributes)
        if fixed is None and spent + cost > budget:
            if ordinal:
                break
            draws = {candidate: sample_instance_attributes(settings, candidate, seed=seed,
                instance_key=draw_key, stage_index=stage_index, stage_count=stage_count) for candidate in eligible}
            costs = {candidate: enemy_combat_cost(candidate, attrs) for candidate, attrs in draws.items()}
            fitting = [candidate for candidate in eligible if costs[candidate] <= budget]
            if not fitting:
                fitting = [candidate for candidate in eligible if costs[candidate] == min(costs.values())]
            species = sample_species(selected, fitting, seed=seed, instance_key=draw_key)
            attributes, cost = draws[species], costs[species]
            if not individual:
                shared = species
        bodies[source["id"]].append({"species": species, "attributes": attributes,
            "spawn_type": catalog["species"][species]["spawn_type"], "ai": dict(catalog["species"][species]["ai"]),
            "hitpoints": _body_hitpoints(catalog["species"][species], attributes)})
        spent += cost
        ordinal += 1
    return bodies


def _revision_two_bodies(sources, candidates, settings, intensity, *, seed, key, catalog, stage_index, stage_count):
    if settings['count_mode'] == 'multiplier':
        return _revision_one_bodies(sources, candidates, settings, intensity, seed=seed, key=key,
            catalog=catalog, stage_index=stage_index, stage_count=stage_count)
    bodies = {source['id']: [] for source in sources}
    if not candidates:
        return bodies
    roster = [source for source in sources
              for _ in range((source['logical_count'] * max(100, settings['density']) + 99) // 100)]
    _random_for(seed, key, 'positions').shuffle(roster)
    selected = {**settings, 'weights': _density_weights(settings, candidates, seed=seed, phase=key,
        stage_index=stage_index, stage_count=stage_count)}
    shared = sample_species(selected, candidates, seed=seed, instance_key=[key, 'group'])
    budget = sum((Fraction(source['combat_cost']) for source in sources), Fraction(0)) * intensity
    spent = Fraction(0)
    for ordinal, source in enumerate(roster):
        draw_key = [key, ordinal]
        individual = _random_for(seed, draw_key, 'variety').randrange(100) < settings['variety']
        species = sample_species(selected, candidates, seed=seed, instance_key=draw_key) if individual else shared
        attributes = sample_instance_attributes(settings, species, seed=seed, instance_key=draw_key,
            stage_index=stage_index, stage_count=stage_count)
        cost = enemy_combat_cost(species, attributes)
        remaining = budget - spent
        allowance = remaining / len(roster) if settings['variety'] == 0 and ordinal == 0 else remaining
        if cost > allowance and (settings['variety'] != 0 or ordinal == 0):
            draws = {candidate: sample_instance_attributes(settings, candidate, seed=seed,
                instance_key=draw_key, stage_index=stage_index, stage_count=stage_count) for candidate in candidates}
            costs = {candidate: enemy_combat_cost(candidate, attrs) for candidate, attrs in draws.items()}
            fitting = [candidate for candidate in candidates if costs[candidate] <= allowance]
            if not fitting:
                fitting = [candidate for candidate in candidates if costs[candidate] == min(costs.values())]
            species = sample_species(selected, fitting, seed=seed, instance_key=draw_key)
            attributes, cost = draws[species], costs[species]
            if not individual:
                shared = species
        bodies[source['id']].append({'species': species, 'attributes': attributes,
            'spawn_type': catalog['species'][species]['spawn_type'], 'ai': dict(catalog['species'][species]['ai']),
            'hitpoints': _body_hitpoints(catalog['species'][species], attributes)})
        spent += cost
    return bodies


def _phase_bodies(sources, candidates, settings, intensity, *, seed, key, catalog, stage_index, stage_count,
                  balanced=False, supply_sources=frozenset(), revision=4, opening=False):
    if settings['count_mode'] == 'multiplier':
        bodies = _revision_one_bodies(sources, candidates, settings, intensity, seed=seed, key=key,
            catalog=catalog, stage_index=stage_index, stage_count=stage_count)
        suppliers = [source for source in sources if source['id'] in supply_sources]
        if suppliers:
            bodies.update(_phase_bodies(suppliers, candidates, {**settings, 'count_mode': 'auto', 'density': 100}, intensity,
                seed=seed, key=key, catalog=catalog, stage_index=stage_index, stage_count=stage_count,
                balanced=balanced, supply_sources=supply_sources, revision=revision))
        return bodies
    bodies = {source['id']: [] for source in sources}
    if not candidates:
        return bodies
    if revision >= 8:
        roster = [source for source in sources for _ in range(source['logical_count'])]
        density_draw = _random_for(seed, key, 'density')
        expandable = [source for source in roster if source['id'] not in supply_sources] if revision >= 9 else roster
        count, remainder = divmod(len(expandable) * max(100, settings['density']), 100)
        count += density_draw.randrange(100) < remainder
        roster += density_draw.choices(expandable, k=count - len(expandable))
    else:
        roster = [source for source in sources
                  for _ in range((source['logical_count'] * max(100, settings['density']) + 99) // 100)]
    _random_for(seed, key, 'positions').shuffle(roster)
    weights = _density_weights(settings, candidates, seed=seed, phase=key,
        stage_index=stage_index, stage_count=stage_count)
    neutral = sorted(BASE_COMBAT_COST[species] for species in candidates)[len(candidates) // 2]
    target = (1 + (neutral - 1) * Fraction(stage_index, max(1, stage_count - 1))
              if balanced and settings['progressive'] else neutral) * intensity
    if balanced and revision >= 5:
        progress = Fraction(stage_index, max(1, stage_count - 1)) if settings['progressive'] else 0
        target = Fraction(neutral, 2) * (1 + progress) * intensity
        if revision >= 7:
            target = Fraction(3 * neutral, 4) * (1 + progress) * intensity
        if revision >= 9 and settings['progressive']:
            target = neutral * (Fraction(settings['intensity'][0], 400) * (1 - progress)
                                + Fraction(3 * settings['intensity'][1], 100) * progress)
    target_numerator, target_denominator = target.numerator ** 2, target.denominator ** 2
    attribute_groups = {}
    representatives = {}
    for species in candidates:
        profile = tuple(tuple(tuple(bounds[stage]) for stage in ('early', 'late'))
                        for ranges in (settings['attributes'][name] for name in ('health', 'damage', 'speed', 'size'))
                        for bounds in [ranges['species'].get(species, ranges['global'])])
        profile += (enemy_speed_capabilities().get(species) == 'qualified',
                    species in settings['size_variation_exclusions'])
        representatives[species] = attribute_groups.setdefault(profile, species)
    base_costs = {species: BASE_COMBAT_COST[species] * (
        12 if revision >= 6 and species == 'buff_pod' or revision >= 7 and species == 'zombie_t1_screecher' else 1)
        for species in candidates}
    cost_scales = {species: base_costs[species] / base_costs[representatives[species]] for species in candidates}
    fodder_slots = set()
    if revision >= 9:
        slots = [index for index, source in enumerate(roster)
                 if any(species in AMMO_FODDER and (not source.get('allowed_species')
                        or species in source['allowed_species']) for species in candidates)]
        if len(roster) >= 4 or any(source['id'] in supply_sources or any(
                catalog['species'][species]['spawn_type'] in source.get('logical_types', [])
                for species in AMMO_FODDER if species in catalog['species']) for source in sources):
            _random_for(seed, key, 'ammunition').shuffle(slots)
            fodder_slots.update(slots[:(len(roster) + 3) // 4])
    remaining = target * len(roster)
    drawn = {}
    shared = None
    for ordinal, source in enumerate(roster):
        draw_key = [key, ordinal]
        random_draws = {}
        samples = {species: sample_instance_attributes(settings, species, seed=seed, instance_key=draw_key,
            stage_index=stage_index, stage_count=stage_count, random_draws=random_draws)
            for species in attribute_groups.values()}
        draws = {species: samples[representatives[species]] for species in candidates}
        profile_costs = {species: enemy_combat_cost(species, attributes, buff_totems=revision >= 6,
                                                  screechers=revision >= 7) for species, attributes in samples.items()}
        costs = {species: (profile_costs[representatives[species]].numerator * cost_scales[species].numerator,
                           profile_costs[representatives[species]].denominator * cost_scales[species].denominator)
                 for species in candidates}
        eligible = candidates
        if opening:
            eligible = [species for species in eligible if costs[species][0] <= 5 * costs[species][1] and species not in
                        {'spirit', 'blood_angel', 'stone_imp', 'marauder', 'archvile', 'buff_pod', 'zombie_t1_screecher'}]
            if not eligible:
                raise ValueError('Starting encounters need an enabled enemy that the initial arsenal can handle')
        if revision >= 6 and source.get('allowed_species'):
            eligible = [species for species in eligible if species in source['allowed_species']]
            if not eligible:
                raise ValueError(f"Native spawn has no enabled compatible species: {source['id']}")
        if source['id'] in supply_sources or ordinal in fodder_slots:
            eligible = [species for species in eligible if species in AMMO_FODDER]
            if not eligible:
                raise ValueError(f"Ammunition source requires an enabled chainsaw fodder species: {source['id']}")
        elif balanced and (revision < 7 or revision >= 9):
            allowance = min(target * 3, remaining * 2 / (len(roster) - ordinal)) if revision >= 9 else target * (3 if revision >= 5 else 6)
            fitted = [species for species in eligible if costs[species][0] * allowance.denominator
                      <= allowance.numerator * costs[species][1]]
            eligible = fitted or [min(eligible, key=lambda species: (Fraction(*costs[species]), species))]
        selected_weights = {}
        for species in eligible:
            cost_numerator, cost_denominator = costs[species]
            target_units = target_numerator * cost_denominator ** 2
            penalty_units = cost_numerator ** 2 * target_denominator
            if balanced and revision >= 5:
                numerator = cost_numerator * target.denominator
                denominator = target.numerator * cost_denominator
                if numerator > denominator:
                    numerator, denominator = denominator, numerator
                if revision >= 9:
                    numerator, denominator = numerator ** 2, denominator ** 2
                    denominator *= 1 + drawn.get(species, 0)
                selected_weights[species] = max(1, weights[species] * numerator * 1000000 // denominator)
            else:
                selected_weights[species] = max(1, weights[species] * target_units * 1000000 // (target_units + penalty_units))
        selected = {**settings, 'weights': selected_weights}
        individual = _random_for(seed, draw_key, 'variety').randrange(100) < settings['variety']
        if shared not in eligible or individual:
            species = sample_species(selected, eligible, seed=seed, instance_key=draw_key)
            if shared is None or revision >= 7: shared = species
        else:
            species = shared
        attributes = draws[species]
        if revision >= 9:
            remaining -= Fraction(*costs[species])
            drawn[species] = drawn.get(species, 0) + 1
        bodies[source['id']].append({'species': species, 'attributes': attributes,
            'spawn_type': catalog['species'][species]['spawn_type'], 'ai': dict(catalog['species'][species]['ai']),
            'hitpoints': _body_hitpoints(catalog['species'][species], attributes)})
    return bodies


def _balanced_phase_bodies(sources, candidates, settings, intensity, *, seed, key, catalog,
                          stage_index, stage_count, supply_sources, extras, arena, opening=False,
                          revision=10, nightmare=False):
    profile = BALANCE_PRESETS[settings['balance_preset']]
    progress = Fraction(stage_index, max(1, stage_count - 1)) if settings['progressive'] else Fraction(1)
    if opening:
        progress = min(progress, Fraction(1, 10))
    large = arena['peak'] >= 8
    draw = _random_for(seed, arena['key'], 'population')
    swarm = large and progress >= Fraction(1, 4) and draw.randrange(10) == 0
    mixed = large and progress >= Fraction(3, 5) and not swarm and draw.randrange(100) < (
        30 if settings['balance_preset'] == 'icons_wrath' else 10 if settings['balance_preset'] == 'hells_vanguard' else 3)
    maximum = 6 + round((6 if settings['balance_preset'] == 'icons_wrath' else 4) * progress)
    maximum += 2 * large
    if swarm:
        maximum += 4
    maximum = max(2, min(128, maximum * settings['density'] // 100))
    heavy_cap = 0
    if revision >= 11:
        rank = tuple(BALANCE_PRESETS).index(settings['balance_preset'])
        maximum = max(3, (4 + (rank >= 3) + 2 * large + round(3 * progress)) * settings['density'] // 100)
        heavy_cap = min(maximum - 2, 1 + int(large) + round((1 + (rank >= 3)) * progress))
        if opening and nightmare:
            heavy_cap = 1
            maximum = min(maximum, 5)
    heavy_limit = 1 if progress < Fraction(1, 5) else profile['superheavy']
    if not large or progress < Fraction(3, 5):
        heavy_limit = min(2, heavy_limit)
    population = dict(maximum=maximum, superheavy=heavy_limit,
                      pressure=min(maximum, 3 + round(4 * progress)), mixed=int(mixed), scope=arena['scope'])
    if revision >= 11:
        population['heavy'] = heavy_cap
        if opening:
            population['superheavy'] = 0
        composition = {
            'uac_patrol': ((92, 8, 0, 0), (78, 20, 1, 1)),
            'sentinel_training': ((86, 14, 0, 0), (65, 31, 3, 1)),
            'slayers_path': ((80, 20, 0, 0), (53, 38, 7, 2)),
            'hells_vanguard': ((72, 28, 0, 0), (39, 45, 12, 4)),
            'icons_wrath': ((65, 35, 0, 0), (28, 50, 16, 6)),
        }
        profile = {**profile, 'groups': composition[settings['balance_preset']]}
    groups = [early * (1 - progress) + late * progress
              for early, late in zip(*profile['groups'])]
    expected_intensity = sum(value * part for value, part in zip(ENEMY_RECIPES[settings['balance_preset']].intensity,
                                                               (1 - progress, progress))) / 100
    for group in (1, 2, 3):
        groups[group] *= intensity / expected_intensity
    if swarm:
        groups = [80, 20, 0, 0]
    roles = {species: min(index, 3) for index, members in enumerate(SPECIES_GROUPS.values()) for species in members}
    roles.update(buff_pod=3, zombie_t1_screecher=3)
    roster = [source for source in sources for _ in range(source['logical_count'] + extras.get(source['id'], 0))]
    if settings['count_mode'] == 'multiplier':
        roster = [source for source in sources for _ in range(source['logical_count'] if source['id'] in supply_sources
                  or source['family'] == 'maintenance' else max(1, round(source['logical_count'] * settings['count_multiplier'] / 100)))]
    _random_for(seed, key, 'positions').shuffle(roster)
    bodies = {source['id']: [] for source in sources}
    ammunition = max(1, (len(roster) + 3) // 4) if len(roster) >= 4 else 0
    available_ammo = [index for index, source in enumerate(roster) if source['id'] in supply_sources]
    slots = [index for index, source in enumerate(roster) if any(species in AMMO_FODDER and
             (not source.get('allowed_species') or species in source['allowed_species']) for species in candidates)]
    _random_for(seed, key, 'ammunition').shuffle(slots)
    ammo_slots = set(available_ammo)
    for index in slots:
        if len(ammo_slots) >= ammunition:
            break
        ammo_slots.add(index)
    drawn, superheavies, supports = {}, 0, 0
    heavy_budget = len(roster) * groups[1] // 100
    if revision == 11 and opening:
        heavy_budget = min(heavy_budget, 1 if settings['balance_preset'] == 'slayers_path' else 2)
    def compatible(source):
        eligible = [species for species in candidates if not source.get('allowed_species')
                    or species in source['allowed_species']]
        if progress < Fraction(1, 4):
            eligible = [s for s in eligible if s not in {'spirit', 'blood_angel', 'stone_imp', 'cursed_prowler',
                                                        'armored_baron', 'tyrant', 'pain_elemental'}]
        if source['family'] == 'maintenance' and (not source.get('maximum_emissions')
                or source['maximum_emissions'] > source['logical_count'] * 2):
            eligible = [s for s in eligible if roles[s] < 2]
        if revision >= 11 and source['family'] == 'maintenance' and all(
                any(catalog['species'][s]['spawn_type'] == kind for s in SPECIES_GROUPS['fodder'] if s in catalog['species'])
                for kind in source['logical_types']):
            eligible = [s for s in eligible if roles[s] == 0]
        if revision >= 11:
            if progress < Fraction(7, 20):
                eligible = [s for s in eligible if roles[s] < 2]
            if opening:
                ceiling = 12 if settings['balance_preset'] == 'slayers_path' else 14
                eligible = [s for s in eligible if BASE_COMBAT_COST[s] <= ceiling]
        return eligible
    eligible_sources = {source['id']: compatible(source) for source in sources}
    heavy_slots = [i for i, source in enumerate(roster) if i not in ammo_slots and
                   any(roles[s] == 1 for s in eligible_sources[source['id']])]
    _random_for(seed, key, 'heavy_positions').shuffle(heavy_slots)
    heavy_slots = set(heavy_slots[:int(heavy_budget)])
    for ordinal, source in enumerate(roster):
        eligible = eligible_sources[source['id']]
        if ordinal in ammo_slots:
            eligible = [s for s in eligible if s in AMMO_FODDER]
        else:
            eligible = [s for s in eligible if groups[roles[s]] > 0
                        and (roles[s] != 2 or len(roster) >= 4 and superheavies < population['superheavy']
                             and (not supports or mixed))
                        and (roles[s] != 3 or large and supports < 1 and (not superheavies or mixed))]
        if revision >= 11:
            eligible = [s for s in eligible if (roles[s] == 1) == (ordinal in heavy_slots)]
        if not eligible:
            if source['id'] in supply_sources:
                raise ValueError(f"Ammunition source has no eligible chainsaw fodder: {source['id']}")
            raise ValueError(f"No compatible enemy for balanced spawn: {source['id']}")
        totals = {group: sum(settings['weights'][s] for s in eligible if roles[s] == group) for group in range(4)}
        weights = {}
        for species in eligible:
            group = roles[species]
            chance = Fraction(1) if ordinal in ammo_slots else Fraction(groups[group])
            chance *= Fraction(settings['weights'][species], max(1, totals[group]))
            if species in {'marauder', 'archvile', 'tyrant', 'doom_hunter', 'armored_baron'}:
                chance *= Fraction({'marauder': 40, 'archvile': 30, 'tyrant': 50,
                                    'doom_hunter': 70, 'armored_baron': 80}[species], 100)
            chance /= 1 + drawn.get(species, 0) * Fraction(settings['variety'], 25)
            weights[species] = max(1, round(chance * 1000000))
        draw_key = [key, ordinal]
        if revision >= 11:
            role_weights = [max(0, round(groups[group] * 1000)) if any(roles[s] == group for s in eligible) else 0
                            for group in range(4)]
            roll = _random_for(seed, draw_key, 'role').randrange(sum(role_weights))
            selected_role = 0
            while roll >= role_weights[selected_role]:
                roll -= role_weights[selected_role]
                selected_role += 1
            eligible = [s for s in eligible if roles[s] == selected_role]
        species = sample_species({**settings, 'weights': weights}, eligible, seed=seed, instance_key=draw_key)
        attributes = sample_instance_attributes(settings, species, seed=seed, instance_key=draw_key,
            stage_index=stage_index, stage_count=stage_count)
        bodies[source['id']].append(dict(species=species, attributes=attributes,
            spawn_type=catalog['species'][species]['spawn_type'], ai=dict(catalog['species'][species]['ai']),
            hitpoints=_body_hitpoints(catalog['species'][species], attributes), population_limits=dict(population)))
        drawn[species] = drawn.get(species, 0) + 1
        superheavies += roles[species] == 2
        supports += roles[species] == 3
    return bodies


def _wave_peak(bodies, *, mobile=False):
    limits = next((body['population_limits'] for body in bodies if body.get('population_limits')), None)
    if not limits:
        return {}
    superheavy = {'baron', 'armored_baron', 'doom_hunter', 'tyrant', 'marauder', 'archvile'}
    special = {'marauder', 'archvile', 'spirit', 'buff_pod', 'zombie_t1_screecher'}
    ordered = sorted(bodies, key=lambda body: enemy_combat_cost(body['species'], body['attributes'],
                     buff_totems=True, screechers=True), reverse=True)
    peaks = []
    for mode in ('ordinary', 'superheavy', 'support', 'mixed'):
        if mode == 'mixed' and not limits['mixed']:
            continue
        selected, heavies, supports, ordinary_heavies = [], 0, 0, 0
        for body in ordered:
            species = body['species']
            heavy, support = species in superheavy, species in special
            ordinary = species in SPECIES_GROUPS['heavy'] or mobile and species in {'super_tentacle', 'turret'}
            if ordinary and limits.get('heavy') and ordinary_heavies >= limits['heavy']:
                continue
            if (mode == 'ordinary' and (heavy or support) or mode == 'superheavy' and support and not heavy
                    or mode == 'support' and heavy or heavy and heavies >= limits['superheavy']):
                continue
            ceiling = limits['maximum'] if mode == 'ordinary' else limits['pressure']
            if len(selected) >= ceiling:
                break
            selected.append(body)
            heavies += heavy
            supports += support
            ordinary_heavies += ordinary
        peaks.append((sum(enemy_combat_cost(body['species'], body['attributes'], buff_totems=True,
                                           screechers=True) for body in selected), len(selected)))
    cost, count = max(peaks)
    return dict(peak_cost=float(cost), peak_bodies=count, population_maximum=limits['maximum'],
                superheavy_maximum=limits['superheavy'])


def build_native_recipes(enemies, campaign, *, seed, catalog=None, revision=19):
    if type(revision) is not int or revision not in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19):
        raise ValueError('Unsupported native recipe revision')
    if not enemies["enabled"]:
        return None
    seed = str(seed)
    if catalog is None:
        catalog = json.loads(files(__package__).joinpath("native_enemy_catalog.json").read_bytes())
    if catalog.get("schema_version") != 1:
        raise ValueError("Unsupported native recipe catalog")
    if revision <= 4:
        catalog = {**catalog, 'maps': {key: {**stage, 'origins': [
            {**{field: source[field] for field in ('id', 'family', 'layers')},
             'protected_reason': source['legacy_protected_reason']} if source.get('legacy_protected_reason') else source
            for source in stage['origins']]} for key, stage in catalog['maps'].items()}}
    maps, combat_costs = {}, {}
    sequence = campaign.get("sequence") if isinstance(campaign, dict) else None
    if (not isinstance(sequence, list) or not sequence or len(sequence) > len(catalog["maps"])
            or any(not isinstance(key, str) or key not in catalog["maps"] for key in sequence)
            or len(sequence) != len(set(sequence))):
        raise ValueError("Native recipe campaign needs distinct source maps")
    mai = campaign.get("mode") == "mission_access_as_items" or (
        revision >= 8 and campaign.get("mode") == "Mission Access as Items")
    if mai:
        bands = campaign.get("enemy_progression_bands")
        maximum = len(sequence) - 1 if revision >= 9 else 4
        if (not isinstance(bands, dict) or any(type(bands.get(key)) is not int or not 0 <= bands[key] <= maximum for key in sequence)):
            raise ValueError("Native recipe campaign progression is invalid")
    composition_bands = campaign.get('composition_bands') if revision >= 3 else None
    if campaign.get('opening_stage') is not None:
        from .native_enemy_safety import OPENING_REGIONS
        if (revision < 8 or campaign['opening_stage'] not in OPENING_REGIONS
                or campaign['opening_stage'] not in sequence or not (enemies['count_mode'] == 'auto'
                    or revision >= 16 and uses_protected_opening(enemies))):
            raise ValueError('Native recipe opening needs a supported mission and progressive enemy count')
    if composition_bands is not None and (not isinstance(composition_bands, dict) or set(composition_bands) != set(sequence)
            or any(type(value) is not int for value in composition_bands.values())
            or sorted(composition_bands.values()) != list(range(len(sequence)))):
        raise ValueError('Native recipe composition bands are invalid')
    stage_count = (max(bands.values()) + 1 if revision >= 9 else 5) if mai else len(sequence)
    campaign_stage_count = stage_count
    for stage_index, map_key in enumerate(sequence):
        stage_count = campaign_stage_count
        if map_key not in catalog["maps"]:
            raise ValueError(f"Missing native recipe map: {map_key}")
        if catalog["maps"][map_key].get("unresolved"):
            raise ValueError(f"Unresolved native origin: {catalog['maps'][map_key]['unresolved'][0]['id']}")
        vanilla_balance = revision >= 13 and enemies.get('balance_preset')
        if vanilla_balance:
            from .vanilla_enemy_profiles import CANONICAL_ORDER
            if mai and map_key in CANONICAL_ORDER:
                stage_index, stage_count = CANONICAL_ORDER.index(map_key), len(CANONICAL_ORDER)
            elif not mai:
                from .native_enemy_balance import PURE_BOSS_MAPS
                pure_boss_maps = PURE_BOSS_MAPS - {'e3m4_boss'} if revision >= 15 else PURE_BOSS_MAPS
                combat_sequence = [key for key in sequence if key not in pure_boss_maps]
                if map_key in combat_sequence:
                    stage_index, stage_count = combat_sequence.index(map_key), len(combat_sequence)
        elif mai and revision >= 8:
            stage_index = campaign["enemy_progression_bands"][map_key]
        elif composition_bands is not None:
            stage_index = composition_bands[map_key]
        elif mai:
            stage_index = campaign["enemy_progression_bands"][map_key]
        denominator = max(1, stage_count - 1)
        intensity = Fraction(enemies["intensity"][0] * (denominator - stage_index)
                             + enemies["intensity"][1] * stage_index, 100 * denominator)
        limits = effective_special_limits(enemies, stage_index, stage_count)
        settings = {**enemies, "special_limits": limits}
        candidates = sorted(species for species in catalog["species"]
                            if enemies["weights"].get(species, 0) > 0 and limits.get(species, 1) > 0
                            and (species not in {'buff_pod', 'zombie_t1_screecher'} or
                                 (species == 'zombie_t1_screecher' and enemies.get('revision',2)<4) or limits['archvile'] > 0)
                            and (species not in {"marauder", "archvile", "spirit", "buff_pod"}
                                 and not (species == 'zombie_t1_screecher' and enemies.get('revision',2)>=4)
                                 or limits["combined_special"] > 0))
        recipes, protected, inactive = [], [], []
        phases = {}
        origins = [*catalog["maps"][map_key]["origins"], *catalog["maps"][map_key].get("runtime_origins", [])]
        if 14 <= revision < 19 and map_key == 'e5m4_boss':
            phantom_types = {catalog['species'][name]['spawn_type'] for name in
                             ('hell_knight', 'mancubus', 'baron', 'tyrant')}
            origins.extend({**source, 'id': source['id'].replace('/native_summons/', '/darklord_phantoms/'),
                'binding': {**source['binding'], 'entity': 'ap_darklord_phantoms'}, 'darklord_phantom': True}
                for source in catalog['maps'][map_key].get('runtime_origins', [])
                if source['spawn_type'] in phantom_types)
        if revision >= 6:
            from .native_enemy_safety import SCRIPTED_BOSS_ACTORS, CINEMATIC_BOSS_ACTORS, FLYING_SPAWN_POINTS, FLYING_REPLACEMENTS
            origins = [{**source, 'protected_reason': 'scripted boss acquisition actor'}
                       if (source.get('binding') or {}).get('entity') in SCRIPTED_BOSS_ACTORS.get(map_key, ())
                       or revision >= 13 and (source.get('binding') or {}).get('entity') in CINEMATIC_BOSS_ACTORS.get(map_key, ())
                       else {**source, 'protected_reason': 'airborne spawn requires a flying species'}
                       if (source.get('binding') or {}).get('reference') in FLYING_SPAWN_POINTS.get(map_key, ()) and not FLYING_REPLACEMENTS.intersection(candidates)
                       else {**source, 'allowed_species': sorted(FLYING_REPLACEMENTS)}
                       if (source.get('binding') or {}).get('reference') in FLYING_SPAWN_POINTS.get(map_key, ()) else source
                       for source in origins]
        if revision >= 11:
            from .native_enemy_safety import SMALL_GROUND_ENEMIES, GROUND_ENCOUNTERS
            origins = [{**source, 'allowed_species': sorted(SMALL_GROUND_ENEMIES)}
                       if ('ENCOUNTER_SPAWN_TENTACLE' in source.get('logical_types', ()) if revision < 13 else
                           source.get('logical_types') == ['ENCOUNTER_SPAWN_TENTACLE'])
                       else {**source, 'allowed_species': sorted(GROUND_ENCOUNTERS[map_key, source['binding']['owner']])}
                       if (map_key, (source.get('binding') or {}).get('owner')) in GROUND_ENCOUNTERS
                       else source for source in origins]
        if vanilla_balance:
            from .vanilla_enemy_profiles import SPECIAL_SPECIES, native_source_identity, reference_source, source_species, vanilla_profile
            lifetimes = {slot['source_id']: slot['renewable'] for wave in vanilla_profile(catalog, map_key)['waves']
                         for slot in wave['slots']}
            origins = [{**source, 'renewable': lifetimes[source['id']]} if source['id'] in lifetimes else source
                       for source in origins]
            stationary = ({'cueball'} if revision >= 14 else set(SPECIES_GROUPS['stationary']) - SPECIAL_SPECIES)
            origins = [{**source, 'protected_reason': 'unknown native population choice', 'vanilla_unknown': True}
                       if reference_source(source) and native_source_identity(source, catalog)['status'] == 'unknown_native_choice' else
                       {**source, 'protected_reason': 'native stationary spawn', 'vanilla_stationary': True}
                       if reference_source(source, catalog) and source_species(source, catalog) and
                       set(source_species(source, catalog)) <= stationary else
                       {**source, 'protected_reason': 'spawn outside the campaign reference'}
                       if not source.get('runtime_only') and not source.get('protected_reason') and
                       not source.get('inactive_reason') and not reference_source(source, catalog) else source for source in origins]
            origins = [{**source, 'protected_reason': 'native train entry geometry', 'vanilla_passthrough': True}
                       if map_key == 'e1m4_boss' and (source.get('binding') or {}).get('owner') ==
                       'train_encounter_manager_intro_train_1' and source.get('logical_types') ==
                       ['ENCOUNTER_SPAWN_MANCUBUS'] or source.get('family') == 'group' and
                       (source.get('binding') or {}).get('owner') == 'game_encounter_manager_traindepot_1' and
                       not (source.get('binding') or {}).get('reference') else source for source in origins]
        if vanilla_balance and revision >= 17:
            from .native_enemy_balance import prepare_encounter_sources
            origins = prepare_encounter_sources(origins, catalog, map_key)
        supplies = ammunition_sources(origins, catalog, finite=revision >= 9) if revision >= 4 else frozenset()
        if vanilla_balance:
            ammo_types = {catalog['species'][species]['spawn_type'] for species in AMMO_FODDER
                          if species in catalog['species']}
            supplies = {source['id'] for source in origins if source['id'] in supplies and
                        set(source.get('logical_types', ())) <= ammo_types}
        for source in origins:
            if not source.get("protected_reason") and not source.get("inactive_reason"):
                count = source["logical_count"]
                if type(count) is not int or not 0 < count <= 0x7fffffff:
                    raise ValueError(f"Invalid native request quantity: {source['id']}")
                phase = source.get('wave', source.get('phase', source['id'])) if revision >= 5 else source.get('phase', source['id'])
                phases.setdefault(phase, []).append(source)
        wave_order = {}
        if revision >= 5:
            for phase in phases:
                owner, _, number = phase.rpartition('/')
                if number.isdigit(): wave_order.setdefault(owner, set()).add(int(number))
        phase_bodies = {}
        phase_progression = {}
        arenas, extras = {}, {}
        if revision >= 10 and enemies.get('balance_preset') and not vanilla_balance:
            for phase, sources in phases.items():
                owner = phase.rsplit('/', 2)[0]
                arena = arenas.setdefault(owner, dict(key=[map_key, owner], peak=0, sources=[]))
                arena['peak'] = max(arena['peak'], sum(source['logical_count'] for source in sources))
                arena['sources'].extend(sources)
            for arena in arenas.values():
                scope = 14695981039346656037
                for character in arena['key'][1].encode('utf-8'):
                    scope = ((scope ^ character) * 1099511628211) & 0xffffffffffffffff
                arena['scope'] = scope
                eligible = [source for source in arena['sources'] if source['family'] in {'immediate', 'group', 'staggered'}
                            and not source.get('runtime_only') and source['id'] not in supplies]
                slots = [source['id'] for source in eligible for _ in range(source['logical_count'])]
                progress = Fraction(stage_index, max(1, stage_count - 1)) if enemies['progressive'] else 1
                count = len(slots) * (enemies['length'] - 100) // 100
                if revision >= 11:
                    count = int(count * progress)
                if slots:
                    for source_id in _random_for(seed, arena['key'], 'length').choices(slots, k=count):
                        extras[source_id] = extras.get(source_id, 0) + 1
        from .native_enemy_safety import opening_source
        opening_phases = {phase for phase, sources in phases.items()
                          if (revision >= 11 and map_key in campaign.get('starting_stages', ()) or
                              campaign.get('opening_stage') == map_key and
                              any(opening_source(map_key, source) for source in sources))}
        protected_opening_phases = opening_phases
        if vanilla_balance:
            opening_phases = {phase for phase, sources in phases.items() if
                (map_key in campaign.get('starting_stages', ()) or campaign.get('opening_stage') == map_key)
                and any(opening_source(map_key, source, verified=True) for source in sources)}
        vanilla_reference = None
        if vanilla_balance:
            from .native_enemy_balance import build_vanilla_stage
            phase_bodies, limits, vanilla_reference = build_vanilla_stage(origins, settings, campaign, catalog,
                seed=seed, map_key=map_key, stage_index=stage_index, stage_count=stage_count,
                supplies=supplies, opening_sources={source['id'] for phase, sources in phases.items()
                    if phase in protected_opening_phases for source in sources}, hitpoints=_body_hitpoints,
                recipe_revision=revision)
            settings = {**settings, 'special_limits': limits}
        for phase, sources in phases.items():
            phase_index, phase_count, phase_intensity = stage_index, stage_count, intensity
            if revision >= 5 and enemies['progressive']:
                owner, _, number = phase.rpartition('/')
                order = sorted(wave_order.get(owner, ()))
                fraction = Fraction(order.index(int(number)), len(order) - 1) if len(order) > 1 else Fraction(1, 2)
                denominator = max(1, stage_count - 1) * 1000
                position = fraction if stage_count == 1 else stage_index + fraction - Fraction(1, 2)
                phase_index = max(0, min(denominator, round(position * 1000)))
                phase_count = denominator + 1
                phase_intensity = Fraction(enemies['intensity'][0] * (denominator - phase_index)
                    + enemies['intensity'][1] * phase_index, 100 * denominator)
            if revision >= 5: phase_progression[phase] = [phase_index, phase_count]
            if vanilla_balance:
                continue
            if revision >= 10 and enemies.get('balance_preset'):
                generated = _balanced_phase_bodies(sources, candidates, settings, phase_intensity, seed=seed,
                    key=[map_key, phase], catalog=catalog, stage_index=phase_index, stage_count=phase_count,
                    supply_sources=supplies, extras=extras, arena=arenas[phase.rsplit('/', 2)[0]], opening=phase in opening_phases,
                    revision=revision, nightmare=campaign.get('difficulty') == 3)
            else:
                generate_bodies = _revision_one_bodies if revision == 1 else _revision_two_bodies if revision == 2 else _phase_bodies
                generated = generate_bodies(sources, candidates, settings, phase_intensity, seed=seed,
                    key=[map_key, phase], catalog=catalog, stage_index=phase_index, stage_count=phase_count,
                    **({'balanced': True, 'supply_sources': supplies, 'revision': revision,
                        'opening': phase in opening_phases} if revision >= 4 else {}))
            phase_bodies.update(generated)
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
            if source['id'] in supplies and not bodies and not vanilla_balance:
                raise ValueError(f"Ammunition source has no eligible chainsaw fodder: {source['id']}")
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
                **({'wave': source.get('wave', source.get('phase', source['id']))} if revision >= 5 else {}),
                "family": source["family"], "selected_spawn_type": selected_type,
                "runtime_only": source.get("runtime_only", False), "quantity_ratio": quantity_ratio,
                "logical_mask": enemy_spawn_mask(selected_type), "logical_count": logical_count,
                "physical_count": len(bodies), "maximum_emissions": physical_maximum,
                "renewable": source.get('renewable', maximum == 0) if vanilla_balance else maximum == 0,
                "final_credit_units": final_credit,
                "binding": dict(source["binding"]) if "binding" in source else None,
                "bodies": bodies})
        maps[map_key] = {"source_sha256": catalog["maps"][map_key]["source_sha256"],
                         "runtime_map": catalog["maps"][map_key].get("runtime_map", map_key),
                         "recipes": recipes, "protected": protected, "inactive": inactive,
                         "progression": [stage_index, stage_count], "special_limits": limits}
        if revision >= 5: maps[map_key]['wave_progression'] = phase_progression
        if vanilla_reference is not None:
            maps[map_key]['vanilla_reference'] = vanilla_reference
        original_cost = sum((Fraction(source["combat_cost"]) for sources in phases.values() for source in sources
                             if not source.get("runtime_only")), Fraction(0))
        actual_cost = sum((enemy_combat_cost(body["species"], body["attributes"], buff_totems=revision >= 6, screechers=revision >= 7)
                           for recipe in recipes if not recipe["runtime_only"] for body in recipe["bodies"]), Fraction(0))
        combat_costs[map_key] = {"original": float(original_cost), "target": float(original_cost * intensity),
                               "actual": float(actual_cost)}
        if revision >= 3:
            by_id = {source['id']: source for source in origins}
            active = [recipe for recipe in recipes if not recipe['runtime_only'] and not (
                by_id[recipe['id']].get('layers') and all('master' in layer.lower()
                for layer in by_id[recipe['id']]['layers']))]
            wave_costs, wave_bodies = {}, {}
            for recipe in active:
                source = by_id[recipe['id']]
                phase = recipe['wave'] if revision >= 5 else source.get('phase', source['id'])
                wave_costs[phase] = wave_costs.get(phase, Fraction(0)) + sum(
                    (enemy_combat_cost(body['species'], body['attributes'], buff_totems=revision >= 6, screechers=revision >= 7) for body in recipe['bodies']), Fraction(0))
                wave_bodies[phase] = wave_bodies.get(phase, 0) + len(recipe['bodies'])
            ordered = sorted(wave_costs.values())
            present = {body['species'] for recipe in active for body in recipe['bodies']}
            if revision >= 4 and any(source.get('protected_reason') and source.get('family') == 'spirit' for source in origins):
                present.add('spirit')
            combat_costs[map_key]['campaign'] = dict(
                actual=float(sum(ordered, Fraction(0))), bodies=sum(len(recipe['bodies']) for recipe in active),
                wave_p90=float(ordered[max(0, math.ceil(len(ordered) * .9) - 1)]) if ordered else 0,
                species=sorted(present))
            if revision >= 5:
                waves = [{'id': phase, 'cost': float(cost),
                          'bodies': wave_bodies[phase],
                          'progression': phase_progression[phase]} for phase, cost in sorted(wave_costs.items())]
                if revision >= 10 and enemies.get('balance_preset'):
                    maintenance = {}
                    for recipe in active:
                        if recipe['family'] == 'maintenance':
                            maintenance.setdefault(recipe['wave'].rsplit('/', 2)[0], []).extend(recipe['bodies'])
                    for wave in waves:
                        bodies = [body for recipe in active if recipe['wave'] == wave['id']
                                  and recipe['family'] != 'maintenance' for body in recipe['bodies']]
                        bodies += maintenance.get(wave['id'].rsplit('/', 2)[0], [])
                        wave.update(_wave_peak(bodies, mobile=revision >= 14))
                if opening_phases:
                    combat_costs[map_key]['campaign']['opening_waves'] = [wave for wave in waves if wave['id'] in opening_phases]
                    combat_costs[map_key]['campaign']['opening_species'] = sorted({body['species']
                        for recipe in active if recipe['wave'] in opening_phases for body in recipe['bodies']})
                cohorts, opening_cohorts = {}, {}
                for recipe in active:
                    identity = native_event_identity(recipe)
                    owner, script = identity.rsplit('/', 2)[:2] if identity else (recipe['id'], '0')
                    targets = [cohorts]
                    if vanilla_balance and recipe['wave'] in opening_phases:
                        targets.append(opening_cohorts)
                    for target_cohorts in targets:
                        cohort = target_cohorts.setdefault(owner, {}).setdefault(script, [0, 0, 0])
                        if recipe['renewable']:
                            cohort[2] += recipe['physical_count']
                        else:
                            cohort[0] += recipe['maximum_emissions']
                            cohort[1] += (min(recipe['maximum_emissions'], recipe['physical_count'] * 2)
                                          if recipe['family'] == 'maintenance' else recipe['maximum_emissions'])
                finite = sum(max(value[0] for value in scripts.values()) for scripts in cohorts.values())
                estimated = sum(max(value[1] + value[2] * 2 for value in scripts.values()) for scripts in cohorts.values())
                combat_costs[map_key]['campaign'].update(waves=waves, finite_spawns=finite,
                    estimated_kills=estimated, effort=estimated + len(waves) * 4)
                if vanilla_balance and opening_phases:
                    combat_costs[map_key]['campaign']['opening_estimated_kills'] = sum(
                        max(value[1] + value[2] * 2 for value in scripts.values()) for scripts in opening_cohorts.values())
    frozen_campaign = {"sequence": list(sequence), "mode": campaign.get("mode", "linear")}
    if revision >= 11:
        frozen_campaign['starting_stages'] = list(campaign.get('starting_stages', ()))
        frozen_campaign['difficulty'] = campaign.get('difficulty', 1)
    if campaign.get('opening_stage'):
        frozen_campaign['opening_stage'] = campaign['opening_stage']
    if composition_bands is not None:
        frozen_campaign['composition_bands'] = dict(composition_bands)
    if mai:
        frozen_campaign["enemy_progression_bands"] = {key: campaign["enemy_progression_bands"][key] for key in sequence}
    plan = {"kind": "native_recipes", "revision": revision, "seed": str(seed), "campaign": frozen_campaign, "maps": maps,
            "secret_timers": _secret_timers(maps, catalog), "combat_costs": combat_costs}
    plan["fingerprint"] = native_recipe_fingerprint(plan)
    return plan


_VALIDATED_PLANS = OrderedDict()
_VALIDATION_LOCK = Lock()
_VALIDATION_CACHE = None


def configure_native_validation_cache(directory, compiler_fingerprint):
    global _VALIDATION_CACHE
    if re.fullmatch(r'[0-9a-f]{64}', compiler_fingerprint) is None:
        raise ValueError('Native validation cache needs a compiler fingerprint')
    _VALIDATION_CACHE = Path(directory) / compiler_fingerprint


def native_recipe_seed(plan):
    if plan is None:
        return None
    return {"kind": "native_recipe_seed", **{key: plan[key] for key in
            ("revision", "seed", "campaign", "fingerprint")}}


def validate_native_recipes(plan, enemies, campaign=None, *, catalog=None):
    if plan is None:
        if enemies["enabled"]:
            raise ValueError("Enemizer requires frozen native recipes")
        return None
    if not isinstance(plan, dict) or not isinstance(plan.get("seed"), str) or not plan["seed"]:
        raise ValueError("Native recipe seed is invalid")
    compact = plan.get("kind") == "native_recipe_seed"
    if compact and (set(plan) != {"kind", "revision", "seed", "campaign", "fingerprint"}
                    or not isinstance(plan.get("fingerprint"), str)
                    or re.fullmatch(r'[0-9a-f]{64}', plan["fingerprint"]) is None):
        raise ValueError("Native recipe seed descriptor is invalid")
    campaign = plan.get("campaign") if campaign is None else campaign
    if not isinstance(campaign, dict) or not isinstance(campaign.get("sequence"), list):
        raise ValueError("Native recipe campaign is invalid")
    if catalog is None:
        catalog = json.loads(files(__package__).joinpath("native_enemy_catalog.json").read_bytes())
    key = hashlib.sha256(json.dumps([plan, enemies, campaign, catalog], sort_keys=True,
                                   separators=(",", ":"), allow_nan=False).encode("utf-8")).digest()
    with _VALIDATION_LOCK:
        if key not in _VALIDATED_PLANS:
            cached = _VALIDATION_CACHE / key.hex() if _VALIDATION_CACHE is not None else None
            try:
                verified = not compact and cached is not None and cached.stat().st_size == len(key) and cached.read_bytes() == key
            except OSError:
                verified = False
            if not verified:
                expected = build_native_recipes(enemies, campaign, seed=plan["seed"], catalog=catalog, revision=plan.get('revision'))
                if plan != (native_recipe_seed(expected) if compact else expected):
                    raise ValueError("Native recipes differ from the seed, options or source catalog")
                if cached is not None and not compact:
                    pending = None
                    try:
                        cached.parent.mkdir(parents=True, exist_ok=True)
                        with tempfile.NamedTemporaryFile(dir=cached.parent, delete=False) as output:
                            pending = Path(output.name)
                            output.write(key)
                        os.replace(pending, cached)
                    except OSError:
                        pass
                    finally:
                        if pending is not None:
                            try:
                                pending.unlink(missing_ok=True)
                            except OSError:
                                pass
            if compact:
                for old_key, value in list(_VALIDATED_PLANS.items()):
                    if value is not None:
                        del _VALIDATED_PLANS[old_key]
            _VALIDATED_PLANS[key] = expected if compact else None
            if len(_VALIDATED_PLANS) > 8:
                _VALIDATED_PLANS.popitem(last=False)
        _VALIDATED_PLANS.move_to_end(key)
        return _VALIDATED_PLANS[key] if compact else plan
