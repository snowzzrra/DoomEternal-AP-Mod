import json
from pathlib import Path
from doom_eap.runtime.context_registry import validate_slot_contract


def test_dlc_missions_do_not_require_dlc_equipment():
    slot = json.loads((Path(__file__).parent / "fixtures/full_campaign_slot_data.json").read_text(encoding="utf-8"))
    slot["use_dlc_content"] = False
    assert slot["include_dlc_missions"] is True
    assert validate_slot_contract(slot)["use_dlc_content"] is False


