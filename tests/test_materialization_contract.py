"""Persistent planning and spool evidence without CommonClient/game discovery."""
import copy
import json
import logging
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from doom_eap.contracts.inventory_domain import MISSING, OWNED, UNKNOWN, InventoryObservation, ItemObservation
from doom_eap.contracts.materialization import GATE_KEY_TO_MAP, MaterializationScope
from doom_eap.contracts.runtime_context import RuntimeContext
from doom_eap.runtime.item_reconciliation import effective_ownership, load_policy_registry
from doom_eap.runtime.materialization import MaterializationPlanError, compile_materialization_plan


@pytest.fixture
def inputs():
    root = Path(__file__).resolve().parents[1]
    definitions = {int(k): v for k, v in json.loads((root / "data/items.json").read_text()).items()}
    policies = load_policy_registry(root / "data/item_replay_policies.json", definitions)
    context = RuntimeContext("tag2-spear", "TAG2", ("game/dlc2/e5m1_spear/e5m1_spear",),
                             ("e5m1_spear",), frozenset({"tag_special_v1", "special_weapon_v1", "support_runes_v1"}))
    scope = MaterializationScope("seed", 0, 1, "published-room", "1:100", 4, "reload", "dedupe-room")
    return definitions, policies, context, scope


def ownership(item_ids):
    return effective_ownership(
        tuple(SimpleNamespace(item=item_id, player=1, location=index) for index, item_id in enumerate(item_ids)),
        randomize_chainsaw=True, randomize_dash=True,
        checked_locations=frozenset(), local_checked_locations=frozenset(),
        server_checked_ready=True, hell_on_earth_locations=frozenset(),
        exultia_complete_location=7770200, slot=1,
    )


def missing_special(scope, context):
    return InventoryObservation(
        scope.room_seed_name, scope.evidence_epoch, context.identity, context.campaign,
        {7770901: ItemObservation(7770901, MISSING), 7770145: ItemObservation(7770145, MISSING)},
    )


@pytest.mark.parametrize("base_received,base_state", [(False, MISSING), (False, OWNED),
                                                       (True, MISSING), (True, UNKNOWN), (True, OWNED)])
def test_frag_upgrades_wait_for_owned_base(inputs, base_received, base_state):
    definitions, policies, _, scope = inputs
    context = RuntimeContext("base-hub", "Base", ("game/hub/hub",), ("hub",), frozenset())
    upgrades = (7770111, 7770112, 7770113, 7770114)
    received = (7770011, *upgrades) if base_received else upgrades
    observation = InventoryObservation(scope.room_seed_name, scope.evidence_epoch, context.identity, "Base",
        {7770011: ItemObservation(7770011, base_state),
         **{item_id: ItemObservation(item_id, MISSING) for item_id in upgrades}})
    for manual in (False, True):
        plan = compile_materialization_plan(ownership(received), context, replace(scope, manual=manual),
            definitions, policies, "the_crucible", observation=observation)
        applied = {command.item_id for command in plan.reconciliation.commands} & set(upgrades)
        assert applied == (set(upgrades) if base_received else set())


@pytest.mark.parametrize("base_received", [False, True])
def test_starting_frag_upgrades_wait_for_base(base_received):
    from tools.decls.devinv_builder import build_devinv_loadout

    inventory = {"Second Frag Grenade": 1}
    if base_received:
        inventory["Frag Grenade"] = 1
    loadout = build_devinv_loadout(inventory, "Ballista")
    assert ("perk/player/equipment/frag_max_capacity" in loadout) == base_received


def test_fixed_dash_uses_configured_completion_ack_and_native_missing(inputs):
    definitions, policies, context, scope = inputs
    def derived(checked=(), ready=True):
        return effective_ownership((), randomize_chainsaw=True, randomize_dash=False,
            checked_locations=frozenset(checked), local_checked_locations=frozenset({7770122}),
            server_checked_ready=ready, hell_on_earth_locations=frozenset(),
            exultia_complete_location=7770122, slot=1)
    assert not derived().vanilla_dash and not derived({7770122}, False).vanilla_dash
    owned = derived({7770122})
    assert owned.reconciliation_item_ids == (7770015,) and owned.vanilla_dash
    context = RuntimeContext("base-hub", "Base", ("game/hub/hub",), ("hub",), frozenset())
    observation = InventoryObservation(scope.room_seed_name, scope.evidence_epoch, context.identity, "Base",
        {7770015: ItemObservation(7770015, MISSING)})
    plan = compile_materialization_plan(owned, context, scope, definitions, policies, "the_crucible", observation=observation)
    assert any(command.item_id == 7770015 for command in plan.reconciliation.commands)


def test_persistent_plan_is_pure_and_keeps_cumulative_special_ownership(inputs, monkeypatch):
    definitions, policies, context, scope = inputs
    never_replay = tuple(item_id for item_id, policy in policies.items() if policy.policy == "never_replay")
    owned = ownership((7770901, 7770901, 7770145, *never_replay))
    original = copy.deepcopy(definitions)
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("planner attempted discovery"))
    observation = missing_special(scope, context)
    plan = compile_materialization_plan(owned, context, scope, definitions, policies, "progressive_special_weapon",
                                        observation=observation)
    repeated = compile_materialization_plan(owned, context, replace(scope, manual=True),
                                            definitions, policies, "progressive_special_weapon",
                                            observation=observation)
    assert plan.reconciliation == repeated.reconciliation
    assert definitions == original
    commands = [intent.command for intent in plan.reconciliation.commands]
    assert "removeInventoryItem weapon/player/crucible" not in commands
    assert any("hammer/ammo_drops_upgraded" in command for command in commands)
    allowed_persistent = {7770017, 7770088, 7770092, 7770145, 7770146, 7770147, 7770901}
    assert not (set(never_replay) - allowed_persistent) & {c.item_id for c in plan.reconciliation.commands}
    assert plan.support_rune_jobs == 1
    with pytest.raises(FrozenInstanceError):
        plan.scope.state_key = "other-room"










@pytest.fixture
def inventory_adapter():
    from doom_eap.runtime.inventory_observation import SentinelInventoryObservation

    link = SimpleNamespace(pid=123, namespace="offline")
    adapter = SentinelInventoryObservation(
        SimpleNamespace(state_key="published-room", native_game_link=lambda: link),
        logging.getLogger(__name__), lambda *args, **kwargs: None)
    return adapter, link


@pytest.mark.parametrize("item_id,map_key", list(GATE_KEY_TO_MAP.items()))
@pytest.mark.parametrize("key,state", [(0, MISSING), (1, OWNED), (255, UNKNOWN)])
def test_slayer_key_observation_and_replay_are_map_scoped(inputs, inventory_adapter, item_id, map_key, key, state):
    from doom_eap.runtime.context_registry import CONTEXT_BY_IDENTITY
    definitions, policies, _, scope = inputs
    context = next(row for row in CONTEXT_BY_IDENTITY.values() if map_key in row.map_keys)
    adapter, link = inventory_adapter
    link.observe_inventory = lambda: {"weapons_after": 0, "ice_bomb_after": 255,
                                      "dash_after": 255, "slayer_key_after": key}
    observation = adapter.observe_inventory(room_seed_name=scope.room_seed_name, epoch=scope.evidence_epoch,
        context_identity=context.identity, campaign=context.campaign)
    assert observation.get_state(item_id) == state
    assert all(observation.get_state(other) == UNKNOWN for other in GATE_KEY_TO_MAP if other != item_id)
    for manual in (False, True):
        arguments = (ownership(tuple(GATE_KEY_TO_MAP)), context, replace(scope, manual=manual), definitions,
                     policies, "the_crucible")
        if state == UNKNOWN:
            with pytest.raises(MaterializationPlanError, match="unresolved native observations"):
                compile_materialization_plan(*arguments, observation=observation)
        else:
            plan = compile_materialization_plan(*arguments, observation=observation)
            assert [command.item_id for command in plan.reconciliation.commands] == ([item_id] if key == 0 else [])
    plan = compile_materialization_plan(ownership(()), context, scope, definitions, policies,
                                       "the_crucible", observation=observation)
    assert not plan.reconciliation.commands


@pytest.mark.parametrize("identity,campaign", [("base/hub", "Base"), ("unknown", "Base"),
                                             ("base/e1m2_war", "TAG2")])
def test_slayer_key_observation_needs_matching_map_context(inventory_adapter, identity, campaign):
    adapter, link = inventory_adapter
    link.observe_inventory = lambda: {"weapons_after": 0, "ice_bomb_after": 255,
                                      "dash_after": 255, "slayer_key_after": 0}
    observation = adapter.observe_inventory(room_seed_name="seed", epoch=4, context_identity=identity, campaign=campaign)
    assert all(observation.get_state(item_id) == UNKNOWN for item_id in GATE_KEY_TO_MAP)


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("frag", [0, 1, 255])
def test_ballista_and_frag_repair_does_not_wait_for_unknown_items(inputs, inventory_adapter, manual, frag):
    from doom_eap.runtime.context_registry import CONTEXT_BY_IDENTITY
    definitions, policies, _, scope = inputs
    context = CONTEXT_BY_IDENTITY["base/e3m1_slayer"]
    adapter, link = inventory_adapter
    link.observe_inventory = lambda: {"weapons_after": 0, "ice_bomb_after": 255,
                                      "dash_after": 255, "frag_grenade_after": frag}
    observation = adapter.observe_inventory(room_seed_name=scope.room_seed_name, epoch=scope.evidence_epoch,
        context_identity=context.identity, campaign=context.campaign)
    plan = compile_materialization_plan(ownership((7770004, 7770011, 7770013)), context,
        replace(scope, manual=manual), definitions, policies, "the_crucible", observation=observation)
    repaired = {command.item_id for command in plan.reconciliation.commands}
    assert repaired == ({7770004, 7770011} if frag == 0 else {7770004})
    link.observe_inventory = lambda: {"weapons_after": 1 << 5, "ice_bomb_after": 0,
                                      "dash_after": 255, "frag_grenade_after": 1}
    observed = adapter.observe_inventory(room_seed_name=scope.room_seed_name, epoch=scope.evidence_epoch,
        context_identity=context.identity, campaign=context.campaign)
    assert observed.get_state(7770004) == OWNED and observed.get_state(7770011) == OWNED


@pytest.mark.parametrize("manual", [False, True])
def test_checkpoint_missing_full_auto_repairs_without_replaying_receipts(inputs, inventory_adapter, manual):
    from doom_eap.runtime.context_registry import CONTEXT_BY_IDENTITY
    definitions, policies, _, scope = inputs
    context = CONTEXT_BY_IDENTITY["base/e1m3_cult"]
    adapter, link = inventory_adapter
    reply = {"weapons_after": 1, "ice_bomb_after": 255, "perks_known": 2, "perks_owned": 0}
    link.observe_inventory = lambda: reply
    owned = ownership((7770059, 7770059, 7770016, 7770026, 7770903, 7770017))
    before = copy.deepcopy(owned)

    def plan():
        native = adapter.observe_inventory(room_seed_name=scope.room_seed_name, epoch=scope.evidence_epoch,
            context_identity=context.identity, campaign=context.campaign)
        return compile_materialization_plan(owned, context, replace(scope, manual=manual), definitions,
            policies, "the_crucible", observation=native, selected_item_ids=frozenset((7770059,)))

    missing = plan().reconciliation
    assert [command.item_id for command in missing.commands] == [7770059]
    assert owned == before
    reply["perks_owned"] = 2
    assert not plan().reconciliation.commands
    reply.update(perks_known=0, perks_owned=0)
    with pytest.raises(MaterializationPlanError, match="unresolved native observations"):
        plan()


def test_selected_owned_mod_resync_ignores_unselected_unknown_capacity(inputs):
    from doom_eap.runtime.context_registry import CONTEXT_BY_IDENTITY
    definitions, policies, _, scope = inputs
    context = CONTEXT_BY_IDENTITY["base/e1m3_cult"]
    observation = InventoryObservation(scope.room_seed_name, scope.evidence_epoch, context.identity, context.campaign,
        {7770059: ItemObservation(7770059, OWNED)})
    result = compile_materialization_plan(ownership((7770059, 7770017)), context, scope, definitions, policies,
        "the_crucible", observation=observation, selected_item_ids=frozenset((7770059,)))
    assert not result.reconciliation.commands




def test_inventory_observation_defers_if_checkpoint_changes_between_native_reads(inputs, inventory_adapter):
    _, _, context, scope = inputs
    adapter, link = inventory_adapter
    identity = ("created", "instance", "build", str(scope.evidence_epoch))
    link.observe_inventory = lambda: {"weapons_after": 0, "ice_bomb_after": 255,
        "perks_known": 2, "perks_owned": 0, "_observation_scope": identity,
        "_observation_map": context.runtime_maps[0]}
    link.observe_special_inventory = lambda: {"_observation_scope": (*identity[:-1], "changed")}
    assert adapter.observe_inventory(room_seed_name=scope.room_seed_name, epoch=scope.evidence_epoch,
        context_identity=context.identity, campaign=context.campaign) is None
    assert adapter.observe_inventory(room_seed_name=scope.room_seed_name, epoch=scope.evidence_epoch + 1,
        context_identity=context.identity, campaign=context.campaign) is None


@pytest.mark.parametrize('native_map,accepted', [('game/hub/hub', True),
    ('game/sp/e2m3_core/e2m3_core', False), (None, False)])
def test_fixed_dash_repair_accepts_independent_native_generation_in_the_same_map(inputs, inventory_adapter,
                                                                              native_map, accepted):
    from doom_eap.runtime.context_registry import CONTEXT_BY_IDENTITY

    definitions, policies, _, scope = inputs
    context = CONTEXT_BY_IDENTITY['base/hub']
    adapter, link = inventory_adapter
    identity = ('created', 'instance', 'build', '99')
    link.observe_inventory = lambda: {'weapons_after': 0, 'ice_bomb_after': 0, 'dash_after': 0,
        '_observation_scope': identity, '_observation_map': native_map}
    link.observe_special_inventory = lambda: {'_observation_scope': identity, 'native_state_known': 0,
                                              '_observation_map': native_map}
    observation = adapter.observe_inventory(room_seed_name=scope.room_seed_name, epoch=scope.evidence_epoch,
        context_identity=context.identity, campaign=context.campaign)
    assert (observation is not None) == accepted
    if not accepted:
        return
    owned = effective_ownership((), randomize_chainsaw=True, randomize_dash=False,
        checked_locations=frozenset({7770122}), local_checked_locations=frozenset(),
        server_checked_ready=True, hell_on_earth_locations=frozenset(), exultia_complete_location=7770122, slot=1)
    plan = compile_materialization_plan(owned, context, scope, definitions, policies, 'the_crucible',
                                        observation=observation)
    assert any(command.item_id == 7770015 for command in plan.reconciliation.commands)
