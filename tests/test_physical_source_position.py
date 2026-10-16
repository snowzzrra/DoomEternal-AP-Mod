from itertools import permutations

import pytest

from tools.maps.ap_map_generator import (
    _spawn_position_from_block,
    generate_independent_pickup_trigger,
)


@pytest.mark.parametrize("axes", list(permutations(("x", "y", "z"))))
def test_physical_check_keeps_source_position_for_any_axis_order(axes):
    coordinates = {"x": -34.1, "y": 219.5, "z": -30.23}
    position = " ".join(f"{axis} = {coordinates[axis]};" for axis in axes)
    source = f"spawnPosition = {{ {position} }}"

    trigger = generate_independent_pickup_trigger("secret_sphere", "AP_CHECK_SECRET_SPHERE", source)

    assert _spawn_position_from_block(trigger) == (-34.1, 219.5, -30.23)


def test_physical_check_rejects_incomplete_source_position():
    with pytest.raises(ValueError, match="requires spawnPosition"):
        generate_independent_pickup_trigger("secret_sphere", "AP_CHECK_SECRET_SPHERE", "spawnPosition = { x = 1; y = 2; }")
