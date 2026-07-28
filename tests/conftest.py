"""Collection policy, data-only selection, and session-scoped map cache."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from content_catalog import ContentCatalog, MapSpec, discover_maps, load_content_catalog
from tools.maps.ap_map_generator import generate_map


ROOT = Path(__file__).resolve().parents[1]
GENERATOR_VERSION = hashlib.sha256(
    (ROOT / "tools" / "maps" / "ap_map_generator.py").read_bytes()
).hexdigest()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("doom-content")
    group.addoption("--map", action="store", dest="doom_map", metavar="KEY", help="run only one catalog map")
    group.addoption("--changed", action="store_true", help="select maps affected by local git diff")


def pytest_configure(config: pytest.Config) -> None:
    config._doom_generated_map_counts = {}
    for marker in (
        "unit: pure/unit-level contract test",
        "catalog: authoring schema, catalog, IDs, routes, publishers, or assets",
        "generated: inspect deterministic generated data without real map generation",
        "integration: generates or patches real map output",
        "slow: build-like or broad regression test",
        "apworld: complete APWorld import, seed, or fill test",
    ):
        config.addinivalue_line("markers", marker)


def pytest_terminal_summary(terminalreporter, exitstatus: int, config: pytest.Config) -> None:
    counts = config._doom_generated_map_counts
    if counts:
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        terminalreporter.write_line(f"doom generated maps (session cache): {rendered}")


def _changed_paths() -> set[str]:
    try:
        output = subprocess.run(
            ["git", "diff", "--name-only"], cwd=ROOT, check=True,
            text=True, capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return set()
    return {line.strip() for line in output.splitlines() if line.strip()}


def selected_map_keys(config: pytest.Config, catalog: ContentCatalog) -> set[str]:
    requested = config.getoption("doom_map")
    all_keys = {spec.key for spec in catalog.enabled_maps()}
    pipeline_selected = os.environ.get("AP_PIPELINE_SELECTED_MAPS")
    if pipeline_selected:
        selected = set(json.loads(pipeline_selected))
        unknown = selected - all_keys
        if unknown:
            raise pytest.UsageError(f"unknown pipeline map keys: {sorted(unknown)}")
        return selected
    if requested:
        if requested not in all_keys:
            raise pytest.UsageError(f"unknown --map {requested!r}; use a key from content_catalog")
        return {requested}
    if not config.getoption("changed"):
        return all_keys
    changed = _changed_paths()
    if not changed:
        return set()
    core_prefixes = ("content_catalog.py", "challenge_registry.py", "tools/", "foundation.py")
    if any(path.startswith(core_prefixes) and not path.startswith("tools/content/") for path in changed):
        return all_keys
    selected: set[str] = set()
    for spec in catalog.enabled_maps():
        authored = {
            spec.level_config_path.relative_to(ROOT).as_posix(),
            spec.manifest_path.relative_to(ROOT).as_posix(),
            f"vanillamaps/{spec.source_file}",
        }
        if spec.onboarding_path:
            authored.add(spec.onboarding_path.relative_to(ROOT).as_posix())
        if authored & changed:
            selected.add(spec.key)
    return selected


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    catalog = load_content_catalog()
    selected = selected_map_keys(config, catalog)
    deselected: list[pytest.Item] = []
    for item in items:
        path = Path(str(item.fspath))
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        name = path.name
        if "worlds/doometernal" in str(path):
            item.add_marker(pytest.mark.apworld)
        elif name == "test_catalog_generated_maps.py":
            item.add_marker(pytest.mark.integration)
            item.add_marker(pytest.mark.slow)
        elif name.startswith("test_content_"):
            item.add_marker(pytest.mark.catalog)
        elif "generated_content" in name:
            item.add_marker(pytest.mark.generated)
        elif "generate_map(" in text or "patch_mission_complete_maps(" in text:
            item.add_marker(pytest.mark.slow)
        else:
            item.add_marker(pytest.mark.unit)
        map_spec = getattr(item, "callspec", None)
        value = getattr(map_spec, "params", {}).get("map_spec") if map_spec else None
        if value is not None and value.key not in selected:
            deselected.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if item not in deselected]


@pytest.fixture(scope="session")
def content_catalog() -> ContentCatalog:
    return load_content_catalog()


@pytest.fixture(scope="session")
def discovered_map_specs(content_catalog: ContentCatalog) -> tuple[MapSpec, ...]:
    return tuple(content_catalog.enabled_maps())


@pytest.fixture(scope="session")
def generated_content_snapshot() -> str:
    return (ROOT.parent / "Archipelago" / "worlds" / "doometernal" / "generated_content.py").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def temporary_generated_maps(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest, content_catalog: ContentCatalog,
) -> dict[str, Path]:
    """Generate each selected real map once in pytest's session-only temp tree."""
    pipeline_maps = os.environ.get("AP_PIPELINE_MAPS_JSON")
    if pipeline_maps:
        supplied = {
            key: Path(path)
            for key, path in json.loads(pipeline_maps).items()
        }
        missing = [str(path) for path in supplied.values() if not path.is_file()]
        if missing:
            raise ValueError(f"pipeline supplied missing generated maps: {missing}")
        return supplied
    cache_root = tmp_path_factory.mktemp("doom-generated-maps")
    selected = selected_map_keys(request.config, content_catalog)
    items = json.loads((ROOT / "data" / "items.json").read_text(encoding="utf-8"))
    output: dict[str, Path] = {}
    for spec in content_catalog.enabled_maps():
        if spec.key not in selected:
            continue
        digest = hashlib.sha256()
        for path in (ROOT / "vanillamaps" / spec.source_file, spec.level_config_path, spec.manifest_path):
            digest.update(path.read_bytes())
        digest.update(GENERATOR_VERSION.encode("ascii"))
        directory = cache_root / f"{spec.key}-{digest.hexdigest()[:16]}"
        directory.mkdir()
        entities = directory / spec.data["generated_output"]
        manifest = directory / f"{spec.key}.json"
        generate_map(ROOT / "vanillamaps" / spec.source_file, entities, spec.level_config_path, manifest, items)
        request.config._doom_generated_map_counts[spec.key] = request.config._doom_generated_map_counts.get(spec.key, 0) + 1
        output[spec.key] = entities
    return output
