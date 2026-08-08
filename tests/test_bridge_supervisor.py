import textwrap
import time

from launcher_core import LaunchWorkflow, release_identity
from launcher_supervisor import BridgeState, BridgeSupervisor


def _wait(supervisor, states, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if supervisor.state in states:
            return
        time.sleep(0.02)
    raise AssertionError(f"supervisor stayed in {supervisor.state}")


def test_supervised_connected_event_installs_and_stop_redacts_password(tmp_path):
    bridge = tmp_path / "fake_bridge.py"
    identity = release_identity()
    bridge.write_text(textwrap.dedent(f"""
        import json, os, time
        def event(kind, **payload):
            print("AP_EVENT " + json.dumps({{"type": kind, **payload}}), flush=True)
        event("client_started")
        event("connecting")
        print("secret=" + os.environ.get("DOOM_AP_PASSWORD", ""), flush=True)
        event("connected", seed_name="supervised", team=0, slot=1,
              slot_data={{"randomize_dash": False,
                         "bridge_protocol": {identity['bridge_protocol_version']!r},
                         "content_revision": {identity['content_revision']!r}}},
              missing_locations=[], checked_locations=[])
        while True:
            time.sleep(1)
    """))
    supervisor = BridgeSupervisor(
        client=bridge,
        workflow=LaunchWorkflow(),
        install_root=tmp_path / "installed",
        profile_id="connected-test",
    )
    supervisor.start(endpoint="localhost:38281", player="Doomguy", password="top-secret")
    _wait(supervisor, {BridgeState.CONNECTED})
    assert supervisor.install_record is not None
    assert supervisor.last_snapshot is not None
    assert supervisor.install_record.state == "active"
    assert supervisor.last_snapshot.seed_name == "supervised"
    _wait(supervisor, {BridgeState.CONNECTED})
    assert all("top-secret" not in line for line in supervisor.logs)
    supervisor.stop()
    assert supervisor.state is BridgeState.STOPPED


def test_unexpected_bridge_exit_becomes_failed(tmp_path):
    bridge = tmp_path / "crash_bridge.py"
    bridge.write_text('print("AP_EVENT {\\"type\\":\\"client_started\\"}", flush=True)\nraise SystemExit(7)\n')
    supervisor = BridgeSupervisor(
        client=bridge,
        workflow=LaunchWorkflow(),
        install_root=tmp_path / "installed",
        profile_id="crash-test",
    )
    supervisor.start(endpoint="localhost:38281", player="Doomguy")
    _wait(supervisor, {BridgeState.FAILED})
    assert supervisor.last_error is not None
    assert supervisor.last_error["code"] == "bridge_exited"
