"""Unit tests for mission challenge override generation and validation."""

import json
from pathlib import Path
import pytest
import tempfile
import shutil

from challenge_registry import (
    MISSION_CHALLENGE_RUNTIME_MAP_BY_MISSION_KEY,
    load_challenge_registry,
    validate_challenge_registry,
)
from tools.decls.mission_challenge_decl_builder import (
    AGGREGATE_LIST_PATH,
    AGGREGATE_SOURCE_OWNER,
    AGGREGATE_TARGET_OWNER,
    CHILD_SOURCE_OWNER,
    CHILD_TARGET_OWNER,
    DHB_DUMMY_PATH,
    NEKRAVOL_DUMMY_PATH,
    PLAN_B_REGISTRATIONS,
    _challenge_paths,
    _level_blocks,
    build_mission_challenge_overrides,
)
from tools.validation.validate_challenge_overrides import (
    validate_overrides_from_mod_root,
    validate_overrides_from_files,
)


@pytest.fixture
def repo_root():
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def temp_mod_root():
    tmp = tempfile.mkdtemp()
    yield Path(tmp)
    shutil.rmtree(tmp)


def test_mission_challenge_owner_contracts_require_patch2_main(repo_root):
    assert CHILD_SOURCE_OWNER == CHILD_TARGET_OWNER == "gameresources"
    assert AGGREGATE_SOURCE_OWNER == AGGREGATE_TARGET_OWNER == "gameresources_patch2"
    source = (
        repo_root
        / "vanilla_decls"
        / "owners"
        / AGGREGATE_SOURCE_OWNER
        / "generated"
        / "decls"
        / AGGREGATE_LIST_PATH
    )
    assert source.is_file(), "canonical patch2 mission challenge main.decl is required"


def test_mission_challenge_runtime_maps_are_complete_and_consistent(repo_root):
    registry = load_challenge_registry(
        repo_root / "data" / "challenge_location_registry.json"
    )
    entries = registry["mission_challenges"]
    assert len(entries) == 27
    assert all(
        entry["runtime_map"]
        == MISSION_CHALLENGE_RUNTIME_MAP_BY_MISSION_KEY[entry["mission_key"]]
        for entry in entries
    )

    missing = json.loads(json.dumps(registry))
    missing["mission_challenges"][0].pop("runtime_map")
    with pytest.raises(ValueError, match="runtime_map is required"):
        validate_challenge_registry(missing)

    mismatched = json.loads(json.dumps(registry))
    mismatched["mission_challenges"][0]["runtime_map"] = (
        MISSION_CHALLENGE_RUNTIME_MAP_BY_MISSION_KEY["e1m4"]
    )
    with pytest.raises(ValueError, match="does not match mission_key"):
        validate_challenge_registry(mismatched)

    import bridge_client

    assert len(bridge_client.MISSION_CHALLENGE_RUNTIME_MAP_BY_UNLOCKABLE) == 27
    assert bridge_client.MISSION_CHALLENGE_RUNTIME_MAP_BY_UNLOCKABLE[
        "mission_challenge/e1m4/challenge_1"
    ] == "game/sp/e1m4_boss/e1m4_boss"


def test_build_and_validate_mission_challenge_overrides(repo_root, temp_mod_root):
    mod_dir = temp_mod_root / "mod"
    audit = build_mission_challenge_overrides(mod_dir)

    assert audit["child_owner"] == "gameresources"
    assert audit["aggregate_owner"] == "gameresources_patch2"
    assert audit["challenge_count"] == 27
    assert len(audit["written_paths"]) == 28  # 27 children + main.decl
    experiment = audit["registration_experiment"]
    assert experiment["mission_count"] == 3
    contracts = experiment["contracts"]
    assert tuple(contract["mission_key"] for contract in contracts) == tuple(
        plan["mission_key"] for plan in PLAN_B_REGISTRATIONS
    )
    assert contracts[0]["dummy"]["path"] == DHB_DUMMY_PATH
    assert contracts[1]["dummy"]["path"] == NEKRAVOL_DUMMY_PATH
    assert contracts[2]["dummy"]["path"] == NEKRAVOL_DUMMY_PATH
    assert all(contract["dummy"]["ap_location"] is None for contract in contracts)

    registry_path = repo_root / "data" / "challenge_location_registry.json"

    errors = validate_overrides_from_mod_root(mod_dir, registry_path)
    assert errors == [], f"Validation errors: {errors}"

    for idx in range(1, 4):
        child_path = mod_dir / "gameresources" / "generated" / "decls" / "unlockable" / "mission_challenge" / "e1m4" / f"challenge_{idx}.decl"
        assert child_path.is_file()
        content = child_path.read_text(encoding="utf-8")
        assert "currencyToGive" in content
        assert "num = 0;" in content
        assert "CURRENCY_PRAETOR_UPGRADE" not in content

    patch3_dir = mod_dir / "gameresources_patch3" / "generated" / "decls" / "unlockable" / "mission_challenge"
    assert not patch3_dir.exists()

    main_path = mod_dir / "gameresources_patch2" / "generated" / "decls" / "missionchallengelist" / "missionchallenge" / "main.decl"
    assert main_path.is_file()
    main_content = main_path.read_text(encoding="utf-8")
    assert main_content.count("mission_challenge/e1m4/challenge_1") == 1
    assert main_content.count("mission_challenge/e1m4/challenge_2") == 1
    assert main_content.count("mission_challenge/e1m4/challenge_3") == 1
    assert main_content.count(DHB_DUMMY_PATH) == 2  # DHB Plan B + vanilla Horde
    assert main_content.count(NEKRAVOL_DUMMY_PATH) == 3  # two Plan B + vanilla Horde
    blocks = {index: block for index, _, _, block in _level_blocks(main_content)}
    for contract in contracts:
        assert _challenge_paths(blocks[contract["level_index"]]) == (
            *contract["real_challenges"],
            contract["dummy"]["path"],
        )
    vanilla_main = (
        repo_root
        / "vanilla_decls"
        / "owners"
        / "gameresources_patch2"
        / "generated"
        / "decls"
        / AGGREGATE_LIST_PATH
    ).read_text(encoding="utf-8").replace("\r\n", "\n")
    vanilla_blocks = {
        index: block for index, _, _, block in _level_blocks(vanilla_main)
    }
    restored = main_content
    for contract in contracts:
        current_blocks = {
            index: block for index, _, _, block in _level_blocks(restored)
        }
        restored = restored.replace(
            current_blocks[contract["level_index"]],
            vanilla_blocks[contract["level_index"]],
            1,
        )
    assert restored == vanilla_main
    assert main_content.count("mission_challenge/e3m2/challenge_1") == 1
    assert main_content.count("mission_challenge/e3m2_b/challenge_1") == 1
    assert "mission_challenge/e1m3/challenge_1" in main_content
    assert "mission_challenge/e2m1/challenge_1" in main_content
    assert "mission_challenge/e3m1/challenge_1" in main_content
    assert "mission_challenge/e3m3/challenge_1" in main_content
    assert "completionUnlock" not in main_content

    for forbidden_owner in ("gameresources", "gameresources_patch1", "gameresources_patch3"):
        forbidden_main = mod_dir / forbidden_owner / "generated" / "decls" / "missionchallengelist" / "missionchallenge" / "main.decl"
        assert not forbidden_main.exists()

    container_path = mod_dir / "gameresources" / "generated" / "decls" / "warehouseofflinecontainer" / "campaign" / "e1m3_complete_challenges_reward_0.decl"
    assert not container_path.exists()


def test_plan_b_dummies_have_zero_ap_location_ownership(repo_root):
    registry = json.loads(
        (repo_root / "data" / "challenge_location_registry.json").read_text(
            encoding="utf-8"
        )
    )
    expected_children = {
        "e1m4": [7770172, 7770173, 7770174],
        "e3m2_hell": [7770358, 7770359, 7770360],
        "e3m2_hell_b": [7770383, 7770384, 7770385],
    }
    for mission_key, location_ids in expected_children.items():
        children = [
            entry for entry in registry["mission_challenges"]
            if entry.get("mission_key") == mission_key
        ]
        assert [entry["location_id"] for entry in children] == location_ids
        aggregate = next(
            entry for entry in registry["all_mission_challenges"]
            if entry["mission_key"] == mission_key
        )
        assert aggregate["signal"]["children"] == location_ids
        assert aggregate["signal"]["required_count"] == 3
    assert DHB_DUMMY_PATH not in json.dumps(registry)
    assert NEKRAVOL_DUMMY_PATH not in json.dumps(registry)


def test_validator_rejects_invented_decl_path(repo_root, temp_mod_root):
    mod_dir = temp_mod_root / "mod"
    build_mission_challenge_overrides(mod_dir)
    registry_path = repo_root / "data" / "challenge_location_registry.json"

    invented_path = mod_dir / "gameresources" / "generated" / "decls" / "warehouseofflinecontainer" / "ap" / "custom_fake.decl"
    invented_path.parent.mkdir(parents=True, exist_ok=True)
    invented_path.write_text("{}", encoding="utf-8")

    errors = validate_overrides_from_mod_root(mod_dir, registry_path)
    assert any("Generated DECL path does not exist in vanilla corpus" in e for e in errors)


def test_validator_rejects_competing_mod_copy(repo_root, temp_mod_root):
    mod_dir = temp_mod_root / "mod"
    build_mission_challenge_overrides(mod_dir)
    registry_path = repo_root / "data" / "challenge_location_registry.json"

    competing = mod_dir / "gameresources_patch3" / "generated" / "decls" / "unlockable" / "mission_challenge" / "e1m4" / "challenge_1.decl"
    competing.parent.mkdir(parents=True, exist_ok=True)
    competing.write_text("{}", encoding="utf-8")

    errors = validate_overrides_from_mod_root(mod_dir, registry_path)
    assert any("Child override found outside gameresources" in e for e in errors)


@pytest.mark.parametrize(
    "wrong_owner",
    ["gameresources", "gameresources_patch1", "gameresources_patch3"],
)
def test_validator_rejects_aggregate_main_in_wrong_owner(
    repo_root, temp_mod_root, wrong_owner, monkeypatch
):
    import tools.decls.mission_challenge_decl_builder as builder

    mod_dir = temp_mod_root / "mod"
    monkeypatch.setattr(builder, "AGGREGATE_TARGET_OWNER", wrong_owner)
    builder.build_mission_challenge_overrides(mod_dir)
    registry_path = repo_root / "data" / "challenge_location_registry.json"

    errors = validate_overrides_from_mod_root(mod_dir, registry_path)
    assert any(
        f"Aggregate main.decl found outside gameresources_patch2: {wrong_owner}" in e
        for e in errors
    )


def test_validator_rejects_failed_completion_unlock_theory_container(repo_root, temp_mod_root):
    mod_dir = temp_mod_root / "mod"
    build_mission_challenge_overrides(mod_dir)
    registry_path = repo_root / "data" / "challenge_location_registry.json"

    failed_container = mod_dir / "gameresources" / "generated" / "decls" / "warehouseofflinecontainer" / "campaign" / "e1m3_complete_challenges_reward_0.decl"
    failed_container.parent.mkdir(parents=True, exist_ok=True)
    failed_container.write_text("{}", encoding="utf-8")

    errors = validate_overrides_from_mod_root(mod_dir, registry_path)
    assert any("Forbidden warehouse aggregate override found" in e for e in errors)
