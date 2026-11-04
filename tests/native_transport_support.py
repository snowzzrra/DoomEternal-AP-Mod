"""Serve Core wire fixtures over test streams without game access."""

import copy
import json
import os
from pathlib import Path

from doom_eap.runtime import sentinel_protocol_generated as codec

CORE = Path(os.environ.get("SENTINEL_CORE_SOURCE", Path(__file__).resolve().parents[2] / "Sentinel-Core"))


def fixtures():
    items = json.loads((CORE / "tests/fixtures/wire/v2.json").read_bytes())["fixtures"]
    return {item["name"]: b"".join(bytes.fromhex(field["hex"]) for field in item["fields"]) for item in items}


def evidence_fixtures():
    return {item["name"]: item for item in
            json.loads((CORE / "tests/fixtures/wire/evidence_results.json").read_bytes())["fixtures"]}


def evidence_result(name):
    item = evidence_fixtures()[name]
    return codec.decode(item["type"], bytes.fromhex(item["hex"]))


def currency_payloads():
    return json.loads((CORE / "tests/fixtures/wire/evidence.json").read_bytes())["fixtures"]


def split(name, body):
    value, end = codec._decode_type(name, body, 0)
    return value, body[end:]


def command_response(reference, *, effect=1, progress=2, execution=1, retention=0, error=0, state=0, reason=0):
    value, _ = split("command_response", fixtures()["terminal_result"][32:])
    value["reply"].update(reference=reference, actual_instance_id=reference["scope"]["instance_id"])
    value["reply"]["dimensions"].update(effect=effect, progress=progress, execution=execution, retention=retention)
    value["execution"].update(state=state, reason=reason)
    value["error"] = error
    return value


def frame(operation, correlation, body=b"", result=0):
    header = {
        "magic": codec.SCHEMA["protocol"]["magic"],
        "version": 2,
        "operation": operation,
        "payload_length": len(body),
        "result": result,
        "correlation_id": correlation,
        "flags": 1,
        "reserved": 0,
    }
    return codec.encode("wire_header_2", header) + body


class Replies:
    def __init__(self, scope=None, reference=None):
        self.fixtures = fixtures()
        self.hello = codec.decode("hello_result", self.fixtures["hello_result"][32:])
        if scope is not None:
            self.hello["scope"] = scope
        self.scope = self.hello["scope"]
        self.reference = reference or codec.decode("command_reference", self.fixtures["terminal_query"][32:])
        self.operations = []

    def body(self, operation, body):
        self.operations.append(operation)
        if operation == 0:
            return codec.encode("command_reference", self.reference)
        if operation == 6:
            return codec.encode("hello_result", self.hello)
        if operation == 1:
            result = {"observation": codec.decode("sc_save_admission_snapshot", bytes(156)), "save_binding": bytes(16)}
            return codec.encode("scope", self.scope) + codec.encode("save_admission_result", result)
        request_reference = self.reference
        if operation in (3, 4):
            request_reference = codec.decode("command_reference", body)
        if operation == 5:
            request_reference = codec.decode("ack", body)["reference"]
        response, tail = split("command_response", self.fixtures["terminal_result"][32:])
        response = copy.deepcopy(response)
        response["reply"]["actual_instance_id"] = self.scope["instance_id"]
        response["reply"]["reference"] = request_reference
        return codec.encode("command_response", response) + tail
