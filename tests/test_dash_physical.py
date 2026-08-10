import json
from pathlib import Path

from item_reconciliation import AP_RECEIPT_FEEDBACK, NATIVE_ONLY_RECEIPT_FEEDBACK
from launcher_core import DASH_LOCATION_ID, ModCompiler, SeedManifest
from tools.maps.ap_map_generator import load_item_notification_policies

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


def test_dash_false_keeps_policy_driven_item_notifications(tmp_path):
    output = tmp_path / "e1m2_war.entities"
    ModCompiler().compile_map(_manifest(False), FIXTURE, output)
    content = output.read_text(encoding="utf-8")

    assert "entityDef ap_notify_item_major_7770001_a" in content
    assert "entityDef ap_notify_item_major_7770001_b" in content
    assert "ap_notify_item_major_7770014_" not in content
    assert "ap_notify_item_filler_7770022_" not in content
    assert "entityDef ap_rpc_v3_7770014" in content
    assert "entityDef ap_rpc_v3_7770022" in content


def test_canonical_notification_policies_include_defaults_and_native_only():
    item_names, receipt_feedback = load_item_notification_policies()

    assert item_names[7770014] == "Blood Punch"
    assert receipt_feedback[7770014] == NATIVE_ONLY_RECEIPT_FEEDBACK
    assert receipt_feedback[7770022] == NATIVE_ONLY_RECEIPT_FEEDBACK
    assert receipt_feedback[7770001] == AP_RECEIPT_FEEDBACK


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
