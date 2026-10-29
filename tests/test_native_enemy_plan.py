import pytest

from doom_eap.content.enemy_options import SPECIES_IDS, normalize_enemy_options
from doom_eap.content.native_enemy_plan import build_native_recipes


def catalog():
    return {"schema_version": 1, "species": {
        "imp": {"spawn_type": "ENCOUNTER_SPAWN_IMP", "hitpoints": {"max": 200, "starting": 200},
                "ai": {"map_key": "base", "entity": "imp", "sha256": "b" * 64}},
        "stone_imp": {"spawn_type": "ENCOUNTER_SPAWN_STONE_IMP", "hitpoints": {"max": 200, "starting": 200},
                      "ai": {"map_key": "dlc", "entity": "stone_imp", "sha256": "c" * 64}}}, "maps": {"base": {
        "source_sha256": "a" * 64, "origins": [{"id": "base/source/0", "family": "group",
            "spawn_type": "ENCOUNTER_SPAWN_IMP", "logical_types": ["ENCOUNTER_SPAWN_IMP"],
            "logical_count": 2, "maximum_emissions": 8, "combat_cost": "2",
            "binding": {"mode": "event", "entity": "source", "reference": "spawn"}},
            {"id": "base/protected", "protected_reason": "tutorial"}]}}}


def settings(**options):
    return normalize_enemy_options({"enemies": {"revision": 5}, "enemy_size_variation_chance": 0,
        **{f"enemy_{name}": {"minimum": 100, "maximum": 100} for name in ("health", "damage", "speed", "size")},
        **options})


def test_auto_density_preserves_requests_and_frozen_revisions():
    source = catalog()
    first = source['maps']['base']['origins'][0]
    first.update(logical_count=3, maximum_emissions=6, phase='arena')
    source['maps']['base']['origins'].append({**first, 'id': 'base/later', 'logical_count': 1, 'maximum_emissions': 1})
    options = settings(enemy_density=150, enemy_weights={name: int(name == 'stone_imp') for name in SPECIES_IDS})
    plan = build_native_recipes(options, {'sequence': ['base']}, seed=7, catalog=source)
    recipes = plan['maps']['base']['recipes']
    assert all(r['physical_count'] >= r['logical_count'] for r in recipes)
    assert sum(r['physical_count'] for r in recipes) == 6
    assert recipes[0]['maximum_emissions'] == recipes[0]['physical_count'] * 2
    assert recipes[1]['maximum_emissions'] == recipes[1]['physical_count']
    frozen = build_native_recipes(options, {'sequence': ['base']}, seed=7, catalog=source, revision=7)
    assert [r['physical_count'] for r in frozen['maps']['base']['recipes']] == [5, 2]
    legacy = build_native_recipes(options, {'sequence': ['base']}, seed=7, catalog=source, revision=1)
    assert legacy['fingerprint'] != plan['fingerprint']
    with pytest.raises(ValueError, match='revision'):
        build_native_recipes(options, {'sequence': ['base']}, seed=7, catalog=source, revision=999)


def test_finite_maintenance_last_body_has_only_the_remaining_logical_credit():
    source = catalog()
    source["maps"]["base"]["origins"][0].update(family='maintenance', maximum_emissions=3,
        spawn_type='ENCOUNTER_SPAWN_CACODEMON', logical_types=['ENCOUNTER_SPAWN_CACODEMON'])
    options = settings(enemy_count_mode="multiplier", enemy_count_multiplier=150,
        enemy_weights={name: int(name == "imp") for name in SPECIES_IDS})
    plan = build_native_recipes(options, {"sequence": ["base"]}, seed=7, catalog=source)
    recipe, = plan["maps"]["base"]["recipes"]
    assert recipe["physical_count"] == 3 and recipe["maximum_emissions"] == 5
    assert recipe["final_credit_units"] == 1
    assert (4 * recipe["logical_count"] + recipe["final_credit_units"]) / recipe["physical_count"] == 3
