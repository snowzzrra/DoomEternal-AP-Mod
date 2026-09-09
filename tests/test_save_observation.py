"""Characterize save discovery and native evidence independently of active-slot proof."""

import os
from pathlib import Path
from types import SimpleNamespace
import pytest

from doom_eap.runtime import bridge_client as bridge


def test_observer_caches_are_scoped_by_file_slot_and_record_kind():
    observer = bridge.SaveObserver()
    base = bridge.PrimarySaveSelection("GAME-AUTOSAVE1", Path("base/game_duration.dat"), 7)
    dlc = bridge.PrimarySaveSelection("DLC1-AUTOSAVE1", Path("dlc/game_duration.dat"), 7)
    assert not observer.duration_is_current(base)
    observer.accept_duration(base)
    assert observer.duration_is_current(base)
    assert not observer.duration_is_current(dlc)
    assert not observer.duration_is_current(base._replace(mtime_ns=8))
    assert observer.observe_record("mastery", base.slot_directory, "record", (1, True))
    assert not observer.observe_record("mastery", base.slot_directory, "record", (1, True))
    assert observer.observe_record("challenge", base.slot_directory, "record", (1, True))
    assert observer.observe_record("mastery", dlc.slot_directory, "record", (1, True))
    assert observer.observe_record("mastery", base.slot_directory, "record", (2, True))
    observer.restore_slot_observations({})
    assert observer.duration_is_current(base)  # Original cache lifetime is process-local.


@pytest.mark.parametrize("exit_code", [0, 20, 7])
def test_explicit_windows_save_reader_preserves_probe_contract(tmp_path, monkeypatch, exit_code):
    from doom_eap.runtime import save_files

    source = tmp_path / "source"
    source.mkdir()
    probe, oodle, save = (source / name for name in ("probe.exe", "oodle.dll", "game_duration.dat"))
    for file in (probe, oodle, save):
        file.write_bytes(b"source")
    runtime = tmp_path / "runtime"
    decrypt_calls = []
    monkeypatch.setattr(save_files, "decrypt", lambda data, aad: decrypt_calls.append((data, aad)) or b"decrypted")
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        (runtime / "game_duration.full.bin").write_bytes(b"unpacked")
        return SimpleNamespace(returncode=exit_code, stdout="numCheckpointDeaths=1", stderr="probe error")

    monkeypatch.setattr(save_files, "subprocess", SimpleNamespace(run=run))
    # Explicit adapter platform only; no global os.name mutation affecting pathlib.
    monkeypatch.setattr(save_files, "os", SimpleNamespace(name="nt"))
    arguments = dict(steam_id=1, runtime_directory=runtime, probe_path=probe, oodle_path=oodle,
                     compat_data=tmp_path / "compat", proton_path=tmp_path / "proton",
                     steam_install=tmp_path / "steam", host_exec=None)
    if exit_code == 7:
        with pytest.raises(RuntimeError, match="code 7.*probe error"):
            save_files.unpack_game_duration(save, **arguments)
    else:
        assert save_files.unpack_game_duration(save, **arguments) == (b"unpacked", exit_code, "numCheckpointDeaths=1")
    assert decrypt_calls == [(b"source", "76561197960265729MANCUBUSgame_duration.dat")]
    assert (runtime / "game_duration.dat").read_bytes() == b"decrypted"
    assert save.read_bytes() == b"source"
    assert commands == [([str(runtime / "probe.exe"), "oodle.dll", "game_duration.dat", "game_duration.full.bin"],
                         dict(cwd=runtime, env=None, capture_output=True, text=True, timeout=10, check=False))]


def test_mission_select_scope_and_proof_decisions_are_owned_by_observer():
    observer = bridge.SaveObserver()
    selected = bridge.PrimarySaveSelection("GAME-AUTOSAVE1", Path("base/game_duration.dat"), 7)
    observer.update_selection(slot=selected.slot_directory, path=str(selected.path), token=7, native_evidence_epoch=1)
    assert observer.accept_mission_select("mission", 12)
    assert not observer.accept_mission_select("mission", 12)
    token = observer.capture_observation()
    observer.clear_mission_select()
    assert not observer.observation_is_current(token)
    assert observer.mission_select.map_name is None
    assert not observer.accept_mission_select("mission", 12)  # Logging dedupe survives invalidation.
    refreshed = observer.plan_proof(selected._replace(mtime_ns=8), proof_evidence_epoch=2, evidence_epoch=2)
    assert (refreshed.action, refreshed.new_evidence, refreshed.reset_observation_slot) == ("refresh", True, True)
    other = selected._replace(slot_directory="GAME-AUTOSAVE2")
    assert observer.plan_proof(other, proof_evidence_epoch=1, evidence_epoch=1).reason == "unproven_epoch"
    assert observer.plan_proof(other, proof_evidence_epoch=2, evidence_epoch=2).action == "activate"


def test_baseline_binding_keeps_initial_true_pending_edges_and_acknowledgement():
    from doom_eap.runtime.save_observer import SaveObserverBaselineStore

    store = SaveObserverBaselineStore({})
    observer = bridge.SaveObserver(baselines=store)
    binding = dict(session_identity="room", team=0, slot=1, registry_revision="r1",
                   doom_save_slot="GAME-AUTOSAVE1", observer_key="mastery")
    def observe(records, ack=frozenset(), **changes):
        return observer.observe_edges(**(binding | changes), records=records, acknowledged_records=ack)

    assert observe({"old": True, "new": False}) == (set(), True, set())
    assert observe({"old": True, "new": True}) == ({"new"}, False, {"new"})
    assert observe({"old": True, "new": True}) == ({"new"}, False, set())
    assert observe({"old": True, "new": True}, {"new"}) == (set(), False, set())
    for changes in ({"session_identity": "other"}, {"slot": 2}, {"team": 1},
                    {"registry_revision": "r2"}, {"doom_save_slot": "DLC1-AUTOSAVE1"}):
        assert observe({"new": True}, **changes) == (set(), True, set())


def test_primary_candidates_preserve_family_filter_and_numeric_tie_break(tmp_path, monkeypatch):
    for slot, seconds, content in (
        ("GAME-AUTOSAVE2", 10, b"save"),
        ("GAME-AUTOSAVE10", 10, b"save"),
        ("DLC1-AUTOSAVE1", 20, b"save"),
        ("BOGUS-AUTOSAVE20", 30, b"save"),
        ("HORDE-AUTOSAVE3", 40, b""),
    ):
        directory = tmp_path / slot
        directory.mkdir()
        path = directory / "game_duration.dat"
        path.write_bytes(content)
        os.utime(path, ns=(seconds * 1_000_000_000, seconds * 1_000_000_000))
    monkeypatch.setattr(bridge, "STEAM_REMOTE_DIR", tmp_path)
    monkeypatch.setattr(bridge, "STEAM_ID3", 1)
    candidates = bridge.primary_save_candidates()
    assert [entry.slot_directory for entry in candidates] == [
        "DLC1-AUTOSAVE1", "GAME-AUTOSAVE10", "GAME-AUTOSAVE2",
    ]
    assert [entry.slot_directory for entry in bridge.primary_save_candidates(slot_prefix="GAME-AUTOSAVE")] == [
        "GAME-AUTOSAVE10", "GAME-AUTOSAVE2",
    ]
    assert bridge.active_primary_save() == candidates[0]
    assert bridge.primary_save_for_slot("GAME-AUTOSAVE2") == candidates[2]
    assert candidates[0].cache_key == (candidates[0].slot_directory, str(candidates[0].path), candidates[0].mtime_ns)
    monkeypatch.setattr(bridge, "STEAM_ID3", 0)
    assert bridge.primary_save_candidates() == []


def test_native_save_evidence_retains_menu_and_parser_boundaries(tmp_path):
    path = tmp_path / "native.state"
    assert bridge.read_gameplay_save_evidence(path) is None
    path.write_text("state=menu\n", encoding="utf-8")
    assert bridge.read_gameplay_save_evidence(path) == bridge.GameplaySaveEvidence("menu", -1, "", "")
    valid = (
        "state=gameplay\nepoch=4\nslot=DLC1-AUTOSAVE2\n"
        "map_name=game\\sp\\hub\\hub/\nprovisional=TRUE\nnative_safe=true\n"
    )
    path.write_text(valid, encoding="utf-8")
    assert bridge.read_gameplay_save_evidence(path) == bridge.GameplaySaveEvidence(
        "gameplay", 4, "DLC1-AUTOSAVE2", "game/hub/hub", True, True,
    )
    for invalid in (valid.replace("epoch=4", "epoch=-1"), valid.replace("epoch=4", "epoch=bad"),
                    valid.replace("DLC1-AUTOSAVE2", "DLC1-AUTOSAVEbad")):
        path.write_text(invalid, encoding="utf-8")
        assert bridge.read_gameplay_save_evidence(path) is None


def test_unlockable_decoder_requires_unique_structured_records():
    entry = {"signal": {"unlockable": "weapon_mastery/shotgun/sticky_bomb"}}
    name = entry["signal"]["unlockable"].encode("ascii")
    header = b"UnlockableManager_0_1_2 idUnlockableManager_2"
    record = (
        bytes([len(name) * 2]) + name + b"\x0e\x0c$numUnlockableRules\x01\x01"
        b" rule_0_satisfied\x0c rule_0_statCount\x01\x03"
        b"&rule_0_statDuration\x02\xf4\x01"
        b"\x1erule_0_statname\x0a\x0akills(unlockableIsUnlocked\x0b"
    )
    assert bridge.read_unlockable_record(header + record, entry) == {
        "numUnlockableRules": 1, "rule_0_statname": "kills", "rule_0_statCount": 3,
        "rule_0_statDuration": 500, "rule_0_satisfied": True, "unlockableIsUnlocked": False,
    }
    assert bridge.read_unlockable_record(header, entry) is None
    for payload, error in (
        (record, "manager is missing"),
        (header + header + record, "manager is missing or ambiguous"),
        (header + record + record, "native record is ambiguous"),
        (header + record.replace(b"rule_0_satisfied\x0c", b"rule_0_satisfied\x00"), "invalid rule_0_satisfied"),
        (header + record.replace(b"$numUnlockableRules\x01", b"$numUnlockableRules\x09"), "invalid metric value width"),
    ):
        with pytest.raises(ValueError, match=error):
            bridge.read_unlockable_record(payload, entry)


def test_mastery_guard_checks_the_extracted_decoder(monkeypatch):
    from tools.decls.mastery_decl_builder import _assert_proven_observer

    _assert_proven_observer([{}] * 13)
    read_text = Path.read_text

    def missing_manager(path, *args, **kwargs):
        content = read_text(path, *args, **kwargs)
        return content.replace("UnlockableManager_0_1_2", "missing") if path.name == "save_records.py" else content

    monkeypatch.setattr(Path, "read_text", missing_manager)
    with pytest.raises(ValueError, match="without save reader/send path"):
        _assert_proven_observer([{}] * 13)


def test_save_readiness_preserves_freeze_slot_and_load_epoch_checks():
    ctx = object.__new__(bridge.DoomEternalContext)
    ctx.save_observer = bridge.SaveObserver()
    ctx.save_observer.accept_proof("GAME-AUTOSAVE1", 2, 10)
    ctx.save_observer.update_selection(slot="GAME-AUTOSAVE1")
    ctx.runtime_observation_lease = SimpleNamespace(gameplay_loaded_ns=10)
    assert ctx.has_authoritative_save_proof()
    ctx.runtime_observation_lease.gameplay_loaded_ns = 11
    assert not ctx.has_authoritative_save_proof()
    ctx.runtime_observation_lease = None
    ctx.save_observer.update_selection(slot="DLC1-AUTOSAVE1")
    assert not ctx.has_authoritative_save_proof()
    ctx.save_observer.update_selection(slot=None)
    assert ctx.has_authoritative_save_proof()
    ctx.invalidate_active_save_proof()
    assert not ctx.has_authoritative_save_proof()
    assert ctx.active_save_proof_slot is None
    assert ctx.active_save_proof_evidence_epoch is None
    assert ctx.active_save_proof_load_epoch is None


def test_save_proof_snapshot_freezing_is_distinct_from_invalidation():
    from dataclasses import FrozenInstanceError
    from doom_eap.runtime.save_observer import SaveObserver

    observer = SaveObserver()
    initial = observer.readiness
    observer.update_selection(slot="GAME-AUTOSAVE1")
    observer.activate_slot("GAME-AUTOSAVE1")
    assert observer.readiness.authoritative
    assert observer.readiness.frozen
    assert observer.readiness.load_epoch is None
    observer.accept_proof("GAME-AUTOSAVE1", 2, 10)
    accepted = observer.readiness
    observer.set_frozen(True)
    assert observer.readiness.authoritative and observer.readiness.load_epoch == 10
    assert not observer.has_authoritative_proof(lease_present=True, gameplay_loaded_ns=10)
    observer.set_frozen(False)
    assert observer.has_authoritative_proof(lease_present=True, gameplay_loaded_ns=10)
    observer.invalidate_proof()
    assert observer.readiness == initial
    assert accepted.slot == "GAME-AUTOSAVE1" and not accepted.frozen
    with pytest.raises(FrozenInstanceError):
        accepted.slot = "DLC1-AUTOSAVE1"


def test_provisional_family_rule_requires_all_proof_inputs():
    observer = bridge.SaveObserver()
    evidence = bridge.GameplaySaveEvidence("gameplay", 2, "DLC1-AUTOSAVE1", "rig", True, True)
    active = bridge.PrimarySaveSelection("GAME-AUTOSAVE1", Path("old.dat"), 100)
    newest = bridge.PrimarySaveSelection("DLC1-AUTOSAVE1", Path("new.dat"), 200)
    inputs = dict(
        evidence=evidence, marker_absent=True, context_known=True, expected_prefix="DLC1-AUTOSAVE",
        active_family_mismatch=True, newest=newest, active=active, evidence_epoch=2, prior_evidence_epoch=1,
    )
    assert observer.permits_provisional_family_switch(**inputs)
    for missing in (
        {"evidence": None}, {"evidence": evidence._replace(state="menu")},
        {"evidence": evidence._replace(provisional=False)}, {"evidence": evidence._replace(native_safe=False)},
        {"marker_absent": False}, {"context_known": False}, {"expected_prefix": None},
        {"active_family_mismatch": False}, {"newest": None}, {"newest": active},
        {"active": None}, {"newest": newest._replace(mtime_ns=100)},
        {"evidence_epoch": None}, {"prior_evidence_epoch": 2},
    ):
        assert not observer.permits_provisional_family_switch(**{**inputs, **missing}), missing
    observer.invalidate_proof()
    assert not observer.permits_provisional_family_switch(**{**inputs, "prior_evidence_epoch": 2})
    assert observer.readiness.frozen and not observer.readiness.authoritative


def test_selection_snapshot_does_not_imply_or_lose_proof():
    from dataclasses import FrozenInstanceError
    observer = bridge.SaveObserver()
    observer.update_selection(slot="GAME-AUTOSAVE1", path="save.dat", token=100, native_evidence_epoch=2)
    selected = observer.selection
    assert not observer.readiness.authoritative and observer.readiness.frozen
    observer.accept_proof("GAME-AUTOSAVE1", 2, 10)
    observer.update_selection(token=200)
    assert selected.token == 100 and observer.selection.token == 200
    assert observer.readiness.authoritative
    observer.invalidate_proof()
    assert observer.selection.slot == "GAME-AUTOSAVE1" and observer.selection.native_evidence_epoch == 2
    with pytest.raises(FrozenInstanceError):
        selected.path = "other.dat"


def test_observation_slot_reset_preserves_unrelated_fields():
    ctx = object.__new__(bridge.DoomEternalContext)
    document = {"GAME-AUTOSAVE1": {"weapon_masteries": {"legacy": True},
                                 "mission_challenges": {"legacy": True}, "custom": 7}}
    ctx.save_observer = bridge.SaveObserver()
    document = ctx.save_observer.restore_slot_observations(document)
    ctx.save_checks = bridge.SaveChecks(
        bridge.WEAPON_MASTERY_BY_UNLOCKABLE, bridge.MISSION_CHALLENGE_BY_UNLOCKABLE,
        bridge.MISSION_CHALLENGE_RUNTIME_MAP_BY_UNLOCKABLE, bridge.ALL_MISSION_CHALLENGES_ENTRIES, bridge.logger,
    )
    ctx.select_save_observation_slot("GAME-AUTOSAVE1")
    assert document["GAME-AUTOSAVE1"] == {"custom": 7}
    assert ctx.selected_observation_slot == "GAME-AUTOSAVE1"
    ctx.save_checks._masteries["local"] = True
    ctx.select_save_observation_slot("GAME-AUTOSAVE1")
    assert ctx.save_checks._masteries["local"]
    ctx.invalidate_save_observation_slot("GAME-AUTOSAVE1")
    assert document["GAME-AUTOSAVE1"] == {}
    assert "local" not in ctx.save_checks._masteries


def test_observation_restore_preserves_candidate_lifetime_and_live_projection():
    observer = bridge.SaveObserver()
    selected = bridge.PrimarySaveSelection("GAME-AUTOSAVE1", Path("save.dat"), 100)
    assert observer.observe_candidate(selected)
    assert not observer.observe_candidate(selected)
    readiness = observer.readiness
    document = observer.restore_slot_observations({
        "GAME-AUTOSAVE1": {"custom": 1}, "DLC2-AUTOSAVE2": [], "invalid": {},
    })
    assert document == {"GAME-AUTOSAVE1": {"custom": 1}}
    assert document is observer.observation_document
    assert observer.observation_slot is None
    assert not observer.observe_candidate(selected)
    assert observer.observe_candidate(selected._replace(mtime_ns=101))
    assert observer.select_observation_slot("GAME-AUTOSAVE1")
    assert not observer.select_observation_slot("GAME-AUTOSAVE1")
    observer.invalidate_observation_slot("GAME-AUTOSAVE1")
    assert document == {"GAME-AUTOSAVE1": {}}
    assert observer.observation_slot is None
    assert observer.readiness is readiness
