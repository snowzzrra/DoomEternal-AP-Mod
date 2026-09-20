import struct

from doom_eap.runtime.weapon_points import SentinelWeaponPoints


SCOPE = {
    "availability": "enabled",
    "lifecycle": "active",
    "process_created": 44,
    "instance_id": "11" * 16,
    "lifecycle_generation": 7,
    "build_id": "build",
}


def link_with(result_factory):
    link = SentinelWeaponPoints("probe", 42, "a" * 64)
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


def test_hook_and_special_use_typed_submit_and_release():
    link, messages = link_with(queued_result)
    link.ensure_meat_hook()
    link.ensure_progressive_special_weapon(2)
    assert [(flag, operation) for flag, operation, _ in messages] == [
        ("--arsenal", 29), ("--arsenal", 32), ("--special", 37), ("--special", 40),
    ]
    assert struct.unpack_from("<Q", messages[0][2], 16)[0] == 16384
    assert struct.unpack_from("<Q", messages[2][2], 16)[0] == 65536


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
