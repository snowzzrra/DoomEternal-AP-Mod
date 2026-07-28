"""Guard the core data pipeline against reintroducing campaign knowledge."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE_MODULES = (
    ROOT / "content_catalog.py",
    ROOT / "challenge_registry.py",
    ROOT / "tools" / "validation" / "validate_data.py",
    ROOT / "tools" / "maps" / "mission_complete_map_patcher.py",
)


def _catalog_map_keys() -> set[str]:
    from content_catalog import discover_maps

    return {spec.key for spec in discover_maps()}


def test_core_modules_do_not_hardcode_current_map_keys_or_location_ranges() -> None:
    keys = "|".join(re.escape(key) for key in sorted(_catalog_map_keys()))
    pattern = re.compile(rf"\b(?:{keys})\b|range\(777|expected_count\s*=")
    offenders = {
        path.relative_to(ROOT).as_posix(): pattern.findall(
            path.read_text(encoding="utf-8").replace("game/sp/hub/hub", "").replace("game/hub/hub", "")
        )
        for path in CORE_MODULES
    }
    assert not {path: values for path, values in offenders.items() if values}


def test_new_catalog_test_architecture_has_no_campaign_knowledge() -> None:
    keys = "|".join(re.escape(key) for key in sorted(_catalog_map_keys()))
    forbidden = re.compile(rf"\b(?:{keys})\b|range\(777|expected_count\s*=")
    central_tests = tuple((ROOT / "tests").glob("test_content*.py")) + (
        ROOT / "tests" / "conftest.py",
        ROOT / "tests" / "test_generated_content.py",
    )
    offenders = {
        path.relative_to(ROOT).as_posix(): forbidden.findall(path.read_text(encoding="utf-8"))
        for path in central_tests
        if path.is_file() and path != Path(__file__)
    }
    assert not {path: values for path, values in offenders.items() if values}


def test_no_map_static_test_file_remains() -> None:
    assert not list((ROOT / "tests").rglob("test_*_static.py"))
