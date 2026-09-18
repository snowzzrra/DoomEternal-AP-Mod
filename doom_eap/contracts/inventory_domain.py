"""Persistent inventory observation contracts, domains, and lifecycle states."""
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

# Observation States
OWNED = "owned"
MISSING = "missing"
UNKNOWN = "unknown"

OBSERVATION_STATES = frozenset({OWNED, MISSING, UNKNOWN})

# Lifecycle / Execution States
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

# Inventory Domains
DOMAIN_WEAPONS = "weapons"
DOMAIN_EQUIPMENT = "equipment"
DOMAIN_CAPACITIES = "capacities"
DOMAIN_PERSISTENT_UPGRADES = "persistent_upgrades"
DOMAIN_SPECIAL_WEAPONS = "special_weapons"

# Capacity Bounds: strictly max 4 perk tiers per capacity domain item.
MAX_CAPACITY_TIER = 4
CAPACITY_ITEM_IDS = frozenset({
    7770017,  # Sentinel Crystal (Health)
    7770088,  # Sentinel Crystal (Armor)
    7770092,  # Sentinel Crystal (Ammo)
})

# Standard weapon item IDs
WEAPON_ITEM_IDS = frozenset({
    7770000,  # Combat Shotgun
    7770001,  # Super Shotgun
    7770002,  # Heavy Cannon
    7770003,  # Chaingun
    7770004,  # Plasma Rifle
    7770005,  # Ballista
    7770006,  # Rocket Launcher
    7770008,  # BFG 9000
    7770010,  # Chainsaw
    7770011,  # Unmaykr
})

# Equipment item IDs
EQUIPMENT_ITEM_IDS = frozenset({
    7770012,  # Equipment Launcher
    7770013,  # Flame Belch
    7770014,  # Blood Punch
    7770015,  # Dash
})

# Special weapon item IDs
SPECIAL_WEAPON_ITEM_IDS = frozenset({
    7770007,  # The Crucible
    7770009,  # Sentinel Hammer
    7770901,  # Progressive Special Weapon
    7770902,  # Progressive Sentinel Hammer
})

# Persistent upgrade item IDs (Support Runes and Slayer Gate Keys)
PERSISTENT_UPGRADE_ITEM_IDS = frozenset({
    7770145,  # Support Rune: Desperate Punch
    7770146,  # Support Rune: Take Back
    7770147,  # Support Rune: Break Blast
    7770051,  # Slayer Key: Exultia
    7770052,  # Slayer Key: Cultist Base
    7770053,  # Slayer Key: Super Gore Nest
    7770054,  # Slayer Key: ARC Complex
    7770055,  # Slayer Key: Phobos / Mars Core
    7770056,  # Slayer Key: Taras Nabad
})

ALL_PERSISTENT_DOMAIN_IDS = frozenset(
    WEAPON_ITEM_IDS
    | EQUIPMENT_ITEM_IDS
    | CAPACITY_ITEM_IDS
    | SPECIAL_WEAPON_ITEM_IDS
    | PERSISTENT_UPGRADE_ITEM_IDS
)


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
            and self.provider_namespace is not None
            and self.provider_namespace != provider_namespace
        ):
            return True
        return False


class InventoryObservationPort(Protocol):
    """Observation producer interface (real native producer implemented in 8A.2)."""

    def observe_inventory(
        self,
        *,
        room_seed_name: str,
        epoch: int | str,
        context_identity: str,
        campaign: str,
    ) -> InventoryObservation | None:
        ...
