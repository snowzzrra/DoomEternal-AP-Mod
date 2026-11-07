import pytest

from doom_eap.runtime.inventory import SentinelWeaponPoints, WeaponPointsBlocked


def test_native_worker_request_and_close_work_without_asyncio_runner(tmp_path, monkeypatch):
    import asyncio

    import doom_eap.runtime.inventory as module

    monkeypatch.delattr(asyncio, "Runner", raising=False)
    closed = []
    class Transport:
        def __init__(self, *args, **kwargs):
            pass
        async def close(self):
            closed.append(True)
    monkeypatch.setattr(module, "NativeTransport", Transport)
    link = SentinelWeaponPoints(tmp_path / "probe", 71, "a" * 64)
    async def request(*args):
        return {"value": 42}
    link._request = request
    assert link.request(25) == {"value": 42}
    link.close()
    assert closed == [True]


def test_probe_diagnostics_preserve_first_error_and_aggregate_sampling(tmp_path, monkeypatch):
    import json

    import doom_eap.runtime.inventory as module
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








@pytest.mark.parametrize("known,owned", [(0, 0), (2, 0), (2, 2), (1 << 63, 1 << 63)])
def test_permanent_perk_masks_preserve_unknown_missing_owned_and_high_bit(known, owned):
    link = SentinelWeaponPoints.__new__(SentinelWeaponPoints)
    reply = dict(abi_version=8, inventory_abi=8, kind=0, outcome=1, flags=3,
                 ice_bomb_after=255, dash_after=255, slayer_key_after=255,
                 frag_grenade_after=255, flame_belch_after=0, blood_punch_after=1,
                 perks_known=known, perks_owned=owned)
    link._run = lambda *args: dict(process_created='1', instance_id='a' * 32, build_id='test', lifecycle_generation=1, current_map='map')
    link.request = lambda *args, **kwargs: reply
    result = link.observe_inventory()
    assert result["perks_known"] == known and result["perks_owned"] == owned
    reply.update(perks_known=0, perks_owned=2)
    with pytest.raises(WeaponPointsBlocked, match="malformed"):
        link.observe_inventory()
