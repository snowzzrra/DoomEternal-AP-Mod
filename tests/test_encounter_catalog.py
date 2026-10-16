import pytest

from tools.maps.encounter_catalog import extract_map, parse_object


def encounter(events, *, layers="", name="arena"):
    return f'''entity {{
{layers}
entityDef {name} {{
inherit = "encounter/manager";
class = "idEncounterManager";
edit = {{
encounterComponent = {{
entityEvents = {{ num = 1; item[0] = {{ entity = "{name}"; events = {{ {events} }} }} }}
}}
}}
}}
}}
'''


@pytest.mark.parametrize("maximum", (-2, -1, 0, 5))
def test_population_requests_keep_renewable_cohorts_and_alternatives_unresolved(maximum):
    events = '''num = 3;
item[0] = { eventCall = { eventDef = "spawnSingleAI"; args = { num = 0; } } }
item[1] = { eventCall = { eventDef = "maintainAICount"; args = {
num = 6; item[0] = { eEncounterSpawnType_t = "ENCOUNTER_SPAWN_IMP"; }
item[1] = { int = 3; } item[2] = { int = -1; } item[3] = { float = 2; }
item[4] = { int = 1; } item[5] = { entity = "fodder_group"; }
} } }
item[2] = { eventCall = { eventDef = "waitKillCount"; args = { num = 1; item[0] = { int = 2; } } } }
'''
    events = events.replace("int = -1;", f"int = {maximum};")
    text = encounter(events, layers='layers { "game/sp/test/master_level" }') + encounter(events, name="alternate")
    result = extract_map(text, "test")
    first, second = result["encounters"]
    assert first["id"] != second["id"]
    assert first["layers"] == ["game/sp/test/master_level"]
    assert first["population_requests"][0]["requested_count"] == 1
    renewable = first["population_requests"][1]
    assert renewable["kind"] == ("renewable" if maximum <= 0 else "finite_maintenance")
    assert renewable["maximum_spawn_count"] == maximum
    assert renewable["initial_cohort"] is None
    assert first["finite_kill_budget"] is None
    assert first["scripts"][0]["events"]["item[2]"]["eventCall"]["eventDef"] == "waitKillCount"


def test_protection_is_scoped_to_occurrence():
    text = encounter("num = 0;", name="boss") + encounter("num = 0;", name="ordinary")
    result = extract_map(text, "test", profiles={"boss": {"category": "boss", "protection": "Mandatory sequence", "evidence": "reviewed"}})
    boss, ordinary = result["encounters"]
    assert boss["protection"] == "Mandatory sequence"
    assert ordinary["protection"] is None


def test_parser_rejects_duplicate_properties_instead_of_discarding_events():
    with pytest.raises(ValueError, match="Duplicate entity property"):
        parse_object('num = 1; num = 2;')


def test_links_keep_completion_chain_cycles_and_instance_spawn_properties():
    def entity(name, kind, properties):
        return f'''entity {{ entityDef {name} {{
inherit = "test/{name}"; class = "{kind}";
edit = {{ {properties} }}
}} }}
'''

    text = encounter('''num = 2;
item[0] = { eventCall = { eventDef = "spawnSingleAI";
args = { num = 1; item[0] = { entity = "point"; } } } }
item[1] = { eventCall = { eventDef = "activate";
args = { num = 1; item[0] = { entity = "completion"; } } } }
''')
    text += entity("point", "idTarget_Spawn", 'entityDefs = { num = 1; item[0] = { name = "actor"; } }')
    text += entity("actor", "idAI2", 'aiHealth = { components = { num = 1; item[0] = { max = 200; starting = 150; } } }')
    text += entity("completion", "idLogicRelay", 'targets = { num = 1; item[0] = "door"; }')
    text += entity("door", "idMover", 'targets = { num = 1; item[0] = "completion"; }')
    text += entity("outside_counter", "idTarget_Count", 'count = 2; targets = { num = 1; item[0] = "completion"; }')
    text += entity("outside_trigger", "idTrigger", 'targets = { num = 1; item[0] = "outside_counter"; }')
    text += entity("unrelated", "idStatic", 'unsupported syntax')
    result = extract_map(text, "test")
    linked = {record["name"]: record for record in result["linked_entities"]}
    assert set(linked) == {"completion", "door", "outside_counter", "outside_trigger"}
    assert linked["completion"]["entity_references"] == [
        {"path": "targets.item[0]", "entity": "door"}
    ]
    assert linked["outside_counter"]["properties"]["count"] == 2
    assert linked["outside_trigger"]["entity_references"] == [
        {"path": "targets.item[0]", "entity": "outside_counter"}
    ]
    assert result["spawns"][0]["entity_references"] == [
        {"path": "entityDefs.item[0].name", "entity": "actor"}
    ]
    health = result["ai_templates"][0]["properties"]["aiHealth"]["components"]["item[0]"]
    assert health == {"max": 200, "starting": 150}
    assert result["encounters"][0]["completion_qualified"] is False
