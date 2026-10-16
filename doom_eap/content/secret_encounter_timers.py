"""Materialize Secret Encounter time limits from qualified source durations."""

import json
import re
from pathlib import Path


def apply_secret_encounter_timers(text: str, member: str, registry: dict) -> str:
    from tools.maps.ap_map_generator import find_entity_block_bounds

    for record in registry["maps"].values():
        if not member.endswith(record["entities_path"]):
            continue
        for timer in record["timers"]:
            bounds = find_entity_block_bounds(text, timer["entity"])
            if bounds is None:
                raise ValueError(f"Secret Encounter timer entity is missing: {timer['entity']}")
            block = text[bounds[0]:bounds[1]]
            duration = timer["duration_ms"] * 3 // 2
            field = re.compile(r"\bchallengeDuration\s*=\s*\d+;")
            if field.search(block):
                rewritten, count = field.subn(f"challengeDuration = {duration};", block)
            else:
                rewritten, count = re.subn(
                    r"(\bedit\s*=\s*\{)",
                    lambda match: f"{match[1]}\n\t\tchallengeDuration = {duration};",
                    block,
                )
            if count != 1:
                raise ValueError(f"Secret Encounter timer is ambiguous: {timer['entity']}")
            text = text[:bounds[0]] + rewritten + text[bounds[1]:]
    return text


def load_secret_encounter_timers() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "data/secret_encounter_timers.json").read_text(encoding="utf-8"))
