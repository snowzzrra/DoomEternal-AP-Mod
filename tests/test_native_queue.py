"""Actual Windows file ownership and scheduler with a deterministic transport port."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def native_queue_executable(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("native-queue-build")
    compiler = shutil.which("cl.exe")
    if compiler is None:
        pytest.skip("native queue harness requires the supported MSVC environment")
    root = Path(__file__).resolve().parents[1]
    executable = tmp_path / "queue_contract.exe"
    result = subprocess.run([
        compiler, "/nologo", "/std:c++17", "/EHsc", "/DNOMINMAX", f"/I{root}",
        str(root / "tests/native_queue_contract.cpp"), str(root / "native/client/command_queue.cpp"),
        f"/Fe{executable}", f"/Fo{tmp_path}\\",
    ], cwd=tmp_path, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


def test_native_queue_contract(tmp_path, native_queue_executable):
    executable = native_queue_executable
    result = subprocess.run([str(executable)], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_auto_arm_cannot_execute_refill_before_debit_confirmation(tmp_path, native_queue_executable):
    import asyncio
    from test_ammo_contract import setup_ammo
    from doom_eap.runtime.ammo_refill import AmmoCommandScope
    from doom_eap.contracts.command_publication import queue_session_namespace

    queue = tmp_path / "base/ap_queue"
    queue.mkdir(parents=True)
    messages = []
    async def send(packets):
        messages.extend(packets)

    ammo, spool, gate, _ = setup_ammo(queue, send)
    assert asyncio.run(ammo.request(AmmoCommandScope("room-A", "map", "slot", True), (None, {})))
    assert ammo.pending and len(messages) == 1
    def drain():
        result = subprocess.run([str(native_queue_executable), queue_session_namespace("room-A")],
                                cwd=tmp_path, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.splitlines()

    assert not gate["enabled"]
    assert spool.publish("give health", coalesce_key="background", state_key="room-A", arm_rpc=False).accepted
    assert not (tmp_path / "base/ap_rpc_enabled").exists()
    assert drain() == ["give health"]
    assert (tmp_path / "base/ap_rpc_enabled").exists()
    ammo.consume_consumed(1)
    assert drain() == ["give ammo", "judgementMeter_Set 3"]
    assert drain() == []
