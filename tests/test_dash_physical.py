import json
from pathlib import Path

from launcher_core import DASH_LOCATION_ID, ModCompiler, SeedManifest


FIXTURE = Path(__file__).parent / "fixtures/exultia_dash_minimal.entities"


def _manifest(enabled: bool) -> SeedManifest:
    return SeedManifest.create(
        seed_name="dash-physical",
        team=0,
        slot=1,
        options={"randomize_dash": enabled},
        active_location_ids=[DASH_LOCATION_ID] if enabled else [],
    )


def test_dash_false_preserves_vanilla_physical_pickup(tmp_path):
    output = tmp_path / "e1m2_war.entities"
    ModCompiler().compile_map(_manifest(False), FIXTURE, output)
    content = output.read_text(encoding="utf-8")
    seed = json.loads(output.with_suffix(".seed.json").read_text())
    checks = json.loads(output.with_suffix(".manifest.json").read_text())
    assert content.count("entityDef capitol_progress_dash_1") == 1
    assert "ap_independent_capitol_progress_dash_1" not in content
    assert "AP_CHECK_CAPITOL_PROGRESS_DASH_1" not in content
    assert checks == {}
    assert seed["options"]["randomize_dash"] is False
    assert DASH_LOCATION_ID not in seed["active_location_ids"]


def test_dash_true_emits_exactly_one_physical_ap_transformation(tmp_path):
    output = tmp_path / "e1m2_war.entities"
    ModCompiler().compile_map(_manifest(True), FIXTURE, output)
    content = output.read_text(encoding="utf-8")
    seed = json.loads(output.with_suffix(".seed.json").read_text())
    checks = json.loads(output.with_suffix(".manifest.json").read_text())
    assert "entityDef capitol_progress_dash_1" not in content
    assert content.count("entityDef ap_independent_capitol_progress_dash_1") == 1
    assert content.count("entityDef AP_CHECK_CAPITOL_PROGRESS_DASH_1") == 1
    assert checks == {"AP_CHECK_CAPITOL_PROGRESS_DASH_1": DASH_LOCATION_ID}
    assert seed["options"]["randomize_dash"] is True
    assert seed["active_location_ids"] == [DASH_LOCATION_ID]
