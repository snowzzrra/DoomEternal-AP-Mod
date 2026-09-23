import struct
from pathlib import Path

from doom_eap.runtime.weapon_points import SentinelWeaponPoints, WeaponPointsBlocked
from doom_eap.contracts.foundation import MASTERY_ITEM_BITS, compile_item_delivery_plan


SCOPE = {
    "availability": "enabled",
    "lifecycle": "active",
    "process_created": 44,
    "instance_id": "11" * 16,
    "lifecycle_generation": 7,
    "build_id": "build",
}


def link_with(result_factory):
    link = SentinelWeaponPoints(Path("probe"), 42, "a" * 64)
    messages = []

    def run(args, payload=None):
        if args[0] == "--pid":
            return SCOPE
        operation = struct.unpack_from("<H", payload, 6)[0]
        request_id = struct.unpack_from("<Q", payload, 16 + 44)[0]
        messages.append((args[0], operation, payload))
        return result_factory(args[0], operation, request_id)

    link._run = run
    return link, messages


def queued_result(flag, operation, request_id):
    if flag == "--arsenal":
        return {"namespace": "a" * 64, "request_id": request_id, "build_id": "build", "state": 3,
                "outcome": 0, "flags": 26, "mods_after": 1 << 8, "operations_applied": 1}
    return {"namespace": "a" * 64, "request_id": request_id, "build_id": "build", "state": 3,
            "outcome": 0, "flags": 106, "owns_crucible": 1, "native_crucible": 1,
            "owns_hammer": 1, "native_hammer": 1, "hammer_tier": 1,
            "native_hammer_perks": 0, "native_state_known": 3, "operations_applied": 1}


def test_special_tier2_requires_terminal_projection_not_native_perks():
    def response(_flag, _operation, request_id):
        return {**queued_result("--special", 37, request_id), "hammer_tier": 2,
                "flags": 106 | (1 << 14), "native_hammer_perks": 0}

    link, messages = link_with(response)
    assert link.ensure_progressive_special_weapon(3)["native_hammer_perks"] == 0
    assert [(flag, op) for flag, op, _ in messages] == [("--special", 37), ("--special", 40)]

    def perks_only(_flag, _operation, request_id):
        return {**response(_flag, _operation, request_id), "flags": 106,
                "native_hammer_perks": 2, "native_state_known": 7}

    link, _ = link_with(perks_only)
    try:
        link.ensure_progressive_special_weapon(3)
    except WeaponPointsBlocked as exc:
        assert "Native Special ownership failed" in str(exc)
    else:
        raise AssertionError("native perks alone certified tier2")

    link, _ = link_with(perks_only)
    assert link.ensure_progressive_special_weapon(2)["hammer_tier"] == 2


def test_special_tier2_noop_replay_keeps_projection_contract():
    def replay(_flag, _operation, request_id):
        return {**queued_result("--special", 37, request_id), "hammer_tier": 2,
                "flags": 98 | (1 << 14), "outcome": 1,
                "native_hammer_perks": 0, "operations_applied": 0}

    link, messages = link_with(replay)
    assert link.ensure_progressive_special_weapon(3)["outcome"] == 1
    assert link.ensure_progressive_special_weapon(3)["outcome"] == 1
    assert [op for _, op, _ in messages] == [37, 40, 37, 40]


def test_hook_and_special_use_typed_submit_and_release():
    link, messages = link_with(queued_result)
    link.ensure_meat_hook()
    link.ensure_progressive_special_weapon(2)
    assert [(flag, operation) for flag, operation, _ in messages] == [
        ("--arsenal", 29), ("--arsenal", 32), ("--special", 37), ("--special", 40),
    ]
    assert struct.unpack_from("<Q", messages[0][2], 16)[0] == 16384
    assert struct.unpack_from("<Q", messages[2][2], 16)[0] == 65536


def test_mastery_is_commandless_and_uses_native_bit_order():
    assert list(MASTERY_ITEM_BITS.values()) == [1 << i for i in range(13)]
    definitions = {item_id: {"type": "perk", "perk": "authored/mastery"} for item_id in MASTERY_ITEM_BITS}
    assert all(not compile_item_delivery_plan(item_id, definitions).commands for item_id in definitions)

    mask = MASTERY_ITEM_BITS[7770072] | MASTERY_ITEM_BITS[7770084]
    def confirmed(_flag, _operation, request_id):
        return {"namespace": "a" * 64, "request_id": request_id, "build_id": "build",
                "state": 3, "outcome": 2, "flags": 27 | 32, "masteries_ap_after": mask}

    link, messages = link_with(confirmed)
    link.ensure_masteries(mask)
    assert [(flag, operation) for flag, operation, _ in messages] == [
        ("--arsenal", 29), ("--arsenal", 32),
    ]
    assert struct.unpack_from("<IIII", messages[0][2], 16 + 72 + 65) == (4, 0, 0, mask)


def test_normal_runes_register_cumulatively_without_slot_changes():
    mask = (1 << 2) | (1 << 7)

    def confirmed(_flag, _operation, request_id):
        return {"namespace": "a" * 64, "request_id": request_id, "build_id": "build",
                "state": 3, "kind": 1, "outcome": 0, "flags": 11,
                "owned_normal_after": mask, "selected_slots_before": [-1, 0, -1],
                "selected_slots_after": [-1, 0, -1]}

    link, messages = link_with(confirmed)
    link.ensure_normal_runes(mask)
    assert [(flag, operation) for flag, operation, _ in messages] == [
        ("--runes", 33), ("--runes", 36),
    ]
    assert struct.unpack_from("<IIIBb", messages[0][2], 16 + 72 + 65) == (1, mask, 0, 0, -1)


def test_automap_publishes_exact_checked_bits_without_native_mutation():
    def result(flag, operation, request_id):
        return {"namespace": "a" * 64, "request_id": request_id, "build_id": "build", "outcome": 0,
                "known": 1, "revision": 99, "pid": 42, "process_created": 44,
                "lifecycle_generation": 7, "native_fault": 0}

    link, messages = link_with(result)
    link.publish_checked_locations({7770001, 7770065, 8888888}, 99)
    assert [(flag, operation) for flag, operation, _ in messages] == [("--automap", 45)]
    payload = messages[0][2]
    body = 16 + 72 + 65
    kind, known, revision, *bits = struct.unpack_from("<IIQ8Q", payload, body)
    assert (kind, known, revision) == (1, 1, 99)
    assert bits[0] == 1 << 1
    assert bits[1] == 1 << 1
    assert sum(bits) == 4
