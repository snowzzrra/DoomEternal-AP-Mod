from doom_eap.content.slot_data_contract import generation_fingerprint
from doom_eap.runtime.campaign import validate_plan


def test_resolved_campaign_keeps_producer_decisions(resolved_room):
    data, _ = resolved_room
    campaign = data["campaign"]
    campaign["fortress"]["phase_thresholds"] = [0, 0, 1]
    campaign["access_items"] = []
    campaign["goal_as_item"] = False
    data["native_generation_fingerprint"] = generation_fingerprint(data)
    assert validate_plan(data) is campaign
    from doom_eap.runtime.campaign import UnifiedCampaign

    state = {}
    runtime = UnifiedCampaign(data, state, lambda: None)
    assert runtime.plan["fortress_phase_thresholds"] == [0, 0, 1]
    assert runtime.plan["access_items"] == {}
    assert runtime.snapshot(set(), [])['fortress_phase'] == 2
