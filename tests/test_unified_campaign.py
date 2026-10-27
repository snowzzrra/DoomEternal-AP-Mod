import json
from pathlib import Path

import pytest

from doom_eap.runtime.unified_campaign import validate_plan


@pytest.mark.parametrize('surplus', [0, 1, 6])
def test_battery_surplus_accepts_budget_reductions_and_full_surplus(surplus):
    slot_data = json.loads((Path(__file__).parent / 'fixtures/full_campaign_slot_data.json').read_text())
    economy = slot_data['campaign_plan']['battery_economy']
    economy.update(surplus=surplus, total=economy['cost'] + surplus)
    assert validate_plan(slot_data) is slot_data['campaign_plan']


@pytest.mark.parametrize('changes', [
    {'surplus': -1, 'total': 21}, {'surplus': 7, 'total': 29},
    {'surplus': True, 'total': 23}, {'total': 27}, {'cost': 20},
    {'surplus_percent': 50}, {'native_first_battery': 1},
])
def test_battery_economy_rejects_inconsistent_spending(changes):
    slot_data = json.loads((Path(__file__).parent / 'fixtures/full_campaign_slot_data.json').read_text())
    slot_data['campaign_plan']['battery_economy'].update(changes)
    with pytest.raises(ValueError, match='Fortress Battery economy'):
        validate_plan(slot_data)
