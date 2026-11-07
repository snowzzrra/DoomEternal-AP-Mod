import json
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from doom_eap.contracts.inventory_domain import MISSING, OWNED, InventoryObservation, ItemObservation
from doom_eap.contracts.materialization import MaterializationScope
from doom_eap.contracts.ownership import CompletionUpgrade, StartingMaterializationFact
from doom_eap.runtime.inventory import (
    InventoryObligation,
    InventoryOwnership,
    NativeEffectKind,
    ReplayPolicy,
    RuneNativeState,
    SentinelInventoryObservation,
    compare_inventory,
    load_policy_registry,
    ownership_from_ledger,
    rune_item_perk_mapping,
)
from doom_eap.runtime.ledger import CommandState, NetworkReceipt, Resolution
from doom_eap.runtime.observers import CONTEXT_BY_IDENTITY


@pytest.fixture
def inventory():
    root = Path(__file__).resolve().parents[1]
    definitions = {int(k): v for k, v in json.loads((root / "data/items.json").read_text()).items()}
    policies = load_policy_registry(root / "data/item_replay_policies.json", definitions)
    context = CONTEXT_BY_IDENTITY["base/hub"]
    scope = MaterializationScope("seed", 0, 1, "room", "1:100", 4, "reload", "room")

    def compare(ids, items=(), *, ownership=None, stage_masks=None, **kwargs):
        observation = InventoryObservation("seed", 4, context.identity, context.campaign,
            {item_id: ItemObservation(item_id, state, stage) for item_id, state, stage in items},
            stage_masks=stage_masks or {})
        return compare_inventory(ownership or InventoryOwnership(tuple(ids)), definitions=definitions,
            policies=policies, context=context, scope=scope, observation=observation, **kwargs)

    return compare, definitions


def test_ingested_prefix_and_starting_currency_are_distinct_acquisitions(inventory):
    compare, _ = inventory
    receipts = (NetworkReceipt(7770016, 10, 1, 0), NetworkReceipt(7770016, 11, 1, 0))
    local = (StartingMaterializationFact(7770016, 1, "starting"),)
    ownership = ownership_from_ledger(iter(receipts), iter(local))
    plan = compare((), ownership=ownership)
    assert ownership.reconciliation_item_ids == (7770016,) * 3
    assert [effect.source_key for effect in plan.acquisitions] == ["ap:0", "ap:1", "local:starting:7770016:0"]
    obligations = (
        InventoryObligation(7770016, 0, CommandState.CONFIRMED, source_key="ap:0"),
        InventoryObligation(7770016, 0, CommandState.EFFECT_UNKNOWN, Resolution.ACCEPTED_BY_PLAYER, "ap:1"),
        InventoryObligation(7770016, 0, CommandState.DISPATCHED, source_key="local:starting:7770016:0"),
    )
    assert not compare((), ownership=ownership, obligations=obligations).acquisitions


def test_unknown_and_manual_resolution_block_only_their_effect(inventory):
    compare, _ = inventory
    ids = (7770004, 7770002, 7770013)
    items = ((7770004, MISSING, 0), (7770002, MISSING, 0))
    blocked = InventoryObligation(7770004, 0, CommandState.EFFECT_UNKNOWN)
    for resolution in (None, Resolution.ACCEPTED_BY_PLAYER):
        plan = compare(ids, items, obligations=(replace(blocked, resolution=resolution),))
        assert [command.item_id for command in plan.reconciliation.commands] == [7770002]
        assert plan.unresolved_item_ids == (7770013,)
    plan = compare(ids, items, obligations=(replace(blocked, resolution=Resolution.REPLACED),))
    assert {command.item_id for command in plan.reconciliation.commands} == {7770004, 7770002}
    unknown = compare((7770013,), weapon_points=(6, 3))
    assert not unknown.reconciliation.commands and unknown.unresolved_item_ids == (7770013,)
    assert unknown.weapon_point_grants == (3,)


def test_capacity_caps_and_progressive_blood_punch_use_observed_stages(inventory):
    compare, _ = inventory
    plan = compare((7770017,) * 7 + (7770904,) * 6,
        ((7770017, OWNED, 2),), stage_masks={7770904: 0b0101})
    assert [c.stage for c in plan.reconciliation.commands if c.item_id == 7770017] == [2, 3]
    assert [(e.kind, e.stage, e.mask) for e in plan.native_effects] == [
        (NativeEffectKind.BLOOD_PUNCH, 1, 2), (NativeEffectKind.BLOOD_PUNCH, 3, 8)]
    unknown = compare((7770904, 7770070), stage_masks={})
    assert not unknown.native_effects and unknown.unresolved_item_ids == (7770070, 7770904)


def test_weapon_points_recover_gained_difference_and_isolate_unknown(inventory):
    compare, _ = inventory
    assert compare((), weapon_points=(66, 66)).weapon_point_grants == ()
    assert compare((), weapon_points=(66, 63)).weapon_point_grants == (63,)
    plan = compare((7770004,), ((7770004, MISSING, 0),), weapon_points=(66, None))
    assert [c.item_id for c in plan.reconciliation.commands] == [7770004]
    assert plan.weapon_points_error is not None and not plan.weapon_point_grants
    pending = InventoryObligation(7770903, 1, CommandState.EFFECT_UNKNOWN)
    assert compare((), weapon_points=(9, 0), obligations=(pending,)).weapon_point_grants == (0,)


def test_rune_registration_and_availability_preserve_player_slots(inventory):
    compare, definitions = inventory
    rune = 7770085
    mapping = rune_item_perk_mapping(definitions, {rune})
    perk = mapping[rune]
    native = RuneNativeState(frozenset({perk}), frozenset(), frozenset({perk}),
                            (perk, None, None), True, "save", 4)
    plan = compare((rune,), ((rune, MISSING, 0),), runes=native, rune_mapping=mapping)
    assert not plan.reconciliation.commands and not plan.runes.repairs
    assert plan.runes.noops[0].reason == "equipped_player_choice_preserved"
    missing = compare((rune,), runes=replace(native, registered_runes=frozenset()), rune_mapping=mapping)
    assert missing.runes.repairs[0].reason == "manager_registration_missing"
    unknown = replace(native, available_perks=frozenset(), registered_runes=frozenset(),
                      equipped_slots=(None, None, None), page_unlocked=None)
    plan = compare((rune, 7770004), ((7770004, MISSING, 0),), runes=unknown, rune_mapping=mapping)
    assert not plan.runes.repairs and plan.runes.status == "deferred"
    assert [c.item_id for c in plan.reconciliation.commands] == [7770004]


def test_traps_and_temporary_effects_require_a_live_acquisition():
    context = CONTEXT_BY_IDENTITY["base/hub"]
    scope = MaterializationScope("seed", 0, 1, "room", "1:100", 4, "reload", "room")
    observation = InventoryObservation("seed", 4, context.identity, context.campaign)
    def compare(ownership, **kwargs):
        return compare_inventory(ownership, context=context, scope=scope, observation=observation,
            definitions={7770159: {"type": "transient_effect"}},
            policies={7770159: ReplayPolicy(7770159, "Weakness Trap", "never_replay")}, **kwargs)
    receipts = (NetworkReceipt(7770159, 10, 1, 0),)
    assert not compare(ownership_from_ledger(receipts)).acquisitions
    ownership = ownership_from_ledger(receipts, live_receipt_indices=frozenset({0}))
    plan = compare(ownership)
    assert len(plan.acquisitions) == 1 and plan.acquisitions[0].transient
    pending = InventoryObligation(7770159, 0, CommandState.EFFECT_UNKNOWN, source_key="ap:0")
    assert not compare(ownership, obligations=(pending,)).acquisitions


def test_observation_keeps_blood_punch_mask_and_discards_a_changed_room():
    context = SimpleNamespace(
        state_key="room",
        items=SimpleNamespace(),
        checks=SimpleNamespace(),
        saves=SimpleNamespace()
    )
    context.items.ctx = context
    context.checks.ctx = context
    context.saves.ctx = context
    identity = ("created", "core", "build", "4")
    result = dict(weapons_after=0, ice_bomb_after=255, upgrades_after=0b0101 << 11,
                  _observation_scope=identity, _observation_map="game/hub/hub")
    link = SimpleNamespace(pid=123, namespace="namespace", observe_inventory=lambda: result)
    context.items.native_game_link = lambda: link
    port = SentinelInventoryObservation(context, logging.getLogger(__name__), lambda *a, **kw: None)
    kwargs = dict(room_seed_name="seed", epoch=4, context_identity="base/hub", campaign="Base")
    observed = port.observe_inventory(**kwargs)
    assert observed.stage_masks == {7770904: 0b0101} and observed.native_scope == identity
    def change_room():
        context.state_key = "other"
        return {"_observation_scope": identity, "_observation_map": "game/hub/hub", "native_state_known": 0}
    link.observe_special_inventory = change_room
    assert port.observe_inventory(**kwargs) is None


def test_completion_blood_punch_upgrade_missing_with_base_owned(inventory):
    compare, definitions = inventory
    context = next(c for c in CONTEXT_BY_IDENTITY.values() if c.campaign == "TAG2")
    scope = MaterializationScope("seed", 0, 1, "room", "1:100", 4, "reload", "room")
    observation = InventoryObservation("seed", 4, context.identity, context.campaign,
        {7770014: ItemObservation(7770014, OWNED)}, stage_masks={7770904: 1})
    upgrade = CompletionUpgrade(7770162, "perk/player/blood_punch/area_of_effect", "Doom Hunter Base")
    ownership = InventoryOwnership((7770014,), (upgrade,))
    policies = load_policy_registry(Path(__file__).resolve().parents[1] / "data/item_replay_policies.json", definitions)
    plan = compare_inventory(ownership, definitions=definitions, policies=policies, context=context,
                             scope=scope, observation=observation)
    assert len(plan.reconciliation.commands) == 1
    assert plan.reconciliation.commands[0].stage == 7770162


def test_selected_support_rune_does_not_repair_other_owned_runes(inventory):
    _, definitions = inventory
    context = next(c for c in CONTEXT_BY_IDENTITY.values() if c.campaign == "TAG2" and c.supports("support_runes_v1"))
    scope = MaterializationScope("seed", 0, 1, "room", "1:100", 4, "resync", "room")
    ids = (7770145, 7770146)
    observation = InventoryObservation("seed", 4, context.identity, context.campaign,
        {i: ItemObservation(i, MISSING) for i in ids})
    policies = load_policy_registry(Path(__file__).resolve().parents[1] / "data/item_replay_policies.json", definitions)
    plan = compare_inventory(InventoryOwnership(ids), definitions=definitions, policies=policies,
        context=context, scope=scope, observation=observation, selected_item_ids=frozenset({7770145}))
    assert [c.item_id for c in plan.reconciliation.commands] == [7770145]
    assert plan.materialization.support_rune_jobs == 1


@pytest.mark.parametrize("changed_field", range(4))
def test_observation_discards_mixed_native_identity(changed_field):
    identity = ("created", "core", "build", "4")
    other = list(identity)
    other[changed_field] = "changed"
    link = SimpleNamespace(pid=123, namespace="namespace",
        observe_inventory=lambda: dict(weapons_after=0, ice_bomb_after=255,
            _observation_scope=identity, _observation_map="game/hub/hub"),
        observe_special_inventory=lambda: dict(_observation_scope=tuple(other),
            _observation_map="game/hub/hub", native_state_known=0))
    context = SimpleNamespace(
        state_key="room",
        items=SimpleNamespace(native_game_link=lambda: link),
        checks=SimpleNamespace(),
        saves=SimpleNamespace()
    )
    context.items.ctx = context
    context.checks.ctx = context
    context.saves.ctx = context
    port = SentinelInventoryObservation(context, logging.getLogger(__name__), lambda *a, **kw: None)
    assert port.observe_inventory(room_seed_name="seed", epoch=4, context_identity="base/hub", campaign="Base") is None
