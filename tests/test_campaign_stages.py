import json
import shutil
from dataclasses import replace

import pytest

from doom_eap.content.campaign_stages import campaign_stages
from doom_eap.content.content_catalog import load_content_catalog

PUBLISHED_STAGES = (
    "e1m1_intro",
    "e1m2_war",
    "e1m3_cult",
    "e1m4_boss",
    "e2m1_nest",
    "e2m2_base",
    "e2m3_core",
    "e2m4_boss",
    "e3m1_slayer",
    "e3m2_hell",
    "e3m2_hell_b",
    "e3m3_maykr",
    "e3m4_boss",
    "e4m1_rig",
    "e4m2_swamp",
    "e4m3_mcity",
    "e5m1_spear",
    "e5m2_earth",
    "e5m3_hell",
    "e5m4_boss",
)
PUBLISHED_MASTER_STAGES = {
    "ml_e2m1_nest": (7776001, 28),
    "ml_e2m3_core": (7776002, 29),
    "ml_e5m1_spear": (7776003, 30),
    "ml_e1m3_cult": (7776004, 31),
    "ml_e2m2_base": (7776005, 32),
    "ml_e3m1_slayer": (7776006, 33),
}


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
    shutil.copyfile(catalog.root / "content/catalog/master_levels.json", path.with_name("master_levels.json"))
    return replace(catalog, root=tmp_path)


def _assert_stage_order_and_ids(masters, normal, original, reordered):
    assert [stage["id"] for stage in reordered] == [stage["id"] for stage in [*reversed(normal), *masters]]
    assert {stage["id"]: stage for stage in reordered} == {stage["id"]: stage for stage in original}


def test_descriptor_order_changes_chronology_without_changing_ids(catalog):
    original = campaign_stages(catalog)
    maps = {key: replace(spec, data={**spec.data, "order": -spec.data["order"]})
            for key, spec in reversed(catalog.maps.items())}
    reordered = campaign_stages(replace(catalog, maps=maps))
    normal = [stage for stage in original if stage["variant"] == "mission"]
    masters = [stage for stage in original if stage["variant"] == "master"]
    _assert_stage_order_and_ids(masters, normal, original, reordered)


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
    assert [stage["id"] for stage in stages] == [*PUBLISHED_STAGES[1:], *PUBLISHED_MASTER_STAGES]
    assert (stages[0]["access_id"], stages[0]["native_index"]) == (7771001, 1)


@pytest.mark.parametrize(("field", "value"), [
    ("status", "invalid"), ("access_id", True), ("access_id", 0),
    ("native_index", 1.0), ("native_index", -1),
])
def test_invalid_stage_identity_is_rejected(catalog, tmp_path, stage_ids, field, value):
    stage_ids["stages"]["e1m1_intro"][field] = value
    with pytest.raises(ValueError, match="invalid"):
        campaign_stages(_catalog_with_ids(catalog, tmp_path, stage_ids))
