from copy import deepcopy

import pytest

from doom_eap.content.enemy_options import (
    DLC_SPECIES, ENEMY_RECIPES, SPECIES_IDS, enemy_options_for_editing, normalize_enemy_options,
)


def test_presets_resolve_controls_and_custom_preserves_effective_values():
    for preset in ENEMY_RECIPES:
        raw = {"enemy_preset": preset}
        before = deepcopy(raw)
        effective = normalize_enemy_options(raw)
        custom = dict(enemy_preset="custom", enemy_intensity_early=effective["intensity"][0],
                      enemy_intensity_late=effective["intensity"][1], enemy_density=effective["density"],
                      enemy_variety=effective["variety"], enemy_count_mode=effective["count_mode"],
                      enemy_count_multiplier=effective["count_multiplier"], enemy_weights=effective["weights"],
                      enemy_special_limits=effective["special_limits"])
        for attribute, ranges in effective["attributes"].items():
            if attribute != "size":
                custom[f"enemy_{attribute}"] = dict(zip(
                    ("early_minimum", "early_maximum", "late_minimum", "late_maximum"),
                    ranges["global"]["early"] + ranges["global"]["late"],
                ))
        assert normalize_enemy_options(custom) == effective
        assert raw == before


def test_gimmick_weights_are_aggregate_and_dlc_selection_is_independent_of_mission_pool():
    imps = normalize_enemy_options({"enemy_preset": "oops_all_imps"})
    assert {species: weight for species, weight in imps["weights"].items() if weight} == {"imp": 9, "stone_imp": 1}
    assert imps["count_mode"] == "multiplier" and imps["count_multiplier"] == 500
    scaled = enemy_options_for_editing({"enemy_preset": "oops_all_imps"})
    scaled["enemy_preset"] = "custom"
    scaled["enemy_weights"] = {species: weight * 100 for species, weight in scaled["enemy_weights"].items()}
    assert normalize_enemy_options(scaled) == imps
    full_weights = normalize_enemy_options({"enemy_preset": "pinata_party"})["weights"]
    for dlc in (True, False):
        pinata = normalize_enemy_options({"enemy_preset": "pinata_party", "use_dlc_content": dlc,
                                          "mission_pool": "base"})
        weights = pinata["weights"]
        assert weights["maykr_angel"] == 0
        other = sum(weight for species, weight in weights.items() if species not in ("zombie_maykr", "blood_angel"))
        if dlc:
            assert weights["zombie_maykr"] * 5 == other * 80
            assert weights["blood_angel"] * 5 == other * 15
        else:
            assert not DLC_SPECIES.intersection(weights)
            assert all(weight * full_weights["zombie_maykr"] == full_weights[species] * weights["zombie_maykr"]
                       for species, weight in weights.items())


def test_fixed_attributes_replace_curves_and_size_variation_is_separate():
    fixed = normalize_enemy_options({"enemy_health": {"minimum": 180, "maximum": 180,
                                                     "imp.minimum": 50, "imp.maximum": 75},
                                     "enemy_size": {"imp.minimum": 130, "imp.maximum": 130},
                                     "enemy_weights": {"imp": 0}})
    assert fixed["attributes"]["health"] == {
        "global": {"early": [180, 180], "late": [180, 180]},
        "species": {"imp": {"early": [50, 75], "late": [50, 75]}},
    }
    assert fixed["size_variation_exclusions"] == ["imp"] and fixed["size_variation_chance"] == 3
    assert fixed["weights"]["imp"] == 0
    raw = {"enemy_randomizer": False, "killsanity": True, "kills_per_check": 50,
           "resource_profile": "lean", "logic_difficulty": "Expert", "easier_secret_encounters": True}
    assert normalize_enemy_options(raw) == normalize_enemy_options({"enemy_randomizer": False})


@pytest.mark.parametrize("overrides", [
    {"enemy_health": {"minimum": 10}},
    {"enemy_speed": {"minimum": 110, "maximum": 90}},
    {"enemy_size": {"minimum": 0, "maximum": 100}},
    {"enemy_weights": {species: 0 for species in SPECIES_IDS}},
    {"enemy_weights": {"imp": True}},
])
def test_invalid_ranges_and_empty_main_pool_fail_before_generation(overrides):
    with pytest.raises(ValueError):
        normalize_enemy_options(overrides)
