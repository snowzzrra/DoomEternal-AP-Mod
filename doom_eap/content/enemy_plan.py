"""Deterministic generation shared by APWorld and the content compiler."""

import hashlib
import json
import random
import math
from fractions import Fraction

from . import enemy_species
from .enemy_options import SPECIES_GROUPS

ENEMY_DRAW_REVISION = 3

# Relative planning costs include body resistance, not the engine's spawn-point debit.
BASE_COMBAT_COST = {
    "zombie_tier_1": Fraction(1, 2), "zombie_tier_3": Fraction(1),
    "imp": Fraction(1), "hell_soldier": Fraction(2),
    "stone_imp": Fraction(10), "zombie_maykr": Fraction(4),
    "gargoyle": Fraction(3, 2), "lost_soul": Fraction(3, 2),
    "shotgun_soldier": Fraction(3), "chaingun_soldier": Fraction(4),
    "demonic_trooper": Fraction(2), "zombie_t1_screecher": Fraction(1),
    "cueball": Fraction(1, 2), "tentacle": Fraction(2), "buff_pod": Fraction(1),
    "turret": Fraction(4), "super_tentacle": Fraction(16), "arachnotron": Fraction(10),
    "revenant": Fraction(8), "hell_knight": Fraction(8), "dread_knight": Fraction(12),
    "prowler": Fraction(5), "cursed_prowler": Fraction(20), "carcass": Fraction(6),
    "pinky": Fraction(8), "spectre": Fraction(9), "cacodemon": Fraction(7),
    "mancubus": Fraction(14), "cyber_mancubus": Fraction(18), "pain_elemental": Fraction(16),
    "whiplash": Fraction(10), "baron": Fraction(24), "armored_baron": Fraction(36),
    "doom_hunter": Fraction(30), "marauder": Fraction(32), "tyrant": Fraction(40),
    "archvile": Fraction(48), "spirit": Fraction(32), "blood_angel": Fraction(24),
    "maykr_angel": Fraction(64),
}


def enemy_combat_cost(species, attributes, *, buff_totems=False, screechers=False):
    if set(attributes) != {f"{name}_percent" for name in ("health", "damage", "speed", "size")} or any(
            type(value) is not int or not 1 <= value <= 10000 for value in attributes.values()):
        raise ValueError("Enemy combat attributes require integer percentages in 1..10000")
    health, damage, speed, size = (attributes[f"{name}_percent"] for name in ("health", "damage", "speed", "size"))
    cost = BASE_COMBAT_COST[species]
    if buff_totems and species == 'buff_pod':
        cost *= 12
    if screechers and species == 'zombie_t1_screecher':
        cost *= 12
    size_numerator, size_denominator = (300 + size, 400) if size >= 100 else (3 * size + 100, 4 * size)
    # One kill still takes a targeting action when health is below a normal shot.
    return Fraction(cost.numerator * max(health, 25) * (100 + damage) * (300 + speed) * size_numerator,
                    cost.denominator * 100 * 200 * 400 * size_denominator)



def _density_weights(enemies, candidates, *, seed, phase, stage_index, stage_count):
    weights = {species: enemies["weights"][species] for species in candidates}
    density = enemies["density"]
    if density == 100:
        return weights
    # A separate estimate keeps selection independent of each actor's attribute draws.
    costs = {species: enemy_combat_cost(species, sample_instance_attributes(
        enemies, species, seed=seed, instance_key=[phase, "density", species],
        stage_index=stage_index, stage_count=stage_count)) for species in candidates}
    reference = sum(costs[species] * weight for species, weight in weights.items()) / sum(weights.values())
    tilt = Fraction(abs(density - 100), 100)
    adjusted = {species: weight * (1 + tilt * costs[species] / reference) ** (4 if density < 100 else -4)
                for species, weight in weights.items()}
    denominator = math.lcm(*(weight.denominator for weight in adjusted.values()))
    integers = {species: int(weight * denominator) for species, weight in adjusted.items()}
    divisor = math.gcd(*integers.values())
    return {species: weight // divisor for species, weight in integers.items()}



LIMITED_SPECIES = ('marauder', 'archvile', 'spirit', 'buff_pod', 'zombie_t1_screecher',
                   'blood_angel', 'cursed_prowler', 'stone_imp', 'armored_baron')


def _limit_suffix(enemies, species):
    limits = enemies["special_limits"]
    values = "".join(f"{limits[name]:08x}" for name in ("marauder", "archvile", "spirit", "combined_special"))
    kinds = {"marauder": "m", "archvile": "a", "spirit": "s"}
    if enemies.get('revision', 2) >= 3:
        kinds['buff_pod'] = 'b'
    if enemies.get('revision', 2) >= 4:
        kinds['zombie_t1_screecher'] = 'c'
    kind = kinds.get(species, "n")
    population = enemies.get('population_limits')
    prefix = ''
    if population:
        flags = int(population.get('strict', False)) << 1
        if population.get('counter'):
            if not population.get('strict') or population['counter'] != 'spirit':
                raise ValueError('Invalid strict population counter')
            flags |= 4
        if 'species' in population:
            caps = population['species']
            if not population.get('strict') or set(caps) != set(LIMITED_SPECIES) or any(
                    type(caps[name]) is not int or not 0 <= caps[name] <= 0x7fffffff for name in LIMITED_SPECIES):
                raise ValueError('Invalid strict species limits')
            prefix = '_v' + ''.join(f'{caps[name]:08x}' for name in LIMITED_SPECIES)
        prefix += f"_a{population['scope']:016x}_p" + ''.join(
            f"{(population[name] | flags | (population.get('heavy', 0) << 8) if name == 'mixed' else population[name]):08x}"
            for name in ('maximum', 'superheavy', 'pressure', 'mixed'))
        if species in {'baron', 'armored_baron', 'doom_hunter', 'tyrant'}:
            kind = 'h'
        elif species in {'zombie_tier_1', 'zombie_tier_3', 'imp', 'gargoyle', 'hell_soldier'}:
            kind = 'f'
        elif (population.get('heavy') or population.get('strict')) and species in SPECIES_GROUPS['fodder'] and species != 'zombie_t1_screecher':
            kind = 'o'
    return f"{prefix}_l{values}_k{kind}"



def effective_special_limits(enemies, stage_index, stage_count):
    if type(stage_count) is not int or type(stage_index) is not int or not 0 <= stage_index < stage_count:
        raise ValueError("Enemy limit progression is invalid")
    denominator = max(1, stage_count - 1)
    early = enemies["special_limits"]
    late = enemies.get("special_limits_late", early)
    limits = {name: (value * (denominator - stage_index) + late[name] * stage_index
                     + denominator // 2) // denominator for name, value in early.items()}
    if early["combined_special"] != late["combined_special"]:
        limits["combined_special"] = min(limits["combined_special"],
            sum(limits[name] for name in ("marauder", "archvile", "spirit")))
    return limits



def _random_for(seed, instance_key, field):
    key = json.dumps([ENEMY_DRAW_REVISION, seed, instance_key, field], separators=(",", ":"), sort_keys=True)
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



def sample_instance_attributes(enemies, species, *, seed, instance_key, stage_index, stage_count, random_draws=None):
    if type(stage_count) is not int or type(stage_index) is not int or not 0 <= stage_index < stage_count:
        raise ValueError("Enemy progression requires an included stage index")
    denominator = max(1, stage_count - 1)
    def draw(field, lower, upper):
        key = (field, lower, upper)
        if random_draws is None:
            return _random_for(seed, instance_key, field).randint(lower, upper)
        if key not in random_draws:
            random_draws[key] = _random_for(seed, instance_key, field).randint(lower, upper)
        return random_draws[key]
    attributes = {}
    for attribute in ("health", "damage", "speed", "size"):
        ranges = enemies["attributes"][attribute]
        bounds = ranges["species"].get(species, ranges["global"])
        lower, upper = (
            (bounds["early"][edge] * (denominator - stage_index)
             + bounds["late"][edge] * stage_index + denominator // 2) // denominator
            for edge in (0, 1)
        )
        attributes[f"{attribute}_percent"] = lower if lower == upper else draw(attribute, lower, upper)

    if enemy_species.enemy_speed_capabilities().get(species) != "qualified":
        attributes["speed_percent"] = 100

    if species not in enemies["size_variation_exclusions"]:
        chance = enemies["size_variation_chance"]
        variation = draw("size_variation", 0, 199) if chance else 0
        attributes["size_percent"] = 50 if variation < chance else 200 if variation < 2 * chance else 100
    return attributes



def validate_frozen_enemy_plan(plan, enemies):
    if plan is None:
        if enemies["enabled"]:
            raise ValueError("Enemy Randomizer requires the room's enemy plan. Generate the room with the matching APWorld.")
        return None
    if not isinstance(plan, dict) or plan.get("kind") != "native_recipes":
        raise ValueError("This enemy plan is incompatible. Generate the room with the v0.7 APWorld.")
    from .native_enemy_plan import validate_native_recipes
    return validate_native_recipes(plan, enemies)
