import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from doom_eap.runtime import delivery
from doom_eap.runtime.delivery import RoomDelivery
from doom_eap.runtime.ledger import CommandState, NetworkReceipt
from doom_eap.runtime.native_transport import NativeTransport
from tests.delivery_scenario_support import FIXTURES, Scenario, environment, runtime_bridge
from tests.generation_support import ROOT
from tools.maps.ap_map_generator import find_entity_block_bounds


def assert_native_fortress_transaction(compiled_room, compiler, tmp_path):
    manifest, package, _ = compiled_room("base")
    assert "SG_LOWER_EAST" in manifest.options["campaign"]["fortress"]["active_spend_group_ids"]
    member = compiler.payload_manifest["maps"]["hub"]["target_member"]
    blocks = []
    for index, source in enumerate((ROOT / "packaging/room_resources/v0.8.0/base_mod.zip", package)):
        compressed, decoded = tmp_path / f"hub-{index}.entities", tmp_path / f"hub-{index}.txt"
        with ZipFile(source) as archive:
            compressed.write_bytes(archive.read(member))
        result = subprocess.run([str(compiler.decompressor), "--decompress", str(compressed), str(decoded)],
                                capture_output=True, timeout=60)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        text = decoded.read_text(encoding="utf-8")
        bounds = find_entity_block_bounds(text, "sentinel_battery_room_interact_hub_2_battery_station_4")
        assert bounds is not None
        blocks.append(text[bounds[0]:bounds[1] + 1])
    assert blocks[0] == blocks[1]


@pytest.mark.parametrize("point", ["ammo_reservation:after", "ammo_total:before", "ammo_confirmation:after"])
def test_ammo_refill_never_pays_twice_after_restart(tmp_path, point):
    peer = Scenario(tmp_path)
    try:
        peer.start(point)
        peer.bridge.send({"action": "ammo"})
        peer.bridge.expect(point)
        peer.kill_bridge()

        operation, = peer.start()["ammo_operations"]
        status = peer.action("ammo_recover")
        if status["ammo_operations"][0]["state"] == "unknown":
            status = peer.action("ammo_retry", operation_id=operation["id"],
                                 revision=status["ammo_operations"][0]["revision"])
        peer.action("ammo_deliver")
        peer.kill_bridge()
        peer.start()
        peer.action("ammo_recover")
        peer.action("ammo_deliver")

        assert peer.effects() == [f"{operation['id']}:0"]
        values = json.loads((peer.root / "ap-storage.json").read_text())
        assert values["doom_eap:DeliveryRoomA:ammo_refill_consumed"] == 1
        assert sum("ammo/ops/" in key for key in values) == 1
    finally:
        peer.close()


def test_fortress_spent_batteries_stay_spent_after_restart(tmp_path, monkeypatch, compiled_room, compiler):
    assert_native_fortress_transaction(compiled_room, compiler, tmp_path)
    peer = Scenario(tmp_path / "peers")
    value, definitions, policies = runtime_bridge(tmp_path)
    value.items_received = [NetworkReceipt(7770142, 7770001, 1, 0)]
    value.receipt_session.observe_packet(0, 1, 1, 1)
    env = environment(peer.root)
    scope = {"pid": 123, "process_created": 123, "instance_id": [0] * 15 + [2], "generation": 1}
    env["PILLAR_CORE_SCOPE"] = json.dumps(scope)
    monkeypatch.setattr(delivery, "NativeTransport", lambda command, scope, **kw: NativeTransport(
        (sys.executable, str(FIXTURES / "fake_delivery_core.py"), "--wire", str(peer.root)), scope, env=env, timeout=30))

    async def play():
        for restart in range(2):
            owner = RoomDelivery(value, {"client_state_file": str(tmp_path / "state.json")}, definitions, policies,
                                 lambda *args, **kwargs: None)
            async def balance():
                pass
            owner._balance = balance
            try:
                await owner.reconcile(SimpleNamespace(epoch=1))
                commands = await owner._rows()
                assert (len(commands), commands[0].state) == (1, CommandState.CONFIRMED)
            finally:
                await owner.close()
            saved = json.loads((peer.root / "wire-core.json").read_text())
            assert saved["battery_gained"] == 2
            if restart == 0:
                assert saved["batteries"] == 2
                saved["batteries"] -= 2
                (peer.root / "wire-core.json").write_text(json.dumps(saved), encoding="utf-8")
            else:
                assert saved["batteries"] == 0
        effects = [json.loads(line) for line in (peer.root / "wire-effects.jsonl").read_text().splitlines()]
        assert [(row["command"], row["payload"]["amount"]) for row in effects] == [(53, 2)]

    try:
        asyncio.run(play())
    finally:
        peer.close()
