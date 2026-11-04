"""Ammo charge accounting and staged-spend policy, independent of CommonClient."""

from dataclasses import dataclass

from doom_eap.strings import text as player_text

AMMO_REFILL_ITEM_ID = 7770024
AMMO_REFILL_PRIMITIVE_ITEM_ID = 7770042
AMMO_REFILL_CAPACITY = 3
AMMO_REFILL_CONFIRMATION_TIMEOUT = 15
AMMO_REFILL_COMMANDS = ("give ammo", "judgementMeter_Set 3")
AMMO_UNAVAILABLE_MESSAGES = {
    "placement_connected": player_text("ammo_refill", "disconnected"),
    "item_state_ready": player_text("ammo_refill", "waiting_for_items"),
    "connected": player_text("ammo_refill", "disconnected"),
    "namespace_match": player_text("ammo_refill", "waiting_for_session"),
    "marker_valid": player_text("ammo_refill", "waiting_for_level"),
    "lease_match": player_text("ammo_refill", "waiting_for_level"),
    "native_safe": player_text("ammo_refill", "unpause"),
    "runtime_effects_ready": player_text("ammo_refill", "waiting_for_save"),
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
