import asyncio
import os
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from doom_eap.runtime import ledger_schema
from doom_eap.runtime.delivery import RoomDelivery
from doom_eap.runtime.ledger import CommandState, Ledger, NetworkReceipt, Resolution, ResolutionAction
from tests.delivery_scenario_support import (
    Scenario,
    environment,
    finish_v2_script,
    history,
    ledger_snapshot,
    runtime_bridge,
)
from tests.test_delivery_ledger import create_v1_database


@pytest.mark.parametrize("point", ["ingestion:after", "result_commit:before", "ack:before"])
def test_interrupted_checkpoint_and_delivery_recover_once(tmp_path, point):
    peer = Scenario(tmp_path / "peers")
    build = Path(os.environ.get("SC_BUILD_DIR", Path(__file__).resolve().parents[3] / "Sentinel-Core/build"))
    native = build / "bin/sentinel_save_storage_tests.exe"
    assert native.is_file(), "Build sentinel_save_storage_tests before running the recovery pillar"
    try:
        with tempfile.TemporaryDirectory(prefix="cp-", dir=build.parent / ".tmp") as scratch:
            checkpoint = subprocess.run([str(native), "--previous-checkpoint"], env=environment(Path(scratch)),
                                        capture_output=True, timeout=45)
        assert checkpoint.returncode == 0, (checkpoint.stdout + checkpoint.stderr).decode(errors="replace")
        peer.start(point)
        if point == "ingestion:after":
            peer.begin_packet([history()])
        else:
            peer.collect([history()])
            peer.action("plan")
            peer.action("prepare")
            peer.action("send")
            if point == "result_commit:before":
                peer.bridge.send({"action": "result"})
            else:
                peer.action("result")
                peer.bridge.send({"action": "ack"})
        peer.bridge.expect(point)

        peer.kill_bridge()
        peer.start()
        finish_v2_script(peer)

        room, commands, _ = ledger_snapshot(peer)
        assert room.receipt_boundary == 1
        assert len(commands) == 1 and commands[0].state is CommandState.CONFIRMED
        assert peer.effects() == [commands[0].plan.command_id]
    finally:
        peer.close()


@pytest.mark.parametrize("after_schema", [False, True])
def test_interrupted_first_ledger_creation_recovers(tmp_path, after_schema):
    path = tmp_path / "delivery.sqlite3"
    script = f"""
import asyncio, os
from pathlib import Path
from doom_eap.runtime import ledger_schema
from doom_eap.runtime.ledger import Ledger
original = ledger_schema._migrate
def interrupted(*args):
    if {after_schema!r}:
        original(*args)
    os._exit(23)
ledger_schema._migrate = interrupted
asyncio.run(Ledger.create(Path({str(path)!r})))
"""
    result = subprocess.run([os.sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
                            env=environment(tmp_path), capture_output=True, timeout=30)
    assert result.returncode == 23, result.stderr.decode(errors="replace")
    assert not path.exists() and not path.with_suffix(".expected").exists()

    async def resume():
        value, definitions, policies = runtime_bridge(tmp_path)
        owner = RoomDelivery(value, {"client_state_file": str(tmp_path / "state.json")}, definitions, policies,
                             lambda *args, **kwargs: None)
        try:
            await owner._open()
            assert (await owner.ledger.room(owner.key)).receipt_boundary == 0
            assert path.with_suffix(".expected").exists()
        finally:
            await owner.close()
        reopened = await Ledger.open(path)
        await reopened.close()

    asyncio.run(resume())


@pytest.mark.parametrize("version", [1, 2])
def test_old_ledger_reaches_player_resolution(tmp_path, monkeypatch, version):
    async def resume():
        path = tmp_path / "delivery.sqlite3"
        key, command = await create_v1_database(path, monkeypatch)
        if version == 2:
            with monkeypatch.context() as patch:
                patch.delitem(ledger_schema._MIGRATIONS, 3)
                intermediate = await Ledger.open(path)
                await intermediate.close()
        value, definitions, policies = runtime_bridge(tmp_path)
        value.room_seed_name, value.team, value.slot = key.seed, key.team, key.slot
        value._connected_slot_data["native_generation_fingerprint"] = key.generation_fp
        value.items_received = [NetworkReceipt(100, -1, 2, 1)]
        owner = RoomDelivery(value, {"client_state_file": str(tmp_path / "state.json")}, definitions, policies,
                             lambda *args, **kwargs: None)
        try:
            await owner._ingest_existing_owner()
            coordinator = owner.coordinator
            coordinator.transport = SimpleNamespace()
            coordinator.context = replace(await owner.ledger.saved_context(key, command.context_id),
                                          binding=owner.binding, context_id=str(uuid4()))
            await owner.ledger.context(owner.binding, coordinator.context, ())
            await coordinator.recover(command.command_id)
            row = await owner.ledger.command(key, command.command_id)
            assert row.state is CommandState.EFFECT_UNKNOWN and row.reason == "legacy_reference_missing"
            assert await coordinator.ack(command.command_id) is None
            await coordinator.resolve(command.command_id, row.revision, ResolutionAction.TRY_AGAIN)
            row = await owner.ledger.command(key, command.command_id)
            assert row.resolution is Resolution.REPLACED
            replacement = await owner.ledger.command(key, row.replacement_id)
            assert replacement.plan.payload == command.payload
            assert (await owner.ledger.receipts(key, 0, 10))[0].item == 100
            assert list(tmp_path.glob("*.backup"))
        finally:
            await owner.close()

    asyncio.run(resume())
