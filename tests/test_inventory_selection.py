import pytest

from doom_eap.contracts.inventory_domain import selected_persistent_items


def test_selection_keeps_persistent_repairs_separate_from_consumables():
    assert selected_persistent_items() is None
    assert selected_persistent_items('weapons',7770002) == frozenset((7770002,))
    assert 7770002 not in selected_persistent_items('equipment')
    with pytest.raises(ValueError):
        selected_persistent_items('equipment',7770002)
    with pytest.raises(ValueError):
        selected_persistent_items('all',7770016)
