import textwrap
import threading
import time

from launcher_core import release_identity
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
              slot_data={{"randomize_chainsaw": False,
                         "randomize_dash": False,
                         "randomize_first_battery": False,
                         "bridge_protocol": {identity['bridge_protocol_version']!r},
                         "content_revision": {identity['content_revision']!r}}},
              missing_locations=[], checked_locations=[])
        while True:
            time.sleep(1)
    """))
    events = []
    log_lines = []
    supervisor = BridgeSupervisor(
        entrypoint=bridge,
        application_dir=tmp_path,
        config_path=tmp_path / "config.yaml",
        profile_id="connected-test",
        event_sink=events.append,
        log_sink=log_lines.append,
    )
    supervisor.start(endpoint="localhost:38281", player="Doomguy", password="top-secret")
    _wait(supervisor, {BridgeState.CONNECTED})
    connected_events = [event for event in events if event["type"] == "connected"]
    assert connected_events
    assert connected_events[-1]["seed_name"] == "supervised"
    _wait(supervisor, {BridgeState.CONNECTED})
    assert all("top-secret" not in line for line in log_lines)
    supervisor.stop(timeout=0.05)
    _wait(supervisor, {BridgeState.STOPPED})


def test_unexpected_bridge_exit_becomes_failed(tmp_path):
    bridge = tmp_path / "crash_bridge.py"
    bridge.write_text('print("AP_EVENT {\\"type\\":\\"client_started\\"}", flush=True)\nraise SystemExit(7)\n')
    events = []
    log_lines = []
    supervisor = BridgeSupervisor(
        entrypoint=bridge,
        application_dir=tmp_path,
        config_path=tmp_path / "config.yaml",
        profile_id="crash-test",
        event_sink=events.append,
        log_sink=log_lines.append,
    )
    supervisor.start(endpoint="localhost:38281", player="Doomguy")
    _wait(supervisor, {BridgeState.FAILED})
    assert supervisor.last_error is not None
    assert supervisor.last_error["code"] == "bridge_exited"


def test_stop_returns_promptly_and_escalates_off_caller_thread(tmp_path, monkeypatch):
    class Input:
        def write(self, _text):
            pass

        def flush(self):
            pass

    class Process:
        stdin = Input()
        stdout = None
        stderr = None

        def __init__(self):
            self.done = threading.Event()
            self.returncode = None
            self.terminated = False
            self.killed = False

        def poll(self):
            return self.returncode

        def wait(self):
            self.done.wait()
            return self.returncode

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True
            self.returncode = -9
            self.done.set()

    process = Process()
    monkeypatch.setattr("launcher_supervisor.subprocess.Popen", lambda *_args, **_kwargs: process)
    events = []
    supervisor = BridgeSupervisor(
        entrypoint=tmp_path / "bridge.py",
        application_dir=tmp_path,
        config_path=tmp_path / "config.json",
        profile_id="async-stop-test",
        event_sink=events.append,
        log_sink=lambda _line: None,
    )
    supervisor.start(endpoint="localhost:38281", player="Doomguy")

    started = time.monotonic()
    supervisor.stop(timeout=0.2)
    elapsed = time.monotonic() - started

    assert elapsed < 0.05
    assert supervisor.state is BridgeState.STOPPING
    _wait(supervisor, {BridgeState.STOPPED})
    assert process.terminated
    assert process.killed
    assert [event["type"] for event in events].count("disconnected") == 1
    assert [event["type"] for event in events].count("worker_stopped") == 1
