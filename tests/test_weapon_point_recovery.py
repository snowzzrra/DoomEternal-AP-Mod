import copy

import pytest

from doom_eap.runtime.weapon_points import WeaponPointReceipts, WeaponPointsBlocked


class Native:
    def __init__(self, gained=63, balance=3):
        self.gained, self.balance, self.grants = gained, balance, []

    def observe(self):
        return {"gained_after": self.gained}

    def grant_weapon_upgrade_points(self, amount, expected):
        assert expected == self.gained
        before = self.gained
        self.grants.append(amount)
        self.gained += amount
        self.balance += amount
        return {"gained_before": before, "gained_after": self.gained}


def test_lost_ledger_recovers_earned_points_without_refunding_spent_points():
    state, native, saved = {}, Native(), []
    history = {index * 2: f"receipt-{index}" for index in range(22)}
    persist = lambda: saved.append(copy.deepcopy(state))
    owner = WeaponPointReceipts(state, persist, native, "room-A")
    owner.reconcile(history)
    assert len(saved[-1]["weapon_points"]["receipts"]) == 21
    assert native.grants == [] and native.balance == 3
    for index, receipt in history.items():
        owner.deliver(index, receipt, history)
    assert native.grants == [3] and native.balance == 6 and native.gained == 66
    owner = WeaponPointReceipts(saved[-1], persist, native, "room-A")
    owner.reconcile(history)
    for index, receipt in history.items():
        owner.deliver(index, receipt, history)
    assert native.grants == [3]


@pytest.mark.parametrize("gained,count", [(63, 20), (64, 22), (-3, 22)])
def test_incomplete_or_incompatible_history_keeps_item_pending(gained, count):
    state, native = {}, Native(gained)
    owner = WeaponPointReceipts(state, lambda: None, native, "room-A")
    with pytest.raises(WeaponPointsBlocked):
        owner.reconcile({index: str(index) for index in range(count)})
    assert state == {} and native.grants == []


def test_recovery_persistence_failure_keeps_native_and_ledger_unchanged():
    state, native = {}, Native()
    def fail():
        raise OSError("disk full")
    owner = WeaponPointReceipts(state, fail, native, "room-A")
    with pytest.raises(OSError):
        owner.reconcile({index: str(index) for index in range(21)})
    assert state == {} and native.grants == []
