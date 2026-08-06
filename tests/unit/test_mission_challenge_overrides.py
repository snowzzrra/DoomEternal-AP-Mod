"""Unit tests for mission challenge override generation and validation."""

import json
from pathlib import Path
import pytest
import tempfile
import shutil

from challenge_registry import load_challenge_registry
from tools.decls.mission_challenge_decl_builder import build_mission_challenge_overrides
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


def test_build_and_validate_mission_challenge_overrides(repo_root, temp_mod_root):
    # Generate overrides
    mod_dir = temp_mod_root / "mod"
    audit = build_mission_challenge_overrides(mod_dir)

    assert audit["owner"] == "gameresources"
    assert audit["challenge_count"] == 27
    assert len(audit["written_paths"]) == 29  # 27 children + main.decl + 1 container

    registry_path = repo_root / "data" / "challenge_location_registry.json"

    # Validate generated mod structure
    errors = validate_overrides_from_mod_root(mod_dir, registry_path)
    assert errors == [], f"Validation errors: {errors}"

    # Verify e1m4 children are in gameresources
    for idx in range(1, 4):
        child_path = mod_dir / "gameresources" / "generated" / "decls" / "unlockable" / "mission_challenge" / "e1m4" / f"challenge_{idx}.decl"
        assert child_path.is_file()
        content = child_path.read_text(encoding="utf-8")
        assert "currencyToGive" in content
        assert "num = 0;" in content
        assert "CURRENCY_PRAETOR_UPGRADE" not in content

    # Verify no copies in gameresources_patch3
    patch3_dir = mod_dir / "gameresources_patch3" / "generated" / "decls" / "unlockable" / "mission_challenge"
    assert not patch3_dir.exists()

    # Verify main.decl uses existing vanilla container path
    main_path = mod_dir / "gameresources" / "generated" / "decls" / "missionchallengelist" / "missionchallenge" / "main.decl"
    assert main_path.is_file()
    main_content = main_path.read_text(encoding="utf-8")
    assert 'completionUnlock = "warehouseofflinecontainer/campaign/e1m3_complete_challenges_reward_0";' in main_content
    assert "ap/mission_challenge_aggregate_suppressed" not in main_content

    # Verify aggregate container override
    container_path = mod_dir / "gameresources" / "generated" / "decls" / "warehouseofflinecontainer" / "campaign" / "e1m3_complete_challenges_reward_0.decl"
    assert container_path.is_file()
    container_content = container_path.read_text(encoding="utf-8")
    assert "gainedItems = {\n\t\t\tnum = 0;\n\t\t}" in container_content


def test_validator_rejects_invented_decl_path(repo_root, temp_mod_root):
    mod_dir = temp_mod_root / "mod"
    build_mission_challenge_overrides(mod_dir)
    registry_path = repo_root / "data" / "challenge_location_registry.json"

    # Inject an invented decl path
    invented_path = mod_dir / "gameresources" / "generated" / "decls" / "warehouseofflinecontainer" / "ap" / "custom_fake.decl"
    invented_path.parent.mkdir(parents=True, exist_ok=True)
    invented_path.write_text("{}", encoding="utf-8")

    errors = validate_overrides_from_mod_root(mod_dir, registry_path)
    assert any("Generated DECL path does not exist in vanilla corpus" in e for e in errors)


def test_validator_rejects_competing_mod_copy(repo_root, temp_mod_root):
    mod_dir = temp_mod_root / "mod"
    build_mission_challenge_overrides(mod_dir)
    registry_path = repo_root / "data" / "challenge_location_registry.json"

    # Inject competing copy in gameresources_patch3
    competing = mod_dir / "gameresources_patch3" / "generated" / "decls" / "unlockable" / "mission_challenge" / "e1m4" / "challenge_1.decl"
    competing.parent.mkdir(parents=True, exist_ok=True)
    competing.write_text("{}", encoding="utf-8")

    errors = validate_overrides_from_mod_root(mod_dir, registry_path)
    assert any("Competing override found in alternate container" in e for e in errors)
