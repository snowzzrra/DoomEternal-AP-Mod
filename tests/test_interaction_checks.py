import json
from pathlib import Path

import pytest

from tools.maps.ap_map_generator import add_ap_check_target, extract_target_names, find_matching_brace


def test_unmaykr_check_requires_acquired_state():
    catalog = json.loads((Path(__file__).parents[1] / "content/maps/hub/locations.json").read_text())
    entity = "sentinel_battery_room_interact_hub_unmaykr_case_1_1829410956"
    policy = catalog["target_policies"][entity]
    states = ["unmaykr_idle", *[f"{n}_shards_inserted" for n in range(1, 6)],
              "unmaykr_unlocked", "unnmaykr_acquired"]
    source = 'edit = { interaction = { stateData = { num = 8;\n' + '\n'.join(
        f'item[{i}] = {{ stateName = "interactables/progress/unmaykr_case/{name}"; }}'
        for i, name in enumerate(states)) + '\n} } targets = { num = 6;\n' + '\n'.join(
        f'item[{i}] = "{name}";' for i, name in enumerate(policy["native_entity_contract"]["original_targets"])) + '\n} }'
    result = add_ap_check_target(source, entity, "AP_CHECK_UNMAYKR", policy)
    assert extract_target_names(result) == policy["native_entity_contract"]["original_targets"]
    for i in range(8):
        start = result.index(f"item[{i}] = {{")
        end = find_matching_brace(result, result.index("{", start))
        assert ("AP_CHECK_UNMAYKR" in result[start:end]) == (i == 7)
    with pytest.raises(ValueError, match="missing or ambiguous"):
        add_ap_check_target(source.replace("unnmaykr_acquired", "unexpected"), entity, "AP_CHECK_UNMAYKR", policy)
