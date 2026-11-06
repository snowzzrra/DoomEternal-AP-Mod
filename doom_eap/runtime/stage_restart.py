"""Player activity from a process-bound Core restart observation."""


def activity_events(previous, current, stage):
    if current is None:
        return []
    if previous is not None and (current["sequence"], current["status"]) == (previous["sequence"], previous["status"]):
        return []
    statuses = {1: "pending", 2: "done", 3: "failed"}
    status = statuses[current["status"]]
    events = []
    if status != "pending" and (previous is None or previous["sequence"] != current["sequence"]):
        events.append({"status": "pending", "stage": stage})
    events.append({"status": status, "stage": stage})
    return events
