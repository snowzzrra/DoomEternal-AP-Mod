import pytest

from doom_eap.contracts.inventory_domain import selected_persistent_items


def test_selection_keeps_persistent_repairs_separate_from_consumables():
    assert selected_persistent_items() is None
    assert selected_persistent_items('weapons',7770002) == frozenset((7770002,))
    assert 7770002 not in selected_persistent_items('equipment')
    assert selected_persistent_items('persistent_upgrades',7770059) == frozenset((7770059,))
    with pytest.raises(ValueError):
        selected_persistent_items('equipment',7770002)
    with pytest.raises(ValueError):
        selected_persistent_items('all',7770016)


def test_resolved_starting_ownership_is_the_only_devinv_input():
    from tools.decls.devinv_builder import build_devinv_loadout, devinv_starting_arguments

    slot_data = {
        "identity": {"schema": 7},
        "starting": {"items": [[7770004, 1]], "weapon_item_id": 7770004},
        "starting_inventory": {"Heavy Cannon": 1}, "starting_weapon": "Heavy Cannon",
        "tracker_options": {"start_inventory": {"Sentinel Battery": 10}},
    }
    source = build_devinv_loadout(**devinv_starting_arguments(slot_data))
    assert source.count('item = "weapon/player/gauss_rifle";') == 1
    assert 'weapon/player/heavy_cannon' not in source
    assert 'CURRENCY_SENTINEL_BATTERY' not in source
