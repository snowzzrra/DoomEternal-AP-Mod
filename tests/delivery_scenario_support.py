import asyncio
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from doom_eap.runtime.inventory import ReceiptSession, load_policy_registry
from doom_eap.runtime.ledger import CommandState, Ledger, NetworkReceipt, RoomKey
from doom_eap.runtime.observers import CONTEXT_BY_IDENTITY

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
                                     os.environ.get("ARCHIPELAGO_SOURCE", str(ROOT.parent / "Archipelago")))),
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
            event = self.events.get(timeout=30)
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
        self.process.wait(timeout=30)
        self.reader.join(timeout=30)
        assert not self.reader.is_alive(), "Child output reader did not finish"
        self.process.stdin.close()
        self.process.stdout.close()


class Scenario:
    def __init__(self, root, *, backend="v2"):
        if backend != "v2":
            raise ValueError(backend)
        prepare(root)
        self.root = root
        self.backend = backend
        self.stack = ExitStack()
        self.reader_loop = asyncio.new_event_loop()
        self.stack.callback(self.reader_loop.close)
        self.ap = self.child("fake_ap_server.py", str(root))
        self.bridge = None
        self.status = None
        self.core = None
        try:
            self.port = self.ap.expect("ready")["port"]
            self.start_core()
        except RuntimeError:
            self.stack.close()
            raise

    def child(self, script, *arguments):
        child = Child([sys.executable, "-B", str(FIXTURES / script), *arguments], self.root)
        self.stack.callback(child.close)
        return child

    def start(self, stop=""):
        script = "delivery_v2_worker.py"
        arguments = [f"ws://127.0.0.1:{self.port}", stop]
        arguments.append(f"ws://127.0.0.1:{self.core_port}")
        self.bridge = self.child(script, *arguments)
        self.status = self.bridge.expect("ready")
        self.ap.expect("connected")
        return self.status

    def kill_bridge(self):
        # Popen.kill uses TerminateProcess on Windows; no child unwinding can commit state.
        self.bridge.close()

    def start_checks(self):
        self.bridge = self.child("delivery_v2_worker.py", f"ws://127.0.0.1:{self.port}", "--checks")
        self.status = self.bridge.expect("ready")
        self.ap.expect("connected")
        return self.status

    def start_core(self, stop=""):
        self.core = self.child("fake_delivery_core.py", str(self.root), stop)
        ready = self.core.expect("ready")
        self.core_port, self.core_instance = ready["port"], ready["instance"]

    def kill_core(self):
        self.core.close()

    def begin_packet(self, packets):
        self.bridge.send({"action": "collect"})
        self.bridge.expect("awaiting_packet")
        self.ap.send({"action": "send", "packets": packets})
        self.ap.expect("sent")

    def collect(self, packets):
        self.begin_packet(packets)
        self.status = self.bridge.expect("done")
        return self.status

    def action(self, action, **fields):
        self.bridge.send({"action": action, **fields})
        self.status = self.bridge.expect("done")
        return self.status

    def release(self):
        self.bridge.send({"action": "collect"})
        self.bridge.expect("awaiting_packet")
        self.ap.send({"action": "release", "packets": [tail(self.status["received"])]})
        self.ap.expect("sent")
        self.status = self.bridge.expect("done")
        return self.status


    def effects(self):
        path = self.root / "effects.jsonl"
        rows = path.read_text().splitlines() if path.exists() else []
        return [json.loads(line)["command"] for line in rows]




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
        source = self.root / "delivery.sqlite3"
        if source.exists():
            with closing(sqlite3.connect(source)) as database, closing(sqlite3.connect(target / source.name)) as backup:
                database.backup(backup)


def history(item=7770025):
    return {"cmd": "ReceivedItems", "index": 0, "items": [[item, 7770100, 1, 0]]}


def tail(index):
    return {"cmd": "ReceivedItems", "index": index, "items": []}


def balance_packets(state_key, consumed=0):
    return [{"cmd": "Retrieved", "keys": {
        f"doom_eap:{state_key}:ammo_refill_consumed": consumed,
        f"doom_eap:{state_key}:ammo_refill_discarded": 0,
    }}, tail(1)]


def runtime_bridge(tmp_path):
    root = Path(__file__).resolve().parents[1]
    definitions = {int(k): v for k, v in json.loads((root / "data/items.json").read_text()).items()}
    policies = load_policy_registry(root / "data/item_replay_policies.json", definitions)
    session = ReceiptSession()
    value = SimpleNamespace(
        state_key="seed:0:1:fixture",
        room_seed_name="seed",
        team=0,
        slot=1,
        _connected_slot_data={"native_generation_fingerprint": "a" * 64, "runtime": {"special_weapon": "the_crucible"}},
        base_directory=tmp_path,
        session_state={"delivery_backend": "ledger"},
        receipt_session=session,
        items_received=[NetworkReceipt(7770900, 1, 1, 0)],
        persist_session_state=lambda: None,
        runtime_lifecycle=SimpleNamespace(snapshot=SimpleNamespace(campaign="Base"), map_epochs=SimpleNamespace(published_materialization_lease="1:100")),
        items=SimpleNamespace(_refresh_runtime_context=lambda *args: CONTEXT_BY_IDENTITY["base/hub"]),
        checks=SimpleNamespace(),
        saves=SimpleNamespace()
    )
    value.items.ctx = value
    value.checks.ctx = value
    value.saves.ctx = value
    link = SimpleNamespace(pid=123, namespace="a" * 64, probe=tmp_path / "unused-probe", command_prefix=(), environment=None)
    async def yes(*args, **kwargs):
        return True
    async def get_link():
        return link
    async def scope(*args, **kwargs):
        return (link.namespace, "ap-save", 123, "123", UUID(int=2).hex, "build", 1, "game/hub/hub")
    async def send(packets):
        raise AssertionError("Storage is replaced by the isolated fixture")
    value.saves.checkpoint_recovery_ready = value.items.runtime_effects_ready = yes
    value.items._native_game_link_async = get_link
    value.items._native_materialization_scope = scope
    value.send_msgs = send
    value.delivery_actions_ready = lambda: True
    value.items.delivery_notification_commands = lambda *args: ()
    value.items.effective_delivery_ownership = lambda receipts: SimpleNamespace(reconciliation_item_ids=tuple(r.item for r in receipts), blood_punch_upgrades=())
    return value, definitions, policies


def ledger_snapshot(peer):
    async def read():
        ledger = await Ledger.open(peer.root / "delivery.sqlite3", read_only=True)
        key = RoomKey("DeliveryRoomA", 0, 1, "fixture-v2")
        try:
            commands = await ledger.commands(key, tuple(CommandState), None, 256)
            attempts = []
            for command in commands:
                attempts.extend(await ledger.attempts(key, command.plan.command_id, 0, 256))
            return await ledger.room(key), commands, tuple(attempts)
        finally:
            await ledger.close()
    return peer.reader_loop.run_until_complete(read())


def finish_v2_script(peer):
    room, commands, attempts = ledger_snapshot(peer)
    if room.receipt_boundary == 0:
        peer.collect([history()])
    if not commands:
        peer.action("plan")
    if not attempts:
        peer.action("prepare")
    _, commands, _ = ledger_snapshot(peer)
    if commands[0].state is not CommandState.CONFIRMED:
        peer.action("send")
        peer.action("result", state="confirmed")
    peer.action("ack")
