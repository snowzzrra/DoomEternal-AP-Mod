"""Model v2 identity and retention in a separate process; this is not a wire codec."""

import asyncio
import json
import os
import sys
import traceback
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

from delivery_signals import signal
from websockets.exceptions import ConnectionClosed
from websockets.legacy.server import serve

from doom_eap.runtime import sentinel_protocol_generated as codec

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from delivery_v2_worker import zero_value
from native_transport_support import command_response, evidence_result, fixtures, frame


def _identity_conflict(stored, request, reference):
    return stored is not None and (
        stored["reference"] != reference
        or (request["op"] == "submit" and stored["envelope"] != request["envelope"])
    )


def _stored_result(reference, request, stored):
    if request["op"] == "dispatch_only":
        stored["result"]["effect"] = "dispatch_only"
    if request["op"] in ("ack", "expire"):
        stored["result"] = {"status": "terminal", "retention": "compact"}
    return {"reference": reference, **stored["result"]}


class Core:
    def __init__(self, root, stop):
        self.root, self.stop = root, stop
        self.instance = str(uuid4())
        self.commands = {}

    async def barrier(self, event):
        if event == self.stop:
            signal(event)
            if not await asyncio.to_thread(sys.stdin.readline):
                raise EOFError(event)

    async def connect(self, socket):
        try:
            async for raw in socket:
                response = await self.receive(json.loads(raw))
                await socket.send(json.dumps(response))
        except ConnectionClosed:
            pass

    async def receive(self, request):
        if request["op"] == "hello":
            return {"instance": self.instance, "version": 2}
        reference = request["reference"]
        decoded = codec.decode("command_reference", bytes.fromhex(reference))
        if bytes(decoded["scope"]["instance_id"]) != UUID(self.instance).bytes:
            return {"status": "instance_changed", "instance": self.instance}
        command_id = decoded["command_id"]
        stored = self.commands.get(command_id)
        if _identity_conflict(stored, request, reference):
            return {"status": "identity_conflict"}
        if request["op"] == "submit" and stored is None:
            return await self.execute(request)
        if stored is None:
            return {"status": "absent"}
        return _stored_result(reference, request, stored)

    async def execute(self, request):
        reference = request["reference"]
        decoded = codec.decode("command_reference", bytes.fromhex(reference))
        command_id = decoded["command_id"]
        stored = {"reference": reference, "envelope": request["envelope"], "result": {"status": "claimed"}}
        self.commands[command_id] = stored
        await self.barrier("attempt_journal:after")
        await self.barrier("external_effect:before")
        with (self.root / "effects.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"command": command_id, "envelope": request["envelope"]}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        effect = "dispatch_only" if decoded["command_type"] == 46 else "applied"
        stored["result"] = {"status": "terminal", "retention": "full", "effect": effect}
        await self.barrier("external_effect:after")
        signal("effect_complete")
        return {"reference": reference, **stored["result"]}


    def wire_setup(self):
        self.path = self.root / "wire-core.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {"weapons": 0, "points": 0, "commands": {}}
        self.state.setdefault("batteries", 0)
        self.state.setdefault("battery_gained", 0)
        self.scope = json.loads(os.environ["PILLAR_CORE_SCOPE"])
        self.hello = codec.decode("hello_result", fixtures()["hello_result"][32:])
        self.hello["scope"] = self.scope
        self.hello["supported"] = [{"command_type": key, "payload_revision": revision}
                                   for key, command in codec.COMMANDS.items() for revision in command["payload_revisions"]]
        self.binding = UUID(int=1).bytes

    def wire_currency_result(self, namespace):
        value = evidence_result("currency_result")
        for key in ("before_scope", "after_scope"):
            value[key].update(process={"pid": self.scope["pid"], "process_created": self.scope["process_created"],
                                       "instance_id": self.scope["instance_id"], "lifecycle_generation": self.scope["generation"]},
                              namespace_id=[*namespace, 0], save_binding=list(self.binding),
                              map_name=list(b"game/hub/hub".ljust(192, b"\0")))
        value.update(balance_before=self.state["batteries"], balance_after=self.state["batteries"],
                     gained_before=self.state["battery_gained"], gained_after=self.state["battery_gained"])
        return value

    def wire_result(self, command, namespace):
        if command == 53:
            return self.wire_currency_result(namespace)
        name = codec.COMMANDS[command]["result"]
        if command in (18, 46):
            data = bytearray(4096)
            offset = 4 + len(codec.encode("sc_diagnostic_result", zero_value("sc_diagnostic_result")))
            data[offset:offset + 64] = b"a" * 64
            value, _ = codec._decode_type(name, data, 0)
        else:
            value = zero_value(name)
        if "namespace_id" in value:
            value["namespace_id"] = [*namespace, 0]
        if command == 12:
            value["save_binding"] = self.binding
        elif command == 25:
            value.update(weapons_after=self.state["weapons"], perks_known=0, perks_owned=0)
        elif command == 18:
            value.update(flags=16, outcome=1, balance_after=self.state["points"], gained_after=self.state["points"])
        return value

    def wire_read(self, body):
        request, _ = codec._decode_type("read_envelope", body, 0)
        command = request["command_type"]
        if command not in (12, 25, 18, 53):
            return 2, b""
        value = self.wire_result(command, request["namespace_id"])
        return 0, codec.encode("scope", self.scope) + codec.encode(codec.COMMANDS[command]["result"], value)

    def wire_prepare(self, body):
        submission, length = codec._decode_type("submission", body, 0)
        payload = body[length:]
        reference = submission["reference"]
        immutable = {**reference, "save_binding": submission["save_binding"], "deadline_ms": submission["deadline_ms"]}
        del immutable["command_id"]
        reference["fingerprint"] = sha256(codec.encode("immutable_submission", immutable) + payload).digest()
        return reference, payload

    def wire_execute(self, reference, payload):
        command, identity = reference["command_type"], reference["command_id"]
        tag, fields = codec.decode_variant(command, reference["payload_revision"], payload)
        value = self.wire_result(command, reference["namespace_id"])
        if command == 25:
            self.state["weapons"] |= fields["weapons"]
            value["weapons_after"] = self.state["weapons"]
        elif command == 18:
            value.update(amount=fields["amount"], expected_gained=fields["expected_gained"],
                         balance_before=self.state["points"], gained_before=self.state["points"])
            self.state["points"] += fields["amount"]
            value.update(balance_after=self.state["points"], gained_after=self.state["points"])
        elif command == 46 and fields.get("entity") == "ap_rpc_v3_7770900":
            self.state["weapons"] |= 1
        elif command == 53:
            self.state["batteries"] += fields["amount"]
            self.state["battery_gained"] += fields["amount"]
            value.update(amount=fields["amount"], expected_gained=fields["expected_gained"],
                         balance_after=self.state["batteries"], gained_after=self.state["battery_gained"])
        self.state["commands"][identity] = codec.encode(codec.COMMANDS[command]["result"], value).hex()
        self.path.write_text(json.dumps(self.state), encoding="utf-8")
        with (self.root / "wire-effects.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"id": identity, "command": command, "tag": tag, "payload": fields}) + "\n")

    def wire_command(self, operation, body):
        if operation in (0, 2):
            reference, payload = self.wire_prepare(body)
        elif operation == 5:
            reference = codec.decode("ack", body)["reference"]
        else:
            reference = codec.decode("command_reference", body)
        if operation == 0:
            return codec.encode("command_reference", reference)
        identity = reference["command_id"]
        refusal = os.environ.get("PILLAR_CORE_REFUSAL")
        if operation == 2 and refusal:
            rule = next(rule for rule in codec.SCHEMA["command_results"]["admission_refusals"] if rule["name"] == refusal)
            return codec.encode("command_response", command_response(
                reference, effect=0, execution=0, error=rule["error"], state=rule.get("state", 0), reason=rule.get("reason", 0)))
        if operation == 2 and identity not in self.state["commands"]:
            self.wire_execute(reference, payload)
        stored = self.state["commands"].get(identity)
        return self.wire_terminal(reference, stored)

    def wire_terminal(self, reference, stored):
        response = codec.encode("command_response", command_response(
            reference, effect=2 if reference["command_type"] == 46 else 1,
            retention=0 if stored else 2, error=0 if stored else 4))
        return response + bytes.fromhex(stored or "")

    def wire_loop(self):
        while raw := sys.stdin.buffer.read(32):
            header = codec.decode("wire_header_2", raw)
            body = sys.stdin.buffer.read(header["payload_length"])
            operation, error = header["operation"], 0
            if operation == 6:
                response = codec.encode("hello_result", self.hello)
            elif operation == 1:
                error, response = self.wire_read(body)
            else:
                response = self.wire_command(operation, body)
            sys.stdout.buffer.write(frame(operation, header["correlation_id"], response, result=error))
            sys.stdout.buffer.flush()


async def main():
    core = Core(Path(sys.argv[1]), sys.argv[2])
    async with serve(core.connect, "127.0.0.1", 0) as server:
        signal("ready", port=server.sockets[0].getsockname()[1], instance=core.instance)
        await asyncio.Future()


def wire_main(root):
    core = Core(root, "")
    core.wire_setup()
    core.wire_loop()


if __name__ == "__main__":
    if sys.argv[1] == "--wire":
        with (Path(sys.argv[2]) / "wire-errors.log").open("w", encoding="utf-8") as errors:
            try:
                wire_main(Path(sys.argv[2]))
            except Exception:
                traceback.print_exc(file=errors)
                raise
    else:
        asyncio.run(main())
