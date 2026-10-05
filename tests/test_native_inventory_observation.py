import pytest

from doom_eap.runtime.weapon_points import SentinelWeaponPoints, WeaponPointsBlocked


def test_probe_diagnostics_preserve_first_error_and_aggregate_sampling(tmp_path, monkeypatch):
    import json
    import doom_eap.runtime.weapon_points as module
    now = [1.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    emitted = []
    link = SentinelWeaponPoints(tmp_path / "probe.exe", 71, "a" * 64, emitted.append)
    def response(index):
        return json.dumps(dict(result="refused", process_created="101", sequence=index, native=dict(
            event_sequence=str(index), callback_sequence=str(index), callback_at_ms=str(index * 10),
            context_sampled_at_ms=str(index * 10), history_oldest=str(index), history_overwritten=str(index),
            event_gap_count=str(index), context_generation="2", game_state=2,
            events=[dict(sequence=str(index), at_ms=str(index * 10))])))
    row = dict(stage="native_probe", operation="save-admission", returncode=2,
        stdout=response(1), error="missing provider")
    link._diagnose(row)
    for i in range(2, 40):
        row["stdout"] = response(i)
        link._diagnose(row)
    assert len(emitted) == 1
    state = link._diagnostic_signatures[("native_probe", "save-admission")]
    assert state["count"] == 39 and state["first_error"]["error"] == "missing provider"
    now[0] = 62.0
    link._diagnose(row)
    assert emitted[-1]["summary"] == "unchanged" and emitted[-1]["count"] == 40
    link._diagnose({**row, "returncode": 0, "error": None, "stdout": '{"result":"ok"}'})
    assert link._diagnostic_signatures[("native_probe", "save-admission")]["first_error"]["error"] == "missing provider"


@pytest.mark.parametrize("abi,dash", [(4, 0), (4, 1), (5, 0), (5, 1), (5, 255)])
def test_independent_dash_observation(abi, dash):
    link = SentinelWeaponPoints.__new__(SentinelWeaponPoints)
    reply = {"inventory_abi": abi, "kind": 0, "outcome": 1, "flags": 3,
             "equipment_after": 0xffffffff, "ice_bomb_after": 255, "dash_after": dash}
    link._execute_typed = lambda *args: reply
    result = link.observe_inventory()
    assert result["dash_after"] == (dash if abi == 5 else 255)
    assert result["equipment_after"] == 0xffffffff
    reply["inventory_abi"] = 5
    reply["dash_after"] = 2
    with pytest.raises(WeaponPointsBlocked):
        link.observe_inventory()


@pytest.mark.parametrize("abi,key", [(4, 1), (5, 0), (5, 1), (6, 0), (6, 1), (6, 255)])
def test_independent_slayer_key_observation(abi, key):
    link = SentinelWeaponPoints.__new__(SentinelWeaponPoints)
    reply = {"inventory_abi": abi, "kind": 0, "outcome": 1, "flags": 3,
             "ice_bomb_after": 255, "dash_after": 1, "slayer_key_after": key}
    link._execute_typed = lambda *args: reply
    assert link.observe_inventory()["slayer_key_after"] == (key if abi == 6 else 255)
    reply.update(inventory_abi=6, slayer_key_after=2)
    with pytest.raises(WeaponPointsBlocked):
        link.observe_inventory()
    reply.update(slayer_key_after=0, flags=1)
    with pytest.raises(WeaponPointsBlocked):
        link.observe_inventory()
