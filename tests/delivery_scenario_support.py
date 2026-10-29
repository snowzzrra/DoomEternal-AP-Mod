"""Own subprocess lifetimes and delivery barriers; production writes remain delegated unchanged."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"
MSVC_REASON = "delivery queue scenarios require Windows and MSVC (select with -m integration)"


class SafetyFailure(AssertionError):
    """A desired delivery guarantee failed, independently of harness failures."""


def require(condition, message):
    if not condition:
        raise SafetyFailure(message)


def environment(root):
    result = dict(os.environ)
    result.update({
        "TEMP": str(root), "TMP": str(root), "PYTHONDONTWRITEBYTECODE": "1",
        "DOOM_AP_APPLICATION_DIR": str(root), "DOOM_AP_CONFIG_FILE": str(root / "config.json"),
        "DOOM_AP_LAUNCHER_EVENTS": "1", "HEADLESS": "1", "CI": "1",
        "LOCALAPPDATA": str(root), "APPDATA": str(root), "XDG_STATE_HOME": str(root),
        "PYTHONPATH": os.pathsep.join((str(ROOT / "packaging/standalone_runtime"), str(ROOT),
                                     str(ROOT.parent / "Archipelago"))),
    })
    result.pop("ARCHIPELAGO_SOURCE", None)
    return result


def prepare(root):
    remote = root / "userdata/1/782330/remote"
    remote.mkdir(parents=True)
    (root / "base/ap_queue").mkdir(parents=True)
    (root / "saves").mkdir()
    (root / "host.yaml").write_text("{}\n", encoding="utf-8")
    (root / "config.json").write_text(json.dumps({
        "doom_base_dir": str(root / "base"), "save_games_dir": str(root / "saves"),
        "steam_remote_dir": str(remote), "steam_id3": 1,
        "client_state_file": str(root / "state.json"), "bridge_log_dir": str(root / "logs"),
        "item_notifications": False,
    }), encoding="utf-8")


class Child:
    def __init__(self, command, root):
        self.events = queue.Queue()
        self.output = []
        self.launcher_events = []
        self.process = subprocess.Popen(
            command, cwd=root, env=environment(root), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        )
        self.log_path = root / f"peer-{self.process.pid}.log"
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        with self.log_path.open("w", encoding="utf-8", buffering=1) as stream:
            for line in self.process.stdout:
                stream.write(line)
                self.output.append(line.rstrip())
                if line.startswith("DELIVERY\t"):
                    self.events.put(json.loads(line[9:]))
                elif line.startswith("AP_EVENT "):
                    self.launcher_events.append(json.loads(line[9:]))
        self.events.put({"event": "eof"})

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def next(self):
        try:
            event = self.events.get(timeout=8)
        except queue.Empty as error:
            raise RuntimeError("Barrier timed out:\n" + "\n".join(self.output)) from error
        if event["event"] == "eof":
            raise RuntimeError("Child exited before barrier:\n" + "\n".join(self.output))
        return event

    def expect(self, name):
        event = self.next()
        if event["event"] != name:
            raise RuntimeError(f"Expected {name}, got {event}\n" + "\n".join(self.output))
        return event

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        self.reader.join(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()


class Scenario:
    def __init__(self, root, executable=None):
        prepare(root)
        self.root = root
        self.executable = executable
        self.stack = ExitStack()
        self.ap = self.child("fake_ap_server.py", str(root))
        try:
            self.port = self.ap.expect("ready")["port"]
        except RuntimeError:
            self.stack.close()
            raise
        self.bridge = None
        self.status = None

    def child(self, script, *arguments):
        child = Child([sys.executable, "-B", str(FIXTURES / script), *arguments], self.root)
        self.stack.callback(child.close)
        return child

    def start(self, stop=""):
        self.bridge = self.child("delivery_bridge_worker.py", f"ws://127.0.0.1:{self.port}", stop)
        self.status = self.bridge.expect("ready")
        self.ap.expect("connected")
        return self.status

    def kill_bridge(self):
        # Popen.kill uses TerminateProcess on Windows; no child unwinding can commit state.
        self.bridge.close()

    def begin_packet(self, packets):
        self.bridge.send({"action": "collect"})
        self.bridge.expect("awaiting_packet")
        self.ap.send({"action": "send", "packets": packets})
        self.ap.expect("sent")

    def collect(self, packets):
        self.begin_packet(packets)
        self.status = self.bridge.expect("done")
        return self.status

    def action(self, action):
        self.bridge.send({"action": action})
        self.status = self.bridge.expect("done")
        return self.status

    def release(self):
        self.bridge.send({"action": "collect"})
        self.bridge.expect("awaiting_packet")
        self.ap.send({"action": "release", "packets": [tail(self.status["received"])]})
        self.ap.expect("sent")
        self.status = self.bridge.expect("done")
        return self.status

    def namespace(self):
        return (self.root / "base/ap_queue/active_session_namespace").read_text().strip()

    def effects(self):
        path = self.root / "effects.jsonl"
        rows = path.read_text().splitlines() if path.exists() else []
        return [json.loads(line)["command"] for line in rows]

    def native(self, stop=""):
        with ExitStack() as stack:
            probe = Child([sys.executable, "-B", str(FIXTURES / "fake_sentinel_probe.py"), str(self.root)], self.root)
            stack.callback(probe.close)
            probe.expect("ready")
            native = Child([str(self.executable), "--scenario", self.namespace()], self.root)
            stack.callback(native.close)
            result = self.drive_native(native, probe, stop)
            self.checkpoint(stop or "native-done")
            return result

    def drive_native(self, native, probe, stop):
        while True:
            event = native.next()
            name = event["event"]
            if name == stop:
                return name
            if name == "done":
                if stop:
                    raise RuntimeError(f"Native queue never reached {stop}")
                return name
            if name == "execute":
                if self.effect(native, probe, event, stop):
                    return stop
            else:
                native.send("continue")

    @staticmethod
    def effect(native, probe, event, stop):
        probe.send(event)
        for name in ("external_effect:before", "external_effect:after"):
            probe.expect(name)
            if name == stop:
                return True
            probe.send("continue")
        probe.expect("result")
        native.process.stdin.write("ok\n")
        native.process.stdin.flush()
        return False

    def close(self):
        self.stack.close()
        self.checkpoint("final")

    def checkpoint(self, name):
        checkpoints = self.root / "checkpoints"
        checkpoints.mkdir(exist_ok=True)
        target = checkpoints / f"{len(list(checkpoints.iterdir())):02d}-{name.replace(':', '-')}"
        target.mkdir()
        shutil.copytree(self.root / "base/ap_queue", target / "queue")
        for name in ("state.json", "effects.jsonl", "ap-storage.json"):
            source = self.root / name
            if source.exists():
                shutil.copyfile(source, target / name)


def history(item=7770025):
    return {"cmd": "ReceivedItems", "index": 0, "items": [[item, 7770100, 1, 0]]}


def tail(index):
    return {"cmd": "ReceivedItems", "index": index, "items": []}


def balance_packets(state_key, consumed=0):
    return [{"cmd": "Retrieved", "keys": {
        f"doom_eap:{state_key}:ammo_refill_consumed": consumed,
        f"doom_eap:{state_key}:ammo_refill_discarded": 0,
    }}, tail(1)]
