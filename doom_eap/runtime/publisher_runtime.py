"""Pure/runtime helpers for publisher trigger validation and acknowledgement."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from doom_eap.contracts.publisher_contracts import (
    PublisherContract,
    publishers_by_trigger,
    trigger_key,
)


class PublisherEngine:
    """Generic publisher discovery; effect execution remains caller-owned."""

    def __init__(self, publishers: tuple[PublisherContract, ...]):
        self.publishers = publishers
        self.publishers_by_trigger = publishers_by_trigger(publishers)

    def observe(
        self,
        strategy: str,
        payload: Mapping[str, Any],
    ) -> tuple[PublisherContract, ...]:
        return self.publishers_by_trigger.get(trigger_key(strategy, payload), ())


def effect_acknowledged(
    effect: Mapping,
    checked_locations: Iterable[int],
    goal_sent: bool,
) -> bool:
    strategy = effect["strategy"]
    if strategy == "location_check":
        return effect["location_id"] in checked_locations
    if strategy == "campaign_goal":
        return goal_sent
    if strategy == "preserved_native_target":
        return True
    raise ValueError(f"unsupported publisher effect: {strategy}")


def publisher_acknowledged(
    publisher: PublisherContract,
    checked_locations: Iterable[int],
    goal_sent: bool,
) -> bool:
    checked = set(checked_locations)
    return all(effect_acknowledged(effect, checked, goal_sent) for effect in publisher.effects)


def read_map_event(path: Path, marker: str) -> tuple[bool, str, str]:
    try:
        contents = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        contents = ""
    digest = hashlib.sha256(contents.encode("utf-8")).hexdigest()
    return marker in contents, contents, digest


def quarantine_malformed_event(
    path: Path,
    *,
    key: str,
    contents: str,
    sha256: str,
    quarantine_root: Path,
) -> tuple[Path, Path]:
    failed = quarantine_root / "failed"
    failed.mkdir(parents=True, exist_ok=True)
    stem = f"{int(time.time_ns())}_{sha256[:12]}_{path.name}"
    quarantined = failed / stem
    metadata = failed / f"{stem}.json"
    try:
        os.replace(path, quarantined)
    except OSError:
        quarantined.write_text(contents, encoding="utf-8", newline="")
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    metadata.write_text(
        json.dumps(
            {
                "publisher_key": key,
                "filename": path.name,
                "content": contents,
                "sha256": sha256,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return quarantined, metadata
