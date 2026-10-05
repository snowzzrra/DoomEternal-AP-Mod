import json
from pathlib import Path
from doom_eap.runtime.context_registry import validate_slot_contract


def test_dlc_missions_do_not_require_dlc_equipment():
    slot = json.loads((Path(__file__).parent / "fixtures/full_campaign_slot_data.json").read_text(encoding="utf-8"))
    slot["use_dlc_content"] = False
    assert slot["include_dlc_missions"] is True
    assert validate_slot_contract(slot)["use_dlc_content"] is False


def test_provider_process_and_source_are_all_required(tmp_path):
    from doom_eap.contracts.save_observation import GameplaySaveEvidence, admitted_evidence_matches
    from doom_eap.runtime.save_files import admitted_ap_save
    root = "ap-" + "b" * 40
    ap = tmp_path / root / "GAME-AUTOSAVE0"
    ap.mkdir(parents=True)
    (ap / "game_duration.dat").write_bytes(b"AP duration")
    (ap / "game.details").write_bytes(b"AP details")
    vanilla = tmp_path / "GAME-AUTOSAVE2"
    vanilla.mkdir()
    (vanilla / "game_duration.dat").write_bytes(b"newer vanilla")
    selected = admitted_ap_save(tmp_path, root, "game_duration.dat")
    assert selected.path.parent == ap
    facts = dict(native_root=root, process_created="101", instance_id="c" * 32)
    evidence = GameplaySaveEvidence("gameplay", 7, "GAME-AUTOSAVE0", "game/hub/hub", False, True,
        "a" * 64, root, 71, "101", "c" * 32, str(ap / "game.details"))
    assert admitted_evidence_matches(evidence, selected, "a" * 64, 71, facts)
    for changes in [dict(namespace="d" * 64), dict(native_root="ap-" + "d" * 40), dict(pid=72),
            dict(process_created="102"), dict(instance_id="d" * 32), dict(slot_directory="GAME-AUTOSAVE2"),
            dict(source_file=str(vanilla / "game.details")), dict(state="menu")]:
        assert not admitted_evidence_matches(evidence._replace(**changes), selected, "a" * 64, 71, facts)
    assert not admitted_evidence_matches(evidence, None, "a" * 64, 71, facts)


def test_native_monitor_reads_only_the_admitted_physical_slot():
    source = (Path(__file__).parents[1] / "native/client/ap_client_exe.cpp").read_text(encoding="utf-8")
    monitor = source[source.index("class MissionTransitionMonitor"):source.index("unsigned long long steamId3_", source.index("class MissionTransitionMonitor"))]
    assert "query_save_admission" in monitor and "SC_SAVE_SESSION_ROUTED" in monitor
    assert 'std::filesystem::path(steamRemoteDir_) / nativeRoot_ / slotDirectory / "game.details"' in monitor
    assert "directory_iterator(remoteRoot" not in monitor


