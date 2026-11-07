from copy import deepcopy
from dataclasses import replace

from doom_eap.content.recipe_codec import decode_recipes, encode_recipes
from doom_eap.content.slot_data_contract import canonical_bytes, recipe_digest, validate_slot_data
from tests.room_fixture import sample_catalog, sample_slot


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
