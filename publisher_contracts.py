"""Declarative runtime publisher contracts shared by compiler and bridge."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent
CONTRACT_PATH = ROOT / "data" / "publisher_contracts.json"
TRIGGER_STRATEGIES = frozenset({"native_transition", "map_event_file", "terminal_owner"})
EFFECT_STRATEGIES = frozenset({
    "location_check", "campaign_goal", "preserved_native_target",
})
FALLBACK_POLICIES = frozenset({"first_success_wins"})


def canonical_map_name(name: str | None) -> str:
    normalized = str(name or "").strip().replace("\\", "/").rstrip("/")
    return "game/hub/hub" if normalized in {"game/hub/hub", "game/sp/hub/hub"} else normalized


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class PublisherContract:
    key: str
    map_key: str
    triggers: tuple[Mapping[str, Any], ...]
    effects: tuple[Mapping[str, Any], ...]
    dedupe_scope: str
    fallback_policy: str

    def triggers_for(self, strategy: str) -> tuple[Mapping[str, Any], ...]:
        return tuple(trigger for trigger in self.triggers if trigger["strategy"] == strategy)


TriggerKey = tuple[str, ...]


def trigger_key(strategy: str, payload: Mapping[str, Any]) -> TriggerKey:
    """Return the canonical, strategy-owned lookup key for a trigger."""
    if strategy == "native_transition":
        return (
            strategy,
            canonical_map_name(payload.get("from_map")),
            canonical_map_name(payload.get("to_map")),
        )
    if strategy == "map_event_file":
        return (strategy, str(payload.get("filename", "")))
    if strategy == "terminal_owner":
        return (strategy, str(payload.get("owner", "")))
    raise ValueError(f"unsupported publisher trigger strategy: {strategy!r}")


def publishers_by_trigger(
    publishers: tuple[PublisherContract, ...],
) -> Mapping[TriggerKey, tuple[PublisherContract, ...]]:
    """Index every publisher without assuming an effect count or category."""
    grouped: dict[TriggerKey, list[PublisherContract]] = {}
    for publisher in publishers:
        for trigger in publisher.triggers:
            grouped.setdefault(trigger_key(trigger["strategy"], trigger), []).append(publisher)
    return MappingProxyType({
        key: tuple(sorted(values, key=lambda publisher: publisher.key))
        for key, values in grouped.items()
    })


def validate_publisher_contracts(document: Mapping[str, Any]) -> None:
    if document.get("schema_version") != 1:
        raise ValueError("publisher contract schema_version must be 1")
    publishers = document.get("publishers")
    if not isinstance(publishers, list) or not publishers:
        raise ValueError("publisher contracts require a non-empty publishers list")
    keys: set[str] = set()
    filename_markers: dict[str, str] = {}
    marker_filenames: dict[str, str] = {}
    for publisher in publishers:
        key = publisher.get("key")
        if not isinstance(key, str) or not key or key in keys:
            raise ValueError(f"publisher key must be unique and non-empty: {key!r}")
        keys.add(key)
        if publisher.get("fallback_policy") not in FALLBACK_POLICIES:
            raise ValueError(f"{key}: unsupported fallback_policy")
        triggers = publisher.get("triggers")
        effects = publisher.get("effects")
        if not isinstance(triggers, list) or not triggers:
            raise ValueError(f"{key}: triggers must be non-empty")
        if not isinstance(effects, list) or not effects:
            raise ValueError(f"{key}: effects must be non-empty")
        for trigger in triggers:
            strategy = trigger.get("strategy")
            if strategy not in TRIGGER_STRATEGIES:
                raise ValueError(f"{key}: unsupported trigger strategy {strategy!r}")
            if strategy == "native_transition":
                if not canonical_map_name(trigger.get("from_map")) or not canonical_map_name(trigger.get("to_map")):
                    raise ValueError(f"{key}: native_transition requires from_map/to_map")
            elif strategy == "map_event_file":
                filename = trigger.get("filename")
                marker = trigger.get("marker")
                if not isinstance(filename, str) or not filename.endswith(".txt"):
                    raise ValueError(f"{key}: map_event_file requires a .txt filename")
                if not isinstance(marker, str) or not marker:
                    raise ValueError(f"{key}: map_event_file requires a marker")
                if filename in filename_markers and filename_markers[filename] != marker:
                    raise ValueError(f"{key}: shared filename has a different marker")
                if marker in marker_filenames and marker_filenames[marker] != filename:
                    raise ValueError(f"{key}: shared marker has a different filename")
                filename_markers[filename] = marker
                marker_filenames[marker] = filename
            elif strategy == "terminal_owner" and not trigger.get("owner"):
                raise ValueError(f"{key}: terminal_owner requires owner")
        for effect in effects:
            strategy = effect.get("strategy")
            if strategy not in EFFECT_STRATEGIES:
                raise ValueError(f"{key}: unsupported effect strategy {strategy!r}")
            if strategy == "location_check":
                location_id = effect.get("location_id")
                if not isinstance(location_id, int) or isinstance(location_id, bool):
                    raise ValueError(f"{key}: location_check requires integer location_id")
            elif strategy == "preserved_native_target" and not effect.get("target"):
                raise ValueError(f"{key}: preserved_native_target requires target")


def publisher_contracts_from_document(
    document: Mapping[str, Any],
    *,
    allow_empty: bool = False,
) -> tuple[PublisherContract, ...]:
    if allow_empty and document.get("publishers") == []:
        if document.get("schema_version") != 1:
            raise ValueError("publisher contract schema_version must be 1")
        return ()
    validate_publisher_contracts(document)
    result = []
    for publisher in document["publishers"]:
        triggers = []
        for raw_trigger in publisher["triggers"]:
            trigger = dict(raw_trigger)
            if trigger["strategy"] == "native_transition":
                trigger["from_map"] = canonical_map_name(trigger["from_map"])
                trigger["to_map"] = canonical_map_name(trigger["to_map"])
            triggers.append(_freeze(trigger))
        result.append(PublisherContract(
            key=publisher["key"],
            map_key=publisher.get("map_key", ""),
            triggers=tuple(triggers),
            effects=tuple(_freeze(effect) for effect in publisher["effects"]),
            dedupe_scope=publisher["dedupe_scope"],
            fallback_policy=publisher["fallback_policy"],
        ))
    return tuple(result)


def load_publisher_contracts(path: Path = CONTRACT_PATH) -> tuple[PublisherContract, ...]:
    return publisher_contracts_from_document(
        json.loads(path.read_text(encoding="utf-8"))
    )


def publishers_for_transition(
    publishers: tuple[PublisherContract, ...],
    from_map: str,
    to_map: str,
) -> tuple[PublisherContract, ...]:
    index = publishers_by_trigger(publishers)
    return index.get(trigger_key("native_transition", {
        "from_map": from_map,
        "to_map": to_map,
    }), ())


def map_publishers_for_owner(
    publishers: tuple[PublisherContract, ...],
    map_key: str,
    owner: str,
) -> tuple[PublisherContract, ...]:
    return tuple(sorted(
        (
            publisher
            for publisher in publishers
            if publisher.map_key == map_key
            and any(
                trigger.get("owner") == owner
                for trigger in (
                    *publisher.triggers_for("map_event_file"),
                    *publisher.triggers_for("terminal_owner"),
                )
            )
        ),
        key=lambda publisher: publisher.key,
    ))
