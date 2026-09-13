"""Production projection onto Sentinel's native Mission Select owner."""
from __future__ import annotations

import json
import secrets
import struct

from .unified_campaign import STAGES


class CampaignMenu:
    def __init__(self):
        self.identity = None
        self.rows = None
        self.revision = 0

    def synchronize(self, link, snapshot):
        link.admitted_save_root()
        scope = link._run(["--pid", str(link.pid), "--native", "--json"])
        if scope.get("availability") != "enabled":
            raise RuntimeError("Native Campaign menu owner is unavailable")
        identity = (link.namespace, scope["pid"], scope["process_created"], scope["instance_id"])
        rows = [(1, 1 | 2 | 16, len(STAGES) + snapshot["fortress_phase"], snapshot["hub_map"], "FORTRESS OF DOOM")]
        stage_ids = {1: "hub"}
        for row in snapshot["rows"]:
            stage_id = STAGES[row["stage"]]["access_id"]
            stage_ids[stage_id] = row["stage"]
            flags = sum(bit for field, bit in (("revealed", 1), ("unlocked", 2), ("completed", 4),
                                              ("goal", 8), ("details_visible", 32)) if row[field])
            rows.append((stage_id, flags, STAGES[row["stage"]]["native_index"], row["map"], row["title"].upper()))
        serialized = json.dumps(rows, separators=(",", ":"))

        def exchange(operation, revision=0, index=0, row=(0, 0, 0, "", "")):
            request_id = secrets.randbits(64) or 1
            payload = struct.pack(
                "<QIQ16sQQ16sI", 4096, link.pid, int(scope["process_created"]),
                bytes.fromhex(scope["instance_id"]), int(scope["lifecycle_generation"]),
                request_id, secrets.token_bytes(16), 2000,
            ) + link.namespace.encode("ascii") + b"\0"
            payload += struct.pack("<QIIIII192s96s", revision, index, len(rows), row[0], row[1], row[2],
                                   row[3].encode("ascii"), row[4].encode("ascii"))
            message = struct.pack("<IHHII", 0x50494353, 1, operation, len(payload), 0) + payload
            result = link._run(["--campaign-menu"], message)
            if result.get("namespace") != link.namespace or int(result.get("request_id", 0)) != request_id:
                raise RuntimeError("Native Campaign projection response identity differs")
            if result.get("build_id") != scope.get("build_id") or result.get("status") != 0:
                raise RuntimeError(f"Native Campaign projection refused: {result.get('reason')}")
            return result

        result = exchange(24)
        if self.identity != identity or self.rows != serialized:
            # An interrupted upload never mutates the committed list. Use a new
            # monotonic revision for its replacement, including after reconnect.
            self.revision = max(self.revision, int(result["committed_revision"])) + 1
            for index, row in enumerate(rows):
                exchange(22, self.revision, index, row)
            selected_id = next((key for key, stage in stage_ids.items() if stage == snapshot["selected"]), 1)
            result = exchange(23, self.revision, row=(selected_id, 0, 0, "", ""))
            if int(result["committed_revision"]) != self.revision:
                raise RuntimeError("Native Campaign projection was not committed")
            self.identity, self.rows = identity, serialized
        result["selected_stage"] = stage_ids.get(result["selected_id"])
        result["loaded_stage"] = stage_ids.get(result["loaded_id"])
        return result
