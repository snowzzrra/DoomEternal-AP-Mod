"""Pure capability selection and immutable room/lease scope for persistent replay."""
from dataclasses import dataclass
from typing import Iterable

from doom_eap.contracts.runtime_context import RuntimeContext


@dataclass(frozen=True)
class MaterializationScope:
    room_seed_name: str
    team: int
    slot: int
    state_key: str
    materialization_lease: str | None
    evidence_epoch: int | None
    reason: str
    dedupe_identity: str
    manual: bool = False


SUPPORT_RUNE_CAPABILITY = "support_runes_v1"
TAG_SPECIAL_CAPABILITY = "tag_special_v1"
SUPPORT_RUNE_IDS = frozenset({7770145, 7770146, 7770147})
TAG_SPECIAL_IDS = frozenset({7770009, 7770902})
CRUCIBLE_ID = 7770007
GATE_KEY_TO_MAP = {
    7770148: "e4m1_rig",
    7770149: "e4m3_mcity",
    7770150: "e1m2_war",
    7770151: "e1m3_cult",
    7770152: "e2m1_nest",
    7770153: "e2m2_base",
    7770154: "e2m3_core",
    7770155: "e3m1_slayer",
}



def context_item_ids(context: RuntimeContext, received_item_ids: Iterable[int]) -> tuple[int, ...]:
    """Select only materializable owned IDs for active context."""
    selected = []
    for item_id in received_item_ids:
        if item_id in SUPPORT_RUNE_IDS and not context.supports(SUPPORT_RUNE_CAPABILITY):
            continue
        if item_id in TAG_SPECIAL_IDS and not context.supports(TAG_SPECIAL_CAPABILITY):
            continue
        if item_id == 7770901 and not context.supports("special_weapon_v1"):
            continue
        if item_id == CRUCIBLE_ID and not context.supports("crucible_v1"):
            continue
        if item_id in GATE_KEY_TO_MAP:
            target_map = GATE_KEY_TO_MAP[item_id]
            if target_map not in context.map_keys:
                continue
        selected.append(item_id)
    return tuple(selected)


def support_rune_commands(received_item_ids: Iterable[int], context: RuntimeContext) -> tuple[int, ...]:
    if not context.supports(SUPPORT_RUNE_CAPABILITY):
        return ()
    return tuple(
        item_id
        for item_id in sorted(set(received_item_ids))
        if item_id in SUPPORT_RUNE_IDS
    )

