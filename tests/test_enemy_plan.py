from copy import deepcopy

import pytest

from doom_eap.content.enemy_options import SPECIES_IDS, normalize_enemy_options
from doom_eap.content.enemy_plan import build_enemy_plan, enemy_combat_cost, sample_instance_attributes, sample_species, validate_frozen_enemy_plan
from doom_eap.content import enemy_species


def sample(enemies, key="actor", index=0, count=3):
    return sample_instance_attributes(enemies, "imp", seed=42, instance_key=key,
                                      stage_index=index, stage_count=count)


def test_variety_groups_individual_sources_by_combat_phase():
    from collections import defaultdict

    def phases(variety):
        enemies = normalize_enemy_options({"enemy_variety": variety})
        plan = build_enemy_plan(enemies, {"sequence": ["e2m1_nest"]}, seed=71404)
        groups = defaultdict(list)
        for operation in plan["maps"]["e2m1_nest"]["operations"]:
            key = operation["manager"]["entity"], operation["script_index"], operation["barrier"]["event_index"]
            groups[key].extend(actor["species"] for wave in operation["waves"] for actor in wave["instances"])
        return groups
    same = phases(0)
    assert any(len(actors) > 1 for actors in same.values())
    assert all(len(set(actors)) == 1 for actors in same.values())
    varied = phases(100)
    assert same.keys() == varied.keys()
    assert any(len(set(actors)) > 1 for actors in varied.values())


@pytest.mark.parametrize("multiplier", [50, 125, 500])
def test_multiplier_rounds_the_combat_phase_once(multiplier):
    import json
    from collections import defaultdict
    from pathlib import Path

    catalog = json.loads((Path(__file__).parents[1] / "content/catalog/encounter_composition.json").read_text())
    enemies = normalize_enemy_options({"enemy_count_mode": "multiplier", "enemy_count_multiplier": multiplier})
    plan = build_enemy_plan(enemies, {"sequence": ["e2m1_nest"]}, seed=71405)
    assert plan == build_enemy_plan(enemies, {"sequence": ["e2m1_nest"]}, seed=71405)
    counts = defaultdict(lambda: [0, 0])
    for encounter, operation in zip(catalog["maps"]["e2m1_nest"]["encounters"], plan["maps"]["e2m1_nest"]["operations"]):
        phase = operation["manager"]["entity"], operation["script_index"], operation["barrier"]["event_index"]
        counts[phase][0] += encounter["original_count"]
        counts[phase][1] += sum(len(wave["instances"]) for wave in operation["waves"])
    assert any(original > 1 for original, _ in counts.values())
    assert all(actual == (original * multiplier + 99) // 100 for original, actual in counts.values())
    assert plan["finite_kills"] == sum(actual for _, actual in counts.values())
    if multiplier == 50:
        assert any(not operation["waves"] for operation in plan["maps"]["e2m1_nest"]["operations"])


def test_pinata_prefers_ordinary_drones_and_keeps_their_native_speed():
    enemies = normalize_enemy_options({"enemy_preset": "pinata_party", "enemy_variety": 100})
    plan = build_enemy_plan(enemies, {"sequence": ["e2m1_nest", "e3m3_maykr"]}, seed=71407)
    actors = [actor for row in plan["maps"].values() for operation in row["operations"]
              for wave in operation["waves"] for actor in wave["instances"]]
    drones = [actor for actor in actors if actor["species"] == "zombie_maykr"]
    assert len(drones) > len(actors) / 2
    assert all(actor["spawn_type"] == "ENCOUNTER_SPAWN_ZOMBIE_MAYKR" for actor in drones)
    assert all(actor["attributes"]["speed_percent"] == 100 for actor in drones)
    assert all(actor["species"] != "maykr_angel" for actor in actors)


def fixed_combat(**options):
    return normalize_enemy_options({
        "enemy_variety": 100, "enemy_size_variation_chance": 0,
        **{f"enemy_{name}": {"minimum": 100, "maximum": 100}
           for name in ("health", "damage", "speed", "size")}, **options,
    })


def test_auto_intensity_spends_actual_cost_without_changing_requested_attributes():
    weights = {species: int(species == "imp") for species in SPECIES_IDS}
    campaign = {"sequence": ["e2m1_nest"]}
    plans = []
    for intensity, health in ((50, 100), (150, 100), (150, 200)):
        enemies = fixed_combat(enemy_weights=weights,
            enemy_intensity_early=intensity, enemy_intensity_late=intensity,
            enemy_health={"minimum": health, "maximum": health})
        plan = build_enemy_plan(enemies, campaign, seed=71408)
        costs = plan["combat_costs"]["e2m1_nest"]
        assert costs["target"] == costs["original"] * intensity / 100
        phases = {(operation["manager"]["entity"], operation["script_index"], operation["barrier"]["event_index"])
                  for operation in plan["maps"]["e2m1_nest"]["operations"]}
        assert abs(costs["actual"] - costs["target"]) <= len(phases) * (health / 100 + 1)
        supplies = sum("reserve" in operation for operation in plan["maps"]["e2m1_nest"]["operations"])
        assert costs["actual"] == plan["finite_kills"] * health / 100 + supplies
        assert all(actor["attributes"]["health_percent"] == health
                   for op in plan["maps"]["e2m1_nest"]["operations"]
                   for wave in op["waves"] for actor in wave["instances"])
        plans.append(plan)
    assert plans[1]["finite_kills"] > plans[0]["finite_kills"]
    assert plans[2]["finite_kills"] < plans[1]["finite_kills"]
    bad = deepcopy(plans[1])
    bad["combat_costs"]["e2m1_nest"]["actual"] += 1
    with pytest.raises(ValueError, match="differs from its actors"):
        validate_frozen_enemy_plan(bad, enemies)


def test_density_trades_more_cheap_actors_for_fewer_expensive_actors():
    weights = {species: int(species in ("imp", "stone_imp")) for species in SPECIES_IDS}
    totals = []
    for density in (50, 200):
        enemies = fixed_combat(enemy_weights=weights, enemy_density=density)
        plans = [build_enemy_plan(enemies, {"sequence": ["e2m1_nest"]}, seed=seed)
                 for seed in range(30)]
        totals.append((sum(plan["finite_kills"] for plan in plans),
                       sum(plan["combat_costs"]["e2m1_nest"]["actual"] for plan in plans)))
    assert totals[1][0] > totals[0][0]
    assert totals[1][1] / totals[1][0] < totals[0][1] / totals[0][0]


def test_multiplier_leaves_intensity_and_density_nonbinding_and_measures_the_result():
    plans = [build_enemy_plan(fixed_combat(enemy_count_mode="multiplier", enemy_count_multiplier=500,
                enemy_density=density, enemy_intensity_early=intensity, enemy_intensity_late=intensity),
                {"sequence": ["e2m1_nest"]}, seed=71409)
             for intensity, density in ((50, 50), (300, 200))]
    assert plans[0] == plans[1]
    costs = plans[0]["combat_costs"]["e2m1_nest"]
    assert costs["target"] is None
    operations = plans[0]["maps"]["e2m1_nest"]["operations"]
    assert costs["actual"] == float(sum(enemy_combat_cost(actor["species"], actor["attributes"])
        for op in operations for wave in op["waves"] for actor in wave["instances"])
        + sum("reserve" in op for op in operations))


def test_attributes_replay_independently_and_use_fixed_species_ranges():
    options = {
        "enemy_health": {"minimum": 30, "maximum": 70, "imp.minimum": 150, "imp.maximum": 200},
        "enemy_damage": {"minimum": 75, "maximum": 125},
        "enemy_speed": {"minimum": 50, "maximum": 150},
        "enemy_size": {"imp.minimum": 130, "imp.maximum": 130},
    }
    enemies = normalize_enemy_options(options)
    before = deepcopy(enemies)
    first = sample(enemies)
    assert sample(enemies) == first
    assert enemies == before
    assert 150 <= first["health_percent"] <= 200
    assert first["size_percent"] == 130
    assert sample(enemies, "next") != first
    changed = normalize_enemy_options({**options, "enemy_health": {"minimum": 400, "maximum": 400}})
    result = sample(changed)
    assert result["health_percent"] == 400
    assert {k: v for k, v in result.items() if k != "health_percent"} == {
        k: v for k, v in first.items() if k != "health_percent"
    }


def test_progression_interpolates_frozen_ranges_and_can_be_disabled():
    options = {"enemy_health": {"early_minimum": 60, "early_maximum": 60,
                                "late_minimum": 140, "late_maximum": 140}}
    enemies = normalize_enemy_options(options)
    assert [sample(enemies, index=i)["health_percent"] for i in range(3)] == [60, 100, 140]
    fixed = normalize_enemy_options({**options, "progressive_difficulty": False})
    assert [sample(fixed, index=i)["health_percent"] for i in range(3)] == [60, 60, 60]
    assert sample(enemies, count=1)["health_percent"] == 60


def test_speed_requires_positive_qualification_and_preserves_other_attributes(monkeypatch):
    enemies = normalize_enemy_options({
        "enemy_health": {"minimum": 125, "maximum": 125},
        "enemy_damage": {"minimum": 75, "maximum": 75},
        "enemy_speed": {"minimum": 150, "maximum": 150},
        "enemy_size": {"minimum": 90, "maximum": 90},
    })
    before = deepcopy(enemies)
    capabilities = enemy_species.enemy_speed_capabilities()
    for species in SPECIES_IDS:
        result = sample_instance_attributes(enemies, species, seed=42, instance_key="actor",
                                            stage_index=0, stage_count=1)
        assert result == {"health_percent": 125, "damage_percent": 75,
                          "speed_percent": 150 if capabilities[species] == "qualified" else 100, "size_percent": 90}
    assert enemies == before
    baseline = sample(enemies)
    monkeypatch.setattr(enemy_species, "enemy_speed_capabilities", lambda: {"imp": "qualified"})
    assert sample(enemies) == {**baseline, "speed_percent": 150}


def test_unusual_size_is_three_percent_split_evenly_and_respects_manual_size():
    enemies = normalize_enemy_options({})
    sizes = [sample(enemies, i)["size_percent"] for i in range(10000)]
    assert set(sizes) == {50, 100, 200}
    assert 250 <= sum(size != 100 for size in sizes) <= 350
    assert 110 <= sizes.count(50) <= 190
    assert 110 <= sizes.count(200) <= 190
    off = normalize_enemy_options({"enemy_size_variation_chance": 0})
    assert all(sample(off, i)["size_percent"] == 100 for i in range(100))
    manual = normalize_enemy_options({"enemy_size_variation_chance": 100,
                                      "enemy_size": {"minimum": 105, "maximum": 105}})
    assert all(sample(manual, i)["size_percent"] == 105 for i in range(100))


def test_frozen_plan_accounts_finite_sources_and_keeps_enemizer_off_unchanged():
    plan = {
        "revision": 3, "seed": "isolated", "finite_kills": 16,
        "maps": {"e1m1_intro": {"schema": 1, "map_key": "e1m1_intro",
                                  "source_sha256": "a" * 64, "operations": []}},
        "finite_sources": [{"map_key": "e1m1_intro", "location": "A reachable fight",
                            "count": 16, "repeatable": True}],
        "combat_costs": {"e1m1_intro": {"original": 0, "target": None, "actual": 0}},
        "secret_timers": {},
    }
    disabled = {"enabled": False}
    assert validate_frozen_enemy_plan(plan, disabled) is plan
    for schema in (2, 3, 4, 5):
        plan["maps"]["e1m1_intro"]["schema"] = schema
        assert validate_frozen_enemy_plan(plan, disabled) is plan
    plan["maps"]["e1m1_intro"]["schema"] = 1
    for mutate in (
        lambda p: p.update(finite_kills=17),
        lambda p: p["finite_sources"].append(deepcopy(p["finite_sources"][0])),
        lambda p: p["finite_sources"][0].update(repeatable=1),
        lambda p: p["finite_sources"][0].update(map_key="missing"),
        lambda p: p["maps"]["e1m1_intro"].update(map_key="different"),
        lambda p: p["maps"]["e1m1_intro"].update(schema=0),
        lambda p: p["maps"]["e1m1_intro"].update(operations=[{}]),
    ):
        bad = deepcopy(plan)
        mutate(bad)
        with pytest.raises(ValueError):
            validate_frozen_enemy_plan(bad, disabled)


def test_species_weights_are_relative_replayable_and_never_bypass_geometry_or_bans():
    zero = dict.fromkeys(SPECIES_IDS, 0)
    first = normalize_enemy_options({"enemy_weights": {**zero, "imp": 9000, "stone_imp": 1000}})
    scaled = normalize_enemy_options({"enemy_weights": {**zero, "imp": 900, "stone_imp": 100}})
    draws = [sample_species(first, ("stone_imp", "imp"), seed=42, instance_key=i) for i in range(1000)]
    assert draws == [sample_species(scaled, ("imp", "stone_imp"), seed=42, instance_key=i) for i in range(1000)]
    assert 860 <= draws.count("imp") <= 940
    assert sample_species(first, ("stone_imp",), seed=42, instance_key=0) == "stone_imp"
    banned = normalize_enemy_options({"enemy_weights": {"imp": 0, "stone_imp": 100}})
    with pytest.raises(ValueError, match="qualified restrictions"):
        sample_species(banned, ("imp",), seed=42, instance_key=0)


def test_secret_auto_timer_includes_unchanged_enemies_and_rejects_drift():
    from doom_eap.content.secret_encounter_timers import load_secret_encounter_timers

    enemies = fixed_combat(enemy_count_mode="multiplier", enemy_count_multiplier=500,
        enemy_weights={species: int(species == "imp") for species in SPECIES_IDS},
        enemy_health={"minimum": 200, "maximum": 200})
    plan = build_enemy_plan(enemies, {"sequence": ["e2m1_nest"]}, seed=71411)
    source = next(timer for timer in load_secret_encounter_timers()["maps"]["e2m1_nest"]["timers"]
                  if timer["manager"] == "game_encounter_manager_70_e2m1")
    duration = plan["secret_timers"]["e2m1_nest"][source["entity"]]
    # unchanged enemies and the initial supply Imp consume part of the timer
    assert duration == 121000
    bad = deepcopy(plan)
    bad["secret_timers"]["e2m1_nest"][source["entity"]] += 1
    with pytest.raises(ValueError, match="timers differ from the composition"):
        validate_frozen_enemy_plan(bad, enemies)


def test_mai_attributes_and_budget_follow_planned_bands_instead_of_list_order():
    options = fixed_combat(enemy_intensity_early=50, enemy_intensity_late=150,
                           enemy_health={"early_minimum": 100, "early_maximum": 100,
                                         "late_minimum": 200, "late_maximum": 200})
    stages = ["e1m1_intro", "e2m1_nest"]
    campaign = {"sequence": stages, "mode": "mission_access_as_items",
                "enemy_progression_bands": {stages[0]: 0, stages[1]: 4}}
    plan = build_enemy_plan(options, campaign, seed=71413)
    reverse = build_enemy_plan(options, {**campaign, "sequence": stages[::-1]}, seed=71413)
    assert plan["maps"] == reverse["maps"]
    for stage, health, intensity in ((stages[0], 100, .5), (stages[1], 200, 1.5)):
        actors = [actor for operation in plan["maps"][stage]["operations"]
                  for wave in operation["waves"] for actor in wave["instances"]]
        assert actors and all(actor["attributes"]["health_percent"] == health for actor in actors)
        costs = plan["combat_costs"][stage]
        assert costs["target"] == costs["original"] * intensity


def test_expensive_phase_has_one_private_supply_without_inflating_finite_kills():
    weights = {species: int(species == "stone_imp") for species in SPECIES_IDS}
    options = fixed_combat(enemy_weights=weights, enemy_count_mode="multiplier", enemy_count_multiplier=500)
    plan = build_enemy_plan(options, {"sequence": ["e1m1_intro"]}, seed=71415)
    operations = plan["maps"]["e1m1_intro"]["operations"]
    reserves = [operation["reserve"] for operation in operations if "reserve" in operation]
    phases = {(op["manager"]["entity"], op["script_index"], op["barrier"]["event_index"])
              for op in operations if op.get("barrier") and op["waves"]}
    assert len(reserves) == len(phases) and all(row["desired_count"] == 1 for row in reserves)
    assert options["weights"]["imp"] == 0
    assert reserves[0]["supply_source"]["species"] == "imp"
    actors = [actor for operation in operations for wave in operation["waves"] for actor in wave["instances"]]
    assert plan["finite_kills"] == len(actors)
    assert plan["combat_costs"]["e1m1_intro"]["actual"] == float(sum(enemy_combat_cost(a["species"], a["attributes"]) for a in actors) + len(reserves))
    next(operation for operation in operations if "reserve" in operation).pop("reserve")
    with pytest.raises(ValueError, match="renewable supply differs"):
        validate_frozen_enemy_plan(plan, options)


def test_density_can_sample_the_full_bestiary_with_exact_relative_weights():
    from doom_eap.content.enemy_plan import _density_weights

    options = normalize_enemy_options({"enemy_density": 200, "enemy_weights": {species: 1 for species in SPECIES_IDS}})
    weights = _density_weights(options, SPECIES_IDS, seed=71416, phase="arena", stage_index=2, stage_count=5)
    assert all(type(weight) is int and weight > 0 for weight in weights.values())
    selected = [sample_species({**options, "weights": weights}, SPECIES_IDS, seed=71416, instance_key=index)
                for index in range(100)]
    assert len(set(selected)) > 1


def test_very_low_health_retains_the_cost_of_targeting_each_actor():
    options = fixed_combat(enemy_weights={species: int(species == "imp") for species in SPECIES_IDS},
                           enemy_health={"minimum": 1, "maximum": 1})
    plan = build_enemy_plan(options, {"sequence": ["e1m1_intro"]}, seed=71416)
    actors = [actor for operation in plan["maps"]["e1m1_intro"]["operations"]
              for wave in operation["waves"] for actor in wave["instances"]]
    assert all(actor["attributes"]["health_percent"] == 1 for actor in actors)
    assert len(actors) <= 4 * plan["combat_costs"]["e1m1_intro"]["original"] + 2


def test_drone_only_combat_has_supply_without_assuming_a_precision_weapon():
    options = fixed_combat(enemy_weights={species: int(species == "zombie_maykr") for species in SPECIES_IDS})
    plan = build_enemy_plan(options, {"sequence": ["e1m1_intro"]}, seed=71417)
    combat = [operation for operation in plan["maps"]["e1m1_intro"]["operations"] if operation.get("barrier")]
    assert any(operation.get("reserve", {}).get("supply_source", {}).get("species") == "imp" for operation in combat)


def test_native_heavy_combat_keeps_supply_when_only_fodder_is_randomized():
    options = fixed_combat(enemy_weights={species: int(species == "imp") for species in SPECIES_IDS})
    plan = build_enemy_plan(options, {"sequence": ["e1m1_intro"]}, seed=71418)
    operations = plan["maps"]["e1m1_intro"]["operations"]
    assert all(actor["species"] == "imp" for operation in operations
               for wave in operation["waves"] for actor in wave["instances"])
    assert any("reserve" in operation for operation in operations)
    actor = next(actor for operation in operations for wave in operation["waves"] for actor in wave["instances"])
    actor["spawner_name"] = actor["spawner_name"].replace("_l00000001", "_l00000000", 1)
    with pytest.raises(ValueError, match="simultaneous limits"):
        validate_frozen_enemy_plan(plan, options)


def test_frozen_tracking_binds_every_actor_to_its_original_slot():
    from doom_eap.content.enemy_options import SPECIES_IDS, normalize_enemy_options
    options = normalize_enemy_options({"enemy_count_mode": "multiplier", "enemy_count_multiplier": 100,
        "enemy_weights": {species: int(species == "imp") for species in SPECIES_IDS}})
    plan = build_enemy_plan(options, {"sequence": ["e1m1_intro"]}, seed=71427)
    validate_frozen_enemy_plan(plan, options)
    assert plan["maps"]["e1m1_intro"]["schema"] == 7
    actor = plan["maps"]["e1m1_intro"]["operations"][0]["waves"][0]["instances"][0]
    actor["group_name"] = actor["group_name"].replace("_t", "_t0", 1)
    with pytest.raises(ValueError, match="tracking"):
        validate_frozen_enemy_plan(plan, options)


def test_native_whiplash_source_preserves_its_complete_mask_in_frozen_bodies():
    enemies = fixed_combat(enemy_count_mode="multiplier", enemy_count_multiplier=100,
        enemy_weights={species: int(species == "stone_imp") for species in SPECIES_IDS})
    plan = build_enemy_plan(enemies, {"sequence": ["e2m1_nest"]}, seed=71417)
    sources = [operation for operation in plan["maps"]["e2m1_nest"]["operations"]
               if operation["manager"]["entity"] in {"game_encounter_manager_71", "game_encounter_manager_56"}
               and operation["from"]["species"] == "whiplash"]
    assert len(sources) == 2
    for operation in sources:
        actors = [actor for wave in operation["waves"] for actor in wave["instances"]]
        assert actors
        assert all("_t00000000020000000000000000000020_l" in actor["ai_name"] for actor in actors)
    validate_frozen_enemy_plan(plan, enemies)


def single_phase_budget(monkeypatch):
    from doom_eap.content import enemy_plan
    catalog = deepcopy(enemy_plan._composition_catalog())
    profile = catalog["maps"]["e1m1_intro"]
    profile["encounters"] = profile["encounters"][:1]
    profile["native_phase_supply"] = []
    profile["encounters"][0]["eligible_species"] = ["imp", "stone_imp"]
    monkeypatch.setattr(enemy_plan, "_composition_catalog", lambda: catalog)
    return {"sequence": ["e1m1_intro"]}


def test_auto_uses_a_fitting_enemy_instead_of_overspending_on_a_heavy(monkeypatch):
    campaign = single_phase_budget(monkeypatch)
    weights = {species: int(species in ("imp", "stone_imp")) for species in SPECIES_IDS}
    enemies = fixed_combat(enemy_weights=weights)
    for seed in range(12):
        plan = build_enemy_plan(enemies, campaign, seed=seed)
        costs = plan["combat_costs"]["e1m1_intro"]
        assert costs["target"] == 2
        assert 1 <= costs["actual"] <= costs["target"]
        assert all(actor["species"] == "imp" for op in plan["maps"]["e1m1_intro"]["operations"]
                   for wave in op["waves"] for actor in wave["instances"])
        validate_frozen_enemy_plan(plan, enemies)


def test_auto_counts_initial_supply_before_admitting_another_heavy(monkeypatch):
    campaign = single_phase_budget(monkeypatch)
    enemies = fixed_combat(enemy_weights={species: int(species == "stone_imp") for species in SPECIES_IDS},
        enemy_intensity_early=300, enemy_intensity_late=300,
        enemy_health={"minimum": 100, "maximum": 100, "stone_imp.minimum": 50, "stone_imp.maximum": 50})
    plan = build_enemy_plan(enemies, campaign, seed=71418)
    costs = plan["combat_costs"]["e1m1_intro"]
    assert costs["target"] == 6
    assert costs["actual"] == 6
    assert plan["finite_kills"] == 1
    validate_frozen_enemy_plan(plan, enemies)


@pytest.mark.parametrize("seed", range(5))
def test_auto_keeps_one_cheapest_body_when_no_requested_body_fits(monkeypatch, seed):
    campaign = single_phase_budget(monkeypatch)
    enemies = fixed_combat(enemy_weights={species: int(species in ("imp", "stone_imp")) for species in SPECIES_IDS},
        enemy_health={"minimum": 100, "maximum": 100, "imp.minimum": 1000, "imp.maximum": 1000,
                      "stone_imp.minimum": 50, "stone_imp.maximum": 50})
    plan = build_enemy_plan(enemies, campaign, seed=seed)
    assert plan["finite_kills"] == 1
    assert plan["combat_costs"]["e1m1_intro"] == {"original": 2.0, "target": 2.0, "actual": 6.0}
    actor = plan["maps"]["e1m1_intro"]["operations"][0]["waves"][0]["instances"][0]
    assert actor["species"] == "stone_imp"
    assert actor["attributes"]["health_percent"] == 50
    validate_frozen_enemy_plan(plan, enemies)
