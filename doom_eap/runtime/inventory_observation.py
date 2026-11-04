"""Collect a coherent native inventory snapshot; adapters own repair and persistence."""

import time
from dataclasses import dataclass
from uuid import UUID

from doom_eap.contracts.inventory_domain import (
    MISSING,
    OWNED,
    PERMANENT_PERK_ITEM_IDS,
    Currency,
    CurrencyGrantResult,
    CurrencyObservation,
    CurrencyOutcome,
    CurrencyValue,
    EffectProof,
    EvidenceScope,
    InventoryObservation,
    InventoryReadback,
    ItemObservation,
    MissionAccessObservation,
    PerkStages,
    ProgressionObservation,
    SaveReadiness,
)
from doom_eap.contracts.materialization import GATE_KEY_TO_MAP
from doom_eap.contracts.runtime_context import canonical_map_name
from doom_eap.runtime.context_registry import CONTEXT_BY_IDENTITY
from doom_eap.runtime.native_transport import WireError

NORMAL_RUNES = (7770085, 7770086, 7770087, 7770089, 7770090, 7770091, 7770093, 7770094, 7770095)


@dataclass(frozen=True)
class DeliverySnapshot:
    readback: InventoryReadback
    weapon_points_gained: int | None


async def observe_delivery(transport, scope, context, room_seed_name, epoch):
    """Read one generation through v2; discard every domain if its save or load changes."""
    namespace, binding = scope.namespace.encode("ascii"), UUID(scope.save_binding).bytes
    before = await transport.hello()
    inventory = await transport.observe(25, namespace, binding)
    special = await observe_optional(transport, 37, namespace, binding)
    runes = await observe_optional(transport, 33, namespace, binding)
    points = await observe_optional(transport, 18, namespace, binding)
    progression = await observe_optional(transport, 54, namespace, binding)
    after = await transport.hello()
    admission = await transport.observe(12, b"0" * 64, bytes(16))
    if (before["scope"] != after["scope"] or before["scope"] != transport.scope
            or admission is None or bytes(admission["save_binding"]) != binding or inventory is None):
        return None
    progress = None if progression is None else progression_observation(progression)
    if progress is not None and (progress.before_scope != scope or progress.after_scope != scope):
        return None
    items = _delivery_items(inventory, special, runes, progress, context)
    observation = InventoryObservation(room_seed_name, epoch, context.identity, context.campaign, items,
                                       process_id=scope.pid, provider_namespace=scope.namespace,
                                       stage_masks=SentinelInventoryObservation._stage_masks(inventory), progression=progress)
    gained = _gained_points(points)
    return DeliverySnapshot(InventoryReadback(scope, scope, observation), gained)


async def observe_optional(transport, command, namespace, binding, **payload):
    try:
        return await transport.observe(command, namespace, binding, **payload)
    except WireError as error:
        if error.result != 2:  # capability_unavailable
            raise
        return None


def _gained_points(points):
    if points is None or points["native_exception"] or points["outcome"] not in (1, 2) or not points["flags"] & 16:
        return None
    return points["gained_after"]


def _delivery_items(inventory, special, runes, progress, context):
    items = _weapons(inventory) | _equipment(inventory) | _capacities(inventory) | _perks(inventory)
    items.update(_gate_keys(inventory, context, context.campaign))
    if progress is not None:
        items.update(_progression_items(progress, context, context.campaign))
    if special is not None:
        items.update(_special_items(special))
    if runes is not None:
        items.update(_rune_items(runes))
    return items


def _rune_items(result):
    from doom_eap.contracts.materialization import SUPPORT_RUNE_IDS

    items = {}
    for ids, field in ((NORMAL_RUNES, "owned_normal_after"), (sorted(SUPPORT_RUNE_IDS), "owned_support_after")):
        mask = result[field]
        if mask != 0xffffffff:
            items.update({item_id: _item(item_id, mask & (1 << bit)) for bit, item_id in enumerate(ids)})
    return items


def _wire_text(value):
    return bytes(value).split(b"\0", 1)[0].decode("utf-8", errors="strict")


def evidence_scope(value):
    process = value["process"]
    return EvidenceScope(process["pid"], process["process_created"], str(UUID(bytes=bytes(process["instance_id"]))),
                         _wire_text(value["namespace_id"]), str(UUID(bytes=bytes(value["save_binding"]))),
                         process["lifecycle_generation"], _wire_text(value["map_name"]),
                         SaveReadiness.READY if value["readiness"] == 1 else SaveReadiness.UNKNOWN)


def _currency_value(result, suffix, valid):
    if not result["flags"] & valid:
        return CurrencyValue()
    return CurrencyValue(result[f"balance_{suffix}"], result[f"gained_{suffix}"])


def currency_observation(result):
    return CurrencyObservation(evidence_scope(result["before_scope"]), evidence_scope(result["after_scope"]),
                               {Currency(result["currency"]): _currency_value(result, "after", 2)})


def currency_grant_result(result, reference):
    return CurrencyGrantResult(reference, evidence_scope(result["before_scope"]), evidence_scope(result["after_scope"]),
                               Currency(result["currency"]), _currency_value(result, "before", 1),
                               _currency_value(result, "after", 2), bool(result["flags"] & 4),
                               result["native_exception"], CurrencyOutcome(result["outcome"]))


def progression_observation(result):
    quantity = result["slayer_key_quantity"]
    return ProgressionObservation(evidence_scope(result["before_scope"]), evidence_scope(result["after_scope"]),
                                  PerkStages(result["suit_known"], result["suit_owned"]),
                                  quantity if quantity >= 0 else None,
                                  (EffectProof.UNPROVEN, EffectProof.PRESENT, EffectProof.ABSENT)[result["meat_hook"]])


def mission_access_observation(result):
    return MissionAccessObservation(evidence_scope(result["menu_scope"]), result["revision"], result["stage_id"],
                                    _wire_text(result["map_name"]), bool(result["entry_usable"]),
                                    evidence_scope(result["destination_scope"]), result["destination_revision"],
                                    result["destination_stage_id"], bool(result["destination_ready"]))


async def _native_currencies(transport, namespace, save_binding):
    currencies = []
    for currency in Currency:
        result = await transport.observe(53, namespace, save_binding, currency=currency)
        if result is not None:
            currencies.append(currency_observation(result))
    return currencies


def _combine_currencies(currencies):
    return CurrencyObservation(currencies[0].before_scope, currencies[0].after_scope,
                               {key: value for fact in currencies for key, value in fact.values.items()})


def _item(item_id, value, *, stage=0, source="sentinel_inventory"):
    return ItemObservation(item_id, OWNED if value else MISSING, observed_stage=stage, source=source)


def _weapons(result):
    mask = result["weapons_after"]
    if mask == 0xffffffff:
        return {}
    ids = (7770900, 7770003, 7770000, 7770005, 7770001,
           7770004, 7770002, 7770006, 7770010, 7770008)
    return {item_id: _item(item_id, mask & (1 << bit)) for bit, item_id in enumerate(ids)}


def _equipment(result):
    fields = ((7770011, "frag_grenade_after"), (7770013, "ice_bomb_after"),
              (7770015, "dash_after"), (7770012, "flame_belch_after"), (7770014, "blood_punch_after"))
    return {item_id: _item(item_id, result[field]) for item_id, field in fields if result.get(field, 255) in (0, 1)}


def _capacities(result):
    fields = ((7770017, "health"), (7770088, "armor"), (7770092, "ammo"))
    items = {}
    for item_id, family in fields:
        tier = result.get(f"{family}_tier_after", 255)
        if type(tier) is int and 0 <= tier <= 4:
            items[item_id] = _item(item_id, tier, stage=tier)
    return items


def _perks(result):
    known, owned = result.get("perks_known", 0), result.get("perks_owned", 0)
    items = {}
    for item_id in PERMANENT_PERK_ITEM_IDS | {7770145, 7770146, 7770147}:
        index = item_id - 7770145 + 61 if item_id >= 7770145 else item_id - 7770058
        if known & (1 << index):
            items[item_id] = _item(item_id, owned & (1 << index), source="sentinel_perk_availability")
    return items


def _gate_keys(result, context, campaign):
    key = result.get("slayer_key_after", 255)
    if key not in (0, 1) or context is None or context.campaign != campaign:
        return {}
    return {item_id: _item(item_id, key) for item_id, map_key in GATE_KEY_TO_MAP.items() if map_key in context.map_keys}


def _special_items(special):
    known = special.get("native_state_known", 0)
    items = {}
    for item_id, field, bit in ((7770007, "native_crucible", 1), (7770009, "native_hammer", 2)):
        if known & bit:
            items[item_id] = _item(item_id, special[field], stage=special[field], source="sentinel_special")
    if known & 7 == 7:
        hammer_stage = 2 if special["native_hammer_perks"] == 3 else 1
        stage = hammer_stage + 1 if special["native_hammer"] else int(bool(special["native_crucible"]))
        items[7770901] = _item(7770901, stage, stage=stage, source="sentinel_special")
    if known & 6 == 6:
        stage = _hammer_stage(special)
        items[7770902] = _item(7770902, stage, stage=stage, source="sentinel_special")
    return items


def _hammer_stage(special):
    if not special["native_hammer"]:
        return 0
    return 2 if special["native_hammer_perks"] == 3 else 1


class SentinelInventoryObservation:
    def __init__(self, context, logger, log_event):
        self.context = context
        self.logger = logger
        self.log_event = log_event
        self.last_logged = None

    async def observe_native(self, transport, namespace, save_binding, **snapshot):
        state_key = self.context.state_key
        progression = await transport.observe(54, namespace, save_binding)
        currencies = await _native_currencies(transport, namespace, save_binding)
        if self.context.state_key != state_key:
            return None
        if currencies and not all(fact.before_scope == currencies[0].before_scope
                                  and fact.after_scope == currencies[0].after_scope for fact in currencies):
            return None
        combined = _combine_currencies(currencies) if currencies else None
        return self.observe_inventory(**snapshot,
                                      progression=None if progression is None else progression_observation(progression),
                                      currencies=combined)

    def _native_inventory(self, link):
        link = link or self.context.native_game_link()
        return link, link.observe_inventory()

    def _read_special(self, link):
        if not hasattr(link, "observe_special_inventory"):
            return None
        try:
            return link.observe_special_inventory()
        except (RuntimeError, OSError, ValueError) as error:
            self.logger.info("SPECIAL_INVENTORY_OBSERVATION_DEFERRED detail=%s", error)
            return None

    def observe_inventory(self, *, room_seed_name, epoch, context_identity, campaign, link=None,
                          progression: ProgressionObservation | None = None,
                          currencies: CurrencyObservation | None = None):
        state_key = self.context.state_key
        try:
            link, result = self._native_inventory(link)
        except (RuntimeError, OSError, ValueError) as error:
            self.logger.info("INVENTORY_OBSERVATION_DEFERRED detail=%s", error)
            return None
        context = CONTEXT_BY_IDENTITY.get(context_identity)
        if result.get("_save_recovering") or not self._matches_context(result, context, campaign):
            return None
        if not _facts_match((progression, currencies), result, link):
            return None
        items = _weapons(result) | _equipment(result) | _capacities(result) | _perks(result)
        items.update(_gate_keys(result, context, campaign))
        if progression is not None:
            items.update(_progression_items(progression, context, campaign))
        special = self._read_special(link)
        if special is not None:
            if not self._same_snapshot(result, special):
                return None
            items.update(_special_items(special))
        if self.context.state_key != state_key:
            return None
        observation = InventoryObservation(
            room_seed_name, epoch, context_identity, campaign, items,
            state_key=state_key, process_id=link.pid, provider_namespace=link.namespace,
            timestamp_ns=time.time_ns(), native_scope=result.get("_observation_scope"),
            stage_masks=self._stage_masks(result),
            progression=progression, currencies=currencies,
        )
        self._log(observation, result)
        return observation

    @staticmethod
    def _stage_masks(result):
        upgrades = result.get("upgrades_after", 0xffffffff)
        return {} if upgrades == 0xffffffff else {7770904: (upgrades >> 11) & 15}

    @staticmethod
    def _matches_context(result, context, campaign):
        if not result.get("_observation_scope"):
            return True
        if context is None or context.campaign != campaign:
            return False
        return canonical_map_name(result.get("_observation_map")) in context.runtime_maps

    @staticmethod
    def _same_snapshot(inventory, other):
        if inventory.get("_save_recovering") or other.get("_save_recovering"):
            return False
        if inventory.get("_observation_save_binding") != other.get("_observation_save_binding"):
            return False
        if inventory.get("_observation_scope") != other.get("_observation_scope"):
            return False
        if inventory.get("_observation_scope") is None:
            return True
        return canonical_map_name(inventory.get("_observation_map")) == canonical_map_name(other.get("_observation_map"))

    def _log(self, observation, result):
        signature = (observation.state_key, observation.process_id, observation.epoch,
                     observation.context_identity, observation.native_scope,
                     tuple(observation.stage_masks.items()),
                     tuple(sorted((key, value.state, value.observed_stage) for key, value in observation.items.items())))
        if signature == self.last_logged:
            return
        self.log_event(
            "ITEM_NATIVE_INVENTORY", state_key=observation.state_key,
            namespace=observation.provider_namespace, process_id=observation.process_id, epoch=observation.epoch,
            context=observation.context_identity, rocket=observation.get_state(7770002),
            bfg=observation.get_state(7770006), ballista=observation.get_state(7770004),
            frag=observation.get_state(7770011), ice=observation.get_state(7770013), dash=observation.get_state(7770015),
            weapons_mask=result["weapons_after"], ice_bomb=result["ice_bomb_after"],
            dash_owned=result.get("dash_after", 255), slayer_key_owned=result.get("slayer_key_after", 255),
            capacity_tiers=tuple(result.get(f"{family}_tier_after", 255) for family in ("health", "armor", "ammo")),
            perks_known=str(result.get("perks_known", 0)), perks_owned=str(result.get("perks_owned", 0)),
            gate_keys={str(item_id): item.state for item_id, item in observation.items.items() if item_id in GATE_KEY_TO_MAP},
        )
        self.last_logged = signature


def _facts_match(facts, result, link):
    if not all(_fact_matches(fact, result, link) for fact in facts if fact is not None):
        return False
    scopes = [fact.after_scope for fact in facts if fact is not None]
    return not scopes or all(scope == scopes[0] for scope in scopes)


def _progression_items(progression, context, campaign):
    items = {}
    quantity = progression.slayer_key_quantity
    if quantity is not None and context is not None and context.campaign == campaign:
        items.update({item_id: _item(item_id, quantity > 0, source="slayer_key_quantity")
                      for item_id, map_key in GATE_KEY_TO_MAP.items() if map_key in context.map_keys})
    if progression.meat_hook is not EffectProof.UNPROVEN:
        items[7770083] = _item(7770083, progression.meat_hook is EffectProof.PRESENT, source="sentinel_progression")
    return items


def _fact_matches(fact, result, link):
    scope = fact.after_scope
    if fact.before_scope != scope or scope.readiness is not SaveReadiness.READY:
        return False
    if (scope.pid, scope.namespace, scope.save_binding) != (
            link.pid, link.namespace, result.get("_observation_save_binding")):
        return False
    if canonical_map_name(scope.map_name) != canonical_map_name(result.get("_observation_map")):
        return False
    identity = result.get("_observation_scope")
    return bool(identity and (str(scope.process_created), scope.core_instance, str(scope.generation)) == (
        identity[0], identity[1], identity[3]))
