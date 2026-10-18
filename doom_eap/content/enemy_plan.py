"""Deterministic generation shared by APWorld and the content compiler."""

import hashlib
import json
import random
import re
import math
from copy import deepcopy
from fractions import Fraction
from importlib.resources import files
from pathlib import Path

from . import enemy_species

ENEMY_PLAN_REVISION = 4
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


def enemy_combat_cost(species, attributes):
    if set(attributes) != {f"{name}_percent" for name in ("health", "damage", "speed", "size")} or any(
            type(value) is not int or not 1 <= value <= 10000 for value in attributes.values()):
        raise ValueError("Enemy combat attributes require integer percentages in 1..10000")
    health, damage, speed, size = (Fraction(attributes[f"{name}_percent"])
                                   for name in ("health", "damage", "speed", "size"))
    # One kill still takes a targeting action when health is below a normal shot.
    return (BASE_COMBAT_COST[species] * max(health / 100, Fraction(1, 4)) * (100 + damage) / 200
            * (300 + speed) / 400 * (3 + max(size / 100, 100 / size)) / 4)


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


def _composition_catalog():
    path = files(__package__).joinpath("encounter_composition.json")
    if not path.is_file():
        path = Path(__file__).resolve().parents[2] / "content/catalog/encounter_composition.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _secret_timers(maps, catalog):
    from .secret_encounter_timers import load_secret_encounter_timers

    timers = {}
    for map_key, record in load_secret_encounter_timers()["maps"].items():
        if map_key not in maps:
            continue
        originals = {
            (row["operation"]["manager"]["entity"], row["operation"]["script_index"],
             row["operation"]["event_index"]): row
            for row in catalog["maps"][map_key]["encounters"]
        }
        timers[map_key] = {}
        for timer in record["timers"]:
            baseline = sum((BASE_COMBAT_COST[species] * count
                            for species, count in timer["original_enemies"].items()), Fraction(0))
            actual = baseline
            for operation in maps[map_key]["operations"]:
                if operation["manager"]["entity"] != timer["manager"]:
                    continue
                source = originals[(operation["manager"]["entity"], operation["script_index"], operation["event_index"])]
                actual -= BASE_COMBAT_COST[source["operation"]["from"]["species"]] * source["original_count"]
                if operation.get("reserve", {}).get("supply_source"):
                    actual += BASE_COMBAT_COST[operation["reserve"]["supply_source"]["species"]]
                actual += sum((enemy_combat_cost(actor["species"], actor["attributes"])
                               for wave in operation["waves"] for actor in wave["instances"]), Fraction(0))
            duration = math.ceil(timer["duration_ms"] * actual / baseline)
            if not 1 <= duration <= 0x7fffffff:
                raise ValueError("Enemy settings require a Secret Encounter timer outside the game's range")
            timers[map_key][timer["entity"]] = duration
    return timers


def _limit_suffix(enemies, species):
    limits = enemies["special_limits"]
    values = "".join(f"{limits[name]:08x}" for name in ("marauder", "archvile", "spirit", "combined_special"))
    kind = {"marauder": "m", "archvile": "a", "spirit": "s"}.get(species, "n")
    return f"_l{values}_k{kind}"


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


def _phase_supplier(phase, profile):
    sources = [row for row in profile["encounters"] if row.get("barrier") and
               (row["operation"]["manager"]["entity"], row["operation"]["script_index"],
                row["barrier"]["event_index"]) == tuple(phase)]
    supplier = next(((row, species) for species in ("imp", "zombie_tier_1", "zombie_tier_3", "hell_soldier", "shotgun_soldier", "gargoyle")
                     for row in sources if species in row["eligible_species"]), None)
    if supplier is None:
        native = next((row for row in profile.get("native_phase_supply", [])
                       if (row["manager"], row["script_index"], row["barrier_index"]) == tuple(phase)), {})
        source = native.get("supply_source")
        if source and source["species"] in enemy_species.AMMUNITION_SPECIES:
            return source, source["species"]
        raise ValueError("This combat phase has no compatible ammunition supplier")
    return supplier


def _phase_needs_supply(phase, profile, species):
    if phase[2] is None:
        return False
    native = next((row for row in profile.get("native_phase_supply", [])
                   if (row["manager"], row["script_index"], row["barrier_index"]) == tuple(phase)), {})
    supply = native.get("native_ammunition")
    if supply is not None:
        sources = [row for row in profile["encounters"] if row["operation"]["manager"]["entity"] == phase[0]
                   and row["operation"]["script_index"] == phase[1]]
        phase_sources = [row for row in sources if (row.get('barrier') or {}).get('event_index')==phase[2]]
        if (not isinstance(supply, dict) or set(supply) != {"event_index", "event_sha256", "event_def", "spawn_type", "label"}
                or not phase_sources or type(supply["event_index"]) is not int
                or not 0 <= supply["event_index"] < min(row["operation"]["event_index"] for row in phase_sources)
                or any(row['operation']['event_index'] < supply['event_index']
                    and (not row.get('barrier') or row['barrier']['event_index']>=supply['event_index']) for row in sources)
                or supply["event_def"] not in {"spawnSingleAI", "spawnAI", "staggeredAISpawn", "maintainAICount"}
                or not isinstance(supply["label"], str) or not isinstance(supply["event_sha256"], str)
                or not re.fullmatch("[0-9a-f]{64}", supply["event_sha256"])):
            raise ValueError("Preserved native ammunition source is invalid")
        mask = enemy_species.enemy_spawn_mask(supply["spawn_type"])
        reliable = enemy_species.enemy_spawn_mask(" ".join(enemy_species.AMMUNITION_SPECIES.values()))
        if mask & ~reliable or any((row["operation"]["event_index"] == supply["event_index"] or
                supply["event_def"] == "maintainAICount" and row['operation']['event_index']>supply['event_index']
                and enemy_species.enemy_spawn_mask(enemy_species.selected_spawn_type(row["operation"]["from"])) & mask
                and (not supply['label'] or not isinstance(row.get('source_label'),str) or row['source_label']==supply['label']))
                for row in profile["encounters"] if row["operation"]["manager"]["entity"] == phase[0]
                and row["operation"]["script_index"] == phase[1]):
            raise ValueError("Generated enemies overlap native ammunition replenishment")
        return Fraction(0)
    needed = native.get("needs_ammunition") or any(
        value not in enemy_species.AMMUNITION_SPECIES for value in species)
    return BASE_COMBAT_COST[_phase_supplier(phase, profile)[1]] if needed else Fraction(0)


def _phase_reserves(map_key, operations, profile, seed, catalog):
    phases = {}
    for index, operation in enumerate(operations):
        if not operation.get("barrier"):
            continue
        key = operation["manager"]["entity"], operation["script_index"], operation["barrier"]["event_index"]
        phases.setdefault(key, []).append((index, operation))
    reserves = {}
    for phase, rows in phases.items():
        actors = [actor for _, operation in rows for wave in operation["waves"] for actor in wave["instances"]]
        if not _phase_needs_supply(phase, profile, (actor["species"] for actor in actors)):
            continue
        first = min(rows, key=lambda row: row[1]["event_index"])[1]
        source, supplier = _phase_supplier(phase, profile)
        template = {**catalog["species"], **profile.get("species", {})}[supplier]
        token = hashlib.sha256(json.dumps([str(seed), map_key, *phase]).encode()).hexdigest()[:20]
        names = {field: "ap_v070_supply_" + token + "_" + suffix for field, suffix in
                 (("manager_name", "manager"), ("commit_name", "commit"), ("finish_name", "finish"),
                  ("label", "actors"), ("completion_flag", "done"))}
        reserves[min(rows, key=lambda row: row[1]["event_index"])[0]] = {
            **deepcopy(catalog["supply_templates"]), **names, "desired_count": 1,
            "min_spawn_delay": 1, "max_spawn_delay": 2,
            "supply_source": {"species": supplier, "spawn_type": template["spawn_type"],
                              "ai": deepcopy(template["ai"]),
                              "spawners": deepcopy(source["spawners"])}}
    return reserves


def _preserve_challenge_targets(actors, encounters, targets, enemies, *, seed, map_key,
                               stage_index, stage_count):
    pinned = set()
    fulfilled = set()
    for target in sorted(targets, key=lambda target: (len(target["species"]), target["name"])):
        species = set(target["species"])
        original = sum(row["original_count"] for row in encounters
                       if row["operation"]["from"]["species"] in species)
        matching = [(row["id"], index) for row in encounters
                    for index, (_, value, _) in enumerate(actors[row["id"]]) if value in species]
        needed = (1 if matching else min(original, 1)) if target["map_key"] is None else min(original, target["minimum"])
        allowed = {value for value in species if enemies["weights"].get(value, 0) > 0
                   and enemies["special_limits"].get(value, 1) > 0
                   and (value not in ("marauder", "archvile", "spirit")
                        or enemies["special_limits"]["combined_special"] > 0)}
        if not needed or not allowed:
            continue
        pinned.update(matching[:needed])
        missing = needed - len(matching)
        if missing <= 0:
            fulfilled.add(target["name"])
            continue
        for row in encounters:
            candidates = allowed.intersection(row["eligible_species"])
            if not candidates:
                continue
            for index, (key, value, attributes) in enumerate(actors[row["id"]]):
                slot = row["id"], index
                if slot in pinned or value in species:
                    continue
                value = sample_species(enemies, candidates, seed=seed,
                                       instance_key=[map_key, target["name"], key])
                attributes = sample_instance_attributes(enemies, value, seed=seed, instance_key=key,
                    stage_index=stage_index, stage_count=stage_count)
                actors[row["id"]][index] = key, value, attributes
                pinned.add(slot)
                missing -= 1
                if not missing:
                    break
            if not missing:
                break
        if missing:
            if target["map_key"] is not None:
                raise ValueError(f"{target['name']}: {map_key} has too few suitable targets at the chosen enemy count")
        else:
            fulfilled.add(target["name"])
    return fulfilled


def build_enemy_plan(enemies, campaign, *, seed, challenge_targets=()):
    if not enemies["enabled"]:
        return None
    seed = str(seed)
    catalog = _composition_catalog()
    maps, sources, combat_costs, progression = {}, [], {}, {}
    fulfilled_targets = set()
    sequence = campaign["sequence"]
    base_enemies = enemies
    stage_count = 5 if campaign.get("mode") == "mission_access_as_items" else len(sequence)
    for stage_index, map_key in enumerate(sequence):
        if campaign.get("mode") == "mission_access_as_items":
            stage_index = campaign["enemy_progression_bands"][map_key]
        profile = catalog["maps"].get(map_key)
        if profile is None:
            continue
        progression[map_key] = [stage_index, stage_count]
        enemies = {**base_enemies, "special_limits": effective_special_limits(base_enemies, stage_index, stage_count)}
        operations = []
        source_counts = {}
        original_cost, target_cost = Fraction(0), Fraction(0)
        phases = {}
        for encounter in profile["encounters"]:
            operation = encounter["operation"]
            phase = (operation["manager"]["entity"], operation["script_index"],
                     encounter["barrier"]["event_index"] if encounter["barrier"] else None)
            phases.setdefault(phase, []).append(encounter)
        actors = {row["id"]: [] for row in profile["encounters"]}
        reductions = {}
        for phase, encounters in phases.items():
            phase = [map_key, *phase]
            roster = [row for row in encounters for _ in range(row["original_count"])]
            _random_for(seed, phase, "positions").shuffle(roster)
            baseline = sum(BASE_COMBAT_COST[row["operation"]["from"]["species"]] * row["original_count"]
                           for row in encounters)
            denominator = max(1, stage_count - 1)
            intensity = Fraction(enemies["intensity"][0] * (denominator - stage_index)
                                 + enemies["intensity"][1] * stage_index, denominator)
            budget = baseline * intensity / 100
            fixed_count = ((len(roster) * enemies["count_multiplier"] + 99) // 100
                           if enemies["count_mode"] == "multiplier" else None)
            selection, shared = {}, {}
            for row in encounters:
                candidates = [species for species in row["eligible_species"] if enemies["weights"].get(species, 0) > 0
                              and enemies["special_limits"].get(species, 1) > 0
                              and (species not in ("marauder", "archvile", "spirit")
                                   or enemies["special_limits"]["combined_special"] > 0)]
                if not candidates:
                    continue
                selected = {**enemies}
                if fixed_count is None:
                    selected["weights"] = _density_weights(enemies, candidates, seed=seed, phase=phase,
                                                           stage_index=stage_index, stage_count=stage_count)
                selection[row["id"]] = selected, candidates
                shared[row["id"]] = sample_species(selected, candidates, seed=seed, instance_key=[phase, "group"])
            roster = [row for row in roster if row["id"] in selection]
            if not roster:
                reductions[encounters[0]["id"]] = {"reason": "no_weighted_enemy_fits", "requested_count": fixed_count}
                original_cost += baseline
                target_cost += budget
                continue
            common = set.intersection(*(set(values[1]) for values in selection.values()))
            if common:
                grouped = enemies
                if fixed_count is None:
                    grouped = {**enemies, "weights": _density_weights(enemies, sorted(common), seed=seed, phase=phase,
                                                                     stage_index=stage_index, stage_count=stage_count)}
                shared_species = sample_species(grouped, common, seed=seed, instance_key=[phase, "group"])
                shared = {identity: shared_species for identity in selection}
            spent, ordinal = Fraction(0), 0
            supplied = _phase_needs_supply(phase[1:], profile, ())
            while (ordinal < fixed_count if fixed_count is not None else ordinal == 0 or spent + supplied < budget):
                row = roster[ordinal % len(roster)]
                key = [*phase, ordinal]
                selected, candidates = selection[row["id"]]
                individual = _random_for(seed, key, "variety").randrange(100) < enemies["variety"]
                species = (sample_species(selected, candidates, seed=seed, instance_key=key)
                           if individual else shared[row["id"]])
                attributes = sample_instance_attributes(enemies, species, seed=seed, instance_key=key,
                    stage_index=stage_index, stage_count=stage_count)
                cost = enemy_combat_cost(species, attributes)
                needs_supply = supplied or _phase_needs_supply(phase[1:], profile, (species,))
                if fixed_count is None and spent + cost + needs_supply > budget:
                    if ordinal:
                        # cheap filler would replace the requested species distribution.
                        break
                    if not individual:
                        common = set.intersection(*(set(values[1]) for values in selection.values()))
                        candidates = sorted(common) if common else candidates
                    draws = {}
                    for candidate in candidates:
                        sampled = sample_instance_attributes(enemies, candidate, seed=seed, instance_key=key,
                            stage_index=stage_index, stage_count=stage_count)
                        candidate_supply = supplied or _phase_needs_supply(phase[1:], profile, (candidate,))
                        draws[candidate] = sampled, enemy_combat_cost(candidate, sampled), candidate_supply
                    fitting = [candidate for candidate, (_, value, supply) in draws.items()
                               if spent + value + supply <= budget]
                    if not fitting:
                        minimum = min(value + supply for _, value, supply in draws.values())
                        fitting = [candidate for candidate, (_, value, supply) in draws.items()
                                   if value + supply == minimum]
                    species = sample_species(selected, fitting, seed=seed, instance_key=key)
                    attributes, cost, needs_supply = draws[species]
                    if not individual:
                        shared = {identity: species if species in selection[identity][1] else value
                                  for identity, value in shared.items()}
                actors[row["id"]].append((key, species, attributes))
                spent += cost
                supplied = needs_supply
                ordinal += 1
            original_cost += baseline
            target_cost += budget
        fulfilled_targets.update(_preserve_challenge_targets(actors, profile["encounters"],
            [target for target in challenge_targets if target["map_key"] in (None, map_key)
             and target["name"] not in fulfilled_targets], enemies,
            seed=seed, map_key=map_key, stage_index=stage_index, stage_count=stage_count))
        actual_cost = sum((enemy_combat_cost(species, attributes)
                           for values in actors.values() for _, species, attributes in values), Fraction(0))
        for encounter in profile["encounters"]:
            operation = deepcopy(encounter["operation"])
            count = len(actors[encounter["id"]])
            wave_size = max(1, count) if encounter["barrier"] is None else encounter["wave_size"]
            staged = count > wave_size
            operation["barrier"] = deepcopy(encounter["barrier"])
            instances = []
            for index, (key, species, attributes) in enumerate(actors[encounter["id"]]):
                token = hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()[:20]
                template = {**catalog["species"], **profile.get("species", {})}[species]
                tracking = enemy_species.enemy_tracking_suffix(enemy_species.selected_spawn_type(operation["from"]), template["spawn_type"])
                suffix = f"{token}{tracking}{_limit_suffix(enemies, species)}__ap_v070_s{attributes['speed_percent']:05d}"
                instance = {
                    "species": species, "spawn_type": template["spawn_type"], "ai": deepcopy(template["ai"]),
                    "ai_name": "ap_v070_ai_" + suffix, "spawner_name": "ap_v070_spawn_" + suffix,
                    "attributes": attributes,
                }
                if operation["event_def"] != "spawnSingleAI":
                    instance["origin_spawner"] = deepcopy(encounter["spawners"][index % len(encounter["spawners"])])
                instance["group_name"] = "ap_v070_group_" + suffix
                instances.append(instance)
            waves = []
            for start in range(0, count, wave_size):
                wave = {"instances": instances[start:start + wave_size]}
                if staged and start:
                    wave.update(label=f"ap_v070_wave_{token}_{start // wave_size}",
                                min_spawn_stagger=0, max_spawn_stagger=0)
                waves.append(wave)
            operation["waves"] = waves
            if encounter["id"] in reductions:
                operation["reduction"] = reductions[encounter["id"]]
            operations.append(operation)
            source_counts[encounter["location"]] = source_counts.get(encounter["location"], 0) + count
        # shortcut: these counts cover profiled spawns; qualify campaign routes before sizing Killsanity.
        sources.extend({"map_key": map_key, "location": location, "count": count, "repeatable": True}
                       for location, count in source_counts.items() if count)
        maps[map_key] = {"schema": 7, "map_key": map_key, "source_sha256": profile["source_sha256"],
                         "operations": operations}
        for index, reserve in _phase_reserves(map_key, operations, profile, seed, catalog).items():
            operations[index]["reserve"] = reserve
            actual_cost += BASE_COMBAT_COST[reserve["supply_source"]["species"]]
        combat_costs[map_key] = {"original": float(original_cost),
                               "target": float(target_cost) if enemies["count_mode"] == "auto" else None,
                               "actual": float(actual_cost)}
    if not maps:
        raise ValueError("The selected missions have no supported enemy composition profiles yet")
    plan = {"revision": ENEMY_PLAN_REVISION, "seed": str(seed), "maps": maps,
            "finite_kills": sum(source["count"] for source in sources), "finite_sources": sources,
            "combat_costs": combat_costs, "secret_timers": _secret_timers(maps, catalog),
            "progression": progression}
    return validate_frozen_enemy_plan(plan, base_enemies)


def validate_frozen_enemy_plan(plan, enemies):
    if plan is None:
        if enemies["enabled"]:
            raise ValueError("Enemy Randomizer requires a frozen enemy generation plan")
        return None
    if not isinstance(plan, dict) or set(plan) != {
        "revision", "seed", "maps", "finite_kills", "finite_sources", "combat_costs", "secret_timers"
    } | ({"progression"} if plan.get("revision") == ENEMY_PLAN_REVISION else set()):
        raise ValueError("Enemy generation plan fields are invalid")
    if type(plan["revision"]) is not int or plan["revision"] not in (3, ENEMY_PLAN_REVISION):
        raise ValueError("Enemy generation plan revision is unsupported")
    if not isinstance(plan["seed"], str) or not plan["seed"]:
        raise ValueError("Enemy generation plan seed is missing")
    if type(plan["finite_kills"]) is not int or not 0 <= plan["finite_kills"] <= 0xffffffffffffffff:
        raise ValueError("Enemy generation plan finite kill total is invalid")
    maps = plan["maps"]
    if not isinstance(maps, dict) or not maps:
        raise ValueError("Enemy generation plan maps are missing")
    costs = plan["combat_costs"]
    if not isinstance(costs, dict) or costs.keys() != maps.keys():
        raise ValueError("Enemy generation combat cost maps are invalid")
    progression = plan.get("progression", {key: [0, 1] for key in maps})
    if not isinstance(progression, dict) or progression.keys() != maps.keys():
        raise ValueError("Enemy limit progression maps are invalid")
    base_enemies = enemies
    for key, row in maps.items():
        stage = progression[key]
        if not isinstance(stage, list) or len(stage) != 2:
            raise ValueError("Enemy limit progression fields are invalid")
        enemies = ({**base_enemies, "special_limits": effective_special_limits(base_enemies, *stage)}
                   if "special_limits" in base_enemies else base_enemies)
        if not isinstance(key, str) or re.fullmatch(r"[a-z0-9_]+", key) is None:
            raise ValueError("Enemy generation plan map key is invalid")
        if not isinstance(row, dict) or set(row) != {"schema", "map_key", "source_sha256", "operations"}:
            raise ValueError(f"Enemy generation map fields are invalid: {key}")
        if type(row["schema"]) is not int or row["schema"] not in (1, 2, 3, 4, 5, 6, 7) or row["map_key"] != key:
            raise ValueError(f"Enemy generation map identity is invalid: {key}")
        if not isinstance(row["source_sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", row["source_sha256"]) is None:
            raise ValueError(f"Enemy generation map source hash is invalid: {key}")
        if not isinstance(row["operations"], list):
            raise ValueError(f"Enemy generation map operations are invalid: {key}")
        if not enemies["enabled"] and row["operations"]:
            raise ValueError("Enemizer Off cannot contain enemy replacement operations")
        try:
            for operation in row["operations"]:
                if row["schema"] >= 7:
                    enemy_species.selected_spawn_type(operation["from"])
                reduction = operation.get("reduction")
                if reduction is not None:
                    if (not isinstance(reduction, dict) or set(reduction) != {"reason", "requested_count"}
                            or reduction["reason"] != "no_weighted_enemy_fits" or operation["waves"]
                            or reduction["requested_count"] is not None and (
                                type(reduction["requested_count"]) is not int or reduction["requested_count"] < 1)):
                        raise ValueError("Enemy generation reduction is invalid")
                for wave in operation["waves"]:
                    for instance in wave["instances"]:
                        enemy_species.validate_enemy_speed_percent(instance["species"], instance["attributes"]["speed_percent"])
                        suffix = f"{_limit_suffix(enemies, instance['species'])}__ap_v070_s{instance['attributes']['speed_percent']:05d}"
                        if row["schema"] >= 5 and any(
                                not instance[name].endswith(suffix) for name in ("ai_name", "spawner_name")):
                            raise ValueError("Enemy generation simultaneous limits differ from its options")
                        if row["schema"] >= 7:
                            suffix = enemy_species.enemy_tracking_suffix(enemy_species.selected_spawn_type(operation["from"]), instance["spawn_type"]) + suffix
                            if any(not instance[name].endswith(suffix) for name in ("ai_name", "spawner_name", "group_name")):
                                raise ValueError("Enemy generation tracking differs from its source slot")
        except (KeyError, TypeError) as error:
            raise ValueError(f"Enemy generation Speed fields are malformed: {key}") from error
        measured = costs[key]
        if not isinstance(measured, dict) or set(measured) != {"original", "target", "actual"}:
            raise ValueError("Enemy generation combat cost fields are invalid")
        for name, value in measured.items():
            if name == "target" and value is None:
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("Enemy generation combat cost is invalid")
        try:
            actual = sum((enemy_combat_cost(actor["species"], actor["attributes"])
                          for operation in row["operations"] for wave in operation["waves"]
                          for actor in wave["instances"]), Fraction(0))
        except (KeyError, TypeError, ZeroDivisionError) as error:
            raise ValueError("Enemy generation combat cost inputs are invalid") from error
        if row["schema"] >= 5:
            catalog = _composition_catalog()
            try:
                reserves = _phase_reserves(key, row["operations"], catalog["maps"][key], plan["seed"], catalog)
            except (KeyError, TypeError) as error:
                raise ValueError("Enemy generation renewable supply sources are invalid") from error
            if any(operation.get("reserve") != reserves.get(index) for index, operation in enumerate(row["operations"])):
                raise ValueError("Enemy generation renewable supply differs from the composition")
            actual += sum((BASE_COMBAT_COST[value["supply_source"]["species"]] for value in reserves.values()), Fraction(0))
        if measured["actual"] != float(actual):
            raise ValueError("Enemy generation combat cost differs from its actors")
    try:
        expected_timers = _secret_timers(maps, _composition_catalog())
    except (KeyError, TypeError) as error:
        raise ValueError("Enemy generation Secret Encounter timer sources are invalid") from error
    if plan["secret_timers"] != expected_timers:
        raise ValueError("Enemy generation Secret Encounter timers differ from the composition")
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
