"""Materialize Secret Encounter time limits from qualified source durations."""

import json
import re
from pathlib import Path
from importlib.resources import files


def apply_secret_encounter_timers(text: str, member: str, registry: dict,
                                  frozen_timers: dict | None = None, *, easier: bool = True) -> str:
    from tools.maps.ap_map_generator import find_entity_block_bounds

    for map_key, record in registry["maps"].items():
        if not member.endswith(record["entities_path"]):
            continue
        for timer in record["timers"]:
            bounds = find_entity_block_bounds(text, timer["entity"])
            if bounds is None:
                raise ValueError(f"Secret Encounter timer entity is missing: {timer['entity']}")
            block = text[bounds[0]:bounds[1]]
            duration = (frozen_timers or {}).get(map_key, {}).get(timer["entity"], timer["duration_ms"])
            if type(duration) is not int or not 1 <= duration <= 0x7fffffff:
                raise ValueError("Secret Encounter duration is outside the game's range")
            if easier:
                duration = duration * 3 // 2
            if duration > 0x7fffffff:
                raise ValueError("Easier Secret Encounters exceeds the game's duration limit")
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
    path = files(__package__).joinpath("secret_encounter_timers.json")
    if not path.is_file():
        path = Path(__file__).resolve().parents[2] / "data/secret_encounter_timers.json"
    return json.loads(path.read_text(encoding="utf-8"))
