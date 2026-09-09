"""Authored TAG loadout requirements, not AP receipts or observed engine state."""

from dataclasses import dataclass


TAG_REQUIRED_NORMAL_MOD_UPGRADES = frozenset({
    "perk/player/weapons/shotgun/pop_rocket_weakpoint_hit",
    "perk/player/weapons/shotgun/pop_rocket_faster_recharge",
    "perk/player/weapons/shotgun/pop_rocket_larger_explosion",
    "perk/player/weapons/shotgun/secondary_full_auto_faster_recovery",
    "perk/player/weapons/shotgun/secondary_full_auto_faster_charge",
    "perk/player/weapons/shotgun/secondary_full_auto_increased_movement_speed",
    "perk/player/weapons/heavy_cannon/bolt_action_faster_movement",
    "perk/player/weapons/heavy_cannon/bolt_action_faster_reload",
    "perk/player/weapons/heavy_cannon/burst_detonate_faster_charge",
    "perk/player/weapons/heavy_cannon/burst_detonate_primary_charge",
    "perk/player/weapons/heavy_cannon/burst_detonate_faster_recharge",
    "perk/player/weapons/plasma_rifle/secondary_aoe_no_primary_delay",
    "perk/player/weapons/plasma_rifle/secondary_aoe_faster_charge",
    "perk/player/weapons/plasma_rifle/secondary_microwave_faster_charge",
    "perk/player/weapons/plasma_rifle/secondary_microwave_max_range",
    "perk/player/weapons/rocket_launcher/detonate_proximity_flare",
    "perk/player/weapons/rocket_launcher/detonate_concussive",
    "perk/player/weapons/rocket_launcher/lockon_faster_recovery",
    "perk/player/weapons/rocket_launcher/lockon_decrease_lock_time",
    "perk/player/weapons/double_barrel/meat_hook_faster_reload",
    "perk/player/weapons/double_barrel/default_faster_reload",
    "perk/player/weapons/gauss_cannon/ballista_movement",
    "perk/player/weapons/gauss_cannon/ballista_larger_explosion",
    "perk/player/weapons/gauss_cannon/destroyer_charge_levels_aoe",
    "perk/player/weapons/gauss_cannon/destroyer_faster_charge_and_recovery",
    "perk/player/weapons/chaingun/turret_faster_equip",
    "perk/player/weapons/chaingun/turret_faster_movement",
    "perk/player/weapons/chaingun/energy_shell_faster_recharge",
    "perk/player/weapons/chaingun/energy_shell_dash_smash",
})

TAG_REQUIRED_BLOOD_PUNCH_PERKS = frozenset({
    "perk/player/blood_punch/area_of_effect",
    "perk/player/blood_punch/ai_charge_rate",
    "perk/player/blood_punch/max_charges",
})


@dataclass(frozen=True)
class AuthoredTagPrerequisites:
    normal_mod_upgrades: frozenset[str]
    blood_punch_perks: frozenset[str]
    provenance: str = "authored_tag_devinv"


AUTHORED_TAG_PREREQUISITES = AuthoredTagPrerequisites(
    TAG_REQUIRED_NORMAL_MOD_UPGRADES, TAG_REQUIRED_BLOOD_PUNCH_PERKS,
)
