"""Enemy recipes and option normalization shared with standalone generation."""
from dataclasses import dataclass
from math import gcd


ENEMY_OPTIONS_REVISION = 8
SPECIES_GROUPS = {
    "fodder": ("chaingun_soldier", "demonic_trooper", "gargoyle", "hell_soldier", "imp", "lost_soul",
               "maykr_angel", "shotgun_soldier", "stone_imp", "zombie_maykr", "zombie_t1_screecher",
               "zombie_tier_1", "zombie_tier_3"),
    "heavy": ("arachnotron", "blood_angel", "cacodemon", "carcass", "cursed_prowler", "cyber_mancubus",
              "dread_knight", "hell_knight", "mancubus", "pain_elemental", "pinky", "prowler", "revenant",
              "spectre", "whiplash"),
    "superheavy": ("archvile", "armored_baron", "baron", "doom_hunter", "marauder", "tyrant"),
    "support": ("spirit",),
    "stationary": ("buff_pod", "cueball", "super_tentacle", "tentacle", "turret"),
}
SPECIES_IDS = tuple(sorted(species for group in SPECIES_GROUPS.values() for species in group))
DLC_SPECIES = frozenset({"armored_baron", "blood_angel", "chaingun_soldier", "cursed_prowler",
                         "demonic_trooper", "spirit", "stone_imp", "super_tentacle", "turret",
                         "zombie_t1_screecher"})


@dataclass(frozen=True)
class EnemyRecipe:
    intensity: tuple[int, int]
    density: int
    variety: int
    health: tuple[int, int, int, int]
    damage: tuple[int, int, int, int]
    speed: tuple[int, int, int, int]
    combined_special: int
    group_weights: tuple[int, int, int, int]


ENEMY_RECIPES = {
    "uac_patrol": EnemyRecipe((60, 85), 85, 40, (70, 105, 85, 125), (65, 95, 80, 105),
                              (85, 100, 90, 105), 1, (150, 75, 20, 20)),
    "sentinel_training": EnemyRecipe((80, 110), 95, 55, (75, 120, 90, 145), (75, 105, 85, 120),
                                     (90, 105, 95, 110), 1, (120, 100, 35, 25)),
    "slayers_path": EnemyRecipe((100, 145), 100, 65, (75, 135, 90, 180), (80, 120, 95, 145),
                                (90, 110, 95, 120), 2, (100, 100, 50, 35)),
    "hells_vanguard": EnemyRecipe((125, 185), 110, 70, (90, 160, 105, 200), (90, 135, 105, 165),
                                  (95, 115, 100, 125), 3, (90, 130, 80, 50)),
    "icons_wrath": EnemyRecipe((160, 230), 120, 75, (100, 180, 110, 230), (100, 150, 115, 185),
                               (100, 120, 105, 130), 4, (80, 150, 100, 65)),
    "gore_nest": EnemyRecipe((100, 140), 180, 60, (65, 115, 80, 145), (75, 110, 90, 130),
                             (90, 110, 95, 120), 2, (200, 60, 20, 35)),
    "gladiators_gauntlet": EnemyRecipe((110, 165), 65, 55, (90, 150, 105, 190), (90, 130, 100, 160),
                                       (90, 110, 95, 120), 2, (60, 150, 100, 35)),
    "cultist_chaos": EnemyRecipe((100, 170), 115, 90, (60, 165, 80, 210), (65, 140, 80, 170),
                                 (85, 120, 90, 130), 3, (100, 100, 100, 70)),
    "oops_all_imps": EnemyRecipe((100, 145), 100, 50, (60, 145, 80, 190), (75, 120, 90, 150),
                                 (90, 115, 95, 125), 0, (0, 0, 0, 0)),
    "pinata_party": EnemyRecipe((100, 150), 135, 55, (75, 135, 90, 180), (80, 120, 95, 145),
                                (90, 110, 95, 120), 2, (100, 100, 50, 35)),
}

BALANCE_PRESETS = {
    "uac_patrol": dict(groups=((85, 15, 0, 0), (70, 28, 1, 1)),
                       attributes=(100, 100, 100), length=100, superheavy=1, combined=1),
    "sentinel_training": dict(groups=((70, 30, 0, 0), (55, 40, 4, 1)),
                              attributes=(100, 100, 100), length=100, superheavy=1, combined=1),
    "slayers_path": dict(groups=((60, 40, 0, 0), (40, 49, 9, 2)),
                         attributes=(110, 105, 105), length=100, superheavy=2, combined=1),
    "hells_vanguard": dict(groups=((55, 45, 0, 0), (32, 50, 14, 4)),
                           attributes=(125, 115, 110), length=120, superheavy=2, combined=2),
    "icons_wrath": dict(groups=((50, 50, 0, 0), (26, 50, 18, 6)),
                        attributes=(135, 120, 110), length=130, superheavy=3, combined=3),
}


def _integer(value, name, minimum=0, maximum=10000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _toggle(raw, key, default):
    value = raw.get(key, default)
    if type(value) is not bool:
        raise ValueError(f"{key} must be a boolean")
    return value


def _recipe_number(raw, key, default, minimum=0, maximum=10000):
    value = raw.get(key, "auto")
    return default if value == "auto" else _integer(value, key, minimum, maximum)


def _curve(values):
    return {"early": list(values[:2]), "late": list(values[2:])}


def _attribute(raw, name, automatic, progressive):
    values = raw.get(f"enemy_{name}", {})
    if not isinstance(values, dict):
        raise ValueError(f"enemy_{name} must be an object")
    bounds = {"minimum", "maximum", "early_minimum", "early_maximum", "late_minimum", "late_maximum"}
    groups = {}
    for key, value in values.items():
        if not isinstance(key, str):
            raise ValueError(f"enemy_{name} range names must be text")
        species, separator, bound = key.rpartition(".")
        if not separator:
            species, bound = "global", key
        if bound not in bounds or (species != "global" and species not in SPECIES_IDS):
            raise ValueError(f"enemy_{name} has unknown range {key!r}")
        groups.setdefault(species, {})[bound] = _integer(value, f"enemy_{name}.{key}", 1)
    result = {"global": _curve(automatic), "species": {}}
    for species, entries in groups.items():
        if set(entries) == {"minimum", "maximum"}:
            ends = [entries["minimum"], entries["maximum"]] * 2
        elif set(entries) == bounds - {"minimum", "maximum"}:
            ends = [entries[key] for key in ("early_minimum", "early_maximum", "late_minimum", "late_maximum")]
        else:
            raise ValueError(f"enemy_{name}.{species} needs both fixed bounds or all four Early/Late bounds")
        if ends[0] > ends[1] or ends[2] > ends[3]:
            raise ValueError(f"enemy_{name}.{species} minimum exceeds maximum")
        if species == "global":
            result["global"] = _curve(ends)
        else:
            result["species"][species] = _curve(ends)
    if not progressive:
        for ranges in (result["global"], *result["species"].values()):
            ranges["late"] = list(ranges["early"])
    return result, set(groups)


def normalize_enemy_options(raw):
    revision = raw.get("enemies", {}).get("revision", ENEMY_OPTIONS_REVISION)
    if revision not in (2, 3, 4, 5, 6, 7, ENEMY_OPTIONS_REVISION):
        raise ValueError("Unsupported enemy options revision")
    preset = raw.get("enemy_preset", "slayers_path")
    if preset == "custom":
        preset = "slayers_path"
    if not isinstance(preset, str) or preset not in ENEMY_RECIPES:
        raise ValueError(f"Unknown Enemy Preset: {preset!r}")
    recipe = ENEMY_RECIPES[preset]
    balance = BALANCE_PRESETS.get(preset) if revision >= 6 else None
    enabled = _toggle(raw, "enemy_randomizer", True)
    progressive = _toggle(raw, "progressive_difficulty", True)
    dlc = _toggle(raw, "use_dlc_content", True)
    count_mode = raw.get("enemy_count_mode", "preset")
    if count_mode == "preset":
        count_mode = "multiplier" if preset == "oops_all_imps" else "auto"
    if count_mode not in ("auto", "multiplier"):
        raise ValueError("Enemy Count must be Auto or Multiplier")
    intensity = [_recipe_number(raw, f"enemy_intensity_{end}", recipe.intensity[index], 1)
                 for index, end in enumerate(("early", "late"))]
    if not progressive:
        intensity[1] = intensity[0]
    density = _recipe_number(raw, "enemy_density", 100 if balance else recipe.density, 1)
    length = _recipe_number(raw, "enemy_length", 100 if balance and revision >= 7 else
                            balance['length'] if balance else max(100, density), 100)
    variety = _recipe_number(raw, "enemy_variety", recipe.variety, 0, 100)
    multiplier = _recipe_number(raw, "enemy_count_multiplier", 500 if preset == "oops_all_imps" else 100, 1)
    eligible = [species for species in SPECIES_IDS if dlc or species not in DLC_SPECIES]
    weights = {species: (recipe.group_weights[index] if index < 4 else 0)
               for index, members in enumerate(SPECIES_GROUPS.values()) for species in members}
    if revision >= 3:
        weights['buff_pod'] = min(recipe.group_weights[2:])
    if revision >= 4:
        weights['zombie_t1_screecher'] = min(recipe.group_weights[2:])
    if revision >= 8:
        weights.update(tentacle=recipe.group_weights[0] // 2,
                       super_tentacle=recipe.group_weights[1] // 4,
                       turret=recipe.group_weights[1] // 2)
    if preset == "oops_all_imps":
        weights.update(imp=900, stone_imp=100)
    elif preset == "pinata_party":
        others = [species for species in SPECIES_IDS if species not in ("maykr_angel", "zombie_maykr", "blood_angel")
                  and weights[species] > 0]
        weights = {species: 5 if species in others else 0 for species in SPECIES_IDS}
        weights.update(zombie_maykr=80 * len(others), blood_angel=15 * len(others))
    overrides = raw.get("enemy_weights", {})
    if not isinstance(overrides, dict):
        raise ValueError("Enemy Weights must be an object")
    for species, weight in overrides.items():
        if species not in SPECIES_IDS:
            raise ValueError(f"Unknown Enemy Weight: {species!r}")
        weights[species] = _integer(weight, f"Enemy Weight {species}")
    weights = {species: weights[species] for species in eligible}
    if enabled and not any(weights.values()):
        raise ValueError("Enemy Weights exclude every eligible random combat species")
    divisor = gcd(*weights.values()) or 1
    weights = {species: weight // divisor for species, weight in weights.items()}
    attributes = {}
    size_groups = set()
    for name in ("health", "damage", "speed", "size"):
        automatic = (100, 100, 100, 100) if name == "size" else getattr(recipe, name)
        if balance and name != "size":
            ceiling = 100 if revision >= 7 and preset == 'slayers_path' else balance['attributes'][
                ("health", "damage", "speed").index(name)]
            automatic = (100, 100 if progressive else ceiling, 100, ceiling)
        elif revision >= 5 and progressive and name != "size":
            ceiling = 120 if name == "health" else 100
            automatic = (min(automatic[0], ceiling), min(automatic[1], ceiling), *automatic[2:])
            if name == 'damage':
                automatic = (*automatic[:2], min(automatic[2], 125), min(automatic[3], 125))
        attributes[name], groups = _attribute(raw, name, automatic, progressive)
        if name == "size":
            size_groups = groups
    chance = _recipe_number(raw, "enemy_size_variation_chance", 3, 0, 100)
    size_exclusions = eligible if "global" in size_groups else sorted(size_groups)
    limits = {"marauder": 1, "archvile": 1, "spirit": 1, "combined_special": recipe.combined_special}
    overrides = raw.get("enemy_special_limits", {})
    if not isinstance(overrides, dict):
        raise ValueError("Special Enemy Limits must be an object")
    for key, value in overrides.items():
        if key not in limits:
            raise ValueError(f"Unknown Special Enemy Limit: {key!r}")
        limits[key] = _integer(value, f"Special Enemy Limit {key}", maximum=2147483647)
    late_limits = {"marauder": max(1, recipe.combined_special),
                   "archvile": max(1, recipe.combined_special),
                   "spirit": max(1, recipe.combined_special),
                   "combined_special": recipe.combined_special * 2, **overrides}
    if balance:
        limits = {"marauder": 0, "archvile": 0, "spirit": 0, "combined_special": 0, **overrides}
        late_limits = {"marauder": 1, "archvile": 1, "spirit": 1,
                       "combined_special": balance['combined'], **overrides}
        if not progressive:
            limits = dict(late_limits)
    for endpoint in (limits, late_limits):
        if "combined_special" not in overrides:
            endpoint["combined_special"] = min(endpoint["combined_special"],
                sum(endpoint[name] for name in ("marauder", "archvile", "spirit")))
        for name in ("marauder", "archvile", "spirit"):
            if name not in overrides:
                endpoint[name] = min(endpoint[name], endpoint["combined_special"])
    if not progressive:
        late_limits = dict(limits)
    return {"revision": revision, "enabled": enabled, "progressive": progressive,
            **({"balance_preset": preset if balance else None, "length": length} if revision >= 6 else {}),
            **({"special_limit_overrides": dict(overrides)} if revision >= 7 else {}),
            "intensity": intensity, "density": density, "variety": variety, "count_mode": count_mode,
            "count_multiplier": multiplier, "weights": weights, "attributes": attributes,
            "size_variation_chance": chance, "size_variation_exclusions": size_exclusions,
            "special_limits": limits, "special_limits_late": late_limits}


def enemy_options_for_editing(raw):
    values = normalize_enemy_options({**raw, "use_dlc_content": True, "progressive_difficulty": True})
    result = {
        "enemy_randomizer": raw.get("enemy_randomizer", True),
        "enemy_preset": raw.get("enemy_preset", "slayers_path"),
        "progressive_difficulty": raw.get("progressive_difficulty", True),
        "enemy_intensity_early": raw.get("enemy_intensity_early", "auto"),
        "enemy_intensity_late": raw.get("enemy_intensity_late", "auto"),
        **{f"enemy_{key}": raw.get(f"enemy_{key}", "auto") for key in (
            "density", "variety", "count_multiplier",
            "size_variation_chance")},
        "enemy_count_mode": values["count_mode"],
        "enemy_weights": dict(raw.get("enemy_weights", {})),
        "enemy_length": raw.get("enemy_length", "auto"),
        "enemy_special_limits": dict(raw.get("enemy_special_limits", {})),
        "enemy_size": dict(raw.get("enemy_size", {})),
    }
    for attribute in ("health", "damage", "speed"):
        result[f"enemy_{attribute}"] = dict(raw.get(f"enemy_{attribute}", {}))
    return result
