import copy
import hashlib
import json
from pathlib import Path

import pytest

from tools.maps.encounter_population_sources import PopulationSources
from tools.maps.encounter_source_definitions import SourceDefinitions


ROOT = Path(__file__).resolve().parents[1]
DLC1 = "actorpopulation/default/dlc1"
TIER1 = "ENCOUNTER_SPAWN_ZOMBIE_TIER_1"
TIER3 = "ENCOUNTER_SPAWN_ZOMBIE_TIER_3"


@pytest.fixture
def sources(tmp_path):
    registry = json.loads((ROOT / "content/catalog/enemy_species.json").read_bytes())
    for source in registry["actorpopulation_sources"].values():
        for variant in source["variants"]:
            target = tmp_path / variant["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / variant["path"]).read_bytes())
    ancestry = "content/catalog/enemy_definition_ancestry.json"
    (tmp_path / ancestry).write_bytes((ROOT / ancestry).read_bytes())
    return registry, tmp_path


def actors():
    return {name: {"name": name, "class": "idAI2", "inherit": inherit} for name, inherit in {
        "hell": "ai/fodder/zombie_t1_hell", "tier3": "ai/fodder/zombie_tier_3",
        "soldier": "ai/fodder/soldier_blaster",
    }.items()}


def definitions(records):
    return [{"name": name} for name in records]


def test_single_source_keeps_qualified_any_path_without_loading_population_sources(tmp_path):
    assert SourceDefinitions(root=tmp_path).select([{"name": "actor"}], "ENCOUNTER_SPAWN_ANY", None, None) == "actor"


@pytest.mark.parametrize("values", [[{"name": "actor", "weight": 1}],
                                  [{"name": "actor"}, {"name": "other", "weight": 1}], ["actor"]])
def test_source_definition_fields_are_checked_before_either_selection_path(tmp_path, values):
    with pytest.raises(ValueError, match="Unqualified source definition fields"):
        SourceDefinitions(root=tmp_path).select(values, TIER1, DLC1, None)


def test_population_variants_preserve_native_source_selection_without_making_writer_indices_unique(sources):
    registry, root = sources
    selector = SourceDefinitions(registry, root)
    records = actors()
    values = definitions(records)
    assert selector.select(values, TIER1, DLC1, records.__getitem__) == "hell"
    assert selector.select(values, TIER3, DLC1, records.__getitem__) == "tier3"
    assert {len(rows) for rows in PopulationSources(registry, root).resolved_variants(DLC1)} == {33, 34}
    with pytest.raises(ValueError, match="Ambiguous actor population variant"):
        PopulationSources(registry, root).resolved(DLC1)


def test_native_dlc2_growth_preserves_source_selection_across_parent_counts_and_rejects_writer_ambiguity(sources):
    registry, root = sources
    population = 'actorpopulation/default/dlc2'
    records = actors()
    assert SourceDefinitions(registry, root).select(definitions(records), TIER1, population, records.__getitem__) == 'hell'
    with pytest.raises(ValueError, match='Ambiguous actor population variant'):
        PopulationSources(registry, root).resolved(population)


def test_native_empty_population_row_does_not_supply_a_zombie_actor_or_a_writable_assignment(sources):
    registry, root = sources
    payload = b'{ edit = { aiTypeDefAssignments = { num = 1; } } }'
    path = root / 'empty_row.decl'
    path.write_bytes(payload)
    population = 'empty_row'
    registry['actorpopulation_sources'][population] = {'variants': [{'path': path.name, 'sha256': hashlib.sha256(payload).hexdigest()}]}
    records = {'hell': actors()['hell']}
    with pytest.raises(ValueError, match='no unique native selection'):
        SourceDefinitions(registry, root).select(definitions(records), TIER1, population, records.__getitem__, native=True)
    with pytest.raises(ValueError, match='Unresolved actor population assignments'):
        PopulationSources(registry, root).resolved(population)


@pytest.mark.parametrize("defect", ["unknown", "second_match", "initial", "any"])
def test_every_source_candidate_and_reset_path_needs_qualified_unique_selection(sources, defect):
    registry, root = sources
    records = actors()
    kwargs = {}
    mask = TIER1
    if defect == "unknown":
        records["soldier"]["inherit"] = "ai/fodder/unqualified"
    elif defect == "second_match":
        records["second"] = {"name": "second", "class": "idAI2", "inherit": "ai/fodder/zombie_tier_1"}
    elif defect == "initial":
        kwargs["initial_definitions"] = [{"name": "soldier"}]
    else:
        mask = "ENCOUNTER_SPAWN_ANY"
    with pytest.raises(ValueError):
        SourceDefinitions(registry, root).select(definitions(records), mask, DLC1, records.__getitem__, **kwargs)


def test_readonly_selection_rejects_variants_with_different_unique_actors(sources):
    original, root = sources
    registry = copy.deepcopy(original)
    variants = []
    for index, inherit in enumerate(("ai/fodder/zombie_tier_1", "ai/fodder/zombie_tier_3")):
        payload = ('{ edit = { aiTypeDefAssignments = { num = 1; item[0] = { entityDef = "'
            + inherit + '"; } } } }').encode()
        path = root / f"variant_{index}.decl"
        path.write_bytes(payload)
        variants.append({"path": path.name, "sha256": hashlib.sha256(payload).hexdigest()})
    registry["actorpopulation_sources"][DLC1]["variants"] = variants
    records = actors()
    with pytest.raises(ValueError, match="variants select different"):
        SourceDefinitions(registry, root).select(definitions(records), TIER1, DLC1, records.__getitem__)


def test_equivalent_native_aliases_preserve_unique_population_selection(sources):
    registry, root = sources
    records = actors()
    records["soul"] = {"name": "soul", "class": "idAI2",
                       "inherit": registry["species"]["lost_soul"]["inherits"][0]}
    selector = SourceDefinitions(registry, root)
    for mask in ("ENCOUNTER_DO_NOT_USE_MAX_COMMON",
                 "ENCOUNTER_SPAWN_LOST_SOUL ENCOUNTER_DO_NOT_USE_MAX_COMMON"):
        assert selector.select(definitions(records), mask, DLC1, records.__getitem__) == "soul"
    with pytest.raises(ValueError, match="one qualified native"):
        selector.select(definitions(records), "ENCOUNTER_SPAWN_LOST_SOUL | ENCOUNTER_SPAWN_IMP",
                        DLC1, records.__getitem__)
