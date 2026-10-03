import pytest

from doom_eap.runtime.weapon_points import SentinelWeaponPoints, WeaponPointsBlocked


@pytest.mark.parametrize("abi,dash", [(4, 0), (4, 1), (5, 0), (5, 1), (5, 255)])
def test_independent_dash_observation(abi, dash):
    link = SentinelWeaponPoints.__new__(SentinelWeaponPoints)
    reply = {"inventory_abi": abi, "kind": 0, "outcome": 1, "flags": 3,
             "equipment_after": 0xffffffff, "ice_bomb_after": 255, "dash_after": dash}
    link._execute_typed = lambda *args: reply
    result = link.observe_inventory()
    assert result["dash_after"] == (dash if abi == 5 else 255)
    assert result["equipment_after"] == 0xffffffff
    reply["inventory_abi"] = 5
    reply["dash_after"] = 2
    with pytest.raises(WeaponPointsBlocked):
        link.observe_inventory()
