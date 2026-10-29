"""External console peer with a durable effect log; it does not consume queue files."""

import json
import os
from pathlib import Path
import sys


def signal(event, **fields):
    print("DELIVERY\t" + json.dumps({"event": event, **fields}), flush=True)


def barrier(event):
    signal(event)
    if not sys.stdin.readline():
        raise EOFError(event)


def main():
    root = Path(sys.argv[1])
    signal("ready")
    for line in sys.stdin:
        request = json.loads(line)
        barrier("external_effect:before")
        with (root / "effects.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(request) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        barrier("external_effect:after")
        signal("result", accepted=True)


if __name__ == "__main__":
    main()
