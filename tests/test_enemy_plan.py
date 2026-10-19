from copy import deepcopy

import pytest

from doom_eap.content.enemy_options import SPECIES_IDS, normalize_enemy_options
from doom_eap.content.enemy_plan import sample_instance_attributes, sample_species, validate_frozen_enemy_plan
from doom_eap.content import enemy_species


def sample(enemies, key="actor", index=0, count=3):
    return sample_instance_attributes(enemies, "imp", seed=42, instance_key=key,
                                      stage_index=index, stage_count=count)



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



def test_density_can_sample_the_full_bestiary_with_exact_relative_weights():
    from doom_eap.content.enemy_plan import _density_weights

    options = normalize_enemy_options({"enemy_density": 200, "enemy_weights": {species: 1 for species in SPECIES_IDS}})
    weights = _density_weights(options, SPECIES_IDS, seed=71416, phase="arena", stage_index=2, stage_count=5)
    assert all(type(weight) is int and weight > 0 for weight in weights.values())
    selected = [sample_species({**options, "weights": weights}, SPECIES_IDS, seed=71416, instance_key=index)
                for index in range(100)]
    assert len(set(selected)) > 1



def test_incompatible_enemy_plan_is_rejected_before_compilation():
    disabled = {"enabled": False}
    assert validate_frozen_enemy_plan(None, disabled) is None
    with pytest.raises(ValueError, match="incompatible"):
        validate_frozen_enemy_plan({"revision": 3, "maps": {}}, disabled)
    with pytest.raises(ValueError, match="requires"):
        validate_frozen_enemy_plan(None, {"enabled": True})
