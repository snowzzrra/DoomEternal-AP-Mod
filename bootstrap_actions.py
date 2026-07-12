"""Audited, versioned map-side onboarding actions.

The game consumed dev1 spools while reporting that its inherited entityDef was
unknown.  Revision 1 is therefore retained only as historical delivery
evidence; revision 2 deliberately owns a separate spool/state namespace.
"""

from collections.abc import Collection

BOOTSTRAP_REVISION = 2
BOOTSTRAP_ENTITY_PREFIX = "ap_bootstrap_v2_"
LEGACY_BOOTSTRAP_ENTITY_PREFIXES = ("ap_bootstrap_v1_",)
BOOTSTRAP_ENTITY_PREFIXES = (*LEGACY_BOOTSTRAP_ENTITY_PREFIXES, BOOTSTRAP_ENTITY_PREFIX)
TRIGGER_POLICIES = (
    "on_connect", "on_reconnect", "on_item_received", "on_supported_map_load",
    "manual_diagnostic",
)
REAPPLY_POLICIES = ("once_per_slot_revision", "once_per_map_load", "manual_only")
SUPPORTED_MAPS = (
    "game/sp/e1m1_intro/e1m1_intro",
    "game/sp/hub/hub",
    "game/sp/e1m2_war/e1m2_war",
    "game/sp/e1m3_cult/e1m3_cult",
)

FORBIDDEN_EFFECT_TERMS = (
    "give ", "giveplayerperk", "chrispy ", "g_giveextralives", "currency",
    "ap_check_", "ap_event_", "objective", "progress", "portal", "gate",
    "relay", "tutorial", "codex", "checkpoint", "activatelayer", "forcestat",
)

# The only locally evidenced PlayerStatModifier uses this explicit class and
# these two fields.  The source's inherit is precisely what dev1 reported as
# unavailable at runtime, so v2 intentionally instantiates the proven class
# directly instead of emitting that unavailable entityDef reference.
BOOTSTRAP_STAT_PRIMITIVE = {
    "source": "Tools/kaizo/e3m2_hell_patch2/maps/game/sp/e3m2_hell/e3m2_hell.entities:8213",
    "class": "idTarget_PlayerStatModifier",
    "inherit": None,
    "fields": ("gameStat", "value"),
    "value": 1,
}
INVALID_BOOTSTRAP_INHERITS = frozenset({"target/player_stat_modifier"})

# Canonical APWorld metadata is ``SUIT_PAGE_UNLOCKING_ITEM_IDS`` in
# Archipelago/worlds/doometernal/items.py.  This compact mirror is packaged
# with the bridge; validate_data verifies it remains exactly synchronized.
# Crystal progressives unlock the parent Suit tab. Frag/Ice base equipment is
# included because the vanilla Suit perk groups have their own acquisition
# preReqStat. Flame Belch is intentionally absent: it has no family in that
# Suit group, so no local UI/DECL evidence makes it a parent-tab unlocker.
SUIT_PAGE_UNLOCKING_ITEM_IDS = frozenset({
    7770011, 7770013, 7770017, 7770088, 7770092,
    7770097, 7770098, 7770099, 7770100, 7770101, 7770102, 7770103, 7770104,
    7770106, 7770107, 7770108, 7770109, 7770110, 7770111, 7770112, 7770113,
    7770114, 7770115, 7770116, 7770117, 7770118,
})
# Compatibility name for local callers; the page predicate is no longer
# limited to independent perks.
SUIT_UPGRADE_ITEM_IDS = SUIT_PAGE_UNLOCKING_ITEM_IDS

BOOTSTRAP_STAT_ALLOWLIST = {
    "rune_page": "STAT_RUNE_PAGE_UNLOCKED",
    "frag_acquired": "STAT_FRAG_GAINED",
    "ice_acquired": "STAT_ICE_BOMB_GAINED",
}

EQUIPMENT_ACQUISITION_STAT_AUDIT = {
    "frag_acquired": {
        "stat": "STAT_FRAG_GAINED",
        "readers": ("perkgroups/suit: Frag equipment family preReqStat",),
        "vanilla_writer": "throwable/player/frag_grenade.gameStat",
        "ui_loadout_only": True,
        "progression_reader_found": False,
        "force_stat_required": False,
    },
    "ice_acquired": {
        "stat": "STAT_ICE_BOMB_GAINED",
        "readers": ("perkgroups/suit: Ice equipment family preReqStat",),
        "vanilla_writer": "throwable/player/ice_bomb.gameStat",
        "ui_loadout_only": True,
        "progression_reader_found": False,
        "force_stat_required": False,
    },
}


def received_any_suit_upgrade(received_item_ids: Collection[int]) -> bool:
    """Whether persisted AP receipt makes the parent Suit tab eligible."""
    return not SUIT_PAGE_UNLOCKING_ITEM_IDS.isdisjoint(received_item_ids)


def _action(action, ownership, description, triggers):
    stat = BOOTSTRAP_STAT_ALLOWLIST[action]
    return {
        "action": action,
        "revision": BOOTSTRAP_REVISION,
        "entity_name": f"{BOOTSTRAP_ENTITY_PREFIX}{action}",
        "maps_supported": SUPPORTED_MAPS,
        "trigger_policy": triggers,
        "reapply_policy": "once_per_slot_revision",
        "required_ap_ownership": ownership,
        "stat": stat,
        "effects": ((stat, 1),),
        "forbidden_effects": FORBIDDEN_EFFECT_TERMS,
        "status": "experimental",
        "automatic_enabled": False,
        "description": description,
    }


BOOTSTRAP_ACTIONS = {
    "rune_page": _action(
        "rune_page", "at_least_one_rune",
        "Set only the Rune dossier page stat after AP Rune receipt.",
        ("on_connect", "on_reconnect", "on_item_received", "manual_diagnostic"),
    ),
    "frag_acquired": _action(
        "frag_acquired", "frag_grenade",
        "Set only the Frag acquisition stat after AP Frag receipt.",
        ("on_connect", "on_reconnect", "on_item_received", "on_supported_map_load"),
    ),
    "ice_acquired": _action(
        "ice_acquired", "ice_bomb",
        "Set only the Ice acquisition stat after AP Ice receipt.",
        ("on_connect", "on_reconnect", "on_item_received", "on_supported_map_load"),
    ),
}

REJECTED_BOOTSTRAP_HISTORY = {
    "suit_page_v2": {
        "revision": 2,
        "stat": "STAT_SUIT_PAGE_UNLOCKED",
        "status": "runtime_rejected",
        "reason": "stat-only candidate did not open the Suit page in runtime",
    }
}


def validate_bootstrap_catalogue(catalogue=BOOTSTRAP_ACTIONS):
    """Reject non-evidenced or gameplay-changing bootstrap entities."""
    action_names = set()
    entity_names = set()
    primitive = BOOTSTRAP_STAT_PRIMITIVE
    if primitive["inherit"] in INVALID_BOOTSTRAP_INHERITS:
        raise ValueError("Bootstrap primitive inherits the runtime-invalid entityDef")
    if primitive["class"] != "idTarget_PlayerStatModifier" or primitive["fields"] != ("gameStat", "value"):
        raise ValueError("Bootstrap primitive is not locally evidenced")
    for key, action in catalogue.items():
        if key not in BOOTSTRAP_STAT_ALLOWLIST:
            raise ValueError(f"Bootstrap action {key} is not allowlisted")
        if key in action_names or action.get("action") != key:
            raise ValueError("Bootstrap action IDs must be unique and canonical")
        action_names.add(key)
        if action.get("revision") != BOOTSTRAP_REVISION:
            raise ValueError(f"Bootstrap action {key} has an unexpected revision")
        if action.get("status") != "experimental" or action.get("automatic_enabled") is not False:
            raise ValueError(f"Bootstrap action {key} has an invalid runtime status")
        if action.get("entity_name") != f"{BOOTSTRAP_ENTITY_PREFIX}{key}":
            raise ValueError(f"Bootstrap action {key} has an unsafe entity name")
        if action["entity_name"] in entity_names:
            raise ValueError("Bootstrap entity names must be unique")
        entity_names.add(action["entity_name"])
        if any(trigger not in TRIGGER_POLICIES for trigger in action.get("trigger_policy", ())):
            raise ValueError(f"Bootstrap action {key} has an unsupported trigger")
        if action.get("reapply_policy") not in REAPPLY_POLICIES:
            raise ValueError(f"Bootstrap action {key} has an unsupported reapply policy")
        expected_stat = BOOTSTRAP_STAT_ALLOWLIST[key]
        if action.get("stat") != expected_stat or action.get("effects") != ((expected_stat, 1),):
            raise ValueError(f"Bootstrap action {key} must contain exactly one allowlisted effect")
        audit = EQUIPMENT_ACQUISITION_STAT_AUDIT.get(key)
        if audit and (audit["stat"] != expected_stat or not audit["ui_loadout_only"]
                      or audit["progression_reader_found"] or audit["force_stat_required"]):
            raise ValueError(f"Bootstrap action {key} lacks a safe acquisition-stat audit")
    return catalogue


validate_bootstrap_catalogue()
