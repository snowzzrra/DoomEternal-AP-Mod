import pytest

from doom_eap.content.slot_data_contract import generation_fingerprint
from doom_eap.runtime.unified_campaign import validate_plan
from tests import test_slot_admission

resolved_room = test_slot_admission.resolved_room


@pytest.mark.parametrize('surplus', [0, 1, 6])
def test_battery_surplus_accepts_budget_reductions_and_full_surplus(surplus):
    from tests.room_fixture import current_room_fixture

    slot_data = current_room_fixture()
    economy = slot_data['campaign']['fortress']['battery_economy']
    economy.update(surplus=surplus, total=economy['cost'] + surplus)
    slot_data['native_generation_fingerprint'] = generation_fingerprint(slot_data)
    assert validate_plan(slot_data) is slot_data['campaign']


@pytest.mark.parametrize('changes', [
    {'surplus': -1, 'total': 21}, {'surplus': 7, 'total': 29},
    {'surplus': True, 'total': 23}, {'total': 27}, {'cost': 20},
    {'surplus_percent': 50}, {'native_first_battery': 1},
])
def test_battery_economy_rejects_inconsistent_spending(changes):
    from tests.room_fixture import current_room_fixture

    slot_data = current_room_fixture()
    slot_data['campaign']['fortress']['battery_economy'].update(changes)
    slot_data['native_generation_fingerprint'] = generation_fingerprint(slot_data)
    with pytest.raises(ValueError, match='battery|Battery|boolean'):
        validate_plan(slot_data)


def test_resolved_campaign_keeps_producer_decisions(resolved_room):
    data, _ = resolved_room
    campaign = data["campaign"]
    campaign["fortress"]["phase_thresholds"] = [0, 0, 1]
    campaign["access_items"] = []
    campaign["goal_as_item"] = False
    data["native_generation_fingerprint"] = generation_fingerprint(data)
    assert validate_plan(data) is campaign
    from doom_eap.runtime.unified_campaign import UnifiedCampaign

    state = {}
    runtime = UnifiedCampaign(data, state, lambda: None)
    assert runtime.plan["fortress_phase_thresholds"] == [0, 0, 1]
    assert runtime.plan["access_items"] == {}
    assert runtime.snapshot(set(), [])['fortress_phase'] == 2


def test_resolved_campaign_does_not_require_chronological_selection(resolved_room):
    from copy import deepcopy

    from tests.test_slot_data_contract import sign_slot

    data, catalog = resolved_room
    catalog["stages"]["e1m2_war"] = {"access_id": 7771001}
    catalog["maps"]["e1m2_war"] = {"stage_id": "e1m2_war"}
    data["campaign"]["sequence"] = ["e1m2_war", "e1m1_intro"]
    data["stage_profiles"]["e1m2_war"] = deepcopy(data["stage_profiles"]["e1m1_intro"])
    sign_slot(data, catalog)
    assert validate_plan(data)["sequence"] == ["e1m2_war", "e1m1_intro"]
