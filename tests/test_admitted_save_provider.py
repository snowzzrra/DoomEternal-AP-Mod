import json
from pathlib import Path
from doom_eap.runtime.context_registry import validate_slot_contract
from doom_eap.content.slot_data_contract import generation_fingerprint


def test_dlc_missions_do_not_require_dlc_equipment():
    slot = json.loads((Path(__file__).parent / "fixtures/resolved_room_slot_data.json").read_text(encoding="utf-8"))
    slot["runtime"]["use_dlc_content"] = False
    slot["native_generation_fingerprint"] = generation_fingerprint(slot)
    assert "e5m4_boss" in slot["campaign"]["sequence"]
    assert validate_slot_contract(slot)["runtime"]["use_dlc_content"] is False


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
    proton = evidence._replace(source_file=str(tmp_path).replace("\\", "/") + "\\" + root
        + "\\GAME-AUTOSAVE0\\game.details")
    assert admitted_evidence_matches(proton, selected, "a" * 64, 71, facts)
    for changes in (dict(pid=72), dict(slot_directory="GAME-AUTOSAVE2"),
                    dict(source_file=proton.source_file.replace("GAME-AUTOSAVE0", "GAME-AUTOSAVE2"))):
        assert not admitted_evidence_matches(proton._replace(**changes), selected, "a" * 64, 71, facts)
    for changes in [dict(namespace="d" * 64), dict(native_root="ap-" + "d" * 40), dict(pid=72),
            dict(process_created="102"), dict(instance_id="d" * 32), dict(slot_directory="GAME-AUTOSAVE2"),
            dict(source_file=str(vanilla / "game.details")), dict(state="menu")]:
        assert not admitted_evidence_matches(evidence._replace(**changes), selected, "a" * 64, 71, facts)
    assert not admitted_evidence_matches(evidence, None, "a" * 64, 71, facts)
