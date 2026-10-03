from copy import deepcopy
import logging
import json
from pathlib import Path

from doom_eap.runtime.reconciliation_publication import ReconciliationPublisher
from doom_eap.runtime.rune_reconciliation import RuneReconciliation, RuneNativeState






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
