"""Unit tests for tools.maps.resource_priority."""

from pathlib import Path
import pytest

from tools.maps.resource_priority import (
    DEFAULT_PACKAGEMAPSPEC,
    load_packagemapspec_indices,
    resolve_owner,
    resolve_owner_evidence,
)


def test_packagemapspec_exists():
    assert DEFAULT_PACKAGEMAPSPEC.exists(), f"packagemapspec.json missing at {DEFAULT_PACKAGEMAPSPEC}"


@pytest.mark.parametrize(
    "candidates,expected_winner",
    [
        (
            [
                "gameresources_patch1.resources",
                "gameresources_patch2.resources",
            ],
            "gameresources_patch1.resources",
        ),
        (
            [
                "game/dlc2/e5m1_spear/e5m1_spear_patch1.resources",
                "game/dlc2/e5m1_spear/e5m1_spear_patch2.resources",
            ],
            "game/dlc2/e5m1_spear/e5m1_spear_patch1.resources",
        ),
        (
            [
                "game/sp/e3m1_slayer/e3m1_slayer_patch2.resources",
                "game/sp/e3m1_slayer/e3m1_slayer_patch3.resources",
            ],
            "game/sp/e3m1_slayer/e3m1_slayer_patch2.resources",
        ),
        (
            [
                "game/sp/e2m3_core/e2m3_core_patch2.resources",
                "game/sp/e2m3_core/e2m3_core_patch3.resources",
            ],
            "game/sp/e2m3_core/e2m3_core_patch2.resources",
        ),
        (
            [
                "game/dlc2/e5m3_hell/e5m3_hell_patch1.resources",
                "game/dlc2/e5m3_hell/e5m3_hell_patch2.resources",
            ],
            "game/dlc2/e5m3_hell/e5m3_hell_patch1.resources",
        ),
    ],
)
def test_proved_priority_pairs(candidates, expected_winner):
    winner = resolve_owner(candidates, DEFAULT_PACKAGEMAPSPEC)
    assert winner == expected_winner
    
    # Also test with order reversed in candidates input
    reversed_candidates = list(reversed(candidates))
    winner_rev = resolve_owner(reversed_candidates, DEFAULT_PACKAGEMAPSPEC)
    assert winner_rev == expected_winner


def test_resolve_owner_errors():
    with pytest.raises(ValueError, match="empty"):
        resolve_owner([])
    
    with pytest.raises(ValueError, match="duplicate"):
        resolve_owner(["gameresources_patch1.resources", "gameresources_patch1.resources"])
        
    with pytest.raises(KeyError, match="not found"):
        resolve_owner(["non_existent_archive.resources"])


def test_resolve_owner_evidence():
    evidence = resolve_owner_evidence(
        [
            "game/sp/e3m1_slayer/e3m1_slayer_patch2.resources",
            "game/sp/e3m1_slayer/e3m1_slayer_patch3.resources",
        ],
        DEFAULT_PACKAGEMAPSPEC,
    )
    assert evidence["source"] == "packagemapspec"
    assert evidence["winner"] == "game/sp/e3m1_slayer/e3m1_slayer_patch2.resources"
    assert "game/sp/e3m1_slayer/e3m1_slayer_patch2.resources" in evidence["candidates"]
    assert "game/sp/e3m1_slayer/e3m1_slayer_patch3.resources" in evidence["candidates"]
    assert len(evidence["packagemapspec_sha256"]) == 64
