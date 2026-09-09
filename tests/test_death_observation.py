import logging
from pathlib import Path

from doom_eap.contracts.save_observation import PrimarySaveSelection
from doom_eap.runtime.death_observation import DeathObservation


def test_checkpoint_baseline_token_and_single_respawn_carryover_remain_distinct():
    owner = DeathObservation(logging.getLogger("death-observation"))

    def observe(tick, dead, epoch, slot="DLC1-AUTOSAVE0"):
        return owner.observe_checkpoint(
            {"checkpoint_death": dead, "raw_num_checkpoint_deaths": int(dead)},
            PrimarySaveSelection(slot, Path(slot) / "game_duration.dat", tick), epoch, "map",
        )

    assert observe(1, True, "load-1") is None
    assert observe(2, True, "load-2") is None
    assert observe(3, False, "load-2") is None
    event = observe(4, True, "load-2")
    assert event == "DLC1-AUTOSAVE0:game_duration.dat:4:deaths=1"
    assert observe(4, True, "load-2") is None
    assert observe(5, True, "load-3") is None
    assert observe(6, True, "load-3") is not None
    assert observe(7, True, "load-3", "GAME-AUTOSAVE0") is None
    snapshot = owner.checkpoints
    observe(8, False, "load-3")
    assert snapshot["DLC1-AUTOSAVE0"] is True
    assert owner.checkpoints["DLC1-AUTOSAVE0"] is False


def test_details_edge_needs_new_timestamp_and_path_switch_rebaselines(caplog):
    owner = DeathObservation(logging.getLogger("death-observation"))

    def observe(path, tick, dead):
        return owner.observe_details({"_path": path, "_mtime_ns": tick, "diedLastGame": str(int(dead))})

    assert not observe("slot-A", 1, True)
    assert not observe("slot-A", 2, False)
    assert not observe("slot-A", 2, True)
    assert not observe("slot-A", 3, False)
    assert observe("slot-A", 4, True)
    assert not observe("slot-B", 5, True)
    owner.probe_failed(OSError("decoder"))
    owner.probe_failed(OSError("decoder"))
    assert len(caplog.records) == 1 and owner.warning == "decoder"
    owner.probe_succeeded()
    assert owner.warning is None
