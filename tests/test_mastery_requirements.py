import logging
import hashlib
import zipfile
import pytest

from doom_eap.content.mastery_requirements import mastery_entries_for_profile, patch_mastery_decl
from doom_eap.contracts.challenge_registry import load_challenge_registry
from doom_eap.contracts.save_observation import unlockable_record_complete
from doom_eap.runtime.save_check_observation import SaveCheckBinding, SaveCheckObservations
from doom_eap.runtime.save_checks import SaveChecks
from doom_eap.runtime.save_observer import SaveObserver, SaveObserverBaselineStore
from tools.decls.mastery_decl_builder import _reward_free_override
from tools.release.room_payloads import assemble_room_files


@pytest.mark.parametrize(("profile", "sticky_goal"), [("Original", 25), ("Half", 13), ("Quarter", 7)])
def test_native_mastery_targets_preserve_actions_and_normal_upgrades(profile, sticky_goal):
    originals = load_challenge_registry()["weapon_masteries"]
    scaled = mastery_entries_for_profile(originals, profile)
    assert scaled == mastery_entries_for_profile(scaled, profile)
    assert scaled[0]["signal"]["rule_0_statCount"] == sticky_goal
    for original, entry in zip(originals, scaled):
        source, perk = _reward_free_override(original)
        result = patch_mastery_decl(source, entry)
        old_goal, new_goal = original["signal"]["rule_0_statCount"], entry["signal"]["rule_0_statCount"]
        assert result.replace(f"count = {new_goal};", f"count = {old_goal};", 1) == source
        assert "perkToGive" not in result and "addStats" not in perk
        if entry["requirement"]["kind"] == "damage":
            assert new_goal == old_goal
        else:
            assert 1 <= new_goal <= old_goal
        complete = {key: value for key, value in entry["signal"].items() if key.startswith("rule_") or key in {"numUnlockableRules", "unlockableIsUnlocked"}}
        assert unlockable_record_complete(complete, entry["signal"])
        assert not unlockable_record_complete({**complete, "rule_0_statCount": new_goal - 1}, entry["signal"])
        assert not unlockable_record_complete({**complete, "rule_0_statname": "unrelated_kill"}, entry["signal"])
        assert not unlockable_record_complete({**complete, "rule_0_satisfied": False}, entry["signal"])


def test_half_mastery_reader_observes_native_completion_without_perk_ownership():
    entry = mastery_entries_for_profile(load_challenge_registry()["weapon_masteries"], "Half")[0]
    unlockable = entry["signal"]["unlockable"]
    service = SaveChecks({unlockable: entry}, {}, {}, (), logging.getLogger("mastery"))
    persisted = {}
    observer = SaveObserver(baselines=SaveObserverBaselineStore(persisted))
    binding = SaveCheckBinding("seed", 0, 1, "room", "revision", frozenset(), True, True)
    observations = SaveCheckObservations(observer, binding, lambda: None, logging.getLogger("mastery"))
    record = {key: value for key, value in entry["signal"].items() if key.startswith("rule_") or key in {"numUnlockableRules", "unlockableIsUnlocked"}}
    service.observe_masteries({unlockable: {**record, "rule_0_statCount": 12}}, "save", "GAME-AUTOSAVE0", observations, authoritative=True)
    service.observe_masteries({unlockable: record}, "save", "GAME-AUTOSAVE0", observations, authoritative=True)
    assert service._masteries[unlockable]
    assert record["rule_0_statCount"] == 13


def test_room_compilation_selects_mastery_targets_from_original_bytes(tmp_path):
    masteries = load_challenge_registry()["weapon_masteries"]
    base, payload = tmp_path / "base.zip", tmp_path / "payload.zip"
    sources = {"gameresources/generated/decls/" + entry["decls"]["unlockable"]["path"]:
               _reward_free_override(entry)[0].encode("utf-8") for entry in masteries}
    sources["fixture/maps/fixture.entities"] = b"compiled fixture"
    with zipfile.ZipFile(base, "w") as archive:
        for member, source in sources.items():
            archive.writestr(member, source)
    raw = b"source fixture\r\n"
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("sources/fixture.entities", raw)
    manifest = {"schema_version": 2, "model": "dependent_map_payloads",
        "physical_option_keys": ["randomize_chainsaw", "randomize_dash", "randomize_first_battery"],
        "base_members": list(sources), "context_targets": {"fixture": "fixture/maps/fixture.entities"},
        "maps": {"fixture": {"option_keys": [], "state_policy": "off_only", "target_member": "fixture/maps/fixture.entities",
            "states": [{"options": {}, "source": "base", "member": None,
                        "sha256": hashlib.sha256(sources["fixture/maps/fixture.entities"]).hexdigest()}]}},
        "source_snapshots": {"fixture": {"member": "sources/fixture.entities",
            "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}}}
    half, _ = assemble_room_files(base, payload, manifest, {"mastery_requirements": "Half"})
    repeated, _ = assemble_room_files(base, payload, manifest, {"mastery_requirements": "Half"})
    original, _ = assemble_room_files(base, payload, manifest, {"mastery_requirements": "Original"})
    assert half == repeated and original == sources
    sticky_member = "gameresources/generated/decls/" + masteries[0]["decls"]["unlockable"]["path"]
    assert b"count = 13;" in half[sticky_member]
