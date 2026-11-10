import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from doom_eap.launcher.launcher_session import APSessionOwner
from doom_eap.runtime.ledger import AmmoTotals, Authority, Ledger, NetworkReceipt, RoomKey
from tests.delivery_scenario_support import FIXTURES, Child, Scenario, prepare
from tests.room_fixture import current_room_fixture


@pytest.mark.parametrize("other", [
    RoomKey("other-room", 0, 1, "a" * 64),
    RoomKey("room", 1, 1, "a" * 64),
    RoomKey("room", 0, 2, "a" * 64),
    RoomKey("room", 0, 1, "b" * 64),
])
def test_rooms_and_profiles_keep_saves_receipts_and_spending_separate(tmp_path, other):
    first = RoomKey("room", 0, 1, "a" * 64)
    config = {"save_games_dir": str(tmp_path / "saves"), "steam_remote_dir": str(tmp_path / "remote")}
    owner = APSessionOwner(tmp_path / "client", tmp_path / "data", tmp_path / "state")
    paths = []
    for key in (first, other):
        data = current_room_fixture()
        data["native_generation_fingerprint"] = key.generation_fp
        snapshot = SimpleNamespace(seed_name=key.seed, team=key.team, slot=key.slot, slot_data=data)
        inspection = owner.inspect_campaign(snapshot, config)
        save = Path(inspection["paths"]["steam_native_autosave"]["path"]) / "game.details"
        paths.append(save)
    paths[0].parent.mkdir(parents=True)
    paths[0].write_bytes(b"first room checkpoint")
    assert not paths[1].exists()
    paths[1].parent.mkdir(parents=True)
    paths[1].write_bytes(b"second room checkpoint")

    async def play():
        path = tmp_path / "delivery.sqlite3"
        ledger = await Ledger.create(path)
        try:
            for index, key in enumerate((first, other)):
                binding = await ledger.bind(key, str(index), compatibility_revision=1, authority=Authority.LEDGER)
                await ledger.ingest(binding, 0, (NetworkReceipt(7770900 + index, 1, 1, 0),))
                await ledger.ammo(binding, (), (), AmmoTotals(0, 3, index, 0, 0, b"server"))
            profile_id = ledger.profile_id
        finally:
            await ledger.close()

        ledger = await Ledger.open(path)
        profile = await Ledger.create(tmp_path / "other-profile.sqlite3")
        try:
            assert ledger.profile_id == profile_id != profile.profile_id
            for index, key in enumerate((first, other)):
                assert [row.item for row in await ledger.receipts(key, 0, 10)] == [7770900 + index]
                assert (await ledger.ammo_totals(key)).consumed == index
                assert (await profile.room(key), await profile.receipts(key, 0, 10)) == (None, ())
            assert [save.read_bytes() for save in paths] == [b"first room checkpoint", b"second room checkpoint"]
        finally:
            await profile.close()
            await ledger.close()

    asyncio.run(play())


def test_second_profile_cannot_spend_first_profiles_charges(tmp_path):
    peer = Scenario(tmp_path)
    second = None
    try:
        peer.start()
        first = peer.action("ammo")
        root = peer.root / "second-profile"
        prepare(root)
        second = Child([sys.executable, "-B", str(FIXTURES / "delivery_v2_worker.py"),
                        f"ws://127.0.0.1:{peer.port}", "", f"ws://127.0.0.1:{peer.core_port}"], root)
        second.expect("ready")
        peer.ap.expect("connected")

        second.send({"action": "ammo", "received": 8})
        status = second.expect("done")

        assert status["ammo_operations"] == []
        assert (status["ammo_consumed"], status["ammo_discarded"]) == (0, 0)
        pending = peer.action("ammo")
        assert pending["ammo_consumed"] == first["ammo_consumed"] == 0
        assert pending["ammo_reserved"] == 1
        command, = peer.action("ammo_deliver")["ammo_commands"]
        peer.action("resolve", command_id=command["id"], revision=command["revision"], resolution="mark_as_received")
        assert peer.action("ammo_recover")["ammo_consumed"] == 1
    finally:
        if second is not None:
            second.close()
        peer.close()
