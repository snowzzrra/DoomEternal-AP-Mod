"""Catalog keys and English display text from the bundled player option schema."""

import re


def option_choice_key(option_key: str, choice_key: object) -> str:
    key = re.sub(r"[^a-z0-9_]+", "_", str(choice_key).lower()).strip("_")
    if not key or not key[0].isalpha():
        key = "value_" + key
    return f"{option_key}.choice.{key}"


OPTION_TEXT_KEYS = {
    "mission_pool.choice.custom": "common.custom",
    "logic_difficulty.name": "common.logic_difficulty",
    "full_saga_final_boss.choice.randomized": "common.random",
    "goal.choice.acquire_the_unmaykr": "common.acquire_the_unmaykr",
    "additional_victory_requirements.choice.acquire_the_unmaykr": "common.acquire_the_unmaykr",
    "enemy_preset.choice.custom": "common.custom",
    "enemy_intensity_early.choice.auto": "common.auto",
    "enemy_intensity_late.choice.auto": "common.auto",
    "enemy_density.name": "common.density",
    "enemy_density.choice.auto": "common.auto",
    "enemy_length.choice.auto": "common.auto",
    "enemy_variety.name": "common.variety",
    "enemy_variety.choice.auto": "common.auto",
    "enemy_count_mode.choice.auto": "common.auto",
    "enemy_count_multiplier.choice.auto": "common.auto",
    "enemy_size_variation_chance.choice.auto": "common.auto",
    "killsanity_maximum_milestones.choice.auto": "common.auto",
    "starting_weapon.choice.random_weapon": "common.random",
    "praetor_suit_upgrades_in_pool.choice.auto": "common.auto",
    "praetor_suit_upgrades_in_pool.choice.random": "common.random",
}

SCHEMA_LABEL_OPTIONS = frozenset(
    [
        "custom_missions",
        "enabled_traps",
        "enemy_damage",
        "enemy_health",
        "enemy_size",
        "enemy_speed",
        "enemy_weights",
        "filler_weights",
        "item_pool_counts",
        "local_items",
        "non_local_items",
        "special_weapon",
        "trap_weights",
    ]
)

SCHEMA_LABEL_CHOICES = frozenset(
    [
        "enemy_count_mode.choice.preset",
        "enemy_special_limits.choice.archvile",
        "enemy_special_limits.choice.marauder",
        "enemy_special_limits.choice.spirit",
        "full_saga_final_boss.choice.davoth",
        "full_saga_final_boss.choice.icon_of_sin",
        "starting_weapon.choice.ballista",
        "starting_weapon.choice.chaingun",
        "starting_weapon.choice.combat_shotgun",
        "starting_weapon.choice.heavy_cannon",
        "starting_weapon.choice.plasma_rifle",
        "starting_weapon.choice.rocket_launcher",
        "starting_weapon.choice.super_shotgun",
    ]
)

ENEMY_ATTRIBUTES = frozenset({"enemy_health", "enemy_damage", "enemy_speed", "enemy_size"})


def option_catalog_key(key: str) -> str:
    return OPTION_TEXT_KEYS.get(key, "options." + key)


def translated_choice(option_key: str, choice_key: object) -> bool:
    return (bool(option_key) and option_key not in SCHEMA_LABEL_OPTIONS
            and option_choice_key(option_key, choice_key) not in SCHEMA_LABEL_CHOICES)


def schema_option_texts(schema: dict) -> dict[str, str]:
    texts = {}
    for option in schema["options"]:
        key = option["key"]
        texts[option_catalog_key(f"{key}.name")] = option["display_name"]
        texts[option_catalog_key(f"{key}.description")] = option["description"]
        _choice_texts(option, texts)
    return texts


def _choice_texts(option: dict, texts: dict) -> None:
    if option["key"] in ENEMY_ATTRIBUTES:
        _enemy_limit_texts(option, texts)
        return
    seen = set()
    for field in ("choices", "special_values", "entries"):
        for choice in option.get(field, []):
            key = option_choice_key(option["key"], choice["key"])
            if key in seen:
                raise ValueError(f"option text key collision: {key}")
            seen.add(key)
            if translated_choice(option["key"], choice["key"]):
                _shared_text(texts, option_catalog_key(key), choice["label"])


def _enemy_limit_texts(option: dict, texts: dict) -> None:
    for entry in option.get("entries", []):
        bound = entry["key"].rsplit(".", 1)[-1]
        _, separator, limit = entry["label"].rpartition(" — ")
        _shared_text(texts, f"options.enemy_limit.{bound}", limit)
        if "." in entry["key"]:
            _shared_text(texts, "options.enemy_limit.separator", separator)


def _shared_text(texts: dict, key: str, value: str) -> None:
    if key in texts and texts[key] != value:
        raise ValueError(f"{key}: inconsistent shared schema text")
    texts[key] = value
