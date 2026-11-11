import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from doom_eap.runtime.ledger import CommandState
from tests.delivery_scenario_support import Scenario, environment, finish_v2_script, history, ledger_snapshot


@pytest.mark.parametrize("point", ["ingestion:after", "result_commit:before", "ack:before"])
def test_interrupted_checkpoint_and_delivery_recover_once(tmp_path, point):
    peer = Scenario(tmp_path / "peers")
    build = Path(os.environ.get("SC_BUILD_DIR", Path(__file__).resolve().parents[3] / "Sentinel-Core/build"))
    native = build / "bin/sentinel_save_storage_tests.exe"
    assert native.is_file(), "Build sentinel_save_storage_tests before running the recovery pillar"
    try:
        with tempfile.TemporaryDirectory(prefix="cp-") as scratch:
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
