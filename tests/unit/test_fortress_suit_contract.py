import json
from pathlib import Path
import pytest


MOD_ROOT = Path(__file__).resolve().parents[2]


def test_fortress_suit_contract_and_positions():
    """Verify Fortress suit locations (7770253, 7770254, 7770255) spawn inside rooms with door contracts."""
    hub_config_path = MOD_ROOT / "level_configs" / "hub.json"
    data = json.loads(hub_config_path.read_text(encoding="utf-8"))
    policies = data.get("target_policies", {})

    # 7770253 - Praetor Suit
    praetor = policies.get("interact_hub_2_battery_station_1")
    assert praetor is not None
    assert praetor.get("independent_ap_trigger") is True
    assert praetor.get("independent_position") == [94.25, 58.45, -11.30]
    assert "native_entity_contract" in praetor

    # 7770254 - Sentinel Armor
    sentinel = policies.get("interact_hub_2_battery_station_2")
    assert sentinel is not None
    assert sentinel.get("independent_ap_trigger") is True
    assert sentinel.get("independent_position") == [-92.50, 85.90, -4.00]
    assert "native_entity_contract" in sentinel

    # 7770255 - Classic Marine Suit
    classic = policies.get("sentinel_battery_room_interact_hub_2_battery_station_1")
    assert classic is not None
    assert classic.get("independent_ap_trigger") is True
    assert classic.get("independent_position") == [0.55, -115.57, 6.34]
    assert "native_entity_contract" in classic
