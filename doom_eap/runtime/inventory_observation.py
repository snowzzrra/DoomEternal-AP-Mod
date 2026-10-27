"""Read native inventory observations for the current room and campaign."""

import time

from doom_eap.contracts.inventory_domain import (
    InventoryObservation, ItemObservation, MISSING, OWNED, PERMANENT_PERK_ITEM_IDS,
)
from doom_eap.contracts.materialization import GATE_KEY_TO_MAP
from doom_eap.contracts.runtime_context import canonical_map_name
from doom_eap.runtime.context_registry import CONTEXT_BY_IDENTITY


class SentinelInventoryObservation:
    def __init__(self, context, logger, log_event):
        self.context = context
        self.logger = logger
        self.log_event = log_event
        self.last_logged = None

    def observe_inventory(self, *, room_seed_name, epoch, context_identity, campaign):
        try:
            link = self.context.native_game_link()
            result = link.observe_inventory()
        except (RuntimeError, OSError, ValueError) as error:
            self.logger.info("INVENTORY_OBSERVATION_DEFERRED detail=%s", error)
            return None
        native_scope = result.get("_observation_scope")
        runtime_context = CONTEXT_BY_IDENTITY.get(context_identity)
        if native_scope and (runtime_context is None or runtime_context.campaign != campaign or
                canonical_map_name(result.get("_observation_map")) not in runtime_context.runtime_maps):
            return None
        items = {}
        weapons = result["weapons_after"]
        if weapons != 0xffffffff:
            for bit, item_id in enumerate((7770900, 7770003, 7770000, 7770005, 7770001,
                                           7770004, 7770002, 7770006, 7770010, 7770008)):
                items[item_id] = ItemObservation(item_id, OWNED if weapons & (1 << bit) else MISSING,
                                                source="sentinel_inventory")
        frag = result.get("frag_grenade_after", 255)
        if frag in (0, 1):
            items[7770011] = ItemObservation(7770011, OWNED if frag else MISSING,
                                            source="sentinel_inventory")
        ice = result["ice_bomb_after"]
        if ice != 255:
            items[7770013] = ItemObservation(7770013, OWNED if ice else MISSING,
                                             source="sentinel_inventory")
        dash = result.get("dash_after", 255)
        if dash in (0, 1):
            items[7770015] = ItemObservation(7770015, OWNED if dash else MISSING,
                                             source="sentinel_inventory")
        key = result.get("slayer_key_after", 255)
        if key in (0, 1) and runtime_context is not None and runtime_context.campaign == campaign:
            for item_id, map_key in GATE_KEY_TO_MAP.items():
                if map_key in runtime_context.map_keys:
                    items[item_id] = ItemObservation(item_id, OWNED if key else MISSING,
                                                     source="sentinel_inventory")
        capacity_tiers = tuple(result.get(f"{family}_tier_after", 255) for family in ("health", "armor", "ammo"))
        for item_id, tier in zip((7770017, 7770088, 7770092), capacity_tiers):
            if type(tier) is int and 0 <= tier <= 4:
                items[item_id] = ItemObservation(item_id, OWNED if tier else MISSING,
                                                 observed_stage=tier, source="sentinel_inventory")
        for item_id, field in ((7770012, "flame_belch_after"), (7770014, "blood_punch_after")):
            value = result.get(field, 255)
            if value in (0, 1):
                items[item_id] = ItemObservation(item_id, OWNED if value else MISSING, source="sentinel_inventory")
        known, owned = result.get("perks_known", 0), result.get("perks_owned", 0)
        for item_id in PERMANENT_PERK_ITEM_IDS | {7770145, 7770146, 7770147}:
            index = item_id - 7770145 + 61 if item_id >= 7770145 else item_id - 7770058
            if known & (1 << index):
                items[item_id] = ItemObservation(item_id, OWNED if owned & (1 << index) else MISSING,
                                                source="sentinel_perk_availability")
        if hasattr(link, "observe_special_inventory"):
            try:
                special = link.observe_special_inventory()
            except (RuntimeError, OSError, ValueError) as error:
                self.logger.info("SPECIAL_INVENTORY_OBSERVATION_DEFERRED detail=%s", error)
            else:
                if native_scope != special.get("_observation_scope"):
                    return None
                special_known = special.get("native_state_known", 0)
                for item_id, field, bit in ((7770007, "native_crucible", 1), (7770009, "native_hammer", 2)):
                    if special_known & bit:
                        value = special[field]
                        items[item_id] = ItemObservation(item_id, OWNED if value else MISSING,
                                                        observed_stage=value, source="sentinel_special")
                if special_known & 7 == 7:
                    hammer_stage = 2 if special["native_hammer_perks"] == 3 else 1
                    stage = hammer_stage + 1 if special["native_hammer"] else int(bool(special["native_crucible"]))
                    items[7770901] = ItemObservation(7770901, OWNED if stage else MISSING,
                                                    observed_stage=stage, source="sentinel_special")
                if special_known & 6 == 6:
                    stage = (2 if special["native_hammer_perks"] == 3 else 1) if special["native_hammer"] else 0
                    items[7770902] = ItemObservation(7770902, OWNED if stage else MISSING,
                                                    observed_stage=stage, source="sentinel_special")
        signature = (self.context.state_key, link.pid, epoch, context_identity,
                     tuple(sorted((key, value.state, value.observed_stage) for key, value in items.items())))
        if signature != self.last_logged:
            self.log_event(
                "ITEM_NATIVE_INVENTORY", state_key=self.context.state_key,
                namespace=link.namespace, process_id=link.pid, epoch=epoch,
                context=context_identity, rocket=items.get(7770002, ItemObservation(7770002)).state,
                bfg=items.get(7770006, ItemObservation(7770006)).state,
                ballista=items.get(7770004, ItemObservation(7770004)).state,
                frag=items.get(7770011, ItemObservation(7770011)).state,
                ice=items.get(7770013, ItemObservation(7770013)).state,
                dash=items.get(7770015, ItemObservation(7770015)).state,
                weapons_mask=weapons, ice_bomb=ice, dash_owned=dash,
                slayer_key_owned=key,
                capacity_tiers=capacity_tiers,
                perks_known=str(known), perks_owned=str(owned),
                gate_keys={str(item_id): item.state for item_id, item in items.items()
                           if item_id in GATE_KEY_TO_MAP},
            )
            self.last_logged = signature
        return InventoryObservation(
            room_seed_name, epoch, context_identity, campaign, items,
            state_key=self.context.state_key, process_id=link.pid,
            provider_namespace=link.namespace, timestamp_ns=time.time_ns(),
        )
