"""Persistent planning and spool evidence without CommonClient/game discovery."""
import copy
from dataclasses import FrozenInstanceError, replace
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from doom_eap.contracts.materialization import GATE_KEY_TO_MAP, MaterializationScope
from doom_eap.contracts.inventory_domain import InventoryObservation, ItemObservation, MISSING, OWNED, UNKNOWN
from doom_eap.contracts.runtime_context import RuntimeContext
from doom_eap.runtime.command_spool import CommandSpool
from doom_eap.runtime.item_reconciliation import effective_ownership, load_policy_registry
from doom_eap.runtime.materialization import MaterializationPlanError, compile_materialization_plan
from doom_eap.runtime.materialization_coordinator import MaterializationCoordinator
from doom_eap.runtime.reconciliation_publication import ReconciliationPublisher


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


def test_failed_publication_retries_and_disappearance_stays_unverified(inputs, tmp_path, caplog):
    definitions, policies, context, scope = inputs
    log = logging.getLogger("materialization-contract")
    spool = CommandSpool(tmp_path, arm_rpc=lambda *a: None, log_delivery=lambda *a, **k: None, logger=log)
    attempt = 0

    def send(command, **kwargs):
        nonlocal attempt
        attempt += 1
        if attempt == 2:
            return False
        return spool.publish(command, **kwargs).accepted

    publisher = ReconciliationPublisher(send, spool.exists, log)
    coordinator = MaterializationCoordinator(log)
    state = {}
    coordinator.bind(state)
    coordinator.trigger("reload")
    persisted = []
    owned = ownership((7770901, 7770901))

    def reconcile():
        return coordinator.reconcile(scope, context, owned, None, definitions, policies,
                                     "progressive_special_weapon", publisher,
                                     lambda: persisted.append(copy.deepcopy(state)),
                                     observation=missing_special(scope, context))

    failed = reconcile()
    assert failed.error and not failed.complete_transition
    assert "completed_key" not in state and not persisted
    assert not coordinator.poll_completion(publisher)
    completed = reconcile()
    assert completed.plan and completed.complete_transition
    assert len(persisted) == 2 and "pending_plan" in persisted[0] and "pending_plan" not in persisted[1]
    assert state["completed_key"].startswith("dedupe-room:")
    assert state["completion_criterion"] == "durable_spool_publication"
    files = list(tmp_path.glob("*.cmd"))
    assert len(files) == len(completed.plan.commands)  # retry kept the already-published first command
    assert all(path.name.startswith("recv-") for path in files)
    assert {path.stem for path in files} == {
        spool.scoped_id(command.spool_id, scope.state_key) for command in completed.plan.commands
    }
    assert not coordinator.poll_completion(publisher)
    for path in files:
        path.unlink()
    with caplog.at_level(logging.INFO, logger=log.name):
        assert coordinator.poll_completion(publisher)
    assert "semantic_state=command_consumed_unverified" in caplog.text
    assert not coordinator.poll_completion(publisher)
    assert reconcile().plan is not None


def test_rebind_discards_old_room_completion_and_triggers(inputs):
    definitions, policies, context, scope = inputs
    coordinator = MaterializationCoordinator(logging.getLogger(__name__))
    publisher = SimpleNamespace(publish=lambda *a, **k: (True, None), exists=lambda *a: False)
    old_state = {}
    coordinator.bind(old_state)
    coordinator.reconcile(scope, context, ownership((7770901,)), None, definitions, policies,
                          "progressive_special_weapon", publisher, lambda: None)
    original = copy.deepcopy(old_state)
    coordinator.trigger("old-room")
    coordinator.bind({})
    assert not coordinator.snapshot.triggers
    assert not coordinator.poll_completion(publisher)
    assert old_state == original


def test_fortress_terminal_result_requires_same_lease(tmp_path):
    spool = CommandSpool(tmp_path, arm_rpc=lambda *a: None, log_delivery=lambda *a, **k: None,
                         logger=logging.getLogger("fortress-contract"))
    scoped = spool.scoped_id("fortress-phase1", "seed:0:1:generation")
    (tmp_path / (scoped + ".result")).write_text("outcome=stale_lease\nlease=1:2\n")
    assert spool.fortress_outcome("fortress-phase1", "seed:0:1:generation", "1:2") == "stale_lease"
    assert spool.fortress_outcome("fortress-phase1", "seed:0:1:generation", "1:3") is None
    (tmp_path / (scoped + ".result")).write_text("outcome=command_consumed_unverified\nlease=1:2\n")
    assert spool.fortress_outcome("fortress-phase1", "seed:0:1:generation", "1:2") == "command_consumed_unverified"


def test_automatic_history_ledger_remains_separate_from_receipt_and_save_epochs():
    coordinator = MaterializationCoordinator(logging.getLogger(__name__))
    document = {}
    coordinator.bind({}, document)
    plan = SimpleNamespace(commands=("published",))
    coordinator.record_automatic_failure("repair", 7, "history-a")
    assert not coordinator.automatic_already_applied(7, "history-a")
    assert coordinator.record_automatic_success(plan, "repair", 7, "history-a", 12) == "complete"
    assert coordinator.automatic_already_applied(7, "history-a")
    assert not coordinator.automatic_already_applied(8, "history-a")
    assert not coordinator.automatic_already_applied(7, "history-b")
    assert document["processed_boundary"] == 12
    coordinator.reset_automatic()
    assert document == {}


@pytest.fixture
def inventory_adapter():
    import ast
    import time
    from doom_eap.runtime.context_registry import CONTEXT_BY_IDENTITY
    root = Path(__file__).resolve().parents[1]
    # Load the production adapter without CommonClient's application configuration.
    tree = ast.parse((root / "doom_eap/runtime/bridge_client.py").read_text(encoding="utf-8-sig"))
    node = next(row for row in tree.body if isinstance(row, ast.ClassDef) and row.name == "SentinelInventoryObservation")
    namespace = dict(InventoryObservation=InventoryObservation, ItemObservation=ItemObservation,
        OWNED=OWNED, MISSING=MISSING, GATE_KEY_TO_MAP=GATE_KEY_TO_MAP, CONTEXT_BY_IDENTITY=CONTEXT_BY_IDENTITY,
        time=time, logger=logging.getLogger(__name__), log_item_event=lambda *args, **kwargs: None)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "production-observation-adapter", "exec"), namespace)
    link = SimpleNamespace(pid=123, namespace="offline")
    adapter = namespace["SentinelInventoryObservation"](SimpleNamespace(state_key="published-room", native_game_link=lambda: link))
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
