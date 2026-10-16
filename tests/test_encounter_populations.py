import copy
import hashlib
import json
from pathlib import Path

import pytest

from tools.maps.ap_map_generator import find_entity_block_bounds
from tools.maps.encounter_catalog import entity_records, parse_object
from tools.maps.encounter_populations import (
    PRIVATE_POPULATION_ASSETSINFO_PREFIX, PRIVATE_POPULATION_PREFIX, materialize_frozen_populations,
)


ROOT = Path(__file__).resolve().parents[1]
BASE = "actorpopulation/default/default_no_bosses"
DLC2 = "actorpopulation/default/dlc2"


def entity(name, class_name, inherit="", population=None):
    field = f'aiTypeDefAssignments = "{population}";' if population else ""
    return (f'entity {{ entityDef {name} {{ inherit = "{inherit}"; class = "{class_name}"; '
            f'edit = {{ {field} }} }} }}\n')


def fixture_plan(*, species="stone", managers=("arena",)):
    inherit, mask = ("ai/fodder/imp_stone", "ENCOUNTER_SPAWN_STONE_IMP") if species == "stone" else (
        "ai/fodder/imp", "ENCOUNTER_SPAWN_IMP")
    text = "".join(entity(name, "idEncounterManager", population=BASE) for name in (*managers, "unselected"))
    text += entity("private_ai", "idAI2", inherit)
    instance = {"ai_name": "private_ai", "spawn_type": mask, "species": species}
    plan = {"map_key": "e1m1_intro", "operations": [
        {"manager": {"entity": name}, "waves": [{"instances": [instance, dict(instance)]}]} for name in managers
    ]}
    return text, plan


@pytest.fixture
def sources(tmp_path):
    registry = json.loads((ROOT / "content/catalog/enemy_species.json").read_text())
    for source in registry["actorpopulation_sources"].values():
        for variant in source["variants"]:
            target = tmp_path / variant["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / variant["path"]).read_bytes())
    return registry, tmp_path


def block(text, name):
    start, end = find_entity_block_bounds(text, name)
    return text[start:end]


def test_private_population_keeps_native_assignments_and_adds_only_planned_stone(sources):
    registry, root = sources
    text, plan = fixture_plan()
    output, declarations = materialize_frozen_populations(text, plan, registry, root)
    assert materialize_frozen_populations(text, plan, registry, root) == (output, declarations)
    member, payload = next((name, data) for name, data in declarations.items() if name.startswith(PRIVATE_POPULATION_PREFIX))
    assert len(declarations) == 2
    document = parse_object(payload.decode().strip()[1:-1])
    assert document["inherit"] == BASE
    assert document["edit"]["aiTypeDefAssignments"] == {
        "num": 30,
        "item[29]": {"entityDef": "ai/fodder/imp_stone", "aiType": "POPULATION_TYPE_STONE_IMP",
                     "defaultSensorySettings": "common"},
    }
    selected, = entity_records(output, include_names={"arena"})
    assert selected["edit"]["aiTypeDefAssignments"] == "actorpopulation/ap_v070/" + Path(member).stem
    metadata, = (data for name, data in declarations.items() if name.startswith(PRIVATE_POPULATION_ASSETSINFO_PREFIX))
    assert json.loads(metadata) == {"assets": [{
        "name": selected["edit"]["aiTypeDefAssignments"], "resourceType": "rs_streamfile",
        "mapResourceType": "actorPopulation", "version": 0,
        "specialByte1": 0, "specialByte2": 0, "specialByte3": 0,
    }]}
    assert block(output, "unselected") == block(text, "unselected")
    assert block(output, "private_ai") == block(text, "private_ai")


def test_population_is_private_to_each_selected_manager(sources):
    registry, root = sources
    text, plan = fixture_plan(managers=("arena", "other_arena"))
    output, declarations = materialize_frozen_populations(text, plan, registry, root)
    assert len(declarations) == 4
    names = {record["edit"]["aiTypeDefAssignments"]
             for record in entity_records(output, include_names={"arena", "other_arena"})}
    assert len(names) == 2 and BASE not in names
    assert block(output, "unselected") == block(text, "unselected")


def test_existing_population_and_empty_plan_need_no_private_resources(sources):
    registry, root = sources
    text, plan = fixture_plan(species="imp")
    assert materialize_frozen_populations(text, plan, registry, root) == (text, {})
    assert materialize_frozen_populations(text, {"operations": []}, {}, root) == (text, {})


@pytest.mark.parametrize("population", ["actorpopulation/default/dlc1", DLC2])
def test_population_present_in_every_native_variant_needs_no_write(sources, population):
    registry, root = sources
    text, plan = fixture_plan(species="imp")
    text = text.replace(BASE, population)
    assert materialize_frozen_populations(text, plan, registry, root) == (text, {})


def test_population_present_in_only_some_variants_still_requires_unique_writer(sources):
    registry, root = sources
    values = registry["actorpopulation_sources"][DLC2]["variants"]
    document = parse_object((root / values[0]["path"]).read_text().strip()[1:-1])
    count = document["edit"]["aiTypeDefAssignments"]["num"]
    payload = ('{ inherit = "' + document["inherit"] + '"; edit = { aiTypeDefAssignments = { num = '
               + str(count) + '; item[3] = { entityDef = "ai/fodder/other"; }; } } }').encode()
    path = root / "missing-imp.decl"
    path.write_bytes(payload)
    values.append(dict(path=path.relative_to(root).as_posix(), sha256=hashlib.sha256(payload).hexdigest()))
    text, plan = fixture_plan(species="imp")
    text = text.replace(BASE, DLC2)
    with pytest.raises(ValueError, match="Ambiguous actor population variant"):
        materialize_frozen_populations(text, plan, registry, root)


def test_writer_rejects_native_dlc1_variants_with_different_assignment_indices(sources):
    registry, root = sources
    text, plan = fixture_plan()
    text = text.replace(BASE, "actorpopulation/default/dlc1")
    with pytest.raises(ValueError, match="Ambiguous actor population variant"):
        materialize_frozen_populations(text, plan, registry, root)


@pytest.mark.parametrize("include_stone", [False, True])
def test_native_default_zombie_assignment_is_retained_without_duplicate(sources, include_stone):
    registry, root = sources
    base = registry["actorpopulation_sources"][BASE]["variants"][0]
    document = parse_object((root / base["path"]).read_text().strip()[1:-1])
    zombie = document["edit"]["aiTypeDefAssignments"]["item[0]"]
    assert "aiType" not in zombie
    text = entity("arena", "idEncounterManager", population=BASE)
    text += entity("private_zombie", "idAI2", zombie["entityDef"])
    instances = [{"species": "zombie_tier_1", "ai_name": "private_zombie",
                  "spawn_type": "ENCOUNTER_SPAWN_ZOMBIE_TIER_1"}]
    if include_stone:
        text += entity("private_stone", "idAI2", "ai/fodder/imp_stone")
        instances.append({"species": "stone_imp", "ai_name": "private_stone",
                          "spawn_type": "ENCOUNTER_SPAWN_STONE_IMP"})
    plan = {"map_key": "e1m1_intro", "operations": [
        {"manager": {"entity": "arena"}, "waves": [{"instances": instances}]}]}
    output, declarations = materialize_frozen_populations(text, plan, registry, root)
    if include_stone:
        declaration, = (data for member, data in declarations.items() if member.startswith(PRIVATE_POPULATION_PREFIX))
        private = parse_object(declaration.decode().strip()[1:-1])
        assert private["inherit"] == BASE
        assignments = private["edit"]["aiTypeDefAssignments"]
        assert set(assignments) == {"num", "item[29]"}
        assert assignments["num"] == 30
        assert assignments["item[29]"]["entityDef"] == "ai/fodder/imp_stone"
    else:
        assert output == text and declarations == {}


@pytest.mark.parametrize("defect", ["hash", "base_variant", "candidate_variant", "missing_assignment"])
def test_population_rejects_unqualified_sources_instead_of_changing_spawn_semantics(sources, defect):
    original, root = sources
    registry = copy.deepcopy(original)
    text, plan = fixture_plan()
    if defect == "hash":
        variant = registry["actorpopulation_sources"][DLC2]["variants"][0]
        (root / variant["path"]).write_bytes(b"drift")
        message = "hash mismatch"
    elif defect.endswith("variant"):
        population = BASE if defect == "base_variant" else DLC2
        values = registry["actorpopulation_sources"][population]["variants"]
        values.append(dict(values[0]))
        message = "Ambiguous actor population variant"
    else:
        registry["population_encounter_conversion"]["values"].pop("POPULATION_TYPE_STONE_IMP")
        message = "Missing or ambiguous population assignment"
    with pytest.raises(ValueError, match=message):
        materialize_frozen_populations(text, plan, registry, root)
