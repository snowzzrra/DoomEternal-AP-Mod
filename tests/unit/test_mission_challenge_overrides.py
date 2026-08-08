"""Unit tests for mission challenge override generation and validation."""

import json
from pathlib import Path
import pytest
import tempfile
import shutil

from challenge_registry import load_challenge_registry
from tools.decls.mission_challenge_decl_builder import (
    AGGREGATE_LIST_PATH,
    AGGREGATE_SOURCE_OWNER,
    AGGREGATE_TARGET_OWNER,
    CHILD_SOURCE_OWNER,
    CHILD_TARGET_OWNER,
    DHB_DUMMY_PATH,
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


def test_build_and_validate_mission_challenge_overrides(repo_root, temp_mod_root):
    mod_dir = temp_mod_root / "mod"
    audit = build_mission_challenge_overrides(mod_dir)

    assert audit["child_owner"] == "gameresources"
    assert audit["aggregate_owner"] == "gameresources_patch2"
    assert audit["challenge_count"] == 27
    assert len(audit["written_paths"]) == 28  # 27 children + main.decl
    experiment = audit["registration_experiment"]
    assert experiment["mission_count"] == 1
    assert experiment["contract"]["dummy"]["path"] == DHB_DUMMY_PATH
    assert experiment["contract"]["dummy"]["ap_location"] is None

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
    assert main_content.count(DHB_DUMMY_PATH) == 2  # DHB experiment + vanilla Horde
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


def test_dhb_dummy_has_zero_ap_location_ownership(repo_root):
    registry = json.loads(
        (repo_root / "data" / "challenge_location_registry.json").read_text(
            encoding="utf-8"
        )
    )
    dhb_children = [
        entry for entry in registry["mission_challenges"]
        if entry.get("mission_key") == "e1m4"
    ]
    assert [entry["location_id"] for entry in dhb_children] == [
        7770172,
        7770173,
        7770174,
    ]
    assert [entry["signal"]["unlockable"] for entry in dhb_children] == [
        "mission_challenge/e1m4/challenge_1",
        "mission_challenge/e1m4/challenge_2",
        "mission_challenge/e1m4/challenge_3",
    ]
    dhb_aggregate = next(
        entry for entry in registry["all_mission_challenges"]
        if entry["location_id"] == 7770175
    )
    assert dhb_aggregate["signal"]["children"] == [7770172, 7770173, 7770174]
    assert DHB_DUMMY_PATH not in json.dumps(registry)


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
