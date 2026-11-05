"""Ammo charge accounting and staged-spend policy, independent of CommonClient."""

from dataclasses import dataclass

AMMO_REFILL_ITEM_ID = 7770024
AMMO_REFILL_PRIMITIVE_ITEM_ID = 7770042
AMMO_REFILL_CAPACITY = 3
AMMO_REFILL_CONFIRMATION_TIMEOUT = 15
AMMO_REFILL_COMMANDS = ("give ammo", "judgementMeter_Set 3")
AMMO_UNAVAILABLE_MESSAGES = {
    "placement_connected": 'Disconnected from Archipelago; reconnect before using Ammo Refill',
    "item_state_ready": 'Ammo Refill unavailable: waiting for items from Archipelago',
    "connected": 'Disconnected from Archipelago; reconnect before using Ammo Refill',
    "namespace_match": "Ammo Refill unavailable: waiting for the current room's game session",
    "marker_valid": 'Ammo Refill unavailable: waiting for the current level to finish preparing',
    "lease_match": 'Ammo Refill unavailable: waiting for the current level to finish preparing',
    "native_safe": 'Ammo Refill unavailable: return to DOOM and unpause before trying again',
    "runtime_effects_ready": 'Ammo Refill unavailable: waiting for the active save and game session',
}


@dataclass(frozen=True)
class AmmoCommandScope:
    state_key: str
    current_map: str | None
    save_slot: str | None
    crucible_active: bool
    materialization_lease: str | None = None


def active_crucible(special_mode, processed_ids):
    if special_mode in {"the_crucible", "The Crucible"}:
        return 7770007 in processed_ids
    if special_mode in {"progressive_special_weapon", "Progressive Special Weapon"}:
        return 7770901 in processed_ids
    return False
