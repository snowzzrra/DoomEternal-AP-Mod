from copy import deepcopy
import logging
import json
from pathlib import Path

from doom_eap.contracts.runtime_context import MapIdentitySnapshot
from doom_eap.runtime.bootstrap import Bootstrap, BootstrapOwnership
from doom_eap.runtime.bootstrap_actions import BOOTSTRAP_ACTIONS
from doom_eap.runtime.command_spool import CommandSpool
from doom_eap.runtime.reconciliation_publication import ReconciliationPublisher
from doom_eap.runtime.rune_reconciliation import RuneReconciliation, RuneNativeState


def test_bootstrap_remains_disabled_and_consumption_is_unknown_with_legacy_quarantine(tmp_path):
    owner = Bootstrap(logging.getLogger(__name__))
    state = {"actions": {"rune_page": {"status": "applied"}}}
    owner.bind(state)
    assert owner.actions["v1:rune_page"]["status"] == "delivered_effect_unknown"
    before = deepcopy(state)
    owner.action_state("frag_acquired")
    assert state == before  # Presentation queries no longer create pending work.
    spool = CommandSpool(tmp_path, arm_rpc=lambda *_: None, log_delivery=lambda *a, **k: None,
                         logger=logging.getLogger(__name__))
    ownership = BootstrapOwnership(frozenset({7770011}), False)
    current_map = next(iter(BOOTSTRAP_ACTIONS["frag_acquired"]["maps_supported"]))
    commits = []
    owner.onboard("on_connect", ownership=ownership, map_identity=MapIdentitySnapshot(current_map=current_map),
                  state_key="room", item_ready=True, rpc_ready=True, spool=spool,
                  persist=lambda: commits.append(deepcopy(state)))
    assert not list(tmp_path.iterdir())
    assert state == before and not commits
    assert owner.enqueue("frag_acquired", "manual_diagnostic", ownership=ownership, current_map=current_map,
                         state_key="room", spool=spool, persist=lambda: commits.append(deepcopy(state)))
    assert owner.action_state("frag_acquired")["status"] == "queued"
    for path in tmp_path.glob("*.cmd"):
        path.unlink()
    legacy = tmp_path / "bootstrap-v1-rune_page.processing"
    legacy.write_text("historical", encoding="utf-8")
    owner.reconcile_spool(state_key="room", spool=spool, persist=lambda: commits.append(deepcopy(state)))
    assert owner.action_state("frag_acquired")["status"] == "delivered_effect_unknown"
    assert owner.action_state("rune_page", 1)["status"] == "quarantined_runtime_invalid"
    assert legacy.with_suffix(".quarantined").read_text() == "historical"
    assert not owner.enqueue("frag_acquired", "manual_diagnostic", ownership=ownership, current_map=current_map,
                             state_key="room", spool=spool, persist=lambda: None)


def test_rune_repair_owns_publication_ledger_and_distinct_perk_epoch():
    owner = RuneReconciliation(logging.getLogger(__name__))
    state, perk = {}, {"epoch": 8, "delivered": {"existing": True}}
    owner.bind(state, perk)
    rune = 7770085
    definitions = {int(key): value for key, value in json.loads((Path(__file__).resolve().parents[1] / "data/items.json").read_text(encoding="utf-8")).items()}
    native = RuneNativeState.from_game_details({"STAT_RUNE_PAGE_UNLOCKED": True}, save_slot="GAME-AUTOSAVE1", evidence_epoch=10)
    plan = owner.compile([rune], native, definitions, frozenset({rune}))
    assert plan.repairs
    sent = []
    publisher = ReconciliationPublisher(lambda *a, **k: sent.append((a, k)) or True,
                                         lambda *a: False, logging.getLogger(__name__))
    commits = []
    result, error = owner.reconcile(plan, "level_ready", slot_identity="seed-1-1", state_key="room",
                                    publisher=publisher, persist=lambda: commits.append(deepcopy(state)))
    assert result is plan and error is None and len(commits) == 1
    assert all(call[1]["state_key"] == "room" for call in sent)
    assert owner.epoch == 9 and perk["delivered"] == {"existing": True}
    count = len(sent)
    owner.reconcile(plan, "reconnect", slot_identity="seed-1-1", state_key="room", publisher=publisher, persist=lambda: None)
    assert len(sent) == count
    assert owner.advance("load") == 10
    owner.reset()
    assert state == {} and perk == {"epoch": 1, "delivered": {}}
