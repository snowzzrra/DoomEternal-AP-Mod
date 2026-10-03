"""Persistent inventory observation contracts, domains, and lifecycle states."""
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

# observation states
OWNED = "owned"
MISSING = "missing"
UNKNOWN = "unknown"

OBSERVATION_STATES = frozenset({OWNED, MISSING, UNKNOWN})

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

# Persistent upgrade item IDs (Support Runes and Slayer Gate Keys)
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
})

ALL_PERSISTENT_DOMAIN_IDS = frozenset(
    WEAPON_ITEM_IDS
    | EQUIPMENT_ITEM_IDS
    | CAPACITY_ITEM_IDS
    | SPECIAL_WEAPON_ITEM_IDS
    | PERSISTENT_UPGRADE_ITEM_IDS
)


def selected_persistent_items(domain="all", item_id=None):
    """Limit repair intent while leaving entitlement and native ownership authoritative."""
    domains = {classify_inventory_domain(value) for value in ALL_PERSISTENT_DOMAIN_IDS}
    if domain != "all" and domain not in domains:
        raise ValueError("Unknown persistent inventory domain")
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

    def get_state(self, item_id: int) -> str:
        """Return observation state for item. Defaults to UNKNOWN."""
        obs = self.items.get(item_id)
        if obs is None:
            return UNKNOWN
        return obs.state

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
