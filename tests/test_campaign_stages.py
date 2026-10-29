import json
from dataclasses import replace

import pytest

from doom_eap.content.campaign_stages import campaign_stages
from doom_eap.content.content_catalog import load_content_catalog

PUBLISHED_STAGES = (
    "e1m1_intro", "e1m2_war", "e1m3_cult", "e1m4_boss",
    "e2m1_nest", "e2m2_base", "e2m3_core", "e2m4_boss",
    "e3m1_slayer", "e3m2_hell", "e3m2_hell_b", "e3m3_maykr", "e3m4_boss",
    "e4m1_rig", "e4m2_swamp", "e4m3_mcity",
    "e5m1_spear", "e5m2_earth", "e5m3_hell", "e5m4_boss",
)


@pytest.fixture(scope="module")
def catalog():
    return load_content_catalog()


@pytest.fixture
def stage_ids(catalog):
    return json.loads((catalog.root / "content/catalog/campaign_stage_ids.json").read_bytes())


def _catalog_with_ids(catalog, tmp_path, document):
    path = tmp_path / "content/catalog/campaign_stage_ids.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return replace(catalog, root=tmp_path)


def test_published_stage_ids_match_projection(catalog):
    stages = campaign_stages(catalog)
    assert {stage["id"]: (stage["access_id"], stage["native_index"]) for stage in stages} == {
        key: (7771000 + index, index) for index, key in enumerate(PUBLISHED_STAGES)
    }
    assert stages == json.loads((catalog.root / "data/campaign_stages.json").read_bytes())


def test_descriptor_order_changes_chronology_without_changing_ids(catalog):
    original = campaign_stages(catalog)
    maps = {key: replace(spec, data={**spec.data, "order": -spec.data["order"]})
            for key, spec in reversed(catalog.maps.items())}
    reordered = campaign_stages(replace(catalog, maps=maps))
    assert [stage["id"] for stage in reordered] == [stage["id"] for stage in reversed(original)]
    assert {stage["id"]: stage for stage in reordered} == {stage["id"]: stage for stage in original}


@pytest.mark.parametrize("field", ["access_id", "native_index"])
@pytest.mark.parametrize("status", ["active", "tombstone"])
def test_stage_numbers_cannot_be_reused(catalog, tmp_path, stage_ids, field, status):
    rows = stage_ids["stages"]
    if status == "tombstone":
        rows["retired_stage"] = {"access_id": 7771020, "native_index": 20, "status": status}
        target = rows["retired_stage"]
    else:
        target = rows["e1m2_war"]
    target[field] = rows["e1m1_intro"][field]
    with pytest.raises(ValueError, match=f"reuse {field}"):
        campaign_stages(_catalog_with_ids(catalog, tmp_path, stage_ids))


def test_retired_descriptor_keeps_numbers_reserved(catalog, tmp_path, stage_ids):
    stage_ids["stages"]["e1m1_intro"]["status"] = "tombstone"
    retired = replace(catalog, maps={key: spec for key, spec in catalog.maps.items() if key != "e1m1_intro"})
    stages = campaign_stages(_catalog_with_ids(retired, tmp_path, stage_ids))
    assert [stage["id"] for stage in stages] == list(PUBLISHED_STAGES[1:])
    assert (stages[0]["access_id"], stages[0]["native_index"]) == (7771001, 1)


@pytest.mark.parametrize("change", ["missing", "extra", "tombstone"])
def test_active_ids_must_match_descriptors(catalog, tmp_path, stage_ids, change):
    rows = stage_ids["stages"]
    if change == "missing":
        del rows["e1m1_intro"]
    elif change == "extra":
        rows["extra_stage"] = {"access_id": 7771020, "native_index": 20, "status": "active"}
    else:
        rows["e1m1_intro"]["status"] = "tombstone"
    with pytest.raises(ValueError, match="must match mission descriptors"):
        campaign_stages(_catalog_with_ids(catalog, tmp_path, stage_ids))


@pytest.mark.parametrize(("field", "value"), [
    ("status", "invalid"), ("access_id", True), ("access_id", 0),
    ("native_index", 1.0), ("native_index", -1),
])
def test_invalid_stage_identity_is_rejected(catalog, tmp_path, stage_ids, field, value):
    stage_ids["stages"]["e1m1_intro"][field] = value
    with pytest.raises(ValueError, match="invalid"):
        campaign_stages(_catalog_with_ids(catalog, tmp_path, stage_ids))
