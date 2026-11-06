"""Production projection onto Sentinel's native Mission Select owner."""
from __future__ import annotations

import json
import math
import struct

from doom_eap.strings import text as player_text

from . import sentinel_protocol_generated as codec
from .unified_campaign import STAGES


def _reward_wire(known, entries):
    entries = tuple(entries)
    if len(entries) > 17:
        raise ValueError("Native reward projection exceeds its authored catalog")
    wire = struct.pack("<II", known, len(entries))
    for kind, entry in entries:
        identity, name, text = entry.unlockable.encode("ascii"), entry.name.encode("utf8"), entry.text.encode("utf8")
        if len(identity) >= 80 or len(name) >= 128 or len(text) >= 512:
            raise ValueError("Native reward text exceeds its field capacity")
        wire += struct.pack("<III80s128s512s", entry.location_id, kind, entry.checked, identity, name, text)
    return wire


def _relative_difficulty_band(rating):
    base, expected, allowance = (float(rating[key]) for key in
                                  ("base_cr", "expected_player_cr", "skill_allowance"))
    if not all(math.isfinite(value) and 0 <= value <= 100
               for value in (base, expected, allowance)):
        raise ValueError("Native relative difficulty facts are invalid")
    # Relative contract uses frozen base, expected kit and allowance.
    deficit = round((base - expected - allowance) * 100)
    band = 1 + sum(deficit > cut for cut in (-1000, 0, 1000))
    return band | (round(expected * 100) + 1) << 8


def _difficulty_band(snapshot, row):
    rating = snapshot.get("ratings", {}).get(row["stage"]) if row["revealed"] else None
    band = 0
    if rating is not None and snapshot.get("ratings_version", 1) == 2:
        band = rating.get("skull_tier")
        if type(band) is not int or not 1 <= band <= 4:
            raise ValueError("Native intrinsic difficulty tier is invalid")
    elif rating is not None and "expected_player_cr" in rating:
        band = _relative_difficulty_band(rating)
    return band


def _summary_wire(snapshot, row, namespace):
    summary = snapshot.get("summaries", {}).get(row["stage"])
    band = _difficulty_band(snapshot, row)
    fields = bytes(292)
    if summary is not None:
        if summary.namespace != namespace or summary.runtime_map != row["map"]:
            raise RuntimeError("Native summary belongs to a different campaign view")
        fields = struct.pack("<IIII", 1 if summary.found is not None else 2,
                             summary.found or 0, summary.total or 0, band)
        if len(summary.challenges) > 3:
            raise ValueError("Native mission has more than three physical challenges")
        for challenge in summary.challenges:
            name = challenge.unlockable.encode("ascii")
            if len(name) >= 80:
                raise ValueError("Native challenge identity is too long")
            fields += struct.pack("<80sIII", name, challenge.found, challenge.required, challenge.checked)
        fields += bytes(92 * (3 - len(summary.challenges)))
    return fields


def _stage_rewards_wire(snapshot, row, namespace):
    reward = snapshot.get("challenge_rewards", {}).get(row["stage"])
    entries = [(1, entry) for entry in reward.challenges] if reward else []
    if reward and reward.aggregate:
        entries.append((2, reward.aggregate))
    entries.extend((4, entry) for entry in snapshot.get('encounter_rewards', {}).get(row['stage'], ()))
    if reward and (reward.namespace != namespace or reward.runtime_map != row["map"]):
        raise RuntimeError("Native rewards belong to a different campaign view")
    return _reward_wire(bool(reward and reward.known), entries)


def _campaign_projection(snapshot, namespace):
    rows = [
        (
            1,
            1 | 2 | 16,
            20 + snapshot["fortress_phase"],
            snapshot["hub_map"],
            player_text("notifications", "fortress_title"),
        )
    ]
    stage_ids = {1: "hub"}
    summaries = [bytes(292)]
    rewards = [_reward_wire(True, ((3, entry) for entry in snapshot.get("masteries", ())))]
    hub_summary = snapshot.get("hub_summary")
    if hub_summary is not None:
        summaries[0] = struct.pack("<IIII", 1 if hub_summary.found is not None else 2,
                                   hub_summary.found or 0, hub_summary.total or 0, 0) + bytes(276)
    for row in snapshot["rows"]:
        stage_id = STAGES[row["stage"]]["access_id"]
        stage_ids[stage_id] = row["stage"]
        flags = sum(bit for field, bit in (("revealed", 1), ("unlocked", 2), ("completed", 4),
                                          ("goal", 8), ("details_visible", 32), ("slayer_gate", 64),
                                          ("gate_key", 128), ("gate_complete", 256)) if row.get(field, False))
        rows.append((stage_id, flags, STAGES[row["stage"]]["native_index"], row["map"], row["title"].upper()))
        summaries.append(_summary_wire(snapshot, row, namespace))
        rewards.append(_stage_rewards_wire(snapshot, row, namespace))
    return rows, stage_ids, summaries, rewards


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
        identity = (link.namespace, scope["target_pid"], scope["process_created"], scope["instance_id"])
        rows, stage_ids, summaries, rewards = _campaign_projection(snapshot, link.namespace)
        extended = "challenge_rewards" in snapshot
        serialized = json.dumps(rows, separators=(",", ":")), tuple(summaries), tuple(rewards) if extended else ()

        def exchange(operation, revision=0, index=0, row=(0, 0, 0, "", "")):
            payload = {}
            if operation == 22:
                native_row = codec.decode("sc_campaign_row", struct.pack("<III192s96s", *row[:3], row[3].encode("ascii"), row[4].encode("utf-8")))
                summary = codec.decode("sc_campaign_summary", summaries[index])
                known, count = struct.unpack_from("<II", rewards[index])
                entries = [codec.decode("campaign_reward", rewards[index][8 + n * 732:8 + (n + 1) * 732]) for n in range(count)]
                payload = dict(revision=revision, index=index, count=len(rows), row=native_row, summary=summary,
                               rewards_known=known, rewards=entries)
            elif operation == 23:
                payload = dict(revision=revision, count=len(rows), selected_id=row[0])
            elif operation == 24:
                payload = dict(revision=revision)
            result = link.request(operation, payload=payload)
            if result.get("status") != 0:
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
