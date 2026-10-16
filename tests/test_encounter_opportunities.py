import copy
import hashlib
import json
from pathlib import Path

import pytest

from tools.maps.verify_encounter_opportunities import verify_catalog
from tools.maps.encounter_catalog import ENTITY_HEADER, entity_records
from tools.maps.encounter_materializer import _value_span
from tools.maps.enemy_attributes import _render


def _catalog_sources(*, finite_maintenance=False, logical_operator="ENCOUNTER_LOGICAL_OP_AND", consumer_mask=None):
    def items(values):
        return {"num": len(values), **{f"item[{index}]": value for index, value in enumerate(values)}}

    def entity(name, inherit, class_name, edit):
        return f'entity {{\nentityDef {name} {{\ninherit = "{inherit}";\nclass = "{class_name}";\nedit = {_render(edit, 0)}\n}}\n}}\n'

    spawn_type = "ENCOUNTER_SPAWN_IMP"
    calls = [dict(eventDef="spawnAI", args=items([dict(eEncounterSpawnType_t=spawn_type), dict(int=2), dict(entity="group"), dict(string="cohort")])),
        dict(eventDef="waitAIRemaining", args=items([dict(eEncounterSpawnType_t=consumer_mask or spawn_type), dict(int=0), dict(string="cohort"), dict(bool=False)]))]
    if finite_maintenance:
        calls[1:] = [
            dict(eventDef="maintainAICount", args=items([dict(eEncounterSpawnType_t=spawn_type), dict(int=1), dict(int=2),
                dict(float=2), dict(float=1), dict(entity="group"), dict(string="cohort"), dict(int=4)])),
            dict(eventDef="waitMulitpleConditions", args=items([dict(int=2), dict(encounterLogicOperator_t=logical_operator), dict(bool=False)])),
            dict(eventDef="waitMaintainComplete", args=items([dict(eEncounterSpawnType_t=spawn_type), dict(int=0), dict(string="cohort"), dict(bool=False)])),
            dict(eventDef="waitAIRemaining", args=items([dict(eEncounterSpawnType_t="ENCOUNTER_SPAWN_ANY"), dict(int=0), dict(string=""), dict(bool=False)])),
        ]
    manager = entity("arena", "encounter/manager", "idEncounterManager", dict(commitTriggers=items(["commit"]),
        encounterComponent=dict(entityEvents=items([dict(entity="arena", events=items([dict(eventCall=call) for call in calls]))]))))
    source = entity("ai", "ai/fodder/imp", "idAI2", dict(aiHealth=dict(components={"components[0]": dict(max=100, starting=100)})))
    source += entity("group", "encounter/spawn_group/zone", "idTargetSpawnGroup", dict(spawners=items(["point", "point2"])))
    for index, point in enumerate(("point", "point2")):
        source += entity(point, "target/spawn", "idTarget_Spawn", dict(entityDefs=items([dict(name="ai")]),
            spawnPosition=dict(x=index, y=0, z=0), spawnEditable=dict(aiStateOverride="AIOVERRIDE_PLAY_ENTRANCE_ANIMATION" if index == 0 else "AIOVERRIDE_DEFAULT")))
    source += entity("commit", "encounter/trigger/commit", "idEncounterTrigger_Commit", {}) + manager
    records = {row["name"]: row for row in entity_records(source, include_names={match.group(1) for match in ENTITY_HEADER.finditer(source)})}
    pins = {name: dict(sha256=row["sha256"], class_name=row["class"]) for name, row in records.items()}
    pins["ai"].update(inherit="ai/fodder/imp", starting_health=100)
    pins["point"].update(spawn_override="AIOVERRIDE_PLAY_ENTRANCE_ANIMATION", protected_reason="authored_entrance_animation",
        replacement_qualification="preserve_source_species_until_entrance_animation_is_qualified")
    left, right = _value_span(manager, ("encounterComponent", "entityEvents", "item[0]", "events", "item[0]", "eventCall"))
    creation = dict(event_index=0, event_def="spawnAI", event_sha256=hashlib.sha256(manager[left:right].strip().encode()).hexdigest(),
        species="imp", spawn_type=spawn_type, count=2, label="cohort", spawner="group",
        members=[dict(spawner=point, ai="ai") for point in ("point", "point2")], replacement_constraints=[dict(event_index=1,
            event_def="waitAIRemaining", spawn_type=spawn_type, replacement_qualification="preserve_source_spawn_type")])
    creations = [creation]
    activation = dict(bindings=dict(commitTriggers=[dict(entity="commit")]), incoming_targets=[], event_gates=[])
    if finite_maintenance:
        creation["replacement_constraints"] = [dict(event_index=3, event_def="waitMaintainComplete", spawn_type=spawn_type,
            replacement_qualification="preserve_source_spawn_type")]
        maintained = copy.deepcopy(creation)
        left, right = _value_span(manager, ("encounterComponent", "entityEvents", "item[0]", "events", "item[1]", "eventCall"))
        maintained.update(event_index=1, event_def="maintainAICount", desired_count=1,
            event_sha256=hashlib.sha256(manager[left:right].strip().encode()).hexdigest(),
            count_kind="successful_new_maintenance_emissions", existing_cohort_creation_indices=[0],
            protected_reason="finite_maintenance_completion_gate",
            replacement_qualification="preserve_source_producer_until_maintenance_materialization_is_qualified")
        creations.append(maintained)
        activation.update(event_gates=[dict(event_index=2, event_def="waitMulitpleConditions", args=list(calls[2]["args"][f"item[{index}]"] for index in range(3)))],
            finite_maintenance_completion=dict(condition_group_event_index=2, maintain_wait_event_index=3,
                population_wait_event_index=4, maintenance_event_indices=[1]))
    registry = dict(species=dict(imp=dict(status="qualified", source_spawn_types=[spawn_type], inherits=["ai/fodder/imp"]),
        arachnotron=dict(status="qualified", source_spawn_types=["ENCOUNTER_SPAWN_ARACHNOTRON"], inherits=["ai/heavy/arachnotron"])))
    registry_bytes = json.dumps(registry).encode()
    logic = {"logic.decl": b"{ edit = {} }"}
    catalog = dict(schema_version=1, source=dict(sha256=hashlib.sha256(source.encode()).hexdigest()),
        species_registry_sha256=hashlib.sha256(registry_bytes).hexdigest(), entities=pins,
        logic_sources={name: dict(sha256=hashlib.sha256(body).hexdigest()) for name, body in logic.items()},
        segments=[dict(id="arena", manager="arena", script_index=0, segment_N=4 if finite_maintenance else 2,
            creations=creations, activation=activation)],
        counting=dict(map_N=None, campaign_N=None))
    return catalog, source.encode(), registry_bytes, logic


@pytest.fixture(scope="module")
def catalog_sources():
    return _catalog_sources()


@pytest.mark.parametrize("finite_maintenance", [False, True])
def test_consistent_isolated_population_is_accepted(finite_maintenance):
    catalog, source, registry, logic = _catalog_sources(finite_maintenance=finite_maintenance)
    verify_catalog(catalog, source, registry, logic_sources=logic)


def test_combined_population_mask_preserves_each_source_type():
    catalog, source, registry, logic = _catalog_sources(consumer_mask="ENCOUNTER_SPAWN_IMP ENCOUNTER_SPAWN_HELL_SOLDIER")
    verify_catalog(catalog, source, registry, logic_sources=logic)
    catalog["segments"][0]["creations"][0]["replacement_constraints"] = []
    with pytest.raises(ValueError, match="Typed consumers differ"):
        verify_catalog(catalog, source, registry, logic_sources=logic)


@pytest.mark.parametrize("completion, message", [
    ("missing", "Finite maintenance has no exhaustion proof"),
    ("or", "Maintenance completion requires AND"),
])
def test_maintenance_budget_requires_exhaustion_and_population_zero(completion, message):
    catalog, source, registry, logic = _catalog_sources(finite_maintenance=True,
        logical_operator="ENCOUNTER_LOGICAL_OP_OR" if completion == "or" else "ENCOUNTER_LOGICAL_OP_AND")
    if completion == "missing":
        catalog["segments"][0]["activation"].pop("finite_maintenance_completion")
    with pytest.raises(ValueError, match=message):
        verify_catalog(catalog, source, registry, logic_sources=logic)


def test_phase_anchor_is_pinned_to_the_source_event(catalog_sources):
    original, source, registry, logic = catalog_sources
    catalog = copy.deepcopy(original)
    creation = catalog["segments"][0]["creations"][0]
    anchor = {key: creation[key] for key in ("event_index", "event_def", "event_sha256")}
    catalog["segments"][0]["activation"]["phase_anchor"] = anchor
    verify_catalog(catalog, source, registry, logic_sources=logic)
    anchor["event_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="Phase anchor differs"):
        verify_catalog(catalog, source, registry, logic_sources=logic)


def test_curated_pins_and_population_match_authoring_sources():
    root = Path(__file__).resolve().parents[1]
    catalog = json.loads((root / "content/catalog/encounter_opportunities/e1m1_intro.json").read_bytes())
    if not (root / catalog["source"]["path"]).is_file():
        pytest.skip("Canonical game map is an external authoring input")
    logic = {name: (root / name).read_bytes() for name in catalog.get("logic_sources", {})}
    verify_catalog(catalog, (root / catalog["source"]["path"]).read_bytes(),
        (root / "content/catalog/enemy_species.json").read_bytes(), logic_sources=logic)


@pytest.mark.parametrize("mutation, message", [
    ("count", "Finite count differs"),
    ("member", "Group member coverage differs"),
    ("species", "Unqualified species mask"),
    ("registry", "Species/template binding differs"),
    ("consumer", "Typed consumers differ"),
    ("logic", "Logic declaration pin differs"),
    ("binding", "Unresolved binding is declared"),
    ("animation", "Entrance animation protection differs"),
])
def test_inconsistent_catalog_cannot_supply_opportunities(catalog_sources, mutation, message):
    original, source, registry_bytes, original_logic = catalog_sources
    catalog = copy.deepcopy(original)
    logic = original_logic.copy()
    creation = next(row for segment in catalog["segments"] for row in segment["creations"] if row["event_def"] == "spawnAI")
    if mutation == "count":
        creation["count"] += 1
    elif mutation == "member":
        creation["members"].pop()
    elif mutation == "species":
        creation["species"] = "arachnotron"
    elif mutation == "registry":
        registry = json.loads(registry_bytes)
        registry["species"][creation["species"]]["inherits"] = []
        registry_bytes = json.dumps(registry).encode("utf-8")
        catalog["species_registry_sha256"] = hashlib.sha256(registry_bytes).hexdigest()
    elif mutation == "consumer":
        constrained = next(row for segment in catalog["segments"] for row in segment["creations"] if row["replacement_constraints"])
        constrained["replacement_constraints"] = []
    elif mutation == "binding":
        resolved = next(binding for segment in catalog["segments"] for binding in segment["activation"]["bindings"]["commitTriggers"] if binding.get("source_resolved") is not False)
        resolved["source_resolved"] = False
    elif mutation == "animation":
        animated = next(pin for pin in catalog["entities"].values() if pin.get("protected_reason") == "authored_entrance_animation")
        animated.pop("protected_reason")
    else:
        name = next(iter(logic))
        logic[name] += b" "
    with pytest.raises(ValueError, match=message):
        verify_catalog(catalog, source, registry_bytes, logic_sources=logic)
