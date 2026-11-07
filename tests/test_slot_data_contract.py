from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from doom_eap.content.slot_data_contract import (
    MAX_COLLECTION,
    MAX_EXPANDED_BODIES,
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    MAX_LIMIT_SETS,
    MAX_PATTERNS,
    MAX_PAYLOAD_BYTES,
    MAX_PROFILES,
    MAX_REFERENCE_ENTRIES,
    ULTRA_NIGHTMARE_CAPABILITY,
    DigestError,
    ExpansionError,
    PayloadSizeError,
    ReferenceError,
    StructureError,
    canonical_bytes,
    placement_digest,
    validate_slot_data,
)
from tests.room_fixture import sample_catalog, sample_slot, sign_slot


def set_path(data, path, value):
    for key in path[:-1]:
        data = data[key]
    data[path[-1]] = value


def test_restart_policy_requires_nightmare_and_compatible_runtime():
    catalog = sample_catalog()
    data = sample_slot(catalog)
    data["runtime"]["difficulty"] = {"engine": 3, "death_policy": "restart_stage"}
    with pytest.raises(StructureError, match="restart support"):
        validate_slot_data(sign_slot(data, catalog), catalog)
    data["compatibility"]["required_capabilities"] = [ULTRA_NIGHTMARE_CAPABILITY]
    with pytest.raises(StructureError, match="unsupported capabilities"):
        validate_slot_data(sign_slot(data, catalog), catalog)
    from doom_eap.runtime.observers import SUPPORTED_SLOT_CAPABILITIES

    support = SUPPORTED_SLOT_CAPABILITIES
    result = validate_slot_data(data, catalog, supported_capabilities=support)
    assert result.runtime.difficulty.death_policy == "restart_stage"
    data["runtime"]["difficulty"]["engine"] = 2
    with pytest.raises(StructureError, match="engine Nightmare"):
        validate_slot_data(sign_slot(data, catalog), catalog, supported_capabilities=support)


def test_admission_returns_deeply_frozen_records_and_accepts_reduced_surplus():
    catalog = sample_catalog()
    data = sample_slot(catalog)
    result = validate_slot_data(data, catalog, placements=[[2_200_000_001, 7770001, 1]])
    assert result.campaign.fortress.battery_economy.surplus == 0
    with pytest.raises(FrozenInstanceError):
        result.identity.schema = 8
    with pytest.raises(TypeError):
        result.stage_profiles["b"] = result.stage_profiles["a"]
    data["recipes"]["patterns"][0][0][1] = 99
    assert result.recipes.patterns[0][0][1] == 2


@pytest.mark.parametrize("field", ["unprepared_start_stages", "opening_stage"])
@pytest.mark.parametrize("stage", ["unknown", "inactive", "nonstart", "retired"])
def test_campaign_start_decisions_reject_other_stages(field, stage):
    catalog = sample_catalog()
    for index, key in enumerate(("inactive", "nonstart", "retired"), 1):
        catalog["stages"][key] = {"access_id": 7771000 + index}
        catalog["maps"][key] = {"stage_id": key}
    catalog["stages"]["retired"]["status"] = "retired"
    data = sample_slot(catalog)
    data["campaign"]["sequence"].append("nonstart")
    if stage == "retired":
        data["campaign"]["sequence"].append(stage)
        data["campaign"]["starting_stages"].append(stage)
    data["campaign"][field] = [stage] if field == "unprepared_start_stages" else stage
    sign_slot(data, catalog)
    with pytest.raises(ReferenceError):
        validate_slot_data(data, catalog)


def test_placements_sort_but_cover_each_value():
    rows = [[2_200_000_001, 7770001, 1], [7770002, 7770001, 2]]
    digest = placement_digest(rows)
    assert digest == placement_digest(rows[::-1])
    for column in range(3):
        changed = deepcopy(rows)
        changed[0][column] += 1
        assert placement_digest(changed) != digest
    with pytest.raises(ReferenceError):
        placement_digest([rows[0], rows[0]])


@pytest.mark.parametrize(("path", "value"), [
    (("campaign", "starting_stages"), ["missing"]), (("campaign", "sequence"), ["a", "a"]),
    (("campaign", "unprepared_start_stages"), ["a", "a"]),
    (("starting", "items"), [[7770001, 1], [7770001, 2]]),
    (("recipes", "maps", "a", "passthrough_sources"), ["0" * 16]),
    (("recipes", "profiles", 0, 0), "missing"), (("recipes", "maps", "a", "rows", 0, 10, 0), 99),
    (("recipes", "patterns", 0, 0, 0), 99), (("recipes", "maps", "a", "rows", 0, 3), 99),
    (("recipes", "timers"), [["2" * 16, 1], ["2" * 16, 2]]), (("runtime", "mastery_requirement_counts"), {"missing": 2}),
    (("recipes", "timers"), []), (("recipes", "maps", "a", "passthrough_sources"), []),
])
def test_bad_references_and_duplicate_ids_are_rejected(path, value):
    data = sample_slot()
    set_path(data, path, value)
    with pytest.raises(ReferenceError):
        validate_slot_data(data, sample_catalog())


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("campaign", "fortress", "battery_economy", "total"), 3),
        (("campaign", "fortress", "battery_economy", "cost"), 0),
        (("campaign", "fortress", "battery_economy", "surplus"), 2),
        (("campaign", "active_masteries_count"), -1),
        (("campaign", "active_masteries_count"), 2),
        (("campaign", "fortress", "battery_economy", "surplus_percent"), 50),
        (("campaign", "fortress", "battery_economy", "native_first_battery"), 1),
        (("campaign", "goal_as_item"), False),
        (("campaign", "goal_as_item"), 1),
        (("campaign", "unprepared_start_stages"), "a"),
        (("campaign", "unprepared_start_stages"), [1]),
        (("campaign", "unprepared_start_stages"), ["a"] * (MAX_COLLECTION + 1)),
        (("campaign", "opening_stage"), 1),
        (("recipes", "maps", "a", "rows", 0, 6), 5),
        (("recipes", "maps", "a", "rows", 0, 9), 0),
        (("killsanity", "count"), 2001),
        (("killsanity", "estimated_kills"), 1),
        (("stage_profiles", "a", "cr_min"), 21),
    ],
)
def test_arithmetic_and_range_errors_are_rejected(path, value):
    data = sample_slot()
    set_path(data, path, value)
    with pytest.raises(StructureError):
        validate_slot_data(data, sample_catalog())


@pytest.mark.parametrize("block", ["tracker_options", "recipes"])
def test_oversized_payload_reports_measured_total_and_recipe_bytes(block):
    data = sample_slot()
    data[block]["large"] = "x" * (MAX_PAYLOAD_BYTES + 1)
    total = len(canonical_bytes(data))
    with pytest.raises(PayloadSizeError, match=str(total)) as caught:
        validate_slot_data(data, sample_catalog())
    assert caught.value.total_bytes == total
    assert caught.value.recipe_bytes == len(canonical_bytes(data["recipes"]))
    assert str(MAX_PAYLOAD_BYTES) in str(caught.value)


@pytest.mark.parametrize("count", [MAX_EXPANDED_BODIES + 1, 0x7fffffff])
def test_pattern_expansion_is_bounded_before_decoding(count):
    data = sample_slot()
    data["recipes"]["patterns"][0][0][1] = count
    with pytest.raises(ExpansionError):
        validate_slot_data(data, sample_catalog())


def test_total_expansion_is_bounded_across_rows():
    catalog = sample_catalog()
    data = sample_slot()
    count = MAX_EXPANDED_BODIES // 2 + 1
    data["recipes"]["patterns"] = [[[0, count]]]
    row = ["0" * 16, "primary", "imp", 0, count, count, count, False, count, count, [0] * count]
    data["recipes"]["maps"]["a"] = {"rows": [row, ["1" * 16, *row[1:]]], "passthrough_sources": [], "disabled_sources": []}
    with pytest.raises(ExpansionError):
        validate_slot_data(data, catalog)


@pytest.mark.parametrize("block", ["catalog", "recipes", "generation", "placement"])
def test_admission_recomputes_digests(block):
    catalog = sample_catalog()
    data = sample_slot(catalog)
    placements = [[2_200_000_001, 7770001, 1]]
    if block == "catalog":
        catalog["templates"]["imp"]["asset"] = "other"
    elif block == "recipes":
        data["recipes"]["timers"][0][1] += 1
    elif block == "generation":
        data["tracker_options"]["difficulty"] += 1
    else:
        placements[0][2] += 1
    with pytest.raises(DigestError):
        validate_slot_data(data, catalog, placements=placements)


def test_recipe_role_matches_its_source_reference():
    catalog = sample_catalog()
    catalog['roles'].append('counter')
    data = sample_slot(catalog)
    data['recipes']['maps']['a']['rows'][0][1] = 'counter'
    with pytest.raises(ReferenceError, match='role disagrees with source'):
        validate_slot_data(sign_slot(data, catalog), catalog)


def test_battery_surplus_ceiling_allows_resolved_amounts():
    catalog = sample_catalog()
    data = sample_slot(catalog)
    economy = data["campaign"]["fortress"]["battery_economy"]
    economy.update(surplus=1, total=3)
    validate_slot_data(sign_slot(data, catalog), catalog)
    economy.update(surplus=2, total=4)
    with pytest.raises(StructureError, match="ceiling"):
        validate_slot_data(sign_slot(data, catalog), catalog)


def test_finite_kill_rewards_have_bounded_counts_and_resolved_support():
    catalog = sample_catalog()
    data = sample_slot(catalog)
    data["killsanity"] = {"interval": 1, "count": 2000, "finite_supported_count": 1999,
                         "finite_kills": 2000, "estimated_kills": 2200, "sources": [["3" * 16, 2000]]}
    admitted = validate_slot_data(sign_slot(data, catalog), catalog)
    assert admitted.killsanity.finite_supported_count == 1999
    data["killsanity"]["finite_kills"] += 1
    with pytest.raises(StructureError, match="total disagrees"):
        validate_slot_data(sign_slot(data, catalog), catalog)


def test_population_limits_and_scope_are_checked():
    catalog = sample_catalog()
    data = sample_slot(catalog)
    population = {"scope": "0123456789abcdef", "maximum": 4, "heavy": 3, "superheavy": 1, "pressure": 2,
                  "mixed": 1, "strict": True, "counter": "imp", "species_caps": [["imp", 2]]}
    data["recipes"]["limit_sets"][0]["population"] = population
    validate_slot_data(sign_slot(data, catalog), catalog)
    population["scope"] = "123"
    with pytest.raises(StructureError, match="scope"):
        validate_slot_data(sign_slot(data, catalog), catalog)
    population.update(scope="0123456789abcdef", heavy=5)
    with pytest.raises(StructureError, match="maximum"):
        validate_slot_data(sign_slot(data, catalog), catalog)


@pytest.mark.parametrize(("table", "limit"), [
    ("limit_sets", MAX_LIMIT_SETS), ("profiles", MAX_PROFILES), ("patterns", MAX_PATTERNS),
])
def test_recipe_tables_have_separate_bounds(table, limit):
    data = sample_slot()
    data["recipes"][table] = [0] * (limit + 1)
    with pytest.raises(StructureError, match=f"recipes.{table} exceeds table limit"):
        validate_slot_data(data, sample_catalog())


@pytest.mark.parametrize("table", ["items", "templates", "sources", "finite_sources", "timers"])
def test_reference_tables_have_their_own_bound(table):
    catalog = sample_catalog()
    catalog[table] = [0] * (MAX_REFERENCE_ENTRIES + 1)
    with pytest.raises(StructureError, match=f"Catalog {table} exceeds reference table limit"):
        validate_slot_data(sample_slot(), catalog)


def test_generic_collections_are_bounded():
    data = sample_slot()
    data["tracker_options"]["large"] = [0] * (MAX_COLLECTION + 1)
    with pytest.raises(StructureError, match="collection is too large"):
        validate_slot_data(data, sample_catalog())


def test_json_node_count_is_bounded_across_collections():
    data = sample_slot()
    data["tracker_options"]["large"] = [[0] * MAX_COLLECTION] * (MAX_JSON_NODES // MAX_COLLECTION + 1)
    with pytest.raises(StructureError, match="JSON node limit"):
        validate_slot_data(data, sample_catalog())


def test_json_nesting_is_bounded():
    data = sample_slot()
    nested = 0
    for _ in range(MAX_JSON_DEPTH):
        nested = [nested]
    data["tracker_options"]["deep"] = nested
    with pytest.raises(StructureError, match="nesting limit"):
        validate_slot_data(data, sample_catalog())


@pytest.mark.parametrize("limits", [[], [0] * 3, [0] * 5])
def test_limit_references_cover_each_body(limits):
    data = sample_slot()
    data["recipes"]["maps"]["a"]["rows"][0][10] = limits
    with pytest.raises(StructureError, match="cover every body"):
        validate_slot_data(data, sample_catalog())
