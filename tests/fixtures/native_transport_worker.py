"""Exercise async transport against fragmented or interrupted helper output."""

import json
import sys
import time
from pathlib import Path

from native_transport_support import Replies, codec, evidence_result, frame


def main():
    mode, trace = sys.argv[1], Path(sys.argv[2])
    replies = Replies()
    if mode == "evidence":
        replies.hello = evidence_result("hello_evidence")
        replies.scope = replies.hello["scope"]
    while raw := sys.stdin.buffer.read(32):
        header = codec.decode("wire_header_2", raw)
        body = sys.stdin.buffer.read(header["payload_length"])
        operation = header["operation"]
        with trace.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"operation": operation, "body": body.hex()}) + "\n")
        if operation == 1 and mode == "evidence":
            envelope, _ = codec._decode_type("read_envelope", body, 0)
            name = {53: "currency_result", 54: "progression_result", 55: "mission_access_result"}[envelope["command_type"]]
            result = evidence_result(name)
            if envelope["command_type"] == 53:
                result["currency"] = codec.decode_variant(53, 1, body[len(codec.encode("read_envelope", envelope)):])[1]["currency"]
            response_body = codec.encode("scope", replies.scope) + codec.encode(codec.COMMANDS[envelope["command_type"]]["result"], result)
        else:
            response_body = replies.body(operation, body)
        response = frame(operation, header["correlation_id"], response_body)
        if operation not in (0, 6):
            response = corrupt(mode, response)
        for offset in range(0, len(response), 3):
            sys.stdout.buffer.write(response[offset : offset + 3])
            sys.stdout.buffer.flush()


def corrupt(mode, response):
    if mode == "timeout":
        time.sleep(10)
    if mode == "partial":
        sys.stdout.buffer.write(response[:17])
        sys.stdout.buffer.flush()
        time.sleep(10)
    if mode == "correlation":
        return response[:16] + bytes(8) + response[24:]
    if mode == "oversized":
        return response[:8] + (16384).to_bytes(4, "little") + response[12:32]
    if mode == "identity":
        return response[:32] + bytes([99]) * 16 + response[48:]
    if mode == "truncated":
        sys.stdout.buffer.write(response[:40])
        sys.stdout.buffer.flush()
        sys.exit(0)
    return response


if __name__ == "__main__":
    main()
