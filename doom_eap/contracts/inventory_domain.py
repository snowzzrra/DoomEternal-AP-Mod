"""Persistent inventory observation contracts, domains, and lifecycle states."""

from dataclasses import dataclass

from dataclasses import dataclass

import json
from pathlib import Path
from typing import Any
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Protocol

# observation states
OWNED = "owned"
MISSING = "missing"
UNKNOWN = "unknown"

OBSERVATION_STATES = frozenset({OWNED, MISSING, UNKNOWN})


class Currency(IntEnum):
    WEAPON_POINTS = 0
    PRAETOR = 1
    MASTERY = 2
    SENTINEL_BATTERY = 4


class CurrencyOutcome(IntEnum):
    NOT_EXECUTED = 0
    OBSERVED = 1
    GRANTED = 2
    PRECONDITION = 3
    READ_FAILED = 4
    UNKNOWN = 5


class SuitPerkStage(IntEnum):
    WEAPON_CHANGE_SPEED = 0
    DASH_REGEN_DELAY = 1
    LEDGE_GRAB_SPEED = 2
    INCREASE_PICKUP_RADIUS = 3
    REDUCE_HAZARD_DAMAGE = 4
    REDUCE_SELF_WEAPON_DAMAGE = 5


class SaveReadiness(str, Enum):
    UNKNOWN = "unknown"
    READY = "ready"
    RECOVERING = "recovering"


class EffectProof(str, Enum):
    UNPROVEN = "unproven"
    PRESENT = "present"
    ABSENT = "absent"


@dataclass(frozen=True)
class EvidenceScope:
    pid: int
    process_created: int
    core_instance: str
    namespace: str
    save_binding: str
    generation: int
    map_name: str
    readiness: SaveReadiness = SaveReadiness.UNKNOWN


@dataclass(frozen=True)
class CurrencyValue:
    balance: int | None = None
    gained: int | None = None


@dataclass(frozen=True)
class CurrencyObservation:
    before_scope: EvidenceScope
    after_scope: EvidenceScope
    values: Mapping[Currency, CurrencyValue] = field(default_factory=dict)


@dataclass(frozen=True)
class CurrencyGrant:
    currency: Currency
    amount: int
    expected_gained: int


@dataclass(frozen=True)
class CurrencyGrantResult:
    reference: bytes
    before_scope: EvidenceScope
    after_scope: EvidenceScope
    currency: Currency
    before: CurrencyValue
    after: CurrencyValue
    writer_entered: bool = False
    native_exception: int = 0
    outcome: CurrencyOutcome = CurrencyOutcome.GRANTED


@dataclass(frozen=True)
class PerkStages:
    known: int = 0
    owned: int = 0


@dataclass(frozen=True)
class MasterySlot:
    host: str
    mod: str
    component_before: int
    component_after: int
    selected: bool = False
    proof: EffectProof = EffectProof.UNPROVEN


@dataclass(frozen=True)
class ProgressionObservation:
    before_scope: EvidenceScope
    after_scope: EvidenceScope
    suit: PerkStages = field(default_factory=PerkStages)
    slayer_key_quantity: int | None = None
    meat_hook: EffectProof = EffectProof.UNPROVEN
    masteries: Mapping[int, MasterySlot] = field(default_factory=dict)


@dataclass(frozen=True)
class SuitStage:
    stage: SuitPerkStage


@dataclass(frozen=True)
class SlayerKey:
    map_name: str


@dataclass(frozen=True)
class MasteryEffect:
    item_id: int
    host: str
    mod: str


@dataclass(frozen=True)
class MeatHookPerk:
    pass


@dataclass(frozen=True)
class MissionAccess:
    revision: int
    stage_id: int
    map_name: str


@dataclass(frozen=True)
class MissionAccessObservation:
    menu_scope: EvidenceScope
    revision: int
    stage_id: int
    map_name: str
    entry_usable: bool = False
    destination_scope: EvidenceScope | None = None
    destination_revision: int | None = None
    destination_stage_id: int | None = None
    destination_ready: bool = False


@dataclass(frozen=True)
class InventoryEffect:
    item_id: int
    stage: int


@dataclass(frozen=True)
class InventoryReadback:
    before_scope: EvidenceScope
    after_scope: EvidenceScope
    observation: "InventoryObservation"


EffectPostcondition = CurrencyGrant | SuitStage | SlayerKey | MasteryEffect | MeatHookPerk | MissionAccess | InventoryEffect
NativeEffectEvidence = CurrencyGrantResult | ProgressionObservation | MissionAccessObservation | InventoryReadback


@dataclass(frozen=True)
class EffectExpectation:
    reference: bytes
    scope: EvidenceScope
    postcondition: EffectPostcondition

# lifecycle / execution states
PLANNED = "planned"
ADMITTED = "admitted"
QUEUED = "queued"
DISPATCHED = "dispatched"
OBSERVED = "observed"
DEFERRED = "deferred"
BLOCKED = "blocked"
INDETERMINATE = "indeterminate"

LIFECYCLE_STATES = frozenset({
    PLANNED,
    ADMITTED,
    QUEUED,
    DISPATCHED,
    OBSERVED,
    DEFERRED,
    BLOCKED,
    INDETERMINATE,
})

# inventory domains
DOMAIN_WEAPONS = "weapons"
DOMAIN_EQUIPMENT = "equipment"
DOMAIN_CAPACITIES = "capacities"
DOMAIN_PERSISTENT_UPGRADES = "persistent_upgrades"
DOMAIN_SPECIAL_WEAPONS = "special_weapons"

# Capacity Bounds: strictly max 4 perk tiers per capacity domain item.
MAX_CAPACITY_TIER = 4
CAPACITY_ITEM_IDS = frozenset({
    7770017,  # sentinel crystal (health)
    7770088,  # sentinel crystal (armor)
    7770092,  # sentinel crystal (ammo)
})

# standard weapon item ids
WEAPON_ITEM_IDS = frozenset({
    7770900,  # combat shotgun
    7770000,  # heavy cannon
    7770001,  # plasma rifle
    7770002,  # rocket launcher
    7770003,  # super shotgun
    7770004,  # ballista
    7770005,  # chaingun
    7770006,  # bfg 9000
    7770008,  # unmaykr
    7770010,  # chainsaw
})

EQUIPMENT_ITEM_IDS = frozenset({
    7770011,  # frag grenade
    7770012,  # flame belch
    7770013,  # ice bomb
    7770014,  # blood punch
    7770015,  # dash
})

# special weapon item ids
SPECIAL_WEAPON_ITEM_IDS = frozenset({
    7770007,  # the crucible
    7770009,  # sentinel hammer
    7770901,  # progressive special weapon
    7770902,  # progressive sentinel hammer
})

PERMANENT_PERK_ITEM_IDS = frozenset({
    7770058, 7770059, 7770060, 7770061, 7770062, 7770064, 7770071,
    7770073, 7770075, 7770077, 7770079, 7770081, 7770083,
    7770085, 7770086, 7770087, 7770089, 7770090, 7770091, 7770093, 7770094, 7770095,
    *range(7770097, 7770105), *range(7770106, 7770119),
})

# persistent upgrades, registered runes and map-local keys
PERSISTENT_UPGRADE_ITEM_IDS = frozenset({
    7770145,  # support rune: break blast
    7770146,  # support rune: desperate punch
    7770147,  # support rune: take back
    7770148,  # slayer key: uac atlantica
    7770149,  # slayer key: the holt
    7770150,  # slayer key: exultia
    7770151,  # slayer key: cultist base
    7770152,  # slayer key: super gore nest
    7770153,  # slayer key: arc complex
    7770154,  # slayer key: phobos / mars core
    7770155,  # slayer key: taras nabad
}) | PERMANENT_PERK_ITEM_IDS

ALL_PERSISTENT_DOMAIN_IDS = frozenset(
    WEAPON_ITEM_IDS
    | EQUIPMENT_ITEM_IDS
    | CAPACITY_ITEM_IDS
    | SPECIAL_WEAPON_ITEM_IDS
    | PERSISTENT_UPGRADE_ITEM_IDS
)


def _selected_persistent_items_domains(domain):
    domains = {classify_inventory_domain(value) for value in ALL_PERSISTENT_DOMAIN_IDS}
    if domain != "all" and domain not in domains:
        raise ValueError("Unknown persistent inventory domain")


def selected_persistent_items(domain="all", item_id=None):
    """Limit repair intent while leaving entitlement and native ownership authoritative."""
    _selected_persistent_items_domains(domain)
    if item_id is not None:
        if type(item_id) is not int or item_id not in ALL_PERSISTENT_DOMAIN_IDS:
            raise ValueError("Consumables and currency cannot be replayed by inventory repair")
        if domain != "all" and classify_inventory_domain(item_id) != domain:
            raise ValueError("Item does not belong to the selected domain")
        return frozenset((item_id,))
    if domain == "all":
        return None
    return frozenset(value for value in ALL_PERSISTENT_DOMAIN_IDS if classify_inventory_domain(value) == domain)


def classify_inventory_domain(item_id: int) -> str | None:
    """Classify an item ID into its persistent inventory domain."""
    if item_id in WEAPON_ITEM_IDS:
        return DOMAIN_WEAPONS
    if item_id in EQUIPMENT_ITEM_IDS:
        return DOMAIN_EQUIPMENT
    if item_id in CAPACITY_ITEM_IDS:
        return DOMAIN_CAPACITIES
    if item_id in SPECIAL_WEAPON_ITEM_IDS:
        return DOMAIN_SPECIAL_WEAPONS
    if item_id in PERSISTENT_UPGRADE_ITEM_IDS:
        return DOMAIN_PERSISTENT_UPGRADES
    return None


@dataclass(frozen=True)
class ItemObservation:
    """Observed state of an individual inventory item."""
    item_id: int
    state: str = UNKNOWN
    observed_stage: int = 0
    source: str = "unobserved"

    def __post_init__(self):
        if self.state not in OBSERVATION_STATES:
            raise ValueError(f"invalid observation state: {self.state!r}")
        if self.observed_stage < 0:
            raise ValueError(f"observed_stage cannot be negative: {self.observed_stage}")


@dataclass(frozen=True)
class InventoryObservation:
    """Scope-bound persistent inventory observation snapshot carrying freshness identity."""
    room_seed_name: str
    epoch: int | str
    context_identity: str
    campaign: str
    items: Mapping[int, ItemObservation] = field(default_factory=dict)
    state_key: str | None = None
    process_id: int | None = None
    provider_namespace: str | None = None
    timestamp_ns: int = 0
    native_scope: tuple[str, ...] | None = None
    stage_masks: Mapping[int, int] = field(default_factory=dict)
    progression: ProgressionObservation | None = None
    currencies: CurrencyObservation | None = None

    def progression_ready(self) -> bool:
        return bool(self.progression and self.progression.before_scope == self.progression.after_scope
                    and self.progression.after_scope.readiness is SaveReadiness.READY)

    def get_state(self, item_id: int) -> str:
        """Return observation state for item. Defaults to UNKNOWN."""
        if item_id == 7770021:
            return self._suit_state()
        obs = self.items.get(item_id)
        if obs is None:
            return UNKNOWN
        return obs.state

    def _suit_state(self) -> str:
        progression = self.progression
        if progression is None or not self.progression_ready() or progression.suit.known != 63:
            return UNKNOWN
        return OWNED if progression.suit.owned == 63 else MISSING

    def get_observed_stage(self, item_id: int) -> int:
        """Return observed stage/tier for item. Defaults to 0."""
        obs = self.items.get(item_id)
        if obs is None:
            return 0
        return obs.observed_stage

    def is_owned(self, item_id: int) -> bool:
        """Return True if item is explicitly observed as OWNED."""
        return self.get_state(item_id) == OWNED

    def is_proven_missing(self, item_id: int) -> bool:
        """Return True ONLY if item is explicitly observed as MISSING.
        
        UNKNOWN is never interpreted as missing.
        """
        return self.get_state(item_id) == MISSING

    def confirms_repair(self, item_id: int, stage: int) -> bool:
        if item_id == 7770904:
            return bool(self.stage_masks.get(7770904, 0) & (1 << stage))
        if item_id == 7770014 and stage in {7770162, 7770290, 7770411}:
            bit = {7770162: 2, 7770290: 4, 7770411: 8}[stage]
            return bool(self.stage_masks.get(7770904, 0) & bit)
        if item_id == 7770021:
            progression = self.progression
            return bool(progression and self.progression_ready()
                        and progression.suit.known & progression.suit.owned & (1 << stage))
        if not self.is_owned(item_id):
            return False
        if item_id in CAPACITY_ITEM_IDS:
            return self.get_observed_stage(item_id) > stage
        if item_id in {7770901, 7770902}:
            return self.get_observed_stage(item_id) >= stage
        return True

    def _is_stale_for_epoch(self, campaign, context_identity, epoch, provider_namespace):
        if epoch is not None and str(self.epoch) != str(epoch):
            return True
        if context_identity is not None and self.context_identity != context_identity:
            return True
        if campaign is not None and self.campaign != campaign:
            return True
        if (
            provider_namespace is not None
            and self.provider_namespace != provider_namespace
        ):
            return True
        return False


    def is_stale_for(
        self,
        *,
        room_seed_name: str | None = None,
        epoch: int | str | None = None,
        context_identity: str | None = None,
        campaign: str | None = None,
        provider_namespace: str | None = None,
    ) -> bool:
        """Reject stale observation if scope, epoch, or namespace does not match."""
        if room_seed_name is not None and self.room_seed_name != room_seed_name:
            return True
        return self._is_stale_for_epoch(campaign, context_identity, epoch, provider_namespace)


class InventoryObservationPort(Protocol):
    """Observation producer interface."""

    def observe_inventory(
        self,
        *,
        room_seed_name: str,
        epoch: int | str,
        context_identity: str,
        campaign: str,
    ) -> InventoryObservation | None:
        ...


REPO_ROOT = Path(__file__).resolve().parents[2]
ITEM_RUNTIME_CONTRACTS_FILE = REPO_ROOT / "data" / "item_runtime_contracts.json"
DEFAULT_DEATH_LINK_MODE = "soft"


def load_item_runtime_contracts(
    path: Path = ITEM_RUNTIME_CONTRACTS_FILE,
) -> dict[int, dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1 or document.get("contract_revision") != 3:
        raise ValueError("item runtime contract schema/revision mismatch")
    raw_items = document.get("items")
    if not isinstance(raw_items, dict):
        raise ValueError("item runtime contracts items must be an object")
    contracts: dict[int, dict[str, Any]] = {}
    for raw_id, contract in raw_items.items():
        item_id = int(raw_id)
        if str(item_id) != raw_id or not isinstance(contract, dict):
            raise ValueError(f"invalid item runtime contract {raw_id!r}")
        contracts[item_id] = dict(contract)
    return contracts


def start_inventory_eligible(item_id: int) -> bool:
    contract = load_item_runtime_contracts().get(item_id)
    return contract is None or bool(contract["start_inventory_eligible"])


TAG_PAID_NORMAL_MOD_UPGRADES = frozenset({
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

# Separate from the 28 paid WUP nodes. The supported patch2 declaration marks
# this authored weak-point modifier componentSave=false; retain it independently
# of purchases until a separate engine requirement change is justified.
TAG_REQUIRED_ENGINE_PERKS = frozenset({
    "perk/player/weapons/shotgun/pop_rocket_weakpoint_hit",
})

TAG_REQUIRED_BLOOD_PUNCH_PERKS = frozenset({
    "perk/player/blood_punch/area_of_effect",
    "perk/player/blood_punch/ai_charge_rate",
    "perk/player/blood_punch/max_charges",
})


@dataclass(frozen=True)
class AuthoredTagPrerequisites:
    engine_perks: frozenset[str]
    blood_punch_perks: frozenset[str]
    provenance: str = "authored_tag_devinv"


AUTHORED_TAG_PREREQUISITES = AuthoredTagPrerequisites(
    TAG_REQUIRED_ENGINE_PERKS, TAG_REQUIRED_BLOOD_PUNCH_PERKS,
)


@dataclass(frozen=True)
class ReceiptSessionToken:
    state_key: str
    generation: int


@dataclass(frozen=True)
class StartingMaterializationFact:
    item_id: int
    quantity: int
    provenance: str


@dataclass(frozen=True)
class DerivedOwnershipFact:
    item_id: int
    provenance: str


@dataclass(frozen=True)
class CompletionUpgrade:
    location_id: int
    perk_path: str
    mission_name: str


@dataclass(frozen=True)
class EffectiveOwnership:
    ap_item_ids: tuple[int, ...]
    reconciliation_item_ids: tuple[int, ...]
    derived_facts: tuple[DerivedOwnershipFact, ...]
    vanilla_dash: bool
    blood_punch_upgrades: tuple[CompletionUpgrade, ...]
    materialization_fingerprint: str
    authored_tag_prerequisites: AuthoredTagPrerequisites
