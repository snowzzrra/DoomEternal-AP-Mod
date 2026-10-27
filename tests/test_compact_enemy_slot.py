import json
from pathlib import Path

from doom_eap.content.enemy_options import normalize_enemy_options
from doom_eap.content.native_enemy_plan import build_native_recipes, native_recipe_seed
from doom_eap.launcher.launcher_core import ModCompiler, RoomSnapshot, SeedManifest
from doom_eap.runtime.context_registry import validate_slot_contract


def test_compact_room_reconstructs_same_plan_in_bridge_and_launcher():
    root = Path(__file__).resolve().parents[1]
    slot = json.loads((root / "tests/fixtures/full_campaign_slot_data.json").read_text())
    slot["enemy_randomizer"] = True
    slot["enemies"] = normalize_enemy_options(slot)
    plan = build_native_recipes(slot["enemies"], slot["campaign_plan"], seed=73822)
    slot["enemy_plan"] = native_recipe_seed(plan)
    slot["required_capabilities"].append("enemy_recipe_seed_v1")
    encoded = json.dumps(slot)
    assert len(encoded.encode()) < 1024 * 1024
    assert validate_slot_contract(slot)["enemy_plan"] == plan
    known = ModCompiler(root).known_location_ids()
    placements = [dict(location_id=code, location_name=f"Location {code}", item_id=7770004,
        item_name="Ballista", recipient_slot=1, recipient_name="Fixture", classification=1,
        trap=False, local=True) for code in sorted(known)]
    snapshot = RoomSnapshot.from_packets({"seed_name": "compact-fixture"}, dict(
        team=0, slot=1, slot_data=slot, missing_locations=sorted(known), checked_locations=[], placements=placements))
    assert snapshot.slot_data["enemy_plan"] == plan
    manifest = SeedManifest.from_room(snapshot, known)
    assert manifest.options["enemy_plan"] == plan
    assert json.loads(encoded) == slot
    assert validate_slot_contract({**slot, "enemy_plan": plan})["enemy_plan"] == plan
