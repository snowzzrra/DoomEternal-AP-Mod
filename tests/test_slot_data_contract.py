import json
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
    DigestError,
    ExpansionError,
    PayloadSizeError,
    ReferenceError,
    StructureError,
    canonical_bytes,
    catalog_digest,
    generation_fingerprint,
    placement_digest,
    recipe_digest,
    validate_slot_data,
)


def sample_catalog():
    return {"stages": {"a": {"access_id": 7771000}}, "maps": {"a": {"stage_id": "a"}}, "items": [7770001],
            "spend_groups": {"room": 2}, "battery_surplus_percent": 25,
            "templates": {"imp": {}, "zombie": {}}, "sources": {"0" * 16: {"stage_id": "a"}, "1" * 16: {"stage_id": "a"}},
            "roles": ["primary"], "spawn_types": ["imp"], "species": ["imp"],
            "timers": {"2" * 16: {"stage_id": "a", "entity": "timer"}}, "finite_sources": {"3" * 16: {"stage_id": "a", "source_id": "kill"}},
            "counters": ["imp"], "masteries": {"challenge": {}}}


def sample_recipes():
    return {"enabled": True, "limit_sets": [{"special": [1, 1, 1, 2], "population": None}],
            "profiles": [["imp", 100, 100, 100, 100], ["zombie", 100, 100, 100, 100]],
            "patterns": [[[0, 2], [1, 1], [0, 1]]],
            "maps": {"a": {"rows": [["0" * 16, "primary", "imp", 0, 4, 4, 4, False, 4, 4, [0, 0, 0, 0]]],
                           "passthrough_sources": ["1" * 16], "disabled_sources": []}}, "timers": [["2" * 16, 15000]]}


def sign_slot(data, catalog):
    data["identity"]["catalog_digest"] = catalog_digest(catalog)
    data["identity"]["recipe_digest"] = recipe_digest(data["recipes"])
    data["native_generation_fingerprint"] = generation_fingerprint(data)
    return data


def sample_slot(catalog=None, recipes=None):
    catalog = sample_catalog() if catalog is None else catalog
    data = {
        "identity": {"schema": 7, "algorithm_revision": 1, "catalog_digest": "", "recipe_digest": ""},
        "compatibility": {"apworld_version": "0.8.0", "minimum_client_version": "0.8.0", "bridge_protocol": 4,
                          "manifest_schema_version": 2, "mod_contract_revision": 1, "required_capabilities": []},
        "campaign": {"mode": "full", "sequence": ["a"], "starting_stages": ["a"], "master_level_stage_ids": [],
                     "unprepared_start_stages": [], "opening_stage": None, "goal_as_item": True,
                     "active_masteries_count": 0,
                     "access_items": [7771000], "fixed_dash_completion_stage": None,
                     "goal": {"kind": "stage", "stage_id": "a", "count": 1, "endpoint_event": "complete",
                              "additional_requirements": []},
                     "fortress": {"active_spend_group_ids": ["room"], "phase_thresholds": [0, 1],
                                  "battery_economy": {"cost": 2, "surplus": 0, "total": 2, "surplus_percent": 25,
                                                      "native_first_battery": False}}},
        "starting": {"items": [[7770001, 1]], "weapon_item_id": 7770001},
        "runtime": {"difficulty": {"engine": 2, "death_policy": "normal"},
                    "death_link": {"enabled": False, "mode": "soft"}, "use_dlc_content": True,
                    "randomize_chainsaw": True, "randomize_dash": True, "randomize_first_battery": True,
                    "enhanced_melee_damage": False, "easier_secret_encounters": True,
                    "reveal_ap_locations_on_automap": False,
                    "checks": {"include_slayer_gates": True, "include_secret_encounters": True,
                               "include_weapon_mastery_challenges": True},
                    "special_weapon": "The Crucible", "mastery_requirement_counts": {"challenge": 2}},
        "recipes": sample_recipes() if recipes is None else recipes,
        "stage_profiles": {"a": {"cr_min": 10, "cr_max": 20, "opening_cr_min": None, "opening_cr_max": None,
                                 "counter_ids": ["imp"], "opening_counter_ids": [], "renewable_ammo": False,
                                 "native_chainsaw_before_combat": False, "skulls": 2, "effort_per_check_milli": 1000}},
        "killsanity": {"interval": 0, "count": 0, "finite_supported_count": 0, "finite_kills": 0,
                       "estimated_kills": 0, "sources": []},
        "tracker_options": {"start_inventory": {}, "start_inventory_from_pool": {}, "difficulty": 2},
        "placement_digest": placement_digest([[2_200_000_001, 7770001, 1]]),
    }
    return sign_slot(data, catalog)


def set_path(data, path, value):
    for key in path[:-1]:
        data = data[key]
    data[path[-1]] = value


def test_canonical_order_and_ascii_are_stable():
    assert canonical_bytes({"z": [2, 1], "a": "é"}) == b'{"a":"\\u00e9","z":[2,1]}'
    assert canonical_bytes({"a": 1, "b": {"c": 3, "d": 4}}) == canonical_bytes({"b": {"d": 4, "c": 3}, "a": 1})
    with pytest.raises(StructureError):
        canonical_bytes({"a": float("nan")})


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


def test_digests_cover_every_block_and_preserve_order():
    catalog = sample_catalog()
    data = sample_slot(catalog)
    original = generation_fingerprint(data)
    for key in data:
        if key == "native_generation_fingerprint":
            continue
        changed = deepcopy(data)
        changed[key] = [changed[key]]
        assert generation_fingerprint(changed) != original
    for key in data["identity"]:
        changed = deepcopy(data)
        changed["identity"][key] = "changed"
        assert generation_fingerprint(changed) != original
    changed = deepcopy(data["recipes"])
    changed["patterns"][0].reverse()
    assert recipe_digest(changed) != recipe_digest(data["recipes"])
    catalog["templates"]["imp"]["asset"] = "other"
    assert catalog_digest(catalog) != data["identity"]["catalog_digest"]


@pytest.mark.parametrize(("field", "value"), [
    ("unprepared_start_stages", ["a"]), ("opening_stage", "a"),
    ("goal_as_item", False), ("active_masteries_count", 1),
])
def test_campaign_decisions_round_trip_and_change_fingerprint(field, value):
    catalog = sample_catalog()
    data = sample_slot(catalog)
    before = canonical_bytes(data)
    fingerprint = data["native_generation_fingerprint"]
    data["campaign"][field] = value
    assert canonical_bytes(data) != before
    assert generation_fingerprint(data) != fingerprint
    if field == "goal_as_item":
        data["campaign"]["access_items"] = []
    with pytest.raises(DigestError):
        validate_slot_data(data, catalog)
    sign_slot(data, catalog)
    result = validate_slot_data(json.loads(canonical_bytes(data)), catalog)
    assert getattr(result.campaign, field) == (tuple(value) if isinstance(value, list) else value)


@pytest.mark.parametrize("field", ["unprepared_start_stages", "opening_stage", "goal_as_item", "active_masteries_count"])
def test_campaign_decisions_are_required(field):
    data = sample_slot()
    del data["campaign"][field]
    with pytest.raises(StructureError, match="campaign has missing or unknown fields"):
        validate_slot_data(data, sample_catalog())


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


def test_unprepared_starts_must_be_sorted():
    catalog = sample_catalog()
    catalog["stages"]["b"] = {"access_id": 7771001}
    catalog["maps"]["b"] = {"stage_id": "b"}
    data = sample_slot(catalog)
    data["campaign"].update(sequence=["a", "b"], starting_stages=["a", "b"],
                            unprepared_start_stages=["b", "a"])
    with pytest.raises(StructureError, match="campaign.unprepared_start_stages must be sorted"):
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


@pytest.mark.parametrize("path", [
    ("identity", "schema"), ("identity", "algorithm_revision"), ("runtime", "difficulty", "engine"),
    ("starting", "items", 0, 1), ("recipes", "profiles", 0, 0), ("recipes", "patterns", 0, 0, 1),
    ("recipes", "maps", "a", "rows", 0, 4), ("recipes", "timers", 0, 1), ("killsanity", "count"),
    ("stage_profiles", "a", "skulls"), ("tracker_options", "difficulty"),
    ("campaign", "active_masteries_count"),
])
def test_bool_is_not_an_integer(path):
    data = sample_slot()
    set_path(data, path, True)
    with pytest.raises(StructureError):
        validate_slot_data(data, sample_catalog())


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


@pytest.mark.parametrize(("path", "value"), [
    (("campaign", "fortress", "battery_economy", "total"), 3),
    (("campaign", "fortress", "battery_economy", "cost"), 0),
    (("campaign", "fortress", "battery_economy", "surplus"), 2),
    (("campaign", "active_masteries_count"), -1), (("campaign", "active_masteries_count"), 2),
    (("campaign", "goal_as_item"), False), (("campaign", "goal_as_item"), 1),
    (("campaign", "unprepared_start_stages"), "a"), (("campaign", "unprepared_start_stages"), [1]),
    (("campaign", "unprepared_start_stages"), ["a"] * (MAX_COLLECTION + 1)),
    (("campaign", "opening_stage"), 1),
    (("recipes", "maps", "a", "rows", 0, 6), 5),
    (("recipes", "maps", "a", "rows", 0, 9), 0), (("killsanity", "count"), 2001),
    (("killsanity", "estimated_kills"), 1), (("stage_profiles", "a", "cr_min"), 21),
])
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


def test_revision_is_recorded_and_capabilities_are_checked():
    catalog = sample_catalog()
    data = sample_slot(catalog)
    data["identity"]["algorithm_revision"] = 999
    data["compatibility"]["required_capabilities"] = ["restart_stage"]
    sign_slot(data, catalog)
    with pytest.raises(StructureError, match="unsupported capabilities"):
        validate_slot_data(data, catalog)
    assert validate_slot_data(data, catalog, supported_capabilities=frozenset({"restart_stage"})).identity.algorithm_revision == 999


@pytest.mark.parametrize(("path", "value"), [
    (("runtime", "difficulty", "engine"), 2.0), (("recipes", "enabled"), 1),
    (("recipes", "profiles", 0), [0, 100, 100, 100, 100, 0]),
    (("recipes", "maps", "a", "rows", 0), [0]), (("recipes", "timers"), "invalid"),
    (("campaign", "goal", "stage_id"), 1), (("runtime", "difficulty", "death_policy"), "unknown"),
])
def test_wire_types_shapes_and_collection_limits(path, value):
    data = sample_slot()
    set_path(data, path, value)
    with pytest.raises(StructureError):
        validate_slot_data(data, sample_catalog())


@pytest.mark.parametrize(('path', 'value'), [
    (("recipes", "profiles", 0, 0), "x" * 97),
    (("recipes", "profiles", 0, 0), ""),
    (("recipes", "maps", "a", "rows", 0, 0), "0" * 15),
    (("recipes", "maps", "a", "rows", 0, 0), "0" * 17),
    (("recipes", "maps", "a", "rows", 0, 0), "A" * 16),
    (("recipes", "maps", "a", "rows", 0, 0), 0),
    (("recipes", "maps", "a", "rows", 0, 1), "x" * 97),
    (("recipes", "maps", "a", "rows", 0, 2), "x" * 97),
    (("recipes", "maps", "a", "passthrough_sources"), ["bad"]),
])
def test_recipe_string_references_have_bounds_and_source_hash_format(path, value):
    data = sample_slot()
    set_path(data, path, value)
    with pytest.raises(StructureError):
        validate_slot_data(data, sample_catalog())


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


def test_kill_totals_fit_the_native_counter_and_disabled_interval_is_retained():
    catalog = sample_catalog()
    data = sample_slot(catalog)
    data["killsanity"]["interval"] = 100
    assert validate_slot_data(sign_slot(data, catalog), catalog).killsanity.count == 0
    data["killsanity"].update(count=2000, finite_supported_count=2000, finite_kills=1 << 40,
                             estimated_kills=1 << 40, sources=[["3" * 16, 1 << 40]])
    assert validate_slot_data(sign_slot(data, catalog), catalog).killsanity.finite_kills == 1 << 40


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


@pytest.mark.parametrize(("table", "value"), [("sources", [1]), ("items", [True]), ("stages", [])])
def test_invalid_catalog_tables_raise_typed_errors(table, value):
    catalog = sample_catalog()
    data = sample_slot(catalog)
    catalog[table] = value
    with pytest.raises(StructureError):
        validate_slot_data(data, catalog)


def test_static_map_recipes_have_no_campaign_access_or_stage_profile():
    catalog = sample_catalog()
    catalog["maps"]["hub"] = {"stage_id": None}
    catalog["sources"]["2" * 16] = {"stage_id": "hub"}
    catalog["timers"]["4" * 16] = {"stage_id": "hub"}
    data = sample_slot(catalog)
    data["recipes"]["maps"]["hub"] = {"rows": [], "passthrough_sources": ["2" * 16], "disabled_sources": []}
    data["recipes"]["timers"].append(["4" * 16, 5000])
    result = validate_slot_data(sign_slot(data, catalog), catalog)
    assert set(result.stage_profiles) == {"a"}
    assert "hub" in result.recipes.maps


def test_tracker_snapshot_preserves_signed_option_values():
    catalog = sample_catalog()
    data = sample_slot(catalog)
    data["tracker_options"].update(enemy_count=-1, kill_rewards=-2, enabled_species=["imp", "zombie"])
    result = validate_slot_data(sign_slot(data, catalog), catalog)
    assert result.tracker_options["enemy_count"] == -1
    assert result.tracker_options["kill_rewards"] == -2
    assert result.tracker_options["enabled_species"] == ("imp", "zombie")


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
