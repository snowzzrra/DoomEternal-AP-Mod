from copy import deepcopy
from dataclasses import replace

from doom_eap.content.recipe_codec import decode_recipes, encode_recipes
from doom_eap.content.slot_data_contract import canonical_bytes, recipe_digest, validate_slot_data
from tests.test_slot_data_contract import sample_catalog, sample_slot


def test_ordered_patterns_and_all_row_values_round_trip():
    catalog = sample_catalog()
    compact = sample_slot(catalog)
    data = validate_slot_data(compact, catalog)
    resolved = decode_recipes(data.recipes)
    row = resolved.maps["a"].rows[0]
    assert [body.template_ref for body in row.bodies] == ["imp", "imp", "zombie", "imp"]
    assert encode_recipes(resolved) == compact["recipes"]
    assert resolved.timers == (("2" * 16, 15000),)
    assert resolved.maps["a"].passthrough_sources == ("1" * 16,)


def test_deduplication_remaps_limits_profiles_and_patterns_deterministically():
    catalog = sample_catalog()
    resolved = decode_recipes(validate_slot_data(sample_slot(catalog), catalog).recipes)
    row = resolved.maps["a"].rows[0]
    alternate_limit = replace(row.bodies[0].limits, special=(0, 0, 0, 0))
    body = replace(row.bodies[0], limits=alternate_limit)
    second = replace(row, source_ref="1" * 16, bodies=(body, body, row.bodies[0], body))
    record = replace(resolved.maps["a"], rows=(second, row), passthrough_sources=())
    expanded = replace(resolved, maps={"a": record})
    packed = encode_recipes(expanded)
    reverse = replace(expanded, maps={"a": replace(record, rows=record.rows[::-1])})
    assert canonical_bytes(packed) == canonical_bytes(encode_recipes(reverse))
    assert (len(packed["limit_sets"]), len(packed["profiles"]), len(packed["patterns"])) == (2, 2, 2)
    admitted = validate_slot_data(sample_slot(catalog, packed), catalog)
    decoded = decode_recipes(admitted.recipes)
    assert decoded.maps["a"].rows == (row, second)
    assert recipe_digest(encode_recipes(decoded)) == recipe_digest(packed)
    changed = deepcopy(packed)
    changed["maps"]["a"]["rows"][1][10].reverse()
    assert recipe_digest(changed) != recipe_digest(packed)


def test_decoding_uses_no_randomness(monkeypatch):
    import random

    def forbidden(*args, **kwargs):
        raise AssertionError("Decoding must use resolved values")

    catalog = sample_catalog()
    data = validate_slot_data(sample_slot(catalog), catalog)
    monkeypatch.setattr(random.Random, "random", forbidden)
    monkeypatch.setattr(random.Random, "getrandbits", forbidden)
    assert encode_recipes(decode_recipes(data.recipes)) == sample_slot()["recipes"]


def test_disabled_recipes_preserve_timers_and_source_decisions():
    catalog = sample_catalog()
    recipes = deepcopy(sample_slot()["recipes"])
    recipes.update(enabled=False, limit_sets=[], profiles=[], patterns=[])
    recipes["maps"]["a"].update(rows=[], passthrough_sources=["0" * 16], disabled_sources=["1" * 16])
    admitted = validate_slot_data(sample_slot(catalog, recipes), catalog)
    assert encode_recipes(decode_recipes(admitted.recipes)) == recipes


def test_population_limits_and_explicit_ratio_round_trip():
    catalog = sample_catalog()
    recipes = sample_slot()["recipes"]
    recipes["limit_sets"][0]["population"] = {
        "scope": "0123456789abcdef", "maximum": 4, "heavy": 3, "superheavy": 1, "pressure": 2,
        "mixed": 1, "strict": True, "counter": "imp", "species_caps": [["imp", 2]]}
    recipes["maps"]["a"]["rows"][0][8] = 12
    admitted = validate_slot_data(sample_slot(catalog, recipes), catalog)
    resolved = decode_recipes(admitted.recipes)
    assert resolved.maps["a"].rows[0].ratio_numerator == 12
    assert encode_recipes(resolved) == recipes
