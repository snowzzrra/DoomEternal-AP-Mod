from __future__ import annotations

from pathlib import Path

from publisher_contracts import (
    PublisherContract,
    publishers_for_transition,
    validate_publisher_contracts,
)
from publisher_runtime import (
    PublisherEngine,
    publisher_acknowledged,
    quarantine_malformed_event,
    read_map_event,
)
from tools.maps.mission_complete_map_patcher import compile_publishers


def _publisher(key: str, effect: dict, marker: str, filename: str) -> PublisherContract:
    return PublisherContract(
        key,
        "synthetic",
        (
            {
                "strategy": "map_event_file",
                "owner": "owner",
                "marker": marker,
                "filename": filename,
            },
            {
                "strategy": "native_transition",
                "from_map": "fixture/from",
                "to_map": "fixture/to",
            },
        ),
        (effect,),
        f"scope_{key}",
        "first_success_wins",
    )


def test_native_transition_publishes_mission_complete_once() -> None:
    publisher = _publisher(
        "mission", {"strategy": "location_check", "location_id": 77}, "MISSION", "mission.txt"
    )
    matching = publishers_for_transition((publisher,), "fixture/from", "fixture/to")
    assert matching == (publisher,)
    assert not publisher_acknowledged(publisher, set(), False)
    assert publisher_acknowledged(publisher, {77}, False)


def test_one_trigger_dispatches_two_publishers() -> None:
    mission = _publisher(
        "mission", {"strategy": "location_check", "location_id": 77}, "MISSION", "mission.txt"
    )
    goal = _publisher("goal", {"strategy": "campaign_goal"}, "GOAL", "goal.txt")
    assert PublisherEngine((goal, mission)).observe(
        "native_transition", {"from_map": "fixture/from", "to_map": "fixture/to"}
    ) == (goal, mission)


def test_two_publishers_may_share_one_map_side_trigger() -> None:
    document = {
        "schema_version": 1,
        "publishers": [
            {
                "key": key,
                "map_key": "synthetic",
                "triggers": [{
                    "strategy": "map_event_file",
                    "owner": "owner",
                    "filename": "shared.txt",
                    "marker": "SHARED",
                }],
                "effects": [effect],
                "dedupe_scope": f"scope_{key}",
                "fallback_policy": "first_success_wins",
            }
            for key, effect in (
                ("mission", {"strategy": "location_check", "location_id": 77}),
                ("goal", {"strategy": "campaign_goal"}),
            )
        ],
    }
    validate_publisher_contracts(document)


def test_publisher_with_two_effects_acknowledges_independently() -> None:
    publisher = PublisherContract(
        "multi",
        "synthetic",
        ({"strategy": "terminal_owner", "owner": "terminal"},),
        (
            {"strategy": "location_check", "location_id": 77},
            {"strategy": "campaign_goal"},
        ),
        "multi_scope",
        "first_success_wins",
    )
    engine = PublisherEngine((publisher,))
    assert engine.observe("terminal_owner", {"owner": "terminal"}) == (publisher,)
    assert not publisher_acknowledged(publisher, {77}, False)
    assert publisher_acknowledged(publisher, {77}, True)


def test_map_and_transition_order_first_success_wins() -> None:
    publisher = _publisher(
        "mission", {"strategy": "location_check", "location_id": 77}, "MISSION", "mission.txt"
    )
    for order in (("map_event_file", "native_transition"), ("native_transition", "map_event_file")):
        sends = 0
        checked: set[int] = set()
        for _strategy in order:
            if not publisher_acknowledged(publisher, checked, False):
                sends += 1
                checked.add(77)
        assert sends == 1


def test_two_publishers_share_only_owner_and_keep_native_target() -> None:
    mission = PublisherContract(
        "mission",
        "synthetic",
        ({"strategy": "map_event_file", "owner": "owner", "marker": "MISSION", "filename": "mission.txt"},),
        (
            {"strategy": "location_check", "location_id": 77},
            {"strategy": "preserved_native_target", "target": "vanilla"},
        ),
        "mission_scope",
        "first_success_wins",
    )
    goal = _publisher("goal", {"strategy": "campaign_goal"}, "GOAL", "goal.txt")
    compiled = compile_publishers((goal, mission))
    assert len(compiled["owner_targets"]) == 3
    assert compiled["preserved_native_targets"] == ["vanilla"]
    assert compiled["publishers"]["mission"]["relay"] != compiled["publishers"]["goal"]["relay"]
    assert compiled["publishers"]["mission"]["filename"] != compiled["publishers"]["goal"]["filename"]
    assert compiled["publishers"]["mission"]["marker"] != compiled["publishers"]["goal"]["marker"]
    assert "echo MISSION; condump" not in compiled["entities"]
    assert "echo GOAL; condump" not in compiled["entities"]


def test_goal_file_parser_accepts_compiled_marker(tmp_path: Path) -> None:
    goal = _publisher("goal", {"strategy": "campaign_goal"}, "GOAL_MARKER", "goal.txt")
    compiled = compile_publishers((goal,))
    assert "echo GOAL_MARKER" in compiled["entities"]
    path = tmp_path / "goal.txt"
    path.write_text("console history\nGOAL_MARKER\n", encoding="utf-8")
    valid, contents, digest = read_map_event(path, "GOAL_MARKER")
    assert valid
    assert contents.endswith("GOAL_MARKER\n")
    assert len(digest) == 64


def test_malformed_event_is_quarantined_without_ack(tmp_path: Path) -> None:
    publisher = _publisher(
        "mission", {"strategy": "location_check", "location_id": 77}, "MISSION", "mission.txt"
    )
    path = tmp_path / "mission.txt"
    path.write_text("missing marker", encoding="utf-8")
    valid, contents, digest = read_map_event(path, "MISSION")
    assert not valid
    quarantined, metadata = quarantine_malformed_event(
        path,
        key=publisher.key,
        contents=contents,
        sha256=digest,
        quarantine_root=tmp_path / "quarantine",
    )
    assert not path.exists()
    assert quarantined.read_text(encoding="utf-8") == "missing marker"
    assert digest in metadata.read_text(encoding="utf-8")
    assert not publisher_acknowledged(publisher, set(), False)
    assert publishers_for_transition((publisher,), "fixture/from", "fixture/to") == (publisher,)
