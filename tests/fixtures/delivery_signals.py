"""Line protocol shared by the isolated delivery workers."""
import json


def signal(event, **fields):
    print("DELIVERY\t" + json.dumps({"event": event, **fields}), flush=True)
