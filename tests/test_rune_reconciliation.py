import pytest

from rune_reconciliation import (
    RUNE_WRITER_STATUS,
    RuneNativeState,
    compile_rune_reconciliation_plan,
    rune_plan_already_recorded,
)


RUNE_A = 1001
RUNE_B = 1002
PERK_A = "perk/player/runes/a"
PERK_B = "perk/player/runes/b"
MAPPING = {RUNE_A: PERK_A, RUNE_B: PERK_B}


def _state(
    *, available=(), active=(), registered=(), slots=(None, None, None), page=True
):
    return RuneNativeState(
        frozenset(available),
        frozenset(active),
        frozenset(registered),
        slots,
        page,
        "GAME-AUTOSAVE0",
        7,
    )


def test_owned_registered_available_rune_needs_no_repair():
    plan = compile_rune_reconciliation_plan(
        [RUNE_A], _state(available=[PERK_A], registered=[PERK_A]), MAPPING
    )

    assert plan.repairs == ()
    assert plan.noops[0].reason == "registered_and_available"
    assert plan.status == "noop"


def test_available_rune_without_manager_registration_is_blocked_candidate():
    plan = compile_rune_reconciliation_plan(
        [RUNE_A], _state(available=[PERK_A], active=[PERK_A]), MAPPING
    )

    assert len(plan.repairs) == 1
    assert plan.repairs[0].reason == "manager_registration_missing"
    assert plan.status == RUNE_WRITER_STATUS


def test_equipped_rune_is_noop_and_slots_are_never_mutated():
    native = _state(
        available=[PERK_A], registered=[PERK_A], active=[PERK_A], slots=(PERK_A, None, None)
    )
    plan = compile_rune_reconciliation_plan([RUNE_A], native, MAPPING)

    assert plan.noops[0].reason == "equipped_player_choice_preserved"
    assert native.equipped_slots == (PERK_A, None, None)


def test_different_equipped_runes_are_preserved_without_overwrite_action():
    native = _state(slots=(PERK_B, None, None), available=[PERK_A, PERK_B])
    plan = compile_rune_reconciliation_plan([RUNE_A], native, MAPPING)

    assert native.equipped_slots == (PERK_B, None, None)
    assert plan.repairs[0].reason == "manager_registration_missing"


def test_unowned_missing_rune_never_creates_grant():
    plan = compile_rune_reconciliation_plan([], _state(), MAPPING)

    assert plan.entries == ()
    assert plan.repairs == ()


def test_historical_repair_plan_has_no_executable_command_surface():
    plan = compile_rune_reconciliation_plan([RUNE_A], _state(), MAPPING)

    assert plan.repairs[0].disposition == "repair_candidate"
    assert not hasattr(plan.repairs[0], "command")


def test_unknown_owned_rune_mapping_fails_closed():
    with pytest.raises(ValueError, match="no mapping"):
        compile_rune_reconciliation_plan(
            [RUNE_B], _state(), {RUNE_A: PERK_A}, expected_rune_item_ids={RUNE_A, RUNE_B}
        )


def test_same_native_epoch_and_fingerprint_dedupes_plan():
    first = compile_rune_reconciliation_plan([RUNE_A], _state(), MAPPING)
    second = compile_rune_reconciliation_plan([RUNE_A], _state(), MAPPING)
    persisted = {"fingerprint": first.fingerprint, "status": first.status}

    assert first == second
    assert rune_plan_already_recorded(persisted, second)


def test_game_details_snapshot_keeps_distinct_rune_surfaces():
    native = RuneNativeState.from_game_details(
        {
            "availablePerkDeclName_0": PERK_A,
            "activePerkDeclName_0": PERK_B,
            "runeName_0": PERK_A,
            "runeSlotName_0": PERK_B,
            "STAT_RUNE_PAGE_UNLOCKED": "1",
            "_path": "/save/game.details",
            "_mtime_ns": 99,
        },
        save_slot="GAME-AUTOSAVE1",
        evidence_epoch=8,
    )

    assert native.available_perks == {PERK_A}
    assert native.active_perks == {PERK_B}
    assert native.registered_runes == {PERK_A}
    assert native.equipped_slots == (PERK_B, None, None)
    assert native.page_unlocked is True
